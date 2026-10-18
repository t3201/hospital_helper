# API — CLI 参考与输出 schema

## 1. CLI（`python -m app.main`）

> 2026-10-06 新增：`--probe-n`（Phase A 快速探测院数, 默认 12）、`--deep-n`（Phase B 深核院数, 默认 5,
> 取初排前 N 名）。`--live-timeout` 为总预算, 按 55%/45% 切给 probe/deep。
> `final.live_coverage` 结构变为 `{probe: {...}, deep: {...}}`（旧结果仍为扁平结构, web 两端兼容）。
> `final.final_llm`（新增）：`{applied, confidence, notes, model_id}` —— Phase C 终裁 LLM 是否应用及元信息;
> 推荐行可带 `llm_final/llm_doctor/llm_reason`。`slots.<hid>` 的 probe 来源行带
> `slot_source=live_probe` + `probe_confidence`（card_btn/bar_only/…）+ `bookable_subtabs`。
> 2026-10-07：行可带 `fee_from_stale=true`（费档由历史观察回填, 仅展示参考）。

| 旗标 | 说明 |
|---|---|
| `--demo {red_flag,chronic,peds}` | 一条命令跑完 demo（别名: chest_pain/hypertension/peds_growth/chest/chronic_followup/growth）。剧本在 `app/fixtures/demo_cases.json`, **与交互走同一解析路径** |
| `--symptom "..."` | 症状自由文本（交互模式, 省去第 1 轮问答; 非交互必填项） |
| `--spec-file PATH` | 结构化病例 JSON（web/表单同渠道; 跳过自由文本解析, 与交互同一 schema）: `{symptom_text, age, gender, urgency_days, visit_type, metro_priority, price_cap_cny, level_expectation, proxy}` |
| `--loc "lng,lat"` | 位置（GCJ-02）; 代挂号主场: 人为指定坐标 |
| `--loc-text "地址"` | 地址文本（优先级: 高德 place/text → 高德 geocode/geo → 内置 stub 小表兜底） |
| `--loc-adb` | 从 ADB dumpsys location 取位置（需 adb） |
| `--no-network` | 强制离线: --loc-text 不发高德请求, 只查 stub（结果为内置示例坐标, 非真实定位, method=stub 标注） |
| `--llm-model ID` | LLM model_id（唯一后端 = Agent Service; 默认 `default`, 服务端按 label 解析） |
| `--agentsvc-base URL` | Agent Service base URL（默认按 `RUNTIME_SERVER_URL`/`AGENTS_URL` 环境变量, 否则 `http://127.0.0.1:7988`） |
| `--live` | 号源循环前批量触发 v2 `--condition` 实时抓取, 覆盖缺失/TTL 过期的 (院,科)（需 `--device`; 一次 adb 会话, 采集深度见 `--live-slot-type`） |
| `--device SERIAL` | ADB 设备序列号（--live 需要; 缺省 v2 用 476cf991） |
| `--live-slot-type` | `--live` 采集深度: 缺省=全部子 tab(专家/普通/专病, 约 5-15 分钟/科室); `expert`=仅专家门诊(快速档, 约 1/3 耗时); `normal`/`specialty` 同理 | `None` |
| `--radius KM` | 覆盖场景距离半径（缺省用场景默认, 见 ARCHITECTURE） |
| `--top N` | 人工审阅列表行数（默认 20） |
| `--no-book` | 跳过模拟挂号步骤 |
| `--out DIR` | 输出目录（默认 `app/out`; 其下 `<case>/result.json`） |
| `--non-interactive` | 禁用交互追问: 必须配 --symptom, 其余取默认（非 TTY 时自动进入该模式） |
| `--check` | 只跑回归断言: 对 `--out`/`*/result.json` 逐例校验 A/B/C/D, 不跑流水线（任一失败非零退出） |
| `--trace` | 出错时打印完整 traceback（默认只给可操作提示） |

环境变量：`AMAP_KEY`（置空串=显式禁用高德, 不再回落 .env）; `RUNTIME_SERVER_URL`/`AGENTS_URL`（LLM base）; `RUNTIME_SERVER_HOST`/`RUNTIME_SERVER_PORT`（默认 127.0.0.1:7988）。

退出码：0=成功; 1=参数/LLM 不可用/EOF; 2=stage-1 无目标或断言失败; 3=未预期异常; 130=Ctrl-C。详见 ARCHITECTURE「失败语义总表」。

## 2. result.json schema（机读; `app/out/<case>/result.json`）

顶层 10 键（实测结构, red_flag 为例）：

```jsonc
{
  "meta": { "generated_at": "2026-10-04T23:11:15", "mode": "demo:red_flag",
            "llm": "agentsvc(model_id=default)", "app": "挂号推荐运行期原型 v0.2（PC 原型, 目标手机端）" },

  // ① 定位
  "loc": { "lng": 121.4737, "lat": 31.2304, "crs": "GCJ-02",
           "method": "user_specified | amap_place | amap_geo | stub（命中缓存加 _cached 后缀且 cached=true）",
           "note": "..." },

  // ② 病例
  "spec": {
    "symptom_text": "62岁男性，胸痛半小时，压榨样疼痛，向左肩臂放射，伴冷汗",
    "patient": { "age": 62, "age_band": "adult|child|elderly", "is_child": false,
                 "gender": "male|female|null", "proxy": false },
    "urgency_days": 0, "visit_type": "new|followup",
    "prefs": { "metro_priority": false, "price_cap_cny": null,
               "level_expectation": "top|balanced|low" },
    "notes": ["..."], "_raw": ["第1轮", "第2轮", "第3轮"]
  },

  // ③ LLM 推理
  "inference": {
    "scenario": "community_first|chronic_followup|complex_severe|red_flag|peds",
    "scenario_zh": "红旗急症",
    "weights": { "match": .24, "level": .28, "breadth": .04, "distance": .14, "metro": .02,
                 "bus": .02, "key_specialty": .04, "emergency": .22, "age_fit": 0, "price": .04 },   // 绝对权重(12维), 行和=1.0, 无关特征可缺键;
                 // price=费用友好度: 不进 stage-1, 仅终排性价比因子(见 SLOTS §4)
    "weight_overrides": {},          // 兼容字段: 新契约下为空（LLM 直出, 无乘子）
    "max_wait_days": 0, "price_cap_cny": null,
    "triage_term": "胸痛",
    "llm_depts": ["急诊内科", "心血管内科", "急诊科", "冠心病", "内科"],  // 归一化 canonical, 按优先级(tier)
    "llm_dept_tiers": { "急诊内科": 1, "心血管内科": 1, "急诊科": 2, "冠心病": 3, "内科": 3 },  // canonical→tier(1首选/2相关/3弱相关); match 按层级取质
    "llm_dept_kinds": { "急诊内科": "clinic", ... },
    "dept_how": "llm | llm_term_lexicon",   // 直选 或 词表回落
    "reasoning": ["...", "..."],            // ≤3 条 + 交叉核对/强制修正注记
    "llm_mode": "agentsvc(model_id=default; 直出权重向量+目标科室, 含基础数据上下文)",
    "llm_meta": { "provider": "agentsvc", "model_id": "default", "base": "http://127.0.0.1:7989",
                  "elapsed_ms": 25078.0, "first_byte_ms": 0.0, "attempts": 1,
                  "thinking_chars": 4156, "session_id": null, "n_data": 2107, "prompt_chars": 14189 },
    "fallback": false,
    "context": { "n_depts": 363, "n_hospitals": 438, "dept_hc_min": 5 }
  },

  // ④ stage-1
  "stage1": {
    "symptom": "胸痛", "scenario": "red_flag",
    "triage": { "term": "胸痛", "how": "llm",
                "targets": [ { "dept_id": "D04780", "canonical": "心血管内科", "kind": "clinic",
                               "priority": 1, "confidence": "high", "hospitals": ["SH-003", ...] } ] },
    "applied_weights": { ...同 inference.weights 剥离 price 后前 11 维重归一(含 age_fit)... },
    "hard_filter": { "radius_km": 15.0, "require_insurance": true, "recall": 172, "passed": 86,
                     "dropped": 86, "drop_reasons": { "distance": 86 }, "dropped_ids": ["..."] },
    "K": 23,
    "K_rule": { "n": 86, "s0": 0.9705, "floor": 0.7505, "gap_thr": 0.0824, "i_floor": 23, "i_gap": 86 },
    "patient_filter": { "note": "..." }   // 未触发时为空对象
  },

  // ⑤ SlotQuery 序列（§4.3 契约, 只生成不发起）
  "slot_queries": [ { "query_id": "q-4f283fb8f075", "hospital_id": "SH-003",
                      "dept_id": "D04780", "dept_ids": ["D04780", "D02633"], "scenario": "red_flag",
                      "user_loc": { "lng": ..., "lat": ..., "crs": "GCJ-02" },
                      "earliest_from": "TODAY", "max_wait_days": 0, "price_cap_cny": null,
                      "target_doctor": {...}, "timeout_ms": 8000, "order": 1,
                      "budget_left": 12, "static_score": 0.9705 } ],

  // ⑤ 号源（每家查询过的医院 → best）
  "slots": { "SH-003": {
      "hospital_id": "SH-003", "dept_id": "D02633",
      "slot_source": "cache|live|stub", "detail": "level_default|observed|...",
      "status": "ok|no_slot|unknown", "verified": false,
      "earliest_date": null, "earliest_days": 5.0, "fee_tier": "50|100+|unknown",
      "fee_tiers": { "50": 0.003 }, "fee_min_tier": "50",   // 费档分布 + 最低可核实档(2026-10-05 新增)
      "tension": null, "perennial_no_slot": false, "perennial_no_slot_rate": null,
      "has_slot_rate": 0.65, "confidence": "high|mid|l",
      "provenance": { "file": "slots/profiles/level_defaults.json", ... },
      "note": "档位估算（stratum=三级甲等|综合医院, n_units=383, confidence=high; 非实时, 不当作号源事实）",
      "stale_reference": {...}, "dept_name": "内科" } },

  // ⑥ 终排
  "final": {
    "recommendations": [ /* top 1–3, 字段同 top_list[0] */ ],
    "top_list": [ { "rank": 1, "hospital_id": "SH-003", "name": "华山医院",
                    "level": "三级甲等", "type": "综合医院", "district": "静安区", "insurance": "yes",
                    "static_score": 0.9705, "slot_utility": 1.0, "final_score": 0.9705,
                    "slot_source": "cache/level_default(档位先验,未核实)", "verified": false,
                    "queried": true, "static_rank": 1, "slot_status": "unknown",
                    "earliest_date": null, "fee_tier": "50", "fee_tiers": {...}, "fee_min_tier": "50", "dept": "内科",
                    "dept_tab": "内科", "dept_raw": "全科",     // 挂号入口: L1 的 tab 名 + 院页面 raw 科室行名(2026-10-05 新增, 来自 manifest l1)
                    "dept_id": "D02633",
                    "marks": ["号源未核实: 档位先验 ..."], "violations": [],
                    "top3_contrib": [ { "feature": "level", "value": 1.0, "w": 0.28, "contrib": 0.28 } ],
                    "deductions": [ ... ],
                    "why": "华山医院（三级甲等/静安区）: 医院等级=三级甲等（+0.28, level），科室命中=命中: 内科,心血管内科 [p1/high]（+0.24, match）。号源未核实（...）, 仅按静态分排序, 不当实时事实。",
                    "evidence": { ...8 键... } } ],   // 默认 20 条（--top 可调）
    "excluded": [ { "hospital_id": "...", "reason": "..." } ],   // 显式排除+原因, 不静默消失
    "slot_tiers": { "ok": 3, "unknown": 55, "no_slot": 2 },      // 2026-10-06: 号源状态计数（两级排序: 已核实有号>未核实>已核实无号, 同状态内按 final_score）
    "live_coverage": { "n_routes": 12, "n_parsed": 6, "n_units": 22, "stop_reason": "deadline", "run_file": "slots/out/condition_runs/run_<ts>.json" },  // 2026-10-06: live 批次覆盖情况（--live 时）
    "fallback_all_unverified": true, "n_verified": 0, "n_total": 23, "top_n": 20
  },

  // ⑦ 模拟挂号
  "booking": { "dry_run": true, "executed": false,
               "steps": ["【dry-run】..."], "note": "dry-run（默认开）: 仅打印将执行的步骤序列; 不真发、不提交、不支付。" },

  // stage-1 全候选（K 家, 供断言与审计）
  "candidates_static": [ /* 同 final.top_list 元素, 无 slot 字段 */ ],

  // ⑧ 断言
  "checks": [ { "id": "A", "name": "已查询预算内前 12 家（candidates_static[:12]）⊆ final.top_list",
                "ok": true, "detail": "缺失=[]" } ]
}
```

### 断言 A/B/C/D（每次运行必跑; `--check` 复跑）

| ID | 断言 |
|---|---|
| A | 已查询预算内前 12 家（candidates_static[:12]）⊆ final.top_list |
| B | fallback_all_unverified → top_list 前 20 项与静态序同序（RANKING_SPEC §4.5） |
| C | max(静态分) 保留: top_list 内最大静态分 == 候选集最大静态分 |
| D | slot_queries 的每家医院 ∈ final.top_list ∪ final.excluded（显式排除+原因, 不得静默消失） |

## 3. report.md 章节（人读; 与 result.json 同构）

```
# 挂号推荐报告（运行期原型）
## 1. 位置
## 2. 病情采集（≤3 轮自由文本）
## 3. 权重推理（LLM 直出: 特征权重向量 + 目标科室 + 约束; 含基础数据上下文）
## 4. 候选集（stage-1, 复用 ranking/scripts/score_hospitals.py）
## 5. 号源（缓存优先: profile → 观察快照 → 档位 → stub; TTL=90min）
## 6. 推荐（1–3, 终排=静态分×号源效用; 未核实按静态分, 不决定生死）   ← 每家含 why + 号源标注
## 7. Top20 人工审阅列表（未核实号源显式标注; 全候选集 K 参与排序, 不剔除已查询者）
## 8. 模拟挂号（dry-run, 默认开）
## 9. 数据与已知局限
## 10. 回归断言（--check 同款, 任一失败进程非零退出）
## 附: SlotQuery 序列（§4.3 契约, 仅生成不发起）
```

## 4. 参考输出（`app/out/` 已含 4 个已验证 case）

| case | 症状 | scenario（LLM 直出） | top1 |
|---|---|---|---|
| `red_flag` | 62 岁男性胸痛（红旗） | red_flag（emergency=0.22, level=0.28; 5 科室含急诊内科/急诊科） | 华山医院 0.9705 |
| `chronic` | 高血压复诊, 费用≤50 | chronic_followup（chronic_fit=0.08; LLM 抽出 price_cap=50） | 第八人民医院 |
| `peds` | 4 岁儿童身高迟缓 | peds（match=0.42; 儿童保健科/儿童生长发育/儿科） | 第六人民医院 |
| `interactive` | 复视（斜视词表桥） | community_first | 第十人民医院 |

注意：LLM 非确定性 → 重跑 demo 的权重/科室可能与参考输出不同, 但断言 A/B/C/D 恒过（设计为权重无关）。

## 5. Web 实测端（`python -m app.web`, 纯标准库 ThreadingHTTPServer + SSE）

用法: `python -m app.web --port 8765` → 浏览器打开 `http://127.0.0.1:8765/`。
单页表单（2026-10-08 简化：**症状描述 / 就诊人下拉 / 预期费用尺度条 / 位置 / 实时拓取档位**）→ 同一 `run_pipeline`。年龄·性别·时效·就诊类型·费用上限·水平预期·地铁优先·代挂号 复选框已移除，改由 LLM 从症状描述解析（费用档位是显式意图，非 auto 时覆盖 `price_cap_cny`）。
**全局单任务**（adb 设备占独 + stdout 捕获为进程级状态）: 忙碌时 `POST /api/run` 返回 409。
web 任务用 `--spec-file` 注入结构化病例，落盘 `app/out/web_<ts>_<rand>/`（同步参与 `--check` 回归门禁）。
位置必填（代挂号勾选与否都要填）: 「📍 自动定位」= 服务端 **adb 读手机最后位置**（`dumpsys location`, fused>gps>network, WGS-84）→ GCJ-02 → 高德逆地理成地址（权威坐标校正）; 30 分钟文件缓存——服务启动 5s 后自动读一次, **页面刷新即回填**, 点按钮=实时重读; 提交走 `loc_text`（pipeline 再用高德 geocode 成精确 GCJ-02）; 手机"最后位置"可能是数小时~数天前的记录（响应含 `age_s`, UI 对 >30min 显示 ⚠ 建议手填）; 手填经纬度（GCJ-02）优先。浏览器 Geolocation 路径已弃用（LAN http 下不可用; `/api/regeo` 保留为底层反链）。
管线 stdout 全量逐行同时落盘 `app/out/<job>/log.txt`（2026-10-05 新增; 此前仅 SSE 实时转发不落盘, 浏览器关闭即失）。

| 端点 | 说明 |
|---|---|
| `GET /` | 单页 `app/web/index.html`（原生 JS, 无框架） |
| `POST /api/run` | body=JSON: `{symptom*, patient_id, fee_expect(auto|zero|ten|hundred|thousand|tenk|any), loc_text*|loc*, live, slot_type, device, no_network, radius, top, agentsvc_base, llm_model, live_timeout_min}`（`patient_id` 取自 `/api/patients` 缓存, 不传=「不选择」=代他人挂号; `fee_expect` 非 auto 时覆盖费用硬上限并原样交给 LLM 调医院类型; live_timeout_min=分钟→`--live-timeout` 秒, 默认 90）; 返回 202 `{job_id, stream}`; 参数错→400（含未知 `patient_id`/非法 `fee_expect`）, 忙碌→409。旧字段 age/gender/urgency_days/visit_type/price_cap/level_expectation/proxy 仍兼容直传 |
| `GET /api/stream?job=` | SSE: 逐帧 `data: {"type":"line","v":"..."}`（流水线 stdout 全量实时转发）; 终框 `{"type":"done","v":{"ok", "code", "out_dir"}}` |
| `GET /api/result?job=` | 完整 `result.json`（同 §2 schema）; **job 按 `app/out/<id>/` 目录解析 → 任意历史 run（web/CLI/demo, 跨重启）可取**; 未完成→202 `{error:"not ready"}` |
| `GET /api/jobs` | 历史运行列表: **扫 `app/out/*/result.json` 持久目录**（web/CLI/demo 全可见, 跨重启不丢）+ 合并内存 running; 字段 id/started_at/generated_at/mode/symptom/top1/top1_score/checks_pass|total/live/running, 按时间倒序, 最多 50 条 |
| `GET /api/device-loc?fresh&dev` | 自动定位反链: `adb -s <dev> shell dumpsys location`（fused>gps>network, WGS-84）→ GCJ-02 → 高德 regeo（地址+权威坐标校正）→ `{ok, lng, lat, crs, address, city, district, provider, device, age_s, from_cache}`; 30min 文件缓存 `app/cache/device_loc.json`, `fresh=1` 实读; 服务启动 5s 后自动预热 |
| `GET /api/patients?fresh&cache&dev` | 随申办 · 预约挂号 · 「就诊人」列表（`app/patients.py`, **只读** adb uiautomator, 绝不点 新增/绑定/去预约）→ `{ok, captured_at, device, total_declared, patients:[{id, label, name_masked, age, gender, gender_source, insurance_card, insurance_card_no, phone_masked, cert_type, id_no_masked, created_days}], notes, from_cache}`; 性别无文本 → 按脱敏证件号（14 星+后 4 位）第 17 位奇偶推断, 依据写 `gender_source`; 30min 缓存 `app/cache/patients.json`, `cache=1` 只读缓存(页面加载用, 不碰 adb), `fresh=1` 实采; 流水线在跑→409（adb 独占）, 采集失败但有旧缓存→200+`error` |
| `GET /api/regeo?lng&lat` | 底层逆地理反链: 参数= **WGS-84** 坐标 → 服务端转 GCJ-02 + 高德逆地理 → `{ok, lng, lat, crs:GCJ-02, address, city, district, amap_snapped, from_cache}`（lng/lat 优先取高德匹配坐标校正）; key 只在服务端解析, 不下发前端; 无 key→503, 参数非法→400 |
| `GET /api/report?job=` | 该 run 的 `report.md`（text/markdown, 历史面板点击后展开）; 无→404 |
| `GET /api/regeo?lng&lat` | 自动定位反链: 入参=浏览器 Geolocation 的 **WGS-84** 坐标 → 服务端转 GCJ-02 + 高德逆地理 → `{ok, lng, lat, crs:GCJ-02, address, city, district, amap_snapped, from_cache}`（lng/lat 优先取高德匹配坐标校正）; key 只在服务端解析, 不下发前端; 无 key→503, 参数非法→400 |

约束: 不发起真实挂号; LLM 推理约 30-70s; `live=true` 时实时抓取约 5-15 分钟/科室（SSE 长连接客户端需耐心等待）。
