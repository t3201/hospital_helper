# 挂号推荐报告（运行期原型）

- 生成: 2026-10-05T16:18:55 | 模式: web | 权重推理: agentsvc(model_id=default)

## 1. 位置
- `121.442988, 31.252108`（GCJ-02）— 高德 amap_place 命中「中远两湾城」普陀区中远两湾城（缓存命中）

## 2. 病情采集（≤3 轮自由文本）
- 第1轮: 右下颌骨增生，或者是咬肌肥大，脸型不对称
- 解析: 患者=adult(age=48, 性别=男) 代挂=False 就诊=new 等待≤14天 费用上限=50 水平预期=balanced

## 3. 权重推理（LLM 直出: 特征权重向量 + 目标科室 + 约束; 含基础数据上下文）
- scenario: **community_first**（社区首诊/轻症）
- LLM 后端: agentsvc model_id=default @ http://127.0.0.1:7989; 实测耗时=21235.0ms（首字节见日志）attempts=1 思维链=3385字（仅记长度, 未并入答案）
- 基础数据上下文(随 prompt 下发, 全部只读): 可约科室 canonical 363 条(覆盖≥5 家医院) / 医院目录 438 家(等级/区县); 场景基线权重 5 套(仅参考)
- 主诉为下颌骨增生或咬肌肥大导致脸型不对称，非急重症且可等待14天，故不启用急诊与慢病权重，优先匹配口腔颌面外科/口腔外科等公立普通门诊。
- 费用上限50元且水平预期为balanced，倾向低等级或普通公立门诊承接，医院等级权重降低，科室命中与可及性权重提高。
- 患者位于普陀区且明确偏好地铁，提升地铁可达性权重，距离权重适中，便于就近选择可约科室。
- triage_term='脸型不对称' 症状词表无命中（不影响: 科室以 LLM 直选为准）
- 特征权重向量（LLM 直出绝对权重, 归一行和=1.0; 5 套场景基线仅作 prompt 参考）: {"match": 0.34, "level": 0.13, "breadth": 0.11, "distance": 0.26, "metro": 0.12, "bus": 0.02, "key_specialty": 0.02}
- 约束: max_wait_days=14, price_cap_cny=50

## 4. 候选集（stage-1, 复用 ranking/scripts/score_hospitals.py）
- triage: 主词="脸型不对称"（llm）→ 目标科室: 医疗美容科(p1/high); 口腔外科(p1/high); 口腔科(p1/high); 口腔颌面外科(p1/high); 整形外科(p1/high)
- 硬过滤: 召回=226 → 通过=92（弃=134: {"超出半径12km": 111, "BLOCKED": 22, "非医保": 1}）, 半径=12.0km
- K=60（肩部规则: {"n": 92, "s0": 0.9502, "floor": 0.7302, "gap_thr": 0.05, "i_floor": 69, "i_gap": 92}）

| # | 医院 | 等级 | 区 | 静态分 | 首要贡献 |
|---|---|---|---|---|---|
| 1 | 第十人民医院 | 三级甲等 | 静安区 | 0.9502 | 科室命中=1.0（命中: 整形外科 [p1/high]） |
| 2 | 同济医院 | 三级甲等 | 普陀区 | 0.9399 | 科室命中=1.0（命中: 口腔外科,口腔科 [p1/high]） |
| 3 | 仁济医院西院 | 三级甲等 | 黄浦区 | 0.9265 | 科室命中=1.0（命中: 口腔科,整形外科 [p1/high]） |
| 4 | 华山医院 | 三级甲等 | 静安区 | 0.9139 | 科室命中=1.0（命中: 口腔科,整形外科 [p1/high]） |
| 5 | 第一人民医院 | 三级甲等 | 虹口区 | 0.9067 | 科室命中=1.0（命中: 口腔科,整形外科 [p1/high]） |
| 6 | 华东医院 | 三级甲等 | 静安区 | 0.8929 | 科室命中=1.0（命中: 口腔科,整形外科 [p1/high]） |
| 7 | 市中西医结合医院 | 三级甲等 | 虹口区 | 0.8927 | 科室命中=1.0（命中: 医疗美容科,口腔科 [p1/high]） |
| 8 | 长征医院 | 三级甲等 | 黄浦区 | 0.89 | 科室命中=1.0（命中: 口腔科 [p1/high]） |
| 9 | 上海市中医医院（芷江院区） | 三级甲等 | 静安区 | 0.8894 | 科室命中=1.0（命中: 口腔科 [p1/high]） |
| 10 | 曙光医院西院 | 三级甲等 | 黄浦区 | 0.889 | 科室命中=1.0（命中: 口腔科 [p1/high]） |

## 5. 号源（缓存优先: profile → 观察快照 → 档位 → stub; TTL=90min）
| 医院 | 科室 | 来源 | 状态 | 时间戳 | 龄期(min) | 标注 |
|---|---|---|---|---|---|---|
| 第十人民医院 | 整形外科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 同济医院 | 口腔外科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 仁济医院西院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 华山医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 第一人民医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 华东医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 市中西医结合医院 | 医疗美容科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 长征医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 上海市中医医院（芷江院区） | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科医院, n_units=56, confidence=m |
| 曙光医院西院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科医院, n_units=56, confidence=m |
| 同济口腔医院 | 口腔颌面外科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidenc |
| 儿童医院北京西路院区 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidenc |

## 6. 推荐（1–3, 终排=静态分×号源效用; 未核实按静态分, 不决定生死）
> ⚠ 本批无可核实实时号源（抓取失败/观察过期/仅档位与stub）→ 按 RANKING_SPEC §4.5 回落到 stage-1 静态排序展示, 全部显式标注"号源未核实", 不假装实时。

### 1. 第十人民医院（三级甲等 / 静安区）
- **why**: 第十人民医院（三级甲等/静安区）: 科室命中=命中: 整形外科 [p1/high]（+0.34, match），距离就近=2.5km（+0.2335, distance）。号源未核实（cache/level_default(档位先验,未核实)）, 仅按静态分排序, 不得当实时事实。
- **evidence**: 静态=0.9502 × 号源效用=1.0 → 终排=0.9502
  - +0.340 match v=1.0（命中: 整形外科 [p1/high]）
  - +0.234 distance v=0.8981（2.5km）
  - +0.130 level v=1.0（三级甲等）
  - -0.026 distance 失分（2.5km）
  - -0.019 metro 失分（步行4.7m）
  - 号源: cache/level_default(档位先验,未核实) | status=unknown | fee=50 | 出处=slots/profiles/level_defaults.json
    档位估算（stratum=三级甲等|综合医院, n_units=383, confidence=high; 非实时, 不可当号源事实）
  - 标注: 号源未核实: 档位先验 level_defaults（stratum=三级甲等|综合医院, confidence=high, 非实时, 不当作号源事实）

### 2. 同济医院（三级甲等 / 普陀区）
- **why**: 同济医院（三级甲等/普陀区）: 科室命中=命中: 口腔外科,口腔科 [p1/high]（+0.34, match），距离就近=1.9km（+0.2399, distance）。号源未核实（cache/level_default(档位先验,未核实)）, 仅按静态分排序, 不得当实时事实。
- **evidence**: 静态=0.9399 × 号源效用=1.0 → 终排=0.9399
  - +0.340 match v=1.0（命中: 口腔外科,口腔科 [p1/high]）
  - +0.240 distance v=0.9227（1.9km）
  - +0.130 level v=1.0（三级甲等）
  - -0.020 distance 失分（1.9km）
  - -0.020 metro 失分（步行5.0m）
  - 号源: cache/level_default(档位先验,未核实) | status=unknown | fee=50 | 出处=slots/profiles/level_defaults.json
    档位估算（stratum=三级甲等|综合医院, n_units=383, confidence=high; 非实时, 不可当号源事实）
  - 标注: 号源未核实: 档位先验 level_defaults（stratum=三级甲等|综合医院, confidence=high, 非实时, 不当作号源事实）

### 3. 仁济医院西院（三级甲等 / 黄浦区）
- **why**: 仁济医院西院（三级甲等/黄浦区）: 科室命中=命中: 口腔科,整形外科 [p1/high]（+0.34, match），距离就近=4.4km（+0.2141, distance）。号源未核实（cache/level_default(档位先验,未核实)）, 仅按静态分排序, 不得当实时事实。
- **evidence**: 静态=0.9265 × 号源效用=1.0 → 终排=0.9265
  - +0.340 match v=1.0（命中: 口腔科,整形外科 [p1/high]）
  - +0.214 distance v=0.8236（4.4km）
  - +0.130 level v=1.0（三级甲等）
  - -0.046 distance 失分（4.4km）
  - -0.028 metro 失分（步行6.9m）
  - 号源: cache/level_default(档位先验,未核实) | status=unknown | fee=50 | 出处=slots/profiles/level_defaults.json
    档位估算（stratum=三级甲等|综合医院, n_units=383, confidence=high; 非实时, 不可当号源事实）
  - 标注: 号源未核实: 档位先验 level_defaults（stratum=三级甲等|综合医院, confidence=high, 非实时, 不当作号源事实）

## 7. Top20 人工甄别列表（未核实号源显式标注; 全候选集 K=60 参与排序, 不剔除已查询者）
| 终排# | 医院 | 等级 | 区 | 静态 | 号源效用 | 终排 | 号源来源 | 最早可约 |
|---|---|---|---|---|---|---|---|---|
| 1 | 第十人民医院 | 三级甲等 | 静安区 | 0.9502 | 1.0 | 0.9502 | cache/level_default(档位先验,未核实) | — |
| 2 | 同济医院 | 三级甲等 | 普陀区 | 0.9399 | 1.0 | 0.9399 | cache/level_default(档位先验,未核实) | — |
| 3 | 仁济医院西院 | 三级甲等 | 黄浦区 | 0.9265 | 1.0 | 0.9265 | cache/level_default(档位先验,未核实) | — |
| 4 | 华山医院 | 三级甲等 | 静安区 | 0.9139 | 1.0 | 0.9139 | cache/level_default(档位先验,未核实) | — |
| 5 | 第一人民医院 | 三级甲等 | 虹口区 | 0.9067 | 1.0 | 0.9067 | cache/level_default(档位先验,未核实) | — |
| 6 | 华东医院 | 三级甲等 | 静安区 | 0.8929 | 1.0 | 0.8929 | cache/level_default(档位先验,未核实) | — |
| 7 | 市中西医结合医院 | 三级甲等 | 虹口区 | 0.8927 | 1.0 | 0.8927 | cache/level_default(档位先验,未核实) | — |
| 8 | 长征医院 | 三级甲等 | 黄浦区 | 0.89 | 1.0 | 0.89 | cache/level_default(档位先验,未核实) | — |
| 9 | 上海市中医医院（芷江院区） | 三级甲等 | 静安区 | 0.8894 | 1.0 | 0.8894 | cache/level_default(档位先验,未核实) | — |
| 10 | 曙光医院西院 | 三级甲等 | 黄浦区 | 0.889 | 1.0 | 0.889 | cache/level_default(档位先验,未核实) | — |
| 11 | 同济口腔医院 | 三级甲等 | 普陀区 | 0.8878 | 1.0 | 0.8878 | cache/level_default(档位先验,未核实) | — |
| 12 | 儿童医院北京西路院区 | 三级甲等 | 静安区 | 0.8826 | 1.0 | 0.8826 | cache/level_default(档位先验,未核实) | — |
| 13 | 儿童医学中心 | 三级甲等 | 浦东新区 | 0.8735 | 1.0 | 0.8735 | 未查询(无号源信息) | — |
| 14 | 国际和平妇幼保健院 | 三级甲等 | 徐汇区 | 0.8725 | 1.0 | 0.8725 | 未查询(无号源信息) | — |
| 15 | 普陀区人民医院 | 二级甲等 | 普陀区 | 0.8712 | 1.0 | 0.8712 | 未查询(无号源信息) | — |
| 16 | 中山医院 | 三级甲等 | 徐汇区 | 0.8682 | 1.0 | 0.8682 | 未查询(无号源信息) | — |
| 17 | 瑞金医院总院 | 三级甲等 | 黄浦区 | 0.8677 | 1.0 | 0.8677 | 未查询(无号源信息) | — |
| 18 | 岳阳中西医结合医院 | 三级甲等 | 虹口区 | 0.8666 | 1.0 | 0.8666 | 未查询(无号源信息) | — |
| 19 | 新华医院 | 三级甲等 | 杨浦区 | 0.8622 | 1.0 | 0.8622 | 未查询(无号源信息) | — |
| 20 | 龙华医院 | 三级甲等 | 徐汇区 | 0.8602 | 1.0 | 0.8602 | 未查询(无号源信息) | — |

> Top20 中 20 家号源未核实（上表已逐行标注）；本报告不给出任何无出处的号源结论。

## 8. 模拟挂号（dry-run, 默认开）
- 1. 打开「随申办」App（包名 mobi.w3studio.apps.android.shsmy.phone）
- 2. 首页 →「预约挂号」→「全部医院 (444)」
- 3. 检索并选择医院: 第十人民医院（静安区, 主院区）
- 4. 科室页找到「整形外科」tab 并进入（L2 页自带 14 天日期条）
- 5. 选择日期: 待定（最早号源未核实, 到院/到页后再选）（最早可约; 日期条若显示无号则换日期/医生）
- 6. 选择医生与时段 →「去预约」（医生列表来自 SlotResult.doctors, is_target 标记目标医生）
- 7. 填写实名信息 + 短信验证码（用户手动完成, 脚本不触碰凭据）
- 8. 确认预约 → 支付（用户手动完成）

> dry-run（默认开）: 仅打印将执行的步骤序列; 不真发、不提交、不支付。

## 9. 数据与已知局限
- 特征权重向量为 LLM 直出（prompt 附 5 套场景基线权重表作参考; 槽位效用系数等启发式仍未调参, 见 RANKING_SPEC §3/§7）, 非个性化医疗建议。
- 号源 TTL=90min；过期观察仅作陈旧参考, 不计入已核实；档位/stub 恒标注未核实; **未核实一律中性效用, 不决定生死**。
- 症状原文（含否定表述）直接交给 LLM 理解; 旧规则引擎的关键词匹配与否定抑制已随 --llm rules 废除。
- 患者层科室过滤: 成人剔除儿科类目标科室, 儿童剔除成人科目标科室（app 层后处理, 词表本身不改）。
- 目标科室 = LLM 从归一化词表（canonical, 覆盖≥5 家医院）直选; 无效条目回落到 LLM triage_term → 症状词表解析; 词表外词经同义词桥映射, 映射不上如实标注, 不编造科室。Agent Service 不可用 → 运行失败退出（不静默回退）。
- 原型不真实挂号/提交/支付（450 家 capabilities.book=false）; 不修改 ranking/、hospital_intel/、ssb_dept/、slots/。
- key_specialty 覆盖 4.4%、metro 172/450 缺失 → 中性策略并已在 explain 中标注。

## 10. 回归断言（--check 同款, 任一失败进程非零退出）
- **PASS** 已查询预算内前 12 家（candidates_static[:12]）⊆ final.top_list
- **PASS** fallback_all_unverified → top_list 前 20 项与静态序同序（§4.5 回落）
- **PASS** max(静态分) 保留: top_list 内最大静态分 == 候选集最大静态分（0.9502）
- **PASS** slot_queries 的每家医院 ∈ final.top_list ∪ final.excluded（显式排除+原因, 不得静默消失）

## 附: SlotQuery 序列（§4.3 契约, 仅生成不发起）
- #1 SH-084 depts=['D05229'] wait≤14天 cap=50 static=0.9502
- #2 SH-071 depts=['D03200', 'D03244'] wait≤14天 cap=50 static=0.9399
- #3 SH-030 depts=['D03244', 'D05229'] wait≤14天 cap=50 static=0.9265
- #4 SH-003 depts=['D03244', 'D05229'] wait≤14天 cap=50 static=0.9139
- #5 SH-095 depts=['D03244', 'D05229'] wait≤14天 cap=50 static=0.9067
- #6 SH-007 depts=['D03244', 'D05229'] wait≤14天 cap=50 static=0.8929
- #7 SH-064 depts=['D03244', 'D02969'] wait≤14天 cap=50 static=0.8927
- #8 SH-053 depts=['D03244'] wait≤14天 cap=50 static=0.89
- #9 SH-045 depts=['D03244'] wait≤14天 cap=50 static=0.8894
- #10 SH-075 depts=['D03244'] wait≤14天 cap=50 static=0.889
- #11 SH-015 depts=['D03308'] wait≤14天 cap=50 static=0.8878
- #12 SH-026 depts=['D03244'] wait≤14天 cap=50 static=0.8826
