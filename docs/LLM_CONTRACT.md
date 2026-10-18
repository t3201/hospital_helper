# LLM_CONTRACT — 头推理(权重/科室)接口契约（唯一后端 = Agent Service）

2026-10-04 重构后：`--llm rules` **已废除**, `--llm ollama` **已并入** agentsvc。
**契约** = ollama 式（模型**直接返回**特征权重大小 + 目标科室 + 约束）；
**调用** = agentsvc 式（SSE 流式客户端 `app/llm_client.py`）。
旧 agentsvc 的"字段抽取 → 规则映射"职责已废除。

> **2026-10-06 起全流水线恰好 2 次 LLM 推理**: 本契约覆盖第 1 次（段3 权重推理, infer.py）。
> 第 2 次 = 段6b 尾终裁（`app/finalize_llm.py`）, 走同一 `chat_json`/同一后端;
> 其输入/输出 schema 与严格白名单校验见 RANKING_SPEC §4.6。

## 1. 传输层（`llm_client.chat_json`）

- 端点：`POST {base}/v1/infer/stream`（SSE）。
- base URL 解析顺序：`--agentsvc-base` 参数 → 环境变量 `RUNTIME_SERVER_URL` → `AGENTS_URL` → 默认 `http://127.0.0.1:7988`（host/port 可用 `RUNTIME_SERVER_HOST`/`RUNTIME_SERVER_PORT` 覆盖）。`0.0.0.0` 自动映射 `127.0.0.1`（Windows 连通性）。
- 请求体含 `model_id`（默认 `'default'` —— 是服务端 **label**, 不是模型名; 服务端按 label 解析）与 system/user 消息; **不携带 session_id**（响应里返回的 session_id 仅记录不追踪）。
- SSE 帧：`event: init` → `data: {session_id, type, ...}` → `id: N` + `data: {role, content, thinking, ...}` → `data: [DONE]`。**`thinking` 与 `content` 是独立字段; thinking 只记长度（`thinking_chars`）, 绝不并入答案。**
- 超时：connect 5s / total 300s; 纯 `http.client` + 读线程 + 队列死线（绕开 Python 3.11 socket read-timeout 缺陷）。零第三方依赖。
- `chat_json(sys, prompt, model_id, base)` → `(obj, meta)`; meta 含 `elapsed_ms / first_byte_ms / attempts / thinking_chars / session_id / n_data / content`。
- JSON 解析：从流内提取 JSON 对象; 解析失败**内建重试 1 次**（附纠错提示）; 再失败抛 `LLMJsonParseError`（携带 raw 前 500 字）。
- 凭据安全：不读取/打印/落盘任何 `api_key` 类字段。

## 2. System prompt

```
只输出 JSON，不要解释、不要 markdown 代码块
```

## 3. User prompt 结构（`infer._build_prompt`, 实测 ~14,200 字符 / ~6,400 tokens）

按序 7 块：

1. **角色与任务**："你是挂号推荐系统的权重推理模块。任务: 根据患者自述+就诊人信息+偏好, 直接给出 特征权重向量 + 目标科室。只输出一个 JSON 对象。"
2. **基础数据(只读上下文)**（全部来自只读数据, 每次运行现组装）：
   - `### 可约科室词表(归一化 canonical, 括号=覆盖医院数; 仅可预约门诊类, 共 363 条)`
     每行 `眼科(118家)`。**来源 = `hospital_intel/dept_index.json`**（上游归一化整理, 见 DATA.md）, 过滤条件: `kind ∈ {clinic, specialty_clinic, screening}` 且 `hospital_count >= 5`（`DEPT_HC_MIN`）, 按覆盖数降序。
   - `### 医院目录(名称|等级|区县, 共 438 家)` 每行 `华山医院|三级甲等|静安区`。来源 = stage-1 `feature_norms`（manifests 派生）。
   - `### 场景基线权重(仅参考; 你输出的是**绝对权重向量**, 不是这些基线本身)` 5 套（community_first / chronic_followup / complex_severe / red_flag / peds）, 来自 stage-1 `SCENARIOS`（专家先验, 供模型校准量级, 不允许照抄）。
3. **特征定义**：12 个分量逐项中文说明（10 静态 + age_fit 人群适配 + price 费用友好度, 见 §5）。
4. **患者自述**：`[自述] <symptom_text>` + `[交互补充] <第 2/3 轮原文>`。
5. **就诊人信息与偏好**：`位置=... | 年龄=... | 性别=... | 年龄段=... | 代挂号=... | 就诊类型=... | 期望等待≤N天 | 费用上限=... | 水平预期=... | 地铁偏好=...`（交互解析结果, 供参考; 与自述冲突时以自述为准）。
6. **输出 JSON 字段**（见 §4）。

## 4. 输出 JSON schema（模型必须返回）

```jsonc
{
  "scenario": "community_first|chronic_followup|complex_severe|red_flag|peds",  // 5 选 1
  "weights": {                                                                  // 绝对权重向量
    "match": 0.24, "level": 0.28, "breadth": 0.04, "distance": 0.14,
    "metro": 0.02, "bus": 0.02, "key_specialty": 0.04, "emergency": 0.22,
    "age_fit": 0.0, "price": 0.04
    // 12 键可选; 与场景无关的特征给 0 或省略; 每个 0~1; 行和≈1.0（代码归一化到恰=1.0）;
    // price=费用友好度(性价比偏好): 用户设明确费用上限/强调性价比时给较高值, 红旗急诊且费用不敏感给 0
  },
  "dept_canonicals": [
    {"dept": "急诊内科", "tier": 1},    // tier=1 首选: 症状直接指向
    {"dept": "心血管内科", "tier": 1},
    {"dept": "急诊科", "tier": 2},      // tier=2 相关: 也能看该症状/常见首诊
    {"dept": "冠心病", "tier": 3},      // tier=3 弱相关: 邻近专科/保守治疗后备
    {"dept": "内科", "tier": 3}
  ],
  // 目标科室数组, 1~5 个, 按优先级排列; 每项 {"dept": canonical名, "tier": 1|2|3}（裸字符串/单元数组容忍, 视为 tier=1）;
  // **dept 必须逐字取自 §3.2 的可约科室词表**, 不可自造或改写（代码对 dept_index 校验 canonical+kind）;
  // tier 必须拉开: 弱相关科室不得与首选同为 1; 儿科病例以症状专科为 tier=1, 纯人群科室(儿科/小儿内科)至多 tier=2
  "triage_term": "胸痛",     // 最能代表主诉的单个症状词（可用自述原词; ≤16 字）
  "max_wait_days": 0,        // int 0~90（0=今日必须）; red_flag 场景代码强制 0
  "price_cap_cny": null,     // int 1~100000 或 null
  "reasoning": ["62岁男性胸痛+放射+冷汗=红旗急症, 急诊与心血管权重最高, 就近三甲", "..."]
  // 中文数组 ≤3 条: 解释急/重/缓、交通、费用、水平预期如何影响权重与科室选择
}
```

## 5. 特征定义（权重向量 12 分量; 前 11 与 stage-1 打分一一对应, price 作用于终排）

| 特征 | 含义 |
|---|---|
| `match` | 科室命中质量（目标科室层级×置信度; LLM tier1=首选→1.0 / tier2=相关→0.70 / tier3=弱相关→0.45; 0=未命中。弱相关科室不再与首选平权） |
| `level` | 医院等级（专家先验映射: 三甲=1.0 … 一级≈0.15-0.22） |
| `breadth` | 科室齐全度（bookable L2 科室数, log 压缩） |
| `distance` | 直线距离衰减（1-km/25km, 0~25km） |
| `metro` | 地铁可达性（步行分钟/直线距离） |
| `bus` | 300m 内公交站台数 |
| `key_specialty` | 特色科室加分（覆盖稀薄 4.4%, 缺失=0, 不可作主权重） |
| `emergency` | 急诊能力（红旗急症场景用: 任一门急诊科室） |
| `chronic` | 慢病专病随访能力（慢病场景用: 命中科室含"专病"visit_type） |
| `chronic_fit` | 分级诊疗先验（慢病场景: 低等级≤二级且 p1 科室命中 → 社区承接加分） |
| `age_fit` | 人群适配。peds 场景三档(2026-10-06): 儿童专属机构（名含儿童医院/儿童医学中心/儿科医院/复旦儿科）=1.0, 有可约儿科类科室(非专属)=0.75, 无=中性 0.5（**叠加加分非惩罚**）; 其他场景恒 1.0。人群(儿科/成人)维度由此特征单独承担, 不再与症状科室混在 dept_canonicals 里平权。**权重约定(2026-10-06)**: 儿科病例 0.05~0.15; 患者明确偏好儿童专属医院时 0.20~0.30(一级权重); 非儿科 0 |
| `price` | 费用友好度（性价比偏好）。**不进 stage-1 静态打分**（静态 10 特征表不动）; 在终排 `slot_utility` 中只对**已核实**号源生效: 可核实费档中预算内占比 p_in → u×(1+price·(2·p_in−1)), 钳制 [0.85,1.15]; 未核实仅标注档位先验的预算内占比, 不参与排序（铁律: 未核实不定生死） |

## 6. 校验与重试（`infer._validate` + 主循环）

| 校验项 | 规则 |
|---|---|
| 顶层 | 必须是 JSON 对象 |
| `scenario` | ∈ 5 枚举, 否则报错 |
| `weights` | 只收 12 特征名下的正数（bool 拒绝）; 全缺/全非正 → 报错; 行和 >0 → **归一化到恰 =1.0**; stage-1 使用前剥离 price 并对前 11 维重归一（`candidates._norm_weights`） |
| `dept_canonicals` | 每项为对象 {"dept", "tier"}（tier=1首选/2相关/3弱相关; 非 int/越界→夹取, 缺省 1）, 裸字符串/元素数组容忍(视为 tier=1); 最多取 5 个; dept `strip` 后对 `dept_index` 查 canonical 且 kind 可约; 有效项去重保留; **有效集为空 → 报错**（附词表外名称明细）; 词表外名称单独记录并在 reasoning 注记"已忽略" |
| `triage_term` | 字符串 ≤16 字, 否则 None |
| `max_wait_days` | int/float 0~90, 否则 None |
| `price_cap_cny` | int/float 0<x≤100000, 否则 None |
| `reasoning` | 字符串当单条; 最多 5 条, 每条截 120 字 |

重试策略：
1. **JSON 解析失败** → `chat_json` 内建重试 1 次（附纠错提示）→ 再失败 `LLMJsonParseError`。
2. **结构校验失败** → 主循环带错误明细纠错重试 **1 次**（prompt 追加"你上一次输出未通过校验: ...; dept_canonicals 必须逐字取自词表, weights 行和≈1.0"）→ 再失败抛 `LLMJsonParseError`。
3. 两者皆败 → `main` 捕获 → **受控非零退出**（附可操作提示）, **不静默回退规则**（rules 已废除）。

## 7. 科室解析链（绝不编造科室）

```
LLM dept_canonicals（逐字校验通过 → 有效集）
   └─ 有效集为空 → LLM triage_term（或自述原文）
        → SYNONYMS 同义词桥（复视/重影 → 斜视; 仅用于词表解析, 不用于科室直选）
        → 症状词表 resolve_terms（ssb_dept/triage/SYMPTOM_LEXICON.json, 1,822 词 → D-ID）
        → kind 过滤（仅 clinic/specialty_clinic/screening）
        → 仍无目标科室 → RuntimeError（main 受控退出, 提示换更具体症状）
```
`dept_how` 落盘取值：`llm`（直选成功）/ `llm_term_lexicon`（词表回落）。
另做 **triage_term × 词表交叉核对**（仅 reasoning 注记: 一致/词表指向 X 而 LLM 选了 Y——以 LLM 为准, 词表仅供参考; 无命中亦注记）。

## 8. 下游约束合并（`infer_llm` 返回前）

- `max_wait_days` 缺省：red_flag → 0, 否则取交互解析 `urgency_days`; red_flag 且 >0 → **强制 0** 并注记"须今曰就诊, 安全优先"。
- `price_cap_cny` 缺省 → 回落交互解析值。
- `triage_term` 缺省 → 取第一个有效科室 canonical。

## 9. 实测参考（Agent Service, model_id=default, 2026-10-04）

| case | 耗时 | prompt | 输出要点 |
|---|---|---|---|
| red_flag | 25.1s | 14,189 字符 | weights: level=0.28, match=0.24, emergency=0.22; 5 科室全词表内; thinking=4,156 字(仅记长度) |
| chronic | 30.2s | ~14.2k | chronic=0.06, chronic_fit=0.08, level=0.05; 4 科室; **LLM 自抽 price_cap=50** |
| peds | 67.9s | ~14.2k | match=0.42, level=0.06, distance=0.30; 3 科室（儿童保健科/儿童生长发育/儿科） |

非确定性说明：同一 case 两次运行的权重/科室可能不同; 回归断言 A/B/C/D 设计为权重无关, 不受影响。
