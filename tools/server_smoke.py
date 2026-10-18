# -*- coding: utf-8 -*-
"""server_smoke.py — 线上 hh-server 端到端体检(零依赖, 只用标准库)。

为什么要有它: 2026-10-14 那次"只同步了一半代码"(线上 api/health.py 是新的、config.py 是旧的)
=> /healthz 直接 500。部署完必须有一套**跑在公网上的**断言, 而不是只看进程在不在。

用法:
    python tools/server_smoke.py                      # 打 https://hh.ei.ac.cn:51120
    python tools/server_smoke.py --base http://127.0.0.1:51120
    HH_ADMIN_TOKEN=xxx python tools/server_smoke.py   # 额外跑发版口令的两条(不上传 APK)
退出码: 0 全绿; 1 有失败。
"""
import argparse
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get('HH_SMOKE_BASE', 'https://hh.ei.ac.cn:51120')
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE      # 线上是自签/内部 CA, 这里只验"能用", 不验信任链

RESULTS = []


def _req(base, path, method='GET', body=None, headers=None, timeout=25, stream=False):
    req = urllib.request.Request(base + path, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        r = urllib.request.urlopen(req, timeout=timeout, context=CTX)
        if stream:
            head = r.read(400)
            r.close()
            return r.status, dict(r.headers), head
        data = r.read()
        return r.status, dict(r.headers), data
    except urllib.error.HTTPError as e:
        data = e.read()
        return e.code, dict(e.headers or {}), data
    except Exception as e:  # noqa
        return 0, {}, ('%s: %s' % (type(e).__name__, e)).encode('utf-8')


def chk(name, ok, detail=''):
    RESULTS.append((bool(ok), name, str(detail)[:200]))
    print('%s %-46s %s' % ('  OK ' if ok else 'FAIL', name, str(detail)[:160]))


def _local_version():
    """读 build.gradle 的 versionCode/versionName(用于 A6: 线上包是否落后于当前构建)。"""
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'mobile', 'controller-app', 'app', 'build.gradle')
    try:
        s = open(p, encoding='utf-8', errors='replace').read()
    except Exception:
        return None, ''
    import re
    vc = re.search(r'versionCode\s+(\d+)', s)
    vn = re.search(r'versionName\s+"([^"]+)"', s)
    return (int(vc.group(1)) if vc else None), (vn.group(1) if vn else '')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default=DEFAULT_BASE)
    ap.add_argument('--full', action='store_true', help='额外下载整包(~35MB, 慢)校验 sha256')
    args = ap.parse_args()
    base = args.base.rstrip('/')
    print('== hh-server 体检 @ %s ==' % base)
    t0 = time.time()

    # 1-3 健康
    st, hd, bd = _req(base, '/healthz')
    j = {}
    try:
        j = json.loads(bd.decode('utf-8'))
    except Exception:
        pass
    chk('H1 /healthz 200', st == 200, 'st=%s' % st)
    chk('H2 healthz.ok=true(不许 500)', bool(j.get('ok')), 'ok=%s' % j.get('ok'))
    chk('H3 上游 agentsvc 可达', bool((j.get('checks') or {}).get('agentsvc', {}).get('tcp')),
        (j.get('checks') or {}).get('agentsvc'))

    # 4-6 推理(非流式)
    body = json.dumps({'messages': [{'role': 'user', 'content': '只输出 JSON: {"department":"呼吸内科"}'}],
                       'model': 'auto', 'stream': False}).encode('utf-8')
    st, hd, bd = _req(base, '/v1/infer', 'POST', body, {'Content-Type': 'application/json'})
    ok = st == 200 and b'content' in bd.lower()
    chk('I1 /v1/infer 非流式 200', ok, 'st=%s len=%d' % (st, len(bd)))
    # 7 流式 SSE
    body2 = json.loads(body.decode('utf-8'))
    body2['stream'] = True
    st, hd, bd = _req(base, '/v1/infer', 'POST', json.dumps(body2).encode('utf-8'),
                      {'Content-Type': 'application/json'}, stream=True)
    ct = (hd.get('Content-Type') or hd.get('content-type') or '')
    chk('I2 /v1/infer stream=true 走 SSE', st == 200 and 'event-stream' in ct, 'st=%s ct=%s' % (st, ct))
    # 8 坏 body => 400 不是 500
    st, hd, bd = _req(base, '/v1/infer', 'POST', b'{not json', {'Content-Type': 'application/json'})
    chk('I3 坏 JSON => 400(不是 500)', st == 400, 'st=%s' % st)

    # 9-10 地理位置
    st, hd, bd = _req(base, '/v1/location?lat=31.2304&lon=121.4737')
    chk('L1 /v1/location 200', st == 200, 'st=%s len=%d' % (st, len(bd)))
    st, hd, bd = _req(base, '/v1/location?lat=abc')
    chk('L2 坏参数 => 4xx(不是 500)', 400 <= st < 500, 'st=%s' % st)

    # 11-15 APK 分发(只读)
    st, hd, bd = _req(base, '/v1/app/version?version_code=1')
    jv = {}
    try:
        jv = json.loads(bd.decode('utf-8'))
    except Exception:
        pass
    chk('A1 /v1/app/version 200', st == 200, 'st=%s' % st)
    chk('A2 老版本 => has_update=True', jv.get('has_update') is True, jv)
    st, hd, bd = _req(base, '/v1/app/list')
    jl = {}
    try:
        jl = json.loads(bd.decode('utf-8'))
    except Exception:
        pass
    items = jl.get('entries') or jl.get('items') or jl.get('apks') or []
    chk('A3 /v1/app/list 有条目', st == 200 and len(items) >= 1, 'count=%s keys=%s' % (len(items), list(jl)[:5]))
    fname = (items[0].get('file') if items else '') or 'app-debug.apk'
    st, hd, bd = _req(base, '/v1/app/download?file=%s' % fname, 'GET', None, {'Range': 'bytes=0-65535'})
    hdr_sha = (hd.get('X-Sha256') or hd.get('x-sha256') or '')
    total = (hd.get('Content-Range') or hd.get('content-range') or '').rsplit('/', 1)[-1]
    chk('A4 下载可取 + 带 X-Sha256 + 总大小合理',
        st in (200, 206) and len(bd) > 1000 and len(hdr_sha) == 64 and (total or '').isdigit() and int(total) > 1000000,
        'st=%s chunk=%d sha=%s… total=%s' % (st, len(bd), hdr_sha[:8], total))
    if args.full:
        st, hd, bd = _req(base, '/v1/app/download?file=%s' % fname, timeout=900)
        sha = hashlib.sha256(bd).hexdigest()
        chk('A4b 整包 sha256 与 X-Sha256 一致', st == 200 and sha == hdr_sha,
            'size=%d sha=%s hdr=%s' % (len(bd), sha[:12], hdr_sha[:12]))
    st, hd, bd = _req(base, '/v1/app/download?file=%s' % fname, 'GET', None, {'Range': 'bytes=0-1023'})
    cr = hd.get('Content-Range') or hd.get('content-range') or ''
    chk('A5 Range 断点续传 => 206 + Content-Range', st == 206 and cr.startswith('bytes 0-1023/'),
        'st=%s cr=%s len=%d' % (st, cr, len(bd)))

    # 16 线上已发布的 APK 是不是当前构建(自更新链路端到端有意义的前提)
    pub = (items[0].get('version_code') if items else None)
    local_vc, local_vn = _local_version()
    if local_vc is not None and pub is not None:
        chk('A6 线上已发布版本 == 本地构建版本', pub >= local_vc,
            '线上=%s(%s) 本地=%s(%s)' % (pub, (items[0].get('version_name') if items else '?'), local_vc, local_vn))
    else:
        print('  -- A6 跳过(找不到 build.gradle 或线上无包)')

    # 17-19 错误路径与并发
    st, hd, bd = _req(base, '/nope-nothing-here')
    chk('E1 未知路径 => 404', st == 404, 'st=%s' % st)
    st, hd, bd = _req(base, '/v1/app/upload?file=x.apk', 'POST', b'not-an-apk',
                      {'Content-Type': 'application/octet-stream'})
    chk('E2 无口令上传 => 401', st == 401, 'st=%s' % st)
    tok = os.environ.get('HH_ADMIN_TOKEN', '').strip()
    if not tok:
        # 令牌不写死在脚本里: 环境变量优先, 否则从 server/.env 读(该文件已被 .gitignore 排除)
        envf = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'server', '.env')
        if os.path.isfile(envf):
            for line in open(envf, encoding='utf-8', errors='replace'):
                if line.strip().startswith('HH_ADMIN_TOKEN='):
                    tok = line.split('=', 1)[1].strip().strip('"').strip("'")
                    break
    if tok:
        st, hd, bd = _req(base, '/v1/app/upload?file=x.apk', 'POST', b'not-an-apk',
                          {'Content-Type': 'application/octet-stream', 'X-Admin-Token': tok})
        chk('E3 正确口令 + 坏 body => 400(不是 401)', st == 400, 'st=%s' % st)
    else:
        print('  -- E3 跳过(未设 HH_ADMIN_TOKEN; 只影响发版, 不影响线上读)')
    bad = _req(base, '/v1/app/upload?file=x.apk', 'POST', b'x',
               {'Content-Type': 'application/octet-stream', 'X-Admin-Token': 'definitely-wrong'})[0]
    chk('E4 错口令 => 401', bad == 401, 'st=%s' % bad)

    fails = [r for r in RESULTS if not r[0]]
    print('小结: %d 项, 失败 %d, 用时 %.1fs' % (len(RESULTS), len(fails), time.time() - t0))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
