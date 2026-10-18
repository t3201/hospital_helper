# -*- coding: utf-8 -*-
"""page_state.py — 观测层: dump -> PageIdentity(页面事实) + 谓词 + 期望/实测比对 + 证据链。

为什么要有这一层(2026-10-15 复盘, 语料 = slots/out/raw + slots/out/diag 共 1322 份真机 dump):
  旧代码里「我们在哪一层」是**写出来的**(pos = _pos_now(level='444') 之类), 不是**读出来的**;
  子 tab 是否真的切过去只有间接证据(cards_key 变没变 / 「暂无可预约」在不在), 于是
  「点了没生效」被当成 ok, 浮窗内容比屏幕超前 1~2 步。本模块只回答一个问题:
      **这张屏幕上, 现在客观是什么页面?**

语料验证过的硬信号(本模块的主判据):
  1) 全屏节点(面积 >= 屏 50%)的 content-desc = SPA 路由标题:
       '选科室'=L1 / '医生主页'=医生主页 / 科室名=L2 / '预约挂号'=搜索页 ...
  2) 子 tab 节点 resource-id='van-tabs-<inst>-<i>', **选中项 selected="true"**:
       含 >=2 个子 tab 的 493 份 dump 里, 368 份恰好 1 个 selected(全部 page_kind=L2),
       125 份 0 个 selected(全部是医生主页, 子 tab 名只出现在「简介」正文里), 0 份 >1。
       => selected 是「子 tab 已激活」的唯一判据; cards_key/空态文本只作降级兜底。
  3) 日期条节点同样是 van-tabs-*, 但 text 形如 '周四 10-08 有号' => 与子 tab 用文本区分。
  4) 空窗(0 个 <node>)是**读屏失败**, 不是页面状态 => level='no_window'(单独熔断计数)。

约定: 本文件不 import devbridge / 不碰设备, PC 与端侧字节一致, 可离线跑语料回归
(tools/page_state_check.py)。
"""
import json
import os
import re
from collections import Counter
import time

# ---------------- 词表(与 fetch_slots 保持同义, 不反向 import 以免循环) ----------------
SUB_TABS = {'专家门诊', '普通门诊', '专病门诊', '特需门诊', '互联网', '夜间门诊', '凌晨门诊'}
YJ = '预约挂号'
T444 = '全部医院 (444)'
T444b = '全部医院(444)'
HINT_444 = '搜索医院、医生、疾病'
AUTH_T = '需要如下个人信息'
# 2026-10-15 语料缺口: 现版随申办登录页不含 AUTH_T, 旧指纹漏判成 'other' => 触发无谓重启
LOGIN_KW = ['请输入登录密码', '注册账户', '第三方免注册登录', '忘记密码', '认证帮助',
            'English Login', '中文登录']
BLOCKED_KW = ['实名认证', '验证码', '滑动验证', '人脸识别', '身份核验', '风控', '安全验证']
TITLE_L1 = '选科室'
TITLE_DOC = '医生主页'
EMPTY_TEXTS = ('暂无可预约', '暂无号源', '没有更多')
RE_444 = re.compile(r'全部医院\s*\(?\s*444')
RE_VAN_TABS = re.compile(r'^van-tabs-\d+-(\d+)$')
RE_DAY_CELL = re.compile(r'^周[一二三四五六日天]\s*\d{2}-\d{2}\s*(有号|无号|约满|停诊)')
RE_ID_LINE = re.compile(r'^(\S.*?)\s*[-–—－]\s*(\S.*)$')
RE_ID_LINE2 = re.compile(r'^(\S+?)\s{2,}(\S.+)$')
# 机构名后缀(2026-10-15 语料: '第一妇婴保健院东院' 不含 '医院', 旧过滤把它漏掉)
HOSP_SUFFIX = ('医院', '保健院', '卫生院', '中心', '诊所', '门诊', '急救', '血站', '卫校', '站')
NODE_RE = re.compile(r'<node\b([^>]*?)/?>')
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
BOUNDS_RE = re.compile(r'\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]')


# ---------------- 原始节点(保留 selected / resource-id / content-desc) ----------------

def raw_nodes(s):
    """dump -> [{text,desc,rid,cls,pkg,selected,clickable,checked,enabled,x1..y2,depth,index}]

    与 parse_nodes 的区别: parse_nodes 只留 (text,cls,bounds) 且丢掉空 text 节点,
    因此**看不见** selected / resource-id —— 这正是旧代码无法核实子 tab 的结构性原因。
    """
    out = []
    if not s:
        return out
    for m in NODE_RE.finditer(s):
        d = dict(ATTR_RE.findall(m.group(1)))
        b = BOUNDS_RE.search(d.get('bounds', ''))
        if not b:
            continue
        x1, y1, x2, y2 = (int(g) for g in b.groups())
        out.append({
            'text': (d.get('text') or '').replace('\xa0', ' ').strip(),
            'desc': (d.get('content-desc') or '').replace('\xa0', ' ').strip(),
            'rid': d.get('resource-id') or '',
            'cls': (d.get('class') or '').rsplit('.', 1)[-1],
            'pkg': d.get('package') or '',
            'selected': d.get('selected') == 'true',
            'clickable': d.get('clickable') == 'true',
            'checked': d.get('checked') == 'true',
            'enabled': d.get('enabled') != 'false',
            'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2,
            'depth': int(d.get('depth') or -1),
            'index': int(d.get('index') or -1),
        })
    return out


def screen_size(nodes, fallback=(1080, 1920)):
    """从节点并集推屏幕尺寸(dump 里全屏节点 bounds=[0,192][1080,1920])。"""
    if not nodes:
        return fallback
    w = max(n['x2'] for n in nodes)
    h = max(n['y2'] for n in nodes)
    return (w, h) if w > 0 and h > 0 else fallback


def page_title(nodes, sw=None, sh=None):
    """全屏节点(面积>=屏 50%)的 **text**(缺 text 才看 content-desc) = SPA 路由标题。

    语料实测(1322 份): 全屏节点 [0,192][1080,1920] 的 text 就是路由名 ——
      '选科室'=L1 / '医生主页'=医生主页 / 科室名(如 '炎症性肠病专病门诊')=L2 / '预约挂号'=搜索页。
    (2026-10-15 首版误取 content-desc => 全部 title='' , 医生主页被误判成 L2。)
    """
    if not nodes:
        return ''
    sw = sw or max(n['x2'] for n in nodes)
    sh = sh or max(n['y2'] for n in nodes)
    area = float(max(sw, 1) * max(sh, 1))
    cand = [n for n in nodes
            if (n['text'] or n['desc'])
            and (n['x2'] - n['x1']) * (n['y2'] - n['y1']) >= 0.5 * area]
    if not cand:
        return ''
    cand.sort(key=lambda n: (n['depth'] if n['depth'] >= 0 else 99, -(n['x2'] - n['x1'])))
    n0 = cand[0]
    return n0['text'] or n0['desc']


# ---------------- 子 tab / 日期条 ----------------

def subtab_nodes(nodes, sw=None, sh=None):
    """L2 子 tab 节点, 按 x 左->右, 同名去重。

    优先用 rid=van-tabs-*-<i>(几何无关, 双机型天然适配); rid 缺失时退回几何窗口
    (0.08H < y1 < 0.46H, 宽>80, 高>20) —— 与旧 find_subtab_nodes 一致, 保证老 dump 可用。
    """
    out = []
    sh = sh or max([n['y2'] for n in nodes] or [1920])
    for n in nodes:
        if n['text'] not in SUB_TABS:
            continue
        m = RE_VAN_TABS.match(n['rid'])
        if not m:
            if not (0.08 * sh < n['y1'] < 0.46 * sh and (n['x2'] - n['x1']) > 80
                    and (n['y2'] - n['y1']) > 20):
                continue
        if any(o['text'] == n['text'] for o in out):
            continue
        out.append(n)
    out.sort(key=lambda n: n['x1'])
    return out


def active_subtab(nodes, sw=None, sh=None):
    """-> (name|None, evidence)  evidence={'selected':[names], 'tabs':[names], 'how':...}

    selected="true" 是唯一硬判据; 恰好 1 个才认为「已激活」。0 个/多个 => name=None,
    由调用方决定降级(cards_key 比对)或报错 —— **不允许**用「暂无可预约」当作切换成功。
    """
    tabs = subtab_nodes(nodes, sw, sh)
    sel = [n['text'] for n in tabs if n['selected']]
    ev = {'selected': sel, 'tabs': [n['text'] for n in tabs], 'how': 'selected'}
    if len(sel) == 1:
        return sel[0], ev
    ev['how'] = 'none' if not sel else 'multi'
    return None, ev


def date_bar(nodes):
    """日期条 -> [{'text','day','state','selected','idx'}] (van-tabs 且 text 形如 周X MM-DD 有号)。"""
    out = []
    for n in nodes:
        m = RE_DAY_CELL.match(n['text'])
        if not m:
            continue
        rid = RE_VAN_TABS.match(n['rid'])
        out.append({'text': n['text'], 'day': n['text'].split()[1] if len(n['text'].split()) > 1 else '',
                    'state': m.group(1), 'selected': n['selected'],
                    'idx': int(rid.group(1)) if rid else -1})
    return out


def identity_line(nodes):
    """医生主页身份行 -> (hospital, dept) | None。实测两行都可用(优先带连字符那行):
       '东方医院南院 - 中医治未病'  /  '第一妇婴保健院东院    妇科'(全角空格归一后 >=2 空格)。"""
    best = None
    for n in nodes:
        t = n['text']
        if not t or '简介' in t or len(t) > 60:
            continue
        m = RE_ID_LINE.match(t) or RE_ID_LINE2.match(t)
        if not m:
            continue
        h, d = m.group(1).strip(), m.group(2).strip()
        if not any(k in h for k in HOSP_SUFFIX):
            continue
        if not d or '医院' in d or '简介' in d:
            continue
        if RE_ID_LINE.match(t):        # 带连字符的行更可靠, 直接采用
            return h, d
        if best is None:
            best = (h, d)
    return best


def hospital_of_l1(nodes):
    """L1(选科室)头部医院名: 医院 logo 右侧同一行的第一行文本。

    语料实测(SH-003 泌尿外科 L1): logo 是 Image 节点, **alt 名落在 text** —— text='hos.2dae5e44'
    [45,237][363,498]; 其右 [393,237][948,294] text='华山医院', 再下面才是 '三级甲等' 与地址。
    (2026-05 首版只看 desc/rid => 0/2 命中; 现同时看 text/desc/rid。
    2026-10-16 实机 SH-084 变体: logo 的 alt 名不是 'hos.*' 而是纯数字('1511431255705'),
    只认 'hos.' 前缀 => 医院名取不到 => L1 身份里 hospital 为空, 证据链无法核实。
    现按"头部带内的图片节点"认 logo, 再退一步直接认头部带里的医院名。)"""
    logo = None
    for n in nodes:
        for v in (n['text'], n['desc'], n['rid']):
            if v.startswith('hos.'):
                logo = n
                break
        if logo is not None:
            break
    sw, sh = screen_size(nodes)
    if logo is None:
        # 变体: 头部带内、左列的一张图片(alt 名可能是纯数字/哈希), 位置特征比名字特征可靠
        for n in nodes:
            if n['cls'] not in ('Image', 'ImageView'):
                continue
            if not (0 < n['x1'] < 0.15 * sw):
                continue
            if not (0.15 * sw <= (n['x2'] - n['x1']) <= 0.45 * sw):
                continue
            if not (0.05 * sh <= n['y1'] <= 0.30 * sh):
                continue
            logo = n
            break
    if logo is None:
        # 最后一步: 头部带里直接找医院名(不猜正文里的医院字样, 只看 logo 右侧那一列)
        for n in sorted([x for x in nodes
                         if x['text'] and x['y1'] < 0.25 * sh and x['x1'] >= 0.3 * sw],
                        key=lambda x: (x['y1'], x['x1'])):
            t = n['text']
            if ('医院' in t and len(t) <= 20 and '门诊' not in t and '科' not in t) or \
                    t.endswith(('卫生院', '保健院', '急救中心', '血液中心', '血站', '卫校')):
                return t
        return ''
    cand = [n for n in nodes
            if n['text'] and not n['text'].startswith('hos.')
            and n['x1'] >= logo['x2'] - 10 and abs(n['y1'] - logo['y1']) < 120]
    if not cand:
        return ''
    cand.sort(key=lambda n: (n['y1'], n['x1']))
    return cand[0]['text']


# ---------------- 页面等级 ----------------

def empty_dump(s):
    """读屏失败(空窗): 不是页面状态, 单独计数。"""
    if not s or not s.strip():
        return True
    return '<node' not in s


def layout_collapsed(nodes):
    """True = 塌陷帧: 绝大多数内容节点 y1==y2(零高) => WebView 布局未稳定。

    实测(2026-10-15 20:49, 476cf991, SH-084 眼科门诊): 整页节点 bounds 全是
    (x,241)-(x',241), 子tab 行高 0。此时**任何几何结论都不可信**(子tab 会被筛空、
    卡片坐标会挤在同一行)。语料 1322 份 dump 里 0 份这种形态 => 它是瞬时帧,
    正确处置是"等一等重读", 不是"按这一帧下结论"。
    """
    body = [n for n in nodes if (n['text'] or n['clickable'])]
    if len(body) < 10:
        return False
    zero = sum(1 for n in body if n['y2'] - n['y1'] <= 1) / float(len(body))
    # 众数 y 占比: 塌陷时几乎所有节点挤在同一行(实测 mode_y=241 = WebView 顶)
    top_n = max(Counter(n['y1'] for n in body).values())
    mode = top_n / float(len(body))
    # 阈值由语料定: 1322 份里命中 0(最接近 mode=0.698 但 zero=0.000); 塌陷样本 0.73/0.77
    return zero >= 0.60 and mode >= 0.70


def level(s, nodes=None, six_match=None):
    """页面等级(标题优先, 关键词兜底)。

    与旧 page_kind 的差别: (a) 空窗 -> 'no_window'(不再混进 'other');
    (b) 登录页 -> 'login'(旧指纹只认 AUTH_T, 现版登录页漏判成 other => 无谓重启);
    (c) 标题优先 => L2 不再依赖「不限 日期 + 有号/无号」这种在空态页会失效的关键词。
    six_match: 可选的六字段判定回调(dump)->bool, 由 fetch_slots 注入 six_fields。
    """
    if empty_dump(s):
        return 'no_window'
    nodes = nodes if nodes is not None else raw_nodes(s)
    if any(k in s for k in BLOCKED_KW):
        return 'blocked'
    if AUTH_T in s:
        # 2026-10-15 端侧: 小程序授权同意页(姓名/手机号/… + 「同意」)。**可自助**,
        # 与需要凭据的登录页不是一回事 => 单独一级, 由状态机的 agree_auth 边处理。
        return 'auth'
    if any(k in s for k in LOGIN_KW):
        return 'login'
    if layout_collapsed(nodes):
        # 塌陷帧: 层级不可判定(等重读)。放在 444 之前 —— 444 的输入框也要靠几何点击。
        return 'unknown'
    if T444 in s or T444b in s or RE_444.search(s) or HINT_444 in s:
        return '444'
    t = page_title(nodes)
    if t == TITLE_DOC:
        return 'six' if (six_match and six_match(s)) else 'doc'
    if t == TITLE_L1:
        return 'L1'
    if t == YJ:
        # 搜索页(独立频道)才有「历史搜索」; 没有它时**不猜** L2, 交给后面的关键词兜底
        return 'search' if '历史搜索' in s else 'unknown'
    if t and t not in (YJ, TITLE_L1, TITLE_DOC):
        if subtab_nodes(nodes) or date_bar(nodes) or '去预约' in s or '暂无可预约' in s:
            return 'L2'
    if 'et_search' in s:
        return 'native_search'
    if '搜索框' in s and '扫一扫' in s:
        return 'home'
    # 关键词兜底(标题缺失的老 dump)
    if '不限 日期' in s and ('有号' in s or '无号' in s):
        return 'L2'
    if ('专家门诊' in s or '普通门诊' in s) and ('去预约' in s or '已约满' in s or '暂无可预约' in s):
        return 'L2b'
    if '选科室' in s:
        return 'L1'
    if '医生主页' in s:
        return 'six' if (six_match and six_match(s)) else 'doc'
    return 'unknown'


# ---------------- L1 科室大类 tab / 医生主页 医生名 ----------------
# 语料实测(1322 份, 2026-10-15):
#   L1(选科室): 左列 = 科室大类 tab(x1=0, 宽 390 = 0.36W), 右列科室列表 x1=387;
#               医院卡片在 y<0.30H(y=237) => 用 y1>0.30H 排除; **选中项带 selected="true"**(2/2)。
#   医生主页  : 页头名可能是人名, 也**常常是科室名**(827/827 份 doc 页里页头=科室名),
#               直接当人名会大面积假阳性 => 分级 title/fav/dept/none, 只有前两者算人名。

def tab_nodes(nodes, sw=None, sh=None):
    """L1 左列「科室大类」tab 节点, 按 y 上->下(几何判据见上方语料实测)。"""
    out = []
    sh = sh or max([n['y2'] for n in nodes] or [1920])
    sw = sw or max([n['x2'] for n in nodes] or [1080])
    for n in nodes:
        if not n['text']:
            continue
        if n['x1'] > 0.35 * sw or (n['x2'] - n['x1']) < 0.20 * sw:
            continue
        if n['y1'] < 0.30 * sh:
            continue
        out.append(n)
    out.sort(key=lambda n: n['y1'])
    return out


def active_tab(nodes, sw=None, sh=None):
    """已激活的科室大类: **恰好一个** selected=True 才算; 0 个或多个 => None(不猜)。

    与 active_subtab 同一诚实规则 —— 没有硬信号就说没有, 不用颜色/位置猜。
    """
    ns = tab_nodes(nodes, sw, sh)
    sel = [n for n in ns if n['selected']]
    if len(sel) == 1:
        return sel[0]['text'], 'selected'
    return None, ('none' if not sel else 'multi')


_SF = [None, False]


def _six_fields():
    """惰性取 six_fields(端侧/PC 两条导入路径都试); 取不到返回 None => doctor 如实降级。"""
    if _SF[1]:
        return _SF[0]
    _SF[1] = True
    sf = None
    try:
        from slots import six_fields as _sf
        sf = _sf
    except Exception:
        try:
            import importlib
            sf = importlib.import_module('six_fields')
        except Exception:
            sf = None
    _SF[0] = sf
    return sf


def doctor_name(s, dept='', hosp=''):
    """医生主页页头 -> (name, how)。

    how: 'title' 页头带职称(主任医师/主治医师/…) => 硬人名
         'fav'   「姓名 收藏」且与科室名不同 => 视为人名
         'dept'  页头名与科室名相同/互含 => 这是科室主页, doctor=None
         'none'  页头取不到 => doctor=None
    """
    sf = _six_fields()
    if sf is None or not s:
        return None, 'no_six_fields'
    r = sf.find_page_doctor(s)
    if not r:
        return None, 'none'
    norm, raw = (r[0] or ''), (r[1] or '')
    tre = getattr(sf, 'DOC_TITLE_RE', None)
    if tre is not None and tre.match(raw.strip()):
        return strip_title(norm), 'title'
    d = (dept or '').strip()
    if d and (norm == d or d in norm or norm in d):
        return None, 'dept'
    h = (hosp or '').strip()
    if h and (norm == h or h in norm or norm in h):
        # 页头是**医院名**(语料实测 '东方医院南院  中医治未病' 这类): 不是人名。
        return None, 'hosp'
    return norm, 'fav'


# 职称剥离/残片判据的**唯一实现在 six_fields**(观测层与记录层必须共用同一判据,
# 两份实现迟早漂移: 观测层剥干净了、记录层还脏着, 就会拿 '任润萍' 去比 '任润萍副')。


def strip_title(name):
    sf = _six_fields()
    f = getattr(sf, 'strip_title', None) if sf else None
    return f(name) if f else (name or '').strip()


def title_residue(name):
    sf = _six_fields()
    f = getattr(sf, 'title_residue', None) if sf else None
    return bool(f(name)) if f else False


# ---------------- PageIdentity ----------------

class PageIdentity(object):
    """一次读屏的全部可核实事实。__slots__ 便于 JSON 化与 diff。"""

    __slots__ = ('level', 'title', 'hospital', 'dept', 'tab', 'tab_how',
                 'subtabs', 'subtab_active', 'subtab_how', 'days', 'day_selected',
                 'doctor', 'doctor_how', 'empty_state', 'collapsed',
                 'nodes', 'n_nodes', 'screen', 'pkg')

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def to_dict(self, with_nodes=False):
        d = {}
        for k in self.__slots__:
            if k == 'nodes' and not with_nodes:
                continue
            d[k] = getattr(self, k)
        return d

    def brief(self):
        return ('level=%s title=%r hosp=%r dept=%r tab=%r(%s) subtab=%r(%s) tabs=%s '
                'doctor=%r(%s) days=%d empty=%s%s'
                % (self.level, self.title, self.hospital, self.dept, self.tab, self.tab_how,
                   self.subtab_active, self.subtab_how, self.subtabs,
                   self.doctor, self.doctor_how, len(self.days or []), self.empty_state,
                   ' COLLAPSED' if self.collapsed else ''))


def observe(s, six_match=None):
    """dump -> PageIdentity(观测的唯一入口)。"""
    nodes = raw_nodes(s)
    sw, sh = screen_size(nodes)
    lv = level(s, nodes, six_match)
    tabs = subtab_nodes(nodes, sw, sh)
    act, ev = active_subtab(nodes, sw, sh)
    days = date_bar(nodes)
    idl = identity_line(nodes)
    hosp = hospital_of_l1(nodes) if lv == 'L1' else (idl[0] if idl else '')
    dept = (idl[1] if idl else (nodes and _dept_from_title(nodes) or ''))
    if lv == 'L1':
        dept = ''
    pkgs = [n['pkg'] for n in nodes if n['pkg']]
    # tab 只有 L1 承载; doctor 只有医生主页(doc/six)承载 —— 其余层级标 'n/a' 而不是空,
    # 这样 matches() 能区分「这页压根没有该信号」与「有信号但对不上」。
    tab, tab_how = active_tab(nodes, sw, sh) if lv == 'L1' else (None, 'n/a')
    if lv in ('doc', 'six'):
        doctor, doctor_how = doctor_name(s, dept or '', hosp)
    else:
        doctor, doctor_how = None, 'n/a'
    return PageIdentity(
        level=lv,
        title=page_title(nodes, sw, sh),
        hospital=hosp,
        dept=dept,
        tab=tab,
        tab_how=tab_how,
        subtabs=[n['text'] for n in tabs],
        subtab_active=act,
        subtab_how=ev['how'],
        days=days,
        day_selected=next((d['text'] for d in days if d['selected']), ''),
        doctor=doctor,
        doctor_how=doctor_how,
        empty_state=any(t in s for t in EMPTY_TEXTS),
        collapsed=layout_collapsed(nodes),
        nodes=nodes,
        n_nodes=len(nodes),
        screen=(sw, sh),
        pkg=pkgs[0] if pkgs else '',
    )


def _dept_from_title(nodes):
    """L2 的科室名 = 全屏节点标题(实测 content-desc 就是科室名, 如 '炎症性肠病专病门诊')。"""
    t = page_title(nodes)
    if t in (YJ, TITLE_L1, TITLE_DOC, ''):
        return ''
    return t


def active_name(s):
    """dump -> 已激活子tab 名(硬判据 selected="true"); 不可用则 None。

    给导航用的**单一入口**: 只吃 dump 原文, 避免调用方把 parse_nodes 的元组喂进来
    (parse_nodes 丢掉了 selected/resource-id, 喂进来必然判不出来)。"""
    name, _ev = active_subtab(raw_nodes(s))
    return name


def dept_consistent(s, dept, obs):
    """科室一致性: 标题一致, 或科室名在页面**任意处**出现 => 通过。入参是 dump 原文。

    为什么放宽到"任意处": 标题是 SPA 路由残留的风险点(语料实测 1322 份里 125 份文件名声称的
    科室在页面上根本不存在 —— 例: 三个不同科室的文件名, dump 标题都是"炎症性肠病专病门诊"),
    但也不能一有出入就判死 —— 有的医院 L2 标题是科室大类名(如 '内科'), 与目标 '内科门诊' 同义。
    因此: 标题不符时再看页面别处有没有该科室名, 都没有才算不符。返回 (ok, why)。"""
    if not dept:
        return True, ''
    if _name_ok(dept, obs.dept if isinstance(obs, PageIdentity) else (obs or {}).get('dept', '')):
        return True, ''
    for n in raw_nodes(s):
        t = n['text']
        if t and not t.startswith('van-tabs-') and (dept in t or _name_ok(dept, t)):
            return True, '页面别处出现科室名(标题=%s)' % (getattr(obs, 'dept', '') or '')
    return False, '标题=%s' % (getattr(obs, 'dept', '') or '')


# ---------------- 期望 vs 实测 ----------------

def matches(expected, obs, strict=False):
    """expected: {'level':.., 'hospital':.., 'dept':.., 'subtab':..} (缺项不校验)
    -> (ok, [差异说明])  差异说明要能直接进日志/浮窗, 供事后核实。

    strict=True(状态机的"到达"判据, 2026-10-15 第③步): 名称只允许
    「完全相等」或「页面确实显示了目标名(want in got)」。
    为什么要有 strict: 宽松互含会把 目标'炎症性肠病专病门诊XX' vs 页面'炎症性肠病专病门诊'
    判成一致 —— 走错了科室却宣布到达, 正是用户投诉的「浮窗说进了、屏上没进」。
    旧采集层仍用宽松版(清单里的 '(骨科)创伤骨科门诊' 这类长名要能对上短标题)。
    """
    diffs, hard = [], 0
    if not isinstance(obs, PageIdentity):
        # 同一份 page_state 可能被以两条导入路径加载成两个模块实例(端侧 'slots.page_state'
        # 与顶层 'page_state'), 此时 isinstance 会失效。鸭子类型识别, 绝不把观测对象
        # 当空 dump 重新观测 —— 那会把真实 L2 误报成 no_window(2026-10-15 实测踩到)。
        if not isinstance(obs, str) and hasattr(obs, 'level'):
            pass
        else:
            obs = observe(obs if isinstance(obs, str) else '')
    eq = _name_ok_strict if strict else _name_ok
    exp_level = expected.get('level')
    if exp_level is not None:
        want = exp_level if isinstance(exp_level, (list, tuple, set)) else [exp_level]
        if obs.level not in want:
            diffs.append('level: 期望 %s, 实测 %s' % ('|'.join(want), obs.level))
            hard += 1
    # soft=True 的字段: 这一页**压根没给出**该信号(组版医生主页没有人名 / L1 没有 selected)
    # => 不能判死, 但必须留痕(前缀 ≈), 事后能从证据链看出「当时无法核实」。
    for key, attr, soft in (('hospital', 'hospital', False), ('dept', 'dept', False),
                            ('subtab', 'subtab_active', False),
                            ('tab', 'tab', True), ('doctor', 'doctor', True)):
        want = expected.get(key)
        if not want:
            continue
        got = getattr(obs, attr, None)
        if soft and not got:
            # 这一页压根没给出该信号 => **不判死**, 但必须留痕(前缀 ≈),
            # 事后从证据链能看出「当时这一项无法核实」, 而不是「核实通过」。
            diffs.append('≈%s: 期望 %r, 页面未显示该信号(不判死, 但无法核实)' % (key, want))
            continue
        if not eq(want, got):
            diffs.append('%s: 期望 %r, 实得 %r' % (key, want, got))
            hard += 1
    return (not hard), diffs


def _name_ok_strict(want, got):
    """严格名比对: 页面必须真的显示着目标名(相等或 want 是 got 的子串)。"""
    if not got:
        return False
    want, got = str(want).strip(), str(got).strip()
    return want == got or want in got


def _name_ok(want, got):
    """宽松但诚实的名称比对: 完全相等 / 互为子串(骨科门诊 vs 骨科) 都算一致; 空值算不一致。"""
    if not got:
        return False
    want, got = str(want).strip(), str(got).strip()
    return want == got or want in got or got in want


# ---------------- 证据链(事后能回答「当时真的在普通门诊吗」) ----------------

class Trace(object):
    """每次状态转移写一行 JSONL + 保留对应 dump, 让「页面与状态是否一致」可被离线复核。

    目录: <out>/trace/<run_id>/events.jsonl + dumps/NNN_<state>.xml
    """

    def __init__(self, out_dir, run_id=None, keep_dumps=True, max_dumps=400):
        self.dir = os.path.join(out_dir, 'trace', run_id or time.strftime('%Y%m%dT%H%M%S'))
        self.keep = keep_dumps
        self.max_dumps = max_dumps
        self.n = 0
        self.fp = None
        try:
            os.makedirs(os.path.join(self.dir, 'dumps'), exist_ok=True)
            self.fp = open(os.path.join(self.dir, 'events.jsonl'), 'a', encoding='utf-8')
        except Exception:
            self.fp = None

    def event(self, state, action, expected, obs, ok, diffs=None, cost_ms=None, dump=None,
              extra=None):
        self.n += 1
        st_label = state if isinstance(state, str) else type(state).__name__
        rec = {'n': self.n, 'ts': time.strftime('%Y%m%dT%H%M%S'), 'state': st_label,
               'action': action, 'expected': expected, 'observed': obs.brief(),
               'ok': bool(ok), 'diffs': diffs or []}
        if cost_ms is not None:
            rec['cost_ms'] = int(cost_ms)
        if extra:
            rec.update(extra)
        if self.keep and dump and self.n <= self.max_dumps:
            try:
                fn = '%03d_%s.xml' % (self.n, re.sub(r'[^\w-]', '_', st_label)[:24])
                with open(os.path.join(self.dir, 'dumps', fn), 'w', encoding='utf-8') as f:
                    f.write(dump)
                rec['dump'] = 'dumps/' + fn
            except Exception:
                pass
        if self.fp:
            try:
                self.fp.write(json.dumps(rec, ensure_ascii=False) + '\n')
                self.fp.flush()
            except Exception:
                pass
        return rec

    def close(self):
        if self.fp:
            try:
                self.fp.close()
            except Exception:
                pass
            self.fp = None
