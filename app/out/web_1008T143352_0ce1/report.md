# 挂号推荐报告（运行期原型）

- 生成: 2026-10-08T14:34:30 | 模式: web | 权重推理: agentsvc(model_id=default)

## 1. 位置
- `121.440641, 31.193908`（GCJ-02）— 高德 amap_place 命中「徐家汇源」徐汇区徐家汇源

## 2. 病情采集（≤3 轮自由文本）
- 第1轮: 牙疼想早点看好
- 解析: 患者=adult(age=48, 性别=男) 代挂=False 就诊=unknown 等待≤7天 费用上限=10 水平预期=balanced

## 3. 权重推理（LLM 直出: 特征权重向量 + 目标科室 + 约束; 含基础数据上下文）
- scenario: **community_first**（社区首诊/轻症）
- LLM 后端: agentsvc model_id=default @ http://127.0.0.1:7989; 实测耗时=36671.0ms（首字节见日志）attempts=1 思维链=2734字（仅记长度, 未并入答案）
- 基础数据上下文(随 prompt 下发, 全部只读): 可约科室 canonical 363 条(覆盖≥5 家医院) / 医院目录 438 家(等级/区县); 场景基线权重 5 套(仅参考)
- 牙疼非红旗急症，但自述“早点看好”提示希望尽快就诊，因此时效收紧到3天，距离与可约性权重较高。
- 费用上限10元且水平预期balanced，优先普通门诊/社区或一级机构，提高price权重并降低level与metro权重。
- 主诉直接指向口腔科，细分牙体牙髓科、口腔外科作为相关科室，牙周病科和口腔综合科作为弱相关后备。
- triage_term='牙疼' 词表交叉核对: 与 LLM 科室一致（口腔科, 口腔外科）
- 特征权重向量（LLM 直出绝对权重, 归一行和=1.0; 5 套场景基线仅作 prompt 参考）: {"match": 0.425, "level": 0.075, "breadth": 0.0875, "distance": 0.3, "metro": 0.025, "bus": 0.0625, "key_specialty": 0.025}
- 权重分量中文名: 科室命中(match)=0.34 | 医院等级(level)=0.06 | 科室齐全度(breadth)=0.07 | 距离就近(distance)=0.24 | 地铁可达(metro)=0.02 | 公交站点(bus)=0.05 | 特色科室加分(key_specialty)=0.02 | 费用友好度(price)=0.2
- match(科室命中)说明: 该医院命中目标科室的质量 = 命中科室中 层级(tier)×置信度 的最大值（tier1=首选→1.0, tier2=相关→0.70, tier3=弱相关→0.45; 0=未命中; 弱相关科室不再与首选平权, 层级由 LLM 按症状直接指向程度给出）。match 权重越高, 候选排序越优先保证"有对的科室"。
- price(费用友好度)=0.2: 终排性价比偏好权重, 只对**已核实**号源生效（可核实费档中预算内占比 p_in → u×(1+price·(2p_in-1)), 钳制[0.85,1.15]）; 未核实仅标注档位先验的预算内占比, 不参与排序（铁律: 未核实不定生死）。
- 约束: max_wait_days=3, price_cap_cny=10

## 4. 候选集（stage-1, 复用 ranking/scripts/score_hospitals.py）
- triage: 主词="牙疼"（llm）→ 目标科室: 口腔(p1/high); 口腔外科(p1/high); 口腔科(p1/high); 一站式特色门诊口腔颌面外科(p2/high); 世博口腔修复科(p2/high)
- 患者层科室过滤: 成人/老年患者剔除目标科室 6 个: 口腔儿童牙病[成人/老年患者不适用于儿科类科室（canonical 含"儿童"）], 口腔儿童青少年矫正/口腔罕见病筛查[成人/老年患者不适用于儿科类科室（canonical 含"儿童"）], 口腔罕见病/特殊儿童诊疗[成人/老年患者不适用于儿科类科室（canonical 含"儿童"）], 口腔青少年矫正/正畸[成人/老年患者不适用于儿科类科室（canonical 含"青少年"）], 口腔青少年矫正/正畸团队[成人/老年患者不适用于儿科类科室（canonical 含"青少年"）], 口腔预防儿童拔牙[成人/老年患者不适用于儿科类科室（canonical 含"儿童"）]（剩余 312 个, 已按过滤后目标同构重算召回/打分/K/抓取计划; 词表与字典只读不改）; 另剔除患者不匹配的专科机构医院 24 家: 上海市妇幼保健中心, 儿童医学中心, 儿童医院泸定路院区, 第一妇婴保健院东院...
  - 剔除: 口腔儿童牙病（D03189, p2/high）
  - 剔除: 口腔儿童青少年矫正/口腔罕见病筛查（D03190, p2/high）
  - 剔除: 口腔罕见病/特殊儿童诊疗（D03286, p2/high）
  - 剔除: 口腔青少年矫正/正畸（D03299, p2/high）
  - 剔除: 口腔青少年矫正/正畸团队（D03300, p2/high）
  - 剔除: 口腔预防儿童拔牙（D03302, p2/high）
- 硬过滤: 召回=258 → 通过=97（弃=161: {"超出半径12km": 128, "BLOCKED": 24, "PATIENT_BAND_HOSPITAL: 成人/老年患者不适用于儿科机构": 5, "PATIENT_BAND_HOSPITAL: 成年男性患者不适用于妇儿机构": 3, "非医保": 1}）, 半径=12.0km
- K=60（肩部规则: {"n": 97, "s0": 0.9487, "floor": 0.7287, "gap_thr": 0.0528, "i_floor": 80, "i_gap": 97}）

| # | 医院 | 等级 | 区 | 静态分 | 首要贡献 |
|---|---|---|---|---|---|
| 1 | 华山医院 | 三级甲等 | 静安区 | 0.9487 | 科室命中=1.0（命中: 北院口腔颌面外科,口腔科,口腔科修复,口） |
| 2 | 中山医院 | 三级甲等 | 徐汇区 | 0.9465 | 科室命中=1.0（命中: 口腔科,口腔颌面外科 [p1/high]） |
| 3 | 华东医院 | 三级甲等 | 静安区 | 0.9318 | 科室命中=1.0（命中: 口腔正畸,口腔种植,口腔科,痘坑及瘢痕修） |
| 4 | 龙华医院 | 三级甲等 | 徐汇区 | 0.9301 | 科室命中=1.0（命中: 口腔科,口腔科正畸 [p1/high]） |
| 5 | 第六人民医院 | 三级甲等 | 徐汇区 | 0.9247 | 科室命中=1.0（命中: 口腔正畸,口腔科,口腔科口腔种植,口腔科） |
| 6 | 仁济医院西院 | 三级甲等 | 黄浦区 | 0.9209 | 科室命中=1.0（命中: 口腔科,口腔科拔牙,口腔科正畸,口腔科洗） |
| 7 | 曙光医院西院 | 三级甲等 | 黄浦区 | 0.9065 | 科室命中=1.0（命中: 口腔科,美容门诊 [p1/high]） |
| 8 | 长征医院 | 三级甲等 | 黄浦区 | 0.9039 | 科室命中=1.0（命中: 口腔科 [p1/high]） |
| 9 | 瑞金医院总院 | 三级甲等 | 黄浦区 | 0.8914 | 科室命中=1.0（命中: 创面修复科,口腔科 [p1/high]） |
| 10 | 第八人民医院 | 二级甲等 | 徐汇区 | 0.8863 | 科室命中=1.0（命中: 口腔正畸,口腔牙周,口腔科,激光美容 [） |

## 5. 号源（缓存优先: profile → 观察快照 → 档位 → stub; TTL=90min）
| 医院 | 科室 | 来源 | 状态 | 时间戳 | 龄期(min) | 标注 |
|---|---|---|---|---|---|---|
| 华山医院 | 口腔科粘膜病 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 中山医院 | 口腔颌面外科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 华东医院 | 皮肤科医学美容 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 龙华医院 | 口腔科正畸 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科医院, n_units=56, confidence=m |
| 第六人民医院 | 口腔科颞下颌关节紊乱病 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 仁济医院西院 | 口腔科种植 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 曙光医院西院 | 美容门诊 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科医院, n_units=56, confidence=m |
| 长征医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 瑞金医院总院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 第八人民医院 | 激光美容 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|专科疾病防治院, n_units=57, confidenc |
| 静安区中心医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级乙等|综合医院, n_units=98, confidence=m |
| 第十人民医院 | 皮肤健康及医学美容中心 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 同济医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 大华医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级乙等|综合医院, n_units=43, confidence=l |
| 第九人民医院(高科园区) | 口腔种植科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 海军特色医学中心 | 口腔科 | stub/合成(未核实) | unknown | — | — | 内置 stub（合成数据, 非实时号源; 用于 profile 缺失时跑通流水线, 恒标注未核实 |
| 瑞金医院卢湾分院 | 口腔科急诊 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|综合医院, n_units=57, confidence=m |
| 海军第九〇五医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|综合医院, n_units=57, confidence=m |
| 第一人民医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 同仁医院 | 口腔正畸 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级乙等|综合医院, n_units=98, confidence=m |
| 东方医院南院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 东方医院 | 口腔科正 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 瑞金康复医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级乙等|综合医院, n_units=43, confidence=l |
| 邮电医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|综合医院, n_units=57, confidence=m |
| 上海市中医医院（芷江院区） | 口腔综合 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科医院, n_units=56, confidence=m |
| 普陀区人民医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|综合医院, n_units=57, confidence=m |
| 普陀区中心医院 | 口腔科正畸 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级乙等|综合医院, n_units=98, confidence=m |
| 市中西医结合医院 | 种植牙停 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 曹家渡街道社区卫生服务中心 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 静安寺街道社区卫生服务中心 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 龙华街道社区卫生服务中心 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 上海市第二人民医院 | 口腔颌面外科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|综合医院, n_units=57, confidence=m |
| 浦南医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|综合医院, n_units=57, confidence=m |
| 南京西路街道社区卫生服务中心 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 闸北中心医院 | 疤痕与创面修复 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|综合医院, n_units=57, confidence=m |
| 公利医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 天平街道社区卫生服务中心 | 口腔 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 石门二路街道社区卫生服务中心 | 口腔科复诊 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 豫园街道社区卫生服务中心 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 上海市老年医学中心(复旦大学附属中山医院闵行院区) | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 岳阳中西医结合医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|专科医院, n_units=56, confidence=m |
| 民航上海医院-瑞金医院古北分院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级乙等|综合医院, n_units=43, confidence=l |
| 普陀区眼病牙病防治所 | 口腔种植科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级|专科疾病防治院, n_units=43, confidence= |
| 闵行区中心医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级乙等|综合医院, n_units=98, confidence=m |
| 江宁路街道社区卫生服务中心 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 黄浦区肿瘤防治院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|专科疾病防治院, n_units=57, confidenc |
| 南京东路街道社区卫生服务中心 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 第四康复医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|专科疾病防治院, n_units=57, confidenc |
| 白玉社区卫生服务中心 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 复旦大学附属华东医院（闵行门诊部） | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 新华医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=三级甲等|综合医院, n_units=383, confidence= |
| 普陀区中医医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|专科疾病防治院, n_units=57, confidenc |
| 天山中医医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|专科疾病防治院, n_units=57, confidenc |
| 利群医院 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=二级甲等|综合医院, n_units=57, confidence=m |
| 芷江西路街道社区卫生服务中心 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 江苏街道社区卫生服务中心 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 宝山路街道社区卫生服务中心 | 口腔 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 大宁路街道社区卫生服务中心 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 彭浦镇社区卫生服务中心 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |
| 小东门街道社区卫生服务中心 | 口腔科 | cache/level_default(档位先验,未核实) | unknown | — | — | 档位估算（stratum=一级甲等|综合医院, n_units=65, confidence=h |

## 6. 推荐（1–3, 两级排序: ① 号源状态 已核实有号>未核实>已核实无号 ② 同状态 终排=静态分×号源效用）
> ⚠ 本批无可核实实时号源（抓取失败/观察过期/仅档位与stub）→ 按 RANKING_SPEC §4.5 回落到 stage-1 静态排序展示, 全部显式标注"号源未核实", 不假装实时。

### 1. 海军特色医学中心（未定级 / 长宁区） — 挂号入口: 「口腔科」tab → 点「口腔科门诊」行
- **why**: 海军特色医学中心（未定级/长宁区）: 科室命中=命中: 口腔科 [p1/high]（+0.425, match），距离就近=1.4km（+0.2837, distance）。号源未核实（stub/合成(未核实)）, 仅按静态分排序, 不得当实时事实。
- **evidence**: 静态=0.8703 × 号源效用=1.0 → 终排=0.8703
  - +0.425 match v=1.0（命中: 口腔科 [p1/high]）
  - +0.284 distance v=0.9457（1.4km）
  - +0.062 breadth v=0.7106（l2=33）
  - -0.047 level 失分（未定级）
  - -0.025 breadth 失分（l2=33）
  - 号源: stub/合成(未核实) | status=unknown | fee=unknown | 出处=内置合成中性值
    内置 stub（合成数据, 非实时号源; 用于 profile 缺失时跑通流水线, 恒标注未核实）
  - 标注: 号源未核实: stub 合成兜底（非实时数据, 不当作号源事实）

## 7. Top20 人工甄别列表（未核实号源显式标注; 全候选集 K=60 参与排序, 不剔除已查询者）
| 终排# | 医院 | 等级 | 区 | 静态 | 号源效用 | 终排 | 号源来源 | 最早可约 |
|---|---|---|---|---|---|---|---|---|
| 1 | 华山医院 | 三级甲等 | 静安区 | 0.9487 | 1.0 | 0.9487 | cache/level_default(档位先验,未核实) | — |
| 2 | 中山医院 | 三级甲等 | 徐汇区 | 0.9465 | 1.0 | 0.9465 | cache/level_default(档位先验,未核实) | — |
| 3 | 华东医院 | 三级甲等 | 静安区 | 0.9318 | 1.0 | 0.9318 | cache/level_default(档位先验,未核实) | — |
| 4 | 龙华医院 | 三级甲等 | 徐汇区 | 0.9301 | 1.0 | 0.9301 | cache/level_default(档位先验,未核实) | — |
| 5 | 第六人民医院 | 三级甲等 | 徐汇区 | 0.9247 | 1.0 | 0.9247 | cache/level_default(档位先验,未核实) | — |
| 6 | 仁济医院西院 | 三级甲等 | 黄浦区 | 0.9209 | 1.0 | 0.9209 | cache/level_default(档位先验,未核实) | — |
| 7 | 曙光医院西院 | 三级甲等 | 黄浦区 | 0.9065 | 1.0 | 0.9065 | cache/level_default(档位先验,未核实) | — |
| 8 | 长征医院 | 三级甲等 | 黄浦区 | 0.9039 | 1.0 | 0.9039 | cache/level_default(档位先验,未核实) | — |
| 9 | 瑞金医院总院 | 三级甲等 | 黄浦区 | 0.8914 | 1.0 | 0.8914 | cache/level_default(档位先验,未核实) | — |
| 10 | 第八人民医院 | 二级甲等 | 徐汇区 | 0.8863 | 1.0 | 0.8863 | cache/level_default(档位先验,未核实) | — |
| 11 | 静安区中心医院 | 三级乙等 | 静安区 | 0.8826 | 1.0 | 0.8826 | cache/level_default(档位先验,未核实) | — |
| 12 | 第十人民医院 | 三级甲等 | 静安区 | 0.8755 | 1.0 | 0.8755 | cache/level_default(档位先验,未核实) | — |
| 13 | 同济医院 | 三级甲等 | 普陀区 | 0.8739 | 1.0 | 0.8739 | cache/level_default(档位先验,未核实) | — |
| 14 | 大华医院 | 二级乙等 | 徐汇区 | 0.8739 | 1.0 | 0.8739 | cache/level_default(档位先验,未核实) | — |
| 15 | 第九人民医院(高科园区) | 三级甲等 | 黄浦区 | 0.8736 | 1.0 | 0.8736 | cache/level_default(档位先验,未核实) | — |
| 16 | 海军特色医学中心 | 未定级 | 长宁区 | 0.8703 | 1.0 | 0.8703 | stub/合成(未核实) | — |
| 17 | 瑞金医院卢湾分院 | 二级甲等 | 黄浦区 | 0.866 | 1.0 | 0.866 | cache/level_default(档位先验,未核实) | — |
| 18 | 海军第九〇五医院 | 二级甲等 | 长宁区 | 0.8647 | 1.0 | 0.8647 | cache/level_default(档位先验,未核实) | — |
| 19 | 第一人民医院 | 三级甲等 | 虹口区 | 0.8627 | 1.0 | 0.8627 | cache/level_default(档位先验,未核实) | — |
| 20 | 同仁医院 | 三级乙等 | 长宁区 | 0.8609 | 1.0 | 0.8609 | cache/level_default(档位先验,未核实) | — |

**已排除（Top20 之外, 显式清单+原因）**: SH-001（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8606, 终排#21（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-027（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8606, 终排#22（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-167（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8567, 终排#23（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-189（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.852, 终排#24（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-045（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8485, 终排#25（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-207（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.847, 终排#26（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-083（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8427, 终排#27（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-064（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8409, 终排#28（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-259（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8393, 终排#29（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-263（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.838, 终排#30（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-345（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8372, 终排#31（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-119（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8367, 终排#32（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-103（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8355, 终排#33（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-230（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8337, 终排#34（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-171（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8303, 终排#35（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-039（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8294, 终排#36（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-277（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8237, 终排#37（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-271（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8236, 终排#38（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-112（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8229, 终排#39（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-044（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.821, 终排#40（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-062（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8195, 终排#41（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-144（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8191, 终排#42（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-176（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8153, 终排#43（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-072（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8138, 终排#44（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-334（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8133, 终排#45（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-126（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8106, 终排#46（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-248（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8086, 终排#47（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-202（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.8077, 终排#48（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-222（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7965, 终排#49（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-049（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7936, 终排#50（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-002（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7934, 终排#51（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-182（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.791, 终排#52（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-147（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7897, 终排#53（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-198（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7892, 终排#54（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-367（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7877, 终排#55（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-234（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7872, 终排#56（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-380（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7807, 终排#57（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-274（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7748, 终排#58（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-387（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7734, 终排#59（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））; SH-140（号源未核实(cache/level_default(档位先验,未核实)) → 号源状态优先级排在已核实有号医院之后 → 终分=0.7664, 终排#60（落在 Top20 展示范围之外; 仍保留在候选集完整排序中, 未剔除））

> Top20 中 20 家号源未核实（上表已逐行标注）；本报告不给出任何无出处的号源结论。

## 8. 模拟挂号（dry-run, 默认开）
- 1. 打开「随申办」App（包名 mobi.w3studio.apps.android.shsmy.phone）
- 2. 首页 →「预约挂号」→「全部医院 (444)」
- 3. 检索并选择医院: 海军特色医学中心（长宁区, 主院区）
- 4. L1 选科室页找到「口腔科」tab, 点「口腔科门诊」行进入科室页（注意: 同 tab 下可能有多个含相似字样的科室, 以该行为准; L2 页自带日期条）
- 5. 选择日期: 待定（最早号源未核实, 到院/到页后再选）（最早可约; 日期条若显示无号则换日期/医生）
- 6. 选择医生与时段 →「去预约」（医生列表来自 SlotResult.doctors, is_target 标记目标医生）
- 7. 选择就诊人「*华」（48岁 · 男 · 医保卡=有）+ 短信验证码（用户手动完成, 脚本不触碰凭据）
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
- **PASS** fallback_all_unverified → top_list 前 20 项与静态序同序（§4.5 回落）
- **PASS** max(静态分)医院可见: SH-003(0.9487) 在 top_list 或 显式排除(带原因); no_slot 降级属合理结果但必须写明
- **PASS** slot_queries 的每家医院 ∈ final.top_list ∪ final.excluded（显式排除+原因, 不得静默消失）

## 附: SlotQuery 序列（§4.3 契约, 仅生成不发起）
- #1 SH-003 depts=['D03244', 'D02880', 'D03248', 'D03268', 'D03272'] wait≤3天 cap=10 static=0.9487
- #2 SH-019 depts=['D03244', 'D03308'] wait≤3天 cap=10 static=0.9465
- #3 SH-007 depts=['D03244', 'D03209', 'D03238', 'D06565', 'D06716'] wait≤3天 cap=10 static=0.9318
- #4 SH-065 depts=['D03244', 'D03268'] wait≤3天 cap=10 static=0.9301
- #5 SH-087 depts=['D03244', 'D03209', 'D03255', 'D03270', 'D03275'] wait≤3天 cap=10 static=0.9247
- #6 SH-030 depts=['D03244', 'D03263', 'D03268', 'D03269', 'D03271'] wait≤3天 cap=10 static=0.9209
- #7 SH-075 depts=['D03244', 'D07332'] wait≤3天 cap=10 static=0.9065
- #8 SH-053 depts=['D03244'] wait≤3天 cap=10 static=0.9039
- #9 SH-036 depts=['D03244', 'D02789'] wait≤3天 cap=10 static=0.8914
- #10 SH-172 depts=['D03244', 'D03209', 'D03226', 'D06021'] wait≤3天 cap=10 static=0.8863
- #11 SH-077 depts=['D03244'] wait≤3天 cap=10 static=0.8826
- #12 SH-084 depts=['D03178', 'D06669'] wait≤3天 cap=10 static=0.8755
