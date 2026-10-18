# 挂号推荐全流程图（矢量）

> 渲染说明：下图为 **Mermaid** 语法，在任何支持 Mermaid 的环境（GitHub / GitLab / VS Code / Typora / Obsidian）中会渲染为**矢量 SVG**，可无损缩放。
> 无需 Mermaid 渲染器时，可直接用浏览器打开同目录预渲染的 **`PIPELINE_FLOW.svg`**（由 mermaid-cli 12.0.0 实际渲染校验通过）。
> 需要重新导出：把代码块内容粘贴到 <https://mermaid.live>，右上角 Actions 即可导出。

## 1. 全流程图

```mermaid
flowchart TD
  %% ==================== 入口层 ====================
  subgraph ENTRY["入口层（两通道, 同构等价）"]
    direction LR
    CLI["CLI 通道<br/>python -m app.main<br/>--case demo:xxx ／ interactive ／ --spec-file spec.json"]
    WEBF["Web 通道<br/>浏览器表单：症状/年龄/性别/位置/时效<br/>费用上限/水平预期/地铁/代挂/live 档位"]
    POST["POST /api/run → 202 + job_id<br/>全局单任务（并发 → 409）"]
    SSE["GET /api/stream?job=（SSE 实时日志）<br/>GET /api/result?job=（result.json）"]
    WEBF --> POST
  end

  %% ==================== 管线 ====================
  subgraph PIPE["run_pipeline 九段管线（app/main.py）"]
    S1["段1 定位 locate.py + amap.py<br/>高德 v3/place/text POI 检索, GCJ-02（与 manifest 同坐标系, 零换算）<br/>key 缺失/超时 → 锚点兜底（stub, 带标注）"]
    S2["段2 病例采集 dialogue.py<br/>interactive：≤3 轮自由文本 → 正则抽取（年龄/性别/时效/偏好/代挂）<br/>demo ／ spec-file：CaseSpec 直入（与 web 表单同结构）"]
    S3["段3 头推理 infer.py（LLM 1/2; 唯一后端 agentsvc）<br/>prompt ≈14k 字：363 科室规范词 + 438 医院目录 + 5 场景基线权重<br/>SSE POST /v1/infer/stream, model_id=default<br/>输出：11 维权重向量（10 静态 + price 费用友好度, 行和=1）+ 1~5 目标科室（canonical 逐字）<br/>+ scenario + max_wait_days + price_cap_cny + triage_term"]
    FB["LLM 兜底（解析/校验 2 连败）<br/>lexicon resolve_terms + 场景基线权重<br/>标 parse_failed, 不阻断管线"]
    S4["段4 候选集 candidates.py（Stage-1 同构）<br/>canonical→dept_id 索引召回（仅 clinic/specialty_clinic/screening）<br/>患者带过滤（成人去儿科 ／ 儿童去成人专属）→ 硬过滤（半径上限）<br/>→ 10 特征 × 权重打分 → pick_k（K=14~60）→ fetch_plan（budget=12）"]
    S6["段6 终排 recommend.py（全候选集 K）<br/>两级排序: verified ok > unverified > verified no_slot<br/>同级比 final = static_score × slot_utility<br/>ok：u = max(0.6, 1−0.03×earliest_days) × (1−0.4×tension)<br/>no_slot：0.25 ／ unverified：1.0（仅标注, 不定生死）<br/>全批无 verified → §4.5 回退 Stage-1 静态榜 + 标“号源未核实”"]
    S6B["段6b 终裁 LLM finalize_llm.py（LLM 2/2, 同后端 agentsvc）<br/>输入=deep 深核明细+患者摘要; 输出 top3+医生+理由<br/>严格白名单校验; 失败/不可用 → 回退确定性 top3, 不阻断"]
    S7["段7 模拟挂号 book.py（dry-run）<br/>输出建议医院/科室/医生/时段/费用<br/>绝不真实提交/支付"]
    S8["段8 断言 validate_result<br/>A/B/C/D（B4：静态 top12 ⊆ final.top_list 等）→ checks"]
    S9["段9 落盘<br/>app/out/&lt;job&gt;/ 的 result.json + report.md + meta<br/>web job = web_&lt;ts&gt;_&lt;rand&gt;"]
    S1 --> S2 --> S3
    S3 -->|结构校验通过| S4
    S3 -->|2 连败| FB
    FB --> S4
    S6 --> S6B --> S7 --> S8 --> S9
  end

  %% ==================== 段5 号源 5 层 ====================
  subgraph SLOT["段5 号源解析 SlotResolver 五层（app/slots.py, 缓存优先, TTL 90 分钟）"]
    direction TB
    L1["L1 slot_profile.json<br/>批量观察画像"]
    L2["L2 slots/out/&lt;hid&gt;-&lt;slug&gt;.json<br/>整院快照"]
    L3["L3 live 实时抓取（仅 --live 触发）<br/>v2 fetch_slots.py --condition 批量; 两段: probe 快筛(12)→deep 深核(5)<br/>probe 命中落 3.5 层, deep 覆盖 probe"]
    L4["L4 level_defaults.json<br/>档位先验（level_default）"]
    L5["L5 stub_slots.json（stub）"]
    L1 -->|miss| L2
    L2 -->|miss/过期| L3
    L3 -->|miss| L4
    L4 -->|miss| L5
    L1 -->|命中: cache observed| S6
    L2 -->|命中: cache observed| S6
    L3 -->|命中: live| S6
    L4 -->|命中: level_default| S6
    L5 -->|命中: stub| S6
  end
  S4 -->|fetch_plan 的 hid,dept 对| L1

  %% ==================== L3 内部 v2 ====================
  subgraph V2["L3 内部：v2 采集（零模型：纯 ADB + UI XML 文本匹配）"]
    direction TB
    V1["nav_state.json 页面级状态复用<br/>stay_l2 ／ back_to_l1 ／ re-enter ／ full reset（最小回溯）"]
    V2n["随申办 → 444 医院列表 → H5 搜索医院名<br/>→ 点医院卡 → L1 选科室页 → 点科室行 → L2 科室页"]
    V3["子 tab（专家/普通/专病；--live-slot-type 可限档）<br/>滚到底（2 连 dump 稳定）→ 枚举医生卡 → 点卡体 → 医生主页（六字段页）"]
    V4["日期条 30 天 × 时段行 → 六字段记录<br/>就诊时间/医院/科室/医生/门诊类型/挂号费用（费用原文 U+FFE5 保留）"]
    V5["输出 condition_runs/&lt;ts&gt;_all.jsonl + run_&lt;ts&gt;.json + raw XML 证据<br/>绝不点“确定”（不挂号）"]
    V1 --> V2n --> V3 --> V4 --> V5
  end
  L3 -->|批量: manifest l1 路由 hid,dept → tab,科室名; UTF-8 condition 文件; 单次 adb 会话| V1
  V5 -->|解析 → SlotInfo verified| S6

  %% ==================== 出口层 ====================
  subgraph OUT["出口层"]
    direction LR
    RES["result.json（机读）<br/>meta/loc/spec/inference/stage1/slots/final/booking/checks"]
    REP["report.md（人读, 10 章节）"]
    APIRES["Web：GET /api/result?job="]
    CHECK["--check 回归门禁<br/>5 cases × A/B/C/D = 20 断言<br/>（4 个内置 demo + 全部 web job）"]
  end
  S9 --> RES
  S9 --> REP
  S9 --> APIRES
  S9 -.->|web job 落盘自动纳入| CHECK
  POST -.->|job 线程执行管线| S1
  S9 -.->|stdout 逐行捕获推送| SSE

  classDef stage fill:#e8f0fe,stroke:#1a73e8,color:#174ea6;
  classDef live fill:#fce8e6,stroke:#ea4335,color:#a50e0e;
  classDef cache fill:#f1f3f4,stroke:#9aa0a6,color:#3c4043;
  classDef out fill:#e6f4ea,stroke:#188038,color:#1e4620;
  class S1,S2,S3,FB,S4,S6,S7,S8,S9 stage;
  class L3,V1,V2n,V3,V4,V5 live;
  class L1,L2,L4,L5 cache;
  class RES,REP,APIRES,CHECK out;
```

## 2. 图例与读图要点

| 颜色 | 含义 |
|---|---|
| 蓝 | 管线主阶段（每次都执行） |
| 灰 | 号源缓存层（L1/L2/L4/L5，按序落空才下沉） |
| 红 | 实时采集（L3 及其内部 v2 五步；**仅 `--live` 时执行**，其余时候整块旁路） |
| 绿 | 出口（落盘 / 查询 / 回归门禁） |

- **实线** = 主流程与数据流；**虚线** = 辅助通道（web job 线程、SSE 日志推送、回归门禁纳入）。
- **两个关键分支点**：
  1. **LLM 双败兜底**（段3）：JSON 解析或结构校验（词汇/场景枚举/权重和）连续失败 → 用症状词表 `resolve_terms` + 场景基线权重顶上，标 `parse_failed`，管线继续。
  2. **§4.5 全未核实回退**（段6）：本批没有任何 verified 实时号源时，终排回退到**完整** Stage-1 静态榜（不是残集），全部条目显式标“号源未核实”——先验永不冒充实时。
- **号源 5 层的 miss 链**：L1→L2→L3→L4→L5 依次落空才下沉；**任何一层命中即返回**，返回的 `SlotInfo` 必带 `slot_source`（live / cache observed / cache level_default / stub / 未查询）与 `provenance`（来源文件、时间戳、TTL 校验）。

## 3. 分阶段说明

| 段 | 模块 | 输入 → 输出 | 失败路径 |
|---|---|---|---|
| 1 定位 | `locate.py` + `amap.py` | `--loc/--lat/--lon` → GCJ-02 坐标 + 锚点标注 | key 缺失/超时 → 锚点兜底（stub），报告标注 |
| 2 病例采集 | `dialogue.py` | 自由文本（≤3 轮）或 spec → `CaseSpec`（症状/年龄/性别/时效/偏好/代挂） | 轮次用尽 → 用当前已抽取 spec 继续 |
| 3 头推理（LLM 1/2） | `infer.py` + `llm_client.py` | spec + ≈14k 字上下文 → 权重向量(10, 和=1) + 目标科室(1~5 canonical) + scenario + 约束 | 校验/解析失败 → 带修正提示重试 1 次 → 再败 → lexicon 兜底 |
| 4 候选集 | `candidates.py` + `ranking/scripts/score_hospitals.py`（importlib 复用, 非重写） | 权重 + canonical → K=14~60 候选 + fetch_plan（≤12 个 (hid, dept)） | LLM 科室出界 → 走 lexicon 兜底链同构重算 |
| 5 号源解析 | `slots.py` | fetch_plan → SlotInfo × n（带 slot_source + provenance） | 五层落空终到 stub；`--live` 子进程失败 → 记日志, 继续下层 |
| 6 终排 | `recommend.py` | 静态分 + SlotInfo → final 列表（final_score / why / evidence） | 全批无 verified → §4.5 静态榜回退 + “号源未核实”标注 |
| 6b 终裁（LLM 2/2） | `finalize_llm.py` | deep 深核明细 + 患者摘要 → top3+医生+理由 | 校验失败/不可用 → 确定性 top3（两级排序）, 不阻断 |
| 7 模拟挂号 | `book.py` | top 推荐 → dry-run 挂号建议（医院/科室/医生/时段/费用） | 永不真实提交（450 家医院 capabilities.book=false） |
| 8 断言 | `validate_result` | 全量结果 → A/B/C/D 断言 pass/fail | 失败 → 非零退出码（见 docs/API.md），不改推荐内容 |
| 9 落盘 | `main.py` | 上述全部 → `result.json` + `report.md` + meta | web job 目录 `web_<ts>_<rand>`，自动纳入 `--check` |

**L3（v2 实时采集）五步**：状态复用决策 → 导航到目标科室页（444 列表 → H5 搜索 → 医院卡 → L1 科室页 → L2）→ 子 tab 枚举医生卡（点**卡体**进医生主页，六字段页）→ 日期条 30 天 × 时段行抓六字段记录 → 落 `condition_runs/` + raw XML 证据。全程零模型（纯 ADB + XML 文本匹配），启动时断言无 LLM 环境变量。

## 4. 关键契约与不变量

1. **LLM 唯一后端 = agentsvc; 全程恰好 2 次推理（段3 头推理 + 段6b 终裁, 均走同一后端）**（SSE `POST /v1/infer/stream`，model_id=`default`）。LLM 直出绝对权重向量（代码侧重归一化到行和=1）、目标科室必须**逐字**来自 `dept_index.json` 规范词表（代码校验，拒绝出界名）、scenario 限 5 值枚举。详见 `docs/LLM_CONTRACT.md`。
2. **slot_source 五值标注**：`live` / `cache(observed)` / `cache(observed expired)` / `cache(level_default 档位先验)` / `stub` / `未查询` —— 每层命中即打上对应标签，先验永不冒充实时。详见 `docs/SLOTS.md`。
3. **§4.5 回退**（`RANKING_SPEC` 契约）：全批无 verified → 回退**完整** Stage-1 静态榜 + 显式“号源未核实”，绝不把档位先验当实时事实呈现。
4. **B4 回归断言**（A/B/C/D）：静态 top12 ⊆ final.top_list、fallback top-20 与静态榜同序、静态最高分保留、被查询医院不得静默消失。`--check` = 5 cases × 4 = 20/20 全过才放行（含所有 web job）。
5. **安全底线**：不真实挂号/提交/支付；v2 采集**绝不点“确定”**；号源采集零模型（纯 ADB + UI XML）。
6. **全局单任务**（web）：adb 设备排他 + stdout 捕获是进程级状态 → 同时只允许 1 个 job，并发请求返回 409。
7. **CJK 安全通道**：live 条件经 UTF-8 JSON 文件（`--condition-file`）传入 v2，中文永不经过 argv。

## 5. 入口 / 出口速查

**入口**

| 通道 | 命令 |
|---|---|
| CLI demo | `python -X utf8 -m app.main --case demo:red_flag`（另有 chronic / peds / interactive） |
| CLI 结构化 | `python -X utf8 -m app.main --case spec-file:spec.json`（与 web 表单同 schema，见 `docs/API.md`） |
| CLI 实时号源 | 追加 `--live --live-slot-type expert --device 476cf991`（档位：expert/normal/specialty/不限） |
| Web | `python -X utf8 -m app.web --port 8765`（默认绑 0.0.0.0）→ 表单提交 |

**出口**

| 出口 | 内容 |
|---|---|
| `app/out/<job>/result.json` | 机读全量结果（meta/loc/spec/inference/stage1/slots/final/booking/checks，schema 见 `docs/API.md`） |
| `app/out/<job>/report.md` | 人读报告（10 章节：定位/病例/推理/候选/号源/终排/挂号/断言/…） |
| `slots/out/condition_runs/` | 仅 `--live`：v2 六字段记录 JSONL + run 汇总 + raw XML 证据 |
| Web SSE | `GET /api/stream?job=` 逐行实时日志；`GET /api/result?job=` 取 result.json；`GET /api/jobs` 历史任务 |

## 6. 复现与验证

```bat
python -X utf8 app\test_llm_chain.py            :: 离线自检（不依赖 LLM/设备）
python -X utf8 -m app.main --check              :: 回归门禁（20 断言全过）
python -X utf8 -m app.main --case demo:red_flag :: 单 case 全流程
python -X utf8 -m app.web --port 8765           :: web 实测端
```
