# -*- coding: utf-8 -*-
"""号源（缓存优先 + 优雅降级）。层序（slots/ 只读, 唯一例外: live 结果回写观察快照 slots/out/<hid>-<slug>.json 供下次运行命中）:
  1) slots/profiles/slot_profile.json     （RANKING_SPEC §5; source=observed, TTL 90min, 键 (dept_id,hospital_id)）
  2) slots/out/<hid>-<slug>.json          （fetch_slots.py 观察快照, captured_at, TTL 90min）
  3) [--live + --device] 批量触发 slots/fetch_slots.py (v2, --condition 统一接口) 子进程实时抓取
     （条件经 --condition-file UTF-8 免损坏通道下发; 结果读 slots/out/condition_runs/ 的 run 摘要+六字段记录）
  4) slots/profiles/level_defaults.json   （档位兜底: 非实时, 带 confidence/stratum 出处）
  5) app/fixtures/stub_slots.json 内置 stub（合成数据, 恒标注“号源未核实”）
每条返回 SlotInfo 必带 provenance（文件/时间戳/龄期/TTL 判定）——不允许出现无出处的号源结论。
profile 文件由另一 Agent 并行产出, 可能缺失 → 逐层降级, 流水线照常跑通。
"""
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(APP_DIR)
TTL_MIN = 90  # 号源态势实测 4 分钟内可翻转 → 缓存 90min 过期即重抓
PROFILE_PATH = os.path.join(WS, 'slots', 'profiles', 'slot_profile.json')
LEVELS_PATH = os.path.join(WS, 'slots', 'profiles', 'level_defaults.json')
RAW_DIR = os.path.join(WS, 'slots', 'out')
STUB_PATH = os.path.join(APP_DIR, 'fixtures', 'stub_slots.json')
FETCH_SCRIPT = os.path.join(WS, 'slots', 'fetch_slots_v1_20261004.py')   # v1: 全院姿态层批量采集（离线批次工具, app live 不再走这里）
FETCH_V2 = os.path.join(WS, 'slots', 'fetch_slots.py')                  # v2 (S20 --condition 统一接口): app live 抓取入口（2026-10-05 接入）
COND_RUNS_DIR = os.path.join(WS, 'slots', 'out', 'condition_runs')      # v2 输出: <ts>_all.jsonl + run_<ts>.json
LIVE_TIMEOUT_S = 3600                                                    # live 批量抓取子进程上限（12 院 x 多科室, 慢但诚实）
ALLOWED_KINDS = {'clinic', 'specialty_clinic', 'screening'}              # 与 stage-1 ALLOWED_HIT_KINDS 对齐
FEE_TIER_ORDER = {'15': 1, '20': 2, '25': 3, '50': 4, '100+': 5}        # 费档序（与 recommend.FEE_VAL 语义一致）
def derive_slug(hid):
    """slug 仅用于输出文件名 slots/out/<hid>-<slug>.json（2026-10-03 起不再是准入门槛）。
    身份来自 ALL_DEPARTMENTS.csv / hospital_intel manifest（hid↔医院名）；slug 本身:
    优先沿用 slots/out 已落盘文件名（稳定）, 缺失时由医院 id 确定性派生（SH-084 -> sh084）。"""
    for p in glob.glob(os.path.join(RAW_DIR, hid + '-*.json')):
        slug = os.path.basename(p)[:-len('.json')][len(hid) + 1:]
        if slug:
            return slug
    return 'sh' + str(hid).split('-')[-1]


def _now():
    return dt.datetime.now()


def _parse_ts(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace('Z', '').split('+')[0])
    except Exception:
        return None


def _age_min(ts):
    if ts is None:
        return None
    return max(0.0, (_now() - ts).total_seconds() / 60.0)


class SlotResolver:
    def __init__(self, allow_live=False, device=None, log=None, slot_type=None, live_timeout_s=None):
        """slot_type: None=全部子 tab(专家/普通/专病), 或 'expert'/'normal'/'specialty' 限定（--live-slot-type）"""
        self.allow_live = allow_live
        self.device = device
        self.log = log or (lambda *a: None)
        self._profile = None
        self._levels = None
        self._raw = {}
        self._stub = None
        self._live_results = {}   # (hid, did) -> SlotInfo（v2 live 批量结果, prepare_live 填充）
        self._probe_results = {}   # (hid, did) -> SlotInfo (v2 probe quick probe results, filled by prepare_probe)
        self._manifest_map = None  # hid -> manifests/<file>
        self._manifests = {}       # hid -> manifest dict（懒加载缓存）
        self.slot_type = slot_type  # None=全部子 tab; 'expert'/'normal'/'specialty' 限定采集深度
        self.live_timeout_s = int(live_timeout_s or LIVE_TIMEOUT_S)  # live 批量超时上限(秒, --live-timeout); 到点 v2 优雅停止, 已完成组保留

    # ---------- 1) slot_profile.json (RANKING_SPEC §5) ----------
    def _load_profile(self):
        if self._profile is None:
            self._profile = {}
            if os.path.exists(PROFILE_PATH):
                try:
                    data = json.load(open(PROFILE_PATH, encoding='utf-8'))
                    for p in data.get('profiles', []):
                        self._profile['%s|%s' % (p.get('dept_id'), p.get('hospital_id'))] = p
                    self.log('[slots] 已读 slot_profile.json: %d 条 (dept,hosp)' % len(self._profile))
                except Exception as e:
                    self.log('[slots] slot_profile.json 读取失败(%s), 跳过' % e)
            else:
                self.log('[slots] slot_profile.json 缺失（另一 Agent 并行产出中）→ 降级')
        return self._profile

    def _from_profile(self, hid, did):
        p = self._load_profile().get('%s|%s' % (did, hid))
        if not p:
            return None
        ts = _parse_ts(p.get('last_fetched_at'))
        age = _age_min(ts)
        ttl_ok = age is not None and age <= TTL_MIN
        perennial = bool(p.get('perennial_no_slot'))
        status = 'no_slot' if perennial else ('unknown' if p.get('tension') is None else 'ok')
        return {
            'hospital_id': hid, 'dept_id': did,
            'slot_source': 'cache', 'detail': 'observed',
            'status': status, 'verified': ttl_ok,
            'earliest_date': None, 'earliest_days': None,
            'fee_tier': p.get('price_tier') or 'unknown',
            'fee_tiers': ({p['price_tier']: 1.0} if (p.get('price_tier') in FEE_TIER_ORDER) else {}),
            'fee_min_tier': p.get('price_tier') if p.get('price_tier') in FEE_TIER_ORDER else None,
            'tension': p.get('tension'), 'perennial_no_slot': perennial,
            'lead_days': p.get('bookable_lead_days'), 'release_cycle': p.get('release_cycle'),
            'obs_count': p.get('obs_count'),
            'provenance': {'file': 'slots/profiles/slot_profile.json',
                           'captured_at': p.get('last_fetched_at'),
                           'age_min': round(age, 1) if age is not None else None,
                           'ttl_min': TTL_MIN, 'ttl_ok': ttl_ok},
            'note': ('observed（TTL 内）' if ttl_ok else '观察过期（age_min>TTL）, 按未核实处理')
                    + ('; 可约提前%s天' % p.get('bookable_lead_days') if p.get('bookable_lead_days') is not None else ''),
        }

    # ---------- 2) slots/out 观察快照 ----------
    def _raw_file(self, hid):
        cands = glob.glob(os.path.join(RAW_DIR, hid + '-*.json'))
        if not cands:
            return None
        return max(cands, key=os.path.getmtime)

    def _load_raw(self, hid):
        if hid in self._raw:
            return self._raw[hid]
        data = None
        path = self._raw_file(hid)
        if path:
            try:
                data = json.load(open(path, encoding='utf-8'))
            except Exception:
                data = None
        self._raw[hid] = data
        return data

    def _from_raw(self, hid, did):
        data = self._load_raw(hid)
        if not data:
            return None
        path = self._raw_file(hid)
        for tab in data.get('tabs', []):
            for _name, u in (tab.get('depts') or {}).items():
                if u.get('dept_id') != did:
                    continue
                ts = _parse_ts(u.get('captured_at'))
                age = _age_min(ts)
                ttl_ok = age is not None and age <= TTL_MIN
                d14 = u.get('days_14') or []
                n_has = sum(1 for d in d14 if d.get('has_slot'))
                earliest = u.get('earliest_bookable')
                ed = None
                if earliest:
                    try:
                        ed = (dt.datetime.strptime(earliest, '%Y-%m-%d')
                              - _now().replace(hour=0, minute=0, second=0, microsecond=0)).days
                    except Exception:
                        ed = None
                return {
                    'hospital_id': hid, 'dept_id': did, 'dept_name': u.get('canonical'),
                    'slot_source': 'cache', 'detail': 'observed',
                    'status': u.get('status', 'unknown'), 'verified': ttl_ok,
                    'earliest_date': earliest, 'earliest_days': ed,
                    'fee_tier': u.get('fee_tier') or 'unknown',
                    'fee_tiers': (u.get('fee_tiers') if isinstance(u.get('fee_tiers'), dict) and u.get('fee_tiers')
                                  else ({u['fee_tier']: 1.0} if u.get('fee_tier') in FEE_TIER_ORDER else {})),
                    'fee_min_tier': (u.get('fee_min_tier') if u.get('fee_min_tier') in FEE_TIER_ORDER
                                     else (u.get('fee_tier') if u.get('fee_tier') in FEE_TIER_ORDER else None)),
                    'tension': None, 'perennial_no_slot': False,
                    'days14_has': n_has, 'days14_total': len(d14),
                    'bookable_doctor_count': u.get('bookable_doctor_count'),
                    'provenance': {'file': os.path.relpath(path, WS).replace('\\', '/') if path else 'slots/out',
                                   'captured_at': u.get('captured_at'),
                                   'age_min': round(age, 1) if age is not None else None,
                                   'ttl_min': TTL_MIN, 'ttl_ok': ttl_ok},
                    'note': ('observed（TTL 内）: 近14天 %d/%d 天有号' % (n_has, len(d14)) if ttl_ok
                             else '观察过期（captured_at=%s, 已 %d 分钟 > TTL %d 分钟）, 仅作陈旧参考'
                             % (u.get('captured_at'), int(age or 0), TTL_MIN)),
                }
        return None

    # ---------- 3) live: v2 --condition 批量抓取（主流程循环前调一次 prepare_live） ----------
    def _manifest_file_map(self):
        if self._manifest_map is None:
            m = {}
            mdir = os.path.join(WS, 'hospital_intel', 'manifests')
            for f in os.listdir(mdir):
                if not f.endswith('.json') or f == '_index.json':
                    continue
                try:
                    d = json.load(open(os.path.join(mdir, f), encoding='utf-8'))
                except Exception:
                    continue
                if isinstance(d, dict) and d.get('id'):
                    m[d['id']] = f
            self._manifest_map = m
            self.log('[slots] manifest 索引: %d 家' % len(m))
        return self._manifest_map

    def _manifest(self, hid):
        if hid not in self._manifests:
            f = self._manifest_file_map().get(hid)
            d = None
            if f:
                try:
                    d = json.load(open(os.path.join(WS, 'hospital_intel', 'manifests', f), encoding='utf-8'))
                except Exception:
                    d = None
            self._manifests[hid] = d
        return self._manifests[hid]

    def _dept_route(self, hid, did):
        """(hid, dept_id) -> (tab 名, 页面 raw 科室名)。v2 --condition 需要 tab+raw 名。
        同一 dept_id 在院 manifest 中可出现多次（如 眼科门诊 / 眼科门诊(国际医疗500)）:
        优先可预约 kind（clinic/specialty_clinic/screening）→ 无括号 plain 名 → 最短名（原序兜底）。"""
        m = self._manifest(hid)
        if not m:
            return None
        try:
            l1 = m['features']['bookable_departments']['value']['l1']
        except Exception:
            return None
        entries = [(t.get('name'), u) for t in l1 for u in (t.get('depts') or [])
                   if u.get('dept_id') == did]
        if not entries:
            return None
        def _rank(e):
            _tname, u = e
            raw = u.get('raw', '')
            return (0 if u.get('kind') in ALLOWED_KINDS else 1,
                    1 if ('(' in raw or '\uff08' in raw) else 0, len(raw))
        entries.sort(key=_rank)
        tname, u = entries[0]
        if not tname or not u.get('raw'):
            return None
        return tname, u['raw']

    def _hosp_name_map(self):
        """hid -> 医院中文名（manifests/_index.json, 轻量; 供日志可读性）。"""
        if not hasattr(self, '_hosp_names'):
            self._hosp_names = {}
            try:
                _idx = json.load(open(os.path.join(WS, 'hospital_intel', 'manifests', '_index.json'), encoding='utf-8'))
                for _e in _idx:
                    if isinstance(_e, dict) and _e.get('id'):
                        self._hosp_names[_e['id']] = _e.get('name') or ''
            except Exception:
                pass
        return self._hosp_names

    def build_live_conditions(self, queries):
        """queries: [(hid, did, canonical)] -> (conds, routes)。
        conds: v2 --condition-file 的条件列表（hospital=hid, tab/dept 来自 manifest 路由）;
        routes: {(hid, did): {'tab','dept'}} 供结果回配。"""
        conds, routes = [], {}
        for hid, did, _canon in queries:
            r = self._dept_route(hid, did)
            if not r:
                continue
            tab, raw = r
            c = {'hospital': hid, 'tab': tab, 'dept': raw}
            if self.slot_type:
                c['slot_type'] = self.slot_type
            conds.append(c)
            routes[(hid, did)] = {'tab': tab, 'dept': raw}
        return conds, routes

    def prepare_live(self, queries, budget_s=None):
        """批量 live 抓取（v2 --condition）: 一次 adb 会话覆盖全部 (院, 科) 条件组,
        nav_state 复用最小化导航。仅 --live 时由主流程在号源循环前调用;
        任何失败 → 记日志并降级档位/stub, 流水线照跑（§4.5 诚实标注）。"""
        if not self.allow_live or not queries:
            return
        if not os.path.exists(FETCH_V2):
            self.log('[slots] %s 缺失 → 跳过 live' % os.path.relpath(FETCH_V2, WS))
            return
        conds, routes = self.build_live_conditions(queries)
        self.live_coverage = {'n_routes': len(routes), 'n_parsed': 0, 'n_units': 0,
                              'stop_reason': None, 'run_file': None}
        if not conds:
            self.log('[slots] live 无可路由的 (院, 科)（manifest 缺失或 dept_id 不在院 manifest）→ 跳过')
            return
        outdir = os.path.join(APP_DIR, 'out')
        os.makedirs(outdir, exist_ok=True)
        ts = dt.datetime.now().strftime('%Y%m%dT%H%M%S')
        cfile = os.path.join(outdir, 'live_conds_%s.json' % ts)
        payload = {'conditions': conds}
        if self.device:
            payload['device'] = self.device
        with open(cfile, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False)
        _budget = int(budget_s or self.live_timeout_s)
        est_per = {'expert': 240, 'normal': 420, 'specialty': 480}.get(self.slot_type, 540)
        est_s = len(conds) * est_per
        if est_s > _budget:
            self.log('[slots] ⚠ live 预算预警: %d 条件组 × 约%d秒/组 ≈ %d分钟 > 超时上限 %d分钟; '
                     '到点后后续组不跑（v2 逐组增量落盘, 已完成组保留; 未跑组降级档位）。'
                     '建议: --live-slot-type expert(最快档) / --live-timeout 调大 / 离线跑 fetch_slots.py 批量积累' % (
                         len(conds), est_per, est_s // 60, _budget // 60))
        t0 = time.time()
        cmd = [sys.executable, FETCH_V2, '--condition-file', cfile]
        if self.device:
            cmd += ['--device', self.device]
        self.log('[slots] 触发 v2 批量 live 抓取: %d 个条件组, 顺序=静态分序(第1名先跑), 超时上限=%d 分钟'
                 % (len(conds), _budget // 60))
        _names = self._hosp_name_map()
        for i, c in enumerate(conds):
            self.log('   %2d) %s %s / tab=%s / dept=%s' % (i + 1, c['hospital'], _names.get(c['hospital'], ''), c['tab'], c['dept']))
        self.log('[slots] 条件文件: %s（UTF-8 免损坏通道, CJK 不经 argv）' % os.path.relpath(cfile, WS))
        _dl = (dt.datetime.now() + dt.timedelta(seconds=_budget)).isoformat(timespec='seconds')
        cmd += ['--deadline', _dl]
        self.log('[slots] v2 --deadline=%s: 到点优雅停止(不跑后续组); v2 逐组增量落盘, 已完成组不丢失' % _dl)
        try:
            # 2026-10-06: 无控制台父进程(pythonw)下避免 v2 弹出控制台窗口; 日志仍写 LOG_FILE.
            subprocess.run(cmd, timeout=_budget + 900, cwd=WS,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired:
            self.log('[slots] live 抓取超过硬上限（>%ds）→ 硬杀; v2 应已在 deadline 优雅停止, 增量落盘的已完成组仍可用'
                     % (_budget + 900))
            try:
                ns = json.load(open(os.path.join(RAW_DIR, 'nav_state.json'), encoding='utf-8'))
                self.log('[slots] 硬杀时 v2 已进行到: %s %s / tab=%s / dept=%s / subtab=%s (ts=%s); '
                         '已完成组已增量落盘 condition_runs(run 摘要 + _partial.jsonl), 其余组未跑' % (
                             ns.get('hid'), ns.get('hosp'), ns.get('tab'), ns.get('dept'),
                             ns.get('subtab'), ns.get('ts')))
            except Exception:
                pass
            return
        except Exception as e:
            self.log('[slots] live 抓取失败: %s → 降级档位/stub' % str(e)[:120])
            return
        self.log('[slots] v2 批量 live 抓取结束（%.0fs）, 解析 condition_runs 输出…' % (time.time() - t0))
        self._parse_live_runs(t0, routes)

    # ---------- 3b) live probe 快速探测 (2026-10-06, 两阶段架构 Phase A) ----------
    def prepare_probe(self, queries, budget_s=None):
        """Phase A: 号源可得性快速探测 (v2 --mode probe): L2 科室页单页信号
        (「去预约」按钮/日期条/空态), **不逐医生点进主页** => 预算内可覆盖几十家医院,
        用于初排(有号>未核实>无号 的两级排序)。结果写入 _probe_results(resolve 层 3.5);
        任何失败 -> 记日志跳过(降级深核/档位)。"""
        if not self.allow_live or not queries:
            return
        if not os.path.exists(FETCH_V2):
            self.log('[slots] %s 缺失 -> 跳过 probe' % os.path.relpath(FETCH_V2, WS))
            return
        _budget = int(budget_s or max(300, int(self.live_timeout_s * 0.55)))
        conds, routes = self.build_live_conditions(queries)
        self.probe_coverage = {'n_routes': len(routes), 'n_parsed': 0, 'n_units': 0,
                               'stop_reason': None, 'run_file': None, 'budget_s': _budget}
        if not conds:
            self.log('[slots] probe 无可路由的 (院, 科)(manifest 缺失或 dept_id 不在院 manifest) -> 跳过')
            return
        outdir = os.path.join(APP_DIR, 'out')
        os.makedirs(outdir, exist_ok=True)
        ts = dt.datetime.now().strftime('%Y%m%dT%H%M%S')
        cfile = os.path.join(outdir, 'probe_conds_%s.json' % ts)
        payload = {'conditions': conds}
        if self.device:
            payload['device'] = self.device
        with open(cfile, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False)
        est_s = len(conds) * 90
        if est_s > _budget:
            self.log('[slots] probe 预算预警: %d 组 x ~90s ≈ %d分钟 > 预算 %d分钟; '
                     '到点后后续组不跑(v2 逐组增量落盘, 已完成组保留; 未跑组降级深核/档位)'
                     % (len(conds), est_s // 60, _budget // 60))
        t0 = time.time()
        cmd = [sys.executable, FETCH_V2, '--mode', 'probe', '--condition-file', cfile]
        if self.device:
            cmd += ['--device', self.device]
        self.log('[slots] 触发 Phase A 快速探测: %d 个(院, 主科室)组, 顺序=静态分序(第1名先跑), '
                 '预算=%d分钟 (L2 单页: 按钮/日期条/空态, 不逐医生点击)' % (len(conds), _budget // 60))
        _names = self._hosp_name_map()
        for i, c in enumerate(conds):
            self.log('   %2d) %s %s / tab=%s / dept=%s' % (i + 1, c['hospital'], _names.get(c['hospital'], ''), c['tab'], c['dept']))
        self.log('[slots] 条件文件: %s（UTF-8 免损坏通道, CJK 不经 argv）' % os.path.relpath(cfile, WS))
        _dl = (dt.datetime.now() + dt.timedelta(seconds=_budget)).isoformat(timespec='seconds')
        cmd += ['--deadline', _dl]
        self.log('[slots] probe --deadline=%s: 到点优雅停止(不跑后续组); v2 逐组增量落盘, 已完成组不丢失' % _dl)
        try:
            subprocess.run(cmd, timeout=_budget + 600, cwd=WS,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired:
            self.log('[slots] probe 超过硬上限（>%ds）-> 硬杀; v2 应已在 deadline 优雅停止, 已完成组保留' % (_budget + 600))
            return
        except Exception as e:
            self.log('[slots] probe 抓取失败: %s -> 跳过(降级深核/档位)' % str(e)[:120])
            return
        self.log('[slots] Phase A 快速探测结束（%.0fs）, 解析 probe 结果…' % (time.time() - t0))
        self._parse_probe_runs(t0, routes)

    def _parse_probe_runs(self, since_t0, routes):
        """读 live 抓取后最新 mode=probe 的 run_<ts>.json, 把 unit 映射进 _probe_results[(hid, did)]。"""
        if not os.path.isdir(COND_RUNS_DIR):
            self.log('[slots] condition_runs/ 不存在 -> 无 probe 结果')
            return
        cands = []
        for f in os.listdir(COND_RUNS_DIR):
            if f.startswith('run_') and f.endswith('.json'):
                fp = os.path.join(COND_RUNS_DIR, f)
                try:
                    if os.path.getmtime(fp) >= since_t0 - 10:
                        cands.append((os.path.getmtime(fp), f, fp))
                except OSError:
                    pass
        cands.sort()
        run = None
        rf = None
        for _mt, f, fp in reversed(cands):
            try:
                r = json.load(open(fp, encoding='utf-8'))
            except Exception:
                continue
            if r.get('mode') == 'probe':
                run, rf = r, f
                break
        if run is None:
            self.log('[slots] probe 抓取后无 mode=probe 的新 run_*.json -> 无 probe 结果（看上方子进程输出）')
            self.probe_coverage = dict(getattr(self, 'probe_coverage', None) or {}, n_parsed=0)
            return
        ts = run.get('ts')
        device = run.get('device')
        units = {}
        for u in run.get('units', []):
            if u.get('mode') == 'probe':
                units[(u.get('hospital_id'), u.get('dept'))] = u
        n = {'ok': 0, 'no_slot': 0, 'unknown': 0}
        for (hid, did), rt in routes.items():
            u = units.get((hid, rt['dept']))
            if u is None:
                self.log('[slots] probe 未匹配到条件组 %s/%s（可能未跑到: stop_reason=%s）'
                         % (hid, rt['dept'], run.get('stop_reason')))
                continue
            info = self._probe_info(hid, did, rt, u, ts, device)
            if info:
                self._probe_results[(hid, did)] = info
                n[info['status']] += 1
        self.log('[slots] probe 结果: %d 组解析 -> 有号 ok=%d / 无号 no_slot=%d（run=%s, stop_reason=%s）'
                 % (len(self._probe_results), n['ok'], n['no_slot'], rf, run.get('stop_reason')))
        _pc = dict(getattr(self, 'probe_coverage', None) or {})
        _pc.update({'n_parsed': len(self._probe_results),
                    'n_units': len(run.get('units', [])),
                    'stop_reason': run.get('stop_reason'),
                    'run_file': 'slots/out/condition_runs/%s' % rf})
        self.probe_coverage = _pc
        if self._probe_results:
            self._persist_live_snapshots()

    def _probe_info(self, hid, did, rt, u, ts, device):
        """probe unit -> SlotInfo(slot_source='live_probe')。
        ok(按钮可见)/bar_hint(日期条有号, 弱信号) => verified ok; no_slot => verified no_slot;
        error => None(不作号源事实, 留给后续层)。"""
        status_u = u.get('status')
        src = u.get('earliest_bookable_source') or ''
        if status_u == 'ok':
            status, conf = 'ok', ('card_btn' if 'card_btn' in src else 'bar_only')
        elif status_u == 'bar_hint':
            status, conf = 'ok', 'bar_only'
        elif status_u == 'no_slot':
            status, conf = 'no_slot', src or 'unknown'
        else:
            return None
        earliest = u.get('earliest_bookable')
        ed = None
        if earliest:
            try:
                ed = (dt.datetime.strptime(earliest, '%Y-%m-%d')
                      - _now().replace(hour=0, minute=0, second=0, microsecond=0)).days
            except Exception:
                ed = None
        bar_dates = [d.get('date') for d in (u.get('bar') or []) if d.get('has_slot')]
        if status == 'ok':
            note = ('快速探测: %d 个「去预约」按钮, 号源类型=%s, 最早日期=%s(信号=%s, 日期条 %s 天)'
                    % (u.get('bookable_doctor_count', 0),
                       ','.join(u.get('bookable_subtabs') or []) or '-',
                       earliest or '-', conf, u.get('bar_days')))
        else:
            note = '快速探测: 无号(信号=%s, 日期条 %s 天)' % (conf, u.get('bar_days'))
        return {
            'hospital_id': hid, 'dept_id': did, 'dept_name': u.get('dept') or rt['dept'],
            'slot_source': 'live_probe', 'detail': 'probe',
            'status': status, 'verified': True,
            'dates': bar_dates, 'earliest_date': earliest, 'earliest_days': ed,
            'fee_tier': 'unknown', 'fee_tiers': {}, 'fee_min_tier': None,
            'tension': None, 'perennial_no_slot': False,
            'n_records': 0, 'bookable_doctor_count': u.get('bookable_doctor_count', 0),
            'probe_confidence': conf, 'bookable_subtabs': u.get('bookable_subtabs') or [],
            'provenance': {'file': 'slots/out/condition_runs/%s_all.jsonl' % ts,
                           'run_file': 'slots/out/condition_runs/run_%s.json' % ts,
                           'device': device,
                           'captured_at': u.get('captured_at') or _now().isoformat(timespec='seconds'),
                           'age_min': 0.0, 'ttl_min': TTL_MIN, 'ttl_ok': True},
            'note': note,
        }
    def _parse_live_runs(self, since_t0, routes, device=None, run_file=None):
        """读 live 抓取后最新 run_<ts>.json + <ts>_all.jsonl, 把 (hid, did) 的 live 结果写入 _live_results。
        2026-10-07: device 参数 = 只认指定设备的 run(多手机并行时防误选他机 run 文件)。"""
        if not os.path.isdir(COND_RUNS_DIR):
            self.log('[slots] condition_runs/ 不存在 → 无 live 结果')
            return
        run = None
        f = None
        if run_file:
            # 2026-10-08: offline catch-up - parse one specific run_*.json directly
            f = run_file
            try:
                run = json.load(open(os.path.join(COND_RUNS_DIR, f), encoding='utf-8'))
            except Exception:
                run = None
            if run is None or run.get('mode') not in (None, 'condition'):
                self.log('[slots] run_file=%s 不可用/非 condition 模式 → 无 live 结果' % f)
                self.live_coverage = dict(getattr(self, 'live_coverage', None) or {}, n_parsed=0)
                return
            if device is not None and run.get('device') not in (device, None):
                self.log('[slots] run_file=%s device 不符(%s) → 跳过' % (f, run.get('device')))
                self.live_coverage = dict(getattr(self, 'live_coverage', None) or {}, n_parsed=0)
                return
        else:
            cands = []
            for _fn0 in os.listdir(COND_RUNS_DIR):
                if _fn0.startswith('run_') and _fn0.endswith('.json'):
                    fp = os.path.join(COND_RUNS_DIR, _fn0)
                    try:
                        if os.path.getmtime(fp) >= since_t0 - 10:
                            cands.append((os.path.getmtime(fp), _fn0, fp))
                    except OSError:
                        pass
            if not cands:
                self.log('[slots] live 抓取后无新 run_*.json → 无 live 结果（v2 可能启动即失败, 看上方子进程输出）')
                self.live_coverage = dict(getattr(self, 'live_coverage', None) or {}, n_parsed=0)
                return
            cands.sort()
            for _mt, _fn, _fp in reversed(cands):
                try:
                    _r = json.load(open(_fp, encoding='utf-8'))
                except Exception:
                    continue
                if _r.get('mode') not in (None, 'condition'):
                    continue
                if device is not None and _r.get('device') not in (device, None):
                    continue
                run = _r
                f = _fn
                break
            if run is None:
                self.log('[slots] no new mode=condition run_*.json after live fetch (may be probe run) -> no live results')
                self.live_coverage = dict(getattr(self, 'live_coverage', None) or {}, n_parsed=0)
                return
        ts = run.get('ts')
        device = run.get('device')
        merged = os.path.join(COND_RUNS_DIR, ts + '_all.jsonl')
        if not os.path.exists(merged) or os.path.getsize(merged) == 0:
            _partial = os.path.join(COND_RUNS_DIR, ts + '_partial.jsonl')
            if os.path.exists(_partial) and os.path.getsize(_partial) > 0:
                _n = sum(1 for _ in open(_partial, encoding='utf-8'))
                self.log('[slots] %s_all.jsonl 缺失/空(超时中断) → 改用增量落盘 _partial.jsonl: %d 条已完成组记录可用' % (ts, _n))
                merged = _partial
        rows_by_ci = {}
        if os.path.exists(merged):
            with open(merged, encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    for ci in rec.get('cond_idxs', []):
                        rows_by_ci.setdefault(ci, []).append(rec)
        units = {}
        for u in run.get('units', []):
            units[(u.get('hospital_id'), u.get('dept'))] = u
        n = {'ok': 0, 'no_slot': 0, 'unknown': 0}
        n_unmatched = 0
        for _rk, rt in routes.items():
            # 2026-10-08: key may be 'hid|did' str (normal_pool) or (hid, did) tuple (web live)
            if isinstance(_rk, str):
                hid, did = _rk.split('|', 1)
            else:
                hid, did = _rk
            u = units.get((hid, rt['dept']))
            if u is None:
                n_unmatched += 1
                if n_unmatched <= 3:
                    self.log('[slots] live 未匹配到条件组 %s/%s（condition_errors=%s）' % (
                        hid, rt['dept'], run.get('condition_errors')))
                continue
            info = self._live_info(hid, did, rt, u, rows_by_ci, ts, device)
            if info:
                self._live_results[(hid, did)] = info
                n[info['status'] if info['status'] in n else 'unknown'] += 1
        self.log('[slots] live 结果: %d 组解析 → ok=%d / no_slot=%d / unknown=%d（run=%s, stop_reason=%s, unmatched=%d）' % (
            len(self._live_results), n['ok'], n['no_slot'], n['unknown'], f, run.get('stop_reason'), n_unmatched))
        self.live_coverage = {'n_routes': len(routes), 'n_parsed': len(self._live_results),
                              'n_units': len(run.get('units', [])),
                              'stop_reason': run.get('stop_reason'),
                              'run_file': 'slots/out/condition_runs/%s' % f}
        if self._live_results:
            self._persist_live_snapshots()

    def _live_info(self, hid, did, rt, u, rows_by_ci, ts, device):
        """单个条件组 unit -> SlotInfo（slot_source='live', 六字段记录聚合）。"""
        hosp, raw = u.get('hospital'), u.get('dept')
        rows = []
        for ci in u.get('cond_idxs', []):
            rows.extend(rows_by_ci.get(ci, []))
        rows = [r for r in rows if r.get('\u5c31\u8bca\u533b\u9662') == hosp and r.get('\u5c31\u8bca\u79d1\u5ba4') == raw]
        status_u = u.get('status')
        if rows:
            status = 'ok'
        elif status_u == 'ok':
            status = 'no_slot'   # 页面已确认解析, 可见窗口内无号（诚实记录, 非"未核实"）
        else:
            status = 'unknown'
        dates = []
        for r in rows:
            m = re.match(r'(\d{4}-\d{2}-\d{2})', str(r.get('\u5c31\u8bca\u65f6\u95f4', '')))
            if m:
                dates.append(m.group(1))
        earliest = min(dates) if dates else None
        ed = None
        if earliest:
            try:
                ed = (dt.datetime.strptime(earliest, '%Y-%m-%d')
                      - _now().replace(hour=0, minute=0, second=0, microsecond=0)).days
            except Exception:
                ed = None
        fees = [r.get('fee_tier') for r in rows if r.get('fee_tier') and r.get('fee_tier') != 'unknown']
        fee_tier = max(set(fees), key=fees.count) if fees else 'unknown'
        tier_cnt = {}
        for _ft in fees:
            tier_cnt[_ft] = tier_cnt.get(_ft, 0) + 1
        fee_tiers = {k: round(v / float(len(fees)), 4) for k, v in tier_cnt.items()} if fees else {}
        _ord = [k for k in tier_cnt if k in FEE_TIER_ORDER]
        fee_min_tier = min(_ord, key=lambda k: FEE_TIER_ORDER[k]) if _ord else None
        doctors = set()
        for pst in (u.get('per_sub_tab') or {}).values():
            doctors.update(pst.get('bookable_doctors') or [])
        if not doctors:
            doctors = {r.get('\u5c31\u8bca\u533b\u751f') for r in rows if r.get('\u5c31\u8bca\u533b\u751f')}
        verified = status in ('ok', 'no_slot')
        if status == 'ok':
            note = ('live v2 六字段: %d 条可约记录（最早 %s, %d 名可约医生, 费档 %s, 窗口=不限日期视图）'
                    % (len(rows), earliest, len(doctors), fee_tier))
        elif status == 'no_slot':
            note = 'live v2: 页面已确认（不限日期视图）, 可见窗口内无可约号源'
        else:
            note = ('live v2: 条件组 status=%s error=%s → 未核实, 不作号源事实'
                    % (status_u, u.get('error_reason')))
        # 2026-10-06 Phase C 素材: 医生名单 + 号段样例(供终裁 LLM 选医生/日期)
        docs_list = []
        for _nm in doctors:
            if _nm and _nm not in docs_list:
                docs_list.append(_nm)
        samples = []
        for r in rows:
            _tt = str(r.get('就诊时间', '') or '')
            _m3 = re.match(r'(\d{4}-\d{2}-\d{2})\s*周[一二三四五六日天]\s*(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})', _tt)
            if _m3 and r.get('就诊医生') and len(samples) < 6:
                samples.append({'date': _m3.group(1), 'time': _m3.group(2) + '-' + _m3.group(3),
                                'fee_tier': r.get('fee_tier'), 'doctor': r.get('就诊医生'),
                                'sub_tab': (r.get('unit') or {}).get('sub_tab')})
        return {
            'hospital_id': hid, 'dept_id': did, 'dept_name': raw,
            'slot_source': 'live', 'detail': 'v2_condition',
            'status': status, 'verified': verified, 'dates': dates,
            'earliest_date': earliest, 'earliest_days': ed,
            'fee_tier': fee_tier, 'fee_tiers': fee_tiers, 'fee_min_tier': fee_min_tier,
            'tension': None, 'perennial_no_slot': False,
            'n_records': len(rows), 'bookable_doctor_count': len(doctors),
            'doctors': docs_list[:20], 'slot_samples': samples,
            'provenance': {'file': 'slots/out/condition_runs/%s_all.jsonl' % ts,
                           'run_file': 'slots/out/condition_runs/run_%s.json' % ts,
                           'device': device,
                           'captured_at': u.get('captured_at') or _now().isoformat(timespec='seconds'),
                           'age_min': 0.0, 'ttl_min': TTL_MIN, 'ttl_ok': True},
            'note': note,
        }

    def _persist_live_snapshots(self):
        """live 结果回写观察快照 slots/out/<hid>-<slug>.json (L2 层, 唯一允许写 slots/ 的点, 2026-10-05):
        下次运行 TTL(90min) 内该(院,科)直接命中 cache/observed(已核实) → 不重复实抓;
        常用科室的静态号源特征随使用逐渐逼近动态真实(迭代收敛)。
        仅回写 verified(ok/no_slot); 与既有快照文件合并(只更新对应 tab/raw 条目, 不动其他科室)。"""
        n = 0
        _both = dict(getattr(self, '_probe_results', {}))
        _both.update(self._live_results)   # deep results override probe (same hospital, same dept)
        for (hid, did), info in sorted(_both.items()):
            if not info.get('verified') or info.get('slot_source') not in ('live', 'live_probe'):
                continue
            try:
                slug = derive_slug(hid)
                path = os.path.join(RAW_DIR, '%s-%s.json' % (hid, slug))
                data = None
                if os.path.exists(path):
                    try:
                        data = json.load(open(path, encoding='utf-8'))
                    except Exception:
                        data = None
                if not isinstance(data, dict):
                    data = {'hospital_id': hid, 'captured_at': None, 'tabs': []}
                data['source'] = 'live_v2_writeback'
                data['updated_at'] = _now().isoformat(timespec='seconds')
                _rt = self._dept_route(hid, did)
                tab = _rt[0] if _rt else None
                raw_name = info.get('dept_name') or ''
                prov = info.get('provenance') or {}
                _dates = sorted(set(info.get('dates') or []))
                _today = _now().replace(hour=0, minute=0, second=0, microsecond=0)
                days14 = []
                for _d in _dates:
                    try:
                        _dd = dt.datetime.strptime(_d, '%Y-%m-%d')
                    except Exception:
                        continue
                    if 0 <= (_dd - _today).days <= 14:
                        days14.append({'date': _d, 'has_slot': True})
                entry = {
                    'dept_id': did, 'canonical': raw_name,
                    'captured_at': _now().isoformat(timespec='seconds'),  # 回写时刻=观察时刻(run ts 为紧凑格式, 不可解析, 勿用)
                    'status': info.get('status'), 'earliest_bookable': info.get('earliest_date'),
                    'fee_tier': info.get('fee_tier'), 'fee_tiers': info.get('fee_tiers') or {},
                    'fee_min_tier': info.get('fee_min_tier'),
                    'days_14': days14,
                    'bookable_doctor_count': info.get('bookable_doctor_count'),
                    'n_records': info.get('n_records'),
                    'source': ('live_v2_probe' if info.get('slot_source') == 'live_probe' else 'live_v2_condition'), 'run_file': prov.get('run_file'),
                    'note': ('live probe 回写(日期条+按钮信号, 未采费档)' if info.get('slot_source') == 'live_probe'
                         else 'live v2 回写(30天不限日期视图观察到的可约日)'),
                }
                tabs = data.setdefault('tabs', [])
                tobj = next((t for t in tabs if t.get('name') == tab), None)
                if tobj is None:
                    tobj = {'name': tab or '未知tab', 'depts': {}}
                    tabs.append(tobj)
                depts = tobj.setdefault('depts', {})
                # 同一 dept_id 只保留最新回写: 清掉旧批次遗留的同 dept_id 别名条目
                # (如 '眼科门诊(国际医疗500)' 与 '眼科门诊' 同映射 D06839),
                # 否则 _from_raw 按 dict 顺序取第一个匹配, 可能取到过期的旧条目
                _old_fee = None
                for _t2 in tabs:
                    _dk = _t2.get('depts') or {}
                    for _kn, _dv in list(_dk.items()):
                        if not (isinstance(_dv, dict) and _dv.get('dept_id') == did):
                            continue
                        if _old_fee is None and isinstance(_dv.get('fee_tier'), str) and _dv.get('fee_tier') in FEE_TIER_ORDER:
                            _old_fee = {'fee_tier': _dv['fee_tier'],
                                        'fee_tiers': _dv.get('fee_tiers') or {_dv['fee_tier']: 1.0},
                                        'fee_min_tier': _dv.get('fee_min_tier') or _dv['fee_tier']}
                        del _dk[_kn]
                if _old_fee and info.get('fee_tier') in (None, 'unknown'):
                    # 2026-10-07: probe 写回(无费档)覆盖旧条目 => 继承旧费档(费档慢变), 避免静态数据费档丢失
                    entry.update(_old_fee)
                depts[raw_name or did] = entry
                tmp = path + '.tmp'
                json.dump(data, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
                os.replace(tmp, path)
                self._raw.pop(hid, None)  # 2026-10-07: 写回后作废该院内存缓存(写回发生在两次 resolve 之间, 旧缓存会遮住新数据 = f89d 费档丢失根因)
                n += 1
                self.log('[slots] 回写快照: %s %s/%s → %s' % (hid, tab, raw_name, os.path.relpath(path, WS)))
            except Exception as e:
                self.log('[slots] 回写快照失败 %s/%s: %s' % (hid, did, str(e)[:100]))
        if n:
            self.log('[slots] live→静态回写完成: %d 条 (下次运行 TTL 内走 cache/observed 已核实, 迭代逼近真实)' % n)

    # ---------- 4) level_defaults.json（档位兜底, 契约见任务书） ----------
    def _load_levels(self):
        if self._levels is None:
            self._levels = {}
            if os.path.exists(LEVELS_PATH):
                try:
                    self._levels = json.load(open(LEVELS_PATH, encoding='utf-8')) or {}
                    self.log('[slots] 已读 level_defaults.json: %d 个 strata' % len(self._levels.get('strata', [])))
                except Exception as e:
                    self.log('[slots] level_defaults.json 读取失败(%s), 跳过' % e)
            else:
                self.log('[slots] level_defaults.json 缺失（另一 Agent 并行产出中）→ 降级到 stub')
        return self._levels

    def _from_levels(self, hid, did, level, hosp_type):
        ld = self._load_levels()
        if not ld:
            return None
        strata = ld.get('strata', [])
        key = '%s|%s' % (level or '未定级', hosp_type or '综合医院')
        st = next((s for s in strata if s.get('key') == key), None)
        if st is None and level:
            st = next((s for s in strata if str(s.get('key', '')).startswith(level)), None)
        if st is None:
            return None
        ed = (st.get('earliest_days') or {}).get('p50')
        tiers = dict(st.get('price_tiers') or {})
        tiers.pop('unknown', None)
        modal_tier = max(tiers, key=lambda k: tiers[k]) if tiers else 'unknown'
        fee_tiers = {k: v for k, v in tiers.items() if isinstance(v, (int, float))}
        _cand = [k for k in fee_tiers if k in FEE_TIER_ORDER and fee_tiers[k] > 0]
        fee_min_tier = min(_cand, key=lambda k: FEE_TIER_ORDER[k]) if _cand else None
        return {
            'hospital_id': hid, 'dept_id': did,
            'slot_source': 'cache', 'detail': 'level_default',
            'status': 'unknown', 'verified': False,
            'earliest_date': None, 'earliest_days': ed,
            'fee_tier': modal_tier, 'fee_tiers': fee_tiers, 'fee_min_tier': fee_min_tier,
            'tension': None, 'perennial_no_slot': False,
            'perennial_no_slot_rate': st.get('perennial_no_slot_rate'),
            'has_slot_rate': st.get('has_slot_rate'),
            'confidence': st.get('confidence'),
            'provenance': {'file': 'slots/profiles/level_defaults.json', 'stratum_key': key,
                           'captured_at': None, 'age_min': None, 'ttl_min': None, 'ttl_ok': False},
            'note': '档位估算（stratum=%s, n_units=%s, confidence=%s; 非实时, 不可当号源事实）'
                    % (key, st.get('n_units'), st.get('confidence')),
        }

    # ---------- 5) 内置 stub ----------
    def _load_stub(self):
        if self._stub is None:
            self._stub = {}
            if os.path.exists(STUB_PATH):
                try:
                    data = json.load(open(STUB_PATH, encoding='utf-8'))
                    self._stub = data.get('entries', {})
                except Exception:
                    self._stub = {}
        return self._stub

    def _from_stub(self, hid, did, dept_name):
        e = self._load_stub().get('%s|%s' % (hid, did))
        if e:
            base = e
        else:
            base = {'status': 'unknown', 'fee_tier': 'unknown', 'tension': None,
                    'earliest_days': None, 'lead_days': None, 'perennial_no_slot': False}
        return {
            'hospital_id': hid, 'dept_id': did, 'dept_name': dept_name,
            'slot_source': 'stub', 'detail': 'fixture' if e else 'synthetic-neutral',
            'status': base.get('status', 'unknown'), 'verified': False,
            'earliest_date': None, 'earliest_days': base.get('earliest_days'),
            'fee_tier': base.get('fee_tier') or 'unknown',
            'fee_tiers': ({base['fee_tier']: 1.0} if base.get('fee_tier') in FEE_TIER_ORDER else {}),
            'fee_min_tier': base.get('fee_tier') if base.get('fee_tier') in FEE_TIER_ORDER else None,
            'tension': base.get('tension'), 'perennial_no_slot': base.get('perennial_no_slot', False),
            'lead_days': base.get('lead_days'),
            'provenance': {'file': 'app/fixtures/stub_slots.json' if e else '内置合成中性值',
                           'captured_at': None, 'age_min': None, 'ttl_min': None, 'ttl_ok': False},
            'note': '内置 stub（合成数据, 非实时号源; 用于 profile 缺失时跑通流水线, 恒标注未核实）',
        }

    # ---------- 编排 ----------
    def resolve(self, hid, did, dept_name, level, hosp_type, l1_cat=''):
        """层序: profile → deep(本run, 带费档) → 观察快照 → probe(本run) → 档位 → stub。
        只有 verified 的结果才作为结论返回; 未核实的一律降级并挂 stale_reference。
        2026-10-07: ① deep 移到文件层之前(本 run 深核更新鲜更完整; 文件层可能是无费档的 probe 写回 = f89d 根因);
        ② 结论无费档时从快照文件回填(fee_from_stale 标记)。"""
        info = self._resolve_layers(hid, did, dept_name, level, hosp_type, l1_cat)
        self._fee_backfill(info, hid, did)
        return info

    def _resolve_layers(self, hid, did, dept_name, level, hosp_type, l1_cat=''):
        stale_ref = None
        info = self._from_profile(hid, did)
        if info:
            if info['verified']:
                return info
            stale_ref = info
        lv = self._live_results.get((hid, did))
        if lv:
            if lv['verified']:
                return lv
            stale_ref = stale_ref or lv
        info = self._from_raw(hid, did)
        if info:
            if info['verified']:
                return info
            stale_ref = stale_ref or info
        pv = self._probe_results.get((hid, did))
        if pv:
            if pv['verified']:
                return pv
            stale_ref = stale_ref or pv
        lvl = self._from_levels(hid, did, level, hosp_type)
        if lvl:
            lvl['stale_reference'] = stale_ref
            return lvl
        st = self._from_stub(hid, did, dept_name)
        st['stale_reference'] = stale_ref
        return st

    def _fee_backfill(self, info, hid, did):
        """2026-10-07: 结论无费档(probe / 六字段未含费档) → 从快照文件同 dept_id 条目回填。
        文件条目状态可能已过期(费档为慢变字段, 仅作展示参考), 标记 fee_from_stale=True。"""
        if not isinstance(info, dict) or info.get('fee_tier') not in (None, 'unknown'):
            return
        data = self._load_raw(hid)
        if not data:
            return
        for tab in data.get('tabs', []):
            for _name, u in (tab.get('depts') or {}).items():
                if not isinstance(u, dict) or u.get('dept_id') != did:
                    continue
                ft = u.get('fee_tier')
                if ft not in FEE_TIER_ORDER:
                    return
                tiers = u.get('fee_tiers') if isinstance(u.get('fee_tiers'), dict) and u.get('fee_tiers') else {ft: 1.0}
                info['fee_tier'] = ft
                info['fee_tiers'] = tiers
                info['fee_min_tier'] = (u.get('fee_min_tier') if u.get('fee_min_tier') in FEE_TIER_ORDER else ft)
                info['fee_from_stale'] = True
                _note = info.get('note') or ''
                if '费档回填' not in _note:
                    info['note'] = (_note + '; ' if _note else '') + '费档由历史观察回填(fee_from_stale, 仅展示参考, 非实时费档)'
                return
