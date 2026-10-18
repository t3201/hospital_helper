# -*- coding: utf-8 -*-
"""终裁 LLM（全流程第 2 次模型推理, 2026-10-06 两阶段架构 Phase C）。

架构（用户指令）: 挂号过程 = 开始一次主诉解析模型推理(infer.infer_llm) + 中间两阶段 live
(probe 快筛初排 + deep 前 6 家深核) + 最终结果确定一次模型推理(本模块)。

输入: 患者摘要 + 目标科室(tier) + 明细列表(deep 已核实的医院: 医生/费档/最早可约/示例号段)
     + probe 摘要(其余已核实医院的单行信号)。
输出: 严格 JSON final top3(rank/hospital_id/doctor/reason) + confidence。
校验失败/LLM 不可用 => 返回 None, 主流程回退确定性 top3(两级排序), 不阻断交付。

离线可测: run_final(..., llm_caller=...) 注入假调用; --check 不经过本模块(只校验已落盘结果)。
"""
import json

from . import llm_client

SYS = (
    '你是医院挂号推荐的终裁者。用户给出患者信息、目标科室、以及「明细列表」(已通过实时深核的医院, 含医生/价格/最早可约日期) '
    '和「探测摘要」(其余已核实医院的简要信号)。你只能从「明细列表」中挑选最终前 3 家医院并排序。'
    '必须遵守:\n'
    '1. 不得选择明细列表之外的医院, 不得编造 hospital_id 或医生名;\n'
    '2. doctor 字段 = 该医院「可约医生」里出现过的名字, 或 null(不指定医生);\n'
    '3. 3 家医院互不重复, rank 必须是 1/2/3 各一次;\n'
    '4. 取舍优先级: ①最早可约日期(越早越好) ②费档(越低越好; 患者有预算上限时预算内优先) '
    '③科室 tier(tier1 首选优先) ④医生职称/专长与主诉匹配 ⑤医院等级与距离;\n'
    '5. 只输出一个严格 JSON 对象(无 markdown 代码块、无解释文字), 格式:\n'
    '{"final_picks":[{"rank":1,"hospital_id":"SH-XXX","doctor":null,"reason":"30字内"}],'
    '"confidence":"high|medium|low","notes":"50字内"}'
)

_TIER_ZH = {1: '首选', 2: '相关', 3: '弱相关'}


def _fee_tiers_txt(ft):
    if not ft:
        return '未记录'
    parts = ['%s %.0f%%' % (k, 100.0 * v) for k, v in sorted(ft.items(), key=lambda kv: -kv[1])[:4]]
    return ', '.join(parts)


def build_prompt(spec, inf, detailed, probe_summary, loc_text=''):
    """detailed: [{hospital_id,name,level,district,distance_km,dept,dept_tab,slot_types,
                    earliest_date,earliest_days,fee_tier,fee_tiers,doctors,slot_samples}]
    probe_summary: [{hospital_id,name,status,earliest_date,slot_types}]"""
    p = spec.get('patient', {})
    lines = []
    _g = {'male': '男', 'female': '女'}.get(p.get('gender'), '未提供')
    _age = p.get('age')
    lines.append('【患者】性别=%s 年龄=%s 主诉="%s" 场景=%s(%s) 位置="%s" 等待≤%s天 费用上限=%s元' % (
        _g, _age if _age is not None else '未提供',
        spec.get('symptom_text', ''), inf.get('scenario', ''), inf.get('scenario_zh', ''),
        loc_text or '未提供',
        spec.get('urgency_days'), spec.get('prefs', {}).get('price_cap_cny')))
    _tiers = inf.get('llm_dept_tiers') or {}
    _depts = inf.get('llm_depts') or []
    lines.append('【目标科室】' + ('; '.join('%s(tier %d %s)' % (d, _tiers.get(d, 1), _TIER_ZH.get(_tiers.get(d, 1), ''))
                                      for d in _depts) or '未指定'))
    lines.append('【明细列表】(实时深核已确认, 按初排顺序; 医生=实时可约名单, 括号内为费档)')
    if not detailed:
        lines.append('(空)')
    for i, d in enumerate(detailed, 1):
        lines.append('%d. %s %s(%s / %s / %skm) 科室=%s' % (
            i, d['hospital_id'], d['name'], d.get('level') or '?', d.get('district') or '?',
            d.get('distance_km'), d.get('dept') or '?'))
        _ed = d.get('earliest_days')
        lines.append('   最早可约: %s%s' % (d.get('earliest_date') or '未确认',
                                           ('(距今天 %d 天)' % _ed) if isinstance(_ed, (int, float)) else ''))
        lines.append('   费档: %s' % _fee_tiers_txt(d.get('fee_tiers')))
        lines.append('   号源类型: %s' % ('、'.join(d.get('slot_types') or []) or '未记录'))
        _docs = d.get('doctors') or []
        if _docs:
            lines.append('   可约医生(%d): %s' % (len(_docs), '、'.join(_docs[:20]) + ('…' if len(_docs) > 20 else '')))
        _ss = d.get('slot_samples') or []
        if _ss:
            lines.append('   示例号段: ' + '; '.join(
                '%s %s %s %s(%s)' % (s.get('date'), s.get('time'), s.get('doctor'), s.get('fee_tier'), s.get('sub_tab'))
                for s in _ss[:5]))
    lines.append('【探测摘要】(快速探测已核实, 未深核明细; 仅供参考, 不可选)')
    if not probe_summary:
        lines.append('(空)')
    for x in probe_summary[:20]:
        _st = '有号' if x.get('status') == 'ok' else ('无号' if x.get('status') == 'no_slot' else x.get('status'))
        lines.append('- %s %s: %s%s%s' % (
            x.get('hospital_id'), x.get('name'), _st,
            ('(最早 %s)' % x['earliest_date']) if x.get('earliest_date') else '',
            ('; 类型=' + '、'.join(x.get('slot_types') or [])) if x.get('slot_types') else ''))
    lines.append('请按要求输出严格 JSON(前 %d 名, 只能从明细列表选)。' % min(3, max(1, len(detailed))))
    return '\n'.join(lines)


def parse_final(obj, allowed_hids, doctor_map, k=3):
    """校验 LLM 输出 => 有效 picks 列表(按 rank 序); 无效 => None。
    allowed_hids: 明细列表 hospital_id 集合; doctor_map: {hid: set(可约医生名)}。"""
    if not isinstance(obj, dict):
        return None
    picks = obj.get('final_picks')
    if not isinstance(picks, list) or not (1 <= len(picks) <= k):
        return None
    out, seen_h, seen_r = [], set(), set()
    for p in picks:
        if not isinstance(p, dict):
            return None
        hid = p.get('hospital_id')
        if hid not in allowed_hids or hid in seen_h:
            return None
        r = p.get('rank')
        if not isinstance(r, int) or not (1 <= r <= k) or r in seen_r:
            return None
        doc = p.get('doctor')
        if doc is not None:
            if not isinstance(doc, str) or not doc.strip():
                return None
            allowed_docs = doctor_map.get(hid)
            if allowed_docs is not None and doc not in allowed_docs:
                return None
            doc = doc.strip()
        else:
            doc = None
        seen_h.add(hid)
        seen_r.add(r)
        out.append({'rank': r, 'hospital_id': hid, 'doctor': doc,
                    'reason': (p.get('reason') or '') if isinstance(p.get('reason'), str) else ''})
    out.sort(key=lambda x: x['rank'])
    return out


def run_final(spec, inf, detailed, probe_summary, model='default', base=None, llm_caller=None, log=None, loc_text=''):
    """发起终裁 LLM 调用。成功 => {'picks','raw','prompt_chars','model_id'}; 失败/校验不过 => None。
    llm_caller(prompt) -> obj 可注入(离线测试); 缺省走 llm_client.chat_json(SYS, prompt)。"""
    log = log or (lambda *a: None)
    if not detailed:
        log('[final-llm] 明细列表为空(无深核已核实有号医院) -> 跳过终裁, 用确定性 top3')
        return None
    prompt = build_prompt(spec, inf, detailed, probe_summary, loc_text=loc_text)
    try:
        if llm_caller is None:
            obj, meta = llm_client.chat_json(SYS, prompt, model_id=model, base=base)
        else:
            obj = llm_caller(prompt)
            meta = None
    except Exception as e:
        log('[final-llm] 调用失败(%s: %s) -> 回退确定性 top3' % (type(e).__name__, str(e)[:160]))
        return None
    allowed = set(d['hospital_id'] for d in detailed)
    doctor_map = {d['hospital_id']: set(d.get('doctors') or []) for d in detailed}
    picks = parse_final(obj, allowed, doctor_map, k=3)
    if picks is None:
        log('[final-llm] 输出未通过校验(非 JSON/医院不在明细/医生名不存在/rank 非法) -> 回退确定性 top3; raw=%s'
            % json.dumps(obj, ensure_ascii=False)[:200])
        return None
    log('[final-llm] 终裁通过: %s (prompt=%d字, confidence=%s)'
        % ('; '.join('#%d %s%s' % (p['rank'], p['hospital_id'], ('[' + p['doctor'] + ']') if p['doctor'] else '')
                      for p in picks), len(prompt), (obj.get('confidence') if isinstance(obj, dict) else '?')))
    return {'picks': picks, 'raw': obj, 'prompt_chars': len(prompt),
            'model_id': (meta or {}).get('model_id') if isinstance(meta, dict) else None}
