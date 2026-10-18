# -*- coding: utf-8 -*-
"""nav_machine —— 导航状态机(2026-10-15 第③步): 把「现在在哪个页」与「下一步做什么」
收敛成一张转移表, 每一步都必须被观测层(page_state)证实后才允许进入下一步。

为什么要有这个模块(取代旧写法):
  旧 goto_unit() 是一串 if 链, 判断依据是 **pos**(上一次记下的位置)而不是 **当前屏幕**;
  屏幕与 pos 一旦不一致(塌陷帧、半渲染、被弹窗顶掉、随申办自己跳页), 就会「以为在 L1」
  而在真实是 L2/unknown 的页面上乱点 —— 这正是「浮窗超前」「普通门诊没进去」的根因。
  本模块把决策改成: 观测 -> 查表选边 -> 执行 -> act() 轮询到「观测==期望」才算这条边走通;
  走不通就重新观测、重新查表(re-plan), 而不是继续按旧 pos 硬走。

设计约束(务必遵守, 否则又变成补丁):
  1) 本模块 **不做任何点击**: 所有动作由调用方(fetch_slots)以 actions 字典注入,
     这样 Nav 里那些经过实战的原语(reset_full/enter/open_dept/back)可以原样复用;
  2) 本模块 **不写日志/不碰浮窗**: 通过 announce / trace 回调注入, 保证浮窗只在
     「已验证到达」之后发声(修 MiniChip 超前);
  3) 表是唯一事实来源: 新增页面/新走法只改 EDGES, 不在调用处加 if。
"""

import time

# 与 fetch_slots 用同一条导入路径, 保证两边拿到的是**同一个** page_state 实例
# (两条路径各加载一份会让 isinstance/判据在跨模块传对象时静默失效)。
try:
    from slots import page_state as PS
except Exception:
    try:
        from . import page_state as PS
    except Exception:
        import page_state as PS

# 页面层级归一: L2b(子tab 列表) 在图里等同 L2; native_search 归到 home 的出边
NORM = {'L2b': 'L2', 'native_search': 'home'}

# 边: (名字, 起点层级集合('*'=任意), 落点层级, 代价, 期望(可含 ctx 占位))
# 代价只影响「多条可行边时优先哪条」: 越便宜越优先, 兜底(reset+enter)最贵。
EDGES = (
    ('stay',      ('L2',),                 'L2',  0.0, {'level': ('L2', 'L2b')}),
    ('back_l2',   ('doc', 'six'),          'L2',  1.0, {'level': ('L2', 'L2b')}),
    ('back_l1',   ('L2', 'doc', 'six'),    'L1',  2.0, {'level': ('L1',)}),
    ('open_dept', ('L1',),                 'L2',  2.0, {'level': ('L2', 'L2b')}),
    ('enter_l1',  ('444', 'search'),       'L1',  3.0, {'level': ('L1',)}),
    ('search',    ('home', 'other'),       '444', 4.0, {'level': ('444', 'search')}),
    # 授权同意页: 点「同意」。落点用宽期望(通常回搜索页/医院页/L2); 若点了仍是 auth,
    # run() 的尝试配额会把这条边禁用, 不会无限点。
    ('agree_auth', ('auth',),                '444', 1.5,
     {'level': ('444', 'search', 'L1', 'L2', 'L2b', 'doc', 'six', 'home')}),
    # 子tab: L2 上点「普通门诊」这类 => 落到 SUBTAB_ACTIVE(= L2 且 selected 命中目标子tab)。
    # 落点期望带 subtab, 所以这条边**只有 selected="true" 真的切过去**才算走通
    # (旧代码把「上一页空态」当切成功, 正是「到了普通门诊却没进去」的根因)。
    ('tap_subtab', ('L2',),                 'SUBTAB_ACTIVE', 1.5,
     {'level': ('L2', 'L2b')}),
    # 医生卡: L2(或已切好子tab)上点医生 => 落到 SIX(六字段医生主页)。
    # 落点期望带 doctor(由 ctx 填), 所以「进的是科室主页而不是某个医生主页」会被拦下。
    ('tap_doctor', ('L2', 'SUBTAB_ACTIVE'), 'six', 1.5, {'level': ('six', 'doc')}),
    ('reset',     ('*',),                  'L1',  9.0, {'level': ('L1',)}),
)

# 这些层级说明「页面被外部状态挡住」, 不是导航能解决的, 直接上报让上层熔断
# 'auth'(授权同意页)**不在**停止条件里: 点「同意」就能过去(见 agree_auth 边);
# 只有需要凭据的 'login' 和风控 'blocked' 才是真停。
STOP_LEVELS = {'blocked': 'blocked', 'login': 'login'}
# 例外(2026-10-15 端侧实测): 登录页有时是会话超时造成的, 整体重置一次常能恢复;
# 风控/验证码(blocked/auth)重置无益且容易陷更深 => 只允许 login 试一次 reset。
STOP_RETRY = {'login': True}

# 「层级对但身份不对」时该往哪一层退一层重进(层级越浅越容易换科室)
PARENT = {'L2': 'L1', 'L1': '444', '444': 'home', 'six': 'L2', 'doc': 'L2',
          'SUBTAB_ACTIVE': 'L2'}

MAX_HOPS = 6          # 一次 goto 最多走几条边(正常 1~3 条)
MAX_REREAD = 4        # 塌陷/半渲染帧(unknown)允许重读几次, 不计入 hop 预算
REREAD_S = 1.2        # 重读间隔(给 SPA 一点重建时间)
# 「读得出」的帧: nudge(轻扫重排)之后必须落到这些 level 之一, 否则算没恢复
READABLE = ('home', '444', 'search', 'L1', 'L2', 'L2b', 'six', 'doc',
            'auth', 'login', 'blocked')


def norm(level):
    return NORM.get(level, level)


# ---------------- 状态声明(唯一一份; 转移本身仍只在 EDGES 里写一次) ----------------
# (状态, 观测 level 集, 期望 identity, 预算, 失败出口)
#   期望 identity: 'CTX' = 由本次目标(ctx)填充; 进入谓词 = level 命中 + 期望 identity 全中。
#   预算: reread=可重读次数(不计 hop), attempts=同一条边最多重试几次(超即禁用该边),
#         timeout=单条边 act() 的等待上限(秒)。
#   失败出口: 走不通时**由表决定**怎么退, 不再在各调用处各写一套 retry/quota。
STATES = (
    ('IDLE',          ('no_window',),              {'level': ('no_window',)},
     {'reread': 0, 'attempts': 1, 'timeout': 6.0},  'self_heal'),
    ('UNKNOWN',       ('unknown',),                {'level': ('unknown',)},
     {'reread': MAX_REREAD, 'nudges': 1, 'attempts': 1, 'timeout': 6.0},
     'reread_nudge_then_report'),
    ('HOME',          ('home', 'native_search'),   {'level': ('home',)},
     {'reread': 2, 'attempts': 2, 'timeout': 12.0}, 'reset'),
    ('SEARCH_444',    ('444', 'search'),           {'level': ('444', 'search')},
     {'reread': 2, 'attempts': 2, 'timeout': 12.0}, 'reset'),
    ('L1',            ('L1',),                     {'level': ('L1',), 'hospital': 'CTX'},
     {'reread': 2, 'attempts': 2, 'timeout': 12.0}, 'back_l1_or_reset'),
    ('L2',            ('L2', 'L2b'),               {'level': ('L2', 'L2b'), 'dept': 'CTX'},
     {'reread': 2, 'attempts': 2, 'timeout': 12.0}, 'back_l1_or_reset'),
    # SUBTAB_ACTIVE 不是 page_state 的 level, 而是 **L2 + 恰好一个 selected 命中目标子tab**
    # 的派生态(进入谓词见 entry_ok)。它的点击预算单独在 EDGE_BUDGET 里。
    ('SUBTAB_ACTIVE', ('SUBTAB_ACTIVE',),
     {'level': ('L2', 'L2b'), 'dept': 'CTX', 'subtab': 'CTX'},
     {'reread': 2, 'attempts': 2, 'timeout': 12.0}, 'back_l1_or_reset'),
    ('SIX',           ('six', 'doc'),
     {'level': ('six', 'doc'), 'dept': 'CTX', 'doctor': 'CTX'},
     {'reread': 2, 'attempts': 2, 'timeout': 12.0}, 'back_l2'),
    ('AUTH',          ('auth',),                   {'level': ('auth',)},
     {'reread': 1, 'attempts': 2, 'timeout': 9.0},  'agree_auth_then_report'),
    ('LOGIN',         ('login',),                  {'level': ('login',)},
     {'reread': 1, 'attempts': 1, 'timeout': 9.0},  'reset_once_then_report'),
    ('BLOCKED',       ('blocked',),                {'level': ('blocked',)},
     {'reread': 0, 'attempts': 0, 'timeout': 6.0},  'report'),
)
STATE_BY_NAME = {s[0]: s for s in STATES}
LEVEL2STATE = {}
for _s in STATES:
    for _lv in _s[1]:
        LEVEL2STATE[_lv] = _s[0]

# 单条边的预算覆盖(端侧实测: 子tab 点击偶发不生效, 允许 3 次**每次重新观测**的尝试;
# 其它边沿用状态预算, 不额外放宽)。
EDGE_BUDGET = {'tap_subtab': {'attempts': 3, 'timeout': 9.0}}


def state_of(level_or_obs):
    '''观测 -> 状态名。L2 且已观测到 selected 子tab => SUBTAB_ACTIVE(派生态)。'''
    lv = getattr(level_or_obs, 'level', level_or_obs)
    if getattr(level_or_obs, 'level', None) in ('L2', 'L2b') and \
            getattr(level_or_obs, 'subtab_active', None):
        return 'SUBTAB_ACTIVE'
    return LEVEL2STATE.get(lv, LEVEL2STATE.get(norm(lv), 'UNKNOWN'))


def budget_for(state, edge=None):
    s = STATE_BY_NAME.get(state)
    b = dict(s[3]) if s else {'reread': MAX_REREAD, 'attempts': 2, 'timeout': 12.0}
    b.update(EDGE_BUDGET.get(edge, {}))
    return b


def exit_for(state):
    s = STATE_BY_NAME.get(state)
    return s[4] if s else 'report'


def allowed_actions(state):
    '''允许动作**从 EDGES 派生**, 不在状态表里再写一遍(否则就成了两张表)。'''
    lv = STATE_BY_NAME.get(state, (None, (None,)))[1][0]
    out = []
    for e in EDGES:
        if e[0] == 'stay':
            continue
        if _frm_hit(e, lv) or (state == 'SUBTAB_ACTIVE' and _frm_hit(e, 'L2')):
            out.append(e[0])
    return tuple(out)


def entry_ok(state, obs, ctx=None):
    '''进入谓词: 这一帧能不能算作该状态(身份字段按该状态声明裁剪后比对)。'''
    s = STATE_BY_NAME.get(state)
    if s is None:
        return False, ['unknown_state']
    exp = {}
    for k, v in s[2].items():
        if v == 'CTX':
            v = (ctx or {}).get(k)
            if not v:
                continue
        exp[k] = v
    if state == 'SUBTAB_ACTIVE':
        if getattr(obs, 'level', None) not in ('L2', 'L2b'):
            return False, ['level=%s' % getattr(obs, 'level', None)]
    elif getattr(obs, 'level', None) not in s[1]:
        return False, ['level=%s' % getattr(obs, 'level', None)]
    return PS.matches(prune_for_level(exp, state), obs)


def chain_edges():
    '''链 IDLE->HOME->444->L1->L2->SUBTAB_ACTIVE->SIX 相邻两态在 EDGES 里是否可达。'''
    order = ('IDLE', 'HOME', 'SEARCH_444', 'L1', 'L2', 'SUBTAB_ACTIVE', 'SIX')
    out = []
    for a, b in zip(order, order[1:]):
        la = STATE_BY_NAME[a][1][0]
        lb = STATE_BY_NAME[b][1][0]
        out.append((a, b, next_edge(la, lb) is not None))
    return out


def _frm_hit(e, lvl):
    frm = e[1]
    return ('*' in frm) or (lvl in frm)


def next_edge(cur, tgt, table=EDGES, banned=None):
    """查表: 从 cur 这一层, 朝 tgt 走, 该选哪条边。返回边元组或 None。

    先找「直达 tgt」的最便宜边; 没有则 BFS 找最便宜的两跳路径的第一条边
    (例: unknown/home -> 444 -> L1 -> L2)。
    """
    cur, tgt = norm(cur), norm(tgt)
    banned = tuple(banned or ())
    direct = [e for e in table
              if _frm_hit(e, cur) and norm(e[2]) == tgt and e[0] not in banned]
    if direct:
        return min(direct, key=lambda e: e[3])
    # BFS: level -> level
    prev = {cur: None}
    cost = {cur: 0.0}
    q = [cur]
    while q:
        n = q.pop(0)
        for e in table:
            if e[0] in banned or not _frm_hit(e, n):
                continue
            m = norm(e[2])
            c = cost[n] + e[3]
            if m not in cost or c < cost[m]:
                cost[m] = c
                prev[m] = (n, e)
                q.append(m)
        if tgt in prev:
            break
    if tgt not in prev:
        return None
    # 回溯到 cur 之后的第一条边
    node = tgt
    while prev.get(node) is not None and prev.get(prev[node][0]) is not None:
        node = prev[node][0]
    # cur==tgt 且没有真正的入边(例如 stay 被禁) => 无解, 交给上层如实报 no_edge
    if prev.get(node) is None:
        return None
    return prev[node][1]


def prune_for_level(target, level):
    """身份字段按「这一页能不能承载」裁剪 —— 不裁就会永远判不符。

    实测(1322 份语料 + 端侧 trace): L2 的标题只带科室名, hospital 恒为空;
    L1 的标题是「选科室」, 只有 hospital_of_l1 能给出医院名, 没有科室/子tab。
    所以: L2 不校 hospital, L1 不校 dept/subtab, 六字段页两者都校。
    """
    t = dict(target)
    lv = level if level == 'SUBTAB_ACTIVE' else norm(level)
    # 目标自己声明了 SUBTAB_ACTIVE => subtab 必须校, 不能因为「当前这帧是 L2」就裁掉
    _want = t.get('level')
    _want = tuple(_want) if isinstance(_want, (list, tuple, set)) else (_want,)
    _keep_subtab = 'SUBTAB_ACTIVE' in _want
    if lv == 'SUBTAB_ACTIVE':
        t.pop('hospital', None)
        t.pop('tab', None)
        t.pop('doctor', None)
        # 保留 SUBTAB_ACTIVE 这个**标记**: 否则期望被裁一次之后就再也带不回 subtab 要求
        # (实测: 子tab 没切过去, act 却报 ok —— 因为 level=L2 的裁剪把 subtab 丢了)。
        t['level'] = ('SUBTAB_ACTIVE', 'L2', 'L2b')
    elif lv == 'L2':
        t.pop('hospital', None)      # L2 标题不带医院名
        t.pop('tab', None)           # 科室大类只在 L1 左列可见
        t.pop('doctor', None)        # L2 不是人名页
        if _keep_subtab:
            t['level'] = ('SUBTAB_ACTIVE', 'L2', 'L2b')  # 这帧按 L2 校 level, 但 subtab 必须校
        else:
            t.pop('subtab', None)
    elif lv == 'L1':
        t.pop('dept', None)
        t.pop('subtab', None)
        t.pop('doctor', None)        # L1 上没有医生身份可言
    elif lv not in ('six', 'doc'):
        # 444/search/home/auth/unknown 承载不了身份名, 只能校 level; 不裁就会
        # 「永远判不符」=> 边永远走不通, 最后 max_hops 假失败。
        for _k in ('dept', 'subtab', 'hospital', 'tab', 'doctor'):
            t.pop(_k, None)
    else:
        t.pop('tab', None)           # 医生主页没有科室大类选中态
    return t


def expect_for(edge, ctx):
    """把边上的静态期望 + 本次目标身份(ctx)合成 act() 用的期望(同样按落点层级裁剪)。"""
    exp = dict(edge[4] or {})
    tgt = dict(ctx or {})
    exp.update(prune_for_level(tgt, edge[2]))
    return exp


def run(dump, observe, act, target, actions, ctx=None, trace=None,
        announce=None, max_hops=MAX_HOPS, log=None, sleep=time.sleep,
        judge=None, tripped=None):
    """把「当前屏幕」搬到 target 描述的状态; 每条边都用 act() 验证。

    dump()     -> 当前 UI dump 文本
    observe(s) -> page_state.PageIdentity
    act(expect=..., action=..., state=..., what=...) -> (ok, obs, s, why)  (fetch_slots.act)
    target     -> {'level': ('L2','L2b'), 'dept': ..., 'hospital': ...}
    actions    -> {边名: callable()}  实际点击/输入动作; 'stay' 可不注册(空动作)
    ctx        -> 额外身份约束, 合并进每条边的期望
    announce   -> announce(边名, obs) 仅在 **验证到达后** 调用(浮窗/进度条)
    judge(ok, what, why, obs) -> bool  **唯一熔断记账点**, 只记"act 之外"的判定
             (严格复核翻盘 / 重读耗尽 / no_edge / no_action / 到达), 返回 True=已熔断。
             act() 自己已经记过它那次判定, 这里**不重复计数**, 否则一次失败被算两次。
    tripped() -> bool  只查询是否已熔断(不记账); True => 立刻以 fail 收手。
    返回 (status, obs, dump_text, why, path)  status: ok|fail|blocked|login
    """
    ctx = dict(ctx or {})
    tgt_level = (target.get('level') or ('L2',))
    tgt = norm(tgt_level[0] if isinstance(tgt_level, (tuple, list)) else tgt_level)
    path = []
    reread, nudge_left = 0, None
    s = dump()
    obs = observe(s)
    hop, reset_tried = 0, False
    tried, banned = {}, set()   # 同一条边最多试 2 次, 防表内死循环
    while hop < max_hops:
        if obs.level in STOP_LEVELS:
            lvl = obs.level
            if STOP_RETRY.get(lvl) and not reset_tried:
                # 会话超时型登录页: 给一次整体重置的机会(只一次, 且必须是 reset 边)
                reset_tried = True
                e = next_edge(lvl, tgt)
                fn = actions.get(e[0]) if (e and e[0] == 'reset') else None
                if fn is not None:
                    if log:
                        log('  MACHINE: 观测=%s => 允许一次整体重置(会话可能恢复)' % lvl)
                    hop += 1
                    exp = expect_for(e, ctx)
                    aok, obs2, s2, why = act(expect=exp, action=fn,
                                             state='machine:reset_from_' + lvl,
                                             what='reset_from_' + lvl)
                    path.append(('reset', aok, why))
                    # act() 已记过这次判定; 这里只查熔断状态, 不重复计数
                    if tripped is not None and tripped():
                        return 'fail', obs2, s2, 'verify_break', path
                    if trace:
                        trace(obs2, 'machine:reset_from_%s' % lvl, exp, obs2, aok, s2,
                              {'why': why})
                    obs, s = obs2, s2
                    continue
            return STOP_LEVELS[lvl], obs, s, 'stop_level=%s' % lvl, path
        if obs.level in ('unknown', ''):
            # 塌陷帧/半渲染: 不能据此决策, 也不能当「不在 L2」处理 —— 先重读。
            # 但**重读不是唯一的恢复手段**(2026-10-16 00:34 实机): 页面视觉正常,
            # 整棵 WebView 虚拟树的 bounds 却全空, 连读 5 帧**字节级相同** => 再读
            # 多少次也不会变好。升级顺序由表决定: 重读 -> 轻扫重排(nudge) -> 交给转移表。
            reread += 1
            _bud = budget_for(state_of(obs))
            if reread <= _bud.get('reread', MAX_REREAD):
                if log:
                    log('  MACHINE: 观测=unknown(塌陷/半渲染) => 重读 %d/%d'
                        % (reread, _bud.get('reread', MAX_REREAD)))
                sleep(REREAD_S)
                s = dump()
                obs = observe(s)
                continue
            if trace:      # 证据链: 连"读不出的帧"也要留档, 否则事后无从复核
                trace(obs, 'machine:unknown_frame', {'level': READABLE}, obs,
                      False, s, {'why': 'reread_x%d' % reread})
            if nudge_left is None:
                nudge_left = _bud.get('nudges', 0)
            if nudge_left > 0 and 'nudge' in actions:
                nudge_left -= 1
                hop += 1
                if log:
                    log('  MACHINE: 重读 %d 次仍是塌陷帧 => 轻扫重排(还剩 %d 次)'
                        % (reread, nudge_left))
                _aok, obs, s, _why = act(expect={'level': READABLE},
                                         action=actions['nudge'],
                                         state='machine:nudge', what='nudge',
                                         timeout=_bud.get('timeout'))
                reread = 0        # 重排之后重新给一次重读预算
                if tripped is not None and tripped():
                    return 'fail', obs, s, 'verify_break', path
                continue
            # 重读 + 重排都用尽 => 不在这里自己写 retry, 交给转移表(unknown -reset-> L1
            # 再重新进入 = 让页面重新渲染); 表里没有出路时下面 no_edge 分支会如实 fail。
            if log:
                log('  MACHINE: 塌陷帧自愈已用尽 => 交给转移表退层重进')
        ok, diffs = PS.matches(prune_for_level(target, obs.level), obs, strict=True)
        if ok:
            if announce:
                announce('arrived', obs)
            if judge is not None:
                judge(True, 'arrived', '', obs)
            if trace:
                trace(obs, 'machine:到达', target, obs, True, s)
            return 'ok', obs, s, '', path
        _b = set(banned)
        _tgt = tgt
        if norm(obs.level) == tgt:
            # 层级已经对、身份不对 => 留在这页没用(上面已证), 也 stay 不出结果:
            # **退一层重进**(L2->L1 再 open_dept), 这是表里的一条常规路线。
            _b.add('stay')
            _tgt = PARENT.get(tgt, tgt)
        e = next_edge(obs.level, _tgt, banned=_b)
        if e is None and _tgt != tgt:
            e = next_edge(obs.level, tgt, banned=_b)
        if e is None:
            if judge is not None:
                judge(False, 'no_edge(%s->%s)' % (obs.level, tgt), 'no_edge', obs)
            return 'fail', obs, s, 'no_edge(%s->%s)' % (obs.level, tgt), path
        name = e[0]
        fn = actions.get(name)
        if fn is None and name != 'stay':
            if judge is not None:
                judge(False, 'no_action(%s)' % name, 'no_action', obs)
            return 'fail', obs, s, 'no_action(%s)' % name, path
        exp = expect_for(e, ctx)
        hop += 1          # 只有"真的走一条边"才消耗 hop; 重读(unknown)不消耗
        _bud = budget_for(state_of(obs), name)

        # 严格判据放进 act: 轮询到**真的**满足为止, 而不是"宽松匹配先过了"就停手。
        # (之前 act 宽松过 + 状态机事后严格复核 => 一次走错页要绕一圈才发现, 而且
        #  act 记的 ok 会把上一次失败清零 —— 熔断计数因此失真。)
        def _strict_ok(o, _s, _exp=exp):
            _ok, _d = PS.matches(prune_for_level(_exp, o.level), o, strict=True)
            return _ok, '; '.join(_d)

        aok, obs2, s2, why = act(expect=exp, action=(fn if fn else (lambda: None)),
                                 state='machine:' + name, what=name,
                                 timeout=_bud.get('timeout'), ok_when=_strict_ok)
        _strict_flip = False
        if aok:
            # 动作方说"到了"还不够: 状态机用自己的**严格**判据复核(act 用的是宽松版),
            # 否则「层级对、科室错」会被当成到达 —— 那正是浮窗超前的来源。
            sok, sdiffs = PS.matches(prune_for_level(exp, obs2.level), obs2, strict=True)
            if not sok:
                aok, why = False, 'strict: %s' % ';'.join(sdiffs)
                _strict_flip = True
        path.append((name, aok, why))
        # 只有"act 说 ok 但严格复核翻盘"才是 act 之外的新判定(act 失败时它已记过,
        # 再记一次会把同一次失败算两遍 => 熔断阈值实际变成 N/2)。
        if _strict_flip and judge is not None and judge(False, name + ':strict', why, obs2):
            return 'fail', obs2, s2, 'verify_break', path
        if tripped is not None and tripped():
            return 'fail', obs2, s2, 'verify_break', path
        if trace:
            trace(obs2, 'machine:%s' % name, exp, obs2, aok, s2, {'why': why})
        if not aok:
            # 这条边没走通: 重新观测再查表(re-plan), 不沿用旧假设
            tried[name] = tried.get(name, 0) + 1
            _att = budget_for(state_of(obs), name).get('attempts', 2)
            if tried[name] >= _att and name != 'stay':
                banned.add(name)
                if log:
                    log('  MACHINE: 边 %s 连续 %d 次未生效(出口=%s) => 本次禁用'
                        % (name, tried[name], exit_for(state_of(obs))))
            if log:
                log('  MACHINE: 边 %s 未生效(%s) => 重新观测' % (name, why))
            s = dump()
            obs = observe(s)
            continue
        obs, s = obs2, s2
        if announce:
            announce(name, obs)
    return 'fail', obs, s, 'max_hops(%d) path=%s' % (max_hops, path), path
