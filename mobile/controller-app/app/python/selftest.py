# -*- coding: utf-8 -*-
"""selftest.py — App 端「自检」入口（P4）。被 Java SelfTest.run() 调用：

    Python.getInstance().getModule("selftest").callAttr("run", manifest_json)
        -> JSON 字符串 {ok, rc, units, six_records_total, elapsed_ms,
                        error, stop_reason, raw_dir, raw_files, new_raw_files,
                        cond_dir, manifest_used, device, started_at}

职责：
  * 解析 manifest（dict / JSON 字符串），抽出 conditions 列表（缺省给一条固定条件）。
  * **只读抓取**：调 fetch_slots.run_conditions（其内部即 run_condition_mode），
    绕开完整 9 段 LLM 流水线。绝不进入任何下单路径。
  * 抓取前对 files/out/slots/out/raw/ 做一次快照，抓取后 diff 出「新生成的 raw 文件」，
    连同条件记录/摘要一起返回，供 Java 落盘 + 打 logcat（run-as 可核对设备侧 raw 真的在写）。

安全：本模块只 import 只读抓取栈（hhpaths/devbridge/fetch_slots），不 import
任何 booking/payment 模块；manifest 仅用于限定 hospital/tab/dept/slot_type。
"""
import datetime as dt
import json
import os

import hhpaths          # noqa: F401  (先 bootstrap sys.path：slots / ranking/scripts)
import devbridge
try:
    from slots import fetch_slots        # 端上 Chaquopy 运行时口径：slots 是子包（与 app/slots.py:286 一致）
except ImportError:                      # 桌面 / 旧布局：python/slots 已在 sys.path，裸 import
    import fetch_slots

# 内置固定条件（按钮触发、manifest 缺 conditions 时用；与 PC 垫片 probe_pos_ctrl 同一条）。
DEFAULT_CONDITIONS = [
    {'hospital': 'SH-084', 'tab': '眼科', 'dept': '眼科门诊', 'slot_type': 'expert'},
]


def _log(msg):
    # 既打 stdout（Chaquopy 默认进 logcat/python），又回吐给 App UI 日志（PyBridge）。
    try:
        print(msg, flush=True)
    except Exception:
        pass
    try:
        devbridge.emit_log(msg)
    except Exception:
        pass
    # 第三路(2026-10-14 补): 广播/后台触发时 MainActivity 可能根本没创建, Listener=null,
    # 上面两路都到不了 PC => 直接再打一份 logcat(HH_SELFTEST), 便于 adb 侧看全细节。
    try:
        from android.util import Log
        Log.i("HH_SELFTEST", msg)
    except Exception:
        pass


MODES = ('full', 'probe')


def _manifest_dict(manifest):
    """manifest -> dict(str/list/dict 都容错); 顶层除 conditions 外还带运行参数。"""
    if manifest is None:
        return {}
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except Exception:
            return {}
    if not isinstance(manifest, dict):
        return {'conditions': manifest}
    return manifest


def _norm_opts(md):
    """manifest 顶层运行参数: mode(full/probe) / limit / resume。

    probe = 只扫到 L2/子tab 的号源态势(不点医生主页), 因此它是**子tab selected 判定**
    唯一的真机通路 —— 自检按钮走 full, 子tab 那条路径永远不会被跑到(2026-10-15 复盘)。
    """
    mode = str(md.get('mode') or 'full').strip().lower()
    if mode not in MODES:
        mode = 'full'
    try:
        limit = int(md.get('limit') or 0)
    except Exception:
        limit = 0
    return {'mode': mode, 'limit': limit, 'resume': bool(md.get('resume'))}


def _norm_conditions(manifest):
    """从 dict/JSON 里抽出 conditions 列表；容错：裸列表 / 单条对象 / 缺省。"""
    if manifest is None:
        return list(DEFAULT_CONDITIONS)
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except Exception:
            manifest = {}
    if not isinstance(manifest, dict):
        manifest = {'conditions': manifest}
    conds = manifest.get('conditions')
    if conds is None:
        conds = manifest.get('condition')
    if conds is None:
        return list(DEFAULT_CONDITIONS)
    if isinstance(conds, dict):
        conds = [conds]
    if not isinstance(conds, list) or not conds:
        return list(DEFAULT_CONDITIONS)
    out = []
    for c in conds:
        if isinstance(c, str):
            try:
                c = json.loads(c)
            except Exception:
                continue
        if isinstance(c, dict) and (c.get('hospital') or c.get('tab')):
            out.append(c)
    return out or list(DEFAULT_CONDITIONS)


def _raw_dir():
    d = hhpaths.out('slots', 'out', 'raw')
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _snap_raw():
    d = _raw_dir()
    s = set()
    try:
        s = set(os.listdir(d))
    except Exception:
        pass
    return d, s


def _list_new(before_set):
    d = _raw_dir()
    entries = []
    try:
        for name in sorted(os.listdir(d)):
            if name in before_set:
                continue
            p = os.path.join(d, name)
            try:
                st = os.stat(p)
                ts = dt.datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                sz = int(st.st_size)
            except Exception:
                ts, sz = '?', -1
            entries.append('%s  (%s, %d bytes)' % (name, ts, sz))
    except Exception:
        pass
    return entries


def _list_dir(d, pat):
    out = []
    try:
        for name in sorted(os.listdir(d)):
            if pat and not name.endswith(pat):
                continue
            p = os.path.join(d, name)
            try:
                st = os.stat(p)
                out.append('%s  (%s, %d bytes)' % (
                    name, dt.datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                    int(st.st_size)))
            except Exception:
                out.append(name)
    except Exception:
        pass
    return out


def run(manifest=None):
    """自检主入口。任何异常都包进返回 JSON，不向 Java 抛。"""
    started = dt.datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f')
    base = {
        'ok': False, 'rc': -1, 'units': -1, 'six_records_total': -1,
        'elapsed_ms': -1, 'error': None, 'stop_reason': None,
        'raw_dir': _raw_dir(), 'raw_files': [], 'new_raw_files': [],
        'cond_dir': hhpaths.out('slots', 'out', 'condition_runs'),
        # 2026-10-14 修复：原先硬编码 '476cf991'，MI 5X 上跑出来的自检结果也写着另一台机器。
        'manifest_used': None, 'device': devbridge.device_key(), 'started_at': started,
    }
    md = _manifest_dict(manifest)
    opts = _norm_opts(md)
    conds = _norm_conditions(md)
    base['manifest_used'] = conds
    base['mode_used'] = opts['mode']
    t0 = dt.datetime.now()
    _log('[selftest] 开始 in-process 号源自检（只读；不预约/不提交/不支付）')
    _log('[selftest] conditions = ' + json.dumps(conds, ensure_ascii=False))
    _log('[selftest] mode=%s limit=%d resume=%s' % (opts['mode'], opts['limit'], opts['resume']))

    raw_dir, before = _snap_raw()
    _log('[selftest] raw/ 基线文件数 = %d  (%s)' % (len(before), raw_dir))

    # 提速轮(2026-10-09): 每次 run 前按 hh_levers.json 应用杠杆开关(A/B 测量 / 独立回退)
    try:
        fetch_slots.apply_lever_overrides()
    except Exception:
        pass
    # 自检必须「真跑」：run_condition_mode 默认按 condition_runs/run_*.json 里的 done 标记
    # 断点续跑，把上次已 ok/no_slot 的单元直接跳过 —— 实测 639ms / units=0，等于空转
    # （2026-10-14 定位：这就是「自检点了跟没点一样、跟之前一样」的直接原因）。
    # 自检的语义就是「现在立刻重新抓一遍」，所以强制 NO_RESUME。
    if not opts['resume']:
        try:
            fetch_slots.NO_RESUME = True
            _log('[selftest] NO_RESUME=True（强制真跑，忽略 condition_runs 的 done 标记）')
        except Exception as e:
            _log('[selftest] 设置 NO_RESUME 失败: %s' % e)
    else:
        _log('[selftest] resume=True: 沿用 condition_runs 的 done 标记(已 ok 的单元跳过)')

    rc = -1
    run_sum = None
    try:
        rc, run_sum = fetch_slots.run_conditions(conds, device=base['device'],
                                                 mode=opts['mode'], limit=opts['limit'],
                                                 max_consec_errors=None, on_log=_log)
    except Exception as e:
        base['error'] = '%s: %s' % (type(e).__name__, str(e)[:300])
        _log('[selftest] run_conditions 异常: ' + base['error'])

    elapsed_ms = int((dt.datetime.now() - t0).total_seconds() * 1000)
    base['elapsed_ms'] = elapsed_ms
    if run_sum is not None:
        base['rc'] = int(rc)
        base['units'] = len(run_sum.get('units', []))
        base['six_records_total'] = int(run_sum.get('six_records_total', -1))
        base['stop_reason'] = run_sum.get('stop_reason')
        base['resume_done_pre'] = run_sum.get('resume_done_pre')
        base['groups_planned'] = run_sum.get('groups_planned')
        if not base['stop_reason']:
            base['stop_reason'] = run_sum.get('circuit_break')
        # 每个 unit 的 status/reason 也带出来，方便端上快速判断
        base['unit_statuses'] = [
            {'hospital': u.get('hospital'), 'tab': u.get('tab'), 'dept': u.get('dept'),
             'status': u.get('status'), 'reason': u.get('error_reason'),
             'six': u.get('six_records_total')}
            for u in run_sum.get('units', [])
        ]

    # 新生成的 raw 文件（相对基线 diff）+ raw/ 全量列表
    base['new_raw_files'] = _list_new(before)
    base['raw_files'] = _list_dir(raw_dir, None)
    # 本轮条件记录（condition_runs 下最新的 .jsonl / run_*.json）
    base['condition_records'] = _list_dir(base['cond_dir'], '.jsonl')
    base['condition_summaries'] = _list_dir(base['cond_dir'], '.json')

    ok = (rc == 0)
    if not ok and not base['error']:
        base['error'] = 'rc=%s (units=%s) %s' % (rc, base['units'],
                                                 base.get('unit_statuses') or '')
    base['ok'] = bool(ok)
    _log('[selftest] 完成 ok=%s rc=%s six=%s new_raw=%d elapsed=%dms'
         % (ok, base['rc'], base['six_records_total'], len(base['new_raw_files']), elapsed_ms))
    _log('[selftest] raw_dir=%s' % raw_dir)
    _log('[selftest] cond_dir=%s' % base['cond_dir'])
    return json.dumps(base, ensure_ascii=False, default=str)


if __name__ == '__main__':  # 桌面自检（无设备时只验 manifest 解析）
    print(run(DEFAULT_CONDITIONS))
