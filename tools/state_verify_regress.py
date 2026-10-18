# -*- coding: utf-8 -*-
"""离线回归: 用真实语料 dump 证明"子tab 是否真的激活"现在能被判出来(以及身份校验)。

跑法: python tools/state_verify_regress.py
不需要手机/adb —— 用 corpus 里的 dump 原文喂给改后的判定函数。
"""
import io
import os
import sys
import glob
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, 'mobile', 'controller-app', 'app', 'python')
sys.path.insert(0, PY)
sys.path.insert(0, os.path.join(PY, 'slots'))

# 端侧专属模块打桩(devbridge 只在手机上存在; 采集路径不碰真机)
_stub = types.ModuleType('devbridge')


def _noop(*a, **k):
    return None


for _n in ('emit_step', 'emit_log', 'emit_reasoning', 'setResult', 'dumpJson', 'tap', 'swipe',
           'back', 'keyevent', 'pressEnter', 'inputText', 'waitForText', 'screenSize',
           'lastKnownLocation', 'now', 'getConfig', 'setConfig', 'deviceKey', 'dataDir',
           'outDir', 'extractAssetsIfNeeded', 'launch', 'isReady'):
    setattr(_stub, _n, _noop)
sys.modules['devbridge'] = _stub

import fetch_slots as F          # noqa: E402
import page_state as PS          # noqa: E402
from slots import six_fields     # noqa: E402

sixm = lambda s: bool(six_fields.match_six_page(s))   # 医生主页(six)判定回调, 由外部注入
obs_of = lambda s: PS.observe(s, six_match=sixm)

F.time.sleep = lambda x: None    # 判定逻辑回归, 不等真实渲染

CORPUS = [os.path.join(ROOT, 'slots', 'out', 'raw'), os.path.join(ROOT, 'slots', 'out', 'diag')]
files = []
for d in CORPUS:
    files += glob.glob(os.path.join(d, '*.xml'))

fails = []


def chk(name, cond, detail=''):
    print(('  PASS %-42s %s' % (name, detail)) if cond else ('  FAIL %-42s %s' % (name, detail)))
    if not cond:
        fails.append(name)


class FakeAdb(object):
    def __init__(self, seq):
        self.seq = list(seq)
        self.i = 0
        self.taps = []

    def dump(self):
        s = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return s

    def tap(self, x, y):
        self.taps.append((x, y))


# ---------- A. 子tab 硬判据: 同一份 dump, 目标不同 => 结论必须不同 ----------
print('== A. 子tab 激活判定(真实 L2 dump) ==')
hit = None
for fp in files:
    s = io.open(fp, encoding='utf-8', errors='replace').read()
    if F.page_kind(s) == 'L2' and PS.active_name(s):
        hit = (fp, s, PS.active_name(s))
        if hit[2] == '普通门诊':
            break
fp, s0, act0 = hit
subs = PS.subtab_nodes(PS.raw_nodes(s0))
print('  样本: %s' % os.path.basename(fp))
print('  页面 selected=%r  子tab 行=%s' % (act0, [n['text'] for n in subs]))

# 目标 = 页面上真正 selected 的那个 => ok
node = [n for n in subs if n['text'] == act0][0]
tnode = (node['text'], node['cls'], node['x1'], node['y1'], node['x2'], node['y2'])
st, _s = F._tap_subtab_and_wait(None, FakeAdb([s0]), tnode, ('__prev__',))
chk('A1 目标=页面 selected 的 tab => ok', st == 'ok', 'st=%s' % st)

# 目标 = 另一个 tab(旧版会因"暂无可预约"/卡集变化误判 ok) => 必须判未生效
other = [n for n in subs if n['text'] != act0]
if other:
    o = other[0]
    tnode2 = (o['text'], o['cls'], o['x1'], o['y1'], o['x2'], o['y2'])
    st2, _s2 = F._tap_subtab_and_wait(None, FakeAdb([s0]), tnode2, ('__prev__',))
    chk('A2 目标=别的 tab => 不得为 ok', st2 != 'ok', 'st=%s (旧版这里会误判 ok)' % st2)
else:
    chk('A2 存在第二个 tab', False, '样本只有一个子tab')

# ---------- B. 身份校验: 文件名声称的科室 vs 页面实测 ----------
# 注意: 文件名解析必须与 tools/page_state_check.py 用同一个 fname_ctx ——
# diag 语料里有 SH-001_c5_182701.xml 这类**诊断占位名**(c5/c6...), 朴素地取 parts[1]
# 会把 'c5' 当科室名, 虚增不符计数(实测虚增 236 份)。
print('== B. 科室身份校验(全语料) ==')
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import page_state_check as CK      # noqa: E402

bad = good = 0
bad_lv = {}
for fp in files:
    ctx = CK.fname_ctx(fp)
    if not ctx or not ctx.get('dept'):
        continue
    s = io.open(fp, encoding='utf-8', errors='replace').read()
    obs = obs_of(s)
    if obs.level not in ('L2', 'six'):
        continue
    ok, _why = PS.dept_consistent(s, ctx['dept'], obs)
    if ok:
        good += 1
    else:
        bad += 1
        bad_lv[obs.level] = bad_lv.get(obs.level, 0) + 1
chk('B1 能抓出错标证据', bad > 0, '抓到 %d 份(文件名科室在页面身份位上不存在) %s' % (bad, bad_lv))
chk('B2 不误伤一致证据', good > 0, '通过 %d 份' % good)
print('  比例: 不符 %d / 合计 %d' % (bad, bad + good))

# ---------- C. verify: 期望与实测不符必须报出来 ----------
print('== C. Nav.verify 判定 ==')
ok1, o1, _ = F.Nav(FakeAdb([s0]), 'x').verify({'level': 'L2', 'subtab': act0}, what='A')
ok2, o2, _ = F.Nav(FakeAdb([s0]), 'x').verify({'level': 'L2', 'subtab': '不存在的门诊'}, what='B')
chk('C1 期望与实测一致 => True', ok1, o1.brief())
chk('C2 期望与实测不符 => False', not ok2, o2.brief())

# ---------- D. act(): 动作之后必须"等到"目标态, 等不到就不放行 ----------
print('== D. act() 等到目标态 ==')


class FakeSeq(object):
    """按 tap 次数切换屏幕: 前 switch_at 次点之前是 A 页, 点之后变成 B 页。"""

    def __init__(self, pages, switch_at=1):
        self.pages = list(pages)
        self.switch_at = switch_at
        self.taps = 0

    def dump(self):
        return self.pages[0] if self.taps < self.switch_at else self.pages[1]

    def tap(self, x, y):
        self.taps += 1


wrong = six = None
for fp2 in files:
    if fp2 == fp:
        continue
    s2 = io.open(fp2, encoding='utf-8', errors='replace').read()
    a2 = PS.active_name(s2)
    if wrong is None and F.page_kind(s2) == 'L2' and a2 and a2 != act0:
        wrong = (fp2, s2, a2)
    if six is None and F.page_kind(s2) == 'six':
        six = (fp2, s2)
    if wrong and six:
        break
chk('D0 对照语料(另一 selected 的 L2 + 一份六字段页)', bool(wrong and six),
    '%s selected=%s / %s' % (os.path.basename(wrong[0]), wrong[2],
                             os.path.basename(six[0])) if (wrong and six) else '语料不足')
if wrong:
    w = [n for n in PS.subtab_nodes(PS.raw_nodes(wrong[1])) if n['text'] == act0]
    if w:
        tn = (w[0]['text'], w[0]['cls'], w[0]['x1'], w[0]['y1'], w[0]['x2'], w[0]['y2'])
        # D1: 点之前停在别的 tab, 点之后才切过去 => 必须判 ok(且确实等到)
        seq = FakeSeq([wrong[1], s0], switch_at=1)
        st, _s = F._tap_subtab_and_wait(None, seq, tn, ('__prev__',))
        chk('D1 点后才切过去 => ok(等到才认)', st == 'ok', 'st=%s taps=%d' % (st, seq.taps))
        # D2: 点了但页面不动 => 不得为 ok, 且只试 max_attempts 次
        seq2 = FakeSeq([wrong[1], wrong[1]], switch_at=99)
        st2, _s2 = F._tap_subtab_and_wait(None, seq2, tn, ('__prev__',))
        chk('D2 点了不生效 => 不为 ok', st2 != 'ok', 'st=%s taps=%d(=max_attempts)' % (st2, seq2.taps))
    else:
        chk('D1/D2 对照页上有目标 tab', False, '%s 上找不到 %r' % (os.path.basename(wrong[0]), act0))
class FakeSeqList(object):
    """按 dump 次数依次给页面(测容错: 第一帧是半成品, 第二帧才定性)。"""

    def __init__(self, pages):
        self.pages = list(pages)
        self.i = 0
        self.taps = 0

    def dump(self):
        s = self.pages[min(self.i, len(self.pages) - 1)]
        self.i += 1
        return s

    def tap(self, x, y):
        self.taps += 1


if six:
    # D3: 期望 L2 却在六字段页 => 立刻判"离开目标页", 不空转
    tn3 = (act0, 'android.view.View', 100, 400, 300, 450)
    seq3 = FakeSeq([six[1], six[1]], switch_at=99)
    st3, _s3 = F._tap_subtab_and_wait(None, seq3, tn3, ('__prev__',))
    chk('D3 走错页(六字段) => 立即中止不空转', st3 == 'left' and seq3.taps == 1,
        'st=%s taps=%d' % (st3, seq3.taps))
    # D6: 第一帧是 SPA 重建的半成品(unknown) => 不得定性为"离开", 第二帧才判
    unk = '<hierarchy><node text="半成品" bounds="[0,0][100,100]"/></hierarchy>'
    chk('D6 半成品帧判为 unknown(而非 L2)', obs_of(unk).level == 'unknown',
        'level=%s' % obs_of(unk).level)
    st6, _s6 = F._tap_subtab_and_wait(None, FakeSeqList([unk, s0, s0]), tn3, ('__prev__',))
    chk('D6 半成品帧不中止 => 下一帧判 ok', st6 == 'ok', 'st=%s' % st6)

# D4: 无动作(纯核对)且当前已符合 => ok 且一次都不点
fa = FakeAdb([s0])
ok4, o4, _s4, why4 = F.act(fa, {'level': 'L2', 'subtab': act0}, None,
                           state='D4', what='D4', timeout=1.0)
chk('D4 无动作+已符合 => ok 且不点', ok4 and not fa.taps, 'st=%s taps=%d' % (o4.level, len(fa.taps)))
# D5: 无动作但当前不符合 => 不得为 ok(旧代码在这里会"睡了就当成功")
fa2 = FakeAdb([wrong[1]] if wrong else [s0])
ok5, o5, _s5, why5 = F.act(fa2, {'level': 'L2', 'subtab': act0}, None,
                           state='D5', what='D5', timeout=0.5)
chk('D5 无动作+不符合 => 不放行', (not ok5) or (not wrong), 'level=%s why=%s' % (o5.level, (why5 or '')[:60]))

# ---------- D7. 塌陷帧(端侧实测 2026-10-15 20:49, 476cf991, SH-084 眼科门诊) ----------
# 同一帧: 观测层 tabs=[专家,普通,专病](selected=专家), 但整页节点零高 => 几何筛空。
# 旧代码在这里判 '(单列,无子tab)', 把默认激活的 专家门诊 列表当 普通门诊 采走。
print('== D7. 塌陷帧(存在但不可点 => 不得判单列) ==')
fx = os.path.join(ROOT, 'tools', 'fixtures', 'L2_collapsed_SH-084_眼科门诊.xml')
if os.path.exists(fx):
    sc = io.open(fx, encoding='utf-8', errors='replace').read()
    oc = PS.observe(sc)
    chk('D7a 塌陷帧 => level=unknown', oc.level == 'unknown' and bool(oc.collapsed), oc.brief()[:110])
    chk('D7b 观测层仍看到 3 个子tab(存在性)',
        F.subtab_names(sc) == ['专家门诊', '普通门诊', '专病门诊'], F.subtab_names(sc))
    _geo = F.find_subtab_nodes(F.parse_nodes(sc))
    chk('D7c 几何不可点(find_subtab_nodes 筛空)', _geo == [], _geo)
    chk('D7d 存在性!=可点性 => 上层必须报错(不得降级单列)',
        bool(F.subtab_names(sc)) and not _geo, 'names=%s geo=%d' % (F.subtab_names(sc), len(_geo)))
    chk('D7e 对照: 正常 L2 样本非塌陷且子tab 可点',
        (not PS.observe(s0).collapsed) and bool(F.find_subtab_nodes(F.parse_nodes(s0))),
        'collapsed=%s' % PS.observe(s0).collapsed)
else:
    chk('D7 塌陷帧样本存在', False, fx)

# ---------- E. 状态机 nav_machine: 转移表 + 观测驱动(第③步) ----------
print('')
print('== E. 状态机 nav_machine(转移表 + 观测驱动) ==')
import nav_machine as NM          # noqa: E402

for _c, _t in (('doc', 'L2'), ('L2', 'L1'), ('home', 'L2'), ('unknown', 'L2')):
    _e = NM.next_edge(_c, _t)
    print('  查表 %-8s -> %-4s = %s' % (_c, _t, _e[0] if _e else None))
chk('E1 doc->L2 = back_l2(最便宜)', NM.next_edge('doc', 'L2')[0] == 'back_l2')
chk('E1 L2->L1 = back_l1(不兜底)', NM.next_edge('L2', 'L1')[0] == 'back_l1')
chk('E1 home->L2 首边 = search(两跳最便宜)', NM.next_edge('home', 'L2')[0] == 'search')
chk('E1 unknown->L2 = reset(兜底边)', NM.next_edge('unknown', 'L2')[0] == 'reset')

# 真实语料当"屏幕": 一份 L1、一份 login、加 s0(正常 L2)、fx(塌陷帧)
def _read(fp):
    return io.open(fp, encoding='utf-8', errors='replace').read()


l1f = next((f for f in files if PS.observe(_read(f)).level == 'L1'), None)
lgf = next((f for f in files if PS.observe(_read(f)).level == 'login'), None)
s_l1, s_login = (_read(l1f) if l1f else None), (_read(lgf) if lgf else None)
dept0 = PS.observe(s0).dept
print('  语料屏幕: L1=%s login=%s L2样本dept=%r' % (bool(s_l1), bool(s_login), dept0))


class Screen(object):
    """假屏幕: 每次 dump 前进一格(模拟 SPA 重建后自己变好); act 语义与 fetch_slots.act 一致。"""

    def __init__(self, frames):
        self.frames, self.i, self.acts = list(frames), 0, []

    def dump(self):
        s = self.frames[min(self.i, len(self.frames) - 1)]
        self.i += 1
        return s

    def act(self, expect=None, action=None, state='', what='', timeout=None,
            settle=None, ok_when=None, abort=None, attempts=1, attempt_settle=1.5):
        if action is not None:
            self.acts.append(state)
            action()
        # 动作之后屏幕已是"下一格"; 下一次 dump() 会把它读出来
        cur = self.frames[min(self.i, len(self.frames) - 1)]
        o = obs_of(cur)
        if ok_when is not None:      # 与 fetch_slots.act 一致: ok_when 覆盖默认判据
            ok, why = ok_when(o, cur)
            return ok, o, cur, (why or '')
        ok, diffs = PS.matches(NM.prune_for_level(expect or {}, o.level), o)
        return ok, o, cur, ('' if ok else ';'.join(diffs))


ann = []
scr = Screen([s0])
st, o, s, why, path = NM.run(scr.dump, obs_of, scr.act, {'level': ('L2', 'L2b'), 'dept': dept0}, {},
                             announce=lambda n, oo: ann.append(n), sleep=lambda x: None)
chk('E2 已在目标 L2 => 0 动作即到达', st == 'ok' and not scr.acts, 'st=%s acts=%s' % (st, scr.acts))
chk('E2 浮窗只在"验证到达"后发声', ann == ['arrived'], ann)

scr = Screen([sc] * 12)
st, o, s, why, path = NM.run(scr.dump, obs_of, scr.act, {'level': ('L2', 'L2b'), 'dept': dept0},
                             {'reset': lambda: None}, sleep=lambda x: None)
chk('E3 塌陷帧连读不决策 => 不按猜测点击, 只按表退层重进(且按预算)',
    st == 'fail' and scr.acts == ['machine:reset'] and 'no_edge' in why,
    'st=%s acts=%s why=%s' % (st, scr.acts, why))
scr = Screen([sc, sc, s0])
st, o, s, why, path = NM.run(scr.dump, obs_of, scr.act, {'level': ('L2', 'L2b'), 'dept': dept0},
                             {}, sleep=lambda x: None)
chk('E3 塌陷帧重读后自愈 => ok(仍 0 动作)', st == 'ok' and not scr.acts, 'st=%s acts=%s' % (st, scr.acts))

if s_login:
    scr = Screen([s_login] * 6)
    st, o, s, why, path = NM.run(scr.dump, obs_of, scr.act, {'level': ('L2', 'L2b'), 'dept': dept0},
                                 {'reset': lambda: None}, sleep=lambda x: None)
    chk('E4 登录页 => 只试一次重置, 仍登录则上报 login',
        st == 'login' and scr.acts == ['machine:reset_from_login'] and 'stop_level=login' in why,
        'st=%s acts=%s why=%s' % (st, scr.acts, why))
    if s_l1:
        scr = Screen([s_login, s_l1, s0])
        st, o, s, why, path = NM.run(scr.dump, obs_of, scr.act, {'level': ('L2', 'L2b'), 'dept': dept0},
                                     {'reset': lambda: None, 'open_dept': lambda: None},
                                     sleep=lambda x: None)
        chk('E4 会话超时型登录页 => 重置后恢复并到达', st == 'ok' and path[0][0] == 'reset',
            'st=%s path=%s' % (st, [p[0] for p in path]))

if s_l1:
    scr = Screen([s_l1])
    st, o, s, why, path = NM.run(scr.dump, obs_of, scr.act, {'level': ('L2', 'L2b'), 'dept': dept0},
                                 {'open_dept': lambda: None}, sleep=lambda x: None)
    chk('E5 点了不生效 => 反复重规划后如实 fail', st == 'fail' and path and all(p[0] == 'open_dept' for p in path),
        'st=%s path=%s' % (st, [p[0] for p in path]))
    # 走错科室页: 层级对但身份不对 => 绝不能算到达
    scr = Screen([s_l1, s0])
    st, o, s, why, path = NM.run(scr.dump, obs_of, scr.act,
                                 {'level': ('L2', 'L2b'), 'dept': dept0 + 'XX'},
                                 {'open_dept': lambda: None}, sleep=lambda x: None)
    chk('E6 走错科室页(层级对身份错) => 不算到达', st == 'fail' and path[0][0] == 'open_dept',
        'st=%s path=%s' % (st, [p[0] for p in path]))

# E7 对照: 同一组「层级对、科室错」—— 旧宽松判据会放行, 状态机的严格判据拦住
_want = dept0 + 'XX'
_loose, _ = PS.matches({'level': ('L2', 'L2b'), 'dept': _want}, obs_of(s0))
_strict, _d = PS.matches({'level': ('L2', 'L2b'), 'dept': _want}, obs_of(s0), strict=True)
chk('E7 对照: 宽松放行/严格拦住', _loose and not _strict, 'loose=%s strict=%s' % (_loose, _strict))
_ok_legit, _ = PS.matches({'level': ('L2', 'L2b'), 'dept': dept0}, obs_of(s0), strict=True)
chk('E7 对照: 真到达在严格判据下仍为 ok', _ok_legit, 'dept=%r' % dept0)

# ---- E8/E9: 授权同意页(端侧 2026-10-15 21:2x 476cf991 实测) ----
AUTH = ('<hierarchy><node text="' + PS.AUTH_T + '" bounds="[0,192][1080,1920]"/>'
        '<node text="姓名" bounds="[100,600][300,660]"/>'
        '<node text="手机号" bounds="[100,700][300,760]"/>'
        '<node text="同意" bounds="[400,1500][680,1600]" clickable="true"/></hierarchy>')
chk('E8 授权页 level=auth(不再混进 login)', PS.observe(AUTH).level == 'auth', PS.observe(AUTH).brief()[:110])
scr = Screen([AUTH, s0])
st, o, s, why, path = NM.run(scr.dump, obs_of, scr.act, {'level': ('L2', 'L2b'), 'dept': dept0},
                             {'agree_auth': lambda: None, 'open_dept': lambda: None},
                             ctx={'dept': dept0}, sleep=lambda x: None)
chk('E8b 授权页 => 走 agree_auth 边(不再当停止条件)',
    st == 'ok' and path and path[0][0] == 'agree_auth',
    'st=%s path=%s why=%s' % (st, [p[0] for p in path], why))
scr = Screen([AUTH] * 8)              # 「同意」点了不动 => 配额止损
st, o, s, why, path = NM.run(scr.dump, obs_of, scr.act, {'level': ('L2', 'L2b'), 'dept': dept0},
                             {'agree_auth': lambda: None, 'reset': lambda: None},
                             ctx={'dept': dept0}, sleep=lambda x: None)
n_ag = sum(1 for p in path if p[0] == 'agree_auth')
chk('E9 同意点不动 => 该边 2 次后禁用(不无限点)',
    n_ag <= 2 and st == 'fail', 'st=%s agree=%d path=%s' % (st, n_ag, [p[0] for p in path]))

# ---------- F. goto_unit 由状态机接管: pos 的"声称"不再驱动点击 ----------
print('')
print('== F. goto_unit(状态机版): pos 声称 vs 屏幕实测 ==')


class FakeNav2(object):
    def __init__(self, scr):
        self.scr, self.calls = scr, []

    def open_dept(self, tab, dept, plan_order, first_d):
        self.calls.append('open_dept')
        self.scr.jump(1)          # 点完科室, 屏幕进入下一格
        return 'ok', None, True

    def enter(self):
        self.calls.append('enter')
        return 'ok', None

    def ensure_444(self, max_rounds=6):
        self.calls.append('ensure_444')
        return True

    def reset_full(self, tries=1):
        self.calls.append('reset_full')
        return 'ok', None


class Screen2(Screen):
    def jump(self, i):
        self.i = i

    def back(self):
        pass

    def swipe(self, *a, **k):
        self.swipes = getattr(self, 'swipes', 0) + 1


G = {'hid': 'SH-084', 'hosp': '第十人民医院', 'tab': '眼科', 'dept': dept0, 'plan_order': [],
     'm': {'features': {'bookable_departments': {'value': {'l1': []}}}}}
ST = {'reset': 0, 'enter': 0, 'stay_l2': 0, 'back_to_l1': 0}

F.verify_reset()          # 熔断计数是「一次运行」级别的: 每个独立场景先清零
scr = Screen2([sc] * 8)
nav = FakeNav2(scr)
st, s, pos = F.goto_unit(nav, scr, {'level': 'L1', 'hid': 'SH-084', 'tab': '眼科', 'dept': dept0}, G, dict(ST))
chk('F1 pos 说在 L1 但屏幕是塌陷帧 => 不许点科室(只允许自愈重排/重进)',
    st == 'fail' and 'open_dept' not in nav.calls
    and getattr(scr, 'swipes', 0) >= 1 and 'reset_full' in nav.calls,
    'st=%s nav.calls=%s swipes=%s' % (st, nav.calls, getattr(scr, 'swipes', 0)))

if s_l1:
    F.verify_reset()
    scr = Screen2([s_l1, s0])
    nav = FakeNav2(scr)
    st, s, pos = F.goto_unit(nav, scr, {'level': None}, G, dict(ST))
    chk('F2 L1 -> 点科室 -> 实测到达 L2 => ok', st == 'ok' and nav.calls == ['open_dept'],
        'st=%s calls=%s pos=%s' % (st, nav.calls, (pos or {}).get('level')))
    F.verify_reset()
    scr = Screen2([s_l1, s0])
    nav = FakeNav2(scr)
    st, s, pos = F.goto_unit(nav, scr, {'level': None},
                             dict(G, dept=dept0 + 'XX'), dict(ST))
    chk('F3 点到错科室页 => 不认到达(旧版会认)', st != 'ok', 'st=%s calls=%s' % (st, nav.calls))

# ---------- G. identity 补齐: tab(左列选中) / doctor(分级) ----------
print('')
print('== G. identity 补齐: tab / doctor ==')

if s_l1:
    _o = PS.observe(s_l1)
    chk('G1 L1 语料帧 => tab 取到且来自 selected',
        bool(_o.tab) and _o.tab_how == 'selected', 'tab=%r how=%s' % (_o.tab, _o.tab_how))
else:
    chk('G1 L1 语料帧缺失', False, 'no L1 dump in corpus')


def _l1_frame(sel):
    """左列三个科室大类, sel 里的带 selected=true(语料实测形状: x=0 w=390)。"""
    rows = []
    for i, t in enumerate(['眼科', '外科', '内科']):
        rows.append('<node index="%d" text="%s" resource-id="android:id/text1" '
                    'class="android.view.View" pkg="mobi.w3studio.apps.android.shsmy.phone" '
                    'content-desc="" selected="%s" bounds="[0,%d][390,%d]"/>'
                    % (i, t, 'true' if t in sel else 'false', 700 + i * 130, 830 + i * 130))
    return ('<hierarchy rotation="0">'
            '<node text="第十人民医院" class="android.widget.TextView" bounds="[400,200][800,260]"/>'
            + ''.join(rows) +
            '<node text="选科室" class="android.webkit.WebView" bounds="[0,192][1080,1920]"/>'
            '</hierarchy>')


_o = PS.observe(_l1_frame(['眼科', '外科']))
chk('G2a 两个 selected => tab=None(不猜)', _o.tab is None and _o.tab_how == 'multi',
    'tab=%r how=%s' % (_o.tab, _o.tab_how))
_o = PS.observe(_l1_frame([]))
chk('G2b 零个 selected => tab=None(不猜)', _o.tab is None and _o.tab_how == 'none',
    'tab=%r how=%s' % (_o.tab, _o.tab_how))
_o = PS.observe(_l1_frame(['外科']))
chk('G2c 恰一个 selected => tab=外科', _o.tab == '外科' and _o.tab_how == 'selected',
    'tab=%r how=%s' % (_o.tab, _o.tab_how))
_o = PS.observe(s0)
chk('G2d 非 L1 页 => tab 标 n/a(不是"没有选中")', _o.tab_how == 'n/a',
    'level=%s how=%s' % (_o.level, _o.tab_how))


def _doc_frame(header, dept=None):
    n = ['<node text="医生主页" class="android.webkit.WebView" bounds="[0,192][1080,1920]"/>',
         '<node text="%s" class="android.view.View" bounds="[40,200][700,260]"/>' % header]
    if dept:
        n.append('<node text="%s" class="android.view.View" bounds="[40,270][700,320]"/>' % dept)
    return '<hierarchy rotation="0">' + ''.join(n) + '</hierarchy>'


_o = PS.observe(_doc_frame('刘芳 主任医师'))
chk('G3a 页头带职称 => doctor=刘芳(hard)', _o.doctor == '刘芳' and _o.doctor_how == 'title',
    'doctor=%r how=%s' % (_o.doctor, _o.doctor_how))
_o = PS.observe(_doc_frame('徐建 收藏', '眼科门诊'))
chk('G3b 「姓名 收藏」=> doctor=徐建(≠科室)', _o.doctor == '徐建' and _o.doctor_how == 'fav',
    'doctor=%r how=%s' % (_o.doctor, _o.doctor_how))
_o = PS.observe(_doc_frame('东方医院南院\u00a0\u00a0中医治未病', '中医治未病'))
chk('G3c 页头就是科室名 => doctor=None(不冒充人名)',
    _o.doctor is None and _o.doctor_how in ('dept', 'none'),
    'doctor=%r how=%s' % (_o.doctor, _o.doctor_how))
_o = PS.observe(_doc_frame('第十人民医院', '眼科门诊'))
chk('G3d 页头是医院名 => doctor=None', _o.doctor is None and _o.doctor_how in ('hosp', 'none'),
    'doctor=%r how=%s' % (_o.doctor, _o.doctor_how))
_o = PS.observe(_doc_frame('东方医院南院 眼科门诊 主任医师', '眼科门诊'))
chk('G3e 页头带职称 => 只取人名(剥掉职称)',
    PS.strip_title('刘芳主任医师') == '刘芳' and PS.strip_title('张三') == '张三',
    'doctor=%r how=%s' % (_o.doctor, _o.doctor_how))

_o = PS.observe(_doc_frame('徐建 收藏', '眼科门诊'))
_ok, _d = PS.matches({'doctor': '徐建'}, _o)
chk('G4a doctor 对上 => ok', _ok, _d)
_o = PS.observe(_doc_frame('李四 主任医师'))
_ok, _d = PS.matches({'doctor': '徐建'}, _o)
chk('G4b doctor 不符 => 硬失败', (not _ok) and any('doctor' in x for x in _d), _d)
_o = PS.observe(_doc_frame('眼科门诊', '眼科门诊'))
_ok, _d = PS.matches({'doctor': '徐建'}, _o)
chk('G4c 取不到人名的页不判死(软, 但留 ≈ 痕)', _ok and any('≈doctor' in x for x in _d), _d)
_o = PS.observe(_l1_frame(['外科']))
_ok, _d = PS.matches({'tab': '眼科'}, _o)
chk('G4d tab 不符 => 硬失败(进错大类当场拦下)', (not _ok) and any('tab' in x for x in _d), _d)
_o = PS.observe(_l1_frame([]))
_ok, _d = PS.matches({'tab': '眼科'}, _o)
chk('G4e 无 selected 信号 => 不判死(软)', _ok and any('≈tab' in x for x in _d), _d)

_full = {'hospital': 'h', 'dept': 'd', 'tab': 't', 'doctor': 'p'}
chk('G5a L2 只校 dept(大类/人名该页承载不了)',
    NM.prune_for_level(dict(_full), 'L2') == {'dept': 'd'}, NM.prune_for_level(dict(_full), 'L2'))
chk('G5b L1 校 hospital+tab',
    NM.prune_for_level(dict(_full), 'L1') == {'hospital': 'h', 'tab': 't'},
    NM.prune_for_level(dict(_full), 'L1'))
chk('G5c six 校 dept/doctor/hospital, 不校 tab',
    NM.prune_for_level(dict(_full), 'six') == {'hospital': 'h', 'dept': 'd', 'doctor': 'p'},
    NM.prune_for_level(dict(_full), 'six'))


# ---------- H. 状态声明表(期望/允许/预算/出口) + SUBTAB_ACTIVE 一等状态 ----------
print('')
print('== H. 状态声明表 / SUBTAB_ACTIVE ==')

import re as _re


def _flip_sub(s, off, on):
    """把 L2 dump 里子tab 的 selected 从 off 换到 on(合成「真的切过去了」的帧)。"""
    def _sub(m):
        tag = m.group(0)
        if 'van-tabs-' not in tag:
            return tag
        if ('text="%s"' % off) in tag:
            return tag.replace('selected="true"', 'selected="false"')
        if ('text="%s"' % on) in tag:
            return _re.sub(r'selected="[^"]*"', 'selected="true"', tag)
        return tag
    return _re.sub(r'<node\b[^>]*>', _sub, s)


_o0 = PS.observe(s0)
_sub0 = _o0.subtab_active
_alt = [x for x in (_o0.subtabs or []) if x != _sub0]
sub_alt = _alt[0] if _alt else None
chk('H0 L2 语料样本可用于子tab 合成', bool(_sub0) and bool(sub_alt),
    'active=%r alt=%r tabs=%s' % (_sub0, sub_alt, _o0.subtabs))

s_sw = _flip_sub(s0, _sub0, sub_alt) if sub_alt else s0
_o_sw = PS.observe(s_sw)
chk('H1 合成帧: selected 换到 %r' % sub_alt, (not sub_alt) or _o_sw.subtab_active == sub_alt,
    'observed=%r' % _o_sw.subtab_active)

_ch = NM.chain_edges()
chk('H2 链 HOME->444->L1->L2->SUBTAB->SIX 每条都有边(转移表唯一)',
    all(n for a, b, n in _ch if a != 'IDLE'), _ch)
chk('H2b IDLE 无直达边, 但失败出口由表声明(self_heal)',
    [n for a, b, n in _ch if a == 'IDLE'] == [False] and NM.exit_for('IDLE') == 'self_heal', _ch)

_b = NM.budget_for('SUBTAB_ACTIVE', 'tap_subtab')
chk('H3 预算来自表: tap_subtab attempts=3 timeout=9',
    _b.get('attempts') == 3 and _b.get('timeout') == 9.0, _b)
_b2 = NM.budget_for('L2', 'back_l1')
chk('H3b 其它边沿用状态预算(没被顺手放宽)',
    _b2.get('attempts') == 2 and _b2.get('timeout') == 12.0, _b2)

_a1, _a2 = NM.allowed_actions('L1'), NM.allowed_actions('L2')
chk('H4 允许动作由 EDGES 派生(L1 无 tap_subtab; L2 有)',
    'open_dept' in _a1 and 'tap_subtab' not in _a1 and 'tap_subtab' in _a2, (_a1, _a2))

if sub_alt:
    _ok, _d = NM.entry_ok('SUBTAB_ACTIVE', _o_sw, ctx={'dept': dept0, 'subtab': sub_alt})
    chk('H5 进入谓词: L2 + selected=目标子tab => 可进入', _ok, _d)
    _ok, _d = NM.entry_ok('SUBTAB_ACTIVE', _o0, ctx={'dept': dept0, 'subtab': sub_alt})
    chk('H5b 进入谓词: selected 仍是别的子tab => 不可进入', not _ok, _d)

    scr = Screen2([s0, s_sw])
    st, o, s, why, path = NM.run(scr.dump, obs_of, scr.act,
                                 {'level': ('SUBTAB_ACTIVE',), 'dept': dept0, 'subtab': sub_alt},
                                 {'tap_subtab': lambda: scr.jump(1)},
                                 ctx={'dept': dept0, 'subtab': sub_alt}, sleep=lambda x: None)
    chk('H6 状态机驱动 L2->SUBTAB_ACTIVE(观测到 selected 才算到)',
        st == 'ok' and [p[0] for p in path] == ['tap_subtab'],
        'st=%s path=%s why=%s' % (st, [p[0] for p in path], why))

    scr = Screen2([s0] * 8)
    scr.act_timeout = None
    st, o, s, why, path = NM.run(scr.dump, obs_of, scr.act,
                                 {'level': ('SUBTAB_ACTIVE',), 'dept': dept0, 'subtab': sub_alt},
                                 {'tap_subtab': lambda: None},
                                 ctx={'dept': dept0, 'subtab': sub_alt}, sleep=lambda x: None)
    n_tap = sum(1 for p in path if p[0] == 'tap_subtab')
    chk('H6b 子tab 点不动 => 用掉 attempts 预算后如实 fail(不空转)',
        st != 'ok' and n_tap <= NM.budget_for('SUBTAB_ACTIVE', 'tap_subtab')['attempts'],
        'st=%s taps=%d why=%s' % (st, n_tap, why))

    scr = Screen2([s0, s_sw])
    st, o, s, why, path = NM.run(scr.dump, obs_of, scr.act,
                                 {'level': ('SUBTAB_ACTIVE',), 'dept': dept0 + 'XX',
                                  'subtab': sub_alt},
                                 {'tap_subtab': lambda: scr.jump(1)},
                                 ctx={'dept': dept0 + 'XX', 'subtab': sub_alt},
                                 sleep=lambda x: None)
    chk('H6c 子tab 切对了但科室不对 => 不认到达', st != 'ok',
        'st=%s path=%s' % (st, [p[0] for p in path]))

    chk('H7 状态名映射: L2+selected=>SUBTAB_ACTIVE / 空dump=>IDLE',
        NM.state_of(_o_sw) == 'SUBTAB_ACTIVE' and NM.state_of(PS.observe('')) == 'IDLE',
        '%s %s' % (NM.state_of(_o_sw), NM.state_of(PS.observe(''))))

# ---------- I. 唯一熔断依据: 连续 N 次 verify 失败 ----------
print('')
print('== I. 唯一熔断(连续 verify 失败) ==')

F.set_verify_break(3)
F.verify_reset()
chk('I0 初始: 计数 0 / 未熔断', F.VERIFY['fail'] == 0 and not F.VERIFY['tripped'], str(F.VERIFY))
F.verify_judge(False, 'a', 'w1')
F.verify_judge(False, 'b', 'w2')
chk('I1a 连续 2 次失败 => 还没熔断', F.VERIFY['fail'] == 2 and not F.VERIFY['tripped'],
    'fail=%d tripped=%s' % (F.VERIFY['fail'], F.VERIFY['tripped']))
F.verify_judge(True, 'c')
chk('I1b 中间一次成功 => 立即清零', F.VERIFY['fail'] == 0 and not F.VERIFY['tripped'],
    'fail=%d' % F.VERIFY['fail'])
for _i in range(3):
    F.verify_judge(False, 'd%d' % _i, 'no_match')
chk('I1c 连续 3 次失败 => 熔断 + 如实说明',
    F.VERIFY['tripped'] and '连续 3 次' in (F.VERIFY['why'] or ''), F.VERIFY['why'])
F.verify_reset()

if sub_alt:
    # I2: 状态机在熔断后立刻收手 —— 不再把剩下的 hop 跑完
    F.verify_reset()
    scr = Screen2([s0] * 12)

    def _act_count(**kw):
        """模拟 fetch_slots.act: 每次判定都进唯一记账点。"""
        ok, o, s_, w = scr.act(**kw)
        F.verify_judge(ok, kw.get('what', ''), w, o)
        return ok, o, s_, w

    st, o, s_, why, path = NM.run(scr.dump, obs_of, _act_count,
                                  {'level': ('SUBTAB_ACTIVE',), 'dept': dept0,
                                   'subtab': sub_alt},
                                  {'tap_subtab': lambda: None},
                                  ctx={'dept': dept0, 'subtab': sub_alt},
                                  sleep=lambda x: None,
                                  judge=F.verify_judge,
                                  tripped=lambda: F.VERIFY['tripped'])
    chk('I2 熔断后立刻收手(why=verify_break)', why == 'verify_break',
        'st=%s why=%s path=%s' % (st, why, [p[0] for p in path]))
    chk('I2b 一次失败只记一次(状态机不重复计数)',
        F.VERIFY['fail'] == 3 and len(path) == 3,
        'fail=%d path=%d' % (F.VERIFY['fail'], len(path)))

    # I3: act 说"到了"但严格复核翻盘 => 这是 act 之外的一次新判定, 必须计入
    F.verify_reset()
    scr = Screen2([s0, s_sw] * 6)

    def _act_count2(**kw):
        ok, o, s_, w = scr.act(**kw)
        F.verify_judge(ok, kw.get('what', ''), w, o)
        return ok, o, s_, w

    _dept_loose = dept0 + '专家'      # 页面实得 dept0: 宽松(互相包含)过, 严格不过
    st, o, s_, why, path = NM.run(scr.dump, obs_of, _act_count2,
                                  {'level': ('SUBTAB_ACTIVE',), 'dept': _dept_loose,
                                   'subtab': sub_alt},
                                  {'tap_subtab': lambda: scr.jump(1)},
                                  ctx={'dept': _dept_loose, 'subtab': sub_alt},
                                  sleep=lambda x: None,
                                  judge=F.verify_judge,
                                  tripped=lambda: F.VERIFY['tripped'])
    _flips = sum(1 for p in path if str(p[2]).startswith('strict:'))
    chk('I3 走错页不被 act 当成到达(严格判据在 act 内), 每条失败边计一次',
        _flips == 0 and why == 'verify_break' and F.VERIFY['fail'] == 3 and len(path) == 3,
        'flips=%d why=%s fail=%d path=%s' % (_flips, why, F.VERIFY['fail'],
                                             [(p[0], p[1]) for p in path]))
    F.verify_reset()

    # I4: 恢复动作不再有配额 —— 熔断之前, reset 可以反复用; 熔断了才停
    _q = [NM.budget_for('unknown', 'reset'), NM.budget_for('L2', 'back_l1')]
    chk('I4 恢复动作只有"预算表"没有独立配额(熔断是唯一停机依据)',
        all(x.get('attempts') for x in _q) and 'quota' not in str(_q), _q)

# ---------- G6. 剥职称(残片会让 doctor 严格核对把真到达判成不符) ----------
print('== G6. 剥职称残片 ==')
chk('G6a 任润萍副主任医师 => 任润萍', PS.strip_title('任润萍副主任医师') == '任润萍',
    repr(PS.strip_title('任润萍副主任医师')))
chk('G6b 刘芳主任医师 => 刘芳', PS.strip_title('刘芳主任医师') == '刘芳',
    repr(PS.strip_title('刘芳主任医师')))
chk('G6c 多层职称(主任医师 教授) => 只留人名', PS.strip_title('张三主任医师 教授') == '张三',
    repr(PS.strip_title('张三主任医师 教授')))
chk('G6d 不误剥: 无人名可剥时原样返回',
    PS.strip_title('医师') == '医师' and PS.strip_title('李师') == '李师',
    '%r / %r' % (PS.strip_title('医师'), PS.strip_title('李师')))
chk('G6e title_residue 抓残片/放过干净人名',
    PS.title_residue('任润萍副') and not PS.title_residue('任润萍'), '')

# ---------- J. 圈5: 浮窗只由**已验证**状态驱动 ----------
print('')
print('== J. 浮窗 = 已验证状态(校验中/已核实/校验失败/熔断) ==')

F.verify_reset()
F.set_verify_break(9)
_chip = []
_step_real = F._step


def _step_spy(text, state='run'):
    _chip.append((state, text))


F._step = _step_spy
try:
    # J1 成功: 必须先「校验中」再「已核实」, 且已核实文案带观测到的身份
    del _chip[:]
    ok, o, _s, _w = F.act(FakeAdb([s0]), {'level': 'L2', 'subtab': act0},
                          action=lambda: None, state='subtab', what='子tab:%s' % act0,
                          attempts=1, timeout=1.0, settle=0.01)
    st = [c[0] for c in _chip]
    chk('J1 成功 => 校验中 -> 已核实(带身份)',
        ok and st == ['verify', 'ok'] and act0 in _chip[-1][1],
        '%s %s' % (st, _chip[-1][1] if _chip else ''))

    # J2 失败: 只能以 fail 收尾, 全程不得出现 ok(未验证的目标不播报)
    del _chip[:]
    ok, o, _s, _w = F.act(FakeAdb([s0]), {'level': 'L2', 'subtab': '不存在的门诊'},
                          action=lambda: None, state='subtab', what='子tab:不存在的门诊',
                          attempts=1, timeout=1.0, settle=0.01)
    st = [c[0] for c in _chip]
    chk('J2 失败 => 校验中 -> 校验失败, 全程无 ok',
        (not ok) and st == ['verify', 'fail'] and 'ok' not in st,
        '%s %s' % (st, _chip[-1][1] if _chip else ''))

    # J3 走错页(abort): 同样不得出现 ok
    del _chip[:]
    ok, o, _s, _w = F.act(FakeAdb([s0]), {'level': 'six'},
                          action=lambda: None, state='doctor', what='点医生:张三',
                          attempts=2, timeout=1.0, settle=0.01,
                          abort=lambda ob, ss: 'left' if ob.level != 'six' else None)
    st = [c[0] for c in _chip]
    chk('J3 走错页中止 => 无 ok', (not ok) and 'ok' not in st and st[-1] == 'fail', st)

    # J4 熔断也要让用户看见(唯一熔断依据 -> 浮窗)
    del _chip[:]
    F.verify_reset()
    F.set_verify_break(2)
    F.verify_judge(False, 'x1', 'w')
    F.verify_judge(False, 'x2', 'w')
    brk = [c for c in _chip if '熔断' in c[1]]
    chk('J4 熔断 => 浮窗出现「熔断」且状态 fail',
        F.VERIFY['tripped'] and len(brk) == 1 and brk[0][0] == 'fail', brk)
    F.verify_reset()
    F.set_verify_break(3)

    # J5 未知英文边名不上浮窗(浮窗不是调试日志)
    chk('J5 边名文案: 已知中文/未知英文不播报',
        F._chip_label('open_dept') == '进入科室' and F._chip_label('weird_edge') == ''
        and F._chip_label('子tab:普通门诊') == '子tab:普通门诊',
        '%r / %r' % (F._chip_label('open_dept'), F._chip_label('weird_edge')))

    # J6 状态机全程: 只有观测到 selected=目标 之后才允许出现 ok
    if sub_alt:
        class _ChipAdb(object):
            def __init__(self, scr):
                self.scr = scr

            def dump(self):
                return self.scr.dump()

            def tap(self, x, y):
                pass

        del _chip[:]
        scr = Screen2([s0, s_sw] * 6)
        st, o, _s, why, path = NM.run(
            scr.dump, obs_of,
            lambda **kw: F.act(_ChipAdb(scr), **kw),
            {'level': ('SUBTAB_ACTIVE',), 'dept': dept0, 'subtab': sub_alt},
            {'tap_subtab': lambda: scr.jump(1)},
            ctx={'dept': dept0, 'subtab': sub_alt}, sleep=lambda x: None,
            judge=F.verify_judge, tripped=lambda: F.VERIFY['tripped'])
        stt = [c[0] for c in _chip]
        chk('J6 到达前无 ok, 到达时 ok 带实测身份',
            st == 'ok' and stt[-1] == 'ok' and sub_alt in _chip[-1][1]
            and stt.count('ok') == 1, '%s %s' % (stt, _chip[-1][1]))

        del _chip[:]
        F.verify_reset()
        scr = Screen2([s0] * 12)          # 点了不生效
        st, o, _s, why, path = NM.run(
            scr.dump, obs_of,
            lambda **kw: F.act(_ChipAdb(scr), **kw),
            {'level': ('SUBTAB_ACTIVE',), 'dept': dept0, 'subtab': sub_alt},
            {'tap_subtab': lambda: None},
            ctx={'dept': dept0, 'subtab': sub_alt}, sleep=lambda x: None,
            judge=F.verify_judge, tripped=lambda: F.VERIFY['tripped'])
        stt = [c[0] for c in _chip]
        chk('J6b 点不动 => 全程无 ok, 以校验失败收尾',
            st != 'ok' and 'ok' not in stt and stt and stt[-1] == 'fail',
            'st=%s %s' % (st, stt[-3:]))
        F.verify_reset()
finally:
    F._step = _step_real

# ================= K. 单一导航所有者(navlock) — ② 的地基 =================
print('')
print('== K. 单一导航所有者(并发互斥: 两个导航所有者会互相踩页面) ==')
import threading as _th
from slots import navlock as NL

_APP = 'mobile/controller-app/app/python'
for _f, _tag in [('slots/fetch_slots.py', 'run_conditions'),
                 (_APP + '/app/preflight.py', 'check_login_json'),
                 (_APP + '/app/patients.py', 'fetch_patients_json'),
                 (_APP + '/runner.py', 'pipeline')]:
    _src = io.open(_f, encoding='utf-8').read()
    chk('K0 %s 接了导航所有权门' % _tag,
        'NAVLOCK.acquire' in _src and 'NAVLOCK.release' in _src, _f)
chk('K0b run_conditions 是门, 实体在 _run_conditions_impl',
    hasattr(F, '_run_conditions_impl') and F.run_conditions is not F._run_conditions_impl)

# K1 同线程重入 = 允许(流水线内部会嵌套调用同款导航)
NL.acquire('pipeline')
NL.acquire('patients_fetch')
_h = NL.holder()
chk('K1 同线程嵌套允许(depth=2, 所有者不变)',
    _h and _h['depth'] == 2 and _h['owner'] == 'pipeline', str(_h))
NL.release('patients_fetch')
NL.release('pipeline')
chk('K1b 计数归零 => 空闲', not NL.held(), NL.describe())

# K2/K3 别的线程占用时: 拿不到所有权, 且 run_conditions 不得触碰设备
_free = _th.Event()


def _zombie_holder():
    NL.acquire('run_conditions(zombie)')
    _free.wait(15)
    NL.release('run_conditions(zombie)')


_t = _th.Thread(target=_zombie_holder)
_t.start()
_free.wait(0.25)          # 让占用线程先拿到锁
_box = {}


def _other():
    _box['acq'] = NL.acquire('preflight_login_check')
    _box['h'] = NL.holder()


_t2 = _th.Thread(target=_other)
_t2.start()
_t2.join()
chk('K2 别的线程拿不到所有权(不排队不抢占)', _box.get('acq') is False, repr(_box.get('acq')))
chk('K2b 占用者如实可见(可复核是谁在导航)',
    (_box.get('h') or {}).get('owner') == 'run_conditions(zombie)', str(_box.get('h')))

_Adb_real = F.Adb


class _BoomAdb(object):
    def __init__(self, *a, **k):
        raise AssertionError('导航被占用时不得触碰设备(并发抢屏 = 23:24 事故)')


F.Adb = _BoomAdb
_touched = False
try:
    _rc, _rs = F.run_conditions(
        [{'hospital': 'SH-084', 'tab': '眼科', 'dept': '眼科门诊', 'slot_type': 'normal'}],
        mode='probe', on_log=lambda m: None)
except AssertionError as _e:
    _rc, _rs, _touched = -1, {'error': str(_e)}, True
finally:
    F.Adb = _Adb_real
chk('K3 占用中 run_conditions 不启动(rc=5 + nav_busy)',
    _rc == 5 and _rs.get('nav_busy') is True, 'rc=%s %s' % (_rc, str(_rs)[:100]))
chk('K3b 未触碰设备(不产生第二次导航动作)', not _touched, str(_rs)[:80])
chk('K3c stop_reason 如实标 nav_busy:<谁>',
    str(_rs.get('stop_reason')).startswith('nav_busy:'), str(_rs.get('stop_reason')))
_free.set()
_t.join()
chk('K3d 占用解除后新任务可进入',
    NL.acquire('probe_next') is True and NL.release('probe_next') is True, NL.describe())

# K4 看门狗: 僵尸持有者可被强制接管(否则一次崩溃永久锁死)
NL.acquire('zombie')
chk('K4 接管前为占用态', NL.held() and (NL.holder() or {}).get('owner') == 'zombie',
    NL.describe())
NL.force_release('watchdog')
chk('K4b 强制接管后空闲', not NL.held(), NL.describe())

# K5 深检/就诊人被占用时的返回契约(不判成"未登录")
_pf = io.open(_APP + '/app/preflight.py', encoding='utf-8').read()
chk('K5 深检占用时返回 state=busy(不判未登录)', "'busy'" in _pf and 'busy' in _pf, 'preflight')
_pt = io.open(_APP + '/app/patients.py', encoding='utf-8').read()
chk('K5b 就诊人占用时 ok=False + busy', "'busy': True" in _pt, 'patients')

# K6 L1 医院名变体: logo 的 alt 名是纯数字(2026-10-16 实机 SH-084), 不是 'hos.*'
_L1_NUM = (
    '<hierarchy>'
    '<node index="0" text="" class="android.webkit.WebView" bounds="[0,241][1080,2347]" />'
    '<node index="1" text="选科室" class="android.webkit.WebView" bounds="[0,241][1080,2347]" />'
    '<node index="2" text="1511431255705" class="android.widget.Image" bounds="[45,286][365,547]" />'
    '<node index="3" text="第十人民医院" class="android.widget.TextView" bounds="[393,286][950,342]" />'
    '<node index="4" text="三级甲等" class="android.widget.TextView" bounds="[393,356][950,421]" />'
    '<node index="5" text="眼科" class="android.view.View" selected="true" bounds="[0,600][390,700]" />'
    '</hierarchy>')
_hosp = PS.hospital_of_l1(PS.raw_nodes(_L1_NUM))
chk('K6 数字 alt logo 的 L1 也能取到医院名', _hosp == '第十人民医院', repr(_hosp))
_o6 = PS.observe(_L1_NUM)
chk('K6b 该帧 level=L1 且 hospital 进身份层',
    _o6.level == 'L1' and _o6.hospital == '第十人民医院', _o6.brief()[:110])
# 对照: 语料里 'hos.*' 前缀的 logo 仍走原路径(不被新逻辑带偏)
_h_ref = None
_h_ref = PS.hospital_of_l1(PS.raw_nodes(_read(l1f))) if l1f else ''
chk('K6c 语料 L1(hos.* logo)仍取到医院名', bool(_h_ref), repr(_h_ref))

# K7 塌陷帧的**升级阶梯**(2026-10-16 00:34 实机: 页面视觉正常但 WebView 虚拟树
# bounds 全空, 连读 5 帧字节级相同 => 只重读永远不会好, 必须让页面重新渲染)。
# 期望顺序: 重读(表预算) -> 轻扫重排 nudge -> 交给转移表(reset 退层重进) -> 如实 fail。
print('')
print('== K7. 塌陷帧自愈阶梯 ==')
if os.path.exists(fx):
    _scr = Screen([sc] * 14)
    _st, _o, _s, _why, _path = NM.run(
        _scr.dump, obs_of, _scr.act, {'level': ('L2', 'L2b'), 'dept': dept0},
        {'reset': lambda: None, 'nudge': lambda: None}, sleep=lambda x: None)
    chk('K7 先 nudge(轻扫重排)再走表 reset, 不是一读不出就 fail',
        _st == 'fail' and _scr.acts[:1] == ['machine:nudge'] and 'machine:reset' in _scr.acts,
        'st=%s acts=%s why=%s' % (_st, _scr.acts, _why))
    chk('K7b reset 也只按 attempts 预算试(不无限重启)',
        _scr.acts.count('machine:reset') <= 2 and 'no_edge' in _why,
        'resets=%d why=%s' % (_scr.acts.count('machine:reset'), _why))
    # 轻扫之后页面重排成功 => 必须到达, 且到达前没有任何"猜测式"点击
    _scr2 = Screen([sc] * 6 + [s0, s0])
    _st2, _o2, _s2, _why2, _p2 = NM.run(
        _scr2.dump, obs_of, _scr2.act, {'level': ('L2', 'L2b'), 'dept': dept0},
        {'reset': lambda: None, 'nudge': lambda: None}, sleep=lambda x: None)
    chk('K7c 重排后读到有效帧 => 到达(只花了一次 nudge)',
        _st2 == 'ok' and _scr2.acts == ['machine:nudge'],
        'st=%s acts=%s' % (_st2, _scr2.acts))
    # 证据链: 连"读不出的帧"也要留档, 否则事后无从复核"当时到底卡在哪"
    _ev = []

    def _tr(obs, state, expected, observed, ok, dump, extra=None):
        _ev.append((state, ok, bool(dump)))

    _scr3 = Screen([sc] * 14)
    NM.run(_scr3.dump, obs_of, _scr3.act, {'level': ('L2', 'L2b'), 'dept': dept0},
           {'reset': lambda: None, 'nudge': lambda: None}, sleep=lambda x: None,
           trace=_tr)
    chk('K7d 塌陷帧本身写进证据链(machine:unknown_frame)',
        any(e[0] == 'machine:unknown_frame' and e[1] is False and e[2] for e in _ev),
        [e[0] for e in _ev][:6])
    _fs_src = io.open(os.path.join(ROOT, 'slots', 'fetch_slots.py'),
                      encoding='utf-8').read()
    chk('K7e 端侧 goto_unit 注册了 nudge 动作', "'nudge': _nudge" in _fs_src, 'actions')
    _nm_src = io.open(os.path.join(ROOT, 'slots', 'nav_machine.py'),
                      encoding='utf-8').read()
    chk('K7f 阶梯写在状态表里(UNKNOWN 带 nudges 预算)', "'nudges':" in _nm_src, 'STATES')
else:
    chk('K7 塌陷帧样本存在', False, fx)

print('== L. 记录层: 就诊医生必须是人名(职称残留) ==')
for _raw, _want in (('刘芳主任医师', '刘芳'), ('任润萍副主任医师', '任润萍'),
                    ('刘国强副', '刘国强'), ('汪朝阳主', '汪朝阳'),
                    ('徐建江', '徐建江'), ('张三主任医师 教授', '张三'),
                    ('医师', ''), ('', '')):
    chk('L1 clean_person_name(%r) => %r' % (_raw, _want),
        six_fields.clean_person_name(_raw) == _want, repr(six_fields.clean_person_name(_raw)))
chk('L2 观测层与记录层同一实现(page_state 不再自带职称表)',
    '_TITLE_WORDS = (' not in io.open(os.path.join(ROOT, 'slots', 'page_state.py'),
                     encoding='utf-8').read(), 'page_state 委托 six_fields')
chk('L2b PS.strip_title 与 six_fields.strip_title 同结果',
    PS.strip_title('任润萍副主任医师') == six_fields.strip_title('任润萍副主任医师') == '任润萍',
    PS.strip_title('任润萍副主任医师'))
# 端到端: 走 build_six_record 这个唯一出口, 职称不得进记录, 且原文留证
_rec = six_fields.build_six_record(s0, 'doc', {}, doctor='彭清主任医师', fee=50,
                           fee_raw='¥50', captured_at='2026-10-16T00:18:53')
chk('L3 记录出口: 彭清主任医师 => 就诊医生=彭清',
    _rec.get('就诊医生') == '彭清', repr(_rec.get('就诊医生')))
chk('L3b 原文留证(可复核页面上写的是什么)',
    _rec.get('就诊医生_原文') == '彭清主任医师', repr(_rec.get('就诊医生_原文')))
_rec2 = six_fields.build_six_record(s0, 'doc', {}, doctor='徐建江')
chk('L3c 干净人名不被破坏', _rec2.get('就诊医生') == '徐建江'
    and _rec2.get('就诊医生_原文') is None, repr(_rec2.get('就诊医生')))
# 语料: 所有医生主页, 记录层将要写入的值都不许带职称
_bad = 0
_tot = 0
for _p in files:
    try:
        _x = _read(_p)
    except Exception:
        continue
    _r = six_fields.find_page_doctor(_x)
    if not _r:
        continue
    _tot += 1
    if six_fields.title_residue(six_fields.clean_person_name(_r[0])):
        _bad += 1
chk('L4 语料 %d 份医生主页: 记录层出口 0 职称残留' % _tot, _bad == 0, 'bad=%d' % _bad)

print('== K8. 唯一熔断依据(静态守卫) ==')
_fs_src2 = io.open(os.path.join(ROOT, 'slots', 'fetch_slots.py'),
                   encoding='utf-8').read()
for _gone in ('RESET_FULL_BUDGET', 'SEARCH_RESTART_BUDGET',
              'EMPTY_DUMP_ERROR_BREAK', 'UNIT_ERROR_BREAK'):
    _hit = [l.strip() for l in _fs_src2.split(chr(10))
            if _gone in l and not l.strip().startswith('#')]
    chk('K8 %s 不再出现在代码里(只许留在注释里)' % _gone, not _hit, str(_hit[:1]))
chk('K8b 停止条件只有连续 verify 失败',
    "VERIFY['fail'] >= VERIFY_BREAK_N" in _fs_src2, 'VERIFY_BREAK_N')
chk('K8c 没有第二套连续错误计数器在判停',
    'consec_err >=' not in _fs_src2 and 'consec_empty >=' not in _fs_src2, '')
chk('K8d 恢复动作(整机 reset / 搜索重启)只被熔断器挡住',
    _fs_src2.count("if VERIFY['tripped']:") >= 3, "count=%d" % _fs_src2.count("if VERIFY['tripped']:"))

print('== M. 登录深检收尾(2026-10-16 真机复盘: 到 444 后不许 BACK) ==')
# 真机证据(logcat, 476cf991): ensure_444 kind=444 -> loginProbe state=OK -> 收尾 adb.back()
# 把随申办从 444 退到「预约挂号·个人信息同意页」(rid=btnSumit), 又因 MIUI「后台弹出界面」
# 被拒回不到本 App => 用户看到"卡在同意页一直停留"。444 才是稳定落点。
_PF_SRC = io.open(os.path.join(ROOT, 'app', 'preflight.py'), encoding='utf-8').read()
_PF_APP = io.open(os.path.join(PY, 'app', 'preflight.py'), encoding='utf-8').read()
chk('M0 两份 app/preflight.py 字节一致', _PF_SRC == _PF_APP, '')
chk('M1 收尾不再 adb.back()', 'adb.back()' not in _PF_SRC, '')
chk('M1b 回报落点字段 landing', "out['landing']" in _PF_SRC, '')

_FRAME_444 = ''
for _p in files:
    try:
        _x = _read(_p)
    except Exception:
        continue
    if F.page_kind(_x) == '444':
        _FRAME_444 = _x
        break
if not _FRAME_444:
    _FRAME_444 = '<hierarchy><node text="全部医院 (444)" bounds="[0,0][1,1]"/></hierarchy>'

_stub.is_ready = lambda: True
import json as _json                                   # noqa: E402
import app.preflight as PF                             # noqa: E402
import app.patients as PAT                             # noqa: E402
import slots.fetch_slots as _FSX                       # noqa: E402  (与 fetch_slots 是两个模块实例)
from slots import navlock as NAVLOCK                   # noqa: E402

_USED = []


class _NoBackAdb(object):
    def __init__(self, dev=None):
        self.calls = []
        _USED.append(self)

    def back(self):
        self.calls.append('back')

    def dump(self):
        self.calls.append('dump')
        return ''

    def tap(self, x, y):
        self.calls.append(('tap', x, y))


class _NavRet(object):
    st, frame = 'ok', ''

    def __init__(self, adb, base=''):
        pass

    def ensure_444(self, max_rounds=6):
        return self.st, self.frame


def _probe(st, frame):
    _NavRet.st, _NavRet.frame = st, frame
    _old = (PAT.Adb, _FSX.Nav)
    PAT.Adb = _NoBackAdb
    _FSX.Nav = _NavRet
    NAVLOCK.force_release('M 前清理')
    try:
        return _json.loads(PF.check_login_json('fake-dev'))
    finally:
        PAT.Adb, _FSX.Nav = _old


_r = _probe('ok', _FRAME_444)
chk('M2 到 444 => state=logged', _r.get('state') == 'logged', repr(_r.get('state')))
chk('M2b 到 444 后一次 BACK 都没按',
    _USED and all(c != 'back' for c in _USED[-1].calls), repr(_USED[-1].calls))
chk('M2c 落点如实回报 landing=444', _r.get('landing') == '444', repr(_r.get('landing')))
chk('M2d 导航所有权不泄漏(锁已释放)', NAVLOCK.holder() is None, repr(NAVLOCK.holder()))
_r2 = _probe('blocked', '')
chk('M3 风控页 => 如实 blocked(也不 BACK)',
    _r2.get('state') == 'blocked' and all(c != 'back' for c in _USED[-1].calls),
    repr(_r2.get('state')))
_r3 = _probe('fail', '')
chk('M3b 进不去 => fail(不硬退层)',
    _r3.get('state') == 'fail' and all(c != 'back' for c in _USED[-1].calls),
    repr(_r3.get('state')))

# ---------------- N: 门禁「未绿」自查与自锁解除（Java 侧静态约束） ----------------
# 现场问题：门禁一直不绿，日志只有 1.5s 重扫，查不出卡在哪项；
# 且「后台弹出被拒」证据一旦记下就永久生效 → 自动深检永远跳过 → LOGIN 永久 UNKNOWN。
_JAVA_DIR = os.path.join('mobile', 'controller-app', 'app', 'src', 'main', 'java',
                         'com', 'hospitalhelper', 'controller')
MA_SRC = io.open(os.path.join(_JAVA_DIR, 'MainActivity.java'),
                 encoding='utf-8', errors='replace').read()
PF_SRC = io.open(os.path.join(_JAVA_DIR, 'Preflight.java'),
                 encoding='utf-8', errors='replace').read()

# maybeAutoProbeLogin 的函数体（到下一个方法注释为止）
_m0 = MA_SRC.index('private void maybeAutoProbeLogin()')
_m1 = MA_SRC.index('\n    /**', _m0)
PROBE_BODY = MA_SRC[_m0:_m1]

chk('N0 门禁未绿时把卡住项写进日志(不再只有重扫)',
    '门禁未绿, 卡住项' in MA_SRC, '')
chk('N1 深检跳过必须说清原因(每项状态)',
    'maybeAutoProbeLogin 跳过' in PROBE_BODY, PROBE_BODY.count('跳过'))
chk('N2 跳过分支不得再调 renderPreflight(互调=死循环)',
    'renderPreflight(' not in PROBE_BODY, '')
chk('N3 卡住项日志限流(同组 10s 内只记一次)',
    'lastStuckLog' in MA_SRC and 'nowMs - lastStuckLogAt > 10000L' in MA_SRC, '')
chk('N4 被拒证据有有效期(否则权限开了也永不重试)',
    'BG_BLOCK_TTL_MS' in PF_SRC and 'bgLaunchBlockedFresh' in PF_SRC, '')
chk('N5 有效期 = 10 分钟(覆盖一次 adb 装完就重测的节奏)',
    'BG_BLOCK_TTL_MS = 10 * 60 * 1000L' in PF_SRC, '')
chk('N6 scan 按「新鲜」判定 BACK_LAUNCH(过期转未验证并提示会重试)',
    'boolean fresh = bgLaunchBlockedFresh(ctx);' in PF_SRC
    and 'if (fresh) {' in PF_SRC and '会再自动试一次' in PF_SRC, '')
chk('N7 登录行文案在渲染时补充(annotateLoginHint)',
    'annotateLoginHint(items);' in MA_SRC and 'private void annotateLoginHint' in MA_SRC, '')
chk('N8 annotateLoginHint 只改 detail, 不跳转不重渲染',
    'renderPreflight(' not in MA_SRC[MA_SRC.index('private void annotateLoginHint'):
                                   MA_SRC.index('private void annotateLoginHint') + 900]
    and 'startActivity' not in MA_SRC[MA_SRC.index('private void annotateLoginHint'):
                                      MA_SRC.index('private void annotateLoginHint') + 900], '')
chk('N9 自动深检仍受 once-per-process 防乒乓保护',
    'autoLoginProbeUsed' in PROBE_BODY, '')
# N10: 必需项其实已全 OK 时，不许打「跳过: 前置未就绪()」这种空原因日志
chk('N10 空原因不打「前置未就绪()」(会误导排查)',
    'if (!w.isEmpty())' in PROBE_BODY, PROBE_BODY.count('前置未就绪'))

# ---------------- O: 2026-10-16 真机「进了无关医院/科室」+「浮窗与屏幕对不上」 ----------------
# 现场证据: trace 20261016T103255 #013 普陀区人民医院 ok=False(level=unknown, title=预约挂号);
# fetch.log 反复 "tap home entry by legacy (473,786)" -> "搜索页输入框未出现";
# 且 [navlock] 拒绝进入 run_conditions: 随申办导航正被 patients_fetch 占用 3257s。
import hashlib as _hl
import time as _time

FS_SRC = io.open(os.path.join('slots', 'fetch_slots.py'), encoding='utf-8').read()
NL_SRC = io.open(os.path.join('slots', 'navlock.py'), encoding='utf-8').read()
NM_SRC = io.open(os.path.join('slots', 'nav_machine.py'), encoding='utf-8').read()
PS_SRC = io.open(os.path.join('slots', 'page_state.py'), encoding='utf-8').read()
PAT_SRC = io.open(os.path.join('app', 'patients.py'), encoding='utf-8').read()
CHIP_SRC = io.open(os.path.join(_JAVA_DIR, 'MiniChip.java'),
                   encoding='utf-8', errors='replace').read()


def _ns(**kw):
    return types.SimpleNamespace(**kw)


chk('O1 严格名核对禁止反向外溢(页面名只是目标名的一部分 => 不算到达)',
    PS._name_ok_strict('炎症性肠病专病门诊甲', '炎症性肠病专病门诊') is False
    and PS._name_ok_strict('炎症性肠病专病门诊', '炎症性肠病专病门诊(甲)') is True
    and PS._name_ok('炎症性肠病专病门诊甲', '炎症性肠病专病门诊') is True, '')

_ob = dict(level='L2', dept='炎症性肠病专病门诊', hospital='', subtab_active='', tab='', doctor='')
chk('O2 matches(strict): 层级对但科室错 => 判未到达(宽松版仍判到达)',
    PS.matches({'level': 'L2', 'dept': '炎症性肠病专病门诊甲'}, _ns(**_ob), strict=True)[0] is False
    and PS.matches({'level': 'L2', 'dept': '炎症性肠病专病门诊甲'}, _ns(**_ob))[0] is True, '')

chk('O3 状态机到达/落地都用 strict(nav_machine 三处)',
    NM_SRC.count('strict=True') >= 3, NM_SRC.count('strict=True'))

chk('O4 首页没有可确认锚点时不得盲点旧坐标 (473,786)',
    "'no-anchor'" in FS_SRC and 'return (473, 786' not in FS_SRC, '')

chk('O5 第二条合法通道: 首页「预约挂号」入口(按入口尺寸过滤)',
    'def _home_444_entry(' in FS_SRC and '100 <= w <= 400 and 100 <= h <= 300' in FS_SRC, '')

_HOME_XML = ('<h><node text="预约挂号" class="android.widget.TextView" bounds="[45,626][1035,890]"/>'
             '<node text="预约挂号" class="android.widget.TextView" bounds="[45,1058][250,1278]"/></h>')
_e = F.Nav._home_444_entry(None, _HOME_XML)
chk('O6 入口只认卡片尺寸: 结果页整行医院卡片(宽990)不会被当入口点',
    _e is not None and _e[0] == 147 and _e[1] == 1168, repr(_e))

chk('O7 点科室前必须证明"这是本院的选科室页"(身份闸门)',
    'def verify_L1(' in FS_SRC and "open_dept 拒点" in FS_SRC and "'wrong_hosp'" in FS_SRC, '')

chk('O8 L2 落地必须核对页面科室名(不再只看 level)',
    'def _dept_landed_ok(' in FS_SRC and 'return False, why_d' in FS_SRC, '')
_d1 = F._dept_landed_ok('炎症性肠病专病门诊甲', '炎症性肠病专病门诊')
chk('O9 科室截断外溢不算到达(浮窗不再替屏幕做断言)', _d1[0] is False, repr(_d1))
_d2 = F._dept_landed_ok('口腔科', '口腔科')
_d3 = F._dept_landed_ok('口腔科', '')
chk('O10 页面未给科室名: 不判死但留痕(≈无法核实)',
    _d2[0] is True and _d3[0] is True and '无法核实' in _d3[1], repr((_d2, _d3)))
chk('O11 浮窗播报带医院名(用户能核对是哪家医院)',
    "已进入门诊挂号: %s · %s" in FS_SRC, '')

chk('O12 导航锁: 超过 STALE_S(20min) 的持有者判僵尸并接管',
    'STALE_S = 1200.0' in NL_SRC and '判为僵尸' in NL_SRC, '')
_NL_OLD = (NL._owner, NL._depth, NL._t0)
try:
    NL.force_release('O13-setup')
    _a = NL.acquire('zombie-holder')
    NL._t0 = _time.time() - 5000          # 假装已持有 5000s(实测 3257s)
    _same = NL.acquire('same-thread-reopen')
    _h1 = NL.holder()                     # 同线程陈旧 => 重新起算(depth 回到 1)
    NL._t0 = _time.time() - 5000          # 再让它陈旧一次: 检查跨线程接管
    _res = {}

    def _other():                         # 另一个线程(自检线程)面对僵尸持有者
        _res['ok'] = NL.acquire('run_conditions')
        _res['holder'] = NL.holder()

    _t = _th.Thread(target=_other)
    _t.start()
    _t.join()
    chk('O13 陈旧锁可自愈: 同线程重算 + 跨线程接管(不再 rc=5 空转)',
        bool(_a) and bool(_same) and _h1 is not None and _h1['depth'] == 1
        and _res.get('ok') and (_res.get('holder') or {}).get('owner') == 'run_conditions',
        repr((_h1, _res)))
finally:
    NL._owner, NL._depth, NL._t0 = _NL_OLD
    NL.force_release('O13-cleanup')

_p0 = PAT_SRC.index('def fetch_patients_json')
_p1 = PAT_SRC.index('def load_patients_json')
chk('O14 patients_fetch 锁在 finally 里成对释放(僵尸锁根因)',
    'finally:' in PAT_SRC[_p0:_p1] and "NAVLOCK.release('patients_fetch')" in PAT_SRC[_p0:_p1], '')
chk('O15 load_patients_json 不再释放别人的导航锁(未 acquire 却 release)',
    'NAVLOCK.release' not in PAT_SRC[_p1:_p1 + 900], '')

chk('O16 浮窗落点按 dp 换算(位置与屏幕像素一致)',
    'p.y = dp(chipCtx(), topYdp)' in CHIP_SRC
    and 'DEFAULT_Y_DP = 56' in CHIP_SRC
    and 'p.x = dp(chipCtx(), 10)' in CHIP_SRC, '')
chk('O17 避让只换左右不换上下(不再盖住页面正文)',
    'rightSide = !rightSide' in CHIP_SRC and 'topCorner' not in CHIP_SRC, '')
chk('O18 避让动作留痕(下次能查出是哪次点击把浮窗推走)',
    'MiniChip 避让' in CHIP_SRC, '')

_MIRROR = [('app/patients.py', os.path.join('mobile', 'controller-app', 'app', 'python', 'app', 'patients.py')),
           ('slots/fetch_slots.py', os.path.join('mobile', 'controller-app', 'app', 'python', 'slots', 'fetch_slots.py')),
           ('slots/navlock.py', os.path.join('mobile', 'controller-app', 'app', 'python', 'slots', 'navlock.py')),
           ('slots/page_state.py', os.path.join('mobile', 'controller-app', 'app', 'python', 'slots', 'page_state.py')),
           ('slots/nav_machine.py', os.path.join('mobile', 'controller-app', 'app', 'python', 'slots', 'nav_machine.py'))]
_diff = [a for a, b in _MIRROR
         if _hl.md5(io.open(a, encoding='utf-8').read().encode('utf-8')).hexdigest()
         != _hl.md5(io.open(b, encoding='utf-8').read().encode('utf-8')).hexdigest()]
chk('O19 端侧镜像与权威源一致(改一处必须同步)', not _diff, _diff)

# O22/O23: 避让是临时的 —— 冷却后回到右上 y=56dp；落点统一走 topYdp。
# 真机 2026-10-16 12:37 截图：dodge 把浮窗推到左上 y=158px，正好盖住随申办顶栏「关闭」键。
_JDIR = os.path.join(ROOT, 'mobile', 'controller-app', 'app', 'src', 'main',
                     'java', 'com', 'hospitalhelper', 'controller')
_CHIP2 = io.open(os.path.join(_JDIR, 'MiniChip.java'), encoding='utf-8').read()
chk('O22 避让会复位(冷却后回右上，不长期压住页面顶栏)',
    'RESTORE_COOLDOWN_MS' in _CHIP2 and 'MiniChip 复位' in _CHIP2
    and 'lastDodgeAt = SystemClock.elapsedRealtime()' in _CHIP2)
chk('O23 落点走 topYdp(默认 56dp/避让 96dp，避让时让开顶栏)',
    'p.y = dp(chipCtx(), topYdp)' in _CHIP2 and 'DODGE_Y_DP = 96' in _CHIP2)

# O20/O21: Activity 不在（进程被 ROM 回收后由广播重新拉起）时，浮窗与日志不能哑火。
# 真机 2026-10-16 12:10 自检：PyBridge.listener==null，整场 logcat 里 HH_APP 一条没有，
# 浮窗停在旧文案上、随申办还在自己变 —— 这是"浮窗与屏幕对不上"的另一种成因。
_JDIR = os.path.join(ROOT, 'mobile', 'controller-app', 'app', 'src', 'main',
                     'java', 'com', 'hospitalhelper', 'controller')
_pyt = io.open(os.path.join(_JDIR, 'PyBridge.java'), encoding='utf-8').read()
_stt = io.open(os.path.join(_JDIR, 'SelfTest.java'), encoding='utf-8').read()
chk('O20 PyBridge: listener==null 时浮窗+logcat 兜底',
    ('private static boolean miniChipOn()' in _pyt
     and _pyt.count('MiniChip.show') >= 2
     and 'Log.i("HH_APP", "[step:' in _pyt))
chk('O21 SelfTest: 无 Activity 监听时结论仍落浮窗(不会停在旧文案)',
    ('private static volatile Context sApp;' in _stt
     and 'MiniChip.showFinal(a, (ok ? ' in _stt))

print('')
print('结果: %s' % ('PASS' if not fails else 'FAIL ' + ', '.join(fails)))
sys.exit(0 if not fails else 1)
