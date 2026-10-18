# WEB 实测诊断报告（job web_1005T161855_e0ef, 2026-10-05）

> 对应实测：web 表单「右下颌骨增生，或者是咬肌肥大，脸型不对称 / 48 岁男 / 时效 14 天 / 费用上限 50 / 水平 balanced / 地铁偏好 / live 勾选」，live 档位未选（= 全部子 tab 全模式）。
> 本报告为 5 个问题的取证诊断 + 已实施修复说明；证据文件路径均可复核。

## 0. 总览

| # | 问题 | 结论 | 修复 |
|---|---|---|---|
| 1 | 48 岁成人候选里出现儿童医院北京西路院区 | 真缺陷：患者层过滤只在科室级，没管医院级机构属性 | 已加医院级过滤（成人/老年剔儿科机构；成年男性另剔妇儿机构），剔除清单落盘+报告+控制台 |
| 2 | top3「最高可约」全是 unknown | unknown = 号源未核实（无实时数据），不是「不限日期」；本批 live 被 60 分钟超时杀掉、v2 收尾才落盘 → 全部成果丢失 → 全落档位先验（无日期） | web 日志落盘 log.txt；live 预算预警 + 超时进度取证；web「unknown」改显「未核实」 |
| 3 | 医院栏只有归一化科室名、无 tab，挂号不便不唯一 | 真缺陷：final 条目缺 (tab, raw 行名)；routing 数据其实一直存在（manifest l1） | final 富化 dept_tab/dept_raw；报告/web/模拟挂号步骤全部改为「tab → 点 raw 行」 |
| 4 | 权重向量无中文名，match 含义不明 | 输出缺中文标注（内部有 FEATURE_ZH 未用于此处）；match = 科室命中质量 = 命中目标科室中 优先级×置信度 的最大值 | 报告/web 双端加中文名 + match 专门说明行；LLM_CONTRACT 增 11 行中文定义表 |
| 5 | 费用上限 50 但费档全是 50，费用不在权重里 | 三重原因：① 10 静态特征无 price 维；② 上限只做「超预算违规检查」不做排序因子；③ fee=50 是档位先验退化产物（stratum 99.7% 未观察, 唯一非零=50→众数=50），且旧 SlotInfo 只暴露众数档、隐藏更便宜档 | price 成为 LLM 权重第 11 分量；终排性价比因子（只对已核实号源）；SlotInfo 全层增 fee_tiers 分布 + fee_min_tier，超预算判定改最低档 |

## 1. 「日志」到底看到了什么

- 你引用的 `2026-10-05T16:18:55 [done] [live] 右下颌骨增生…` 不是日志行，是**左侧历史列表条目**（`j.ts + [status] + [live] + 症状`，index.html 历史渲染逻辑）。16:18:55 = job 提交时刻；job 实际跑完约在 17:19（report/result 文件 mtime），**全程约 60 分钟**。
- 当时管线 stdout 只经 SSE 转发到浏览器 + web 进程控制台，**不落盘**：job 目录只有 spec.json/report.md/result.json；`web_server.log` 只有 HTTP 访问行（stderr）。浏览器一关，逐行日志就没了 —— 「日志打印不充分」属实（是不落盘）。
- **修复**：`app/web.py::_Tee` 现在把管线 stdout 全量逐行追加到 `app/out/<job>/log.txt`（与 SSE 同源）。

## 2. Q1 儿童医院为何入选

**证据**（job 目录 + report.md）：
- report §5 表第 12 行 / Top20 #12 / SlotQuery 附 #12：`SH-026 儿童医院北京西路院区 depts=['D03244']（口腔科） static=0.8826` —— 在预算 12 家内，还进了 live 批量。
- `app/out/live_conds_20261005T161917.json`：`{"hospital":"SH-026","tab":"儿科","dept":"口腔…"}`。
- LLM 目标科室 = 医疗美容科 / 口腔外科 / 口腔科 / 口腔颌面外科 / 整形外科 —— 全是**通用科室 canonical**。

**根因**：患者层过滤（`candidates.py`）只作用于**科室级**（canonical 名含儿科标识 → 剔除）。`口腔科 D03244` 不是儿科 canonical，儿童医院又开设该科室 → 整院被召回。48 岁成人因此匹配上「儿童医院的口腔科」。同批候选里 #13 儿童医学中心同理。

**修复**：`candidates.py` 新增医院级过滤 `_hospital_exclusions`：
- 成人/老年 → 剔除名称含「儿童医院/儿童医学中心/儿科医院/复旦儿科」的机构；
- 成年男性 → 另剔「妇婴/妇儿/妇幼保健/妇产」机构（如国际和平妇婴保健院，本批 #14）。
剔除清单落盘 `stage1.patient_filter.dropped_hospitals` + 报告 §4 note + 控制台「医院级剔除」行。儿童患者不受影响（妇儿/儿科机构正是儿童的目标）。

## 3. Q2 「unknown」到底什么意思

**定义**：unknown = 该 (院,科) **没有任何可核实的实时号源信息**（status=unknown, verified=false, earliest_date=None）。它**不是**「不限日期」——「不限日期」是 v2 的**采集策略**（date=null 时走医生主页「不限日期」视图收 30 天日期条），属于采集内部细节，与展示值 unknown 无关。

**本批时间线**（证据：文件 mtime + 内容）：

| 时刻 | 事件 |
|---|---|
| 16:18:55 | job 提交（web_1005T161855_e0ef）；LLM 21.2s |
| 16:19:17 | 条件文件生成：17 个条件组（12 院, 部分 2 科），无 slot_type = **全部子 tab（全模式）** |
| 16:19:22 | v2 启动 → `condition_runs/20261005T161922_all.jsonl`（**0 字节**） |
| 16:19→17:19 | v2 逐组推进：nav_state.json 停在 `SH-095 第一人民 / 口腔科 / 专家门诊, ts=17:18:39`（第 9/17 组） |
| ~17:19 | `subprocess.run(timeout=LIVE_TIMEOUT_S=3600)` 超时杀进程；**`run_20261005T161922.json` 摘要从未生成** |
| 17:19 | slots.py 找不到新 `run_*.json` → 「无 live 结果」→ 12 个 (院,科) 全部落 L4 档位先验 → status=unknown |

**两个结构性原因**：
1. **预算失配**：全模式实测 ≈6.6 分钟/组（59.5 分钟走完 9/17 组），17 组 ≈ 112 分钟 > 60 分钟超时；
2. **v2 收尾才一次性落盘**：超时被杀 → 前 8 组已收集的六字段记录全部丢失（jsonl 0 字节），部分进度只剩 nav_state。

**修复**：
- `prepare_live` 前置估算（全模式 540s / expert 240s / normal 420s / specialty 480s × 组数），超 `LIVE_TIMEOUT_S` 时**醒目预警**（含「v2 收尾才落盘, 超时=本批全失」与「建议 expert 档/减少医院数」）；
- 超时分支读 `nav_state.json` 打印「超时时 v2 已进行到 哪院/哪 tab/哪科室/哪子tab」取证行；
- web 表格 earliest 列：`unknown` → 「**未核实**（无可核实实时号源数据，见号源来源）」。

**下次 live 建议**：表单「live 档位」选 `expert`（约 4 分钟/组）；17 组仍 ≈68 分钟偏紧，可接受「只完成前 N 组」或减少目标科室。

## 4. Q3 医院栏只有归一化科室名

**事实**：`final.top_list` 条目只有 `dept`（归一化 canonical，如「整形外科」），无 tab、无院页面 raw 行名；web 表格渲染成「医院名 + canonical」；旧 `book.py` dry-run 步骤 4 甚至写「科室页找到「整形外科」**tab** 并进入」——把科室行说成 tab，正是你说的不便+不唯一（SH-084 的「外科」tab 下就有多个含「整形外科」字样的行；manifest l1 给出的 raw 行名其实是「**整形外科门诊**」）。

**修复**（routing 数据一直都在，live 适配器就用它）：
- `main.py`：finalize 后对 top_list 每行经 `resolver._dept_route(hid, dept_id)`（manifest l1 索引）回填 `dept_tab` / `dept_raw`，落盘 result.json；
- report §6 标题：`### 1. 第八人民医院（二级甲等/徐汇区） — 挂号入口: 「内科」tab → 点「全科」行`；
- web 表格医院栏：`第八人民医院` + `「内科」tab / 「全科」行`；
- `book.py` 步骤 4：`L1 选科室页找到「内科」tab, 点「全科」行进入科室页（同 tab 下可能有多个含相似字样的科室, 以该行为准）`。

## 5. Q4 权重向量：中文名与 match 的含义

**match 的实际机制**（`ranking/scripts/score_hospitals.py::score_hospital`，只读引用）：
```
match(该医院) = max{ PRIORITY_W[t.priority] × CONF_W[t.confidence] }
                遍历: LLM 选的目标科室中, kind∈{clinic,specialty_clinic,screening}
                      且 该医院拥有(dept_index hospitals 集合) 的每一个 t
```
- p1/high → v=1.0；优先级/置信度降档 → v<1.0；0 = 未命中任何目标科室。
- 本 job：第十人民医院 命中「整形外科 [p1/high]」→ v=1.0，贡献 = 权重 0.34 × 1.0 = **+0.34**（该医院静态分 0.9502 的第一大来源，见 report §6 evidence 行）。
- 语义：match 权重 0.34 = LLM 判断本主诉下「有没有对的科室」最重要（占 34%）。它是**门槛性特征**：没有对科室，医院再近再高级也进不了召回（召回本身就由目标科室驱动）。

**修复**：
- report §3 新增「权重分量中文名: 科室命中(match)=0.34 | 医院等级(level)=0.13 | …」+ match 专门说明行 + price 说明行；
- web 权重条：`科室命中 match 0.34 ▇▇▇`（11 键中文映射内置）；
- `docs/LLM_CONTRACT.md` §5 增 11 行中文定义表（含 price 的作用域说明）。

## 6. Q5 费用：为什么全是 50、费用为什么没起作用

**三个事实**：
1. **10 个静态特征没有价格维** → stage-1 医院排序完全不考虑费用。`price_cap_cny=50` 此前只做一件事：`FEE_VAL[fee_tier(众数)] > cap` 时记 violation 并把该院移出推荐（elig 过滤）。本批众数档=50，50>50 为假 → 既不罚也不奖，「性价比」不存在。
2. **fee=50 不是真实费用，是档位先验的退化产物**：`level_defaults.json` 的 `三级甲等|综合医院` stratum（n_units=383, 每单位仅 1 次观察）`price_tiers = {15:0, 25:0, 50:0.003, 100+:0, unknown:0.997}` —— 99.7% 未观察，剔除 unknown 后唯一非零是 50 → 众数=50。三家「费档 50」都是这个先验值。
3. **「普通门诊 <50」没反映出来的直接原因**：live 数据整体丢失（§3），没有医生级真实费用；且旧 SlotInfo 只暴露**一个众数档**，即使 live 成功，便宜的 15/25 档也会被众数（如 50/100+）掩盖。

**已实施修复**：
1. **price 成为 LLM 权重向量第 11 分量**（费用友好度）：prompt 特征定义/场景基线（community 0.06 / chronic 0.05 / complex 0.03 / red_flag 0.02 / peds 0.05）/校验器/报告/web 全链路；stage-1 前 10 维表不动（ranking/ 只读约束不破），price 不进静态打分。实测本 job 之后 LLM 直出 price=0.08~0.1。
2. **终排性价比因子**（`recommend.slot_utility`，保持铁律）：
   - 已核实 ok：`u ×= (1 + price·(2·p_in − 1))`，p_in = 可核实费档中 ≤cap 的占比，钳制 [0.85, 1.15]（price=0.1 时最多 ±10%）；全预算内/全超预算分别标注；
   - 未核实：u 不变（1.0），仅标注「该档位/观察样本中预算内费档占 x%（未核实, 不参与排序）」——保证 §4.5 回退序与断言 B 不被破坏；
   - **超预算判定改用 `fee_min_tier`（最低可核实档）而非众数**，避免「明明有便宜档却被误判超预算而整院移出推荐」。
3. **费档分布透明化**：5 层 SlotInfo 全带 `fee_tiers`（分布）+ `fee_min_tier`（最低档）；report §6 输出「费档分布: 25(2%), 50(2%), 15(0%), 100+(0%); 最低档=25」；web 表格显示「最低档」。

**诚实边界**：live 拿不到时（如本批），档位先验的费用分布是单次观察样本（99.7% unknown），性价比标注只是弱先验；真实的普通门诊低价档（15/25）要 live（expert/normal 档）采集后才能核实并参与排序。

## 7. 验证记录

| 验证 | 结果 |
|---|---|
| `python -X utf8 app\test_llm_chain.py`（离线自检: 上下文 363/438、校验好/坏例、run_llm 全链、同义词桥、儿童过滤） | ALL OFFLINE CHECKS PASSED |
| `python -X utf8 -m app.main --check`（6 cases × A/B/C/D，含本 web job） | **24/24 PASS** |
| E2E `--demo chronic`（真实 LLM, Agent Service:7989） | LLM 直出 11 维权重含 price=0.1；报告 §3 中文名+match/price 说明；§6「挂号入口: 「内科」tab → 点「全科」行」；§8 步骤 4 用 tab+raw；费档分布+最低档+性价比标注齐全；4/4 断言 PASS |
| web 服务 | 已重启加载新代码（新 PID 75868, `/api/jobs` 200） |

## 8. 遗留 / 建议

- live 全模式 17 组 vs 60 分钟超时仍可能不够（expert 档 ≈68 分钟也偏紧）；如需 12 院全覆盖，建议把 `LIVE_TIMEOUT_S` 提到 4500~5400 或让 v2 支持**逐组增量落盘**（后者是 slots/ 侧改动, 需另行排期）。
- 档位先验 stratum 的 fee 分布样本量太小（n=1/unit, 99.7% unknown）→ 性价比标注弱；Track A 批次补采（费用明细只在医生主页层可观察）可显著改善。
- 本次 5 项修复全部在 app/ 层 + docs/，未触碰 ranking/、hospital_intel/、ssb_dept/、slots/（只读约束保持）。

## 9. 后续: live 顺序/超时/增量/回写 四项落地（2026-10-05 晚, 用户三项指令）

### 9.1 "第1名最后跑" 的真正根因
- 条件文件（live_conds_20261005T161917.json）**本身是静态分序**（SH-084 第1名在 index 0）。
- 但 v2 `expand_conditions` 末尾有一行 `units = sorted(order, key=(hid,tab,dept))` —— 按医院ID/tab/科室**字母序重排**（原意"最小化换医院"），把静态第1名 SH-084 实际推到了**第 6 位**才执行。用户观察到的"第十人民最后跑"是**对的**，此前"顺序没问题"的结论错误，已修正。
- **修复**: 改为保持调用方（静态分序）顺序执行。真机验证: 同一医院两条件组, 输入序=眼科先, 执行序=眼科先（修复前实测执行序=外科先, 因 '外'<'眼'）。

### 9.2 超时 60 分钟 → 可配置 + 优雅停止
- `--live-timeout <秒>`（默认 3600=60 分钟）; `prepare_live` 换算成 ISO 时间戳传 v2 `--deadline`, v2 每组开始前检查, 到点 `stop_reason='deadline'` 干净退出, 不启动后续组; 子进程硬杀余量 +900s 仅作兜底。
- 真机验证（run_20261005T223424, deadline=150s）: 眼科门诊 60 条/471s 跑完后到点, 整形外科门诊**未启动**, 日志 `deadline 到点(2026-10-05T22:36:51) -> 停止后续组`。

### 9.3 增量落盘（超时不再全丢）
- v2 每完成一组: `_checkpoint()` flush 合并记录快照 `<ts>_partial.jsonl` + 原子重写 run 摘要 `run_<ts>.json`（tmp+rename）。正常跑完终版仍写 `<ts>_all.jsonl` 并删 partial。
- app 侧 `_parse_live_runs`: `<ts>_all.jsonl` 缺失/空（硬杀场景）→ 自动改用 `_partial.jsonl`（已完成组记录仍可用）。
- 预算预警文案同步改为"到点后后续组不跑; 已完成组保留"。

### 9.4 live→静态回写（迭代收敛）
- 新增 `SlotResolver._persist_live_snapshots()`: live 解析后把 verified(ok/no_slot) 的 (院,科) 回写 `slots/out/<hid>-<slug>.json`（L2 观察快照层, **本包唯一允许写 slots/ 的点**, 模块 docstring 已注明）; 同 dept_id 的旧别名条目（如"眼科门诊(国际医疗500)"）自动清除, 避免 `_from_raw` 取到过期的旧条目。
- 实测闭环: run_20261005T223424 眼科门诊 60 条 → 回写 SH-084-sh084.json（24 tabs 其余条目无损保留）→ 模拟下次运行 `resolve()` 命中 **cache/observed, verified=True, age 0.2min**, earliest=2026-10-07 / fee=50 / 15 可约医生 / 7 天有号 —— 90 分钟 TTL 内该科室不再实抓。
- 过程中修复的隐患: `_live_info` provenance `captured_at` 曾回退到紧凑格式 run ts（`20261005T223424`, `_parse_ts` 不可解析 → 回写快照永远 stale）, 已改为 ISO 时间戳。

### 9.5 验证汇总

## 10. 用户重跑 web_1005T230305_0d64 的复盘（2026-10-06）

### 10.1 为什么第1/2名（SH-084/SH-003）被"显式排除"
- live 批量**确实撞了 60 分钟上限**: run_20261005T230344 `stop_reason=deadline`, 18 组完成 14 组（~60.3 分钟）, 增量落盘生效（14 组记录全部保留）。
- 但 SH-084（第1组, 111.9s 完成）与 SH-003（第5组, 227.7s 完成）**不是超时**——它们实查完成且**无号**: SH-084 整形外科门诊 0 可约医生/0 六字段记录 → `no_slot` → 号源效用 0.25 → 终分 0.2364 → 终排#59; SH-003 口腔科 日期条 3 名可约医生但医生页无可约记录 → `no_slot` → 终分 0.2267 → 终排#60。
- 旧排除文案只说"落在 Top20 之外"没讲原因 → 看起来像超时。**修复**: `recommend._excl_reason()` 现在写明 `实查无可约号源(no_slot...) → 号源效用=0.25 → 终分=x, 终排#N`（未核实用 `号源未核实(来源) → 按中性处理`）。
- 断言语义同步修正（`validate_result`）: A 改为"已解析号源的医院 ∈ top_list ∪ excluded(带原因, 含号源状态)"; C 改为"max(静态分)医院在 top_list 或显式排除"。no_slot 降级属**合理结果**（已核实的坏消息就该降权），但必须显式可见。已落盘的 0d64 结果用新语义复验 4/4 PASS 并重生 report.md（含重建的 #59/#60 终排, 与运行报告一致）。
- `meta.generated_at` 原为**开始**时刻（live 跑 60 分钟时误导）→ 现拆为 `started_at` + `generated_at`（完成时刻）。

### 10.2 日志可读性
- `prepare_live` 条件组清单现在输出医院名: `1) SH-084 第十人民医院 / tab=外科 / dept=整形外科门诊`（名称来自 `hospital_intel/manifests/_index.json`）。

### 10.3 web 位置自动填写
- 表单「位置」必填（代挂号勾选与否都要填, API 层 400 拦截不变）; 新增「📍 自动定位」+ 页面加载自动尝试: 浏览器 Geolocation（WGS-84）→ `GET /api/regeo`（服务端: 近似转 GCJ-02 + 高德逆地理, 优先采用高德匹配坐标校正, key 不下发前端）→ 回填地址描述; 提交走 `loc_text`, pipeline 再用高德 geocode 成精确 GCJ-02（**不依赖** WGS-84→GCJ-02 转换精度——实测该转换的公开多项式为流传变体, 上海偏移仅 ~20m 而非 ~500m, 故地址路径为唯一精确通道）。
- 非安全上下文（LAN IP 直连 http）浏览器 Geolocation 不可用 → 页面明示并保留手填。
- 实测: `/api/regeo?lng=121.4737&lat=31.2304` → `上海市黄浦区南京东路街道上海市人民政府`（GCJ-02, 缓存命中验证通过; 坏参数 400）。

| 项 | 结果 |
|---|---|
| v2 顺序修复真机（两条件组, 同医院） | 眼科(输入序第1) 先执行 ✓ |
| v2 deadline 优雅停止真机 | stop_reason=deadline, 后续组未启动, all.jsonl=60 条(仅已完成组) ✓ |
| 增量落盘 | checkpoint 日志 + partial 正常跑完即删 + app 侧 partial 回退代码就位 ✓ |
| app 回写闭环 | snapshot 生成 → 下次 resolve 命中 L2 verified ✓ |
| `--check`（6 cases）/ test_llm_chain | PASS |
| 改动文件 | slots/fetch_slots.py（v2: checkpoint+顺序+deadline日志）, app/slots.py（超时配置/deadline下发/partial回退/回写）, app/main.py（--live-timeout）, docs/SLOTS.md, 本文档 |


## 11. adb 设备定位实现与 WGS-84→GCJ-02 180 倍公式 bug（2026-10-06）

**背景**: web 页「📍 自动定位」原先走浏览器 Geolocation, 在局域网 IP 直连（http 非安全上下文）下不可用。
按用户要求改为: 按钮/服务启动时由**服务端 adb 读手机位置**（`dumpsys location`）, 30 分钟文件缓存, 页面刷新自动回填。

### 11.1 实现链路
`GET /api/device-loc?fresh&dev` → `locate.locate_from_adb()`（`adb -s <dev> shell dumpsys location`,
解析 `last location=Location[<provider> lat,lng hAcc=.. et=..]`, provider 优先级 fused>gps>network）
→ WGS-84→GCJ-02（`locate.wgs84_to_gcj02` 全式）→ `amap.regeo_gcj02(radius=1500)` 取地址 + 用高德返回的
`location` 权威 GCJ-02 坐标做最终校正 → 写 `app/cache/device_loc.json`（30min TTL）。
提交仍走 `loc_text`（地址文本）, pipeline 内高德 geocode 成精确坐标——不依赖转换精度。

### 11.2 dumpsys 格式坑
MIUI（476cf991）输出为 `last location=Location[fused 31.254646,121.432931 hAcc=48.0 et=+5d10h48m1s67ms {Bundle[...]}]`
（provider 小写在 `Location[` 内）, 旧代码正则 `(Network|GPS|Fused)\s*:\s*...` 一条都匹配不到 →
`--loc-adb` 与设备定位全部解析失败。已改为双格式兼容（新格式优先, 旧格式仅兜底不覆盖）。

### 11.3 公式 bug（关键）
`locate.wgs84_to_gcj02` 最后两步换算的分母缺 `*180.0`：
`dlat = dlat*180/(M/π)` 而标准式为 `dlat*180/(M*180/π)` → **偏移被放大 180 倍**。
实测后果: 手机 fused 位置（WGS 121.4329,31.2546, 普陀/长宁一带）被"换算"成 GCJ (121.4786,31.2364)——
偏差 ~4.5km 到人民广场, 高德 regeo 还"成功"返回了一个真实地址（南京东路步行街）, 极具迷惑性。
修复后同输入 → (121.4332,31.2545)（偏移 ~25m, 正常量级）, regeo → "普陀区宜川路街道石泉东路223号香溢花城"。
注: 该 bug 影响的是 `locate.py` 私有实现（`--loc-adb` 路径）; `amap.py` 的变体公式分母结构正确（系数变体）, 不受影响。

### 11.4 数据时效（age_s）
`dumpsys location` 返回的是手机**最后记录**的位置, 不是实时 GPS fix: 实测 fused 的 `et=+5d10h48m`
（elapsedRealtime 5.4 天前）, 按 `/proc/uptime` 折算 `age_s≈739434s`（~8.5 天前）——
用户手机近期未触发定位更新。UI 对 `age_s>30min` 显示 ⚠"该位置记录约 N 小时前, 可能非当前位置, 建议手填";
用户让手机用一次定位（地图/任何请求位置的 app）后, 点按钮重读即可拿到新值。

## 12. web_1006T142416_6daf（孩子咽喉不适, 2026-10-06 14:24, --live）— 四问复盘与修复

### 12.1 用户四问
1. 勾选了 --live, 为什么推荐第 1 名（闸北中心医院）是"未核实/档位先验"？一小时花在哪了？
2. 费档"分布 25:2% 50:2% 15:0%"加起来只有 4%, 剩下 96% 是什么？
3. 主诉明确说"倾向去儿童专属医院", 为什么儿童医院（儿童医学中心/泸定路/北京西路…）被患者层过滤剔除？
4. age_fit（人群适配）权重涨到 0.15 但无足轻重, 没影响结果——为什么？

### 12.2 Q1: live 跑了 3166s, 但第 1 名未核实
live 批量（12 个条件组, 单 adb 会话）实际结果: 8 组 ok 但 0 条六字段记录（页面无医生卡片也无"暂无可预约"文案 → 判 no_slot, 已核实）;
1 组 SH-171 闸北中心医院 error（no_doctor_cards）→ 未核实; 3 组有真实记录（SH-053 长征 10 条 / SH-095 第一人医 40 条 / SH-036 瑞金 30 条）。
旧 resolve 循环有"已核实 ok 满 3 条即早停"规则, 闸北中心 live 结果是 error → 回落 level_defaults 档位先验（未核实, u=1.0 中性）,
而已核实的 no_slot 医院 u=0.25 被压下去 → 未核实医院反而排第 1。
**修复**: 移除早停——prepare_live 是单次 adb 批量会话, 逐院 resolve 是纯缓存/字典查找（零设备开销）, 全部 12 家都 resolve;
另加 1% 未核实折扣（批次含已核实时 u×0.99, 刻意极小, 不违背"未核实不决定生死"铁律）。

### 12.3 Q2: 96% 是"费用未记录", 不是免费
档位先验来自 level_defaults.json stratum: "三级甲等|综合医院" n_units=383, 其中 99.7% 的 unit fee_tier=unknown
（静态字典大多没采过费用）。25:2% 50:2% 15:0% 是少量已记录的分布, 剩下 ~96% 就是 unknown。
**修复**: web feeDist 在已记录占比 <99.5% 时追加"未记录 X%"。

### 12.4 Q3: "孩子"被丢 → adult 带 → 儿童医院被患者层剔除
web 表单提交 age=null, spec_from_dict 直接落 adult band; 症状文本里的"孩子/倾向儿童专属医院"只有交互自由文本路径解析,
结构化路径不看 → adult 带过滤掉儿科目标科室 + 5 家儿童专属机构（按医院名）。
**修复**: a) dialogue.py 增加共享 _band_from_text(), spec_from_dict 在 age=null 时用症状文本推断 child;
b) main.py 加 LLM 后兜底: age=None 且 LLM scenario=peds 且 band≠child → 强制 child 并告警。
重跑确认: 患者=child, 儿童医院不再被剔除（改剔 4 个含"产科/妇科"的同族成员, 对儿童病例正确）。

### 12.5 Q4: 两层原因
**A 层（评分太钝+权重太低）**: 旧 age_fit 二值——"有儿科科室"和"儿童专属"同得 1.0, LLM 又只给 0.15。
**修复**: 三档（儿童专属机构=1.0 / 有可约儿科类科室(非专属)=0.75 / 无=0.5 中性）; prompt 明确"患者倾向儿童专属医院 → age_fit 0.20-0.30（一级权重）"。
**B 层（本轮定位, 儿童医院进不了 Top20 的真正原因）**: 即使 band=child, 儿童医院耳鼻喉科的 canonical 与 LLM 目标不是同一个 dept_id:
- LLM 选 D07533 "耳鼻喉科"（4 字: 耳鼻+喉, 60 家, 不含儿童医院）
- 儿童医学中心/张江院区: D07513 "耳鼻咽喉科"（5 字: 耳鼻+咽喉, 59 家）
- 泸定路/北京西路院区等: D07506 "耳鼻咽喉头颈外科"（8 字, 13 家）
前缀 family 规则要求公共前缀 ≥3 字, "耳鼻喉"≠"耳鼻咽"（第 3 字 喉/咽 不同）→ 未连通 → 儿童医院 match=0,
仅靠 age_fit 0.25×1.0 追不上综合医院的 match 1.0×0.36。
（上一轮"D07506 13 家却只扩出 1 家"的疑点 = 7 字"耳鼻喉头颈外科"(hc=1, 另一 dept_id) 与 8 字 D07506 混淆。）
**修复**: candidates.py 增加 SPECIALTY_FAMILIES 精选同族表（耳鼻喉/骨科/眼科/儿科/口腔）:
core 茎命名变体（stem+变体后缀 且 hc≥2）与目标**同 tier**; 相邻专科（五官科等）tier+1; hc=1 变体降 tier+1（防"耳鼻喉科蒋雯"类医生名伪名）;
原 3 字前缀规则保留作补充。

### 12.6 验证（同 spec 重跑, LLM 实时权重 match=0.36 / age_fit=0.22）
| # | 医院 | static | 说明 |
|---|------|--------|------|
| 1 | 儿童医学中心 (SH-018) | 0.9195 | 耳鼻咽喉科同 tier 命中 + 儿童专属 age_fit=1.0 |
| 2 | 第十人民医院 (SH-084) | 0.9126 | 耳鼻喉科 + 0.75 档 |
| 3 | 儿童医院北京西路院区 (SH-026) | 0.9057 | 耳鼻咽喉头颈外科同 tier + 专属 |
| 7 | 儿科医院 (SH-074) | 0.8902 | 专属 |
| 10 | 儿童医院泸定路院区 (SH-020) | 0.8759 | 耳鼻咽喉头颈外科同 tier + 专属（普陀, 离用户最近） |

修复前 Top3 为 第十人民/华山/长征（均为三甲综合, age_fit=0.75）; 修复后 4 家儿童专属机构全部进 Top10, 第 1 名为儿童专属。
回归: `python -X utf8 -m app.main --check` 14/14 PASS（chronic/interactive/peds/red_flag/match2_peds_throat/各 web job）。

### 12.7 改动文件
- app/candidates.py — SPECIALTY_FAMILIES + _family_member_prio/_family_info/_fam_vsuffix（替换 3 字前缀 _family_canonicals）
- app/dialogue.py — _band_from_text(); spec_from_dict age=null 推断
- app/main.py — LLM 后 band 兜底; 移除 resolve 早停
- app/slots.py — 批次含已核实时未核实 u×0.99
- app/web/index.html — feeDist 追加"未记录 X%"
- ranking/scripts/score_hospitals.py — age_fit 三档
- LLM prompt / docs/LLM_CONTRACT.md — 儿童专属偏好 → age_fit 0.20-0.30
## 13. web_1006T181534_9dc8（18:15 实时 job）：推荐前三全"档位先验" + 号源状态优先级

**用户反馈（2026-10-06 晚）**: 勾了 --live 的推荐表前三名（华山/同济/儿童医院泸定路）号源来源全是
"档位先验（未核实）"。用户判定: ① 若核实后无号源，排名应重算靠后（现在没号≠一直没号, 不剔除）;
② **有可用号源应作为独立高权重特征** —— 不够好的医院/科室/距离但有号 > 最好的医院/科室/距离但无号;
③ 核实到前几名无号后要继续算后续排名, 最终给出的医院科室**肯定是有号的才有意义**（系统就是做挂号推荐）。

### 13.1 事实还原（run_20261006T181615, 22 条件组, stop_reason=deadline）

| 层 | 事实 |
| --- | --- |
| live 批次 | 12 家候选院 × 每院 ≤5 目标科室 = 54 条件 → v2 合并 22 组, 60 分钟只跑完 6 院（儿童医学中心/北京西路/第十人民/仁济西/儿科医院/华山）; 同济/泸定路/长征/第一人医/仁济/SH-207 **根本没被抓取** → 停留档位先验(未核实) |
| 已核实有号 | 儿童医学中心 耳鼻喉科 30 条(最早 10-10, 100+); 第十人民 儿科门诊 140 条(最早 10-08, 50); 儿科医院 新生儿科 20 条(最早 10-07, 50) |
| 已核实无号 | 北京西路 耳鼻咽喉头颈外科; 仁济西院 耳鼻喉科（ok/0 条 = 页面确认无号） |
| 旧排序 | final=静态×u, 未核实 u=0.99≈中性 → 高静态未核实院(华山 0.8777/同济 0.8697/泸定路 0.8682)排在已核实有号院(儿科医院 0.8608/第十人民 0.8477)前面; 儿童医学中心 0.8079 掉到 #22 被排除 |

**华山异常（附带 bug）**: 华山 耳鼻喉科(D07533) live 实查 = 已核实 no_slot, 但结果上报未核实的兄弟科室
D07518「声带疾病专病门诊」(live error)。根因 `_best_info`(main.py) 旧 tie-break: 已核实 no_slot
(earliest_days=None→99) 与未核实 level_default(earliest_days=p50=5.0) 同分, 未核实者凭 ed 胜出 →
已核实负事实被未核实估计掩盖。

### 13.2 修复（代码已落, 2026-10-06）

1. **两级终排（用户铁律落地, app/recommend.py finalize）**: 排序 = ① 号源状态优先级
   **已核实有号(0) > 未核实(1) > 已核实无号(2)**, ② 同状态内 -final_score。
   - 有号是高权重独立特征: 不够好但有号 > 最好但无号（挂号推荐目的=找到可挂的号）;
   - 已核实无号 u=0.25 沉底但**不剔除**（现在没号≠一直没号）, excluded 写明"号源状态优先级沉底";
   - 全部未核实(fallback) → 全落状态 1 → 终排序 ≡ stage-1 静态序（§4.5 回落与断言 B 不变）;
   - recommendations(前 3) 自动 = 有号院（若有）——"最终给出的医院科室肯定是有号的"。
2. **`_best_info` 事实优先（app/main.py）**: 已核实有号 > 已核实无号 > 未核实。
   已核实事实（即使为负）必须优先于未核实估计; 华山下次 live 将正确显示"已核实·无号"沉底。
3. **live 批量每院只抓主科室（app/main.py）**: 只把每院第 1 个目标科室（tier 最高 = LLM 首选 =
   推荐行挂号入口）打成条件组 → 12 院 ≈ 12 组 ≈ 30-45 分钟, 60 分钟时限内可覆盖全部 12 院;
   兄弟科室 resolve 走缓存/档位（零设备开销）, 真实数据由离线 fetch_slots.py 批量积累（迭代收敛不变）。
4. **live 覆盖透明化**: `final.live_coverage`={n_routes, n_parsed, n_units, stop_reason, run_file};
   web 端 n_parsed<n_routes 显示警示横幅"实时核实仅覆盖 X/Y 家医院（deadline 到点, 后续组未跑）"。
5. **web 表 3 态号源徽标**: 已核实·有号 / 已核实·无号(红) / 档位先验·未核实; 最早可约列对无号显示
   "无号(已核实)"; 号源u tooltip 说明两级排序规则。

### 13.3 验证

- 单元: `_best_info` 3 例 + `finalize` 两级排序 8 例（含 fallback 不变量）—— 11/11 PASS。
- 9dc8 同数据重算: #1 儿科医院(有号 10-07) #2 第十人民(有号 10-08) #3 儿童医学中心(有号 10-10)
  #4 华山(未核实 0.8777) … #59/60 北京西路/仁济西(已核实无号, u=0.25, 显式排除+原因);
  recommendations = 3 家有号。
- 回归 `--check`: 15/15 case PASS（含 9dc8）。
- red_flag 离线 demo 端到端 PASS（全未核实 → 回落静态序, 断言 B 保持）。
- 文档: RANKING_SPEC §4.5（两级终排）/ SLOTS.md §3+§5（_best_info, 每院主科室, live_coverage）/ API.md（final 新字段）。

**遗留**: 9dc8 已落盘结果的 top_list 仍是旧序（result.json 不回溯重排; web 历史页点 9dc8 可见旧表,
重跑同表单即得新序）。下次 live 批次每院 1 组, 60 分钟可覆盖前 12 院 → 推荐前三大概率全部"已核实·有号"。

## 14. 一小时只核 6 院的根因 + 两阶段 live（probe/deep）+ 终裁 LLM（2026-10-06 夜）

### 14.1 时间诊断（用户质疑：一小时 6 院，平均每院 10 分钟，不正常）
对 9dc8 的 run_20261006T181615 逐组 cost_s 统计（22 组，共 3696s）：
- 0 记录组 85~216s（导航 + tab 切换 + 列表枚举滚动 + 逐医生点进去确认无号）；
- 有记录组：SH-084 儿科门诊 140 条 = 494s（≈8.2s/医生）；SH-018 耳鼻咽喉科 30 条 = 310s（≈10.3s/医生）。
**结论：时间大头 = 逐医生点进主页（~10s/人）+ 列表滚动枚举**，与用户判断一致。
而「有没有号」的判断信号全部在 L2 科室页一次 dump 内：
「去预约」按钮（可约医生卡）/ 日期条（部门级 有号·无号；屏外项 bounds=[0,0][0,0] 也在 XML，按文档序，**无需横滑**）/
「暂无可预约」空态；只有首屏未见「去预约」按钮才需要上下滑动（有界）。

### 14.2 实施（用户指令：--condition 增加目的参数；probe 快筛定粗排 → 前 6 深核 → 最后一次大模型终裁）
1. **`fetch_slots.py --mode probe`**（full 为默认, 行为不变）：
   - `probe_one_dept_unit`：L2 一次 dump（日期条 + 子 tab）→ 逐子 tab 快扫（`probe_scan_subtab`：
     统计 去预约/已约满/医生卡；未见按钮才 ≤4 次下滑，找到即停）；**不点医生主页**。
   - 状态映射：ok(按钮) / bar_hint(日期条有号无按钮, 弱信号) / no_slot(空态或条全无号) / error；
     输出 unit 一行写 `<ts>_all.jsonl`，run 摘要带 `mode=probe`（与 full 的 condition run 互不污染）。
   - 真机验证（2026-10-06 22:20, run_20261006T222043）：SH-084 儿科/儿科门诊 **61.9s** status=ok
     earliest=2026-10-08（与 full 494s 跑出的最早日完全一致）, 按钮 8（专家 7/普通 1, 专病空态）；
     SH-003 耳鼻咽喉科/耳鼻喉科 **134.1s** status=ok earliest=2026-10-21（专病门诊 1 个按钮, 日期条仅 10-21 有号）。
     均速 ~98s/院 ⇒ 一小时可覆盖 30+ 院（用户目标"几十家"达成）。
2. **`app/slots.py` 两阶段接入**：
   - `prepare_probe(queries, budget_s)`（Phase A, 预算=总 55%）→ `_parse_probe_runs`（只认 mode=probe）
     → `_probe_info`：ok/bar_hint→verified ok（probe_confidence=card_btn/bar_only），no_slot→verified no_slot，error→不入层；
   - resolve 层序变为：profile → 观察快照 → **deep live** → **probe** → 档位 → stub；
   - `prepare_live(queries, budget_s)`（Phase B）支持预算参数；`_parse_live_runs` 只认 mode∈(None,condition)
     （修复隐患：否则最新 probe run 会被当 full 解析, probe 的 "ok" 单元会被误读成 no_slot）；
   - probe/deep 的 verified 结果都回写 slots/out 快照（source=live_v2_probe/live_v2_condition, 同 dept_id 深结果覆盖）；
   - `_live_info` 增加 `doctors`（可约医生名单≤20）与 `slot_samples`（≤6 条 日期/时段/费档/医生/子tab 样例）——终裁素材。
3. **`app/main.py` 编排**：Phase A probe top20（`--probe-n`）→ 全候选集 resolve（取消 12 家预算, no_slot 事实全量保留）
   → 初排 finalize → Phase B deep 初排前 6（`--deep-n`, 预算=剩余 45%）→ 再 resolve + finalize
   → Phase C 终裁 LLM。
4. **`app/finalize_llm.py`（新）终裁 LLM = 全流程第 2 次推理**：
   - 输入：患者摘要 + 目标科室 tier + 明细列表（deep 已核实有号行：医生/费档/最早可约/号段样例）+ probe 摘要（其余已核实行, 仅供参考不可选）；
   - 输出：严格 JSON `final_picks[3]`（rank/hospital_id/doctor/reason）+ confidence；
   - 硬校验（`parse_final`）：医院必须∈明细列表、医生必须∈该院可约名单、rank 1..3 不重复；
     `apply_llm_picks`（recommend.py）再防幻觉：已核实无号院禁止入选、医院必须在 top_list；
   - 失败/校验不过/无明细 → 回退确定性 top3（两级排序），**不阻断交付**；`final.final_llm` 记录 applied/confidence。
5. **web**：号源列徽标区分「已核实·有号(探测)/已核实·无号(探测)」；覆盖横幅两阶段
   「Phase A 快速探测 x/y 院 · Phase B 深核 x/y 院 · 终裁LLM 已应用」；推荐行显示终裁医生+理由。

### 14.3 验证
- 单元 30/30（probe 状态映射、真机 run 解析、resolve 层序、parse_final 全分支、run_final mock、apply_llm_picks 含幻觉拒绝）；
- red_flag 离线端到端 PASS（无 live 时 §4.5 回退不变, 4/4 断言）；`--check` 15/15；index.html 内联 JS `node --check` PASS；
- 文档：RANKING_SPEC §4.6、SLOTS §5.1、API（--probe-n/--deep-n, live_coverage/final_llm schema）、本 §14。

## 15. web_1006T230719_f89d：深核费档丢失根因 + fetch_slots 提速（2026-10-07 凌晨）

### 15.1 现象
两阶段 live 首跑（probe 12 院 + deep 5 院）：推荐前三号源全部「已核实·有号」，但费档列全是 unknown。
用户判断：这三个科室都做了第二轮 adb 深核，六字段记录里明明有真实价格，结果里丢了。
同 run 的 SH-207 还有更隐蔽的问题：probe 说 ok（1 个按钮），deep 说 no_slot（0 条记录），最终展示成了 ok——
已核实的「无号」事实被「有按钮」的 probe 事实盖住了（与 9dc8 同类病灶，但这次出在 resolve 层序上）。

### 15.2 根因（三层叠加，全部在 app/slots.py）
1. **内存缓存不失效**：`_load_raw(hid)` 把快照文件读进 `self._raw` 后永不重读。Phase A probe 结束时
   `_persist_live_snapshots()` 把 **probe 数据（无费档）** 写进 `slots/out/<hid>-<slug>.json`；
   Phase B deep 结束后又把 **deep 数据（有费档）** 写回同一文件——但第二次 `_resolve_all()` 读的还是
   Phase A 之前加载的旧内存副本（probe 条目）→ deep 的费档永远到不了结果。
   证据：f89d 结果里 SH-026 provenance captured_at=23:30:33（=probe 写回时刻）、fee=unknown，
   而磁盘上同一 dept 条目最终是 deep 写回（fee=50, n_rec=30, earliest=10-09）。
2. **层序错误**：resolve 层序是 profile → 快照文件 → 本run deep → 本run probe → 档位 → stub。
   文件层（第 2）优先于本 run deep（第 3）——而文件层此时恰好是 probe 写回的无费档数据。
   本 run 的数据永远比文件里任何数据更新鲜、更完整，deep 必须排在文件层之前。
3. **`_from_raw` 丢分布**：文件条目带完整 `fee_tiers`（如 {'50':0.8,'100+':0.2}），`_from_raw` 却把它
   压扁成 `{fee_tier: 1.0}`，分布信息在读文件这一层就丢了。

### 15.3 修复
- `_persist_live_snapshots` 写回后 `self._raw.pop(hid, None)` 作废内存缓存；
- resolve 层序改为 **profile → deep(本run) → 快照 → probe(本run) → 档位 → stub**（deep 前移）；
- `_from_raw` 保留文件里的 `fee_tiers`/`fee_min_tier` 原值；
- **费档回填**：结论无费档（probe / 六字段未含费档）时从快照文件同 dept_id 条目回填，
  标记 `fee_from_stale=true`（费档是慢变字段，状态可过期但费档可作展示参考）；web 费档列显示「历史参考」。
- **写回保费**：probe 写回覆盖旧条目时继承旧条目的费档（旧代码直接删旧条目，静态数据费档被 probe 清掉）。
- 附带修复 SH-207 类问题：deep 前移后，deep 的 no_slot（已核实）会正确压过 probe 的 ok——铁律不变。

### 15.4 验证
- 8/8 单测（deep 压过文件+probe、缓存 pop、_from_raw 保分布、回填、回填不覆盖已知费档、
  verified raw 压过 probe、写回保费）；
- 回归 16/16（含自动入集的 f89d case）；
- exec-out dump 真机验证 OK（2.7s/dump，自动降级 pull 可用）。

### 15.5 fetch_slots 提速研究（用户观察：页面停留 1 分钟+ / 滚到头还滚 10 次+）
f89d 单组成本分解（probe 12 组 1346s / deep 5 组 1350s）：大头不是滚动，而是**导航**
（relaunch 44s + 搜索进 L2 53s ≈ 单组 59%），滚动/轮询是第二梯队。逐项处理：

| 项 | 旧行为 | 新行为 | 实测 |
|---|---|---|---|
| L2 卡枚举停底 | 末 3 卡 y 几何入稳定 key，底部回弹抖动 → stable 永不达 2 → 白滚满 12 次(~45s) | 仅卡名序列判稳定 | 到底 8.3s / 2 次 swipe |
| 死 subtab | 8 轮(~30s)+retry 6 轮(~23s)≈53s | 连续 3 次卡集不变提前判 unchanged(~12s)，retry 仍给 6 轮保慢切换 | 死 tab ~20s |
| 日期条全无号 | 照旧逐 subtab 扫描(每组 ~40-60s 白费) | bar 每日 chip 均明确有号/无号(unknown=0)，全无号直接 no_slot 跳过扫描 | SH-084 148.8s→90.1s |
| dump | shell dump + pull 往返；失败重试等 7s×4 | exec-out 直管道(省 pull 往返)+失败自动回退 pull；重试 3s×3 | 2.6-2.7s/dump |
| 搜索 LOADING | 检测到转圈睡 7s 再 dump（「停留一分钟」主因之一） | 2s 轮询 | — |
| 点医院结果 | 固定睡 5s 再 dump（慢则误 fail 触发昂贵 relaunch） | 轮询 L1 就绪（2s 间隔至多 10s）：快则即取，慢则不误 fail | — |
| 各处 swipe settle | 1.6 / 1.2s | 1.0 / 0.9s；回顶步长 900→1300px | — |

预期（90min 预算，probe 12 院 + deep 5 院）：probe 单组稳态 ~60-90s（no_slot 组 ~40-60s），
12 院 ≈ 15-20 分钟；deep 单组 ~3-6 分钟，5 院 ≈ 15-30 分钟——预算内全量完成，不再靠 deadline 截断。

### 15.6 遗留
- 慢 WebView（切 tab >33s）理论上可能被 3+6 轮窗口漏掉，概率低且后果=该 subtab 标 error（保守不采），深核阶段可覆盖。
- 搜索进 L2 的固定 sleep（1.0-1.8s 输入/聚焦类）未再压缩——广播输入有真实时序依赖，压了风险大、收益小。
- 两阶段 run 的 deep 组若被 deadline 截断，已采组走 `_partial.jsonl` 增量，不变。
