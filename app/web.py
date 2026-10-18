# -*- coding: utf-8 -*-
"""挂号推荐 web 实测端 — 纯标准库 ThreadingHTTPServer + SSE, 零第三方依赖。

用法:
    python -m app.web --port 8765            # 浏览器打开 http://127.0.0.1:8765/

端点:
    GET  /               单页 (app/web/index.html)
    POST /api/run        启动一次流水线; body=JSON 表单字段; 返回 {job_id, stream}
    GET  /api/stream?job SSE 实时进度: data: {"type":"line","v":...} / {"type":"done","v":{ok,code,...}}
    GET  /api/result?job 完整 result.json (流水线落盘后)
    GET  /api/regeo?lng&lat 自动定位反链: 浏览器 WGS-84 → 转 GCJ-02 → 高德逆地理 → 地址描述
    GET  /api/jobs       历史 job 列表 (扫 app/out/* 持久目录, 跨重启可见)
    GET  /api/device-loc?fresh&dev   adb 手机定位: dumpsys location (WGS-84, fused>gps>network)
                              -> GCJ-02 -> 高德 regeo 地址+权威坐标校正; 30 分钟文件缓存
    GET  /api/patients?fresh&dev     随申办·预约挂号·「就诊人」列表(只读 adb uiautomator 采集):
                              脱敏姓名/年龄/性别(证件号第17位推断)/医保卡; 30 分钟文件缓存
    GET  /api/report?job   该 run 的 report.md

约束:
    * 全局同一时刻只允许 1 条流水线在跑 (adb 设备独占 + stdout 捕获是进程级状态)。
    * web 不发起任何真实挂号; 与 CLI 同一 run_pipeline 代码路径, 结果落盘 app/out/web_<ts>/。
    * --live 需本机有 adb 且设备在线 (随申办已登录), 采集耗时 ~5-15 分钟/科室。
"""
import argparse
import datetime as dt
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, 'web')
OUT_DIR = os.path.join(APP_DIR, 'out')

from . import main as main_mod
from . import dialogue  # noqa: F401  (spec_from_dict 在 main 内调用; 此处引用保证模块已加载)
from . import patients   # 就诊人采集(随申办 APP, 只读 adb)

JOBS = {}
JOBS_LOCK = threading.Lock()

DEVICE_LOC_CACHE = os.path.join(APP_DIR, 'cache', 'device_loc.json')
DEVICE_LOC_TTL_S = 1800  # 30 分钟; 过期/点按钮 -> adb 实读
_JOB_ID_RE = re.compile(r'^[A-Za-z0-9_\-]+$')


def _job_dir(jid):
    if not _JOB_ID_RE.match(jid or ''):
        return None
    p = os.path.join(OUT_DIR, jid)
    return p if os.path.isdir(p) else None


def _device_loc(fresh=False, dev=None):
    """adb 手机定位: locate.locate_from_adb (dumpsys, WGS-84 -> GCJ-02) -> 高德 regeo 地址。
    结果写文件缓存 DEVICE_LOC_CACHE (30min) -> 页面刷新自动回填。
    返回 (dict|None, err|None); 实读失败但有旧缓存时返回旧缓存并带 error 说明。"""
    dev = (dev or os.environ.get('DEVICE_SERIAL') or '476cf991').strip() or '476cf991'
    cached = None
    try:
        with open(DEVICE_LOC_CACHE, encoding='utf-8') as fh:
            cached = json.load(fh)
    except Exception:
        cached = None
    if cached and not fresh and (time.time() - float(cached.get('ts', 0))) < DEVICE_LOC_TTL_S:
        cached['from_cache'] = True
        return cached, None
    from . import locate, amap
    try:
        loc = locate.locate_from_adb(dev)
    except locate.GeocodeError as e:
        msg = str(e)[:150]
        if cached:
            cached['from_cache'] = True
            cached['error'] = '实时读失败(%s) → 返回旧缓存' % msg
            return cached, None
        return None, msg
    key = amap.resolve_key()
    lng, lat, address, city, district, amap_cached, snapped = loc['lng'], loc['lat'], '', '', '', False, False
    if key:
        try:
            r, amap_cached = amap.regeo_gcj02(key, loc['lng'], loc['lat'], radius=1500)
            if r and r.get('found'):
                address = r.get('address') or ''
                city = r.get('city') or ''
                district = r.get('district') or ''
                fl = (r.get('location') or '').split(',')
                if len(fl) == 2:
                    lng, lat = float(fl[0]), float(fl[1])
                    snapped = abs(lng - loc['lng']) > 1e-5 or abs(lat - loc['lat']) > 1e-5
        except amap.AmapError:
            pass  # regeo 失败(配额/网络) -> 只回坐标
    d = {'ts': time.time(), 'ts_iso': dt.datetime.now().isoformat(timespec='seconds'),
         'lng': lng, 'lat': lat, 'crs': 'GCJ-02', 'address': address,
         'city': city, 'district': district, 'provider': loc.get('provider', ''),
         'age_s': loc.get('age_s'),
         'device': dev, 'amap_snapped': snapped, 'amap_cached': amap_cached,
         'from_cache': False}
    try:
        os.makedirs(os.path.dirname(DEVICE_LOC_CACHE), exist_ok=True)
        tmp = DEVICE_LOC_CACHE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, DEVICE_LOC_CACHE)
    except Exception:
        pass
    return d, None


def _list_jobs():
    """扫 app/out/*/result.json (持久化历史: web/CLI/demo 全可见), 合并内存 running。"""
    jobs = []
    try:
        names = os.listdir(OUT_DIR)
    except OSError:
        names = []
    for name in names:
        rp = os.path.join(OUT_DIR, name, 'result.json')
        if not os.path.isfile(rp):
            continue
        try:
            with open(rp, encoding='utf-8') as fh:
                data = json.load(fh)
        except Exception:
            continue
        meta = data.get('meta') or {}
        spec = data.get('spec') or {}
        fin = data.get('final') or {}
        recs = fin.get('recommendations') or []
        top1 = recs[0] if recs else {}
        checks = data.get('checks') or []
        slots_ = data.get('slots') or {}
        jobs.append({
            'id': name,
            'started_at': meta.get('started_at') or meta.get('generated_at') or '',
            'generated_at': meta.get('generated_at') or '',
            'mode': meta.get('mode') or '',
            'symptom': (spec.get('symptom_text') or '')[:40],
            'top1': top1.get('name'),
            'top1_score': top1.get('final_score'),
            'checks_pass': sum(1 for c in checks if c.get('ok')),
            'checks_total': len(checks),
            'live': any(str(v.get('slot_source') or '').startswith('live') for v in slots_.values()),
            'running': False,
        })
    with JOBS_LOCK:
        for j in JOBS.values():
            if j['status'] == 'running' and not any(x['id'] == j['id'] for x in jobs):
                jobs.append({'id': j['id'], 'started_at': j['ts'], 'generated_at': '', 'mode': 'web',
                             'symptom': j['symptom_head'], 'top1': None, 'top1_score': None,
                             'checks_pass': 0, 'checks_total': 0, 'live': bool(j.get('live')),
                             'running': True})
    jobs.sort(key=lambda x: x.get('started_at') or '', reverse=True)
    return jobs[:50]


class _Tee(object):
    """stdout 转发: 原样输出 + 逐行推入 job 队列 (SSE 进度源)。"""

    def __init__(self, orig, q, fh=None):
        self._orig = orig
        self._q = q
        self._fh = fh

    def write(self, s):
        self._orig.write(s)
        if self._fh:
            try:
                self._fh.write(s)
            except Exception:
                pass
        for ln in s.splitlines():
            try:
                self._q.put_nowait({'type': 'line', 'v': ln})
            except queue.Full:
                pass

    def flush(self):
        self._orig.flush()
        if self._fh:
            try:
                self._fh.flush()
            except Exception:
                pass


def _worker(job):
    """跑一次 run_pipeline (与 CLI 同一函数), stdout 逐行推入 job 队列。"""
    q = job['queue']
    try:
        argv = ['--spec-file', job['spec_file'], '--non-interactive']
        if job.get('loc_text'):
            argv += ['--loc-text', job['loc_text']]
        else:
            argv += ['--loc', job['loc']]
        if job.get('no_network'):
            argv.append('--no-network')
        if job.get('live'):
            argv.append('--live')
            argv += ['--device', job.get('device') or '476cf991']
            if job.get('slot_type'):
                argv += ['--live-slot-type', job['slot_type']]
            if job.get('live_timeout_min'):
                try:
                    argv += ['--live-timeout', str(int(job['live_timeout_min']) * 60)]
                except (TypeError, ValueError):
                    pass
        if job.get('radius'):
            argv += ['--radius', str(job['radius'])]
        argv += ['--top', str(job.get('top') or 20)]
        if job.get('agentsvc_base'):
            argv += ['--agentsvc-base', job['agentsvc_base']]
        if job.get('llm_model'):
            argv += ['--llm-model', job['llm_model']]

        args = main_mod.parse_args(argv)
        job['argv'] = argv
        log_path = os.path.join(os.path.dirname(job['spec_file']), 'log.txt')
        logfh = open(log_path, 'a', encoding='utf-8')
        old = sys.stdout
        sys.stdout = _Tee(old, q, logfh)
        try:
            code = main_mod.run_pipeline(args, out_subdir=job['id'])
        finally:
            sys.stdout = old
            try:
                logfh.close()
            except Exception:
                pass
        job['status'] = 'done' if code == 0 else 'fail'
        job['code'] = code
        q.put({'type': 'done', 'v': {'ok': code == 0, 'code': code, 'out_dir': job['out_rel']}})
    except SystemExit as e:
        job['status'] = 'fail'
        code = e.code if isinstance(e.code, int) else 1
        if isinstance(e.code, str) and e.code:
            q.put({'type': 'line', 'v': '[受控退出] ' + e.code})
        q.put({'type': 'done', 'v': {'ok': False, 'code': code}})
    except Exception as e:  # noqa: BLE001
        job['status'] = 'error'
        q.put({'type': 'done', 'v': {'ok': False, 'code': -1,
                                     'error': '%s: %s' % (type(e).__name__, str(e)[:500])}})
    finally:
        q.put(None)


class Handler(BaseHTTPRequestHandler):
    server_version = 'HospitalHelperWeb/0.1'

    def log_message(self, fmt, *args):
        sys.stderr.write('[web] %s\n' % (fmt % args))

    # ---------- helpers ----------
    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ('/', '/index.html'):
            try:
                with open(os.path.join(WEB_DIR, 'index.html'), 'rb') as fh:
                    self._send(200, fh.read(), 'text/html; charset=utf-8')
            except OSError:
                self._json(500, {'error': 'index.html missing'})
            return
        if u.path == '/api/regeo':
            # 浏览器 Geolocation(WGS-84) -> 服务端转 GCJ-02 -> 高德逆地理 -> 地址描述。
            # key 只在本进程解析, 绝不下发前端; 失败一律 5xx + 原因, 不假装成功。
            try:
                lng = float((q.get('lng') or [''])[0])
                lat = float((q.get('lat') or [''])[0])
                if not (-180.0 <= lng <= 180.0 and -90.0 <= lat <= 90.0):
                    raise ValueError('坐标超范围')
            except (ValueError, TypeError, IndexError):
                self._json(400, {'error': 'lng/lat 参数缺失或非法'})
                return
            from . import amap
            key = amap.resolve_key()
            if not key:
                self._json(503, {'error': 'AMAP_KEY 未配置 (env AMAP_KEY / hospital_intel/.env), 逆地理不可用'})
                return
            lng_gcj, lat_gcj = amap.wgs84_to_gcj02(lng, lat)
            try:
                r, from_cache = amap.regeo_gcj02(key, lng_gcj, lat_gcj, radius=1500)
            except amap.AmapError as e:
                self._json(502, {'error': str(e)[:200]})
                return
            if not r or not r.get('found'):
                self._json(502, {'error': '逆地理未命中 (坐标可能在海外/水域)'})
                return
            fin_lng, fin_lat, snapped = lng_gcj, lat_gcj, False
            try:
                fl = (r.get('location') or '').split(',')
                if len(fl) == 2:
                    fin_lng, fin_lat = float(fl[0]), float(fl[1])
                    snapped = abs(fin_lng - lng_gcj) > 1e-5 or abs(fin_lat - lat_gcj) > 1e-5
            except (ValueError, TypeError):
                pass
            self._json(200, {'ok': True, 'lng': fin_lng, 'lat': fin_lat, 'crs': 'GCJ-02',
                              'address': r.get('address', ''), 'city': r.get('city', ''),
                              'district': r.get('district', ''), 'from_cache': from_cache,
                              'amap_snapped': snapped})
            return
        if u.path == '/api/device-loc':
            # adb 读手机最后位置 (dumpsys, fused>gps>network, WGS-84) -> GCJ-02 -> 高德 regeo 地址。
            # fresh=1 强制实读; 缺省优先 30min 文件缓存 (页面刷新自动回填)。
            fresh = (q.get('fresh') or ['0'])[0] in ('1', 'true', 'yes')
            dev = (q.get('dev') or [''])[0]
            d, err = _device_loc(fresh=fresh, dev=dev)
            if d:
                body = dict(d)
                body['ok'] = True
                if err:
                    body['error'] = err
                self._json(200, body)
            else:
                self._json(502, {'ok': False, 'error': err or 'unknown error'})
            return
        if u.path == '/api/patients':
            # 随申办 · 预约挂号 · 「就诊人」列表（只读采集: adb uiautomator dump, 不点新增/绑定/预约）。
            # fresh=1 强制实读; 缺省优先 30min 文件缓存 (app/cache/patients.json)。
            # adb 设备独占: 流水线在跑时不并发操作手机 -> 409（避免互相抢前台/抢 dump）。
            fresh = (q.get('fresh') or ['0'])[0] in ('1', 'true', 'yes')
            cache_only = (q.get('cache') or ['0'])[0] in ('1', 'true', 'yes')
            dev = (q.get('dev') or [''])[0]
            if cache_only:
                d = patients.load_cache()
                if d:
                    d = dict(d); d['from_cache'] = True; d['ok'] = True
                    self._json(200, d)
                else:
                    self._json(404, {'ok': False, 'error': '无就诊人缓存（点「获取就诊人」实采一次）'})
                return
            with JOBS_LOCK:
                running = [j for j in JOBS.values() if j['status'] == 'running']
            if running:
                self._json(409, {'ok': False, 'error': '流水线运行中(adb 设备独占): %s 结束后再获取就诊人'
                                            % running[0]['id']})
                return
            d, err = patients.get_patients(dev=dev, fresh=fresh, log=lambda m: print(m, flush=True))
            if d:
                body = dict(d)
                body['ok'] = True
                if err:
                    body['error'] = err
                self._json(200, body)
            else:
                self._json(502, {'ok': False, 'error': err or 'unknown error'})
            return
        if u.path == '/api/amap-js-config':
            # 前端内嵌高德 JSAPI(Web端 key)。优先级:
            # AMAP_JS_KEY/AMAP_JS_CODE (env > .env) > 回退 AMAP_KEY
            # (仅当该 key 在高德控制台开了"Web端(JS API)"平台才可用; 不可用时前端自动降级为"新窗口打开高德"链接)。
            from . import amap
            envd = amap.load_env_file(amap.ENV_FILE)

            def _pick(k):
                v = os.environ.get(k)
                if v is not None:
                    return v.strip() or None
                return (envd.get(k) or '').strip() or None

            jk, jc = _pick('AMAP_JS_KEY'), _pick('AMAP_JS_CODE')
            js_fallback = False
            if not jk:
                jk = amap.resolve_key()  # AMAP_KEY 回退
                js_fallback = bool(jk)
            self._json(200, {'ok': bool(jk), 'key': jk, 'code': jc or '',
                             'key_source': 'AMAP_JS_KEY' if not js_fallback else 'AMAP_KEY(fallback)'})
            return
        if u.path == '/api/jobs':
            self._json(200, {'jobs': _list_jobs()})
            return
        if u.path == '/api/result':
            jid = (q.get('job') or [''])[0]
            d = _job_dir(jid)
            p = os.path.join(d, 'result.json') if d else None
            if p and os.path.exists(p):
                with open(p, 'rb') as fh:
                    self._send(200, fh.read())
                return
            job = JOBS.get(jid)
            if job and job['status'] == 'running':
                self._json(202, {'error': 'not ready', 'status': job['status']})
                return
            self._json(404, {'error': 'job not found (app/out/%s/result.json 不存在)' % jid})
            return
        if u.path == '/api/report':
            jid = (q.get('job') or [''])[0]
            d = _job_dir(jid)
            p = os.path.join(d, 'report.md') if d else None
            if not p or not os.path.exists(p):
                self._json(404, {'error': 'report.md not found'})
                return
            with open(p, 'rb') as fh:
                self._send(200, fh.read(), 'text/markdown; charset=utf-8')
            return
        if u.path == '/api/stream':
            job = JOBS.get((q.get('job') or [''])[0])
            if not job:
                self._json(404, {'error': 'job not found'})
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()
            try:
                for item in iter(job['queue'].get, None):
                    self.wfile.write(('data: %s\n\n' % json.dumps(item, ensure_ascii=False)).encode('utf-8'))
                    self.wfile.flush()
                self.wfile.write(b'event: close\ndata: {}\n\n')
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self._json(404, {'error': 'not found'})

    # ---------- POST ----------
    def do_POST(self):
        u = urlparse(self.path)
        if u.path != '/api/run':
            self._json(404, {'error': 'not found'})
            return
        try:
            ln = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(ln).decode('utf-8'))
        except Exception:
            self._json(400, {'error': 'bad json'})
            return
        sym = (body.get('symptom') or '').strip()
        loc_text = (body.get('loc_text') or '').strip()
        loc = (body.get('loc') or '').strip()
        if not sym:
            self._json(400, {'error': 'symptom 不能为空'})
            return
        if not loc_text and not loc:
            self._json(400, {'error': '需要位置: loc_text (地址文本) 或 loc ("lng,lat")'})
            return
        try:
            cap = body.get('price_cap')
            if cap not in (None, ''):
                int(cap)
            urg = body.get('urgency_days', 7)
            if urg not in (None, ''):
                int(urg)
            age = body.get('age')
            if age not in (None, ''):
                int(age)
        except (TypeError, ValueError):
            self._json(400, {'error': 'age/urgency_days/price_cap 需为整数'})
            return

        # 表单简化(2026-10-08): 前端只送 症状 / 就诊人 / 费用预期档 / 位置。
        # 时效·就诊类型·水平预期 交给 LLM 从症状描述解析; 地铁优先、代挂号复选框已取消。
        fee_expect = (body.get('fee_expect') or 'auto').strip()
        if fee_expect not in dialogue.FEE_EXPECT:
            self._json(400, {'error': 'fee_expect 需为 %s' % '|'.join(t[0] for t in dialogue.FEE_EXPECT_TIERS)})
            return
        pat_id = (body.get('patient_id') or '').strip()
        patient = None
        if pat_id:
            pc = patients.load_cache() or {}
            patient = next((p for p in (pc.get('patients') or []) if p.get('id') == pat_id), None)
            if patient is None:
                self._json(400, {'error': '就诊人缓存里没有 id=%s（请先点「获取就诊人」刷新）' % pat_id})
                return

        with JOBS_LOCK:
            running = [j for j in JOBS.values() if j['status'] == 'running']
            if running:
                self._json(409, {'error': '流水线忙碌 (adb 设备独占, 全局单任务): 当前 %s 结束后重试' % running[0]['id']})
                return
            job_id = 'web_' + dt.datetime.now().strftime('%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:4]
            jdir = os.path.join(OUT_DIR, job_id)
            os.makedirs(jdir, exist_ok=True)
            spec = {
                'symptom_text': sym,
                'patient': patient,
                'patient_selected': bool(patient),
                'fee_expect': fee_expect,
                'metro_priority': False,   # 交通主要靠 distance, metro/bus 折算进便利度(算法暂不改)
            }
            # 兼容旧客户端(直接 POST 老字段): 传了就透传, spec_from_dict 仍支持
            for k_old in ('age', 'gender', 'urgency_days', 'visit_type',
                          'price_cap_cny', 'level_expectation', 'proxy'):
                if body.get(k_old) not in (None, ''):
                    spec[k_old] = body[k_old]
            spec_file = os.path.join(jdir, 'spec.json')
            with open(spec_file, 'w', encoding='utf-8') as fh:
                json.dump(spec, fh, ensure_ascii=False, indent=1)
            job = {
                'id': job_id,
                'ts': dt.datetime.now().isoformat(timespec='seconds'),
                'status': 'running',
                'queue': queue.Queue(),
                'spec_file': spec_file,
                'symptom_head': sym[:40],
                'patient_label': (patient or {}).get('label') or '',
                'fee_expect': fee_expect,
                'loc_text': loc_text,
                'loc': loc,
                'no_network': bool(body.get('no_network')),
                'live': bool(body.get('live')),
                'device': (body.get('device') or '').strip(),
                'slot_type': body.get('slot_type'),
                'live_timeout_min': body.get('live_timeout_min'),
                'radius': body.get('radius'),
                'top': body.get('top', 20),
                'agentsvc_base': (body.get('agentsvc_base') or '').strip() or None,
                'llm_model': (body.get('llm_model') or '').strip() or None,
                'out_rel': os.path.relpath(jdir, os.getcwd()),
            }
            JOBS[job_id] = job
        threading.Thread(target=_worker, args=(job,), name='pipeline-' + job_id, daemon=True).start()
        self._json(202, {'job_id': job_id, 'stream': '/api/stream?job=%s' % job_id})


def _startup_device_loc():
    """服务启动 5s 后读一次手机位置写缓存 -> 页面刷新即见 (不等用户点按钮)。"""
    time.sleep(5)
    try:
        d, err = _device_loc(fresh=True)
        if d:
            print('[web] 初始设备定位: %s %s (provider=%s dev=%s cached=%s)' % (
                d.get('address') or ('%.6f,%.6f' % (d['lng'], d['lat'])),
                d.get('city', ''), d.get('provider'), d.get('device'), d.get('from_cache')))
        else:
            print('[web] 初始设备定位失败: %s' % err)
    except Exception as e:
        print('[web] 初始设备定位异常: %r' % e)


def main():
    ap = argparse.ArgumentParser(description='挂号推荐 web 实测端 (纯标准库)')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--host', default='0.0.0.0')  # 默认全接口绑定: 本机/局域网手机均可访问
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=_startup_device_loc, name='startup-device-loc', daemon=True).start()
    if a.host in ('0.0.0.0', '::'):
        import socket
        try:
            lan = [ip for ip in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET) if not ip[4][0].startswith('127.')][0][4][0]
        except Exception:
            lan = '127.0.0.1'
        print('[web] 本机: http://127.0.0.1:%d/' % a.port)
        print('[web] 局域网: http://%s:%d/  (手机/平板同网段可开)' % (lan, a.port))
    else:
        print('[web] http://%s:%d/' % (a.host, a.port))
    print('[web] Ctrl-C 退出; 全局单任务; 不发起真实挂号')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n[web] 退出')
    return 0


if __name__ == '__main__':
    sys.exit(main())
