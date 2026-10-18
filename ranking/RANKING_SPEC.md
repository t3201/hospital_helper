# RANKING_SPEC — 候选医院静态排序规格（stage-1）+ 两段式接口契约

- 版本: v1.0-draft（专家先验初值，**未调参**，无标注数据、不做拟合）
- 范围: 只做 **stage-1（静态特征 → 小候选集 → 抓取顺序）** 与 **两段之间接口契约**；
  **不实现**任何号源/排班抓取，不新增 connector 能力位，不做全网全科室遍历。
- 代码: `ranking/scripts/score_hospitals.py`（纯标准库、零模型调用、幂等）
- 产物: `ranking/out/`（feature_norms / DEMO_* / eval_results / self_check）、`ranking/eval/`（EVAL_SET / EVAL_REPORT）
- 上游只读: `ssb_dept/triage/SYMPTOM_LEXICON.json`（1822 词→科室）+ `hospital_intel/dept_index.json`（10334 科室×438 家）+ 438 份 manifest。**绝不写入 hospital_intel/、ssb_dept/**。

## 0. 数据事实（摸底结论，直接引用）

| 事实 | 值 | 对排序的含义 |
|---|---|---|
| 号源能力 | `query_schedule/query_slots/book` 全部 **0 家**；`query_departments` 438 家 | stage-2 能力尚不存在，本件只定契约 |
| 有科室数据 | 438/450（12 家无 bookable_departments：SH-032/190/269/309/343/405/445~450） | 硬过滤砍掉；覆盖率自检以 438 为准 |
| BLOCKED | `verify_verdict=wrong` 34 家 | 硬过滤砍掉（距离/等级再好也不召回） |
| 等级分布 | 三甲 81 / 三乙 15 / 二甲 74 / 二乙 24 / 一甲 230 / 其余零散 14 | 等级映射分档，不一刀切 |
| 类型 | 综合 338（含大量社区卫生服务中心）/ 专科疾病防治院 91 / 其他 21 | 类型不进权重，只做展示 |
| `key_specialties` | **仅 20/450（4.4%）非空** | **只能做加分项，不可作为主权重**（显式标注） |
| `medical_insurance` | yes 438 / no 1 / 未知 11 | **无方差 → 只做硬过滤/展示，不进权重** |
| 交通 | `metro_walk_min` 278/450 有值（172 缺失）；`bus_stops_300m` 450/450 有值（23 家=0，属真实值非缺失） | 缺失必须有中性策略（见 §2） |
| 科室齐全度 | `bookable_dept_l2_count` p10=3 / p50=19 / p90=142 / max=731 | **长尾极重 → log1p + p90 分位帽** |
| 院区 | 每家恰好 1 个院区，均有 GCJ-02 坐标，16 个行政区 | 距离=主院区直线距离（haversine）；多院区未建模，不假装 |

## 1. 硬过滤（先砍，顺序即优先级）

1. **BLOCKED**：`verify_verdict == "wrong"` → 砍（34 家）。
2. **无科室数据**：不在 `dept_index.hospitals` 内 → 砍（12 家）。
3. **医保**：用户要求医保（默认是）且 `medical_insurance == "no"` → 砍（1 家）；未知（11 家）保留、展示时标注“医保未证实”。
4. **距离半径**：主院区直线距离 > 场景默认半径（community 12km / chronic 15km / complex 30km / red_flag 15km / peds 20km，用户可覆盖）→ 砍。
5. **科室命中（kind 政策）**：必须命中 ≥1 个 triage 目标科室，且该科室 `kind ∈ {clinic, specialty_clinic, screening}`；
   `kind ∈ {other(预约/疫苗条目), site(号/站点), selfname(站名), ancillary(影像检查)}` 一律**不算命中**。
   零命中 → 砍（这是召回集的定义本身）。

硬过滤之后的集合才进入打分。**静态分只决定候选集内部顺序与抓取顺序，绝不决定生死**（生死=是否有号，只能由 stage-2 回答）。

## 2. 特征表（归一化 × 权重初值 × 缺失策略 × 可信度降权）

归一化全部预计算进 `ranking/out/feature_norms.json`（438 家分位/映射查表，运行时零计算、可解释）。
**缺失策略：缺失给中性分 0.5 并在 explain 中标注 `缺失→中性0.5`，不惩罚；“已知为空”（如 key_specialties 空列表）给 0.0，不做中性化。**
**可信度降权：`provenance.confidence=low` 的特征值向中性 0.5 混合 40%（v' = 0.6v + 0.2），explain 标注 `低置信`。**

| 特征 | 归一化 | 初值权重(见§3场景) | 缺失策略 | 说明 |
|---|---|---|---|---|
| `match` 科室命中质量 | max over 命中目标科室 of PRIORITY_W×CONF_W；PRIORITY_W={p1:1.0,p2:0.70,p3:0.45}, CONF_W={high:1.0,mid:0.85,low:0.65}。**LLM 路径下 priority=LLM tier**（1=首选/2=相关/3=弱相关）——弱相关科室(邻近专科/保守后备)按 0.45 计, 不再与首选平权 | 主干（0.26~0.45） | 无命中已被硬过滤 | kind 政策内；命中列表+层级进 explain |
| `level` 等级 | 映射：三甲 1.0 / 三乙 0.72 / 三级 0.62 / 二甲 0.50 / 二乙 0.38 / 二级 0.33 / 二甲丙 0.30 / 一甲 0.22 / 一乙 0.18 / 一丙 0.15 / 未定级 0.28 | 场景敏感（0.04~0.42） | 未定级=0.28（中性偏低） | 2 家 low 置信 → 降权混合 |
| `breadth` 科室齐全度 | `min(1, log1p(l2)/log1p(142))`（p90 帽，长尾不压死头部） | 0.08~0.16 | 缺失→中性 0.5 | 438 家全有值 |
| `distance` 距离 | `max(0, 1 - km/25)`（市域上限 25km） | 0.06~0.28 | 无坐标→中性 0.5 | 全 438 家有 GCJ-02 坐标 |
| `metro` 地铁可达 | 有 `metro_walk_min`: `1-min(1,wm/30)`；否则 `metro_nearest_dist_m`: `1-min(1,md/2500)` | 0.02~0.10（“地铁优先”×1.6 再归一） | 缺失→中性 0.5；54 家 low 置信→降权 | 278/450 有值 |
| `bus` 公交 | `min(1, log1p(stops)/log1p(5))`（p90=5 帽） | 0.01~0.05 | 缺失→中性 0.5；0 站=真实值（低分非中性） | 450/450 有值 |
| `key_specialty` 特色科室 | 非空且与目标科室互含子串 → 1.0，否则 0.0 | **0.03~0.06（封顶小权重）** | **空=已知缺失→0.0**；**当前覆盖 20/450=4.4%，不可作为主权重** | 20 家 mid 置信 |
| `emergency` 急诊能力 | 任一 `canonical∋急诊` 且 kind 政策内 → 1.0 | 仅 red_flag 0.10 | 无=0.0（真实值） | 39/438 家有急诊类科室 |
| `chronic` 专病随访 | 命中科室 `visit_types∋专病` → 1.0 | 仅 chronic 0.05 | 无=0.0 | 416/438 家有专病条目 |
| `chronic_fit` 分级诊疗承接 | 低等级（level_score≤0.50，即≤二甲）**且** p1 命中 → 1.0 | 仅 chronic 0.10 | 不适用=0.0 | 专家先验：慢病复诊下沉社区/二级 |
| `age_fit` 人群适配 | peds 场景: 医院 `pediatric_capable`（可约科室含儿科类 canonical 或名称含儿童机构）→ 1.0, 否则中性 0.5; 其他场景恒 1.0 | peds prompt 基线 0.10（LLM 直出权重时生效） | 无缺失 | 人群(儿科)维度独立于症状科室; 叠加加分非惩罚 |

`medical_insurance` 不进权重（无方差）；`type/ownership/district` 仅展示。

## 3. 场景预设（单一权重表一定错 → 5 套专家先验，行和=1.0）

| 特征 | community_first 社区首诊/轻症 | chronic_followup 慢病复诊 | complex_severe 疑难重症 | red_flag 红旗急症 | peds 儿科 |
|---|---|---|---|---|---|
| match | .34 | .26 | .28 | .26 | **.45** |
| level | .12 | .08 | **.42** | .26 | **.04** |
| breadth | .08 | .10 | .16 | .12 | .10 |
| distance | **.28** | .26 | .06 | .16 | **.25** |
| metro | .10 | .07 | .02 | .04 | .07 |
| bus | .05 | .05 | .01 | .02 | .04 |
| key_specialty | .03 | .03 | .05 | .04 | .05 |
| emergency | – | – | – | **.10** | – |
| chronic | – | .05 | – | – | – |
| chronic_fit | – | **.10** | – | – | – |
| age_fit（prompt 基线） | – | – | – | – | **.10** |

差异与理由（专家先验，未调参）：
- **community_first**：就近+可达主导（distance .28/metro .10），等级几乎不看（.12）——轻症去最近能看的地方。
- **chronic_followup**：等级压到 .08 + `chronic_fit` .10（低等级且有 p1 科室的社区/二级直接加分）——编码国家分级诊疗“慢病在社区复诊”的先验，让一甲社区卫生服务中心能进 Top10（验收样例 3）。
- **complex_severe**：等级 .42 + breadth .16 主导——疑难重症看学科高度与齐全度，距离让位。
- **red_flag**：`emergency` .10 一票加权 + match 保持 .26（triage 的 p1=急诊科 自然给 1.0）+ distance .16——胸痛先抢“有急诊+心血管”的三甲（验收样例 1）。
- **peds**：match 抬到 .45、等级压到 .04——儿童保健科 96 家中 67 家是一甲，等级权重高就会把社区儿保全部压掉（验收样例 2）。另加 `age_fit` .10（prompt 基线）：人群(儿科)维度独立特征——有儿科能力的医院 +0.5×w 叠加加分, 不再把"儿科"与症状科室混在目标列表里平权。

调参所需标注量（本件不做）：每个场景 ≥200 条“症状+位置+用户最终选择/放弃”二元标注，另需 ≥30 条序对（A 优于 B）用于检查权重方向；当前 0 标注，故全部为先验。

## 4. 两段接口契约（stage-1 ↔ stage-2）

### 4.1 stage-1 职责（本件实现）
`triage 词 → 目标科室(kind 政策) → 召回并集(∩438家) → 硬过滤 → 场景加权打分 → K 剪枝 → 抓取顺序`。
**stage-1 输出 = 候选集 + 排序 + SlotQuery 序列（按静态分降序）。静态分绝不用来定生死：**
被 K 剪掉的医院不是“不行”，只是“本次预算内不抓”；K 之外的医院在下次查询/更大预算下仍可进入。

### 4.2 K 的确定规则（肘部/落差，不固定 TopN）
1. 相对地板：保留 `score ≥ s0 - 0.22`（s0=Top1 分）；
2. 相邻落差：首个 `s[i-1]-s[i] > max(0.05, 0.15×span)` 处切断（span=s0-末位）；
3. `K = min(两者)`，钳制 `[14, 60]`（实现常量 K_MIN=14 / K_MAX=60）。
回放结论（54 例）：K∈[14,60]，均值 41.9；近距 GT 召回 K=10→0.531、K=20→0.746、K=50→0.914。

### 4.3 SlotQuery（stage-2 入参，契约；本件只生成 stub 序列，**不发起请求**）
```jsonc
{
  "query_id": "q-<sha1(hospital|dept|date|loc)>",
  "hospital_id": "SH-xxx", "dept_id": "Dxxxxx",        // dept 取命中目标科室（≤5 个）
  "scenario": "red_flag",
  "user_loc": {"lng": 121.4737, "lat": 31.2304, "crs": "GCJ-02"},
  "earliest_from": "TODAY",                             // 最早可约日下界
  "max_wait_days": 7,                                   // 用户可接受等待（早停用）
  "price_cap_cny": null,                                // 挂号费上限（null=不限）
  "target_doctor": {"name": null, "title": null},       // 指定医生（职称 主任/副主任/主治）
  "timeout_ms": 8000,
  "order": 1, "budget_left": 12                         // 抓取顺序 + 剩余预算
}
```

### 4.4 SlotResult（stage-2 出参，契约）
```jsonc
{
  "query_id": "q-...", "hospital_id": "SH-xxx", "dept_id": "Dxxxxx",
  "status": "ok | no_slot | error",
  "earliest_slot": {"date": "YYYY-MM-DD", "time_range": "08:00-12:00"},  // 最早可约日
  "remaining_quota": 12,                                // 余量（null=不公开）
  "fee_tier": "15 | 50 | 100+ | unknown",               // 挂号费档（元）
  "doctors": [
    {"name": "...", "title": "主任医师", "expertise": ["冠心病","介入"], "is_target": true}
  ],                                                    // 医生职称与擅长、是否目标医生
  "fetched_at": "ISO8601",                              // 抓取时间戳
  "error_reason": null,                                 // 失败原因：timeout|anti_crawl|no_data|http_5xx|parse_fail
  "ttl_hint_min": 60                                    // 结果建议缓存时长
}
```

### 4.5 运行策略（契约级规定）
- **渐进式返回**：先立即返回 stage-1 静态结果（本件产物）；号源边抓边以 `SlotResult` 增量推送，终排 = 静态分 × 号源效用（可约时间/价格/诊疗优势/医生信息）——终排公式留待 stage-2 实现时定。
- **早停（已废弃, 2026-10-06）**：原规则"满足约束的 `SlotResult` 累计达 3 条即停"为逐院顺序抓取时代的设备预算规则; live 批量(prepare_live 单次 adb 会话)后逐院 resolve 零设备开销, 且早停会丢弃已核实 no_slot 事实 → 故仅保留预算 12 家, 12 家全部解析。
- **两级终排（2026-10-06 修订, 用户铁律）**：`finalize` 排序 = ① 号源状态优先级 **已核实有号 > 未核实 > 已核实无号**，② 同状态内 `-final_score`（final=静态分×号源效用）。有号是高权重独立特征：不够好的医院/科室/距离但有号 > 最好的医院/科室/距离但无号（挂号推荐的目的就是找到可挂的号）；已核实无号不剔除、仅沉底并显式写明原因（现在没号≠一直没号）；全部未核实(fallback)时全部落在"未核实"状态 → 终排序 ≡ stage-1 静态序（回落不变，断言 B 强制）。
- **预算上限**：每查询最多抓 `budget=12` 家；候选集 K 超出时**按静态分降序裁剪**（K 本身可 14~60，预算另计）。
- **stage-2 全失败回落**：所有 `status=error/no_slot` 时，回落 stage-1 静态排序展示，并显式标注“**暂无号源**（静态排序，号源抓取失败 N/N）”，不假装实时。
- **禁止事项**：本链路禁止发起号源/排班抓取、禁止全网全科室遍历、禁止为“有号”新增能力位（capabilities 仍 0/0/0）。

### 4.6 两阶段 live + 终裁 LLM（2026-10-06, 用户指令落地）
背景：9dc8 实跑证明 full 模式（逐医生主页六字段）≈10s/医生，1 小时只覆盖 6/12 院，前排全是档位先验。
有号是独立高权重特征，粗排不需要逐医生下钻。架构改为（全流程 2 次 LLM 推理）：

- **Phase A probe（快速探测）**：`fetch_slots.py --mode probe` 只读 L2 科室页一次 dump——
  「去预约」按钮（可约医生卡）/ 日期条（部门级有号·无号, 屏外项也在 XML, 无需横滑）/ 「暂无可预约」空态；
  未见按钮才有限上下滑（≤4 次）。不点医生主页。实测 60~135s/院（full 85~494s），预算内覆盖 top 12 院（--probe-n 默认 12）。
  信号映射：按钮可见→verified ok（card_btn）；日期条有号但无按钮→verified ok（bar_only, 弱信号）；
  空态或日期条全无号→verified no_slot；error→未核实。probe 结果走 resolve 层 3.5（deep 之后、档位之前）。
  2026-10-07 费档修复（f89d）：resolve 层序改为 profile→**deep(本run)**→快照→probe→档位→stub（deep 带费档且最新, 必须先于可能是 probe 写回的快照文件）；
  结论无费档时从快照文件回填（费档为慢变字段, 标记 `fee_from_stale`, web 显示"历史参考"）。
- **初排**：probe 事实进入两级终排（§4.5）→ 临时 top 序。
- **Phase B deep（深核）**：初排前 5 名（`--deep-n`, 默认 5）走 full 模式逐医生六字段——医生名单/费档/最早时段/号段样例，
  是终裁素材；deep 结果覆盖 probe（resolve 层 3 优先于 3.5）。
- **Phase C 终裁 LLM（第 2 次推理）**：`app/finalize_llm.py` 输入 = 患者摘要 + 目标科室 tier +
  明细列表（deep 已核实有号行）+ probe 摘要（其余已核实行）；输出严格 JSON final top3（rank/hospital_id/doctor/reason）。
  硬校验：医院必须∈明细列表、医生必须∈该院可约名单、rank 1..3 不重复、已核实无号院禁止入选（幻觉防护）。
  校验失败/LLM 不可用 → 回退确定性 top3（两级排序），不阻断交付；`final.final_llm` 记录 applied/confidence。
- **预算**：`--live-timeout`（总, 秒）按 55%/45% 切给 probe/deep；两阶段各自 `--deadline` 优雅停止 + 逐组增量落盘。
- **回退不变**：全未核实（无 live / 全失败）时终排序 ≡ stage-1 静态序（§4.5, 断言 B 不变）。

## 5. 离线预解算契约（只定 schema 与范围，不实现）

`slot_profile.json`（每 `(dept_id, hospital_id)` 一条；热路径优先）：
```jsonc
{
  "meta": {"version": "0.1", "generated_at": "<content-fingerprint>", "scope": "hot-path"},
  "profiles": [
    {
      "dept_id": "D04780", "hospital_id": "SH-002",
      "release_cycle": "T-7_daily | T-1 | realtime | unknown",  // 放号周期
      "bookable_lead_days": 7,                                  // 可约提前天数（null=常年约满）
      "tension": 0.0-1.0,                                       // 紧张度（余量/需求 的 7 日均值，null=无数据）
      "price_tier": "15 | 50 | 100+ | unknown",                 // 价格档
      "perennial_no_slot": false,                               // 常年无号（≥3 次观测全失败）
      "obs_count": 3, "last_fetched_at": "ISO8601"
    }
  ]
}
```
- **范围（热路径优先）**：只对 `triage 命中过的 dept × 拥有该科室的医院` 做快照（即 stage-1 召回过的 (dept,hospital) 对），**不做 438×10334 全遍历**；预计热路径对数 ≪ 全集（每查询召回 100~350 家 × ≤5 科室）。
- **用途**：stage-1 的下一版可用 `tension/perennial_no_slot` 替换“抓取顺序”为“抓取期望”；stage-2 终排可直接读档免抓。
- **实现前置条件**：`query_slots` 能力存在（当前 0 家）→ 本件**只写契约，一行抓取代码都不写**。

## 6. 可解释性与自检（实现已落地）

- 每家输出 `score` + **top-3 贡献项**（feature/权重/取值/贡献值/备注）+ **扣分项**（按“距满分丢失分”排序的 lost=w(1-v)）。
- `--check`（写 `out/self_check.json`）：438 家 × 5 场景全特征全总分有限（无 NaN/None）、norms 重建字节一致、stage-1 重复运行结果一致。
- 幂等：输出 `generated_at` 为输入内容指纹（lexicon+dept_index+manifest _index 的 sha1 前 16 位），无墙钟；连续两次 `--all` 全部产物字节级一致（已验证 9/9 文件）。
- 不触碰：`hospital_intel/`、`ssb_dept/` 全程只读。

## 7. 已知风险与“最可能需要真实号源/标注修正”的权重项

1. **`emergency` .10 与 red_flag 的 match 结构**：急诊科室仅 39/438 家可识别（canonical∋急诊），三甲急诊能力实际远强于该信号；“胸痛 Top 是否真的该给急诊院优先”只有号源（急诊绿色通道是否开放/等候）能验证。
2. **`chronic_fit` .10（分级诊疗先验）**：假设“低等级+p1 命中=适合复诊”，但社区中心慢病随访的真实可约性（是否挂得出号、慢病药能否开）完全依赖号源观测；错误方向会把用户推到约不到号的社区。
3. **`level` 映射（尤其 complex .42 / peds .04 两端）**：等级是代理变量，真实学科强度（如三甲 vs 强二甲专科）与“该病该看的医生是否放号”需要 ≥200 条场景标注+号源终排反馈才能校准。
