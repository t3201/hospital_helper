# DATA — 数据字典

本包数据全部为**只读上游**（app 运行时只读, 不写回; 唯一写路径: `ranking/out/feature_norms.json` 的幂等重建 与 `app/cache/amap/`、`app/out/` 运行时产物）。

## 1. 文件清单

| 文件 | 大小 | schema 摘要 | 谁在用 | 出处/更新方式 |
|---|---|---|---|---|
| `hospital_intel/dept_index.json` | 3.2 MB | `{"depts": {"D04780": {"canonical":"心血管内科","kind":"clinic","l1_category":"内科","hospital_count":118,"visit_types":[...], ...}}, ...}`; **10,334 部门, 438 家医院**, canonical 全局唯一 1:1 | stage-1（kind 政策: 仅 clinic/specialty_clinic/screening 可作命中）; `infer.build_context`（LLM 词表=hc≥5 的 363 条 + canonical→dept_id 校验）; 患者层过滤 | 上游归一化整理产物: `ssb_dept/dept/DEPT_CORE.csv`(9,099 归一化科室) + `DEPT_ALIAS.json`(别名归并) + `DEPT_HOSPITAL.csv`(23,636 院×科实例) 经 `build_dept_index.py` 汇总。**上游源文件不在本包**（属归一化工程, 非运行期依赖） |
| `hospital_intel/manifests/_index.json` | — | 450 条 `[{"id":"SH-001","file":"东方医院南院.json","name","level","type","district","tel","geo_confidence",...}]` | stage-1 `load_inputs` | 450 家医院采集工程产物 |
| `hospital_intel/manifests/<医院名>.json` ×450 | ~10 MB 合计 | 每家: 名称/等级/类型/区县/主院区坐标(GCJ-02)/capabilities(含 book=false)/科室清单(tab×dept) | stage-1 `load_inputs` → `feature_norms`（距离/等级/科室齐全度/医保/地铁公交特征） | 同上; 单院区坐标（**多院区未建模**, 已知局限） |
| `hospital_intel/.env` | 42 B | `AMAP_KEY=...` | `app/amap.py::resolve_key`（环境变量优先; 置空串=显式禁用） | 高德 Web 服务 key。**机密, 勿提交公共仓库/勿外发**; 缺失时 geocoding 降级 stub 小表（显式标注） |
| `ssb_dept/triage/SYMPTOM_LEXICON.json` | 912 KB | `{"terms": {"胸痛": [{"dept_id":"D02633","dept_canonical":"内科","priority":1,"confidence":"high"}, ...], ...}}`; **1,822 词**; 词条 dept_id 100% 落在 DEPT_CORE 归一化集内 | stage-1 `resolve_terms`（精确→症状含词(最长)→词含症状(最短)→分词回落）; `infer._depts_from_term`（LLM 词表回落, 含 app 层同义词桥 复视/重影→斜视） | 症状→科室映射离线整理; 只读 |
| `ranking/scripts/score_hospitals.py` | 46 KB | stage-1 引擎（纯标准库; `load_inputs`/`load_or_build_norms`/`resolve_terms`/`hard_filters`/`score_hospital`/`pick_k`/SCENARIOS 权重表） | `candidates.py` 以 importlib **import 复用（禁止重写）** | `ranking/RANKING_SPEC.md` 的实现 |
| `ranking/RANKING_SPEC.md` | 15 KB | 权威契约: §3 特征与权重, §4.3 SlotQuery, §4.5 未核实回落, §5 号源 schema, §7 局限 | 全链路引用 | 人工维护 |
| `ranking/out/feature_norms.json` | 285 KB | 438 家医院特征归一（名称/等级/区县/距离基准/metro/bus/breadth/...）; 含输入指纹（SYMPTOM_LEXICON+dept_index 哈希） | stage-1（幂等: 指纹不符自动重建） | **预热缓存**, 可删（首次运行自动重建） |
| `slots/profiles/slot_profile.json` | 3.5 MB | 7,614 条 (dept_id,hospital_id) 号源画像: `{"status","earliest_days","fee_tier","has_slot_rate","tension","source":"observed|level_default","captured_at|last_fetched_at","confidence",...}` | slots 第 1 层（TTL 90min） | Track A 抽样采集（10 家/702 单元）+ 档位外推 |
| `slots/profiles/level_defaults.json` | 73 KB | 按 `等级\|类型` 分层的档位先验: stratum → {no_slot_rate, earliest_days, fee_tier, n_units, confidence} | slots 第 4 层 | 同上（外推产物, **非实时**） |
| `slots/profiles/variance_report.json` | 2.6 KB | 方差分解: 医院间 12.3% / 科室级 49.7% / 残差 38% | 文档引用（为何档位只作排序张力） | 同上 |
| `slots/out/SH-*.json` ×10 | ~3.3 MB | 观察快照: 医院×tab×科室 的号源姿态/医生明细（`captured_at`） | slots 第 2 层（TTL 90min）; slug 派生参照 | fetch_slots 采集落盘 |
| `slots/fetch_slots.py` | 113 KB | **v2 抓取脚本**（S20 `--condition` 统一接口: hospital/tab/dept/date/slot_type/time_range/doctor 七条件可重复; nav_state 复用最小回溯; 输出 `slots/out/condition_runs/`; 零模型守卫） | **slots 第 3 层 = app live 引擎**（`--live --device` 时子进程调用, 2026-10-05 接入） |
| `slots/six_fields.py` | 11 KB | v2 伴生模块: 六字段记录 schema/解析/指纹（纯标准库, 零模型） | v2 依赖 |
| `slots/fetch_slots_v1_20261004.py` | 118 KB | v1 抓取脚本（`--hospital` 全院姿态层 / `--dept` 单科室; ADB+随申办, 零模型） | 离线全院批量采集工具（app live 不再调用） | 2026-10-04 重构前的完整版备份; **v2 `--condition` 未随本包携带, 未接入** |
| `app/fixtures/demo_cases.json` | 1 KB | 3 个 demo 剧本（位置+≤3 轮问答脚本） | `dialogue.load_demo` | 人工维护 |
| `app/fixtures/geocode_stub.json` | 631 B | 3 个演示地址坐标（零网络兜底） | `locate.StubGeocoder` | 演示 fixture（**非真实 geocoder**） |
| `app/fixtures/stub_slots.json` | 1.1 KB | 合成号源最小样本（profile+档位均缺失时跑通流水线的兜底） | slots 第 5 层 | 演示 fixture |
| `app/out/{red_flag,chronic,peds,interactive}/` | ~620 KB | 已验证参考输出: `result.json`（机读 schema 见 docs/API.md）+ `report.md` | 回归基线/开发方参照 | 每次运行覆盖对应 case 目录 |
| `app/cache/amap/*.json` | — | geocode 落盘缓存（sha1(query) 命名, TTL 30 天, 含未命中负缓存省配额; **不含 key 明文**） | `app/amap.py` | 运行时自动; 可整体删除 |

## 2. 运行时写路径（仅三处, 均可安全删除重建）

1. `app/out/<case>/` — 每次运行产物（result.json + report.md）。
2. `app/cache/amap/` — geocode 缓存（30 天 TTL）。
3. `ranking/out/feature_norms.json` — 输入指纹变化时自动重建（幂等）。

其余一切（hospital_intel/ ssb_dept/ slots/ ranking 源码与规范）**只读**。

## 3. 坐标系与单位约定

- 位置坐标 **GCJ-02**（高德系; 与 manifests 坐标同系, 零转换）。
- 距离: haversine 直线 km（主院区; 0~25km 衰减, DIST_CAP 25km）。
- 时间: ISO 8601 本地时间; TTL 以分钟计（90min）。
- 金额: 人民币元; fee_tier 分档 `50` / `100+` / `unknown`（姿态层无价格, 仅医生明细层可观测）。

## 4. 上游归一化（不在本包, 供溯源）

科室归一化工程产物链（`ssb_dept/dept/`）：`DEPT_CORE.csv`（9,099 归一化科室: canonical/l1/kind/visit_types/别名样本）→ `DEPT_ALIAS.json`（别名→canonical）→ `DEPT_HOSPITAL.csv`（23,636 院×科实例）→ `build_dept_index.py` → **`dept_index.json`（本包）**。
症状词表 `SYMPTOM_LEXICON.json` 的全部 dept_id 均引用 DEPT_CORE 归一化集（零越界）—— 即 LLM 词表直选与词表回落在**同一归一化词表**内闭环。

## 5. 更新数据时的注意事项

- 替换 `dept_index.json` / `SYMPTOM_LEXICON.json` / `manifests/` 后, `feature_norms.json` 指纹失效自动重建（首次运行稍慢, 属预期）。
- `dept_index.json` 更新后 LLM 词表规模（hc≥5 的条数）随之变化, prompt 体量约按条数线性增减（当前 363 条 ≈ 14k 字符）。
- `slot_profile.json` 新增 observed 条目后, 对应 (院,科) 在 90min 内走"已核实"路径; 过期自动降 stale_reference。
- **不要**向 `app/` 内写入"规则兜底"逻辑 —— LLM 不可用 = 受控退出（见 README 硬不变量 3）。
