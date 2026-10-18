# 挂号推荐报告（运行期原型）

- 生成: 2026-10-08T14:37:52 | 模式: interactive | 权重推理: agentsvc(model_id=default)

## 1. 位置
- `121.440641, 31.193908`（GCJ-02）— 高德 amap_place 命中「徐家汇源」徐汇区徐家汇源（缓存命中）

## 2. 病情采集（≤3 轮自由文本）
- 第1轮: 孩子4岁半，发烧两天，晚上咳得厉害，想在社区附近尽快看下
- 解析: 患者=child(age=None, 性别=未提供) 代挂=True 就诊=unknown 等待≤7天 费用上限=None 水平预期=balanced

## 3. 权重推理（LLM 直出: 特征权重向量 + 目标科室 + 约束; 含基础数据上下文）
- scenario: **peds**（儿科）
- LLM 后端: agentsvc model_id=default @ http://127.0.0.1:7989; 实测耗时=24266.0ms（首字节见日志）attempts=1 思维链=6314字（仅记长度, 未并入答案）
- 基础数据上下文(随 prompt 下发, 全部只读): 可约科室 canonical 363 条(覆盖≥5 家医院) / 医院目录 438 家(等级/区县); 场景基线权重 5 套(仅参考)
- 4岁半儿童发热两天伴夜间咳嗽，未提示呼吸困难、精神差等红旗急症，按儿童常见急性症状处理。
- 自述强调社区附近尽快，距离权重提高，医院等级和地铁权重降低，儿童适配权重提高。
- 费用无明确上限，仅按社区首诊适度保留费用友好度；科室优先覆盖发热、咳喘、儿科与全科。
- triage_term='发烧' 词表交叉核对: 与 LLM 科室一致（发热, 全科医疗科, 儿科）
- 特征权重向量（LLM 直出绝对权重, 归一行和=1.0; 5 套场景基线仅作 prompt 参考）: {"match": 0.3684, "level": 0.0316, "breadth": 0.0842, "distance": 0.2947, "metro": 0.0211, "bus": 0.0421, "key_specialty": 0.0316, "age_fit": 0.1263}
- 权重分量中文名: 科室命中(match)=0.35 | 医院等级(level)=0.03 | 科室齐全度(breadth)=0.08 | 距离就近(distance)=0.28 | 地铁可达(metro)=0.02 | 公交站点(bus)=0.04 | 特色科室加分(key_specialty)=0.03 | 人群适配(儿科能力)(age_fit)=0.12 | 费用友好度(price)=0.05
- match(科室命中)说明: 该医院命中目标科室的质量 = 命中科室中 层级(tier)×置信度 的最大值（tier1=首选→1.0, tier2=相关→0.70, tier3=弱相关→0.45; 0=未命中; 弱相关科室不再与首选平权, 层级由 LLM 按症状直接指向程度给出）。match 权重越高, 候选排序越优先保证"有对的科室"。
- age_fit(人群适配)=0.12: 儿科病例中"有儿科能力"(可约儿科类科室/儿童机构)的医院=1.0, 无=中性0.5(叠加加分非惩罚); 非儿科病例恒1.0
- price(费用友好度)=0.05: 终排性价比偏好权重, 只对**已核实**号源生效（可核实费档中预算内占比 p_in → u×(1+price·(2p_in-1)), 钳制[0.85,1.15]）; 未核实仅标注档位先验的预算内占比, 不参与排序（铁律: 未核实不定生死）。
- 约束: max_wait_days=1, price_cap_cny=None

## 4. 候选集（stage-1, 复用 ranking/scripts/score_hospitals.py）
- triage: 主词="发烧"（llm）→ 目标科室: 发热(p1/high); 儿科(p2/high); 全科医疗科(p2/high); 发热传染(p2/high); 发热伴皮疹门诊(p2/high)
- 患者层科室过滤: 儿童患者剔除目标科室 4 个: 产科儿保陈雪莲[儿童患者不适用于成人科（canonical 含"产科"）], 产科新生儿保健[儿童患者不适用于成人科（canonical 含"产科"）], 小儿妇科内分泌[儿童患者不适用于成人科（canonical 含"妇科"）], 小儿妇科内分泌特色团队[儿童患者不适用于成人科（canonical 含"妇科"）]（剩余 189 个, 已按过滤后目标同构重算召回/打分/K/抓取计划; 词表与字典只读不改）
  - 剔除: 产科儿保陈雪莲（D01743, p3/high）
  - 剔除: 产科新生儿保健（D01758, p3/high）
  - 剔除: 小儿妇科内分泌（D04284, p3/high）
  - 剔除: 小儿妇科内分泌特色团队（D04285, p3/high）
- 硬过滤: 召回=328 → 通过=172（弃=156: {"超出半径20km": 127, "BLOCKED": 28, "非医保": 1}）, 半径=20.0km
- K=60（肩部规则: {"n": 172, "s0": 0.8982, "floor": 0.6782, "gap_thr": 0.0683, "i_floor": 62, "i_gap": 172}）

| # | 医院 | 等级 | 区 | 静态分 | 首要贡献 |
|---|---|---|---|---|---|
| 1 | 儿科医院 | 三级甲等 | 闵行区 | 0.8982 | 科室命中=1.0（命中: 儿保发育行为门诊,发热,发热传染,发热复） |
| 2 | 儿童医院北京西路院区 | 三级甲等 | 静安区 | 0.8899 | 科室命中=1.0（命中: 发热,呼吸内科,新生儿科 [p1/hig） |
| 3 | 胸科医院 | 三级甲等 | 徐汇区 | 0.8743 | 科室命中=1.0（命中: 发热,呼吸内科,小儿心外科专题门诊 [p） |
| 4 | 华东医院 | 三级甲等 | 静安区 | 0.8643 | 科室命中=1.0（命中: 全科医疗科,发热,呼吸内科,呼吸内科特约） |
| 5 | 静安区中心医院 | 三级乙等 | 静安区 | 0.8613 | 科室命中=1.0（命中: 全科医疗科,发热,发热儿科,呼吸内科CO） |
| 6 | 海军第九〇五医院 | 二级甲等 | 长宁区 | 0.8588 | 科室命中=1.0（命中: 儿科,发热,呼吸内科 [p1/high]） |
| 7 | 瑞金医院卢湾分院 | 二级甲等 | 黄浦区 | 0.8538 | 科室命中=1.0（命中: 儿科,全科医疗科,发热,呼吸内科 [p1） |
| 8 | 儿童医院泸定路院区 | 三级甲等 | 普陀区 | 0.8467 | 科室命中=1.0（命中: 发热,呼吸内科,新生儿科 [p1/hig） |
| 9 | 普陀区人民医院 | 二级甲等 | 普陀区 | 0.835 | 科室命中=1.0（命中: 儿科,发热 [p1/high]） |
| 10 | 闸北中心医院 | 二级甲等 | 静安区 | 0.8213 | 科室命中=1.0（命中: 儿科,全科医疗科,发热,呼吸内科 [p1） |

## 5. 号源（缓存优先: profile → 观察快照 → 档位 → stub; TTL=90min）
| 医院 | 科室 | 来源 | 状态 | 时间戳 | 龄期(min) | 标注 |
|---|---|---|---|---|---|---|
| 儿科医院 | 新生儿外科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidenc |
| 儿童医院北京西路院区 | 新生儿科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidenc |
| 胸科医院 | 小儿心外科专题门诊 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidenc |
| 华东医院 | 呼吸内科特约 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 静安区中心医院 | 呼吸内科COPD | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级乙等|综合医院, n_units=98, confidence=m |
| 海军第九〇五医院 | 呼吸内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|综合医院, n_units=57, confidence=m |
| 瑞金医院卢湾分院 | 小儿哮喘 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|综合医院, n_units=57, confidence=m |
| 儿童医院泸定路院区 | 新生儿科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidenc |
| 普陀区人民医院 | 发热 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|综合医院, n_units=57, confidence=m |
| 闸北中心医院 | 新生儿保健 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|综合医院, n_units=57, confidence=m |
| 上海市中医医院（芷江院区） | 小儿厌食 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科医院, n_units=56, confidence=m |
| 华山医院 | 全科医疗科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 中山医院 | 呼吸内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 第六人民医院 | 小儿运动损伤 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 公利医院 | 呼吸内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 儿童医学中心 | 新生儿/婴幼儿 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidenc |
| 龙华医院 | 小儿敷贴门诊 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科医院, n_units=56, confidence=m |
| 国际和平妇幼保健院 | 新生儿科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidenc |
| 岳阳中西医结合医院 | 呼吸内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科医院, n_units=56, confidence=m |
| 仁济医院西院 | 全科医疗科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 利群医院 | 发热 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|综合医院, n_units=57, confidence=m |
| 新华医院 | 新生儿外科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 长征医院 | 全科医疗科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 妇产科医院黄浦院区 | 新生儿 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidenc |
| 第八人民医院 | 呼吸内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|专科疾病防治院, n_units=57, confidenc |
| 曙光医院西院 | 小儿抽动多动 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科医院, n_units=56, confidence=m |
| 大华医院 | 呼吸内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级乙等|综合医院, n_units=43, confidence=l |
| 瑞金医院总院 | 小儿外科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 建工医院 | 小儿内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|综合医院, n_units=57, confidence=m |
| 第十人民医院 | 呼吸内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 枫林街道社区卫生服务中心 | 全科医疗科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 长宁区妇幼保健院 | 儿科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|专科疾病防治院, n_units=57, confidenc |
| 岳阳中西医结合医院青海路门诊部 | 呼吸内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科医院, n_units=56, confidence=m |
| 第一人民医院 | 新生儿科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 同仁医院 | 新生儿 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级乙等|综合医院, n_units=98, confidence=m |
| 同济医院 | 全科医疗科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 曹家渡街道社区卫生服务中心 | 全科医疗科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 静安寺街道社区卫生服务中心 | 呼吸内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 南京西路街道社区卫生服务中心 | 全科医疗科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 长宁区虹桥街道社区卫生服务中心 | 全科医疗科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 上海市第二人民医院 | 新生儿科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|综合医院, n_units=57, confidence=m |
| 普陀区中心医院 | 呼吸内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级乙等|综合医院, n_units=98, confidence=m |
| 东方医院南院 | 呼吸内科副 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 东方医院 | 呼吸内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 石门二路街道社区卫生服务中心 | 全科医疗科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 复旦大学附属华东医院（浦东门诊部） | 小儿针灸门诊 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 浦南医院 | 呼吸内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|综合医院, n_units=57, confidence=m |
| 市东医院 | 呼吸内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|综合医院, n_units=57, confidence=m |
| 江宁路街道社区卫生服务中心 | 全科医疗科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 邮电医院 | 咳喘门诊 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|综合医院, n_units=57, confidence=m |
| 南京东路街道社区卫生服务中心 | 呼吸内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 长海医院 | 小儿科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 市中西医结合医院 | 小儿推拿 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 第一妇婴保健院东院 | 新生儿黄疸 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidenc |
| 龙华街道社区卫生服务中心 | 全科医疗科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 徐汇区中心医院 | 全科医疗科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|综合医院, n_units=57, confidence=m |
| 天山中医医院 | 儿科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|专科疾病防治院, n_units=57, confidenc |
| 上海市老年医学中心(复旦大学附属中山医院闵行院区) | 呼吸内科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 天山路街道社区卫生服务中心 | 儿科 | stub/合成(未核实) | unknown | — | — | 内置 stub（合成数据, 非实时号源; 用于 profile 缺失时跑通流水线, 恒标注未核实 |
| 闵行区中心医院 | 呼吸内科鼾症门诊 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级乙等|综合医院, n_units=98, confidence=m |

## 6. 推荐（1–3, 两级排序: ① 号源状态 已核实有号>未核实>已核实无号 ② 同状态 终排=静态分×号源效用）
> ⚠ 本批无可核实实时号源（抓取失败/观察过期/仅档位与stub）→ 按 RANKING_SPEC §4.5 回落到 stage-1 静态排序展示, 全部显式标注"号源未核实", 不假装实时。

### 1. 儿科医院（三级甲等 / 闵行区） — 挂号入口: 「小儿骨科专业」tab → 点「新生儿外科」行
- **why**: 儿科医院（三级甲等/闵行区）: 科室命中=命中: 儿保发育行为门诊,发热,发热传染,发热复诊 [p1/（+0.3684, match），距离就近=7.3km（+0.2083, distance）。号源未核实（cache/level_default(档位先验,未核实)）, 仅按静态分排序, 不得当实时事实。
- **evidence**: 静态=0.8982 × 号源效用=1.0 → 终排=0.8982
  - +0.368 match v=1.0（命中: 儿保发育行为门诊,发热,发热传染,发热复诊 [p1/high]）
  - +0.208 distance v=0.7068（7.3km）
  - +0.126 age_fit v=1.0（儿童专属机构）
  - -0.086 distance 失分（7.3km）
  - -0.009 bus 失分（3站）
  - 号源: cache/level_default(档位先验,未核实) | status=unknown | fee=100+ | 出处=slots/profiles/level_defaults.json
    费档分布: 100+(5%), 50(2%), 15(0%), 25(0%); 最低档=50
    档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidence=mid; 非实时, 不可当号源事实）
  - 标注: 号源未核实: 档位先验 level_defaults（stratum=三级甲等|专科疾病防治院, confidence=mid, 非实时, 不当作号源事实）

### 2. 儿童医院北京西路院区（三级甲等 / 静安区） — 挂号入口: 「儿科」tab → 点「新生儿科（北京西路）」行
- **why**: 儿童医院北京西路院区（三级甲等/静安区）: 科室命中=命中: 发热,呼吸内科,新生儿科 [p1/high]（+0.3684, match），距离就近=4.1km（+0.2468, distance）。号源未核实（cache/level_default(档位先验,未核实)）, 仅按静态分排序, 不得当实时事实。
- **evidence**: 静态=0.8899 × 号源效用=1.0 → 终排=0.8899
  - +0.368 match v=1.0（命中: 发热,呼吸内科,新生儿科 [p1/high]）
  - +0.247 distance v=0.8372（4.1km）
  - +0.126 age_fit v=1.0（儿童专属机构）
  - -0.048 distance 失分（4.1km）
  - -0.032 key_specialty 失分（空（覆盖4.4%））
  - 号源: cache/level_default(档位先验,未核实) | status=unknown | fee=100+ | 出处=slots/profiles/level_defaults.json
    费档分布: 100+(5%), 50(2%), 15(0%), 25(0%); 最低档=50
    档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidence=mid; 非实时, 不可当号源事实）
  - 标注: 号源未核实: 档位先验 level_defaults（stratum=三级甲等|专科疾病防治院, confidence=mid, 非实时, 不当作号源事实）

### 3. 胸科医院（三级甲等 / 徐汇区） — 挂号入口: 「小儿胸心外科专业」tab → 点「小儿心外科专题门诊」行
- **why**: 胸科医院（三级甲等/徐汇区）: 科室命中=命中: 发热,呼吸内科,小儿心外科专题门诊 [p1/high（+0.3684, match），距离就近=1.4km（+0.2777, distance）。号源未核实（cache/level_default(档位先验,未核实)）, 仅按静态分排序, 不得当实时事实。
- **evidence**: 静态=0.8743 × 号源效用=1.0 → 终排=0.8743
  - +0.368 match v=1.0（命中: 发热,呼吸内科,小儿心外科专题门诊 [p1/high]）
  - +0.278 distance v=0.9422（1.4km）
  - +0.095 age_fit v=0.75（有可约儿科类科室(非儿童专属机构)）
  - -0.033 breadth 失分（l2=20）
  - -0.032 key_specialty 失分（空（覆盖4.4%））
  - 号源: cache/level_default(档位先验,未核实) | status=unknown | fee=100+ | 出处=slots/profiles/level_defaults.json
    费档分布: 100+(5%), 50(2%), 15(0%), 25(0%); 最低档=50
    档位估算（stratum=三级甲等|专科疾病防治院, n_units=56, confidence=mid; 非实时, 不可当号源事实）
  - 标注: 号源未核实: 档位先验 level_defaults（stratum=三级甲等|专科疾病防治院, confidence=mid, 非实时, 不当作号源事实）

## 7. Top5 人工甄别列表（未核实号源显式标注; 全候选集 K=60 参与排序, 不剔除已查询者）
| 终排# | 医院 | 等级 | 区 | 静态 | 号源效用 | 终排 | 号源来源 | 最早可约 |
|---|---|---|---|---|---|---|---|---|
| 1 | 儿科医院 | 三级甲等 | 闵行区 | 0.8982 | 1.0 | 0.8982 | cache/level_default(档位先验,未核实) | — |
| 2 | 儿童医院北京西路院区 | 三级甲等 | 静安区 | 0.8899 | 1.0 | 0.8899 | cache/level_default(档位先验,未核实) | — |
| 3 | 胸科医院 | 三级甲等 | 徐汇区 | 0.8743 | 1.0 | 0.8743 | cache/level_default(档位先验,未核实) | — |
| 4 | 华东医院 | 三级甲等 | 静安区 | 0.8643 | 1.0 | 0.8643 | cache/level_default(档位先验,未核实) | — |
| 5 | 静安区中心医院 | 三级乙等 | 静安区 | 0.8613 | 1.0 | 0.8613 | cache/level_default(档位先验,未核实) | — |

**已排除（Top5 之外, 显式清单+原因）**: SH-145（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8588, 终排#6（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-179（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8538, 终排#7（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-020（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8467, 终排#8（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-207（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.835, 终排#9（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-171（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8213, 终排#10（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-045（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8206, 终排#11（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-003（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8131, 终排#12（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-019（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8114, 终排#13（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-087（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.797, 终排#14（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-039（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7951, 终排#15（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-018（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7909, 终排#16（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-065（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7908, 终排#17（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-061（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7907, 终排#18（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-062（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7854, 终排#19（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-030（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7811, 终排#20（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-198（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7809, 终排#21（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-002（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7711, 终排#22（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-053（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7674, 终排#23（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-082（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7662, 终排#24（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-172（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7647, 终排#25（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-075（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7602, 终排#26（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-121（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.758, 终排#27（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-036（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7565, 终排#28（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-149（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7436, 终排#29（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-084（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7405, 终排#30（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-262（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7358, 终排#31（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-212（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7355, 终排#32（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-092（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7324, 终排#33（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-095（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7315, 终排#34（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-093（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7295, 终排#35（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-071（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7275, 终排#36（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-259（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7269, 终排#37（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-263（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7261, 终排#38（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-230（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.722, 终排#39（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-252（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7206, 终排#40（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-119（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.72, 终排#41（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-083（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.717, 终排#42（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-001（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7166, 终排#43（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-027（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7166, 终排#44（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-271（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7156, 终排#45（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-058（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7139, 终排#46（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-103（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7112, 终排#47（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-157（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7108, 终排#48（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-334（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7064, 终排#49（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-189（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7043, 终排#50（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-248（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.702, 终排#51（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-080（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7012, 终排#52（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-064（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.6994, 终排#53（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-024（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.6949, 终排#54（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-345（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.6944, 终排#55（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-099（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.6898, 终排#56（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-147（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.6859, 终排#57（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-044（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.6844, 终排#58（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-444（号源未核实(stub/合成(未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.681, 终排#59（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-072（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.6803, 终排#60（落在 Top5 展示范围之外; 仍保留在候选集完整排序中, 未剔除））

> Top5 中 5 家号源未核实（上表已逐行标注）；本报告不给出任何无出处的号源结论。

## 8. 模拟挂号（dry-run, 默认开）
- 1. 打开「随申办」App（包名 mobi.w3studio.apps.android.shsmy.phone）
- 2. 首页 →「预约挂号」→「全部医院 (444)」
- 3. 检索并选择医院: 儿科医院（闵行区, 主院区）
- 4. L1 选科室页找到「小儿骨科专业」tab, 点「新生儿外科」行进入科室页（注意: 同 tab 下可能有多个含相似字样的科室, 以该行为准; L2 页自带日期条）
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
- **PASS** 已解析号源的 12 家医院不得静默消失: ∈ final.top_list ∪ final.excluded(带原因, 含号源状态)
- **PASS** fallback_all_unverified → top_list 前 5 项与静态序同序（§4.5 回落）
- **PASS** max(静态分)医院可见: SH-074(0.8982) 在 top_list 或 显式排除(带原因); no_slot 降级属合理结果但必须写明
- **PASS** slot_queries 的每家医院 ∈ final.top_list ∪ final.excluded（显式排除+原因, 不得静默消失）

## 附: SlotQuery 序列（§4.3 契约, 仅生成不发起）
- #1 SH-074 depts=['D03121', 'D03122', 'D03128', 'D03130', 'D05316'] wait≤1天 cap=None static=0.8982
- #2 SH-026 depts=['D03121', 'D05325', 'D03443'] wait≤1天 cap=None static=0.8899
- #3 SH-069 depts=['D03121', 'D03443', 'D04287'] wait≤1天 cap=None static=0.8743
- #4 SH-007 depts=['D03121', 'D02450', 'D03443', 'D03460'] wait≤1天 cap=None static=0.8643
- #5 SH-077 depts=['D03121', 'D02450', 'D03124', 'D03446'] wait≤1天 cap=None static=0.8613
- #6 SH-145 depts=['D03121', 'D02013', 'D03443'] wait≤1天 cap=None static=0.8588
- #7 SH-179 depts=['D03121', 'D02013', 'D02450', 'D03443', 'D04279'] wait≤1天 cap=None static=0.8538
- #8 SH-020 depts=['D03121', 'D05325', 'D03443'] wait≤1天 cap=None static=0.8467
- #9 SH-207 depts=['D03121', 'D02013'] wait≤1天 cap=None static=0.835
- #10 SH-171 depts=['D03121', 'D02013', 'D02450', 'D03443', 'D05310'] wait≤1天 cap=None static=0.8213
- #11 SH-045 depts=['D03121', 'D02013', 'D02036', 'D04265', 'D04269'] wait≤1天 cap=None static=0.8206
- #12 SH-003 depts=['D02450', 'D02430', 'D02432'] wait≤1天 cap=None static=0.8131
