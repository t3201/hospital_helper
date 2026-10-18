# -*- coding: utf-8 -*-
"""six_fields.py — 号源「六字段」明细记录（stage-2 明细层），零模型、纯标准库、只读采集。

用户口径（2026-10-03 重定义）:
  号源必须以达到含【就诊时间、就诊医院、就诊科室、就诊医生、门诊类型、挂号费用】的
  页面为准；**不要在那个页面点"确定"**（只读 + Back）。
  一般在到达这个页面之前的页面（如医生主页）也有这些信息 —— 实测医生主页含 5 项
  （时间/科室/医生/门诊类型/费用），「就诊医院」所在页面指纹待安卓小能手探路结论。

诚实性规则（硬性）:
  * 逐字段 provenance.source ∈ {'page_self'(该页自身文本), 'nav_context'(从导航上下文补, 显式标注), None}。
  * 就诊医院若不在该页，必须记 nav_context 或 None，**绝不拿导航上下文冒充页面事实**。
  * 「六字段齐全页」由指纹规则 SIX_PAGE_RULES 判定；规则未命中 => 报 error 不猜。
    探路结论到达后在此登记 markers（全含即命中）；当前列表为空 = 永不猜测。
  * captured_at 一律用**设备时钟**；raw_evidence 指向该页 UI XML 快照（可审计）。
  * S13: 费用 evidence 必须**逐字来自 XML**（全角 ￥/半角 ¥ 以页面为准）, 禁止 '¥<数值>'
    合成串；新增字段 挂号费用_原文 存该原串；extract/extract_fee/extract_type 存源文本在
    raw XML 中的**字节偏移**（text_off, grep -bo 口径, 与安卓小能手 s14-unified-v1 同约定）
    + node_bounds, 供 compare_truth.py 零别名零容错裁决。
价位映射沿用现口径: <=15 -> '15', >15 且 <100 -> '50'(51–99 归 50), >=100 -> '100+'；原始价 fee_cny_raw 保留。

S3 登记（2026-10-03, 来源 slots/S1_SIXFIELD_REPORT.md, 十院眼科 徐挺主任医师 实测）:
  * 医生主页(个人版): 点 L2 医生卡**卡身**到达, 六字段同页齐全, 无需点「去预约」。
  * 医生主页(组版): 点卡上「去预约」按钮到达(本批不点), 缺「就诊医生」仅 5 字段
    -> find_page_doctor=None => variant=group_missing_doctor, 不产出六字段记录
       (六字段定义要求医生名在场; 产出 missing=就诊医生 的 5 字段记录会污染
        台账与抽查比对, 故只记 variant 不产出 — 理由: 交付口径=六字段齐全页)。
  * 确认预约信息页(兜底): L2 起 2 次点击, 只 dump 不点「确定」。
  * 坑6: 费用字符两套 — 医生主页 U+FFE5(￥) / 确认页 U+00A5(¥) => FEE_RE 兼容。
  * 坑7: 确认页医生值 = 职称+\xa0\xa0+姓名(顺序与医生主页相反) => CONFIRM_DOC_RE 归一。
  * 坑2: 姓名节点带「 收藏」后缀 => DOC_TITLE_RE/DOC_NAME_ONLY_RE 兼容。
"""
import hashlib
import json
import os
import re
import shutil

SIX_FIELD_KEYS = ('就诊时间', '就诊医院', '就诊科室', '就诊医生', '门诊类型', '挂号费用')

# ---------------- 「六字段齐全页」指纹（探路结论到达后登记，空表 = 不猜） ----------------
# 规则: markers 全部子串命中, 且若声明 any_of/any_of_regex 则其并集至少一者命中 => 判定为该页。
SIX_PAGE_RULES = [
    {
        'name': 'six_doc_home',
        'note': ('S1探路2026-10-03 十院眼科徐挺: 医生主页(个人/组版同模板); '
                 'WebView标题 医生主页 + 不限 日期 + (时段行|暂无可预约) + 医生简介; '
                 '命中后必须再经 find_page_doctor 分版(组版=缺就诊医生, 不产出记录)'),
        'markers': ['医生主页', '不限 日期', '医生简介'],
        'any_of': ['暂无可预约'],
        'any_of_regex': [r'\d{4}-\d{2}-\d{2}\s*周[一二三四五六日天]\s*\d{2}:\d{2}\s*-\s*\d{2}:\d{2}'],
    },
    {
        'name': 'six_confirm',
        'note': ('S1探路2026-10-03: 确认预约信息页(兜底, L2起2次点击, 两个「确定」只dump未点); '
                 '六 label 左列 + value 右列(几何配对); 费用=U+00A5; 医生值=职称+\\xa0\\xa0+姓名'),
        'markers': ['确认预约信息'],
    },
]


def match_six_page(s):
    """返回命中的规则名；未命中返回 None（调用方据此报 error，不得猜测）。
    规则: markers 全部子串命中; 若规则声明 any_of/any_of_regex, 其并集至少一者命中。"""
    for r in SIX_PAGE_RULES:
        if not r.get('markers') or not all(m in s for m in r['markers']):
            continue
        any_strs = r.get('any_of') or []
        any_re = r.get('any_of_regex') or []
        if (any_strs or any_re) and not (any(x in s for x in any_strs)
                                         or any(re.search(p, s) for p in any_re)):
            continue
        return r['name']
    return None


def fingerprint(s, kind):
    """page_fingerprint: 规则命中情况 + 可见文本内容哈希（抽查比对/审计用）。"""
    norm = ''.join(re.findall(r'text="([^"]*)"', s))
    return {
        'matched_rule': match_six_page(s),
        'kind': kind,
        'text_sha1': hashlib.sha1(norm.encode('utf-8')).hexdigest()[:16],
    }


# ---------------- 页面文本解析（与 fetch_slots.py 同口径: 过滤 icon-font/空文本, (y,x) 排序） ----------------
_NODE_RE = re.compile(r'<node [^>]*?/?>')
_BOUNDS_RE = re.compile(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]')

# 时段行: "2026-10-06 周二 15:30-16:00"（医生主页实测格式, S13 锁定）
# S13: 时段行 = 完整日期格式 YYYY-MM-DD 周X HH:MM-HH:MM; 与 L2 日期栏 BAR_RE 的
# 周X MM-DD 有号/无号 是**两种格式**, 各走各的正则, 禁止混用(本正则以 \d{4}- 开头,
# 天然不吃日期栏格式; BAR_RE 以 周 开头, 天然不吃时段行格式)。
SLOT_ROW_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})\s*周[一二三四五六日天]\s*(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})$')
# S3 坑6: 费用字符两套 — 医生主页 U+FFE5(￥) / 确认页 U+00A5(¥) => 正则兼容 + 可选空白
FEE_RE = re.compile(r'^[\u00a5\uffe5]\s*(\d+)$')
# 姓名+职称 同一文本节点（医生主页标题可带「 收藏」后缀; L2 卡身可为 姓名+空格+职称）
# 姓名组非贪婪: 避免 4 字姓名/长职称被贪婪吞并(「徐挺主任医师」=> 姓名徐挺+职称主任医师, 而非 徐挺主任+医生)
# S8: 收藏 用 (收藏)? 分组 —— 裸 收藏? 意为 收+可选藏, 会使「姓名+职称」(无收藏后缀)形态失配, find_page_doctor 对页头「徐峮 主任医师」返回 None => 静默丢医生
DOC_TITLE_RE = re.compile(r'^([\u4e00-\u9fff]{2,4}?)\s*(主任医师|副主任医师|主治医师|住院医师|医师|医生|护士|主管护师|主任护师|药师)\s*(?:收藏)?$')
# 无职称医生: 节点=姓名(+「 收藏」), 如 L2 卡身「徐健江」/ 医生主页标题「徐健江 收藏」
# S9: {2,4} -> {2,12}: 专病门诊子 tab 的页头是诊所名(「小儿眼病与视觉 收藏」等 >4 字),
# 旧长度上限使 find_page_doctor 返回 None => 误记 group_missing_doctor 丢六字段。
# 仍要求「收藏」后缀, 不会误配页头 chrome 词(医生主页/医生简介等无收藏)。
# S13 (S11 §1.4/C4): 专病门诊页头可含半角/全角括号(实测「屈光不正(近视) 收藏」,
# 半角 U+0028/U+0029) => 字符集加 ()（）·- 且长度放宽到 16。刻意**不加**「、」/空格:
# 组版页头「白内障、干眼 收藏」必须仍失配(否则组版误判个人版, group_missing_doctor
# 判据被破坏; 回归断言见 run_batch --check 项 23)。
DOC_NAME_ONLY_RE = re.compile(r'^([\u4e00-\u9fff()（）·\-]{2,16})\s*收藏$')
# 确认页医生值 = 职称+\xa0\xa0+姓名（与医生主页顺序相反, S1 码点核对）
CONFIRM_DOC_RE = re.compile(r'^(主任医师|副主任医师|主治医师|住院医师|医师|医生)[\s\xa0]*([\u4e00-\u9fff]{2,4})$')
# 门诊类型词表（长词优先匹配，避免 "皮肤科门诊" 误命中泛词 "门诊"）
# S9: 补「专病门诊」— 十院眼科 L2 有专病门诊子 tab, 诊所主页时段行块内实测文本即「专病门诊」;
# 缺它 => 行级类型丢失, 页内回退扫到科室名「眼科门诊」里的泛词「门诊」(S9 首跑 40 条中 10 条受影响)
OUTPATIENT_TYPES = sorted(['普通门诊', '专家门诊', '特需门诊', '专科门诊', '专病门诊', '互联网门诊',
                            '夜间门诊', '凌晨门诊', '儿童门诊', '成人门诊', '中医门诊', '门诊'],
                           key=lambda t: -len(t))
# 组版医生主页变体标记（缺「就诊医生」, 不产出六字段记录 — 见模块 docstring 理由）
VARIANT_GROUP_MISSING_DOCTOR = 'group_missing_doctor'
CONFIRM_LABELS = ('就诊时间', '就诊医院', '就诊科室', '就诊医生', '门诊类型', '挂号费用')


def parse_texts(s):
    """-> [(text, x1, y1, x2, y2)] 按 (y,x) 排序。"""
    rows = []
    for n in _NODE_RE.findall(s):
        tm = re.search(r'text="([^"]*)"', n)
        bm = re.search(r'bounds="(\[\d+,\d+\]\[\d+,\d+\])"', n)
        if not tm or not bm:
            continue
        t = tm.group(1)
        if not t.strip():
            continue
        if all(0xE000 <= ord(c) <= 0xF8FF for c in t):
            continue
        x1, y1, x2, y2 = map(int, _BOUNDS_RE.match(bm.group(1)).groups())
        rows.append((t, x1, y1, x2, y2))
    rows.sort(key=lambda r: (r[2], r[1]))
    return rows


def locate_node(s, text):
    """S13 (统一真值交换格式): 在 UI XML 中定位 text == text 的节点(精确匹配, 零别名/零归一化),
    返回其原文定位信息:
      {'text_off': 文本内容的**字节偏移**(text="..." 属性值起, UTF-8 字节流, grep -bo 口径,
         与安卓小能手 s14-unified-v1 真值同约定),
       'node_bounds': '[x1,y1][x2,y2]'}
    找不到 => None(调用方存 extract=None, 不猜)。取**首个**匹配节点; 调用方若持有
    文档序节点(如 parse_doc_home 的 finditer)应直接用该节点计算偏移, 不依赖本函数。"""
    if not text:
        return None
    esc = (str(text).replace('&', '&amp;').replace('<', '&lt;')
           .replace('>', '&gt;').replace('"', '&quot;'))
    m = re.search(r'<node [^>]*?text="%s"[^>]*?/?>' % re.escape(esc), s)
    if not m:
        return None
    node = m.group(0)
    tm = re.search(r'text="([^"]*)"', node)
    if tm is None or tm.group(1) != esc:
        return None
    bm = re.search(r'bounds="(\[\d+,\d+\]\[\d+,\d+\])"', node)
    char_off = m.start() + tm.start(1)
    byte_off = len(s[:char_off].encode('utf-8'))
    return {'text_off': byte_off, 'node_bounds': bm.group(1) if bm else None}


def fee_tier_from(cny):
    """价位映射: <=15 -> '15'; <=99 -> '50'(即 >50 且 <100 归 50 档); >=100 -> '100+'。
    原始价另存 fee_cny_raw。这是抓取到的实测挂号费分档(喂 ranking/API)，
    与用户预算档 fee_expect 是两条不同的轴，勿混淆。"""
    if cny is None:
        return None
    return '15' if cny <= 15 else ('50' if cny < 100 else '100+')


# ---------------- 医生主页 个人版/组版 判定 ----------------
def _prov(v, source, evidence=None):
    return {'value': v, 'source': source, 'evidence': evidence}


_HEADER_BLACKLIST = {'医生主页', '预约挂号', '医生简介', '科室简介', '推荐医生', '预约须知',
                     '预约', '挂号', '展开', '收起', '不限', '日期', '全部', '简介', '须知'}
_TITLE_WORDS_RE = re.compile(r'^(主任医师|副主任医师|主治医师|住院医师|医生|医师|护士|主管护师|主任护师|药师)$')


# 职称表必须**按长度降序**: '主任医师' 排在 '副主任医师' 前面会把 '任润萍副主任医师'
# 剥成 '任润萍副'(语料实测 324/744 份带残片) —— 人名一旦带职称残片, 后续 doctor 严格
# 核对就会拿 '任润萍副' 去比 '任润萍', 真到达也被判不符。
_TITLE_WORDS = ('副主任医师', '主任医师', '主治医师', '住院医师', '主管技师', '主管护师',
                '副主任技师', '副主任护师', '副研究员', '研究员', '副教授', '教授',
                '医师', '技师', '药师', '护师', '护士', '医生')


def strip_title(name):
    """'任润萍 副主任医师' -> '任润萍'; 可剥多层('主任医师 教授'), 剥完再收尾。

    剥不掉/剥剩下不到 2 字 => 原样返回(宁可脏, 也不要把人名削成单字)。
    """
    n = (name or '').strip()
    if not n:
        return n
    cut = n
    for _ in range(3):
        hit = False
        for t in _TITLE_WORDS:
            if cut.endswith(t) and len(cut) > len(t) + 1:
                cut = cut[:-len(t)].strip(' ·,，;；/\u3000')
                hit = True
                break
        if not hit:
            break
    return cut if len(cut) >= 2 else n


def title_residue(name):
    """人名里是否还挂着职称(剥职称失败的表征)。观测层与记录层共用这一份判据。"""
    n = (name or '').strip()
    if not n:
        return False
    if n.endswith(('副', '主', '技', '护', '医', '师', '研', '究')):
        return True
    return any(t in n for t in _TITLE_WORDS)


def clean_person_name(v):
    """记录字段 '就诊医生' 的**唯一清洗口**: 必须是人名, 不挂职称也不挂职称残片。

    实机(2026-10-16, run_20261016T001853): 158 条记录里 16 个 '医生' 有 12 个写成
    '刘芳主任医师'/'刘国强副' —— 因为 find_page_doctor 的 norm 口径是 **姓名+职称**
    (它用于 L2 卡片比对是对的), 被原样写进了记录。这里在出口剥职称 + 逐字收尾残片;
    整值就是职称词 => 返回 ''(调用方记 None), 不编造人名。原文由调用方保留复核。
    """
    if not isinstance(v, str) or not v.strip():
        return v if isinstance(v, str) else ''
    raw = v.strip()
    if raw in _TITLE_WORDS:
        return ''
    n = strip_title(raw)
    while len(n) > 2 and title_residue(n):
        n = n[:-1]          # UI 截断的职称残片('刘国强副'/'汪朝阳主')
    return n if not title_residue(n) else (n if len(n) >= 2 else '')


def _header_fav_node(rows):
    """True when the header band holds a standalone 收藏 node (old-WebView split layout)."""
    for (t, x1, y1, x2, y2) in rows:
        if t == '收藏' and 200 <= y1 < 550:
            return True
    return False


def find_page_doctor(s):
    """医生主页页头区(y1<550)找 医生 节点。
    返回 (norm, raw): norm=姓名+职称(无职称则仅姓名, 与 L2 卡身口径一致); raw=节点原文。
    组版页(页头=科室名+收藏, 如「白内障、干眼 收藏」) => None
    （调用方记 variant=group_missing_doctor, 不产出六字段记录）。"""
    for (t, x1, y1, x2, y2) in parse_texts(s):
        if y1 >= 550:
            continue
        m = DOC_TITLE_RE.match(t)
        if m:
            return (m.group(1) + m.group(2), t)
        m2 = DOC_NAME_ONLY_RE.match(t)
        if m2:
            return (m2.group(1), t)
    # 2026-10-07: old WebViews (MI 5X / Android 8.1) split the doc-home header into separate
    # a11y nodes: a name node (e.g. '儿科医院   新生儿科') + a standalone '收藏' node, so the
    # per-node regexes miss both. Fallback: if the header band (200<=y1<550) holds exactly one
    # CJK-only text node (width>100, not a known header word), treat it as the name node;
    # name = first segment when the last segment is a title word, else the last segment
    # (dept-group card '儿科医院  新生儿科' -> '新生儿科'; doctor '张伟 主任医师' -> '张伟').
    if _header_fav_node(parse_texts(s)):
        cands = []
        for (t, x1, y1, x2, y2) in parse_texts(s):
            if not (200 <= y1 < 550):
                continue
            if not t.strip() or t in _HEADER_BLACKLIST or t == '收藏':
                continue
            if (x2 - x1) <= 100:
                continue
            if not re.match(r'^[\u4e00-\u9fff()\uff08\uff09\u00b7\-\s\xa0]+$', t):
                continue
            cands.append(t)
        if len(cands) == 1:
            segs = [x for x in cands[0].replace('\xa0', ' ').split() if x]
            if segs:
                name = segs[0] if (len(segs) > 1 and _TITLE_WORDS_RE.match(segs[-1])) else segs[-1]
                return (name, cands[0])
    return None


def extract_confirm(s):
    """确认预约信息页: 左列 label(六字段名) -> 同行右侧最近 value 节点
    （几何配对: value.x1 > label.x2, y 区间重叠, 取 x1 最小; S1 实测 bounds 验证）。
    医生值 职称+姓名 -> 归一 姓名+职称; 费用 两套 ¥ 字符 -> int(原文逐字另存 vals['挂号费用_原文'])。
    S13: 返回 (vals, prov, locs); locs = {'time': 就诊时间节点定位, 'fee': 费用节点定位}
    (统一真值交换格式 extract 用; 定位不到 => None, 不猜)。"""
    rows = parse_texts(s)
    labels = {}
    for (t, x1, y1, x2, y2) in rows:
        if t in CONFIRM_LABELS and t not in labels:
            labels[t] = (x1, y1, x2, y2)
    vals, prov = {}, {}
    vals['挂号费用_原文'] = None
    locs = {'time': None, 'fee': None}
    for f in CONFIRM_LABELS:
        lb = labels.get(f)
        v = None
        if lb:
            best = None
            for (t2, cx1, cy1, cx2, cy2) in rows:
                if t2 == f or cx1 <= lb[2] or cx1 > 1080:
                    continue
                if cy2 <= lb[1] or cy1 >= lb[3]:
                    continue
                if all(0xE000 <= ord(c) <= 0xF8FF for c in t2):
                    continue
                if best is None or cx1 < best[1]:
                    best = (t2, cx1)
            v = best[0] if best else None
        if f == '就诊医生' and v:
            m = CONFIRM_DOC_RE.match(v)
            if m:
                v = m.group(2) + m.group(1)
        v_fee_raw = None
        if f == '挂号费用' and v:
            v_fee_raw = v   # S13: value 节点原文逐字(确认页半角 ¥319, 医生主页全角 ￥519)
            m = re.search(r'[\u00a5\uffe5]\s*(\d+)', v)
            v = int(m.group(1)) if m else None
            vals['挂号费用_原文'] = v_fee_raw
            locs['fee'] = locate_node(s, v_fee_raw)
        if f == '就诊时间' and v:
            m = re.search(r'\d{4}-\d{2}-\d{2}\s*周[一二三四五六日天]\s*\d{2}:\d{2}\s*-\s*\d{2}:\d{2}', v)
            if m:
                v = m.group(0)
                locs['time'] = locate_node(s, v)   # S13: 确认页 value 节点原文即整行(实测)
        vals[f] = v
        # S13: 费用 evidence = value 节点原文逐字, 不是 int(其他字段 evidence 仍为值本身)
        prov[f] = _prov(v, 'page_self' if v is not None else None,
                        v_fee_raw if f == '挂号费用' and v_fee_raw else v)
    return vals, prov, locs


# ---------------- 六字段抽取（逐字段 provenance） ----------------
def extract_fields(s, ctx, row_text=None, fee=None, fee_raw=None, doctor=None,
                   outpatient_type=None):
    """在一页 UI XML 上抽六字段。
    ctx = {'hospital_name':…, 'dept_name':…}（导航上下文, 仅作**显式标注**的补充来源）。
    row_text = 就诊时间（时段行原文, 页内）；fee = 行级 ¥ 价；
    fee_raw = 行级费用节点**原文**(S13, 如全角 ￥519; fee 与 fee_raw 应同来源传入);
    doctor = 医生节点原文。
    outpatient_type = 行级门诊类型（时段行块内, 优先于页内全局词表扫描）。
    返回 (vals, prov)，字段名用中文六字段。
    S13: vals['挂号费用_原文'] = 费用节点 XML 原串(逐字, 不合成); provenance.挂号费用.evidence
    同步改为该原串(旧口径 half-width ¥<数值> 合成串废除)。"""
    texts = [t for (t, _x1, _y1, _x2, _y2) in parse_texts(s)]
    vals, prov = {}, {}

    # 1) 就诊时间: 仅页内时段行（无行 = None，不猜）
    vals['就诊时间'] = row_text
    prov['就诊时间'] = _prov(row_text, 'page_self', row_text)

    # 2) 就诊医生: 页内 姓名+职称 节点
    doc = doctor
    if doc is None:
        for t in texts:
            if DOC_TITLE_RE.match(t):
                doc = t
                break
    vals['就诊医生'] = doc
    prov['就诊医生'] = _prov(doc, 'page_self', doc)

    # 3) 就诊科室: 页内出现 => page_self；否则用导航上下文补 => 显式 nav_context；皆无 => None
    dept = ctx.get('dept_name')
    ev = next((t for t in texts if dept and dept in t), None) if dept else None
    if ev is not None:
        vals['就诊科室'] = dept
        prov['就诊科室'] = _prov(dept, 'page_self', ev)
    elif dept:
        vals['就诊科室'] = dept
        prov['就诊科室'] = _prov(dept, 'nav_context', None)
    else:
        vals['就诊科室'] = None
        prov['就诊科室'] = _prov(None, None, None)

    # 4) 就诊医院: 同上。诚实性: 页内不存在 => nav_context（显式）或 None，绝不冒充 page_self
    hosp = ctx.get('hospital_name')
    ev = next((t for t in texts if hosp and hosp in t), None) if hosp else None
    if ev is not None:
        vals['就诊医院'] = hosp
        prov['就诊医院'] = _prov(hosp, 'page_self', ev)
    elif hosp:
        vals['就诊医院'] = hosp
        prov['就诊医院'] = _prov(hosp, 'nav_context', None)
    else:
        vals['就诊医院'] = None
        prov['就诊医院'] = _prov(None, None, None)

    # 5) 门诊类型: 优先行级值（时段行块内, S1 实测「专家门诊」在时段行）；否则页内词表（长词优先）
    ot, ev = (outpatient_type, outpatient_type) if outpatient_type else (None, None)
    if ot is None:
        for t in texts:
            for tok in OUTPATIENT_TYPES:
                if tok in t:
                    if ot is None or len(tok) > len(ot):
                        ot, ev = tok, t
                    break
    vals['门诊类型'] = ot
    prov['门诊类型'] = _prov(ot, 'page_self', ev)

    # 6) 挂号费用: 行级 ¥（页内）；否则取页内首个 ¥ 节点；无 => None
    fee_raw_eff = fee_raw
    if fee is None:
        for t in texts:
            m = FEE_RE.match(t)
            if m:
                fee = int(m.group(1))
                fee_raw_eff = t   # S13: 页内扫描命中 => 原文即该节点文本(实测全角 ￥519)
                break
    vals['挂号费用'] = fee
    vals['挂号费用_原文'] = fee_raw_eff if fee is not None else None
    # S13: evidence 必须逐字来自 XML, 不许合成(旧口径 '¥%d' % fee 半角合成串废除)
    prov['挂号费用'] = _prov(fee, 'page_self' if fee is not None else None,
                            fee_raw_eff if fee is not None else None)
    return vals, prov


def build_six_record(s, kind, ctx, row_text=None, fee=None, fee_raw=None, doctor=None,
                     outpatient_type=None, captured_at=None, raw_evidence=None,
                     hospital_id=None, unit=None, doctor_idx=None,
                     row_loc=None, fee_loc=None, type_loc=None, extract_file=None):
    """产出一条六字段记录（明细层号源的最小事实单元）。
    kind = page_kind 结果: 'six'(指纹命中) / 'doc'(医生主页, 5+nav_context 项) / 其他 => error 不猜。
    规则 six_confirm 命中 => 走 extract_confirm(几何配对); 其余 => extract_fields。
    S13 (统一真值交换格式, 与安卓小能手 s14-unified-v1 同 schema):
      * 新增字段 挂号费用_原文 = 费用节点 XML 原串(逐字, 全角 ￥ / 半角 ¥ 以页面为准);
      * 顶层 sub_tab = unit.sub_tab (对账主键第 1 元, 与 unit 内同值冗余);
      * extract / extract_fee / extract_type = {'file', 'text_off'(**字节偏移**, grep -bo 口径),
        'node_bounds'} — 指向 raw XML 中 就诊时间/费用/类型 源文本的逐字位置, 供零别名零容错裁决;
        定位不到 => 不写该键(不猜)。row_loc/fee_loc/type_loc 由调用方按文档序传入
        (费用节点文本每行重复, 禁止用 locate_node 全局首命中兜底)。"""
    rule = match_six_page(s)
    loc_time = loc_fee = None
    if kind in ('six', 'doc') and rule == 'six_confirm':
        vals, prov, locs = extract_confirm(s)
        time_fb = vals.get('就诊时间') is None and bool(row_text)
        if time_fb:
            vals['就诊时间'] = row_text
            prov['就诊时间'] = _prov(row_text, 'page_self', row_text)
        if vals.get('就诊医生') is None and doctor:
            vals['就诊医生'] = doctor
            prov['就诊医生'] = _prov(doctor, 'nav_context', None)
        if vals.get('挂号费用') is None and fee is not None:
            vals['挂号费用'] = fee
            prov['挂号费用'] = _prov(fee, 'nav_context', fee_raw)   # S13: evidence 用原文(逐字), 缺原文才 None
            if vals.get('挂号费用_原文') is None:
                vals['挂号费用_原文'] = fee_raw
        loc_time = row_loc if (time_fb or locs['time'] is None) else locs['time']
        loc_fee = fee_loc or locs['fee']
    elif kind in ('six', 'doc'):
        vals, prov = extract_fields(s, ctx, row_text=row_text, fee=fee, fee_raw=fee_raw,
                                    doctor=doctor, outpatient_type=outpatient_type)
        loc_time = row_loc
        loc_fee = fee_loc
    else:
        vals = {f: None for f in SIX_FIELD_KEYS}
        vals['挂号费用_原文'] = None
        prov = {f: _prov(None, None, None) for f in SIX_FIELD_KEYS}
    # 就诊医生: 在**唯一出口**清洗(见 clean_person_name)。原文进 '就诊医生_原文',
    # provenance 的证据文本不动 —— 事后仍能复核"当时页面上到底写的是什么"。
    _doc_raw = vals.get('就诊医生')
    _doc = clean_person_name(_doc_raw if isinstance(_doc_raw, str) else
                             (_doc_raw or ''))
    if _doc_raw and _doc != _doc_raw:
        vals['就诊医生_原文'] = _doc_raw
    vals['就诊医生'] = _doc or None

    rec = {
        '就诊时间': vals['就诊时间'],
        '就诊医院': vals['就诊医院'],
        '就诊科室': vals['就诊科室'],
        '就诊医生': vals['就诊医生'],
        '就诊医生_原文': vals.get('就诊医生_原文'),   # 页面原串(带职称时留证)
        '门诊类型': vals['门诊类型'],
        '挂号费用': vals['挂号费用'],
        '挂号费用_原文': vals.get('挂号费用_原文'),   # S13: 页面 XML 费用原串逐字
        'fee_cny_raw': vals['挂号费用'],          # 原始价保留
        'fee_tier': fee_tier_from(vals['挂号费用']),
        'source_page': kind,
        'page_fingerprint': fingerprint(s, kind),
        'captured_at': captured_at,               # 设备时钟（调用方传 adb.device_date()）
        'raw_evidence': raw_evidence,             # 该页 UI XML 快照指针
        'provenance': prov,
    }
    if hospital_id:
        rec['hospital_id'] = hospital_id
    if unit:
        rec['unit'] = unit
        if unit.get('sub_tab') is not None:
            rec['sub_tab'] = unit['sub_tab']      # S13: 顶层 sub_tab(真值交换格式)
    if doctor_idx is not None:
        rec['doctor_idx'] = doctor_idx
    # S13: 真值交换定位(字节偏移); 就诊时间行原文每行唯一 => 允许 locate_node 兜底;
    # 费用/类型文本重复 => 仅认调用方传入的文档序定位, 缺则不写(不猜)
    if kind in ('six', 'doc'):
        if loc_time is None and rec['就诊时间']:
            loc_time = locate_node(s, rec['就诊时间'])
        ex_file = extract_file if extract_file else raw_evidence
        if loc_time is not None and loc_time.get('text_off') is not None:
            rec['extract'] = {'file': ex_file, 'text_off': loc_time['text_off'],
                              'node_bounds': loc_time.get('node_bounds')}
        if loc_fee is not None and loc_fee.get('text_off') is not None:
            rec['extract_fee'] = {'file': ex_file, 'text_off': loc_fee['text_off'],
                                  'node_bounds': loc_fee.get('node_bounds')}
        if type_loc is not None and type_loc.get('text_off') is not None:
            rec['extract_type'] = {'file': ex_file, 'text_off': type_loc['text_off'],
                                   'node_bounds': type_loc.get('node_bounds')}
    if kind in ('six', 'doc'):
        rec['status'] = 'ok'
        rec['complete'] = all(rec[f] is not None for f in SIX_FIELD_KEYS)
        rec['error_reason'] = None
    else:
        # 识别不到六字段页 => 报 error，不猜
        rec['status'] = 'error'
        rec['complete'] = False
        rec['error_reason'] = 'page_not_six_candidate(kind=%s): 六字段齐全页指纹未命中, 不猜' % kind
    return rec


# ---------------- raw_evidence / ledger ----------------
RAW_DIRNAME = 'raw'


def save_raw_evidence(last_dump_local, out_dir, hid, dept, doctor, ts):
    """把当前 UI XML 快照落盘为不可变证据文件，返回相对 workspace 的指针。

    last_dump_local 可以是：
      * 文件路径（桌面 pull 路径） → shutil.copyfile
      * XML 文本字符串（端上 devbridge.dump() 返回）→ 直接写盘
    2026-10-28 修复：端上 live 路径从未写 _last_dump.xml，os.path.exists 恒 False，
    shutil.copyfile 被静默跳过，raw/ 目录建了但 0 文件；现在直接接 XML 文本写盘。
    """
    rawdir = os.path.join(out_dir, RAW_DIRNAME)
    os.makedirs(rawdir, exist_ok=True)

    def safe(x):
        return re.sub(r'[^\w\u4e00-\u9fff-]', '_', str(x))[:24]

    fn = '%s_%s_%s_%s.xml' % (hid, safe(dept), safe(doctor),
                              str(ts).replace(':', '').replace('-', '').replace(' ', 'T'))
    dst = os.path.join(rawdir, fn)
    if isinstance(last_dump_local, str) and '<hierarchy' in last_dump_local:
        with open(dst, 'w', encoding='utf-8') as f:
            f.write(last_dump_local)
    elif last_dump_local and os.path.exists(last_dump_local):
        # 边界保护: last_dump_local 可能为 None (devbridge.dump() 返回 None), os.path.exists(None) 会抛 TypeError
        shutil.copyfile(last_dump_local, dst)
    return os.path.relpath(dst, os.path.dirname(out_dir)).replace('\\', '/')


LEDGER_NAME = 'six_fields.jsonl'


def append_ledger(out_dir, rec):
    p = os.path.join(out_dir, LEDGER_NAME)
    with open(p, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')


def load_ledger_for(out_dir, hid, include_errors=False):
    """读回该院六字段记录（抽查比对基准）。按 (unit,医生,就诊时间) 去重，保留最后一条。"""
    p = os.path.join(out_dir, LEDGER_NAME)
    if not os.path.exists(p):
        return []
    seen, recs = {}, []
    with open(p, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get('hospital_id') != hid:
                continue
            if not include_errors and r.get('status') != 'ok':
                continue
            u = r.get('unit') or {}
            key = (u.get('tab'), u.get('dept'), r.get('就诊医生'), r.get('就诊时间'))
            if key in seen:
                recs[seen[key]] = r
            else:
                seen[key] = len(recs)
                recs.append(r)
    return recs
