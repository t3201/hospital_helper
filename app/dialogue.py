# -*- coding: utf-8 -*-
"""病情采集：交互式自由文本问答（最少必要轮次, 目标 ≤3 轮）→ 结构化 CaseSpec。
demo 模式用 fixtures/demo_cases.json 的脚本答案走同一解析路径（解析逻辑不分支）。"""
import json
import os
import re
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEMO_FILE = os.path.join(APP_DIR, 'fixtures', 'demo_cases.json')

Q1 = '请描述病情（症状、持续时间、伴随表现），自由文本：'
Q2 = '患者情况（可一句说完）：给谁看（成人/儿童/老人/代挂, 性别）？初诊还是复诊？想多久内看上（立刻/三天内/一周内/两周内/不急）？'


def _gender(text):
    """性别解析（保守: 只认明确的性别表述; 女儿/女士等称呼不判）"""
    if re.search(r'女性|女的|女患者|女[,，、]\s*\d{1,3}(?:\s*岁)?|\d{1,3}\s*岁[,，、]?\s*女|女[,，、]?\s*(?:初诊|复诊|看病|就诊)', text):
        return 'female'
    if re.search(r'男性|男的|男患者|男[,，、]\s*\d{1,3}(?:\s*岁)?|\d{1,3}\s*岁[,，、]?\s*男|男[,，、]?\s*(?:初诊|复诊|看病|就诊)', text):
        return 'male'
    return None

Q3 = '交通与费用（可跳过=默认）：地铁优先吗？费用预期（如“50元以内”）？诊疗水平预期（顶级大医院/均衡/社区就近即可）？'


def _age(text):
    m = re.search(r'(\d{1,3})\s*岁', text)
    if m:
        return int(m.group(1))
    return None


# 儿童/老年人群关键词(与 parse_case 原内联正则一致; spec 结构化路径共用, 2026-10-06)
CHILD_KW = re.compile(r'儿童|幼儿|小孩|宝宝|孩子|新生儿|婴儿|儿子|女儿|青少年|个月')
ELDER_KW = re.compile(r'老人|老年|老爷子|老太太|爸妈|父母|爷爷奶奶')

# 预期费用尺度条（web 表单, 2026-10-08）：档位 key -> (中文标签, 数值硬上限 None=不设硬上限)。
# 档位是用户显式意图：非 auto 时覆盖「从描述解析」的费用上限, 并把档位原样交给大模型
# 去调医院类型（0元/10元 → 社区/一级与普诊; 千元/万元 → 可接受特需/国际/高端; 不限 → 费用不敏感）。
FEE_EXPECT_TIERS = [
    ('auto',     '自动(从描述解析)', None),
    ('zero',     '0元(免费/最低)',   None),
    ('ten',      '10元',            10),
    ('hundred',  '百元',            100),
    ('thousand', '千元',            1000),
    ('tenk',     '万元',            10000),
    ('any',      '不限',            None),
]
FEE_EXPECT = {k: (lab, cap) for k, lab, cap in FEE_EXPECT_TIERS}


def _band_from_text(full):
    """自由文本 -> (band, note): 有年龄按年龄; 无年龄按儿童/老年关键词; 都没有 -> (None, None) 由调用方定默认。"""
    age = _age(full)
    if age is not None:
        band = 'child' if age < 18 else ('elderly' if age >= 65 else 'adult')
        return band, '年龄=%d → %s' % (age, band)
    if CHILD_KW.search(full):
        return 'child', '儿童关键词 → child'
    if ELDER_KW.search(full):
        return 'elderly', '老年关键词 → elderly'
    return None, None


def _urgency_days(text):  # noqa
    if re.search(r'立刻|马上|现在|尽快|紧急|今天', text):
        return 0
    if re.search(r'三天|3天|三日', text):
        return 3
    if re.search(r'一周|这周|7天|七天', text):
        return 7
    if re.search(r'两周|14天|十四天|半个月内|两周内', text):
        return 14
    if re.search(r'不急|慢慢|可以等', text):
        return 14
    return None


def _price_cap(text):
    m = re.search(r'(\d+)\s*元以内|不超过\s*(\d+)\s*元|(\d+)\s*元以下|(\d+)\s*元(?!以?)', text)
    if m:
        v = next(g for g in m.groups() if g)
        return int(v)
    if '百元以内' in text or '一百元以内' in text:
        return 100
    return None


def parse_case(answers):
    """answers: [r1, r2?, r3?] → CaseSpec（含解析 notes, 用于数据可信度标注）。"""
    r1 = (answers[0] or '').strip()
    r2 = (answers[1] or '').strip() if len(answers) > 1 else ''
    r3 = (answers[2] or '').strip() if len(answers) > 2 else ''
    full = ' '.join(x for x in (r1, r2, r3) if x)
    notes = []

    age = _age(full)
    band, _bn = _band_from_text(full)
    if band is None:
        band, _bn = 'adult', '未提及年龄 → 默认 adult'
    notes.append(_bn)

    proxy = bool(re.search(r'代挂|代约|帮我|帮家人|给我|替(我)?|家属|家长', full))
    if proxy:
        notes.append('代挂号信号 → proxy=True（位置由 --loc 人为指定的主场景）')

    visit = None
    if re.search(r'复诊|开药|随访|长期|老毛病|控制稳定|一直在吃|定期', full):
        visit = 'followup'
    elif re.search(r'初诊|第一次|头一回|新发', full):
        visit = 'new'
    if visit is None:
        visit = 'unknown'
        notes.append('未提及就诊类型 → unknown（由规则推断）')
    else:
        notes.append('visit=%s' % visit)

    urg = _urgency_days(full)
    if urg is None:
        urg = 7
        notes.append('未提及时效 → 默认 max_wait_days=7')
    else:
        notes.append('时效 → max_wait_days=%d' % urg)

    prefs = {'metro_priority': False, 'price_cap_cny': None, 'level_expectation': 'balanced'}
    if re.search(r'地铁|轨道交通', full):
        prefs['metro_priority'] = True
        notes.append('地铁优先 → weight_overrides.metro×1.6')
    cap = _price_cap(full)
    if cap:
        prefs['price_cap_cny'] = cap
        notes.append('费用预期 → price_cap_cny=%d' % cap)
    if re.search(r'顶级|最好|大医院|三甲|权威|高水平', full):
        prefs['level_expectation'] = 'top'
        notes.append('诊疗水平预期=top → weight_overrides.level×1.5')
    elif re.search(r'社区|就近|附近|便宜|离家近|方便的', full):
        prefs['level_expectation'] = 'community'
        notes.append('诊疗水平预期=community → weight_overrides.level×0.6')
    else:
        notes.append('水平预期未提及 → 默认 balanced（不改等级权重）')

    gender = _gender(full)
    if gender:
        notes.append('性别=%s' % gender)

    return {
        'symptom_text': r1,
        'patient': {'age': age, 'age_band': band, 'is_child': band == 'child', 'proxy': proxy,
                    'gender': gender},
        'urgency_days': urg,
        'visit_type': visit,
        'prefs': prefs,
        'notes': notes,
        '_raw': [r1, r2, r3],
    }


def spec_from_dict(d):
    """结构化字段（web/表单入口, 如 --spec-file）→ CaseSpec, 与 parse_case 同 schema。

    d: {symptom_text, age, gender(male/female/None), urgency_days, visit_type,
        metro_priority, price_cap_cny, level_expectation, proxy, age_band?,
        patient?(随申办就诊人 dict: name_masked/age/gender/gender_source/insurance_card),
        fee_expect?(预期费用档位 auto|zero|ten|hundred|thousand|tenk|any)}

    web 简化表单（2026-10-08）只送 symptom_text / patient / fee_expect（+位置）：
    时效、就诊类型、水平预期不再由表单给, 由 LLM 从症状描述里解析（infer.infer_weights）。
    """
    notes = ['病例来源: 显式结构化字段 (web/表单), 非自由文本解析']
    sym = (d.get('symptom_text') or '').strip()
    if not sym:
        raise SystemExit('symptom_text 不能为空')
    try:
        age = int(d['age']) if d.get('age') not in (None, '') else None
    except (TypeError, ValueError):
        age = None
    band = d.get('age_band')
    if band not in ('child', 'adult', 'elderly'):
        if age is not None:
            band = 'child' if age < 18 else ('elderly' if age >= 65 else 'adult')
        else:
            _b, _bn = _band_from_text(sym)
            if _b:
                band = _b
                notes.append('年龄未提, 症状文本推断: %s' % _bn)
            else:
                band = 'adult'
    gender = d.get('gender')
    if gender not in ('male', 'female'):
        gender = None
    proxy = bool(d.get('proxy', False))
    if proxy:
        notes.append('代挂号信号 → proxy=True（位置由 --loc 人为指定的主场景）')
    patient = {}
    pat = d.get('patient')
    if isinstance(pat, dict) and pat:      # 随申办「就诊人」列表里选的人（页面本身脱敏, 不还原隐私）
        if pat.get('age') not in (None, ''):
            age = int(pat['age'])
            band = 'child' if age < 18 else ('elderly' if age >= 65 else 'adult')
        if pat.get('gender') in ('male', 'female'):
            gender = pat['gender']
        patient = {
            'name_masked': pat.get('name_masked'),
            'insurance_card': bool(pat.get('insurance_card')),
            'gender_source': pat.get('gender_source'),
            'patient_source': pat.get('source') or 'ssb_app/预约挂号/就诊人',
        }
        proxy = bool(pat.get('proxy', False))
        notes.append('就诊人=%s(%s岁, %s, 医保卡=%s) 来源=%s; 性别依据=%s'
                     % (patient['name_masked'] or '未命名', age if age is not None else '?',
                        {'male': '男', 'female': '女'}.get(gender, '性别未定'),
                        '有' if patient['insurance_card'] else '无',
                        patient['patient_source'], patient['gender_source'] or '—'))
    elif d.get('patient_selected') is False:   # 表单选了「不选择项」= 为名单外的人挂号
        proxy = True
        notes.append('未选择就诊人（下拉框第一项）→ proxy=True：为名单外的人挂号；'
                     '年龄/性别由大模型从症状描述解析, 位置仍单独设定（与是否代挂号无关）')
    notes.append('年龄=%s → %s' % (age if age is not None else '未提', band))
    if gender:
        notes.append('性别=%s' % gender)
    visit = d.get('visit_type')
    if visit not in ('new', 'followup', 'unknown'):
        visit = 'unknown'
    try:
        urg = int(d['urgency_days']) if d.get('urgency_days') not in (None, '') else 7
    except (TypeError, ValueError):
        urg = 7
    notes.append('时效 → max_wait_days=%d%s' % (urg, '（表单未给, 默认值; 由大模型从描述改写）'
                                              if d.get('urgency_days') in (None, '') else ''))
    prefs = {'metro_priority': bool(d.get('metro_priority', False)), 'price_cap_cny': None,
             'level_expectation': 'balanced'}
    if prefs['metro_priority']:
        notes.append('地铁优先')
    cap = d.get('price_cap_cny')
    if cap not in (None, ''):
        prefs['price_cap_cny'] = int(cap)
        notes.append('费用预期 → price_cap_cny=%d' % prefs['price_cap_cny'])
    fee = d.get('fee_expect')
    if fee not in FEE_EXPECT:
        fee = 'auto'
    fee_lab, fee_cap = FEE_EXPECT[fee]
    prefs['fee_expect'] = fee
    prefs['fee_expect_label'] = fee_lab
    prefs['fee_expect_cny'] = fee_cap
    if fee != 'auto':
        prefs['price_cap_cny'] = fee_cap      # 尺度条是用户显式意图 → 覆盖文本/LLM 的费用上限
        notes.append('预期费用档位=%s（%s）→ 覆盖文本解析的费用上限(price_cap_cny=%s)'
                     % (fee, fee_lab, fee_cap if fee_cap is not None else '不设硬上限'))
    else:
        notes.append('预期费用档位=自动 → 费用上限由大模型从症状描述解析')
    lev = d.get('level_expectation')
    if lev in ('top', 'community'):
        prefs['level_expectation'] = lev
        notes.append('诊疗水平预期=%s' % lev)
    else:
        notes.append('诊疗水平预期未给 → balanced（由大模型从症状描述改写）')
    pat_out = {'age': age, 'age_band': band, 'is_child': band == 'child', 'proxy': proxy,
               'gender': gender}
    pat_out.update(patient)          # name_masked / insurance_card / gender_source / patient_source
    return {
        'symptom_text': sym,
        'patient': pat_out,
        'urgency_days': urg,
        'visit_type': visit,
        'prefs': prefs,
        'notes': notes,
        '_raw': [sym, '', ''],
    }


def load_demo(case_id):

    data = json.load(open(DEMO_FILE, encoding='utf-8'))
    if case_id not in data:
        raise SystemExit('demo 用例不存在: %s（可选 %s）' % (case_id, ', '.join(data)))
    return data[case_id]


def _is_tty():
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def _ask(prompt):
    """TTY 提问; stdin 已关闭（如 < NUL, 某些 harness 的伪 TTY）时 EOFError → 视为"回车"（空答案）。"""
    try:
        return input(prompt)
    except EOFError:
        print()
        return ''


def run_interactive(prefill=None, non_interactive=False):
    """≤3 轮自由文本 → CaseSpec。
    非交互保护（B3/B4）: stdin 非 TTY（管道/CI/被 agent 调用）或显式 --non-interactive 时,
    绝不调用 input()（避免 EOFError 裸崩溃）: 有 --symptom 则用默认值补全其余轮次;
    无 --symptom 则给出可操作提示并以非零码退出。
    stdin=NUL（伪 TTY 下 isatty 可能仍为真）: EOFError 按"空答案"处理, 流水线继续。"""
    print('\n=== 病情采集（自由文本, 最多 3 轮）===')
    interactive = (not non_interactive) and _is_tty()
    if interactive:
        r1 = (prefill or _ask(Q1)).strip()
        if not r1:
            raise SystemExit('症状不能为空')
        r2 = _ask(Q2).strip()
        r3 = _ask(Q3 + '（直接回车=默认）').strip()
    else:
        if not prefill:
            raise SystemExit(
                '【非交互】stdin 不是 TTY（管道/CI/agent 调用）且未提供 --symptom, 不能发起追问。\n'
                '可操作选项: ① 加 --symptom "症状描述"（如 --symptom "胸痛"）; '
                '② 加 --non-interactive 且提供 --symptom; ③ 用 --demo red_flag/chronic/peds 跑验收用例。')
        r1, r2, r3 = prefill.strip(), '', ''
        print('   （非交互模式: 跳过 Q2/Q3 追问, 患者/时效/交通费用均取默认值; '
              '如需修改请改用 TTY 交互或补充 --symptom 文本）')
    return parse_case([r1, r2, r3])
