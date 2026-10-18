# app/ — 挂号推荐运行时原型（PC 原型, 目标手机端）

纯标准库、零第三方依赖。权威契约：`ranking/RANKING_SPEC.md`（stage-1 静态排序 + 两段式接口）。
**只读** `ranking/`、`hospital_intel/`、`ssb_dept/`、`slots/`；不真挂号、不做打包。

## 怎么跑（验收命令）

```bash
python -m app.main --demo red_flag    # ① 胸痛/急诊
python -m app.main --demo chronic     # ② 高血压复诊（demo 固定点, stub 样例坐标）
python -m app.main --demo peds        # ③ 儿童生长发育
python -m app.main --check            # 回归断言: 对 out/*/result.json 逐例校验 A/B/C/D（任一失败非零退出）
```

**回归断言（B4 硬断言, 每次运行末尾必跑 + `--check` 可独立复跑）**:
- A: `set(candidates_static[:n_budget]) ⊆ set(final.top_list)`（已查询预算内前 12 家不得从终排消失）;
- B: `fallback_all_unverified` 时 `final.top_list` 前缀与 `candidates_static` **同序**（§4.5 回落到 stage-1 静态排序）;
- C: `final.top_list` 内最大静态分 == 候选集最大静态分（最好的医院不得消失）;
- D: 每个进入 `slot_queries` 的 hospital_id ∈ `final.top_list` ∪ `final.excluded`（显式"已排除+原因"清单, 不得静默消失）。

每个 demo 输出到 `app/out/<case>/result.json`（机器可读）+ `report.md`（人话报告），
报告含：位置 → 病情(≤3轮) → 权重推理 → 候选集 K → 号源来源标注 → 1–3 推荐(why+evidence) →
Top20 人工甄别列表（未核实显式标注）→ 模拟挂号 dry-run 步骤。


## Web 实测端（`app/web.py`, 纯标准库）

```bash
python -m app.web --port 8765    # 浏览器打开 http://127.0.0.1:8765/
```

- 单页表单（2026-10-08 简化）：**症状描述 / 就诊人下拉 / 预期费用尺度条 / 位置 / --live 档位** → **同一 `run_pipeline`**。已去掉 年龄·性别·时效·就诊类型·费用上限·水平预期·地铁优先·代挂号 复选框：这些由 LLM 从症状描述解析（`infer.py` 有「表单已取消的字段 — 由你从[自述]解析」段），费用档位是用户显式意图、非 auto 时覆盖 `price_cap_cny`。
- 位置必填（代挂号勾选与否都要填, API 层 400 拦截）: 「📍 自动定位」= 服务端 **adb 读手机最后位置**（`/api/device-loc`: `dumpsys location` fused>gps>network, WGS-84→GCJ-02→高德逆地理, 权威坐标校正）; 30 分钟文件缓存, **服务启动 5s 后自动读一次, 页面刷新即回填**, 点按钮实时重读; 提交走 `loc_text`（pipeline 再高德 geocode 成精确 GCJ-02）; 手机最后位置可能数小时~数天前（响应 `age_s`, UI 对 >30min 显示 ⚠）; 手填经纬度（GCJ-02）优先。（2026-10-06 修复: `locate.wgs84_to_gcj02` 分母缺 `*180/pi` 致偏移放大 180 倍, 详见 docs/DIAGNOSIS §11; 浏览器 Geolocation 路径已弃用——LAN http 下不可用。）
- 「👤 获取就诊人」= `GET /api/patients`（`app/patients.py`，**只读** adb uiautomator，不点新增/绑定/去预约）：随申办 · 预约挂号 · 「就诊人」列表 → 脱敏姓名/年龄/性别/是否医保卡（+医保卡号、脱敏证件号、创建天数）；性别无文本，按脱敏证件号（14 星 + 后 4 位）的**第 17 位奇偶**推断，依据写进 `gender_source`；30 分钟缓存 `app/cache/patients.json`，页面加载只读缓存（`?cache=1`），点按钮才实采（`?fresh=1`）；流水线在跑时返 409（adb 设备独占）。下拉首项「不选择」= 代他人挂号（`proxy=true`，年龄/性别交给 LLM 从描述解析）；选中就诊人则其年龄/性别/医保卡覆盖表单并 `proxy=false`。
- 历史面板 = **持久化**: `/api/jobs` 扫 `app/out/*/result.json`（web/CLI/demo 全可见, 跨重启不丢）, 点击任意一条 → `/api/result` + `/api/report` 显示完整结果与 report.md。
- live 超时可调: 表单「live 超时（分钟）」→ `live_timeout_min` → `--live-timeout`; 夜间大池批量建议 120-180。
- 病例经 `--spec-file` 结构化注入（`dialogue.spec_from_dict`, 与交互解析同 schema）, 不走自由文本正则。简化后 web 只送 `symptom_text` / `patient` / `patient_selected` / `fee_expect`（+位置）；`fee_expect` 档位 `auto|zero|ten|hundred|thousand|tenk|any` 定义在 `dialogue.FEE_EXPECT_TIERS`（前端尺度条与此一一对应），非 auto 时覆盖 `price_cap_cny` 并把档位原样交给 LLM 调医院类型。旧字段（age/gender/urgency_days/visit_type/price_cap_cny/level_expectation/proxy）仍兼容直传。
- SSE（`/api/stream?job=`）实时转发流水线 stdout 全量; 终帧 done 后 `/api/result?job=` 取 result.json。
- **全局单任务**（adb 设备占独 + stdout 捕获为进程级状态）: 忙碌时 POST /api/run 返 409。
- 任务落盘 `app/out/web_<ts>_<rand>/`, 同步参与 `--check` 回归门禁。端点清单见 docs/API.md §5。
- 不发起真实挂号; LLM 不可用时与 CLI 同样受控退出（SSE 展示错误框）。

交互式（真实使用形态，代挂号主场景 = `--loc` 人为指定位置）：

```bash
python -m app.main --loc "121.4737,31.2304" --symptom "胸痛"
python -m app.main --loc-text "中远两湾城" --symptom "眼睛看远处一直有重影"   # 真实高德 geocoding
python -m app.main --loc-adb --device <serial>          # 从手机取位置
python -m app.main --demo chronic --llm-model Qwen-local2   # 换 LLM model_id（唯一后端 = Agent Service, 默认 default）
python -m app.main --demo red_flag --live --device <serial>   # 批量 v2 live 抓取（--live-slot-type expert 快速档）
python -m app.main --loc "121.4737,31.2304" --symptom "胸痛" --non-interactive  # 管道/CI/agent 安全
```

**非交互（管道/CI/被 agent 调用）**: stdin 非 TTY 时**绝不调用 `input()`**（B3 残留修复）——
有 `--symptom` 即跳过 Q2/Q3 追问、其余取默认值并显式提示; 无 `--symptom` 给出可操作选项并以非零码退出。
任何情况下都不裸 traceback（`--trace` 可显式要完整堆栈）。

## 定位解析（locate.py + amap.py）

`--loc-text` 优先级（输出统一 GCJ-02，与 manifest 同系零转换）：

1. **amap_place**：高德 `v3/place/text`（POI 检索）。小区/地标/医院名这类"非结构化地址"只有 POI 检索准，
   `geocode/geo` 会退化甚至猜错 → 优先命中就用（返回带 address/adname/adcode）。
2. **amap_geo**：高德 `v3/geocode/geo`（结构化地址 geocoding），place 未命中时回落。
3. **stub**：`fixtures/geocode_stub.json` 内置小表兜底。**一旦最终落到 stub，`method=stub` 且
   `note`/控制台/`report.md` 均显式标注「位置为内置样例坐标，非真实定位」**，绝不把样例坐标当真实定位结果。

- 缓存命中时 `method` 加 `_cached` 后缀且结果带 `cached: true`（如 `amap_place_cached`）。
- `--loc "lng,lat"` 恒为 `method=user_specified`；`--loc-adb` 走 dumpsys（WGS-84→GCJ-02 换算）。
- demo 用例固定走 stub 小表/固定坐标（结果可复现，报告同样显式标注样例坐标）。

### 高德 key 来源与脱敏（amap.py，仓库硬规则）

- key 解析顺序：`os.environ['AMAP_KEY']`（**置空串=显式禁用**，不再回落文件）→ `hospital_intel/.env` 的 `AMAP_KEY` →
  都没有则高德禁用，`--loc-text` 直接走 stub（显式标注）。
- **绝不打印/记录含 key 的 URL**；日志只出现端点短名（`v3/place/text`）；
  任何错误消息若含 key 明文/URL 编码，一律脱敏为 `***`。
- key 不硬编码、不进日志、不进缓存文件、不进提交。

### 缓存规则

- geocode 结果落盘 `app/cache/amap/<place|geo>_<sha1(query)>.json`，TTL 30 天；**命中与未命中都缓存**（省配额）。
- 缓存内容只有结果结构（`found/lng/lat/address/adname/adcode/name/q/_ts`），
  **不含 key 明文 / URL 编码 / `key=` 片段**——可用 `python hospital_intel/scripts/scan_cache_leak.py` 复检。

### `--no-network`（离线演示）

```bash
python -m app.main --loc-text "徐汇区宜山路600号" --symptom "高血压复诊" --no-network
```

强制不发高德请求，`--loc-text` 只查内置 stub 小表；stub 小表外的地址会明确报错并列出可用地址。

## 模块与接口契约

| 模块 | 职责 | 关键接口 |
|---|---|---|
| `locate.py` | 位置（GCJ-02） | `--loc` > `--loc-text`(amap_place→amap_geo→stub 兜底, `--no-network` 只走 stub) > `--loc-adb`(dumpsys, WGS84→GCJ02) |
| `amap.py` | 高德 Web API 客户端（纯 urllib） | `resolve_key()` / `search_place(key,text)` / `geocode_geo(key,text)` → `(result{address,adname,adcode,location}, from_cache)`；脱敏+缓存内聚 |
| `dialogue.py` | 病情采集 ≤3 轮 → `CaseSpec`（含 年龄/性别/年龄带/代挂/时效/费用/水平预期） | `parse_case([r1,r2,r3])`（demo 与交互同一解析路径；含解析 notes）；非 TTY/`--non-interactive` 不 `input()` |
| `infer.py` | 头 LLM 推理（1/2, Agent Service）: LLM **直出** 特征权重向量(行和=1.0) + 目标科室(归一化词表直选) + scenario + 约束 | `infer_llm(spec, stage1, model, base)`; 基础数据上下文 = 可约科室 canonical（hc≥5, 363 条, 源自 ssb_dept/dept 归一化 DEPT_CORE） + 医院目录（438 家 等级/区县） + 场景基线权重 5 套（仅参考）; 词表外 → LLM triage_term → 症状词表（含同义词桥）; 结构校验失败纠错重试 1 次; LLM 不可用 → 抛错终止（**不静默回退**, rules 已废除） |
| `llm_client.py` | Agent Service 流式推理客户端（纯标准库） | `chat_json(sys, prompt, model_id, base)` → (obj, meta); SSE `/v1/infer/stream`; JSON 解析失败内建重试 1 次; thinking 仅记长度; 不触碰任何凭据 |
| `candidates.py` | 调 `ranking/scripts/score_hospitals.py`（**import 复用**）→ 候选集 K + §4.3 `SlotQuery` 序列 | 新契约 `Stage1.run_llm(term, llm_canonicals, scenario, loc, weights, patient_band)`（LLM 绝对权重 + 词表直选科室, 同构重算召回/打分/K/抓取计划）; 旧契约 `run()`（weight_overrides 乘子 + 词表 triage）保留; **患者层科室过滤**（成人/儿童互斥科室, 两入口同一 _excluded_reason） |
| `slots.py` | 号源缓存优先（**只读** `slots/`） | `SlotResolver.resolve(hid, did, ...)` → SlotInfo（必带 provenance） |
| `recommend.py` | 终排 = 静态分 × 号源效用（仅已核实者）；1–3 推荐(why+evidence) + Top20 | 完整候选集 K 参与排序, 未核实中性 u=1.0 不决定生死；§4.5 全失败回落静态序并显式标注；`validate_result` 硬断言 A/B/C/D |
| `finalize_llm.py` | 尾终裁 LLM（2/2）: 从 deep 深核名单定 top3+医生+理由 | `run_final`→picks/None; `parse_final` 严格白名单校验; 失败/不可用→确定性 top3, 不阻断（RANKING_SPEC §4.6） |
| `book.py` | 模拟挂号 | dry-run 默认开：只打印步骤序列；`--execute` 一律拒绝（450 家 capabilities.book=false） |
| `main.py` | CLI 编排 + 落盘 | `python -m app.main --demo <case>`；`--spec-file` 结构化病例；`--no-network` 强制离线；`--check` 回归断言；`--non-interactive` |
| `web.py` + `web/index.html` | web 实测端（ThreadingHTTPServer + SSE + 原生 JS 单页） | `POST /api/run` → job 工作线程调 `run_pipeline`; `GET /api/stream?job=` SSE 进度; `GET /api/result?job=`; `GET /api/jobs`; `GET /api/device-loc`; `GET /api/patients`; 全局单任务 |
| `patients.py` | 随申办「就诊人」列表采集（**只读** adb uiautomator, 纯标准库） | `get_patients(dev, fresh)` → dict(patients/captured_at/…); 缓存 `app/cache/patients.json` TTL 30min; 性别按脱敏证件号第 17 位奇偶推断（`gender_source`）; CLI `python -m app.patients [--cache]`; 绝不点 新增/绑定/去预约 |

### 号源层级（slots.py, 全部标注 slot_source ∈ stub|cache|live）

1. `slots/profiles/slot_profile.json`（§5 schema, `last_fetched_at`, TTL 90min）
2. `slots/out/<hid>-<slug>.json`（fetch_slots.py 观察快照, `captured_at`, TTL 90min）
3. 【`--live --device`】批量子进程调 `slots/fetch_slots.py`（**v2**, `--condition` 统一接口, `--live-slot-type` 控采集深度）→ 解析 `slots/out/condition_runs/` 的 run 摘要+六字段记录（两段: probe 快筛 --probe-n(12) → deep 深核 --deep-n(5), deep 覆盖 probe; v1 脚本保留为离线全院批量采集工具）
4. `slots/profiles/level_defaults.json`（档位兜底, 非实时, 带 stratum/confidence 出处）
5. `app/fixtures/stub_slots.json` 内置 stub（合成数据, 恒标注"号源未核实"）

## 降级行为（验收要求）

- **无 profile 文件照样跑**：1/4 层缺失 → 逐层降级到 stub，输出标注 `slot_source: stub` 与"号源未核实"；
- **TTL**：实测号源 4 分钟内可翻转 → 缓存 90min；过期观察 `verified=false`，仅作 `stale_reference`
  （如 SH-010 心血管内科: captured_at=2026-10-01T21:23, 龄期 1229min > 90min → 未核实）；
- **§4.5 回落**：无可核实实时号源 → 按静态排序展示并显式标注，不给出无出处的号源结论；
- **Agent Service 不可达/超时, 或 LLM 输出两次未通过结构校验** → 受控非零退出（rules 回退已废除, 不静默降级）;
- **无 AMAP_KEY / `--no-network`**：`--loc-text` 只走 stub 小表——位置是**内置样例坐标，非真实定位**，
  控制台与 `report.md` 显式标注；小表外的地址报错并列出可用地址（不猜坐标）。

## 已知局限

1. 特征权重向量为 **LLM 直出**（5 套场景基线权重表仅作 prompt 参考）；号源效用系数（0.03/天、0.4×tension 等）仍为启发式（未调参、无标注数据, RANKING_SPEC §3/§7）。**LLM 非确定**: 同一 case 两次运行的权重/科室可能不同; 回归断言 A/B/C/D 不依赖具体权重值。
   **终排铁律（B4 修复）**: 最终列表始终基于完整候选集 K 构造——已核实号源按 `静态分×号源效用` 参与排序,
   已尝试未核实/未查询者一律中性 u=1.0 按静态分排序并显式标注（档位先验/过期/stub/未查询）, 未核实数据不决定生死;
   全部未核实时终排序 ≡ stage-1 静态序（§4.5）。`slot_source` 标注五类不混:
   `live` / `cache/observed(已核实)` / `cache/observed(已过期)` / `cache/level_default(档位先验)` / `stub(合成)` / `未查询`。
2. 目标科室 = LLM 从**归一化词表直选**（可约 canonical, hc≥5, 363 条）；低频科室（hc<5）不在词表内, 依赖 LLM triage_term → 症状词表解析（含 app 层同义词桥 `复视/重影→斜视`）; 多症状复合句的次级科室由 LLM 自行取舍。
    词表/字典只读; 词表外词映射不上如实标注, 绝不编造科室。
3. 症状原文（含否定表述）直接交给 LLM 理解; 旧规则引擎的关键词匹配与否定前缀抑制已随 rules 后端废除。安全策略保留: LLM 判 red_flag 时 max_wait_days 强制 0（须今日就诊）。
    （历史: B4 否定前缀修复 无/没有/不伴/否认/非/未见 + 红旗词豁免 宁过勿漏 — 现由模型承担。）
4. **患者层科室过滤（B4 修复, app 层后处理）**: 成人/老年剔除儿科类目标科室（儿科/儿童/小儿/新生儿/青少年…）,
   儿童剔除成人科（妇科/产科/孕产/更年期/男科/老年…）; 过滤后按 stage-1 原语**同构重算**召回/打分/K/抓取计划,
   并在 `res['patient_filter']` 与报告 §4 显式列出剔除了什么。词表/字典只读不改; 若目标科室全部被剔除则保留原候选集并显式标注。
5. geocode 已接真实高德（`amap.py`, key 来自环境变量/`hospital_intel/.env`）；`fixtures/geocode_stub.json`
   仅为**离线/无 key 兜底**的内置样例坐标，用到时必显式标注“非真实定位”。
6. `--live` 对任意已建 manifest 的医院可触发（2026-10-03 起 slug 不再当准入门槛）：slug 从
   `slots/out/<hid>-*.json` 落盘文件名自动派生，缺失时由医院 id 确定性派生（`SH-084 -> sh084`），仅作输出文件名。
7. 距离=主院区直线距离（haversine, 单院区建模, 与 stage-1 一致）；多院区未建模。
8. 场景权重决定静态排序 → 同一症状在不同 scenario 下静态#1 可能不同（例: “重影”在误判的 chronic 场景下
   静态#1=SH-207, 修正为 community_first 后静态#1=SH-084）; 这是权重先验的正常表现, 非终排缺陷
   （终排断言保证 final#1 == 该场景静态#1, 已查询前 12 家必在终排列表内）。
9. 本原型不发起任何挂号/提交/支付请求（RANKING_SPEC §4.5 禁止事项）。
