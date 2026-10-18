# -*- coding: utf-8 -*-
"""候选集：import ranking/scripts/score_hospitals.py（stage-1, 复用不重写）,
在其原语上编排。两个入口:
  * run_llm()（2026-10-04 新契约, main 默认路径）: LLM 直出**绝对权重向量** +
    **归一化词表内直选的目标科室**（无效/缺省时由 LLM triage_term 走症状词表解析）;
  * run()（旧契约, 保留）: 支持 weight_overrides（临时替换 SCENARIOS[scenario] 权重表,
    用完还原）+ 症状词表 triage。
两者都把 fetch_plan 的 stub 升级为 RANKING_SPEC §4.3 正式 SlotQuery 序列。

**患者层科室过滤（app 层后处理, 词表/字典只读不改）**:
  成人/老年患者剔除儿科类目标科室; 儿童患者剔除成人科目标科室（妇科/产科/孕产/男科/老年 等）。
  过滤后**用 stage-1 原语重算** 召回→硬过滤→打分→K→fetch_plan（复用同一 score_hospital/hard_filters/pick_k,
  保证与 stage-1 完全同构）, 并在 res['patient_filter'] 中显式列出剔除了什么（explain 可见）。
"""
import hashlib
import importlib.util
import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(APP_DIR)
STAGE1_PATH = os.path.join(WS, 'ranking', 'scripts', 'score_hospitals.py')

# 儿科类科室标识（canonical 名包含其一 → 对成人/老年患者剔除）
PEDIATRIC_MARKS = ('儿科', '儿童', '小儿', '新生儿', '婴', '幼儿', '青少年', '儿保', '母婴')
# 成人科标识（canonical 名包含其一 → 对儿童患者剔除）
ADULT_ONLY_MARKS = ('妇科', '产科', '孕', '产妇', '产前', '产检', '产后', '妊娠', '更年期',
                    '不孕', '计划生育', '男科', '前列腺', '老年')
# 儿科机构标识（医院名包含其一 → 对成人/老年患者剔除该医院; 科室级过滤覆盖不了"儿科机构里的通用科室"）
PEDIATRIC_HOSP_MARKS = ('儿童医院', '儿童医学中心', '儿科医院', '复旦儿科')
# 妇儿机构标识（医院名包含其一 → 对成年男性患者剔除; 妇儿机构主要服务女性/儿童）
MATERNAL_HOSP_MARKS = ('妇婴', '妇儿', '妇幼保健', '妇产')

# 专科同族表（app 层只读扩展, 2026-10-06 二次修复）:
# canonical 碎片化使同一专科有多个 dept_id, 且名称变体的公共前缀可能不足 3 字或字序不同
# (耳鼻咽喉科=耳鼻咽喉 / 耳鼻咽喉科=耳鼻咽喉头颈外科 等), 前缀规则会错过。
# 每个家族: (id, core_stems, variant_suffixes, peripheral_stems)
#   core   徒房: 同一专科的命名变体 (stem + variant_suffix) → 与目标同 tier;
#   periph 徒房: 相邻/更宽专科 → tier+1; hc==1 的变体也降 tier+1 (防医生名干扰伪名字) 。
SPECIALTY_FAMILIES = (
    # (家族, core 房徒, 同 tier 变体后缀, 相邻专科房徒)
    ('耳鼻喉', ('耳鼻喉', '耳鼻咽喉'),
     ('科', '外科', '中心', '门诊', '团队', '病区', '病房', '室', '组', '头颈外科'),
     ('五官',)),
    ('骨科', ('骨科', '骨伤'),
     ('科', '中心', '门诊', '病区', '病房', '室', '组'),
     ('脊柱外科', '关节外科', '手足外科', '创伤正形', '运动医学', '骨肿瘤')),
    ('眼科', ('眼科',),
     ('科', '中心', '门诊', '病区', '病房', '室', '组'),
     ('眼视光',)),
    ('儿科', ('儿科', '小儿', '新生儿'),
     ('科', '外科', '中心', '门诊', '病区', '病房', '室', '组'),
     ('儿保',)),
    ('口腔', ('口腔',),
     ('科', '外科', '中心', '门诊', '病区', '病房', '室', '组'),
     ('牙体牙髓', '正形', '种植', '颌面', '修复', '美容')),
)


def _load_stage1_module():
    spec = importlib.util.spec_from_file_location('stage1_score_hospitals', STAGE1_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_SH = _load_stage1_module()
# 以 stage-1 引擎为单一数据源覆盖上方本地副本（避免两份标识表漂移）
PEDIATRIC_MARKS = _SH.PEDIATRIC_MARKS
PEDIATRIC_HOSP_MARKS = _SH.PEDIATRIC_HOSP_MARKS


def _target_summary(t):
    return {'dept_id': t['dept_id'], 'canonical': t['canonical'], 'kind': t['kind'],
            'priority': t['priority'], 'confidence': t['confidence'], 'hosp': len(t['hospitals']),
            'family_of': t.get('family_of')}


class Stage1:
    def __init__(self, verbose=True):
        self.sh = _SH  # 模块级单例（引擎只加载一次）
        self.lex, self.di, self.mans = self.sh.load_inputs()
        self.norms, _ = self.sh.load_or_build_norms(self.di, self.mans)
        # canonical → (dept_id, dept)（归一化名唯一, 1:1）—— LLM 直选科室的校验/建 target 索引
        self.canon_index = {d['canonical']: (kid, d) for kid, d in self.di['depts'].items()}
        if verbose:
            print('[stage1] 已复用 ranking/scripts/score_hospitals.py（import 模式）: '
                  '%d manifests / %d 科室 / %d 症状词'
                  % (len(self.mans), len(self.di['depts']), len(self.lex['terms'])))

    @staticmethod
    def _norm_weights(weights):
        """LLM 绝对权重向量 → 归一（行和=1.0）; 非法/空 → 抛错（不静默）。"""
        feat = {'match', 'level', 'breadth', 'distance', 'metro', 'bus',
                'key_specialty', 'emergency', 'chronic', 'chronic_fit', 'age_fit'}
        w = {k: float(v) for k, v in (weights or {}).items()
             if k in feat and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0}
        tot = sum(w.values())
        if tot <= 0:
            raise ValueError('LLM 权重向量缺失/非法: %r' % (weights or {}))
        return {k: v / tot for k, v in w.items()}

    def _targets_from_canonicals(self, pairs):
        """LLM 词汇直选的 canonical(+层级) -> stage-1 target 结构（tier→priority; kind 必须可约）。
        pairs 元素兼容: (canonical, tier) / {'canonical','tier'} / 裸 canonical(str, 旧契约 tier=1)。"""
        out = []
        for item in pairs or []:
            if isinstance(item, dict):
                name, tier = item.get('canonical') or item.get('dept'), item.get('tier', 1)
            elif isinstance(item, (list, tuple)) and item:
                name, tier = item[0], (item[1] if len(item) > 1 else 1)
            else:
                name, tier = item, 1
            if not isinstance(name, str):
                continue
            try:
                tier = int(tier)
            except (TypeError, ValueError):
                tier = 1
            tier = max(1, min(3, tier))
            hit = self.canon_index.get(name.strip())
            if hit is None:
                continue
            kid, d = hit
            if d['kind'] not in self.sh.ALLOWED_HIT_KINDS:
                continue
            out.append({'dept_id': kid,
                        'canonical': d['canonical'], 'kind': d['kind'],
                        'priority': tier, 'confidence': 'high',
                        'visit_types': d.get('visit_types', []),
                        'hospitals': set(d['hospitals'])})
        # 同族扩展(2026-10-06): canonical 碎片化使同一专科多 dept_id(耳鼻咽喉科/耳鼻咽喉头颈外科/耳鼻咽喉),
        # LLM 只能看到 hc>=5 的主 canonical, 族员以 tier+1(钳制 3) 追加保证召回; 词典只读不改。
        _seen = {t['canonical']: t for t in out}
        for t in list(out):
            for fam, fam_prio in self._family_member_prio(t['canonical'], t['priority']):
                if fam in _seen:
                    # LLM 已直选该同族成员: 只升不降(同族下限 = 主科室 tier+1)
                    if _seen[fam]['priority'] > fam_prio:
                        _seen[fam]['priority'] = fam_prio
                        _seen[fam]['family_of'] = t['canonical']
                    continue
                kid, d = self.canon_index[fam]
                if d['kind'] not in self.sh.ALLOWED_HIT_KINDS:
                    continue
                _seen[fam] = {'dept_id': kid, 'canonical': fam, 'kind': d['kind'],
                             'priority': fam_prio, 'confidence': 'high',
                             'visit_types': d.get('visit_types', []),
                             'hospitals': set(d['hospitals']), 'family_of': t['canonical']}
                out.append(_seen[fam])
        return out

    def _family_member_prio(self, name, prio):
        """同族 canonical 及其 tier (app 层只读扩展, 2026-10-06):
        返回 [(canon, new_prio), ...] = 精选专科同族表(SPECIALTY_FAMILIES) ∪ 前缀规则。
        core 同家名称变体 (stem+后缀, hc>=2) → 与目标同 tier; 其余同族成员 → tier+1 (钳制 3)。
        后缀规则也补充精选表未覆盖的同族变体。字典只读不改。"""
        out = {}
        fam_id, t_stem = self._family_info(name)
        if fam_id is not None:
            for c, _cd in self.canon_index.items():
                if c == name:
                    continue
                cid, c_stem = self._family_info(c)
                if cid != fam_id:
                    continue
                same = (t_stem is not None and c_stem is not None)
                if same:
                    rest = c[len(c_stem):]
                    if rest and rest not in self._fam_vsuffix(fam_id):
                        same = False  # 子专科后缀(内分泌/手术/保健...)≠同一科室
                    if same and len(self.canon_index[c][1]['hospitals']) < 2:
                        same = False  # hc==1 变体可能是医生名干扰伪名
                out[c] = prio if same else min(3, prio + 1)
        # 前缀规则(旧, tier+1): 其一为另一前缀, 或公共前缀 >= 3 字
        for cand in self.canon_index:
            if cand == name:
                continue
            is_pref = cand.startswith(name) or name.startswith(cand)
            if not is_pref:
                n = 0
                for a, b in zip(cand, name):
                    if a != b:
                        break
                    n += 1
                is_pref = n >= 3
            if not is_pref:
                continue
            p = min(3, prio + 1)
            if cand not in out or out[cand] > p:
                out[cand] = p
        return sorted(out.items())

    @staticmethod
    def _fam_vsuffix(fam_id):
        for fid, _c, v, _p in SPECIALTY_FAMILIES:
            if fid == fam_id:
                return v
        return ()

    @staticmethod
    def _family_info(name):
        """名称 → (家族 id, core 房徒 or None); 未命中精选家族 → (None, None)。"""
        for fid, cores, _v, periph in SPECIALTY_FAMILIES:
            for st in cores:
                if name.startswith(st):
                    return fid, st
        for fid, _c, _v, periph in SPECIALTY_FAMILIES:
            for st in periph:
                if st in name:
                    return fid, None
        return None, None
    def run_llm(self, term, llm_canonicals, scenario, loc, radius_km=None,
                weights=None, patient_band=None, dept_how='llm', patient_gender=None):
        """LLM 直出权重+科室入口（2026-10-04 新契约）: 与 stage-1 run_stage1 **完全同构**
        （复用同一批 recall/hard_filters/score_hospital/pick_k 原语）, 仅两处不同:
          * 目标科室 = LLM 词表直选（无效/缺省时由 LLM triage_term 走症状词表解析）;
          * 权重     = LLM 绝对权重向量（归一后直接替换 SCENARIOS 基线, 无乘子）。
        患者层科室过滤（成人/儿童互斥科室）与 run() 同一 _excluded_reason。"""
        sh = self.sh
        weights = self._norm_weights(weights)
        targets = self._targets_from_canonicals(llm_canonicals)
        how = dept_how
        if not targets:
            _t, _h, targets = sh.target_depts(term, self.lex['terms'], self.di)
            if targets:
                how = 'llm_term_lexicon'
        if not targets:
            return None

        pf, kept = {}, list(targets)
        if patient_band in ('adult', 'elderly', 'child'):
            kept, dropped = [], []
            for t in targets:
                why = self._excluded_reason(t['canonical'], patient_band)
                if why:
                    dropped.append((t, why))
                else:
                    kept.append(t)
            if dropped:
                band_zh = '儿童' if patient_band == 'child' else '成人/老年'
                pf = {'band': patient_band,
                      'dropped_targets': [{'dept_id': t['dept_id'], 'canonical': t['canonical'],
                                           'priority': t['priority'], 'confidence': t['confidence'],
                                           'reason': w} for t, w in dropped],
                      'kept_n': len(kept),
                      'note': ('%s患者剔除目标科室 %d 个: %s（剩余 %d 个, 已按过滤后目标同构重算'
                               '召回/打分/K/抓取计划; 词表与字典只读不改）'
                               % (band_zh, len(dropped),
                                  ', '.join('%s[%s]' % (t['canonical'], w) for t, w in dropped),
                                  len(kept)))}
                if not kept:
                    kept = list(targets)
                    pf['note'] += ' —— 目标科室全部被剔除, 保留原候选集（罕见, 建议人工处理）'

        h_excl = self._hospital_exclusions(patient_band, patient_gender)
        if h_excl:
            _names = ', '.join(self.norms['hospitals'][h]['name'] for h in sorted(h_excl)[:4])
            if pf:
                pf['note'] += ('; 另剔除患者不匹配的专科机构医院 %d 家: %s%s' % (
                    len(h_excl), _names, '...' if len(h_excl) > 4 else ''))
            else:
                pf = {'band': patient_band,
                      'note': '剔除患者不匹配的专科机构医院 %d 家: %s%s' % (
                          len(h_excl), _names, '...' if len(h_excl) > 4 else '')}
        rk = radius_km if radius_km is not None else sh.SCENARIO_RADIUS_DEFAULT[scenario]
        res = {'symptom': term, 'scenario': scenario,
               'loc': [float(loc[0]), float(loc[1])]}
        res = self._rebuild_with_targets(res, term, how, kept, scenario,
                                         (float(loc[0]), float(loc[1])), rk, weights, pf,
                                         hospital_excl=h_excl)
        res['applied_weights'] = {k: round(v, 4) for k, v in weights.items()}
        res['weights'] = res['applied_weights']
        return res

    def _hospital_exclusions(self, band, gender):
        """医院级患者层过滤: 返回 {hid: reason}（成人/老年 剔儿科机构; 成年男性 另剔妇儿机构）。
        科室级过滤只看 canonical 名, 覆盖不了"儿科/妇儿机构里开设的通用科室"（如儿童医院口腔科）。"""
        out = {}
        if band not in ('adult', 'elderly'):
            return out
        for hid, rec in self.norms['hospitals'].items():
            nm = rec.get('name') or ''
            hit = next((m for m in PEDIATRIC_HOSP_MARKS if m in nm), None)
            if hit:
                out[hid] = '成人/老年患者不适用于儿科机构(名称含"%s")' % hit
                continue
            if gender == 'male':
                hit2 = next((m for m in MATERNAL_HOSP_MARKS if m in nm), None)
                if hit2:
                    out[hid] = '成年男性患者不适用于妇儿机构(名称含"%s")' % hit2
        return out

    def _excluded_reason(self, canonical, band):
        if band in ('adult', 'elderly'):
            hit = next((m for m in PEDIATRIC_MARKS if m in canonical), None)
            return ('成人/老年患者不适用于儿科类科室（canonical 含"%s"）' % hit) if hit else None
        if band == 'child':
            hit = next((m for m in ADULT_ONLY_MARKS if m in canonical), None)
            return ('儿童患者不适用于成人科（canonical 含"%s"）' % hit) if hit else None
        return None

    def _rebuild_with_targets(self, res, term, how, targets, scenario, loc, radius_km,
                              weights, patient_filter, hospital_excl=None):
        """与 stage-1 run_stage1 完全同构的流水线, 仅用**过滤后**的目标科室（复用同一批原语）。"""
        sh = self.sh
        norms = self.norms
        recall = set()
        for t in targets:
            if t['kind'] in sh.ALLOWED_HIT_KINDS:
                recall |= t['hospitals']
        recall = sorted(recall & set(norms['hospitals'].keys()))
        universe, filtered = {}, {}
        for hid in recall:
            if hospital_excl and hid in hospital_excl:
                filtered[hid] = 'PATIENT_BAND_HOSPITAL: ' + hospital_excl[hid]
                continue
            rec = norms['hospitals'][hid]
            hit = any(hid in t['hospitals'] for t in targets if t['kind'] in sh.ALLOWED_HIT_KINDS)
            ok, reason = sh.hard_filters(hid, rec, radius_km, True, {'loc': loc, 'hit': hit})
            if ok:
                score, contribs = sh.score_hospital(hid, rec, weights, loc, targets, scenario)
                universe[hid] = (score, contribs, rec)
            else:
                filtered[hid] = reason
        ranked = sorted(universe.items(), key=lambda kv: (-kv[1][0], kv[0]))
        scores_desc = [s for _, (s, _, _) in ranked]
        K, k_info = sh.pick_k(scores_desc)
        candidates = ranked[:K]
        overflow = ranked[K:]
        fetch_plan = []
        for rank, (hid, (s, _, rec)) in enumerate(ranked[:min(sh.BUDGET_FETCH_DEFAULT, len(ranked))], 1):
            fetch_plan.append({
                'order': rank, 'hospital_id': hid, 'name': rec['name'], 'static_score': round(s, 4),
                'SlotQuery_stub': {
                    'query_id': 'stub-%s-%s' % (hid, scenario), 'hospital_id': hid,
                    'dept_ids': [t['dept_id'] for t in sorted(targets, key=lambda t: (t['priority'], t['dept_id']))
                                 if hid in t['hospitals']][:5],
                    'scenario': scenario, 'earliest_from': 'TODAY', 'max_wait_days': 7,
                    'price_cap_cny': None, 'target_doctor': None,
                    'timeout_ms': 8000, 'budget_left': sh.BUDGET_FETCH_DEFAULT - rank + 1,
                },
            })
        res['triage'] = {'term': term, 'how': how,
                         'targets': [_target_summary(t) for t in
                                     sorted(targets, key=lambda t: (t['priority'], t['dept_id']))]}
        res['hard_filter'] = {'radius_km': radius_km, 'require_insurance': True,
                              'recall': len(recall), 'passed': len(universe),
                              'dropped': len(filtered),
                              'drop_reasons': sh._count_reasons(filtered),
                              'dropped_ids': sorted(filtered.keys())}
        res['K'] = K
        res['K_rule'] = k_info
        res['candidates'] = [
            {'rank': i + 1, 'hospital_id': hid, 'name': rec['name'],
             'level': rec['level'], 'type': rec['type'], 'district': rec['district'],
             'insurance': rec['medical_insurance'],
             **sh.explain(c, s)}
            for i, (hid, (s, c, rec)) in enumerate(candidates)
        ]
        res['pruned_by_K'] = [{'hospital_id': hid, 'name': rec['name'], 'score': round(s, 4)}
                              for hid, (s, _, rec) in overflow[:50]]
        res['fetch_plan'] = fetch_plan
        pf = dict(patient_filter or {})
        if hospital_excl:
            dropped_h = [{'hospital_id': hid, 'name': norms['hospitals'][hid]['name'], 'reason': why}
                         for hid, why in sorted(hospital_excl.items()) if hid in recall]
            if dropped_h:
                pf['dropped_hospitals'] = dropped_h
        res['patient_filter'] = pf or None
        return res

    def run(self, symptom, scenario, loc, radius_km=None, weight_overrides=None,
            require_insurance=True, patient_band=None):
        sh = self.sh
        weights = dict(sh.SCENARIOS[scenario])
        if weight_overrides:
            for k, mult in weight_overrides.items():
                if k in weights and isinstance(mult, (int, float)) and mult > 0:
                    weights[k] = weights[k] * mult
            tot = sum(weights.values())
            weights = {k: v / tot for k, v in weights.items()}
        saved = sh.SCENARIOS[scenario]
        sh.SCENARIOS[scenario] = weights
        try:
            res = sh.run_stage1(symptom, scenario, (float(loc[0]), float(loc[1])),
                                radius_km=radius_km, require_insurance=require_insurance,
                                lex=self.lex, di=self.di, mans=self.mans, norms=self.norms)
        finally:
            sh.SCENARIOS[scenario] = saved
        res['applied_weights'] = {k: round(v, 4) for k, v in weights.items()}

        # 患者层科室过滤: 成人剔儿科类 / 儿童剔成人科（过滤后同构重算, 非"缩小K"式作弊）
        if patient_band in ('adult', 'elderly', 'child') and res['triage']['term'] is not None:
            term, how, raw_targets = sh.target_depts(symptom, self.lex['terms'], self.di)
            if term is not None:
                dropped, kept = [], []
                for t in raw_targets:
                    why = self._excluded_reason(t['canonical'], patient_band)
                    if why:
                        dropped.append((t, why))
                    else:
                        kept.append(t)
                if dropped:
                    band_zh = '儿童' if patient_band == 'child' else '成人/老年'
                    note = ('%s患者剔除目标科室 %d 个: %s（剩余 %d 个, 已按过滤后目标同构重算召回/打分/K/抓取计划; '
                            '词表与字典只读不改）'
                            % (band_zh, len(dropped),
                               ', '.join('%s[%s]' % (t['canonical'], w) for t, w in dropped),
                               len(kept)))
                    pf = {'band': patient_band,
                          'dropped_targets': [{'dept_id': t['dept_id'], 'canonical': t['canonical'],
                                               'priority': t['priority'], 'confidence': t['confidence'],
                                               'reason': w} for t, w in dropped],
                          'kept_n': len(kept), 'note': note}
                    if kept:
                        rk = radius_km if radius_km is not None else sh.SCENARIO_RADIUS_DEFAULT[scenario]
                        res = self._rebuild_with_targets(res, term, how, kept, scenario,
                                                         (float(loc[0]), float(loc[1])), rk,
                                                         weights, pf)
                    else:
                        # 目标科室全部被患者层剔除（罕见）: 保留原候选集并显式标注, 交人工
                        pf['note'] += ' —— 目标科室全部被剔除, 保留原候选集（罕见, 建议人工处理）'
                        res['patient_filter'] = pf
        return res

    def slot_queries(self, res, inf, loc, budget=12):
        """按 RANKING_SPEC §4.3 生成正式 SlotQuery 序列（只生成, 不发起请求）。"""
        out = []
        for fp in res['fetch_plan'][:budget]:
            hid = fp['hospital_id']
            dids = fp['SlotQuery_stub']['dept_ids']
            qid = 'q-' + hashlib.sha1(
                ('%s|%s|TODAY|%s' % (hid, ','.join(dids), str(loc))).encode('utf-8')
            ).hexdigest()[:12]
            out.append({
                'query_id': qid,
                'hospital_id': hid,
                'dept_id': dids[0] if dids else None,
                'dept_ids': dids,
                'scenario': res['scenario'],
                'user_loc': {'lng': loc[0], 'lat': loc[1], 'crs': 'GCJ-02'},
                'earliest_from': 'TODAY',
                'max_wait_days': inf['max_wait_days'],
                'price_cap_cny': inf['price_cap_cny'],
                'target_doctor': {'name': None, 'title': None},
                'timeout_ms': 8000,
                'order': fp['order'],
                'budget_left': fp['SlotQuery_stub']['budget_left'],
                'static_score': fp['static_score'],
            })
        return out

    def hospital_target_depts(self, hid, res, max_n=5):
        """该医院实际拥有的 triage 目标科室（≤5, 按 priority 排序）——已含患者层过滤。"""
        dept_ids = set(self.di['hospitals'].get(hid, {}).get('dept_ids', []))
        hits = [t for t in res['triage']['targets'] if t['dept_id'] in dept_ids]
        hits.sort(key=lambda t: (t['priority'], t['dept_id']))
        return hits[:max_n]

    def dept_l1(self, dept_id):
        d = self.di['depts'].get(dept_id)
        return (d or {}).get('l1_category', '')
