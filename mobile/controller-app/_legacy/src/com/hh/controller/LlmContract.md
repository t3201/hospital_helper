# LlmContract（手机端控制器 App 版）

> 本文件是 `docs/LLM_CONTRACT.md` + `app/llm_client.py` 语义在 **Android 端**的抄录。
> 唯一后端 = Agent Service。传输层实现见 `AgentClient.java`（零第三方依赖）。

## 1. 传输层（`AgentClient.streamChat` / `chatJson`）

- 端点：`POST {base}/v1/infer/stream`（SSE）。
- 请求体：`{"model_id": "default", "messages": [{"role":"system","content":...},{"role":"user","content":...}]}`。
  - `model_id` 默认 `default` —— 是服务端 **label**，不是模型名；服务端按 label 解析（实测 200）。
  - **不携带 session_id**；响应 `event: init` 里的 session_id 只记录、不依赖、可空。
- SSE 帧：
  ```
  event: init
  data: {"session_id": "...", "type": "init"}

  id: 1
  data: {"role": "assistant", "content": "…", "thinking": "…"}

  data: [DONE]
  ```
- **累加规则（硬约束）**：
  - **只累加 `content`** 作为答案。
  - `thinking` 是独立字段，**只记长度**（`thinkingChars`），**绝不并入答案**（实测 `content=""` 时 `thinking` 有值）。
  - 非 JSON 的 `data:` 行忽略，绝不拼进答案。
  - `data: [DONE]` 结束；服务端若直接 EOF（无 `[DONE]`）也按结束处理。
- 超时：**connect 5s / total 300s**。
  - `setConnectTimeout(5000)`；`setReadTimeout(300000)`；循环内用 deadline 兜底（超过 300s 抛错）。
- 安全：请求体**只有** `model_id + messages`；**不读/不写/不打印/不落盘**任何 `api_key` 类字段；
  **永不访问** `/v1/models`（其响应内含 api_key）。

## 2. base URL 解析（约束#9）

- 手机端无环境变量；`AgentClient.resolveBase(override)`：override 非空 → 用之；否则 `DEFAULT_BASE`。
- **默认 `http://192.168.1.100:7989`（PC 局域网 IP:7989）** —— 手机上 `127.0.0.1:7988` 指向手机自己，无效。
- 与 Python `resolve_base_url` 对齐：去尾部斜杠；通配 host(`0.0.0.0`/`::`) 改写 `127.0.0.1`。
- 明文放行：`AndroidManifest.xml` 开 `usesCleartextTraffic="true"`（HTTP 非 TLS）。

## 3. System prompt

```
只输出 JSON，不要解释、不要 markdown 代码块
```

## 4. User prompt 结构（对齐 `infer._build_prompt`，约 14.2k 字符）

按序 7 块：角色与任务 / 基础数据(可约科室词表 + 医院目录 + 场景基线权重) / 特征定义 /
患者自述 / 就诊人信息与偏好 / 输出 JSON 字段。**P0–P2 只做传输层，不组装 prompt。**

## 5. 输出 JSON schema（模型必须返回）

```jsonc
{
  "scenario": "community_first|chronic_followup|complex_severe|red_flag|peds",
  "weights": { "match":0.24, "level":0.28, "breadth":0.04, "distance":0.14,
               "metro":0.02, "bus":0.02, "key_specialty":0.04, "emergency":0.22,
               "age_fit":0.0, "price":0.04 },          // 0~1，行和≈1.0（代码归一到恰=1.0）
  "dept_canonicals": [ {"dept":"急诊内科","tier":1}, … ] // tier=1 首选 / 2 相关 / 3 弱相关
}
```

## 6. 解析与重试

- `chatJson`：从**答案内容**（不含 thinking）解析第一个完整 JSON 对象。
- 解析失败 → **内置重试 1 次**（附纠错提示 + 上文原文前 400 字）。
- 再失败 → 抛 `LLMJsonParseError`（携 raw）。
- 网络类失败（连不上 / HTTP 错 / 超时）**不重试**，直接抛 `AgentsvcError` 供快速回落。

## 7. 全流水线恰好 2 次推理（P3 落地）

- 第 1 次 = 头推理（权重向量 + 目标科室），`infer.py` 对应段。
- 第 2 次 = 尾终裁（白名单内定 top3），`finalize_llm.py` 对应段。
- 两次走**同一** `chatJson` / 同一后端。
