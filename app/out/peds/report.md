# 挂号推荐报告（运行期原型）

- 生成: 2026-10-04T23:27:18 | 模式: demo:peds | 权重推理: agentsvc(model_id=default)

## 1. 位置
- `121.4345, 31.1955`（GCJ-02）— demo 固定点: 徐汇区宜山路/漕溪北路一带（GCJ-02）

## 2. 病情采集（≤3 轮自由文本）
- 第1轮: 4岁男孩，近一年身高长得慢，身高98cm明显低于同龄人，家长担心生长发育想评估一下
- 第2轮: 儿童，初诊，两周内可以
- 第3轮: 均衡，费用100元以内
- 解析: 患者=child(age=4, 性别=男) 代挂=True 就诊=new 等待≤14天 费用上限=100 水平预期=balanced

## 3. 权重推理（LLM 直出: 特征权重向量 + 目标科室 + 约束; 含基础数据上下文）
- scenario: **peds**（儿科）
- LLM 后端: agentsvc model_id=default @ http://127.0.0.1:7989; 实测耗时=67937.0ms（首字节见日志）attempts=1 思维链=8253字（仅记长度, 未并入答案）
- 基础数据上下文(随 prompt 下发, 全部只读): 可约科室 canonical 363 条(覆盖≥5 家医院) / 医院目录 438 家(等级/区县); 场景基线权重 5 套(仅参考)
- 4岁男孩身高增长缓慢属于儿童生长发育初筛，非急症，两周内可约，因此match与distance权重较高，emergency为0。
- 费用上限100元且水平预期均衡，优先就近可负担的儿童保健科初筛，level权重压低，breadth适中，key_specialty仅对儿童生长发育保留小权重。
- 无地铁偏好且公交权重低，选择可约科室组合：儿童保健科首推，儿童生长发育作专病补充，儿科兜底。
- triage_term='身高增长缓慢' 症状词表无命中（不影响: 科室以 LLM 直选为准）
- 特征权重向量（LLM 直出绝对权重, 归一行和=1.0; 5 套场景基线仅作 prompt 参考）: {"match": 0.42, "level": 0.06, "breadth": 0.12, "distance": 0.3, "metro": 0.02, "bus": 0.04, "key_specialty": 0.04}
- 约束: max_wait_days=14, price_cap_cny=100

## 4. 候选集（stage-1, 复用 ranking/scripts/score_hospitals.py）
- triage: 主词="身高增长缓慢"（llm）→ 目标科室: 儿科(p1/high); 儿童保健科(p1/high); 儿童生长发育(p1/high)
- 硬过滤: 召回=198 → 通过=106（弃=92: {"超出半径20km": 70, "BLOCKED": 21, "非医保": 1}）, 半径=20.0km
- K=60（肩部规则: {"n": 106, "s0": 0.9429, "floor": 0.7229, "gap_thr": 0.0519, "i_floor": 75, "i_gap": 106}）

| # | 医院 | 等级 | 区 | 静态分 | 首要贡献 |
|---|---|---|---|---|---|
| 1 | 第六人民医院 | 三级甲等 | 徐汇区 | 0.9429 | 科室命中=1.0（命中: 儿科,儿童保健科 [p1/high]） |
| 2 | 仁济医院西院 | 三级甲等 | 黄浦区 | 0.9186 | 科室命中=1.0（命中: 儿科 [p1/high]） |
| 3 | 龙华医院 | 三级甲等 | 徐汇区 | 0.9183 | 科室命中=1.0（命中: 儿科 [p1/high]） |
| 4 | 儿科医院 | 三级甲等 | 闵行区 | 0.8995 | 科室命中=1.0（命中: 儿童保健科 [p1/high]） |
| 5 | 长征医院 | 三级甲等 | 黄浦区 | 0.8993 | 科室命中=1.0（命中: 儿科 [p1/high]） |
| 6 | 儿童医学中心 | 三级甲等 | 浦东新区 | 0.8934 | 科室命中=1.0（命中: 儿童保健科 [p1/high]） |
| 7 | 曙光医院西院 | 三级甲等 | 黄浦区 | 0.8882 | 科室命中=1.0（命中: 儿科 [p1/high]） |
| 8 | 瑞金医院总院 | 三级甲等 | 黄浦区 | 0.8867 | 科室命中=1.0（命中: 儿科 [p1/high]） |
| 9 | 第八人民医院 | 二级甲等 | 徐汇区 | 0.883 | 科室命中=1.0（命中: 儿科 [p1/high]） |
| 10 | 第十人民医院 | 三级甲等 | 静安区 | 0.8821 | 科室命中=1.0（命中: 儿科 [p1/high]） |

## 5. 号源（缓存优先: profile → 观察快照 → 档位 → stub; TTL=90min）
| 医院 | 科室 | 来源 | 状态 | 时间戳 | 龄期(min) | 标注 |
|---|---|---|---|---|---|---|
| 第六人民医院 | 儿科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 仁济医院西院 | 儿科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 龙华医院 | 儿科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科医院, n_units=56, confidence=m |
| 儿科医院 | 儿童保健科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidenc |
| 长征医院 | 儿科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 儿童医学中心 | 儿童保健科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidenc |
| 曙光医院西院 | 儿科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科医院, n_units=56, confidence=m |
| 瑞金医院总院 | 儿科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 第八人民医院 | 儿科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|专科疾病防治院, n_units=57, confidenc |
| 第十人民医院 | 儿科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 大华医院 | 儿科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级乙等|综合医院, n_units=43, confidence=l |
| 儿童医院北京西路院区 | 儿童保健科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidenc |

## 6. 推荐（1–3, 终排=静态分×号源效用; 未核实按静态分, 不决定生死）
> ⚠ 本批无可核实实时号源（抓取失败/观察过期/仅档位与stub）→ 按 RANKING_SPEC §4.5 回落到 stage-1 静态排序展示, 全部显式标注"号源未核实", 不假装实时。

### 1. 第六人民医院（三级甲等 / 徐汇区）
- **why**: 第六人民医院（三级甲等/徐汇区）: 科室命中=命中: 儿科,儿童保健科 [p1/high]（+0.42, match），距离就近=2.1km（+0.2752, distance）。号源未核实（cache/level_default(档位先验,未核实)）, 仅按静态分排序, 不得当实时事实。
- **evidence**: 静态=0.9429 × 号源效用=1.0 → 终排=0.9429
  - +0.420 match v=1.0（命中: 儿科,儿童保健科 [p1/high]）
  - +0.275 distance v=0.9174（2.1km）
  - +0.120 breadth v=1.0（l2=353）
  - -0.025 distance 失分（2.1km）
  - -0.025 bus 失分（1站）
  - 号源: cache/level_default(档位先验,未核实) | status=unknown | fee=50 | 出处=slots/profiles/level_defaults.json
    档位估算（stratum=三级甲等|综合医院, n_units=383, confidence=high; 非实时, 不可当号源事实）
  - 标注: 号源未核实: 档位先验 level_defaults（stratum=三级甲等|综合医院, confidence=high, 非实时, 不当作号源事实）

### 2. 仁济医院西院（三级甲等 / 黄浦区）
- **why**: 仁济医院西院（三级甲等/黄浦区）: 科室命中=命中: 儿科 [p1/high]（+0.42, match），距离就近=6.4km（+0.2232, distance）。号源未核实（cache/level_default(档位先验,未核实)）, 仅按静态分排序, 不得当实时事实。
- **evidence**: 静态=0.9186 × 号源效用=1.0 → 终排=0.9186
  - +0.420 match v=1.0（命中: 儿科 [p1/high]）
  - +0.223 distance v=0.7441（6.4km）
  - +0.120 breadth v=1.0（l2=208）
  - -0.077 distance 失分（6.4km）
  - 号源: cache/level_default(档位先验,未核实) | status=unknown | fee=50 | 出处=slots/profiles/level_defaults.json
    档位估算（stratum=三级甲等|综合医院, n_units=383, confidence=high; 非实时, 不可当号源事实）
  - 标注: 号源未核实: 档位先验 level_defaults（stratum=三级甲等|综合医院, confidence=high, 非实时, 不当作号源事实）

### 3. 长征医院（三级甲等 / 黄浦区）
- **why**: 长征医院（三级甲等/黄浦区）: 科室命中=命中: 儿科 [p1/high]（+0.42, match），距离就近=5.2km（+0.2378, distance）。号源未核实（cache/level_default(档位先验,未核实)）, 仅按静态分排序, 不得当实时事实。
- **evidence**: 静态=0.8993 × 号源效用=1.0 → 终排=0.8993
  - +0.420 match v=1.0（命中: 儿科 [p1/high]）
  - +0.238 distance v=0.7927（5.2km）
  - +0.094 breadth v=0.7842（l2=48）
  - -0.062 distance 失分（5.2km）
  - -0.026 breadth 失分（l2=48）
  - 号源: cache/level_default(档位先验,未核实) | status=unknown | fee=50 | 出处=slots/profiles/level_defaults.json
    档位估算（stratum=三级甲等|综合医院, n_units=383, confidence=high; 非实时, 不可当号源事实）
  - 标注: 号源未核实: 档位先验 level_defaults（stratum=三级甲等|综合医院, confidence=high, 非实时, 不当作号源事实）

## 7. Top20 人工甄别列表（未核实号源显式标注; 全候选集 K=60 参与排序, 不剔除已查询者）
| 终排# | 医院 | 等级 | 区 | 静态 | 号源效用 | 终排 | 号源来源 | 最早可约 |
|---|---|---|---|---|---|---|---|---|
| 1 | 第六人民医院 | 三级甲等 | 徐汇区 | 0.9429 | 1.0 | 0.9429 | cache/level_default(档位先验,未核实) | — |
| 2 | 仁济医院西院 | 三级甲等 | 黄浦区 | 0.9186 | 1.0 | 0.9186 | cache/level_default(档位先验,未核实) | — |
| 3 | 龙华医院 | 三级甲等 | 徐汇区 | 0.9183 | 1.0 | 0.9183 | cache/level_default(档位先验,未核实) | — |
| 4 | 儿科医院 | 三级甲等 | 闵行区 | 0.8995 | 1.0 | 0.8995 | cache/level_default(档位先验,未核实) | — |
| 5 | 长征医院 | 三级甲等 | 黄浦区 | 0.8993 | 1.0 | 0.8993 | cache/level_default(档位先验,未核实) | — |
| 6 | 儿童医学中心 | 三级甲等 | 浦东新区 | 0.8934 | 1.0 | 0.8934 | cache/level_default(档位先验,未核实) | — |
| 7 | 曙光医院西院 | 三级甲等 | 黄浦区 | 0.8882 | 1.0 | 0.8882 | cache/level_default(档位先验,未核实) | — |
| 8 | 瑞金医院总院 | 三级甲等 | 黄浦区 | 0.8867 | 1.0 | 0.8867 | cache/level_default(档位先验,未核实) | — |
| 9 | 第八人民医院 | 二级甲等 | 徐汇区 | 0.883 | 1.0 | 0.883 | cache/level_default(档位先验,未核实) | — |
| 10 | 第十人民医院 | 三级甲等 | 静安区 | 0.8821 | 1.0 | 0.8821 | cache/level_default(档位先验,未核实) | — |
| 11 | 大华医院 | 二级乙等 | 徐汇区 | 0.8714 | 1.0 | 0.8714 | cache/level_default(档位先验,未核实) | — |
| 12 | 儿童医院北京西路院区 | 三级甲等 | 静安区 | 0.8713 | 1.0 | 0.8713 | cache/level_default(档位先验,未核实) | — |
| 13 | 海军第九〇五医院 | 二级甲等 | 长宁区 | 0.8667 | 1.0 | 0.8667 | 未查询(无号源信息) | — |
| 14 | 同济医院 | 三级甲等 | 普陀区 | 0.8624 | 1.0 | 0.8624 | 未查询(无号源信息) | — |
| 15 | 同仁医院 | 三级乙等 | 长宁区 | 0.8619 | 1.0 | 0.8619 | 未查询(无号源信息) | — |
| 16 | 岳阳中西医结合医院青海路门诊部 | 三级甲等 | 静安区 | 0.8564 | 1.0 | 0.8564 | 未查询(无号源信息) | — |
| 17 | 瑞金医院卢湾分院 | 二级甲等 | 黄浦区 | 0.8542 | 1.0 | 0.8542 | 未查询(无号源信息) | — |
| 18 | 长宁区妇幼保健院 | 二级甲等 | 长宁区 | 0.8503 | 1.0 | 0.8503 | 未查询(无号源信息) | — |
| 19 | 普陀区中心医院 | 三级乙等 | 普陀区 | 0.8482 | 1.0 | 0.8482 | 未查询(无号源信息) | — |
| 20 | 上海市中医医院（芷江院区） | 三级甲等 | 静安区 | 0.8424 | 1.0 | 0.8424 | 未查询(无号源信息) | — |

> Top20 中 20 家号源未核实（上表已逐行标注）；本报告不给出任何无出处的号源结论。

## 8. 模拟挂号（dry-run, 默认开）
- 1. 打开「随申办」App（包名 mobi.w3studio.apps.android.shsmy.phone）
- 2. 首页 →「预约挂号」→「全部医院 (444)」
- 3. 检索并选择医院: 第六人民医院（徐汇区, 主院区）
- 4. 科室页找到「儿科」tab 并进入（L2 页自带 14 天日期条）
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
- **PASS** max(静态分) 保留: top_list 内最大静态分 == 候选集最大静态分（0.9429）
- **PASS** slot_queries 的每家医院 ∈ final.top_list ∪ final.excluded（显式排除+原因, 不得静默消失）

## 附: SlotQuery 序列（§4.3 契约, 仅生成不发起）
- #1 SH-087 depts=['D02076', 'D02013'] wait≤14天 cap=100 static=0.9429
- #2 SH-030 depts=['D02013'] wait≤14天 cap=100 static=0.9186
- #3 SH-065 depts=['D02013'] wait≤14天 cap=100 static=0.9183
- #4 SH-074 depts=['D02076'] wait≤14天 cap=100 static=0.8995
- #5 SH-053 depts=['D02013'] wait≤14天 cap=100 static=0.8993
- #6 SH-018 depts=['D02076'] wait≤14天 cap=100 static=0.8934
- #7 SH-075 depts=['D02013'] wait≤14天 cap=100 static=0.8882
- #8 SH-036 depts=['D02013'] wait≤14天 cap=100 static=0.8867
- #9 SH-172 depts=['D02013'] wait≤14天 cap=100 static=0.883
- #10 SH-084 depts=['D02013'] wait≤14天 cap=100 static=0.8821
- #11 SH-121 depts=['D02190', 'D02013'] wait≤14天 cap=100 static=0.8714
- #12 SH-026 depts=['D02076'] wait≤14天 cap=100 static=0.8713
