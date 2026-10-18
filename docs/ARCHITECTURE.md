# ARCHITECTURE — 9 段流水线

入口 `app/main.py::run_pipeline`。每段有明确输入/输出契约; 段落编号与 `result.json`/`report.md` 章节一一对应。

```
 疾病自述+就诊人+偏好
        │
 ① 定位 locate ────────────── 高德 place/text → geocode/geo → stub 小表（GCJ-02; 30 天缓存）
        │ loc{lng,lat,crs,method,note}
 ② 病情采集 dialogue ──────── ≤3 轮自由文本（demo 剧本走同一解析路径; 非 TTY 安全）
        │ spec{symptom_text, patient{age,age_band,is_child,gender,proxy},
        │      urgency_days, visit_type, prefs{metro,price_cap,level_expectation}, notes, _raw}
 ③ 权重推理 infer（LLM 推理 1/2; 唯一 LLM 后端 = Agent Service）
        │ inf{scenario, weights(绝对,行和=1.0), llm_depts(归一化词表直选), triage_term,
        │     max_wait_days, price_cap_cny, reasoning, llm_meta, context}
        │    prompt 携带基础数据上下文: 363 可约科室 canonical + 438 医院目录 + 5 套场景基线权重
 ④ 候选集 candidates（import ranking/scripts/score_hospitals.py, 复用不重写）
        │ res{triage{term,how,targets}, hard_filter, K, candidates(静态分排序), fetch_plan}
        │    科室目标: LLM 直选(canonical→D-ID 索引) 或 词表回落; 患者层过滤(儿童/成人互斥科室)
 ⑤ 号源 slots.SlotResolver ── 5 层: profile → 观察快照 → [live deep] → [live probe] → 档位先验 → stub
        │ slot_by_hid{hid: {best, all}}（预算 12 家不早停; TTL 90min; 全标注来源; 含已核实源时未核实 1% 折扣）
 ⑥ 终排 recommend.finalize ── 两级排序(已核实有号>未核实>已核实无号) + 同级 final_score = static × slot_utility（全候选集 K）
        │ final{recommendations(1–3, why+evidence), top_list(人工审阅 20), excluded,
        │       n_verified, fallback_all_unverified, live_coverage, final_llm}
        │ ⑥b 终裁 LLM(推理 2/2, finalize_llm.py): 输入=deep 深核明细+患者摘要, 输出 top3+医生+理由;
        │    严格白名单校验(hid/医生/rank 去重), 失败或不可用 → 回退确定性 top3, 不阻断交付
 ⑦ 模拟挂号 book（dry-run 默认开; 不发、不提交、不支付）
 ⑧ 回归断言 recommend.validate_result（A/B/C/D, 每次运行必跑, 失败→非零退出）
 ⑨ 落盘 app/out/<case>/result.json + report.md
```

## 模块职责表

| 模块 | 职责 | 关键 API | 依赖 |
|---|---|---|---|
| `app/main.py` | 9 段编排、CLI、受控退出 | `run_pipeline(args, case_id)`; `run_checks(args)` | 全部 |
| `app/locate.py` | 位置解析 | `locate(args)` → `{lng,lat,crs,method,cached,is_stub,note}`; `parse_loc("lng,lat")` | `amap.py`, `fixtures/geocode_stub.json` |
| `app/amap.py` | 高德 Web API 客户端（纯 urllib） | `resolve_key()`; `search_place(key,text)`; `geocode_geo(key,text)` | 网络; `hospital_intel/.env`; 缓存 `app/cache/amap/` |
| `app/dialogue.py` | 病情采集（正则/启发式解析自由文本） | `run_interactive(prefill, non_interactive)`; `parse_case(answers)`; `load_demo(case_id)` | `fixtures/demo_cases.json` |
| `app/infer.py` | **头推理（LLM 1/2）**：LLM 直出权重向量+目标科室 | `infer_llm(spec, stage1, loc_note, model, base)` → inf; `build_context(stage1)`; `_validate(obj, ctx)` | `llm_client.py`, `dept_index.json`, 症状词表, feature_norms |
| `app/llm_client.py` | Agent Service SSE 客户端（纯 http.client） | `chat_json(sys, prompt, model_id, base)` → `(obj, meta)`; `resolve_base_url()` | 网络; 见 docs/LLM_CONTRACT.md |
| `app/candidates.py` | stage-1 复用 + 候选集 + SlotQuery | `Stage1().run_llm(term, llm_canonicals, scenario, loc, weights, patient_band)`; `.slot_queries(res, inf, loc)`; `.run(...)`（旧契约保留） | importlib → `ranking/scripts/score_hospitals.py` |
| `app/slots.py` | 号源 5 层解析 | `SlotResolver(allow_live, device, log).resolve(hid, dept_id, canonical, level, type, l1_cat)` | `slots/profiles/*`, `slots/out/SH-*.json`, `fixtures/stub_slots.json`, live→`slots/fetch_slots_v1_20261004.py` |
| `app/recommend.py` | 终排、可解释、断言、报告 | `finalize(candidates, slot_by_hid, inf, top_n)`; `validate_result(data)`; `render_report(...)` | RANKING_SPEC §4.5/§5 |
| `app/finalize_llm.py` | **尾终裁（LLM 2/2）**：从 deep 深核名单定 top3+医生+理由 | `run_final(...)`→picks/None; `build_prompt`; `parse_final`（严格白名单校验） | `llm_client.py`; RANKING_SPEC §4.6 |
| `app/book.py` | 模拟挂号 | `run(top, slot_best)` → `{dry_run, executed, steps, note}` | 无（纯打印步骤序列） |
| `ranking/scripts/score_hospitals.py` | stage-1 静态排序引擎（**import 复用, 禁止重写**） | `load_inputs()`; `load_or_build_norms(di, mans)`; `resolve_terms(term, terms)`; `hard_filters`; `score_hospital`; `pick_k` | `SYMPTOM_LEXICON.json`, `dept_index.json`, `manifests/` |

## stage-1 内部（`score_hospitals.py` 复用原语, 详见 RANKING_SPEC）

1. **召回**：症状词 → `resolve_terms`（精确 → 症状含词(最长) → 词含症状(最短) → 分词回落）→ 目标科室（kind ∈ {clinic, specialty_clinic, screening}）→ 覆盖医院集合。LLM 直选科室走同一 D-ID 索引, 不重造解析。
2. **硬过滤**：距离半径（场景默认: community 12 / chronic 15 / complex 30 / red_flag 15 / peds 20 km）、医保、DIST_CAP 25km 等。
3. **打分**：`score_hospital` = Σ 10 特征 × 权重（`weights.get(name, 0.0)`, 缺失特征=0 安全跳过）。特征归一在 `feature_norms.json`（438 家有科室数据; 指纹校验幂等重建）。
4. **选 K**：`pick_k`（K_MIN=14, K_MAX=60, 绝对落差 0.22 / 相对落差 0.15 规则）。
5. **SlotQuery 序列**（§4.3 契约）：预算 12 条, 按静态分序, 每条含 hospital_id/dept_id(s)/scenario/user_loc/earliest_from/max_wait_days/price_cap_cny/timeout_ms。**只生成不发起**（发起在 ⑤ 号源层）。

## 患者层科室过滤（app 层, 只读词表/字典）

- 成人/老人：剔除儿科标记的目标科室。
- 儿童：剔除成人专属标记（妇科/产科/孕/产前/孕检/产后/孕产妇/更年期/不孕/计划生育/男科/前列腺/老年）。
- 过滤后**同构重建**召回/打分/K/抓取计划（stage-1 原代码）, 排除项带 `_excluded_reason`（报告 §7 可见, 不静默消失）。

## 失败语义总表

| 故障 | 行为 | 退出码 |
|---|---|---|
| 定位解析失败（无 key 且不在 stub 表） | `【1 位置】解析失败: ...` | 1 |
| Agent Service 不可达/超时 | `【3 权重推理】LLM 不可用或输出未通过校验: ...` + 可操作提示 | 1 |
| LLM 输出两次未过结构校验 | 同上（含错误明细, 不静默回退） | 1 |
| LLM 无有效科室 且 triage_term 词表无命中 | `LLM 未给出有效目标科室, 且 ... 无命中 — 请用更具体的症状描述重跑` | 1 |
| 终裁 LLM（2/2）失败/校验不过/不可用 | 回退确定性 top3（两级排序）, `final.final_llm.applied=false`, **不阻断交付** | 0 |
| stage-1 无目标科室/空候选 | 提示换更具体症状 | 2 |
| 回归断言 A/B/C/D 任一失败 | `【回归断言失败】...` | 2 |
| stdin EOF（非交互误用） | 提示补 --symptom / --demo | 1 |
| 其他未预期异常 | 一行错误 + 可操作选项（--trace 看完整栈） | 3 |
| Ctrl-C | `【中断】...未发起任何真实挂号` | 130 |
