# -*- coding: utf-8 -*-
"""权重推理（2026-10-04 重构）：唯一 LLM 后端 = Agent Service。

旧三后端处置：
  * --llm rules    → 废除（不再有任何"关键词规则引擎"路径）
  * --llm ollama   → 并入 agentsvc：**契约**采用 ollama 式（模型**直接返回**各特征权重大小
                     + 目标科室），**调用方式**采用 agentsvc 式（llm_client.chat_json,
                     SSE /v1/infer/stream, model_id 默认 'default'）
  * --llm agentsvc → 保留为唯一后端（原"字段抽取+规则映射"职责废除）

基础数据上下文（全部只读, 随 prompt 下发）：
  * 可约科室词表: hospital_intel/dept_index.json（源自 ssb_dept/dept/DEPT_CORE 归一化整理）,
    仅可预约 kind（clinic/specialty_clinic/screening）且 hospital_count>=DEPT_HC_MIN 的
    canonical —— LLM 的科室输出**必须逐字取自该词表**, 由代码对 di 校验（canonical+kind）;
  * 医院目录: stage-1 feature_norms（438 家: 名称/等级/区县）;
  * 场景基线权重表: stage-1 SCENARIOS（5 套专家先验, 供模型参考; 模型输出的是绝对权重向量）。

科室解析链（绝不编造科室）：
  LLM dept_canonicals（逐字校验）→ 有效集为空时, 用 LLM triage_term 走症状词表
  resolve_terms（数据结构解析, 非规则引擎; 含同义词桥）→ 仍无目标科室 → 抛错, 由 main 受控退出。

失败即终止（不静默回退）：Agent Service 不可用 / 输出两次均未通过结构校验
→ llm_client.AgentsvcError（JSON 解析失败由 chat_json 内建重试 1 次;
结构校验失败由本模块纠错重试 1 次）。
"""
import json

from . import llm_client

SCENARIOS = ['community_first', 'chronic_followup', 'complex_severe', 'red_flag', 'peds']
SCENARIO_ZH = {
    'community_first': '社区首诊/轻症',
    'chronic_followup': '慢病复诊',
    'complex_severe': '疑难重症',
    'red_flag': '红旗急症',
    'peds': '儿科',
}

FEATURES = ['match', 'level', 'breadth', 'distance', 'metro', 'bus',
            'key_specialty', 'emergency', 'chronic', 'chronic_fit', 'age_fit', 'price']

FEATURE_NOTES_ZH = {
    'match': '科室命中质量（目标科室层级×置信度: tier1=首选→1.0 / tier2=相关→0.70 / tier3=弱相关→0.45; 0=未命中; 弱相关科室不再与首选平权）',
    'level': '医院等级（专家先验映射: 三甲=1.0 … 一级≈0.15-0.22）',
    'breadth': '科室齐全度（bookable L2 科室数, log 压缩）',
    'distance': '直线距离衰减（1-km/25km, 0~25km）',
    'metro': '地铁可达性（步行分钟/直线距离）',
    'bus': '300m 内公交站台数',
    'key_specialty': '特色科室加分（覆盖稀薄 4.4%, 缺失=0, 不可作主权重）',
    'emergency': '急诊能力（红旗急症场景用: 任一门急诊科室）',
    'chronic': '慢病专病随访能力（慢病场景用: 命中科室含"专病"visit_type）',
    'chronic_fit': '分级诊疗先验（慢病场景: 低等级≤二级且 p1 科室命中 → 社区承接加分）',
    'age_fit': '人群适配（儿科病例三档: 儿童专属机构[儿童医院/儿童医学中心等]=1.0, 有可约儿科类科室(非专属)=0.75, 无=中性0.5——叠加加分非惩罚; 非儿科病例恒1.0）',
    'price': '费用友好度(性价比偏好; 终排只对已核实号源生效: 预算内费档占比越高越好; 红旗急症/疑难重症且费用不敏感时给 0)',
}

# price / age_fit 场景基线（并入 5 套场景基线表供 prompt 参考; 其余静态维按 (1-pw-aw) 等比压缩, 行和仍=1.0）
PRICE_BASELINE = {'community_first': 0.06, 'chronic_followup': 0.05,
                  'complex_severe': 0.03, 'red_flag': 0.02, 'peds': 0.05}
AGE_BASELINE = {'community_first': 0.0, 'chronic_followup': 0.0,
                'complex_severe': 0.0, 'red_flag': 0.0, 'peds': 0.10}


def _scenario_tables_with_price(base_tables):
    out = {}
    for k, v in base_tables.items():
        pw = PRICE_BASELINE.get(k, 0.04)
        aw = AGE_BASELINE.get(k, 0.0)
        out[k] = {f: round(w * (1.0 - pw - aw), 4) for f, w in v.items()}
        out[k]['price'] = round(pw, 4)
        out[k]['age_fit'] = round(aw, 4)
    return out

DEPT_HC_MIN = 5                 # 进入 LLM 科室词表的 hospital_count 门槛
ALLOWED_KINDS = ('clinic', 'specialty_clinic', 'screening')

SYS_JSON_ONLY = '只输出 JSON，不要解释、不要 markdown 代码块'

# 同义词桥: LLM triage_term 为词表外词时 → 词表术语（仅用于 triage_term 解析/交叉核对,
# 不用于科室直选; 词典在 ssb_dept/ 只读, 此处只做入口映射）。
SYNONYMS = {'复视': '斜视', '重影': '斜视'}


def _gender_zh(g):
    return {'male': '男', 'female': '女'}.get(g, '未提供')


# ----------------------------------------------------------------------------
# 基础数据上下文
# ----------------------------------------------------------------------------
def build_context(stage1):
    """从 stage-1 已加载的只读数据组装 LLM 上下文（科室词表 + 医院目录 + 场景基线权重）。"""
    di = stage1.di
    depts = [d for d in di['depts'].values()
             if d.get('kind') in ALLOWED_KINDS and (d.get('hospital_count') or 0) >= DEPT_HC_MIN]
    depts.sort(key=lambda d: -d['hospital_count'])
    by_canon = {d['canonical']: d for d in di['depts'].values()}
    norms = stage1.norms['hospitals']
    hosp = sorted((v['name'], v['level'] or '未定级', v['district'] or '区县未知')
                  for v in norms.values())
    return {
        'depts': depts,
        'by_canon': by_canon,
        'hospitals': hosp,
        'scenario_tables': _scenario_tables_with_price(stage1.sh.SCENARIOS),
        'n_depts': len(depts),
        'n_hospitals': len(hosp),
    }


def _build_prompt(ctx, spec, loc_note):
    p = spec['patient']
    prefs = spec['prefs']
    dept_lines = '\n'.join('%s(%d家)' % (d['canonical'], d['hospital_count']) for d in ctx['depts'])
    hosp_lines = '\n'.join('%s|%s|%s' % (n, l, d) for n, l, d in ctx['hospitals'])
    scen_lines = '\n'.join('%s: %s' % (k, ' '.join('%s=%.2f' % (f, w) for f, w in v.items()))
                           for k, v in ctx['scenario_tables'].items())
    feat_lines = '\n'.join('- %s: %s' % (f, FEATURE_NOTES_ZH[f]) for f in FEATURES)
    supp = ' '.join(x for x in (spec.get('_raw') or [''])[1:] if x).strip() or '(无)'
    fee_expect = prefs.get('fee_expect') or 'auto'
    fee_lab = prefs.get('fee_expect_label') or '自动(从描述解析)'
    return (
        '你是挂号推荐系统的权重推理模块。任务: 根据患者自述+就诊人信息+偏好, '
        '直接给出 特征权重向量 + 目标科室。只输出一个 JSON 对象, 不要多余文本、不要代码块。\n\n'
        '## 基础数据(只读上下文)\n'
        '### 可约科室词表(归一化 canonical, 括号=覆盖医院数; 仅可预约门诊类, 共 %d 条)\n%s\n\n'
        '### 医院目录(名称|等级|区县, 共 %d 家)\n%s\n\n'
        '### 场景基线权重(仅参考; 你输出的是**绝对权重向量**, 不是这些基线本身)\n%s\n\n'
        '## 特征定义(权重向量的 12 个分量; price=费用友好度, age_fit=人群适配, 其余为 stage-1 静态特征)\n%s\n\n'
        '## 患者自述\n[自述] %s\n[交互补充] %s\n\n'
        '## 就诊人信息与偏好(结构化输入; 与自述原文冲突时以自述为准)\n'
        '位置=%s | 就诊人=%s | 年龄=%s | 性别=%s | 年龄段=%s | 代挂号=%s | 医保卡=%s | 就诊人来源=%s\n'
        '就诊类型=%s | 期望等待≤%s天 | 费用上限=%s元 | 预期费用档位=%s(%s) | 水平预期=%s | 地铁偏好=%s\n\n'
        '## 表单已取消的字段 —— 由你从[自述]解析(解析不到才用默认值, 并在 reasoning 里说明)\n'
        '- 时效 max_wait_days: 自述里的"立刻/今天/尽快/明天/一周内/不着急"等 → 0/1/3/7/14…; '
        '上面"期望等待≤N天"若为默认 7 而自述另有说法, 以自述为准\n'
        '- 就诊类型: "第一次看/没看过/初次"→new, "复诊/配药/看报告/随访"→followup; '
        '上面=unknown 时必须自己从自述判断\n'
        '- 水平预期: "想去大医院/三甲/权威/专家"→top, "社区/就近/常见病/随便看看"→community, '
        '未提→balanced; 上面=balanced 而自述另有说法时以自述为准\n'
        '- 预期费用档位=%s(%s): 用户显式拉的尺度条(非 auto 时已覆盖费用上限), 按它调**医院类型**与 price 权重: '
        '0元/10元→社区/一级+普通门诊为主(price 0.10~0.25); 百元→普通门诊为主(price 0.05~0.15); '
        '千元→可接受专家门诊/二级以上(price≈0.03); 万元→可接受特需/国际医疗(price≈0); '
        '不限→费用不敏感(price=0); 自动→从自述解析费用线索并自行给 price_cap_cny\n\n'
        '## 输出 JSON 字段\n'
        '- scenario: %s 之一\n'
        '- weights: {match, level, breadth, distance, metro, bus, key_specialty, emergency, chronic, chronic_fit, age_fit, price} '
        '绝对权重(每个 0~1, 行和≈1.0; 与场景无关的特征给 0; 按水平预期/地铁偏好/时效/费用期望调整; '
        'price=费用友好度: 用户设定明确费用上限或强调性价比时给较高值(如 0.05~0.2), '
        '红旗急症/疑难重症且描述里费用不敏感时给 0; '
        'age_fit=人群适配: 儿科病例想优先"有儿科科室/儿童机构"的医院时给 0.05~0.15; '
        '患者明确表达偏好儿童专属医院(如"想去儿童医院"/"倾向儿童专属")时给 0.20~0.30(一级权重, 与 match 同量级); 非儿科病例给 0)\n'
        '- dept_canonicals: 目标科室数组(1~5 个, 按优先级排列); 每项为对象 {"dept": canonical名, "tier": 层级}\n'
        '    tier 语义: 1=首选(症状直接指向该科室, 如咽喉不适→耳鼻喉科), 2=相关(也能看该症状/常见首诊), 3=弱相关(邻近专科或保守治疗后备)\n'
        '    **dept 必须逐字取自上面的可约科室词表**, 不可自造或改写; 不同科室的 tier 必须拉开(首选与弱相关不可同为 1)\n'
        '    儿科患者: 症状专科(如耳鼻喉科)才是 tier=1; "儿科/小儿内科/儿童保健科"等纯人群归属科室至多 tier=2'
        '（人群维度由 age_fit 特征单独承担; 仅当症状本身是儿科专科问题, 如生长发育/营养不良, 才把儿科标 tier=1）\n'
        '- triage_term: 最能代表主诉的单个症状词(可用自述原词)\n'
        '- max_wait_days: int(0=今日必须)\n'
        '- price_cap_cny: int 或 null\n'
        '- reasoning: 中文数组, ≤3 条(解释急/重/缓、交通、费用、水平预期如何影响权重与科室选择)\n'
    ) % (ctx['n_depts'], dept_lines, ctx['n_hospitals'], hosp_lines, scen_lines, feat_lines,
         spec.get('symptom_text') or '(无)', supp,
         loc_note or '(未提供)',
         p.get('name_masked') or '(未选就诊人)',
         p.get('age') if p.get('age') is not None else '未提供',
         _gender_zh(p.get('gender')), p.get('age_band'), p.get('proxy'),
         ('有' if p.get('insurance_card') else ('无' if 'insurance_card' in p else '未提供')),
         p.get('patient_source') or '(非就诊人列表)',
         spec.get('visit_type'), spec.get('urgency_days'),
         prefs.get('price_cap_cny') if prefs.get('price_cap_cny') else '无',
         fee_expect, fee_lab,
         prefs.get('level_expectation'), prefs.get('metro_priority'),
         fee_expect, fee_lab,
         SCENARIOS)


# ----------------------------------------------------------------------------
# 输出校验
# ----------------------------------------------------------------------------
def _validate(obj, ctx):
    """校验 LLM JSON → 字段 + errors（errors 非空则触发纠错重试; 词表外科室名单独记录）。"""
    if not isinstance(obj, dict):
        return None, ['顶层不是 JSON 对象']
    errs = []
    sc = obj.get('scenario')
    if sc not in SCENARIOS:
        errs.append('scenario 非法: %r（应为 %s 之一）' % (sc, SCENARIOS))
        sc = None

    w_in = obj.get('weights')
    weights = {}
    if isinstance(w_in, dict):
        for f in FEATURES:
            v = w_in.get(f)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                weights[f] = float(v)
    if not weights:
        errs.append('weights 缺失或全部非正数')
    tot = sum(weights.values())
    if tot > 0:
        weights = {k: v / tot for k, v in weights.items()}

    raw = obj.get('dept_canonicals')
    if isinstance(raw, str):
        raw = [raw]
    depts, invalid = [], []
    if isinstance(raw, list):
        seen = set()
        for item in raw[:5]:
            if isinstance(item, dict):
                name = item.get('dept') or item.get('canonical')
                tier = item.get('tier', 1)
            elif isinstance(item, (list, tuple)) and item:
                name, tier = item[0], (item[1] if len(item) > 1 else 1)
            else:
                name, tier = item, 1
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()
            try:
                tier = int(tier)
            except (TypeError, ValueError):
                tier = 1
            tier = max(1, min(3, tier))
            d = ctx['by_canon'].get(name)
            if d is not None and name not in seen:
                seen.add(name)
                depts.append({'canonical': name, 'kind': d['kind'], 'tier': tier,
                              'hospital_count': d.get('hospital_count') or 0})
            elif name not in invalid:
                invalid.append(name)
    if not depts:
        errs.append('dept_canonicals 无有效条目%s（必须逐字取自可约科室词表）'
                    % ('，词表外名称: %s' % invalid if invalid else '（缺失或为空）'))

    term = obj.get('triage_term')
    term = term.strip()[:16] if isinstance(term, str) and term.strip() else None

    mw = obj.get('max_wait_days')
    if isinstance(mw, bool) or not isinstance(mw, (int, float)) or not (0 <= mw <= 90):
        mw = None
    else:
        mw = int(mw)
    pc = obj.get('price_cap_cny')
    if isinstance(pc, bool) or not isinstance(pc, (int, float)) or not (0 < pc <= 100000):
        pc = None
    else:
        pc = int(pc)

    rr = obj.get('reasoning')
    if isinstance(rr, str):
        rr = [rr]
    reasoning = []
    if isinstance(rr, list):
        for r in rr[:5]:
            if isinstance(r, str) and r.strip():
                reasoning.append(r.strip()[:120])
    return {'scenario': sc, 'weights': weights, 'depts': depts, 'invalid_depts': invalid,
            'triage_term': term, 'max_wait_days': mw, 'price_cap_cny': pc,
            'reasoning': reasoning}, errs


def _depts_from_term(term, stage1):
    """LLM triage_term → 症状词表 resolve_terms（含同义词桥）→ 归一化科室列表。
    这是数据结构解析（词表是离线整理好的 symptom→dept 映射）, 不是规则推理引擎。"""
    if not term:
        return []
    terms = stage1.lex['terms']
    for syn, target in SYNONYMS.items():
        if syn in term and target in terms and target not in term:
            term = target
            break
    t, _how, cands = stage1.sh.resolve_terms(term, terms)
    if t is None:
        return []
    out = []
    for c in cands:
        d = stage1.di['depts'].get(c['dept_id'])
        if not d or d['kind'] not in ALLOWED_KINDS:
            continue
        if any(x['canonical'] == d['canonical'] for x in out):
            continue
        out.append({'canonical': d['canonical'], 'kind': d['kind'],
                    'hospital_count': d.get('hospital_count') or 0})
    return out


def _cross_check(term, depts, stage1):
    """triage_term × 症状词表交叉核对（仅标注, 不自动合并; 科室以 LLM 直选为准）。"""
    if not term:
        return []
    t, _how, cands = stage1.sh.resolve_terms(term, stage1.lex['terms'])
    if t is None or not cands:
        return ['triage_term=%r 症状词表无命中（不影响: 科室以 LLM 直选为准）' % term]
    lex_canon = [c['dept_canonical'] for c in cands[:3]]
    llm_set = {d['canonical'] for d in depts}
    hit = [c for c in lex_canon if c in llm_set]
    if hit:
        return ['triage_term=%r 词表交叉核对: 与 LLM 科室一致（%s）' % (term, ', '.join(hit[:3]))]
    return ['triage_term=%r 词表交叉核对: 词表指向 %s, LLM 选了 %s — 以 LLM 为准, 词表仅供参考'
            % (term, '/'.join(lex_canon[:3]), '/'.join(sorted(llm_set)[:3]))]


# ----------------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------------
def infer_llm(spec, stage1, loc_note=None, model='default', base=None):
    """唯一推理路径: LLM（Agent Service）直出 特征权重向量 + 目标科室 + 约束。

    返回 inf dict（下游 recommend/main 契约）:
      scenario/scenario_zh/weights(绝对, 行和=1.0)/max_wait_days/price_cap_cny/
      triage_term/llm_depts/llm_dept_tiers/dept_how/reasoning/llm_mode/llm_meta/context/fallback
    失败抛 llm_client.AgentsvcError（含 LLMJsonParseError）—— 不静默回退。
    """
    ctx = build_context(stage1)
    prompt = _build_prompt(ctx, spec, loc_note)
    v, meta, fatal = None, None, None
    for attempt in (1, 2):
        p = prompt
        if fatal:
            p += ('\n\n你上一次输出未通过校验: %s。请重新输出一个 JSON 对象: '
                  'dept_canonicals 为 [{"dept": canonical名, "tier": 1|2|3}] 数组, dept 必须逐字取自"可约科室词表"（不要自造/改写科室名）, '
                  'weights 行和≈1.0, 不要任何解释。' % '; '.join(fatal[:3]))
        obj, meta = llm_client.chat_json(SYS_JSON_ONLY, p, model_id=model, base=base)
        v, errs = _validate(obj, ctx)
        fatal = errs
        if not fatal:
            break
    else:
        raise llm_client.LLMJsonParseError(
            'LLM 输出两次均未通过结构校验: %s' % '; '.join(fatal[:4]),
            raw=meta.get('content', '')[:500] if meta else None)

    reasoning = list(v['reasoning'])
    depts = v['depts']
    how = 'llm'
    if v['invalid_depts']:
        reasoning.append('LLM 给出词表外科室名（已忽略, 仅用词表内条目）: %s' % v['invalid_depts'])
    if not depts:
        # 词表直选为空 → LLM triage_term 走症状词表（数据结构解析）
        depts = _depts_from_term(v['triage_term'] or spec.get('symptom_text'), stage1)
        how = 'llm_term_lexicon'
        if depts:
            reasoning.append('LLM dept_canonicals 未通过校验 → 用 LLM triage_term=%r '
                             '经症状词表解析出科室 %d 个（归一化 D-ID）'
                             % (v['triage_term'] or spec.get('symptom_text'), len(depts)))
        else:
            raise RuntimeError(
                'LLM 未给出有效目标科室, 且 triage_term=%r 在症状词表无命中 — '
                '请用更具体的症状描述（如"胸痛""高血压复诊""孩子发烧"）重跑'
                % (v['triage_term'] or '(缺省)'))
    reasoning.extend(_cross_check(v['triage_term'] or spec.get('symptom_text'), depts, stage1))

    term = v['triage_term'] or depts[0]['canonical']

    max_wait = v['max_wait_days']
    if max_wait is None:
        max_wait = 0 if v['scenario'] == 'red_flag' else int(spec['urgency_days'])
    if v['scenario'] == 'red_flag' and max_wait > 0:
        reasoning.append('red_flag 强制 max_wait_days=0（须今日就诊, 安全优先）')
        max_wait = 0
    prefs = spec['prefs']
    fee_expect = prefs.get('fee_expect') or 'auto'
    if fee_expect != 'auto':
        # 用户显式拉的「预期费用尺度条」优先于 LLM 从文本解析的费用上限;
        # 0元/不限 两档不设数值硬上限（0 元靠权重把医院类型压到社区/普诊, 不限则 price≈0）
        price_cap = prefs.get('fee_expect_cny')
        if v['price_cap_cny'] is not None and v['price_cap_cny'] != price_cap:
            reasoning.append('预期费用档位=%s(%s) → 覆盖 LLM 从描述解析的费用上限 %s → %s'
                             % (fee_expect, prefs.get('fee_expect_label') or fee_expect,
                                v['price_cap_cny'],
                                price_cap if price_cap is not None else '不设硬上限'))
    else:
        price_cap = v['price_cap_cny'] if v['price_cap_cny'] is not None else prefs['price_cap_cny']

    return {
        'scenario': v['scenario'],
        'scenario_zh': SCENARIO_ZH[v['scenario']],
        'weights': weights_round(v['weights']),
        'weight_overrides': {},  # 兼容字段: 新契约下 LLM 直出绝对权重, 无乘子
        'max_wait_days': max_wait,
        'price_cap_cny': price_cap,
        'fee_expect': fee_expect,
        'fee_expect_label': prefs.get('fee_expect_label') or '自动(从描述解析)',
        'triage_term': term,
        'triage_hits': [],
        'llm_depts': [d['canonical'] for d in depts],
        'llm_dept_tiers': {d['canonical']: d.get('tier', 1) for d in depts},
        'llm_dept_kinds': {d['canonical']: d['kind'] for d in depts},
        'dept_how': how,
        'reasoning': reasoning,
        'llm_mode': 'agentsvc(model_id=%s; 直出权重向量+目标科室, 含基础数据上下文)' % model,
        'llm_meta': {'provider': 'agentsvc', 'model_id': model,
                     'base': base or llm_client.resolve_base_url(),
                     'elapsed_ms': meta.get('elapsed_ms'),
                     'first_byte_ms': meta.get('first_byte_ms'),
                     'attempts': meta.get('attempts'),
                     'thinking_chars': meta.get('thinking_chars'),
                     'session_id': meta.get('session_id'),
                     'n_data': meta.get('n_data'),
                     'prompt_chars': len(prompt)},
        'fallback': False,
        'context': {'n_depts': ctx['n_depts'], 'n_hospitals': ctx['n_hospitals'],
                    'dept_hc_min': DEPT_HC_MIN},
    }


def weights_round(w):
    return {k: round(v, 4) for k, v in w.items()}
