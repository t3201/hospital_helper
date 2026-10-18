# -*- coding: utf-8 -*-
"""终排 = 两级排序（RANKING_SPEC §4.5, 2026-10-06 修订; 用户铁律: 有号是高权重独立特征）:
  ① 号源状态优先级: 已核实有号 > 未核实 > 已核实无号 ——
     不够好的医院/科室/距离但有号 > 最好的医院/科室/距离但无号（挂号推荐的目的就是找到可挂的号）;
  ② 同状态内 final = 静态分 × 号源效用。
  全未核实(fallback)时全部落在"未核实"状态 → 终排序 ≡ stage-1 静态序（§4.5 回落不变）。
输出: 1–3 个推荐（人话 why + 数值 evidence）+ Top-N 人工甄别列表（号源状态显式标注）。

**最终列表构造铁律（§4.1: 静分只决定候选集内顺序与抓取顺序, 绝不决定生死）**:
  * `finalize` 始终对**完整候选集 K** 构造 top_list, 绝不只排"未尝试的剩余部分";
  * 已核实有号(ok)    → 状态1, final = 静态分 × 号源效用（真实号源信号可升可降）;
  * 已核实无号(no_slot) → 状态3, final = 静态分 × 0.25（现在没号≠一直没号: 沉底但不剔除, 显式展示+写明原因）;
  * 未核实(未查询/档位先验/过期/stub/live error) → 状态2, final = 静态分（中性 u=1.0）, 仅标注。
  —— 未核实数据绝不参与"生死"判断, 只允许标注; 因此 stage-2 全失败时(fallback)
     终排序 ≡ stage-1 静态序（§4.5 回落）。
号源效用（启发式, 未调参, 全部带出处）:
  已核实 ok     : u = max(0.6, 1-0.03*earliest_days) × (1-0.4*tension); perennial_no_slot → ≤0.35
  已核实 no_slot: u = 0.25（仍展示, 显式标注）
  未核实(未查询/过期/档位/stub): 中性 u=1.0, 状态2内按静态分排序, 仅标注
slot_source 标注分类, 不许混: live / cache(observed 已核实) / cache(observed 已过期) /
  cache(level_default 档位先验) / stub(合成) / 未查询(无号源信息)。
"""
import json

FEE_VAL = {'15': 15, '20': 20, '25': 25, '50': 50, '100+': 120}
FEATURE_ZH = {
    'match': '科室命中', 'level': '医院等级', 'breadth': '科室齐全度',
    'distance': '距离就近', 'metro': '地铁可达', 'bus': '公交站点',
    'key_specialty': '特色科室加分', 'emergency': '急诊能力',
    'chronic': '慢病专病随访', 'chronic_fit': '分级诊疗承接', 'age_fit': '人群适配(儿科能力)', 'price': '费用友好度',
}


def _src_label(info):
    """slot_source 标注: 区分 live / cache(observed) / cache(level_default 档位先验) / stub / 未查询, 不许混。"""
    if not info:
        return '未查询(无号源信息)'
    src, d = info['slot_source'], info.get('detail')
    if src == 'live':
        return 'live/实时(已核实)'
    if src == 'live_probe':
        return 'live_probe/快速探测(已核实)'
    if src == 'cache':
        if d == 'level_default':
            return 'cache/level_default(档位先验,未核实)'
        if info.get('verified'):
            return 'cache/observed(已核实)'
        return 'cache/observed(已过期,未核实)'
    return 'stub/合成(未核实)'


def slot_utility(info, max_wait_days, price_cap_cny, price_w=0.0):
    """返回 (u, marks, violations)。price_w = LLM 的 price(费用友好度) 权重分量。
    铁律: 只有**已核实**号源才允许改变排序权重(u≠1); 未核实(含未查询/档位先验/过期/stub)
    一律中性 u=1.0, 仅以静态分参与排序并标注 —— 未核实数据不决定生死（§4.1）, 只允许标注。
    2026-10-06: finalize 层在本轮含已核实号源时对未核实候选追加未核实折扣(u×0.99)——详见 finalize。
    price 因子同样只对已核实号源生效: 预算内费档占比 p_in ∈ [0,1] → u ×= (1+price_w*(2*p_in-1)),
    钳制在 [0.85, 1.15]（price_w=0.1 时最多 ±10%）。"""
    u, marks, violations = 1.0, [], []
    if info is None:
        marks.append('未查询: 本轮未对该院发起号源查询(预算), 无号源信息, 按静态分排序')
        return u, marks, violations
    verified = info.get('verified')
    status = info.get('status', 'unknown')
    prov = info.get('provenance', {})
    if verified:
        if status == 'ok':
            ed = info.get('earliest_days') or 0
            u = max(0.6, 1.0 - 0.03 * ed)
            t = info.get('tension')
            if isinstance(t, (int, float)):
                u *= (1.0 - 0.4 * max(0.0, min(1.0, t)))
            if info.get('perennial_no_slot'):
                u = min(u, 0.35)
            marks.append('已核实有号（captured_at=%s）' % prov.get('captured_at'))
            if ed > (max_wait_days or 0):
                violations.append('最早可约 %d 天, 超出可接受等待 %d 天' % (ed, max_wait_days or 0))
        elif status == 'no_slot':
            u = 0.25
            marks.append('已核实无号（captured_at=%s）' % prov.get('captured_at'))
        else:
            marks.append('已核实抓取但状态 unknown（无数据）')
    else:
        # 已尝试但未核实: 中性 u=1.0(按静态分排序), 仅标注来源与原因, 不当作号源事实
        d = info.get('detail')
        if d == 'level_default':
            marks.append('号源未核实: 档位先验 level_defaults（stratum=%s, confidence=%s, 非实时, 不当作号源事实）'
                         % (prov.get('stratum_key'), info.get('confidence')))
        elif d in ('observed',):
            marks.append('号源未核实: 观察已过期（captured_at=%s, 龄期 %s 分钟 > TTL %s 分钟）'
                         % (prov.get('captured_at'), prov.get('age_min'), prov.get('ttl_min')))
        else:
            marks.append('号源未核实: stub 合成兜底（非实时数据, 不当作号源事实）')
    fee = FEE_VAL.get(info.get('fee_tier'))
    fee_min = FEE_VAL.get(info.get('fee_min_tier')) if info.get('fee_min_tier') else None
    eff_fee = fee_min if (fee_min is not None and (fee is None or fee_min < fee)) else fee
    if price_cap_cny and eff_fee and eff_fee > price_cap_cny:
        est = '' if verified else '（%s, 未核实）' % _src_label(info)
        violations.append('费用档 %s（≈%d元%s）超出预算 %d 元' % (info.get('fee_tier'), eff_fee, est, price_cap_cny))
    if price_w and price_cap_cny and info:
        ft = info.get('fee_tiers') or {}
        if verified and status == 'ok' and ft:
            p_in = sum(v for k, v in ft.items() if k in FEE_VAL and FEE_VAL[k] <= price_cap_cny)
            if p_in >= 1.0:
                marks.append('性价比(已核实): 可核实费档全部在预算内（≤%d元）' % price_cap_cny)
            elif p_in <= 0.0:
                marks.append('性价比(已核实): 可核实费档全部超预算（>%d元）, 已降权' % price_cap_cny)
            else:
                u = max(0.85, min(1.15, u * (1.0 + price_w * (2.0 * p_in - 1.0))))
                marks.append('性价比(已核实): 预算内(≤%d元)费档占比 %.0f%%, price权重=%s' % (
                    price_cap_cny, 100.0 * p_in, round(price_w, 3)))
        elif (not verified) and ft:
            p_s = sum(v for k, v in ft.items() if k in FEE_VAL and FEE_VAL[k] <= price_cap_cny)
            if 0.0 < p_s < 1.0:
                marks.append('性价比(未核实, 仅标注): 该档位/观察样本中预算内(≤%d元)费档占 %.0f%%'
                             '（未核实, 不参与排序）' % (price_cap_cny, 100.0 * p_s))
    return round(u, 4), marks, violations


def _slot_fact(r):
    if r.get('earliest_date'):
        return '最早可约 %s' % r['earliest_date']
    if r.get('slot_status') == 'ok':
        return '有号（具体最早日未核实）'
    return '号源状态=%s' % r.get('slot_status')


def _why(r, inf):
    parts = []
    for t in (r.get('top3_contrib') or [])[:2]:
        parts.append('%s=%s（+%s, %s）' % (FEATURE_ZH.get(t['feature'], t['feature']),
                                           (t.get('note') or '')[:30], t['contrib'], t['feature']))
    static_txt = '，'.join(parts) if parts else '静态分 %s' % r['static_score']
    if r['verified'] and r['slot_status'] == 'ok':
        slot_txt = '号源已核实: %s（来源 %s）' % (_slot_fact(r), r['slot_source'])
    elif r['verified']:
        slot_txt = '号源已核实: %s（来源 %s）' % (_slot_fact(r), r['slot_source'])
    else:
        slot_txt = '号源未核实（%s）, 仅按静态分排序, 不得当实时事实' % r['slot_source']
    why = '%s（%s/%s）: %s。%s。' % (r['name'], r['level'], r['district'], static_txt, slot_txt)
    if r['violations']:
        why += ' 注意: ' + '；'.join(r['violations'])
    return why


def _evidence(r, inf, slotrec):
    best = (slotrec or {}).get('best')
    si = None
    if best:
        si = {k: best.get(k) for k in
              ('slot_source', 'detail', 'status', 'verified', 'earliest_date', 'earliest_days',
               'fee_tier', 'fee_tiers', 'fee_min_tier', 'tension', 'perennial_no_slot',
               'days14_has', 'days14_total', 'provenance', 'note')}
    return {
        'static_score': r['static_score'], 'slot_utility': r['slot_utility'],
        'final_score': r['final_score'],
        'top3_contrib': r.get('top3_contrib', []),
        'deductions': r.get('deductions', []),
        'slot_info': si,
        'constraints': {'max_wait_days': inf['max_wait_days'], 'price_cap_cny': inf['price_cap_cny']},
        'violations': r['violations'],
    }


def _excl_reason(r, top_n):
    """显式排除原因: 必须回答'为什么'——号源状态/效用/终分, 而不只是'落在 TopN 之外'。"""
    st = r.get('slot_status')
    if r.get('verified') and st == 'no_slot':
        bdc, nr = r.get('bookable_doctor_count'), r.get('n_records')
        extra = ''
        if bdc and not nr:
            extra = ', 日期条显示 %d 名可约医生但医生页无可约记录' % bdc
        head = '实查无可约号源(no_slot%s) → 号源效用=0.25, 号源状态优先级沉底(现在没号≠一直没号, 保留在完整排序)' % extra
    elif r.get('verified'):
        head = '实查有号(最早=%s)但被更多已核实有号医院排挤出 Top%d' % (r.get('earliest_date') or '—', top_n)
    else:
        head = '号源未核实(%s) → 号源状态优先级排在已核实有号医院之后' % r.get('slot_source')
    return '%s → 终分=%s, 终排#%d（落在 Top%d 展示范围之外; 仍保留在候选集完整排序中, 未剔除）' % (
        head, r.get('final_score'), r.get('final_rank'), top_n)


def finalize(candidates, slot_by_hid, inf, top_n=20):
    """对**完整候选集 K** 构造最终列表（§4.1 静分不决定生死; §4.5 全失败回落静态序）。
    - top_list = 全 K 行按 final_score 排序取前 top_n（并列按静态名次 tie-break,
      保证全部未核实时终排序 ≡ stage-1 静态序）;
    - excluded = 已查询但排在 top_n 之外的医院（显式"已排除+原因"清单, 不得静默消失）;
    - recommendations = 无约束冲突且非"已核实无号"的前 3 名。"""
    # 2026-10-06: 未核实折扣——本轮含已核实号源事实时, 未核实候选 u×0.99(1% 让位)。
    # 仍不违反铁律: 1% 远小于已核实 ok 的 3%/天衰减与 no_slot 的 0.75 差距, 不决定生死,
    # 只在"未核实 vs 已核实有号"的毫厘间让位; 全未核实轮 any_verified=False → 全部 1.0, §4.5 回退静态序不变。
    any_verified = any(((rec.get('best') or {}).get('verified')) for rec in slot_by_hid.values())
    rows = []
    for c in candidates:
        hid = c['hospital_id']
        rec = slot_by_hid.get(hid) or {}
        info = rec.get('best')
        u, marks, violations = slot_utility(info, inf['max_wait_days'], inf['price_cap_cny'],
                                            price_w=(inf.get('weights') or {}).get('price', 0.0))
        if any_verified and not (info and info.get('verified')):
            u = round(u * 0.99, 4)
            marks.append('未核实折扣: 本轮含已核实号源 → 未核实 1% 让位(不影响生死判定)')
        rows.append({
            'rank': c['rank'], 'hospital_id': hid, 'name': c['name'], 'level': c['level'],
            'type': c['type'], 'district': c['district'], 'insurance': c['insurance'],
            'static_score': c['score'], 'slot_utility': u,
            'final_score': round(c['score'] * u, 4),
            'slot_source': _src_label(info),
            'verified': bool(info and info.get('verified')),
            'queried': hid in slot_by_hid,
            'static_rank': c['rank'],
            'slot_status': info.get('status') if info else None,
            'earliest_date': info.get('earliest_date') if info else None,
            'fee_tier': info.get('fee_tier') if info else None,
            'fee_tiers': info.get('fee_tiers') if info else None,
            'fee_min_tier': info.get('fee_min_tier') if info else None,
            'fee_from_stale': bool(info.get('fee_from_stale')) if info else None,
            'bookable_doctor_count': info.get('bookable_doctor_count') if info else None,
            'n_records': info.get('n_records') if info else None,
            'dept_id': info.get('dept_id') if info else None,
            'dept': info.get('dept_name') if info else None,
            'marks': marks, 'violations': violations,
            'top3_contrib': c.get('top3_contrib', []),
            'deductions': c.get('deductions', []),
        })
    # 2026-10-06 两级排序(用户铁律, 9dc8 教训): ① 号源状态优先级 已核实有号(0) > 未核实(1) > 已核实无号(2),
    # ② 同状态内 -final_score, static_rank tie-break。有号是高权重独立特征:
    # 不够好但有号 > 最好但无号; 全未核实轮全部状态1 → 回落静态序(§4.5 不变)。
    def _slot_tier(rr):
        if rr.get('verified'):
            return 0 if rr.get('slot_status') == 'ok' else 2
        return 1
    rows.sort(key=lambda r: (_slot_tier(r), -r['final_score'], r['static_rank']))
    for i, r in enumerate(rows, 1):
        r['final_rank'] = i
    top_list = rows[:top_n]
    top_ids = set(r['hospital_id'] for r in top_list)
    excluded = []
    for r in rows[top_n:]:
        if r['queried']:
            excluded.append({
                'hospital_id': r['hospital_id'], 'name': r['name'],
                'reason': _excl_reason(r, top_n),
            })
    elig = [r for r in rows if not r['violations']
            and not (r['verified'] and r['slot_status'] == 'no_slot')]
    recs = []
    for r in elig[:3]:
        r['why'] = _why(r, inf)
        r['evidence'] = _evidence(r, inf, slot_by_hid.get(r['hospital_id']))
        recs.append(r)
    return {
        'recommendations': recs,
        'top_list': top_list,
        'excluded': excluded,
        'slot_tiers': {'ok': sum(1 for r in rows if r['verified'] and r['slot_status'] == 'ok'),
                       'unknown': sum(1 for r in rows if not r['verified']),
                       'no_slot': sum(1 for r in rows if r['verified'] and r['slot_status'] != 'ok')},
        'fallback_all_unverified': not any(r['verified'] for r in rows),
        'n_verified': sum(1 for r in rows if r['verified']),
        'n_total': len(rows),
        'top_n': top_n,
    }


def apply_llm_picks(final, slot_by_hid, inf, llm_result):
    """Phase C: 用终裁 LLM 的 picks 替换 final['recommendations'](2026-10-06 两阶段架构)。
    校验: 医院必须在本轮 top_list 内, 且不得为已核实无号(铁律: 已核实无号沉底不可推荐);
    医生名必须来自深核名单(parse_final 已校验, 此处防御性再查)。
    无任何有效 pick => recommendations 保持 finalize 的确定性 top3(回退)。"""
    rows_by_hid = {}
    for r in final.get('top_list', []):
        rows_by_hid[r['hospital_id']] = r
    recs = []
    for p in llm_result.get('picks', []):
        hid = p.get('hospital_id')
        r = rows_by_hid.get(hid)
        if r is None:
            continue
        sinfo = (slot_by_hid.get(hid) or {}).get('best') or {}
        if sinfo.get('verified') and sinfo.get('status') == 'no_slot':
            continue   # 终裁 LLM 幻觉防护: 已核实无号的医院不得进推荐
        rr = dict(r)
        rr['llm_final'] = True
        rr['llm_rank'] = p.get('rank')
        if p.get('doctor'):
            rr['llm_doctor'] = p['doctor']
        rr['llm_reason'] = p.get('reason') or ''
        rr['why'] = _why(rr, inf) + ' 【终裁LLM: %s】' % (p.get('reason') or '')
        rr['evidence'] = _evidence(rr, inf, slot_by_hid.get(hid))
        recs.append(rr)
    if not recs:
        return final
    recs.sort(key=lambda r: (r.get('llm_rank') or 99))
    final['recommendations'] = recs[:3]
    raw = llm_result.get('raw') or {}
    final['final_llm'] = {'applied': True,
                          'confidence': raw.get('confidence') if isinstance(raw, dict) else None,
                          'notes': raw.get('notes') if isinstance(raw, dict) else None,
                          'model_id': llm_result.get('model_id')}
    return final


def validate_result(result):
    """硬回归断言（`--check` 与每次 pipeline 末尾必跑; 任一失败 → 调用方非零退出）。
    针对 B4 缺陷（最终列表系统性剔除"已查询的静态分最高 12 家", 永远推荐第 13~32 名）:
    A. 已解析号源的医院不得静默消失: ∈ final.top_list ∪ final.excluded(带明确原因, 含号源状态);
       （已核实 no_slot → 效用 0.25 降出 Top20 属合理结果, 但必须显式列出并写明原因; 未解析的预算内医院
       按 u=1.0 中性处理, 终排=静态名次≤12 恒在 Top20 内, 天然满足）;
    B. fallback_all_unverified 时, final.top_list 前缀与 candidates_static 同序（§4.5 回落）;
    C. 静态分最高的医院可见: 在 top_list, 或显式排除(带原因)（不得无声消失）;
    D. 每个进入 slot_queries 的 hospital_id 必须在 final.top_list, 否则必须在 final.excluded
       显式"已排除+原因"清单里（不得静默消失）。
    """
    cands = result.get('candidates_static') or []
    final = result.get('final') or {}
    top = final.get('top_list') or []
    queries = result.get('slot_queries') or []
    n_budget = min(len(queries), len(cands))
    top_ids = [r['hospital_id'] for r in top]
    top_id_set = set(top_ids)
    excl = final.get('excluded') or []
    excl_ids = set(e['hospital_id'] for e in excl)
    checks = []

    excl_with_reason = {e['hospital_id'] for e in excl if (e.get('reason') or '').strip()}
    resolved = set((result.get('slots') or {}).keys())
    if not resolved:
        resolved = {q['hospital_id'] for q in queries}
    missing = sorted(resolved - top_id_set - excl_with_reason)
    checks.append({'id': 'A',
                   'name': '已解析号源的 %d 家医院不得静默消失: ∈ final.top_list ∪ final.excluded(带原因, 含号源状态)' % len(resolved),
                   'ok': not missing, 'detail': '缺失=%s' % missing})

    if final.get('fallback_all_unverified') and cands:
        m = min(len(top_ids), len(cands))
        same = top_ids[:m] == [c['hospital_id'] for c in cands[:m]]
        checks.append({'id': 'B',
                       'name': 'fallback_all_unverified → top_list 前 %d 项与静态序同序（§4.5 回落）' % m,
                       'ok': same,
                       'detail': '' if same else '前%d项=%s vs 静态=%s' % (
                           m, top_ids[:m], [c['hospital_id'] for c in cands[:m]])})

    if cands:
        max_c = max(cands, key=lambda c: c['score'])
        ok = max_c['hospital_id'] in top_id_set or max_c['hospital_id'] in excl_with_reason
        checks.append({'id': 'C',
                       'name': 'max(静态分)医院可见: %s(%.4f) 在 top_list 或 显式排除(带原因); no_slot 降级属合理结果但必须写明' % (max_c['hospital_id'], max_c['score']),
                       'ok': ok, 'detail': '' if ok else '%s 既不在 top_list 也不在 excluded' % max_c['hospital_id']})

    bad = [q['hospital_id'] for q in queries
           if q['hospital_id'] not in top_id_set and q['hospital_id'] not in excl_ids]
    no_reason = [e['hospital_id'] for e in excl if not (e.get('reason') or '').strip()]
    ok = not bad and not no_reason
    checks.append({'id': 'D',
                   'name': 'slot_queries 的每家医院 ∈ final.top_list ∪ final.excluded（显式排除+原因, 不得静默消失）',
                   'ok': ok, 'detail': '' if ok else '未出现=%s 排除无原因=%s' % (bad, no_reason)})
    return checks


def render_report(meta, loc, spec, inf, res, slot_by_hid, final, booking, queries, checks=None):
    L = []
    ap = L.append
    ap('# 挂号推荐报告（运行期原型）')
    ap('')
    ap('- 生成: %s | 模式: %s | 权重推理: %s' % (meta['generated_at'], meta['mode'], meta['llm']))
    ap('')
    ap('## 1. 位置')
    ap('- `%s, %s`（%s）— %s' % (loc['lng'], loc['lat'], loc.get('crs'), loc.get('note', '')))
    ap('')
    ap('## 2. 病情采集（≤3 轮自由文本）')
    for i, a in enumerate(spec.get('_raw', []), 1):
        if a:
            ap('- 第%d轮: %s' % (i, a))
    p = spec['patient']
    ap('- 解析: 患者=%s(age=%s, 性别=%s) 代挂=%s 就诊=%s 等待≤%d天 费用上限=%s 水平预期=%s' % (
        p['age_band'], p['age'], {'male': '男', 'female': '女'}.get(p.get('gender'), '未提供'), p['proxy'], spec['visit_type'], spec['urgency_days'],
        spec['prefs']['price_cap_cny'], spec['prefs']['level_expectation']))
    ap('')
    ap('## 3. 权重推理（LLM 直出: 特征权重向量 + 目标科室 + 约束; 含基础数据上下文）')
    ap('- scenario: **%s**（%s）' % (inf['scenario'], inf.get('scenario_zh', '')))
    lm = inf.get('llm_meta')
    if lm:
        ap('- LLM 后端: %s model_id=%s @ %s; 实测耗时=%sms（首字节见日志）attempts=%s 思维链=%s字（仅记长度, 未并入答案）'
           % (lm.get('provider'), lm.get('model_id'), lm.get('base'), lm.get('elapsed_ms'),
              lm.get('attempts'), lm.get('thinking_chars')))
    cxt = inf.get('context')
    if cxt:
        ap('- 基础数据上下文(随 prompt 下发, 全部只读): 可约科室 canonical %d 条(覆盖≥%d 家医院) / 医院目录 %d 家(等级/区县); 场景基线权重 5 套(仅参考)' % (cxt.get('n_depts', 0), cxt.get('dept_hc_min', 0), cxt.get('n_hospitals', 0)))
    for r in inf['reasoning']:
        ap('- %s' % r)
    ap('- 特征权重向量（LLM 直出绝对权重, 归一行和=1.0; 5 套场景基线仅作 prompt 参考）: %s' % json.dumps(res.get('applied_weights', {}), ensure_ascii=False))
    # 显示用 LLM 原始输出(含 price); applied_weights 是 stage-1 用的前 10 维重归一版
    _w = inf.get('weights') or res.get('applied_weights') or {}
    if _w:
        ap('- 权重分量中文名: %s' % ' | '.join('%s(%s)=%s' % (FEATURE_ZH.get(k, k), k, v) for k, v in _w.items()))
    ap('- match(科室命中)说明: 该医院命中目标科室的质量 = 命中科室中 层级(tier)×置信度 的最大值'
      '（tier1=首选→1.0, tier2=相关→0.70, tier3=弱相关→0.45; 0=未命中; 弱相关科室不再与首选平权, 层级由 LLM 按症状直接指向程度给出）。match 权重越高, 候选排序越优先保证"有对的科室"。')
    if (inf.get('weights') or {}).get('age_fit'):
        ap('- age_fit(人群适配)=%s: 儿科病例中"有儿科能力"(可约儿科类科室/儿童机构)的医院=1.0, 无=中性0.5(叠加加分非惩罚); 非儿科病例恒1.0' % inf['weights']['age_fit'])
    if _w.get('price') is not None:
        ap('- price(费用友好度)=%s: 终排性价比偏好权重, 只对**已核实**号源生效'
      '（可核实费档中预算内占比 p_in → u×(1+price·(2p_in-1)), 钳制[0.85,1.15]）; '
      '未核实仅标注档位先验的预算内占比, 不参与排序（铁律: 未核实不定生死）。' % _w['price'])
    ap('- 约束: max_wait_days=%s, price_cap_cny=%s' % (inf['max_wait_days'], inf['price_cap_cny']))
    ap('')
    ap('## 4. 候选集（stage-1, 复用 ranking/scripts/score_hospitals.py）')
    t = res['triage']
    ap('- triage: 主词="%s"（%s）→ 目标科室: %s' % (
        t['term'], t['how'],
        '; '.join('%s(p%d/%s)' % (x['canonical'], x['priority'], x['confidence']) for x in t['targets'][:5])))
    pf = res.get('patient_filter')
    if pf:
        ap('- 患者层科室过滤: %s' % pf['note'])
        for d in pf.get('dropped_targets', []):
            ap('  - 剔除: %s（%s, p%d/%s）' % (d['canonical'], d['dept_id'], d['priority'], d['confidence']))
    hf = res['hard_filter']
    ap('- 硬过滤: 召回=%d → 通过=%d（弃=%d: %s）, 半径=%skm' % (
        hf['recall'], hf['passed'], hf['dropped'],
        json.dumps(hf['drop_reasons'], ensure_ascii=False), hf['radius_km']))
    ap('- K=%s（肩部规则: %s）' % (res['K'], json.dumps(res['K_rule'], ensure_ascii=False)))
    ap('')
    ap('| # | 医院 | 等级 | 区 | 静态分 | 首要贡献 |')
    ap('|---|---|---|---|---|---|')
    for c in res['candidates'][:10]:
        top = c['top3_contrib'][0] if c['top3_contrib'] else {}
        ap('| %d | %s | %s | %s | %s | %s=%s（%s） |' % (
            c['rank'], c['name'], c['level'], c['district'], c['score'],
            FEATURE_ZH.get(top.get('feature'), top.get('feature', '')),
            top.get('v'), (top.get('note') or '')[:24]))
    ap('')
    ap('## 5. 号源（缓存优先: profile → 观察快照 → 档位 → stub; TTL=90min）')
    ap('| 医院 | 科室 | 来源 | 状态 | 时间戳 | 龄期(min) | 标注 |')
    ap('|---|---|---|---|---|---|---|')
    for c in res['candidates']:
        rec = slot_by_hid.get(c['hospital_id'])
        if not rec:
            continue
        b = rec['best']
        prov = b.get('provenance', {})
        ap('| %s | %s | %s | %s | %s | %s | %s |' % (
            c['name'], b.get('dept_name') or '?', _src_label(b), b.get('status'),
            prov.get('captured_at') or '—', prov.get('age_min') if prov.get('age_min') is not None else '—',
            (b.get('note') or '')[:48]))
    ap('')
    ap('## 6. 推荐（1–3, 两级排序: ① 号源状态 已核实有号>未核实>已核实无号 ② 同状态 终排=静态分×号源效用）')
    if final['fallback_all_unverified']:
        ap('> ⚠ 本批无可核实实时号源（抓取失败/观察过期/仅档位与stub）→ 按 RANKING_SPEC §4.5 回落到 stage-1 静态排序展示, 全部显式标注"号源未核实", 不假装实时。')
        ap('')
    for i, r in enumerate(final['recommendations'], 1):
        route = ''
        if r.get('dept_tab'):
            route = ' — 挂号入口: 「%s」tab → 点「%s」行' % (r['dept_tab'], r.get('dept_raw') or r.get('dept') or '')
        ap('### %d. %s（%s / %s）%s' % (i, r['name'], r['level'], r['district'], route))
        ap('- **why**: %s' % r['why'])
        ap('- **evidence**: 静态=%s × 号源效用=%s → 终排=%s' % (r['static_score'], r['slot_utility'], r['final_score']))
        for t2 in r['top3_contrib']:
            ap('  - +%.3f %s v=%s（%s）' % (t2['contrib'], t2['feature'], t2['v'], t2['note']))
        for d2 in (r['deductions'] or [])[:2]:
            ap('  - -%.3f %s 失分（%s）' % (d2['lost'], d2['feature'], d2['note']))
        si = r['evidence'].get('slot_info')
        if si:
            ap('  - 号源: %s | status=%s | fee=%s | 出处=%s' % (
                _src_label(si), si.get('status'), si.get('fee_tier'), si.get('provenance', {}).get('file')))
            if si.get('fee_tiers'):
                _ft = ', '.join('%s(%.0f%%)' % (k, 100.0 * v) for k, v in sorted(si['fee_tiers'].items(), key=lambda kv: -kv[1])[:5])
                ap('    费档分布: %s%s' % (_ft, ('; 最低档=%s' % si['fee_min_tier']) if si.get('fee_min_tier') else ''))
            ap('    %s' % (si.get('note') or ''))
        for mk in r['marks']:
            ap('  - 标注: %s' % mk)
        if r['violations']:
            ap('  - ⚠ 约束: %s' % '；'.join(r['violations']))
        ap('')
    if not final['recommendations']:
        ap('（无满足约束的推荐目标；请见下方 Top 列表人工甄别, 全部显式标注"号源未核实"。）')
        ap('')
    n = len(final['top_list'])
    ap('## 7. Top%d 人工甄别列表（未核实号源显式标注; 全候选集 K=%d 参与排序, 不剔除已查询者）' % (n, final.get('n_total', n)))
    ap('| 终排# | 医院 | 等级 | 区 | 静态 | 号源效用 | 终排 | 号源来源 | 最早可约 |')
    ap('|---|---|---|---|---|---|---|---|---|')
    for r in final['top_list']:
        ap('| %d | %s | %s | %s | %s | %s | %s | %s | %s |' % (
            r['final_rank'], r['name'], r['level'], r['district'], r['static_score'],
            r['slot_utility'], r['final_score'], r['slot_source'], r.get('earliest_date') or '—'))
    if final.get('excluded'):
        ap('')
        ap('**已排除（Top%d 之外, 显式清单+原因）**: ' % n + '; '.join(
            '%s（%s）' % (e['hospital_id'], e['reason']) for e in final['excluded']))
    unv = [r for r in final['top_list'] if not r['verified']]
    ap('')
    ap('> Top%d 中 %d 家号源未核实（上表已逐行标注）；本报告不给出任何无出处的号源结论。' % (n, len(unv)))
    ap('')
    ap('## 8. 模拟挂号（dry-run, 默认开）')
    if booking:
        for s in booking.get('steps', []):
            ap('- %s' % s)
        ap('')
        ap('> %s' % booking.get('note', ''))
    else:
        ap('- （跳过: --no-book 或无推荐目标）')
    ap('')
    ap('## 9. 数据与已知局限')
    ap('- 特征权重向量为 LLM 直出（prompt 附 5 套场景基线权重表作参考; 槽位效用系数等启发式仍未调参, 见 RANKING_SPEC §3/§7）, 非个性化医疗建议。')
    ap('- 号源 TTL=90min；过期观察仅作陈旧参考, 不计入已核实；档位/stub 恒标注未核实; **未核实一律中性效用, 不决定生死**。')
    ap('- 症状原文（含否定表述）直接交给 LLM 理解; 旧规则引擎的关键词匹配与否定抑制已随 --llm rules 废除。')
    ap('- 患者层科室过滤: 成人剔除儿科类目标科室, 儿童剔除成人科目标科室（app 层后处理, 词表本身不改）。')
    ap('- 目标科室 = LLM 从归一化词表（canonical, 覆盖≥5 家医院）直选; 无效条目回落到 LLM triage_term → 症状词表解析; 词表外词经同义词桥映射, 映射不上如实标注, 不编造科室。Agent Service 不可用 → 运行失败退出（不静默回退）。')
    ap('- 原型不真实挂号/提交/支付（450 家 capabilities.book=false）; 不修改 ranking/、hospital_intel/、ssb_dept/、slots/。')
    ap('- key_specialty 覆盖 4.4%、metro 172/450 缺失 → 中性策略并已在 explain 中标注。')
    ap('')
    if checks:
        ap('## 10. 回归断言（--check 同款, 任一失败进程非零退出）')
        for c in checks:
            ap('- **%s** %s%s' % ('PASS' if c['ok'] else 'FAIL', c['name'],
                                  (' — ' + c['detail']) if (c['detail'] and not c['ok']) else ''))
        ap('')
    ap('## 附: SlotQuery 序列（§4.3 契约, 仅生成不发起）')
    for q in queries[:12]:
        ap('- #%d %s depts=%s wait≤%s天 cap=%s static=%s' % (
            q['order'], q['hospital_id'], q['dept_ids'], q['max_wait_days'],
            q['price_cap_cny'], q['static_score']))
    return '\n'.join(L) + '\n'
