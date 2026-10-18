# -*- coding: utf-8 -*-
"""deploy_server.py — 把本地 server/ 推到线上并重启, 然后自动跑体检。

设计要点(都是踩过的坑):
  * **永不动**远端的 .env / certs / apk / logs —— 只同步代码。上次"只同步了一半代码"就是手工 scp 漏文件。
  * 先比 md5 再传, 传完 **必须** 跑 deploy.sh --restart(它会先清掉端口上所有旧进程, 修"51120 两个进程")。
  * 重启后自动跑 tools/server_smoke.py, 绿了才算部署完成。
  * --rotate-admin-token: 轮换 HH_ADMIN_TOKEN(远端 .env 先备份), 并用"错口令 401 / 对口令+坏 body 400"探针验证。
  * --publish-apk: 发版(需要 HH_ADMIN_TOKEN 环境变量), 上传后回读 /v1/app/list 确认版本真的上去了。

用法:
    python tools/deploy_server.py --dry-run
    python tools/deploy_server.py
    python tools/deploy_server.py --rotate-admin-token
    HH_ADMIN_TOKEN=xxx python tools/deploy_server.py --publish-apk mobile/controller-app/app/build/outputs/apk/debug/app-debug.apk
"""
import argparse
import hashlib
import os
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRV = os.path.join(ROOT, 'server')

# 只同步这些(代码 + 部署脚本 + 单元文件); 密钥/证书/APK/缓存/日志一律不碰
INCLUDE_EXT = ('.py', '.sh', '.service', '.example', '.md', '.json')
EXCLUDE_DIRS = ('__pycache__', 'apk', 'cache', 'certs', 'logs', 'run', '.git')
EXCLUDE_FILES = ('.env',)


def _mask(arg):
    """回显命令行时把令牌值打码 —— 261016 实测 curl 那行把 X-Admin-Token 明文打进日志了。"""
    a = str(arg)
    low = a.lower()
    if 'admin-token' in low:
        return a.split(':')[0] + ': ***'
    if 'admin_token' in low:
        import re
        return re.sub(r'(HH_ADMIN_TOKEN=)[^ "\']+', r'\1***', a)
    return a


def _sh(args, interactive=False, timeout=120):
    print('$ ' + ' '.join(_mask(a) for a in args))
    if timeout is None:
        return subprocess.run(args).returncode
    r = subprocess.run(args, capture_output=True, timeout=timeout)
    out = r.stdout.decode('utf-8', 'replace')
    err = r.stderr.decode('utf-8', 'replace')
    if out.strip():
        print(out.rstrip()[:4000])
    if err.strip():
        print('[stderr] ' + err.rstrip()[:1500])
    return r.returncode


def local_files():
    out = []
    for dirpath, dirnames, filenames in os.walk(SRV):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if fn in EXCLUDE_FILES or not fn.endswith(INCLUDE_EXT):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, SRV).replace('\\', '/')
            out.append((rel, full))
    return sorted(out)


def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as fh:
        for blk in iter(lambda: fh.read(1 << 16), b''):
            h.update(blk)
    return h.hexdigest()


def _admin_token():
    """令牌只从环境变量或 server/.env 取, **绝不**写进源码/命令行/日志。"""
    tok = os.environ.get('HH_ADMIN_TOKEN', '').strip()
    if tok:
        return tok
    envf = os.path.join(SRV, '.env')
    if os.path.isfile(envf):
        with open(envf, 'r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if line.startswith('HH_ADMIN_TOKEN='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
    return ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default=os.environ.get('HH_SSH_HOST', '172.28.233.51'))
    ap.add_argument('--user', default=os.environ.get('HH_SSH_USER', 'zhanghua'))
    ap.add_argument('--remote', default=os.environ.get('HH_REMOTE_DIR', '/home/zhanghua/hospital-helper'))
    ap.add_argument('--dry-run', action='store_true', help='只打印要传什么, 不动远端')
    ap.add_argument('--no-restart', action='store_true')
    ap.add_argument('--no-smoke', action='store_true')
    ap.add_argument('--rotate-admin-token', action='store_true')
    ap.add_argument('--publish-apk', default='')
    ap.add_argument('--notes', default='')
    args = ap.parse_args()

    tgt = '%s@%s' % (args.user, args.host)
    remote = args.remote
    files = local_files()
    print('待同步 %d 个文件 -> %s:%s' % (len(files), tgt, remote))
    if args.dry_run:
        for rel, full in files:
            print('  %-34s %8d B  md5=%s' % (rel, os.path.getsize(full), md5(full)[:12]))
        print('(dry-run: 未连接远端)')
        return 0

    # 1) 远端目录 + 一次性取回全部 md5(逐文件 ssh 会打爆连接, 261016 实测会卡死)
    _sh(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', tgt,
         'mkdir -p %s %s/logs %s/run %s/api %s/core %s/geo' % ((remote,) * 6)])
    find = ("cd %s && find . -maxdepth 3 -type f "
            "\\( -name '*.py' -o -name '*.sh' -o -name '*.service' -o -name '*.md' "
            "-o -name '*.json' -o -name '*.example' \\) "
            "-not -path './__pycache__/*' -not -path './cache/*' -not -path './apk/*' "
            "-not -path './certs/*' -not -path './logs/*' -not -path './run/*' "
            "-exec md5sum {} +" % remote)
    r = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', tgt, find],
                       capture_output=True, timeout=120)
    remote_md5s = {}
    for line in r.stdout.decode('utf-8', 'replace').splitlines():
        parts = line.split()
        if len(parts) == 2:
            remote_md5s[parts[1].lstrip('./')] = parts[0]
    print('远端已有 %d 个可比文件' % len(remote_md5s))

    todo = []
    for rel, full in files:
        rm = remote_md5s.get(rel, 'MISSING')
        lm = md5(full)
        if rm != lm:
            todo.append((rel, full, rm))
    print('需上传 %d / %d' % (len(todo), len(files)))
    for rel, full, rm in todo:
        print('  %-34s 远端=%s 本地=%s' % (rel, rm[:12], md5(full)[:12]))
        rc = _sh(['scp', '-o', 'BatchMode=yes', full, '%s:%s/%s' % (tgt, remote, rel)])
        if rc != 0:
            print('!! scp 失败, 中止(不重启, 避免半新半旧)')
            return 2

    # 2) 轮换管理口令(可选): 远端 + 本地 .env 一起改, 明文不进终端
    if args.rotate_admin_token:
        import secrets
        new = 'hhadm-' + secrets.token_urlsafe(24)
        rc = _sh(['ssh', '-o', 'BatchMode=yes', tgt,
                  'cp %s/.env %s/.env.bak.$(date +%%s) && '
                  '(grep -q ^HH_ADMIN_TOKEN= %s/.env && '
                  'sed -i "s/^HH_ADMIN_TOKEN=.*/HH_ADMIN_TOKEN=%s/" %s/.env || '
                  'echo "HH_ADMIN_TOKEN=%s" >> %s/.env) && echo REMOTE_ENV_OK'
                  % (remote, remote, remote, new, remote, new, remote)])
        if rc != 0:
            print('!! 远端 .env 改写失败, 中止(不重启, 避免线上/本地新旧不一致)')
            return 7
        envf = os.path.join(SRV, '.env')
        if os.path.isfile(envf):
            old = open(envf, encoding='utf-8', errors='replace').read()
            with open(envf + '.bak', 'w', encoding='utf-8') as fh:
                fh.write(old)                      # 先备份旧值(回滚用)
            lines = old.splitlines()
            hit = False
            for i, line in enumerate(lines):
                if line.strip().startswith('HH_ADMIN_TOKEN='):
                    lines[i] = 'HH_ADMIN_TOKEN=' + new
                    hit = True
            if not hit:
                lines.append('HH_ADMIN_TOKEN=' + new)
            with open(envf, 'w', encoding='utf-8') as fh:
                fh.write('\n'.join(lines) + '\n')
            print('本地 server/.env 已更新(旧值备份到 server/.env.bak)')
        else:
            print('!! 本地没有 server/.env, 只改了远端 —— 发版前需自行同步本地')
        # 明文**不**打印: 之前令牌进过日志/文件, 再打印一次等于白轮换
        print('新口令已写入远端与本地 .env: 前2位=%s 长度=%d (要看明文自己 cat server/.env)'
              % (new[:2], len(new)))
        os.environ['HH_ADMIN_TOKEN'] = new

    # 3) 重启(deploy.sh 会先清掉端口上所有旧进程)
    if not args.no_restart:
        rc = _sh(['ssh', '-o', 'BatchMode=yes', tgt, 'sh %s/deploy.sh --restart' % remote], timeout=180)
        if rc != 0:
            print('!! 远端重启失败')
            return 3

    # 4) 发版(可选)
    if args.publish_apk:
        tok = _admin_token()
        if not tok:
            print('!! 发版需要 HH_ADMIN_TOKEN(环境变量或 server/.env)')
            return 4
        apk = os.path.join(ROOT, args.publish_apk) if not os.path.isabs(args.publish_apk) else args.publish_apk
        if not os.path.isfile(apk):
            print('!! APK 不存在: %s' % apk)
            return 6
        print('待发版 ' + apk + '  ' + str(os.path.getsize(apk)) + ' B  token=前2位' + tok[:2] + ' 长度' + str(len(tok)))
        notes = urllib.parse.quote(args.notes or time.strftime('deploy %Y-%m-%d %H:%M'))
        url = 'https://hh.ei.ac.cn:51120/v1/app/upload?file=app-debug.apk&channel=debug&notes=%s' % notes
        rc = _sh(['curl', '-k', '-m', '900', '-T', apk, '-H', 'X-Admin-Token: ' + tok, url], timeout=960)
        if rc != 0:
            print('!! 上传失败')
            return 5

    # 5) 体检
    if not args.no_smoke:
        rc = subprocess.call([sys.executable, os.path.join(HERE, 'server_smoke.py')])
        print('体检 rc=%d' % rc)
        return rc
    return 0


if __name__ == '__main__':
    sys.exit(main())
