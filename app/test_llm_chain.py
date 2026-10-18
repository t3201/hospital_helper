# -*- coding: utf-8 -*-
"""app 推理链路离线自检（不依赖 LLM / 网络）:
  * build_context: 归一化科室词表(hc≥5, 363) + 医院目录(438)
  * _build_prompt: 基础数据上下文随 prompt 下发; 12 维特征含 age_fit; dept_canonicals 带 tier
  * _validate:     LLM 输出结构校验（scenario/weights 归一/词表内科室; {dept,tier} 新格式 + 裸字符串旧格式兼容）
  * run_llm:       LLM 直出权重+分层科室 → stage-1 同构（召回/硬过滤/打分/K; tier→priority）
  * age_fit:       peds 场景 有儿科能力=1.0 / 无=中性0.5（叠加加分非惩罚）
  * _depts_from_term: 同义词桥 重影→斜视 → 症状词表
  * 患者层科室过滤: 儿童剔除妇科
用法: python app/test_llm_chain.py
"""
import json
import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(APP_DIR)
os.chdir(WS)
sys.path.insert(0, WS)

import app.infer as infer
import app.candidates as candidates

sh = candidates._load_stage1_module()
lex, di, mans = sh.load_inputs()
norms, _ = sh.load_or_build_norms(di, mans)

class FakeStage1:
    pass
fs = FakeStage1()
fs.sh, fs.lex, fs.di, fs.mans = sh, lex, di, mans
fs.norms = norms
fs.canon_index = {d['canonical']: (kid, d) for kid, d in di['depts'].items()}

# 1) 上下文
ctx = infer.build_context(fs)
print('ctx: n_depts=%d n_hospitals=%d (expect 363/438)' % (ctx['n_depts'], ctx['n_hospitals']))
assert ctx['n_depts'] == 363 and ctx['n_hospitals'] == 438
assert all(d['kind'] in ('clinic', 'specialty_clinic', 'screening') for d in ctx['depts'])
assert all(d['hospital_count'] >= 5 for d in ctx['depts'])
# 场景基线表现含 price + age_fit, 行和=1.0
for _sc, _tab in ctx['scenario_tables'].items():
    assert 'price' in _tab and 'age_fit' in _tab, _sc
    assert abs(sum(_tab.values()) - 1.0) < 2e-3, (_sc, sum(_tab.values()))
assert ctx['scenario_tables']['peds']['age_fit'] == 0.10

# 2) prompt 构建
spec = {
    'symptom_text': '眼睛看远处一直有重影，不能合成3D视觉，很多年了',
    'patient': {'age': 45, 'age_band': 'adult', 'is_child': False, 'proxy': False, 'gender': '女'},
    'urgency_days': 14, 'visit_type': 'new',
    'prefs': {'metro_priority': False, 'price_cap_cny': None, 'level_expectation': 'balanced'},
    'notes': [], '_raw': ['眼睛看远处一直有重影，不能合成3D视觉，很多年了', '', ''],
}
prompt = infer._build_prompt(ctx, spec, '中远两湾城(121.443,31.252)')
print('prompt chars: %d (~%d tokens est)' % (len(prompt), len(prompt) // 2.2))
assert '可约科室词表' in prompt and '医院目录' in prompt
assert '女' in prompt  # gender 进入上下文
assert '眼科' in [d['canonical'] for d in ctx['depts']]
assert 'tier' in prompt and 'age_fit' in prompt  # 新契约: 层级 + 人群适配

# 3) 校验器: mock LLM 输出（新契约 {dept,tier} 格式）
good = {
    'scenario': 'community_first',
    'weights': {'match': .34, 'level': .12, 'breadth': .08, 'distance': .28,
                'metro': .10, 'bus': .05, 'key_specialty': .03, 'age_fit': .05,
                'emergency': 0, 'chronic': 0, 'chronic_fit': 0},
    'dept_canonicals': [{'dept': '眼科', 'tier': 1}, {'dept': '神经内科', 'tier': 2}],
    'triage_term': '斜视',
    'max_wait_days': 14,
    'price_cap_cny': None,
    'reasoning': ['复视为眼科问题, 就近即可'],
}
v, errs = infer._validate(good, ctx)
print('validate good: errs=%r depts=%r tiers=%r scenario=%r'
      % (errs, [d['canonical'] for d in v['depts']],
         {d['canonical']: d.get('tier') for d in v['depts']}, v['scenario']))
assert not errs and v['scenario'] == 'community_first' and len(v['depts']) == 2
assert abs(sum(v['weights'].values()) - 1.0) < 1e-9
assert {d['canonical']: d['tier'] for d in v['depts']} == {'眼科': 1, '神经内科': 2}
# tier 越界夹取
v_t, errs_t = infer._validate(dict(good, dept_canonicals=[{'dept': '眼科', 'tier': 9}]), ctx)
assert not errs_t and v_t['depts'][0]['tier'] == 3
# 旧契约裸字符串容忍（→tier=1）
v_s, errs_s = infer._validate(dict(good, dept_canonicals=['眼科', '神经内科']), ctx)
assert not errs_s and all(d['tier'] == 1 for d in v_s['depts'])

bad = dict(good, scenario='urgent', dept_canonicals=['眼科门诊(斜视)'])
v2, errs2 = infer._validate(bad, ctx)
print('validate bad: errs=%r' % errs2)
assert any('scenario' in e for e in errs2) and any('dept_canonicals' in e for e in errs2)

# 4) run_llm: mock 权重 + 词表直选分层科室 跑完整 stage-1 同构流程
c = candidates.Stage1(verbose=False)
w = {k: v['weights'][k] for k in infer.FEATURES if v['weights'].get(k)}
res = c.run_llm('斜视', [('眼科', 1), ('神经内科', 2)], 'community_first', (121.442988, 31.252108),
                weights=w, patient_band='adult')
assert res is not None
print('run_llm: triage how=%s targets=%d K=%s recall=%s passed=%s' % (
    res['triage']['how'], len(res['triage']['targets']), res['K'],
    res['hard_filter']['recall'], res['hard_filter']['passed']))
assert res['triage']['how'] == 'llm'
assert any(t['canonical'] == '眼科' and t['priority'] == 1 for t in res['triage']['targets'])
assert any(t['canonical'] == '神经内科' and t['priority'] == 2 for t in res['triage']['targets'])
assert abs(sum(res['applied_weights'].values()) - 1.0) < 1e-9
print('top3:', [(x['rank'], x['hospital_id'], x['name']) for x in res['candidates'][:3]])

# 4b) age_fit: peds 场景下 有儿科能力=1.0 / 无=中性0.5（直接打 score_hospital 单元验证）
_w2 = dict(w); _w2['age_fit'] = 0.10
_tgt = [{'kind': 'clinic', 'priority': 1, 'confidence': 'high', 'canonical': '儿科',
         'hospitals': set(norms['hospitals'].keys()), 'dept_id': 'X'}]
_hped = next(h for h, r in sorted(norms['hospitals'].items()) if r.get('pediatric_capable'))
_hnop = next(h for h, r in sorted(norms['hospitals'].items()) if not r.get('pediatric_capable'))
_s1, _c1 = sh.score_hospital(_hped, norms['hospitals'][_hped], _w2, (121.4737, 31.2304), _tgt, 'peds')
_s2, _c2 = sh.score_hospital(_hnop, norms['hospitals'][_hnop], _w2, (121.4737, 31.2304), _tgt, 'peds')
_af1 = [x for x in _c1 if x['feature'] == 'age_fit'][0]['value']
_af2 = [x for x in _c2 if x['feature'] == 'age_fit'][0]['value']
print('age_fit: %s(ped)=%s  %s(noped)=%s' % (_hped, _af1, _hnop, _af2))
assert _af1 == 1.0 and _af2 == 0.5
# 非 peds 场景恒 1.0
_s3, _c3 = sh.score_hospital(_hped, norms['hospitals'][_hped], _w2, (121.4737, 31.2304), _tgt, 'community_first')
assert [x for x in _c3 if x['feature'] == 'age_fit'][0]['value'] == 1.0

# 5) 词表外科室 → infer 层 _depts_from_term（同义词桥 重影→斜视 → 词表）
fb = infer._depts_from_term('重影', c)
print('infer._depts_from_term(重影): %s' % [d['canonical'] for d in fb])
assert [d['canonical'] for d in fb][:2] == ['眼科', '儿科'], fb
# run_llm 自身词表回落: 直接可解析的词
res2 = c.run_llm('糖尿病', [], 'chronic_followup', (121.442988, 31.252108), weights=w, patient_band='adult')
assert res2 is not None and res2['triage']['how'] == 'llm_term_lexicon'
print('run_llm lexicon fallback: how=%s targets=%s' % (
    res2['triage']['how'], [t['canonical'] for t in res2['triage']['targets']][:4]))

# 6) 儿童场景: LLM 选了 妇科 → 患者层过滤剔除
res3 = c.run_llm('发热', [('儿科', 1), ('妇科', 2)], 'peds', (121.4737, 31.2304), weights=w, patient_band='child')
print('child run: pf=%s targets=%s' % (
    res3.get('patient_filter', {}).get('note', '(无过滤)')[:60],
    [t['canonical'] for t in res3['triage']['targets']]))
assert not any(t['canonical'] == '妇科' for t in res3['triage']['targets'])

print()
print('ALL OFFLINE CHECKS PASSED')
