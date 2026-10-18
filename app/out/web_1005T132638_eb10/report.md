# 挂号推荐报告（运行期原型）

- 生成: 2026-10-05T13:26:38 | 模式: web | 权重推理: agentsvc(model_id=default)

## 1. 位置
- `121.422376, 31.178477`（GCJ-02）— 高德 amap_place 命中「上海交通大学医学院附属第六人民医院(徐汇院区)」徐汇区上海交通大学医学院附属第六人民医院(徐汇院区)

## 2. 病情采集（≤3 轮自由文本）
- 第1轮: 胸口压榨样疼痛2小时, 向左肩放射, 伴出冷汗, 68岁男性
- 解析: 患者=elderly(age=68, 性别=男) 代挂=False 就诊=new 等待≤0天 费用上限=None 水平预期=top

## 3. 权重推理（LLM 直出: 特征权重向量 + 目标科室 + 约束; 含基础数据上下文）
- scenario: **red_flag**（红旗急症）
- LLM 后端: agentsvc model_id=default @ http://127.0.0.1:7989; 实测耗时=27282.0ms（首字节见日志）attempts=1 思维链=3574字（仅记长度, 未并入答案）
- 基础数据上下文(随 prompt 下发, 全部只读): 可约科室 canonical 363 条(覆盖≥5 家医院) / 医院目录 438 家(等级/区县); 场景基线权重 5 套(仅参考)
- 胸口压榨样疼痛、向左肩放射、出冷汗且68岁男性，符合急性冠脉综合征红旗，优先急诊内科/急诊科并快速转入心血管内科，因此emergency、match、level权重最高。
- 患者期望等待0天且位置在第六人民医院附近，距离权重适当提高，metro/bus因急性急诊和地铁偏好为False而压低。
- 水平预期为top且费用上限无，优先三甲急诊能力；非慢病随访场景，chronic与chronic_fit为0。
- triage_term='胸口压榨样疼痛' 词表交叉核对: 词表指向 疼痛科/疼痛/疼痛康复, LLM 选了 冠心病/心血管内科/急诊内科 — 以 LLM 为准, 词表仅供参考
- 特征权重向量（LLM 直出绝对权重, 归一行和=1.0; 5 套场景基线仅作 prompt 参考）: {"match": 0.26, "level": 0.2, "breadth": 0.02, "distance": 0.12, "metro": 0.02, "bus": 0.01, "key_specialty": 0.03, "emergency": 0.34}
- 约束: max_wait_days=0, price_cap_cny=None

## 4. 候选集（stage-1, 复用 ranking/scripts/score_hospitals.py）
- triage: 主词="胸口压榨样疼痛"（llm）→ 目标科室: 冠心病(p1/high); 心血管内科(p1/high); 急诊内科(p1/high); 急诊科(p1/high)
- 硬过滤: 召回=118 → 通过=58（弃=60: {"超出半径15km": 53, "BLOCKED": 7}）, 半径=15.0km
- K=19（肩部规则: {"n": 58, "s0": 0.9853, "floor": 0.7653, "gap_thr": 0.0869, "i_floor": 19, "i_gap": 19}）

| # | 医院 | 等级 | 区 | 静态分 | 首要贡献 |
|---|---|---|---|---|---|
| 1 | 第六人民医院 | 三级甲等 | 徐汇区 | 0.9853 | 急诊能力=1.0（有急诊类科室） |
| 2 | 华山医院 | 三级甲等 | 静安区 | 0.9692 | 急诊能力=1.0（有急诊类科室） |
| 3 | 中山医院 | 三级甲等 | 徐汇区 | 0.9688 | 急诊能力=1.0（有急诊类科室） |
| 4 | 儿科医院 | 三级甲等 | 闵行区 | 0.9684 | 急诊能力=1.0（有急诊类科室） |
| 5 | 儿童医学中心 | 三级甲等 | 浦东新区 | 0.9468 | 急诊能力=1.0（有急诊类科室） |
| 6 | 胸科医院 | 三级甲等 | 徐汇区 | 0.9429 | 急诊能力=1.0（有急诊类科室） |
| 7 | 华东医院 | 三级甲等 | 静安区 | 0.9389 | 急诊能力=1.0（有急诊类科室） |
| 8 | 儿童医院北京西路院区 | 三级甲等 | 静安区 | 0.9278 | 急诊能力=1.0（有急诊类科室） |
| 9 | 岳阳中西医结合医院青海路门诊部 | 三级甲等 | 静安区 | 0.922 | 急诊能力=1.0（有急诊类科室） |
| 10 | 儿童医院泸定路院区 | 三级甲等 | 普陀区 | 0.9208 | 急诊能力=1.0（有急诊类科室） |

## 5. 号源（缓存优先: profile → 观察快照 → 档位 → stub; TTL=90min）
| 医院 | 科室 | 来源 | 状态 | 时间戳 | 龄期(min) | 标注 |
|---|---|---|---|---|---|---|
| 第六人民医院 | 心血管内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 华山医院 | 心血管内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 中山医院 | 心血管内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 儿科医院 | 心血管内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidenc |
| 儿童医学中心 | 心血管内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidenc |
| 胸科医院 | 心血管内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidenc |
| 华东医院 | 心血管内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 儿童医院北京西路院区 | 心血管内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidenc |
| 岳阳中西医结合医院青海路门诊部 | 心血管内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科医院, n_units=56, confidence=m |
| 儿童医院泸定路院区 | 心血管内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidenc |
| 上海市中医医院（芷江院区） | 冠心病 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科医院, n_units=56, confidence=m |
| 岳阳中西医结合医院 | 心血管内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科医院, n_units=56, confidence=m |

## 6. 推荐（1–3, 终排=静态分×号源效用; 未核实按静态分, 不决定生死）
> ⚠ 本批无可核实实时号源（抓取失败/观察过期/仅档位与stub）→ 按 RANKING_SPEC §4.5 回落到 stage-1 静态排序展示, 全部显式标注"号源未核实", 不假装实时。

### 1. 第六人民医院（三级甲等 / 徐汇区）
- **why**: 第六人民医院（三级甲等/徐汇区）: 急诊能力=有急诊类科室（+0.34, emergency），科室命中=命中: 心血管内科 [p1/high]（+0.26, match）。号源未核实（cache/level_default(档位先验,未核实)）, 仅按静态分排序, 不得当实时事实。
- **evidence**: 静态=0.9853 × 号源效用=1.0 → 终排=0.9853
  - +0.340 emergency v=1.0（有急诊类科室）
  - +0.260 match v=1.0（命中: 心血管内科 [p1/high]）
  - +0.200 level v=1.0（三级甲等）
  - -0.008 metro 失分（步行11.7m）
  - -0.006 bus 失分（1站）
  - 号源: cache/level_default(档位先验,未核实) | status=unknown | fee=50 | 出处=slots/profiles/level_defaults.json
    档位估算（stratum=三级甲等|综合医院, n_units=383, confidence=high; 非实时, 不可当号源事实）
  - 标注: 号源未核实: 档位先验 level_defaults（stratum=三级甲等|综合医院, confidence=high, 非实时, 不当作号源事实）

### 2. 华山医院（三级甲等 / 静安区）
- **why**: 华山医院（三级甲等/静安区）: 急诊能力=有急诊类科室（+0.34, emergency），科室命中=命中: 心血管内科 [p1/high]（+0.26, match）。号源未核实（cache/level_default(档位先验,未核实)）, 仅按静态分排序, 不得当实时事实。
- **evidence**: 静态=0.9692 × 号源效用=1.0 → 终排=0.9692
  - +0.340 emergency v=1.0（有急诊类科室）
  - +0.260 match v=1.0（命中: 心血管内科 [p1/high]）
  - +0.200 level v=1.0（三级甲等）
  - -0.022 distance 失分（4.6km）
  - -0.006 metro 失分（步行9.8m）
  - 号源: cache/level_default(档位先验,未核实) | status=unknown | fee=50 | 出处=slots/profiles/level_defaults.json
    档位估算（stratum=三级甲等|综合医院, n_units=383, confidence=high; 非实时, 不可当号源事实）
  - 标注: 号源未核实: 档位先验 level_defaults（stratum=三级甲等|综合医院, confidence=high, 非实时, 不当作号源事实）

### 3. 中山医院（三级甲等 / 徐汇区）
- **why**: 中山医院（三级甲等/徐汇区）: 急诊能力=有急诊类科室（+0.34, emergency），科室命中=命中: 心血管内科,急诊科 [p1/high]（+0.26, match）。号源未核实（cache/level_default(档位先验,未核实)）, 仅按静态分排序, 不得当实时事实。
- **evidence**: 静态=0.9688 × 号源效用=1.0 → 终排=0.9688
  - +0.340 emergency v=1.0（有急诊类科室）
  - +0.260 match v=1.0（命中: 心血管内科,急诊科 [p1/high]）
  - +0.200 level v=1.0（三级甲等）
  - -0.018 distance 失分（3.8km）
  - -0.008 metro 失分（步行12.1m）
  - 号源: cache/level_default(档位先验,未核实) | status=unknown | fee=50 | 出处=slots/profiles/level_defaults.json
    档位估算（stratum=三级甲等|综合医院, n_units=383, confidence=high; 非实时, 不可当号源事实）
  - 标注: 号源未核实: 档位先验 level_defaults（stratum=三级甲等|综合医院, confidence=high, 非实时, 不当作号源事实）

## 7. Top19 人工甄别列表（未核实号源显式标注; 全候选集 K=19 参与排序, 不剔除已查询者）
| 终排# | 医院 | 等级 | 区 | 静态 | 号源效用 | 终排 | 号源来源 | 最早可约 |
|---|---|---|---|---|---|---|---|---|
| 1 | 第六人民医院 | 三级甲等 | 徐汇区 | 0.9853 | 1.0 | 0.9853 | cache/level_default(档位先验,未核实) | — |
| 2 | 华山医院 | 三级甲等 | 静安区 | 0.9692 | 1.0 | 0.9692 | cache/level_default(档位先验,未核实) | — |
| 3 | 中山医院 | 三级甲等 | 徐汇区 | 0.9688 | 1.0 | 0.9688 | cache/level_default(档位先验,未核实) | — |
| 4 | 儿科医院 | 三级甲等 | 闵行区 | 0.9684 | 1.0 | 0.9684 | cache/level_default(档位先验,未核实) | — |
| 5 | 儿童医学中心 | 三级甲等 | 浦东新区 | 0.9468 | 1.0 | 0.9468 | cache/level_default(档位先验,未核实) | — |
| 6 | 胸科医院 | 三级甲等 | 徐汇区 | 0.9429 | 1.0 | 0.9429 | cache/level_default(档位先验,未核实) | — |
| 7 | 华东医院 | 三级甲等 | 静安区 | 0.9389 | 1.0 | 0.9389 | cache/level_default(档位先验,未核实) | — |
| 8 | 儿童医院北京西路院区 | 三级甲等 | 静安区 | 0.9278 | 1.0 | 0.9278 | cache/level_default(档位先验,未核实) | — |
| 9 | 岳阳中西医结合医院青海路门诊部 | 三级甲等 | 静安区 | 0.922 | 1.0 | 0.922 | cache/level_default(档位先验,未核实) | — |
| 10 | 儿童医院泸定路院区 | 三级甲等 | 普陀区 | 0.9208 | 1.0 | 0.9208 | cache/level_default(档位先验,未核实) | — |
| 11 | 上海市中医医院（芷江院区） | 三级甲等 | 静安区 | 0.9089 | 1.0 | 0.9089 | cache/level_default(档位先验,未核实) | — |
| 12 | 岳阳中西医结合医院 | 三级甲等 | 虹口区 | 0.8951 | 1.0 | 0.8951 | cache/level_default(档位先验,未核实) | — |
| 13 | 新华医院 | 三级甲等 | 杨浦区 | 0.8926 | 1.0 | 0.8926 | 未查询(无号源信息) | — |
| 14 | 同仁医院 | 三级乙等 | 长宁区 | 0.8775 | 1.0 | 0.8775 | 未查询(无号源信息) | — |
| 15 | 海军第九〇五医院 | 二级甲等 | 长宁区 | 0.8368 | 1.0 | 0.8368 | 未查询(无号源信息) | — |
| 16 | 瑞金医院卢湾分院 | 二级甲等 | 黄浦区 | 0.8304 | 1.0 | 0.8304 | 未查询(无号源信息) | — |
| 17 | 浦南医院 | 二级甲等 | 浦东新区 | 0.8212 | 1.0 | 0.8212 | 未查询(无号源信息) | — |
| 18 | 大华医院 | 二级乙等 | 徐汇区 | 0.8145 | 1.0 | 0.8145 | 未查询(无号源信息) | — |
| 19 | 利群医院 | 二级甲等 | 普陀区 | 0.8002 | 1.0 | 0.8002 | 未查询(无号源信息) | — |

> Top19 中 19 家号源未核实（上表已逐行标注）；本报告不给出任何无出处的号源结论。

## 8. 模拟挂号（dry-run, 默认开）
- 1. 打开「随申办」App（包名 mobi.w3studio.apps.android.shsmy.phone）
- 2. 首页 →「预约挂号」→「全部医院 (444)」
- 3. 检索并选择医院: 第六人民医院（徐汇区, 主院区）
- 4. 科室页找到「心血管内科」tab 并进入（L2 页自带 14 天日期条）
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
- **PASS** fallback_all_unverified → top_list 前 19 项与静态序同序（§4.5 回落）
- **PASS** max(静态分) 保留: top_list 内最大静态分 == 候选集最大静态分（0.9853）
- **PASS** slot_queries 的每家医院 ∈ final.top_list ∪ final.excluded（显式排除+原因, 不得静默消失）

## 附: SlotQuery 序列（§4.3 契约, 仅生成不发起）
- #1 SH-087 depts=['D04780'] wait≤0天 cap=None static=0.9853
- #2 SH-003 depts=['D04780'] wait≤0天 cap=None static=0.9692
- #3 SH-019 depts=['D04868', 'D04780'] wait≤0天 cap=None static=0.9688
- #4 SH-074 depts=['D04780'] wait≤0天 cap=None static=0.9684
- #5 SH-018 depts=['D04780'] wait≤0天 cap=None static=0.9468
- #6 SH-069 depts=['D04868', 'D04780'] wait≤0天 cap=None static=0.9429
- #7 SH-007 depts=['D04840', 'D04780'] wait≤0天 cap=None static=0.9389
- #8 SH-026 depts=['D04868', 'D04780'] wait≤0天 cap=None static=0.9278
- #9 SH-092 depts=['D04780'] wait≤0天 cap=None static=0.922
- #10 SH-020 depts=['D04868', 'D04780'] wait≤0天 cap=None static=0.9208
- #11 SH-045 depts=['D04840', 'D04868', 'D04780', 'D02683'] wait≤0天 cap=None static=0.9089
- #12 SH-062 depts=['D04868', 'D04780'] wait≤0天 cap=None static=0.8951
