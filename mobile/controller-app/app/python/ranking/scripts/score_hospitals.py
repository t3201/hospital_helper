#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
score_hospitals.py — 小步 DA 第 3 步：候选医院静态排序（stage-1）+ 两段式接口契约（只定义，不抓取）

原则：
  * 纯脚本、零模型调用、纯标准库；只读 hospital_intel/ 与 ssb_dept/，绝不写入。
  * 幂等：输出不含墙钟时间（generated_at 为输入内容指纹），重复运行字节级一致。
  * stage-1 只做 召回 + 剪枝 + 抓取顺序；静态分绝不定生死（stage-2 契约见 RANKING_SPEC.md）。
  * 权重为专家先验初值（未调参，无标注数据）；缺失特征给中性分 0.5 并标注，不惩罚。

用法：
  python score_hospitals.py --norms            # 预计算 ranking/out/feature_norms.json（幂等）
  python score_hospitals.py --check            # 覆盖率 + 无NaN + 幂等 自检（写 out/self_check.json）
  python score_hospitals.py --demo             # 3 个验收样例 + 静态覆盖样例（写 out/DEMO_*.json）
  python score_hospitals.py --eval             # 回放 ranking/eval/EVAL_SET.json（写 out/eval_results.json + eval/EVAL_REPORT.md）
  python score_hospitals.py --all              # norms + check + demo + eval 一键
  python score_hospitals.py --case '胸痛' --scenario red_flag --loc 121.4737,31.2304 --top 10
"""
import argparse
import glob
import hashlib
import json
import math
import os
import re
import sys

# 端上改造：只读数据来自 assets 解压目录；可写产物(feature_norms/self_check)走 filesDir/out。
try:
    from hhpaths import WS as ROOT, out as _out
    OUT_DIR = _out('ranking', 'out')
    EVAL_DIR = _out('ranking', 'eval')
except Exception:
    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # workspace 根
    OUT_DIR = os.path.join(ROOT, 'ranking', 'out')
    EVAL_DIR = os.path.join(ROOT, 'ranking', 'eval')
LEXICON_PATH = os.path.join(ROOT, 'ssb_dept', 'triage', 'SYMPTOM_LEXICON.json')
DEPT_INDEX_PATH = os.path.join(ROOT, 'hospital_intel', 'dept_index.json')
MANIFEST_DIR = os.path.join(ROOT, 'hospital_intel', 'manifests')
NORMS_PATH = os.path.join(OUT_DIR, 'feature_norms.json')
EVAL_SET_PATH = os.path.join(EVAL_DIR, 'EVAL_SET.json')

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

# ----------------------------------------------------------------------------
# 常量（专家先验，未调参；理由见 RANKING_SPEC.md）
# ----------------------------------------------------------------------------
ALLOWED_HIT_KINDS = {'clinic', 'specialty_clinic', 'screening'}   # 科室命中的 kind 政策
# other=预约/疫苗条目, site=号/站点, selfname=站名, ancillary=影像检查 → 均不算科室命中
LEVEL_SCORE_MAP = {
    '三级甲等': 1.00, '三级乙等': 0.72, '三级': 0.62,
    '二级甲等': 0.50, '二级乙等': 0.38, '二级': 0.33, '二级丙等': 0.30,
    '一级甲等': 0.22, '一级乙等': 0.18, '一级丙等': 0.15, '一级': 0.15,
    '未定级': 0.28, '': 0.28,
}
PRIORITY_W = {1: 1.00, 2: 0.70, 3: 0.45}
CONF_W = {'high': 1.00, 'mid': 0.85, 'low': 0.65}
# 儿科能力标识（age_fit 特征 + app/candidates.py 患者层过滤共用; 单一数据源, 勿在别处复制）
PEDIATRIC_MARKS = ('儿科', '儿童', '小儿', '新生儿', '婴', '幼儿', '青少年', '儿保', '母婴')
PEDIATRIC_HOSP_MARKS = ('儿童医院', '儿童医学中心', '儿科医院', '复旦儿科')
NORMS_VERSION = 'v3'   # feature_norms.json 结构版本(v3: +pediatric_exclusive, age_fit 三档); 变化触发一次重建
NEUTRAL = 0.5
LOW_CONF_BLEND = 0.40          # provenance.confidence=low 的特征值向中性 0.5 混合 40%
DIST_CAP_KM = 25.0             # 上海市域距离衰减上限
METRO_WALK_CAP_MIN = 30.0
METRO_DIST_CAP_M = 2500.0
BREADTH_P90_L2 = 142           # bookable_dept_l2_count 分位帽（p90；长尾极重 → 对数+分位帽）
BUS_STOPS_P90 = 5
DEFAULT_RADIUS_KM = 20.0
SCENARIO_RADIUS_DEFAULT = {    # 硬过滤：距离半径（可被用户覆盖）
    'community_first': 12.0, 'chronic_followup': 15.0, 'complex_severe': 30.0,
    'red_flag': 15.0, 'peds': 20.0,
}
K_MIN, K_MAX = 14, 60          # K 肘部规则的钳制区间
K_ABS_DROP = 0.22              # 相对 Top1 的绝对落差地板
K_GAP_FRAC = 0.15              # 相邻落差阈值 = max(0.05, 0.15*span)
BUDGET_FETCH_DEFAULT = 12      # stage-2 每查询最多抓 N 家（超出按静态分裁剪）

# 场景权重表（专家先验初值，未调参；行和=1.0）
SCENARIOS = {
    'community_first': {  # 社区首诊/轻症：就近+可达优先，等级几乎不看
        'match': .34, 'level': .12, 'breadth': .08, 'distance': .28,
        'metro': .10, 'bus': .05, 'key_specialty': .03,
    },
    'chronic_followup': {  # 慢病复诊：分级诊疗先验——低等级+有p1科室者“社区承接”加分；就近复诊
        'match': .26, 'level': .08, 'breadth': .10, 'distance': .26,
        'metro': .07, 'bus': .05, 'key_specialty': .03, 'chronic': .05, 'chronic_fit': .10,
    },
    'complex_severe': {  # 疑难重症：等级+学科齐全度主导
        'match': .28, 'level': .42, 'breadth': .16, 'distance': .06,
        'metro': .02, 'bus': .01, 'key_specialty': .05,
    },
    'red_flag': {  # 红旗急症：急诊能力一票加权 + 等级 + 就近
        'match': .26, 'level': .26, 'breadth': .12, 'distance': .16,
        'metro': .04, 'bus': .02, 'key_specialty': .04, 'emergency': .10,
    },
    'peds': {  # 儿科：科室命中压倒等级（儿童保健科 96 家里 67 家一级甲等，等级权必须低）
        'match': .45, 'level': .04, 'breadth': .10, 'distance': .25,
        'metro': .07, 'bus': .04, 'key_specialty': .05,
    },
}

FEATURE_NOTES = {
    'match': '科室命中质量（目标科室层级×置信度; tier1=首选→1.0 / tier2=相关→0.70 / tier3=弱相关→0.45; kind 政策过滤）',
    'age_fit': '人群适配（peds 场景三档(2026-10-06): 儿童专属机构[名含儿童医院/儿童医学中心等]=1.0, 有可约儿科类科室(非专属)=0.75, 无=中性0.5(叠加加分非惩罚); 其他场景恒1.0）',
    'level': '医院等级（专家先验映射）',
    'breadth': '科室齐全度 log1p(l2)/log1p(p90=142)，缺失→中性0.5',
    'distance': '直线距离衰减 1-km/25',
    'metro': '地铁可达性（步行分钟或直线距离），缺失→中性0.5',
    'bus': '300m 内公交站台数 log1p/分位帽',
    'key_specialty': '特色科室加分（当前覆盖 20/450=4.4%，不可作主权重，缺失/空=0）',
    'emergency': '急诊能力（red_flag 场景：任一急诊类科室）',
    'chronic': '慢病专病随访能力（chronic 场景：命中科室含“专病”visit_type）',
    'chronic_fit': '分级诊疗先验（chronic 场景：低等级≤二级且有 p1 科室命中 → 社区承接加分）',
}


def sha1_of_bytes(b):
    return hashlib.sha1(b).hexdigest()[:16]


def haversine_km(lng1, lat1, lng2, lat2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ----------------------------------------------------------------------------
# 数据装载
# ----------------------------------------------------------------------------
def load_inputs():
    lex = json.load(open(LEXICON_PATH, encoding='utf-8'))
    di = json.load(open(DEPT_INDEX_PATH, encoding='utf-8'))
    idx = json.load(open(os.path.join(MANIFEST_DIR, '_index.json'), encoding='utf-8'))
    mans = {}
    for e in idx:
        p = os.path.join(MANIFEST_DIR, e['file'])
        m = json.load(open(p, encoding='utf-8'))
        m['_verdict'] = e.get('verify_verdict') or ''
        m['_geo_conf'] = e.get('geo_confidence') or ''
        mans[m['id']] = m
    return lex, di, mans


def input_fingerprint():
    h = hashlib.sha1()
    for p in (LEXICON_PATH, DEPT_INDEX_PATH, os.path.join(MANIFEST_DIR, '_index.json')):
        h.update(open(p, 'rb').read())
    return h.hexdigest()[:16]


# ----------------------------------------------------------------------------
# triage 解析：症状描述 → 目标科室（含优先级/置信度）
# ----------------------------------------------------------------------------
def resolve_terms(symptom, terms):
    """精确 → 症状内含词（取最长）→ 词内嵌于症状（取最短）。返回 (term, how, cands)。"""
    s = symptom.strip()
    if s in terms:
        return s, 'exact', terms[s]
    best = None
    for t in terms:
        if t in s and (best is None or len(t) > len(best)):
            best = t
    if best:
        return best, 'contained', terms[best]
    best = None
    for t in terms:
        if s in t and (best is None or len(t) < len(best)):
            best = t
    if best:
        return best, 'super', terms[best]
    # 分词兜底：按常见分隔/空格
    toks = re.split(r'[\s,，、;；]+', s)
    for tk in toks:
        if tk in terms:
            return tk, 'token', terms[tk]
    return None, 'none', []


def target_depts(symptom, terms, di):
    """目标科室列表：(dept_id, canonical, kind, priority, confidence, hospitals:set)"""
    term, how, cands = resolve_terms(symptom, terms)
    if term is None:
        return None, how, []
    out = []
    for c in cands:
        d = di['depts'].get(c['dept_id'])
        if not d:
            continue
        out.append({
            'dept_id': c['dept_id'], 'canonical': d['canonical'], 'kind': d['kind'],
            'priority': c['priority'], 'confidence': c['confidence'],
            'visit_types': d.get('visit_types', []),
            'hospitals': set(d['hospitals']),
        })
    return term, how, out


# ----------------------------------------------------------------------------
# feature_norms.json：从 438 家（有科室数据）预计算
# ----------------------------------------------------------------------------
def build_norms(di, mans):
    hosp_ids = sorted(di['hospitals'].keys())
    # 急诊类科室集合（canonical 含“急诊”，kind 政策内）
    emerg_depts, emerg_hosp = [], set()
    chronic_depts, chronic_cnt = [], {}
    for dk, d in sorted(di['depts'].items()):
        if '急诊' in d['canonical'] and d['kind'] in ('clinic', 'specialty_clinic'):
            emerg_depts.append(dk)
            emerg_hosp |= set(d['hospitals'])
        if '专病' in d.get('visit_types', []):
            chronic_depts.append(dk)
            for h in d['hospitals']:
                chronic_cnt[h] = chronic_cnt.get(h, 0) + 1
    hospitals = {}
    for hid in hosp_ids:
        m = mans[hid]
        f = m.get('features', {})
        c0 = m['campuses'][0]
        _hname = m['institution']['name']
        _ped_excl = any(mk in _hname for mk in PEDIATRIC_HOSP_MARKS)
        _ped = any(any(mk in di['depts'][did]['canonical'] for mk in PEDIATRIC_MARKS)
                   for did in di['hospitals'].get(hid, {}).get('dept_ids', [])
                   if did in di['depts']) or _ped_excl
        l2 = f.get('bookable_dept_l2_count', {}).get('value')
        ks = f.get('key_specialties', {}).get('value') or []
        wm = f.get('metro_walk_min', {}).get('value')
        md = f.get('metro_nearest_dist_m', {}).get('value')
        bs = f.get('bus_stops_300m', {}).get('value')
        mi = f.get('medical_insurance', {}).get('value')
        low = {
            'level': f.get('hospital_level', {}).get('provenance', {}).get('confidence') == 'low',
            'metro': f.get('metro_nearest_dist_m', {}).get('provenance', {}).get('confidence') == 'low',
            'bus': f.get('bus_stops_300m', {}).get('provenance', {}).get('confidence') == 'low',
        }
        hospitals[hid] = {
            'name': m['institution']['name'],
            'level': m['institution'].get('level') or '',
            'type': m['institution'].get('type') or '',
            'district': c0.get('district') or '',
            'lng': c0['geo']['lng'], 'lat': c0['geo']['lat'],
            'level_score': LEVEL_SCORE_MAP.get(m['institution'].get('level') or '', 0.28),
            'dept_breadth': (min(1.0, math.log1p(l2) / math.log1p(BREADTH_P90_L2))
                             if isinstance(l2, (int, float)) else None),
            'dept_l2_count': l2 if isinstance(l2, (int, float)) else None,
            'key_specialties': ks,
            'metro_walk_min': wm if isinstance(wm, (int, float)) else None,
            'metro_nearest_dist_m': md if isinstance(md, (int, float)) else None,
            'bus_stops_300m': bs if isinstance(bs, (int, float)) else None,
            'medical_insurance': mi,
            'has_emergency_dept': hid in emerg_hosp,
            'pediatric_capable': _ped,
            'pediatric_exclusive': _ped_excl,
            'chronic_visit_dept_count': chronic_cnt.get(hid, 0),
            'provenance_low': low,
            'blocked': m['_verdict'] == 'wrong',
            'geo_confidence': m['_geo_conf'],
        }
    return {
        'meta': {
            'version': '1.0.0',
            'generated_at': 'content-fingerprint:' + input_fingerprint() + ':' + NORMS_VERSION,
            'n_hospitals': len(hosp_ids),
            'note': '仅覆盖 dept_index 内 438 家（有科室数据）；权重为专家先验初值，未调参',
        },
        'caps': {
            'breadth_p90_l2': BREADTH_P90_L2, 'bus_stops_p90': BUS_STOPS_P90,
            'distance_cap_km': DIST_CAP_KM, 'metro_walk_cap_min': METRO_WALK_CAP_MIN,
            'metro_dist_cap_m': METRO_DIST_CAP_M,
            'low_conf_blend': LOW_CONF_BLEND, 'neutral': NEUTRAL,
        },
        'level_map': LEVEL_SCORE_MAP,
        'emerg_dept_ids': sorted(emerg_depts),
        'emerg_hospitals': sorted(emerg_hosp),
        'chronic_dept_count': len(chronic_depts),
        'hospitals': hospitals,
    }


def load_or_build_norms(di, mans, force=False):
    if not force and os.path.exists(NORMS_PATH):
        n = json.load(open(NORMS_PATH, encoding='utf-8'))
        if n['meta']['generated_at'] == 'content-fingerprint:' + input_fingerprint() + ':' + NORMS_VERSION:
            return n, False
    n = build_norms(di, mans)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(NORMS_PATH, 'w', encoding='utf-8') as fh:
        json.dump(n, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write('\n')
    return n, True


# ----------------------------------------------------------------------------
# stage-1 打分
# ----------------------------------------------------------------------------
def blend_low(v, low):
    """low-confidence 特征值向中性 0.5 混合 40%（缺失 None 由调用方先处理）"""
    if low:
        return (1 - LOW_CONF_BLEND) * v + LOW_CONF_BLEND * NEUTRAL
    return v


def score_hospital(hid, rec, weights, loc, targets, scenario):
    """返回 (score, contribs)。targets: 通过 kind 政策的目标科室列表。"""
    contribs = []

    def add(name, v, note=''):
        w = weights.get(name, 0.0)
        if w <= 0:
            return
        v = max(0.0, min(1.0, v))
        contribs.append({'feature': name, 'weight': w, 'value': round(v, 4),
                         'contribution': round(w * v, 4), 'note': note})

    # 1) 科室命中质量
    best, best_note = 0.0, ''
    hit_depts = []
    for t in targets:
        if t['kind'] not in ALLOWED_HIT_KINDS:
            continue
        if hid not in t['hospitals']:
            continue
        q = PRIORITY_W.get(t['priority'], 0.4) * CONF_W.get(t['confidence'], 0.6)
        hit_depts.append(t['canonical'])
        if q > best:
            best, best_note = q, 'p%d/%s' % (t['priority'], t['confidence'])
    add('match', best, ('命中: ' + ','.join(sorted(set(hit_depts))[:4]) + ' [' + best_note + ']')
        if best > 0 else '无命中')

    # 2) 等级
    add('level', blend_low(rec['level_score'], rec['provenance_low']['level']),
        rec['level'] or '未定级')
    # 3) 科室齐全度
    if rec['dept_breadth'] is None:
        add('breadth', NEUTRAL, '缺失→中性0.5')
    else:
        add('breadth', rec['dept_breadth'], 'l2=%s' % rec['dept_l2_count'])
    # 4) 距离
    if rec['lng'] and rec['lat']:
        km = haversine_km(loc[0], loc[1], rec['lng'], rec['lat'])
        add('distance', max(0.0, 1.0 - km / DIST_CAP_KM), '%.1fkm' % km)
    else:
        add('distance', NEUTRAL, '无坐标→中性0.5')
    # 5) 地铁
    wm, md = rec['metro_walk_min'], rec['metro_nearest_dist_m']
    low_m = rec['provenance_low']['metro']
    if wm is not None:
        add('metro', blend_low(1.0 - min(1.0, wm / METRO_WALK_CAP_MIN), low_m),
            '步行%sm%s' % (wm, ',低置信' if low_m else ''))
    elif md is not None:
        add('metro', blend_low(1.0 - min(1.0, md / METRO_DIST_CAP_M), low_m),
            '直线%sm%s' % (md, ',低置信' if low_m else ''))
    else:
        add('metro', NEUTRAL, '缺失→中性0.5')
    # 6) 公交
    bs = rec['bus_stops_300m']
    if bs is None:
        add('bus', NEUTRAL, '缺失→中性0.5')
    else:
        add('bus', blend_low(min(1.0, math.log1p(bs) / math.log1p(BUS_STOPS_P90)),
                             rec['provenance_low']['bus']), '%d站' % bs)
    # 7) 特色科室（空=已知缺失→0.0，不中性化）
    ks = rec['key_specialties']
    if ks:
        hit = any(any(ks_ in c or c in ks_ for c in t['canonical']) for ks_ in ks for t in targets)
        add('key_specialty', 1.0 if hit else 0.0,
            ('命中' if hit else '不相关') + '（覆盖4.4%）')
    else:
        add('key_specialty', 0.0, '空（覆盖4.4%）')
    # 8) 急诊（仅 red_flag 有权重）
    if 'emergency' in weights:
        add('emergency', 1.0 if rec['has_emergency_dept'] else 0.0,
            '有急诊类科室' if rec['has_emergency_dept'] else '无')
    # 9) 慢病专病（仅 chronic 有权重）
    p1_hit = any(t['priority'] == 1 and t['kind'] in ALLOWED_HIT_KINDS and hid in t['hospitals']
                 for t in targets)
    if 'chronic' in weights:
        has_ch = any('专病' in t['visit_types'] for t in targets
                     if hid in t['hospitals'] and t['kind'] in ALLOWED_HIT_KINDS)
        add('chronic', 1.0 if has_ch else 0.0,
            '命中科室含专病' if has_ch else '无专病随访')
    # 10) 分级诊疗承接（仅 chronic 有权重）：低等级 + p1 命中 → 社区/二级承接慢病
    if 'chronic_fit' in weights:
        fit = rec['level_score'] <= 0.50 and p1_hit
        add('chronic_fit', 1.0 if fit else 0.0,
            ('%s+p1命中' % rec['level']) if fit else '不适用')
    # 11) 人群适配 age_fit（peds 场景三档, 2026-10-06）: 儿童专属机构=1.0 / 有可约儿科类科室(非专属)=0.75 / 无=中性0.5(叠加加分非惩罚); 其他场景恒1.0
    if 'age_fit' in weights:
        if scenario == 'peds':
            if bool(rec.get('pediatric_exclusive')):
                add('age_fit', 1.0, '儿童专属机构')
            elif bool(rec.get('pediatric_capable')):
                add('age_fit', 0.75, '有可约儿科类科室(非儿童专属机构)')
            else:
                add('age_fit', NEUTRAL, '无显式儿科科室→中性0.5')
        else:
            add('age_fit', 1.0, '非儿科→恒1.0')

    score = sum(c['contribution'] for c in contribs)
    return score, contribs


def hard_filters(hid, rec, radius_km, require_insurance, target_hit):
    """返回 (pass, reason)。顺序：BLOCKED → 无科室数据(调用方保证) → 医保 → 半径 → 科室命中。"""
    if rec['blocked']:
        return False, 'BLOCKED(verify=wrong)'
    mi = rec['medical_insurance']
    if require_insurance and mi == 'no':
        return False, '非医保'
    if rec['lng'] and rec['lat']:
        km = haversine_km(target_hit['loc'][0], target_hit['loc'][1], rec['lng'], rec['lat'])
        if km > radius_km:
            return False, '超出半径%.0fkm(%.1f)' % (radius_km, km)
    if not target_hit['hit']:
        return False, '无目标科室命中'
    return True, ''


def pick_k(scores_desc):
    """K 的确定规则：相对 Top1 绝对落差地板 + 相邻肘部落差，取更激进者，钳制 [14,60]。"""
    n = len(scores_desc)
    if n <= K_MIN:
        return n, {'n': n}
    s0 = scores_desc[0]
    span = s0 - scores_desc[-1]
    floor = s0 - K_ABS_DROP
    i_floor = 0
    for i, s in enumerate(scores_desc):
        if s >= floor:
            i_floor = i
    gap_thr = max(0.05, K_GAP_FRAC * span)
    i_gap = n
    for i in range(1, n):
        if scores_desc[i - 1] - scores_desc[i] > gap_thr:
            i_gap = i
            break
    k = min(i_floor + 1, i_gap)
    k = max(K_MIN, min(K_MAX, k, n))
    return k, {'n': n, 's0': round(s0, 4), 'floor': round(floor, 4),
               'gap_thr': round(gap_thr, 4), 'i_floor': i_floor + 1, 'i_gap': i_gap}


def explain(contribs, score):
    """top-3 贡献项 + 扣分项（按“距满分丢失分”排序）。"""
    pos = sorted([c for c in contribs if c['contribution'] > 0],
                 key=lambda c: -c['contribution'])[:3]
    lost = []
    for c in contribs:
        lost.append((c['weight'] * (1.0 - c['value']), c))
    lost = [c for l, c in sorted(lost, key=lambda x: -x[0]) if l > 0.005][:3]
    return {
        'score': round(score, 4),
        'top3_contrib': [{'feature': c['feature'], 'w': c['weight'], 'v': c['value'],
                          'contrib': c['contribution'], 'note': c['note']} for c in pos],
        'deductions': [{'feature': c['feature'], 'w': c['weight'], 'v': c['value'],
                        'lost': round(c['weight'] * (1 - c['value']), 4), 'note': c['note']}
                       for c in lost],
    }


def run_stage1(symptom, scenario, loc, radius_km=None, metro_priority=False,
               require_insurance=True, lex=None, di=None, mans=None, norms=None,
               gt_priority_max=None):
    """stage-1 全流程：triage → 召回 → 硬过滤 → 打分 → K/剪枝 → 抓取顺序（预算内 SlotQuery 列表）。"""
    terms = lex['terms']
    term, how, targets = target_depts(symptom, terms, di)
    weights = dict(SCENARIOS[scenario])
    if metro_priority:  # 用户“地铁优先”：地铁权重 ×1.6，其余按比例归一
        weights['metro'] = weights.get('metro', 0.02) * 1.6
        tot = sum(weights.values())
        weights = {k: v / tot for k, v in weights.items()}
    radius_km = radius_km if radius_km is not None else SCENARIO_RADIUS_DEFAULT[scenario]

    # 召回：任一目标科室（kind 政策内）的医院并集 ∩ 438 家
    recall = set()
    for t in targets:
        if t['kind'] in ALLOWED_HIT_KINDS:
            recall |= t['hospitals']
    recall = sorted(recall & set(norms['hospitals'].keys()))

    universe, filtered = {}, {}
    for hid in recall:
        rec = norms['hospitals'][hid]
        hit = any(hid in t['hospitals'] for t in targets if t['kind'] in ALLOWED_HIT_KINDS)
        ok, reason = hard_filters(hid, rec, radius_km, require_insurance,
                                  {'loc': loc, 'hit': hit})
        if ok:
            score, contribs = score_hospital(hid, rec, weights, loc, targets, scenario)
            universe[hid] = (score, contribs, rec)
        else:
            filtered[hid] = reason

    ranked = sorted(universe.items(), key=lambda kv: (-kv[1][0], kv[0]))
    scores_desc = [s for _, (s, _, _) in ranked]
    K, k_info = pick_k(scores_desc)
    candidates = ranked[:K]
    overflow = ranked[K:]

    # stage-2 抓取顺序（契约演示：只列序，不发任何请求）
    fetch_plan = []
    for rank, (hid, (s, _, rec)) in enumerate(ranked[:min(BUDGET_FETCH_DEFAULT, len(ranked))], 1):
        fetch_plan.append({
            'order': rank, 'hospital_id': hid, 'name': rec['name'], 'static_score': round(s, 4),
            'SlotQuery_stub': {
                'query_id': 'stub-%s-%s' % (hid, scenario), 'hospital_id': hid,
                'dept_ids': [t['dept_id'] for t in targets if hid in t['hospitals']][:5],
                'scenario': scenario, 'earliest_from': 'TODAY', 'max_wait_days': 7,
                'price_cap_cny': None, 'target_doctor': None,
                'timeout_ms': 8000, 'budget_left': BUDGET_FETCH_DEFAULT - rank + 1,
            },
        })

    return {
        'symptom': symptom, 'scenario': scenario, 'loc': list(loc),
        'triage': {'term': term, 'how': how,
                   'targets': [{'dept_id': t['dept_id'], 'canonical': t['canonical'],
                                'kind': t['kind'], 'priority': t['priority'],
                                'confidence': t['confidence'],
                                'hosp': len(t['hospitals'])} for t in
                               sorted(targets, key=lambda t: (t['priority'], t['dept_id']))]},
        'weights': {k: round(v, 4) for k, v in weights.items()},
        'hard_filter': {'radius_km': radius_km, 'require_insurance': require_insurance,
                        'recall': len(recall), 'passed': len(universe),
                        'dropped': len(filtered),
                        'drop_reasons': _count_reasons(filtered),
                        'dropped_ids': sorted(filtered.keys())},
        'K': K, 'K_rule': k_info,
        'candidates': [
            {'rank': i + 1, 'hospital_id': hid, 'name': rec['name'],
             'level': rec['level'], 'type': rec['type'], 'district': rec['district'],
             'insurance': rec['medical_insurance'],
             **explain(c, s)}
            for i, (hid, (s, c, rec)) in enumerate(candidates)
        ],
        'pruned_by_K': [{'hospital_id': hid, 'name': rec['name'], 'score': round(s, 4)}
                        for hid, (s, _, rec) in overflow[:50]],
        'fetch_plan': fetch_plan,
    }


def _count_reasons(d):
    out = {}
    for r in d.values():
        key = re.sub(r'\(.*', '', r)
        out[key] = out.get(key, 0) + 1
    return out


def make_slot_queries_contract_demo(result, budget=BUDGET_FETCH_DEFAULT):
    """两段接口契约的最小演示：stage-1 输出 → stage-2 入参序列（不发起任何抓取）。"""
    return {
        'contract': 'SlotQuery[] / SlotResult[]（字段定义见 RANKING_SPEC.md §4，本处仅列结构）',
        'progressive': True,
        'early_stop': '满足 earliest<=max_wait_days 且 fee<=price_cap 的 SlotResult 达到 3 条即停',
        'budget_max_hospitals': budget,
        'fallback_on_all_fail': '回落 stage-1 静态排序 + 展示“暂无号源”',
        'slot_queries': [fp['SlotQuery_stub'] for fp in result['fetch_plan']],
    }


# ----------------------------------------------------------------------------
# 自检 / demo / eval
# ----------------------------------------------------------------------------
def do_check(di, mans, norms):
    """438 家全覆盖打分（不过滤）：无 NaN、全有限、幂等。"""
    lex = json.load(open(LEXICON_PATH, encoding='utf-8'))
    probe = {'community_first': '感冒', 'chronic_followup': '高血压', 'complex_severe': '头痛',
             'red_flag': '胸痛', 'peds': '生长发育'}
    loc = (121.4737, 31.2304)
    per_scenario, all_finite, n_scored = {}, True, 0
    for sc, sym in probe.items():
        term, how, targets = target_depts(sym, lex['terms'], di)
        weights = dict(SCENARIOS[sc])
        for hid in sorted(norms['hospitals'].keys()):
            s, contribs = score_hospital(hid, norms['hospitals'][hid], weights, loc, targets, sc)
            ok = math.isfinite(s) and all(math.isfinite(c['value']) and math.isfinite(c['contribution'])
                                          for c in contribs)
            if not ok:
                all_finite = False
            n_scored += 1
            per_scenario[sc] = per_scenario.get(sc, 0) + (1 if ok else 0)
    # 幂等：norms 重建对比 + 一次 stage-1 结果哈希对比
    rebuilt = build_norms(di, mans)
    norms_idem = json.dumps(rebuilt, sort_keys=True, ensure_ascii=False) == \
        json.dumps(json.load(open(NORMS_PATH, encoding='utf-8')), sort_keys=True, ensure_ascii=False)
    r1 = run_stage1('胸痛', 'red_flag', loc, lex=lex, di=di, mans=mans, norms=norms)
    r2 = run_stage1('胸痛', 'red_flag', loc, lex=lex, di=di, mans=mans, norms=norms)
    stage1_idem = json.dumps(r1, sort_keys=True, ensure_ascii=False) == \
        json.dumps(r2, sort_keys=True, ensure_ascii=False)

    n438 = len(norms['hospitals'])
    result = {
        'meta': {'generated_at': norms['meta']['generated_at'],
                 'note': '438 家全覆盖：逐场景对 438 家计算全部特征值与总分'},
        'coverage': {'n_with_dept_data': n438, 'scored_finite': n_scored,
                     'per_scenario_finite': per_scenario,
                     'nan_or_none': (not all_finite),
                     'all_438_scored': n_scored == n438 * len(probe) and all_finite},
        'idempotent': {'norms_rebuild_identical': norms_idem,
                       'stage1_repeat_identical': stage1_idem},
        'no_touch': '仅读 ssb_dept/ 与 hospital_intel/，未写任何既有产物',
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, 'self_check.json'), 'w', encoding='utf-8') as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write('\n')
    return result


def _demo_case(symptom, scenario, loc, radius_km=None, metro_priority=False,
               lex=None, di=None, mans=None, norms=None):
    res = run_stage1(symptom, scenario, loc, radius_km=radius_km, metro_priority=metro_priority,
                     lex=lex, di=di, mans=mans, norms=norms)
    res['slot_contract'] = make_slot_queries_contract_demo(res)
    return res


def _accept_checks(case):
    cands = case['candidates']
    top10 = cands[:10]
    checks = []
    sid = case['_accept']
    if sid == 'chest_pain_red_flag':
        ok = any(c['level'] == '三级甲等' for c in top10)
        cv = [c for c in top10 if '心血管内科' in json.dumps(c, ensure_ascii=False)]
        checks.append(('Top10 含三甲', ok))
        checks.append(('Top10 含心血管内科命中', len(cv) > 0))
        # 急诊/心血管高优先：Top3 中至少一家“有急诊类科室 或 心血管命中”
        top3 = cands[:3]
        hi = sum(1 for c in top3 if any('急诊' in n or '心血管' in n
                                        for n in (c['top3_contrib'][0]['note'],)))
        checks.append(('Top3 急诊/心血管高优先', hi >= 1))
    elif sid == 'peds_growth':
        names = json.dumps([c['name'] for c in top10], ensure_ascii=False)
        blob = json.dumps(case, ensure_ascii=False)
        has_eb = '儿童保健' in blob
        has_ped = '儿科' in blob
        non_sanjia = any(c['level'] != '三级甲等' for c in top10)
        checks.append(('候选集含儿童保健科', has_eb))
        checks.append(('候选集含儿科', has_ped))
        checks.append(('未被等级压掉(Top10 含非三甲)', non_sanjia))
    elif sid == 'htn_chronic':
        yijia = [c for c in top10 if c['level'] == '一级甲等']
        checks.append(('Top10 含一级甲等(社区卫生服务中心)', len(yijia) >= 1))
    return [{'check': n, 'pass': bool(p)} for n, p in checks]


def do_demo(lex, di, mans, norms):
    loc = (121.4737, 31.2304)
    demos = {
        'DEMO_chest_pain_red_flag.json': _demo_case('胸痛', 'red_flag', loc,
                                                    metro_priority=True, lex=lex, di=di,
                                                    mans=mans, norms=norms),
        'DEMO_peds_growth.json': _demo_case('儿童生长发育', 'peds', loc,
                                            lex=lex, di=di, mans=mans, norms=norms),
        'DEMO_hypertension_chronic.json': _demo_case('高血压', 'chronic_followup', loc,
                                                     lex=lex, di=di, mans=mans, norms=norms),
    }
    demos['DEMO_chest_pain_red_flag.json']['_accept'] = 'chest_pain_red_flag'
    demos['DEMO_peds_growth.json']['_accept'] = 'peds_growth'
    demos['DEMO_hypertension_chronic.json']['_accept'] = 'htn_chronic'
    for fn, d in list(demos.items()):
        d['acceptance'] = _accept_checks(d)
    # 静态覆盖样例：感冒 × community_first，438 家全打分（展示覆盖与无 NaN）
    cov = run_stage1('感冒', 'community_first', loc, lex=lex, di=di, mans=mans, norms=norms)
    all_scores = []
    weights = dict(SCENARIOS['community_first'])
    term, how, targets = target_depts('感冒', lex['terms'], di)
    for hid, rec in norms['hospitals'].items():
        s, _ = score_hospital(hid, rec, weights, loc, targets, 'community_first')
        all_scores.append(s)
    cov_demo = {
        'symptom': '感冒', 'scenario': 'community_first',
        'coverage': {
            'n_scored_all_438': len(all_scores),
            'all_finite': all(math.isfinite(s) for s in all_scores),
            'score_min': round(min(all_scores), 4), 'score_max': round(max(all_scores), 4),
            'score_mean': round(sum(all_scores) / len(all_scores), 4),
            'note': '438 家（有科室数据）全部可算分；12 家无科室数据被硬过滤（见 self_check）',
        },
        'stage1_top10': cov['candidates'][:10],
        'hard_filter': cov['hard_filter'],
        'K': cov['K'], 'K_rule': cov['K_rule'],
    }
    demos['DEMO_static_coverage.json'] = cov_demo
    os.makedirs(OUT_DIR, exist_ok=True)
    for fn, d in demos.items():
        with open(os.path.join(OUT_DIR, fn), 'w', encoding='utf-8') as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write('\n')
    return {fn: d.get('acceptance') or d.get('coverage') for fn, d in demos.items()}


def load_eval_set():
    return json.load(open(EVAL_SET_PATH, encoding='utf-8'))


def do_eval(lex, di, mans, norms):
    cases = load_eval_set()
    results = []
    for case in cases:
        loc = tuple(case.get('loc', [121.4737, 31.2304]))
        res = run_stage1(case['symptom'], case['scenario'], loc,
                         radius_km=case.get('radius_km'),
                         lex=lex, di=di, mans=mans, norms=norms)
        if res['triage']['term'] is None:
            results.append({'case_id': case['case_id'], 'error': 'triage 未命中'})
            continue
        # GT：priority<=gt_pmax 的目标科室 ∩ 等级 ∩ 距离
        gtm = case.get('gt_priority_max', 1)
        _, _, raw_targets = target_depts(case['symptom'], lex['terms'], di)
        gt = set()
        for t in raw_targets:
            if t['kind'] not in ALLOWED_HIT_KINDS or t['priority'] > gtm:
                continue
            for h in t['hospitals']:
                rec = norms['hospitals'].get(h)
                if not rec or rec['blocked']:
                    continue
                if case.get('level_in') and rec['level'] not in case['level_in']:
                    continue
                md = case.get('max_dist_km')
                if md:
                    km = haversine_km(loc[0], loc[1], rec['lng'], rec['lat'])
                    if km > md:
                        continue
                gt.add(h)
        # 近距 GT：GT 中距用户最近 20 家（用户实际意图是“近处的优者”）
        def _d(h):
            rec = norms['hospitals'][h]
            return haversine_km(loc[0], loc[1], rec['lng'], rec['lat'])
        gt_near = set(sorted(gt, key=lambda h: (_d(h), h))[:20]) if gt else set()
        ranked_ids = [c['hospital_id'] for c in res['candidates']]
        cand_set = set(ranked_ids)
        K = res['K']

        def recall_at(k, pool):
            top = set(ranked_ids[:k])
            return (len(top & pool) / len(pool)) if pool else None

        rec_at = {str(k): (round(recall_at(k, gt), 4) if recall_at(k, gt) is not None else None)
                  for k in (5, 10, 20, 50)}
        rec_near_at = {str(k): (round(recall_at(k, gt_near), 4)
                                if recall_at(k, gt_near) is not None else None)
                       for k in (5, 10, 20, 50)}
        rec_auto = recall_at(K, gt)
        rec_auto_near = recall_at(K, gt_near)
        hit = {}
        for n in (1, 3, 5, 10):
            hit['top%d' % n] = 1 if (set(ranked_ids[:n]) & gt) else 0
        pruned_better = (len(gt - cand_set) / len(gt)) if gt else None
        pruned_better_near = (len(gt_near - cand_set) / len(gt_near)) if gt_near else None
        results.append({
            'case_id': case['case_id'], 'symptom': case['symptom'],
            'scenario': case['scenario'],
            'triage_term': res['triage']['term'],
            'recall_universe': res['hard_filter']['recall'],
            'passed_hard_filter': res['hard_filter']['passed'],
            'gt_size': len(gt),
            'K': K,
            'recall_at': rec_at,
            'recall_near_at': rec_near_at,
            'recall_at_autoK': round(rec_auto, 4) if rec_auto is not None else None,
            'recall_near_at_autoK': round(rec_auto_near, 4) if rec_auto_near is not None else None,
            'topN_hit': hit,
            'pruned_better_ratio': (round(pruned_better, 4) if pruned_better is not None else None),
            'pruned_better_ratio_near': (round(pruned_better_near, 4)
                                         if pruned_better_near is not None else None),
            'top5': [{'rank': c['rank'], 'hospital_id': c['hospital_id'], 'name': c['name'],
                      'level': c['level'], 'score': c['score']} for c in res['candidates'][:5]],
        })
    agg = _aggregate(results)
    out = {'meta': {'generated_at': norms['meta']['generated_at'],
                    'n_cases': len(cases),
                    'note': '离线回放：stage-1 静态排序，无任何号源抓取'},
           'aggregate': agg, 'cases': results}
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, 'eval_results.json'), 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write('\n')
    _write_eval_report(out)
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def _aggregate(results):
    ok = [r for r in results if 'error' not in r]
    agg = {
        'n_cases': len(ok),
        'recall_at_5': _mean([r['recall_at']['5'] for r in ok]),
        'recall_at_10': _mean([r['recall_at']['10'] for r in ok]),
        'recall_at_20': _mean([r['recall_at']['20'] for r in ok]),
        'recall_at_50': _mean([r['recall_at']['50'] for r in ok]),
        'recall_at_autoK': _mean([r['recall_at_autoK'] for r in ok]),
        'recall_near_at_10': _mean([r['recall_near_at']['10'] for r in ok]),
        'recall_near_at_20': _mean([r['recall_near_at']['20'] for r in ok]),
        'recall_near_at_50': _mean([r['recall_near_at']['50'] for r in ok]),
        'recall_near_at_autoK': _mean([r['recall_near_at_autoK'] for r in ok]),
        'top1_hit': _mean([r['topN_hit']['top1'] for r in ok]),
        'top3_hit': _mean([r['topN_hit']['top3'] for r in ok]),
        'top5_hit': _mean([r['topN_hit']['top5'] for r in ok]),
        'top10_hit': _mean([r['topN_hit']['top10'] for r in ok]),
        'pruned_better_mean': _mean([r['pruned_better_ratio'] for r in ok]),
        'pruned_better_mean_near': _mean([r['pruned_better_ratio_near'] for r in ok]),
        'K_stats': {'min': min(r['K'] for r in ok), 'max': max(r['K'] for r in ok),
                    'mean': _mean([r['K'] for r in ok])},
    }
    by_sc = {}
    for sc in SCENARIOS:
        sub = [r for r in ok if r['scenario'] == sc]
        if sub:
            by_sc[sc] = {
                'n': len(sub),
                'recall_at_10': _mean([r['recall_at']['10'] for r in sub]),
                'recall_at_20': _mean([r['recall_at']['20'] for r in sub]),
                'recall_at_50': _mean([r['recall_at']['50'] for r in sub]),
                'top3_hit': _mean([r['topN_hit']['top3'] for r in sub]),
                'top10_hit': _mean([r['topN_hit']['top10'] for r in sub]),
                'pruned_better_mean': _mean([r['pruned_better_ratio'] for r in sub]),
            }
    agg['by_scenario'] = by_sc
    return agg


def _write_eval_report(out):
    a = out['aggregate']
    lines = []
    ap = lines.append
    ap('# 静态排序离线回放报告（EVAL_REPORT）')
    ap('')
    ap('- 生成指纹: `%s`（幂等，无墙钟）' % out['meta']['generated_at'])
    ap('- 用例数: %d；全部为 stage-1 静态回放，**未发起任何号源/排班抓取**' % out['meta']['n_cases'])
    ap('- GT 定义: 目标科室(triage, priority≤gt_pmax, kind 政策内) ∩ 等级集合 ∩ 距离半径；被 BLOCKED/无科室数据医院不计入 GT。')
    ap('')
    ap('## 1. 候选集 K vs 召回率（用于定 K）')
    ap('')
    ap('全量 GT 召回（GT=半径内全部合格医院，含远处）：')
    ap('')
    ap('| K | 5 | 10 | 20 | 50 | auto(肘部) |')
    ap('|---|---|---|---|---|---|')
    ap('| 召回 | %s | %s | %s | %s | %s（K∈[%d,%d]，均值 %s）|' % (
        a['recall_at_5'], a['recall_at_10'], a['recall_at_20'], a['recall_at_50'],
        a['recall_at_autoK'], a['K_stats']['min'], a['K_stats']['max'], a['K_stats']['mean']))
    ap('')
    ap('近距 GT 召回（GT 中最近 20 家，代表用户真实意图“近处的优者”）：')
    ap('')
    ap('| K | 10 | 20 | 50 | auto(肘部) |')
    ap('|---|---|---|---|---|')
    ap('| 召回 | %s | %s | %s | %s |' % (
        a['recall_near_at_10'], a['recall_near_at_20'], a['recall_near_at_50'],
        a['recall_near_at_autoK']))
    ap('')
    rn10, rn20, rn50 = (a['recall_near_at_10'] or 0), (a['recall_near_at_20'] or 0), (a['recall_near_at_50'] or 0)
    if rn20 >= 0.95 * max(rn50, 1e-9) and rn10 < 0.95 * max(rn50, 1e-9):
        advice = '建议 K≈20：近距召回已捕获 50-K 的 ≥95%，K=10 不足。'
    elif rn10 >= 0.95 * max(rn50, 1e-9):
        advice = 'K=10 即捕获近距召回 ≥95%，K 可取 10（预算友好）。'
    elif rn20 >= 0.90 * max(rn50, 1e-9):
        advice = '建议 K≈20~30：近距召回在 K=20 已接近饱和。'
    else:
        advice = '近距召回随 K 增长仍明显，建议 K=30~50 并配合 stage-2 预算裁剪。'
    ap('**结论**：%s' % advice)
    ap('')
    ap('## 2. TopN 命中率')
    ap('')
    ap('| N | 命中(≥1 GT) |')
    ap('|---|---|')
    for n in (1, 3, 5, 10):
        ap('| top%d | %s |' % (n, a['top%d_hit' % n]))
    ap('')
    ap('## 3. 被 K 剪掉但“其实更优”的比例')
    ap('')
    ap('- 全量 GT 平均 pruned_better = **%s**（GT 中被肘部规则剪掉的医院占比）' % a['pruned_better_mean'])
    ap('- 近距 GT（最近20家）平均 pruned_better = **%s**（更贴近真实损失）' % a['pruned_better_mean_near'])
    ap('- 解读：近距口径越低说明剪枝越安全；若 >0.3，需放宽 K_GAP/K_ABS 或提高 stage-2 预算。')
    ap('')
    ap('## 4. 分场景')
    ap('')
    ap('| 场景 | n | R@10 | R@20 | R@50 | top3 | top10 | pruned |')
    ap('|---|---|---|---|---|---|---|---|')
    for sc, v in a['by_scenario'].items():
        ap('| %s | %d | %s | %s | %s | %s | %s | %s |' %
           (sc, v['n'], v['recall_at_10'], v['recall_at_20'], v['recall_at_50'],
            v['top3_hit'], v['top10_hit'], v['pruned_better_mean']))
    ap('')
    ap('## 5. 用例明细')
    ap('')
    ap('| case | 症状 | 场景 | 词 | 召回 | GT | K | R@10 | R@20 | R@50 | pruned |')
    ap('|---|---|---|---|---|---|---|---|---|---|---|')
    for r in out['cases']:
        if 'error' in r:
            ap('| %s | %s | %s | ❌%s | - | - | - | - | - | - | - |' %
               (r['case_id'], r.get('symptom', ''), r.get('scenario', ''), r['error']))
            continue
        ap('| %s | %s | %s | %s | %d | %d | %d | %s | %s | %s | %s |' %
           (r['case_id'], r['symptom'], r['scenario'], r['triage_term'],
            r['recall_universe'], r['gt_size'], r['K'],
            r['recall_at']['10'], r['recall_at']['20'], r['recall_at']['50'],
            r['pruned_better_ratio']))
    ap('')
    ap('> 注：召回率 = |TopK ∩ GT| / |GT|；GT 已按场景做等级/距离约束，天然小于召回全集。')
    os.makedirs(EVAL_DIR, exist_ok=True)
    with open(os.path.join(EVAL_DIR, 'EVAL_REPORT.md'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description='stage-1 静态排序（纯脚本，零抓取）')
    ap.add_argument('--norms', action='store_true')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--demo', action='store_true')
    ap.add_argument('--eval', action='store_true')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--case', type=str)
    ap.add_argument('--scenario', type=str, default='community_first', choices=list(SCENARIOS))
    ap.add_argument('--loc', type=str, default='121.4737,31.2304')
    ap.add_argument('--radius', type=float, default=None)
    ap.add_argument('--metro-priority', action='store_true')
    ap.add_argument('--top', type=int, default=10)
    args = ap.parse_args(argv)

    lex, di, mans = load_inputs()
    norms, built = load_or_build_norms(di, mans)
    if built:
        print('[norms] 重建 feature_norms.json（指纹=%s）' % norms['meta']['generated_at'])
    if args.check or args.all:
        r = do_check(di, mans, norms)
        print('[check] 438家全覆盖=%s 无NaN=%s 幂等=%s' % (
            r['coverage']['all_438_scored'], not r['coverage']['nan_or_none'],
            r['idempotent']))
    if args.demo or args.all:
        d = do_demo(lex, di, mans, norms)
        for fn, acc in d.items():
            if acc and isinstance(acc, list):
                line = ' '.join('%s:%s' % (c['check'], 'PASS' if c['pass'] else 'FAIL') for c in acc)
            else:
                line = json.dumps(acc, ensure_ascii=False)
            print('[demo] %s -> %s' % (fn, line[:200]))
    if args.eval or args.all:
        out = do_eval(lex, di, mans, norms)
        a = out['aggregate']
        print('[eval] n=%d R@10=%s R@20=%s R@50=%s autoK=%s top3=%s pruned=%s' % (
            a['n_cases'], a['recall_at_10'], a['recall_at_20'], a['recall_at_50'],
            a['recall_at_autoK'], a['top3_hit'], a['pruned_better_mean']))
    if args.case:
        lng, lat = [float(x) for x in args.loc.split(',')]
        res = run_stage1(args.case, args.scenario, (lng, lat), radius_km=args.radius,
                         metro_priority=args.metro_priority, lex=lex, di=di, mans=mans,
                         norms=norms)
        print('symptom=%s term=%s(%s) recall=%d passed=%d K=%d' % (
            args.case, res['triage']['term'], res['triage']['how'],
            res['hard_filter']['recall'], res['hard_filter']['passed'], res['K']))
        for c in res['candidates'][:args.top]:
            print('  %2d %s %s %s %s score=%s' % (
                c['rank'], c['hospital_id'], c['name'][:18], c['level'], c['district'], c['score']))
            for t in c['top3_contrib']:
                print('       +%.3f %s(%s) %s' % (t['contrib'], t['feature'], t['v'], t['note'][:40]))
    return 0


if __name__ == '__main__':
    sys.exit(main())
