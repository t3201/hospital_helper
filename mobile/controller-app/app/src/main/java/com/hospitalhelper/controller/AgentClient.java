package com.hospitalhelper.controller;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * hh-server 推理网关客户端（POST {base}/v1/infer，SSE 语义与 app/llm_client.py 一致）。
 *
 * <p>2026-10-14 接口归集：App 不再直连 Agent Service（原 http://192.168.2.5:7989 + Bearer key），
 * 一律走自建服务端 {@code https://hh.ei.ac.cn:51120}，由服务端注入 Bearer token 并固定
 * {@code agent_ids=['hospital_helper']}；App 侧不再持有任何上游密钥。
 *
 * <p>约束#2：零第三方依赖 —— 用 {@link HttpURLConnection} 逐行解析 SSE；JSON 用 {@code org.json}（Android 自带）。
 *
 * <p>语义（与 Python 侧一致）：
 * <ul>
 *   <li>端点 {@code POST {base}/v1/infer}，body <code>{model_id, messages, stream:true}</code>，无 session_id。</li>
 *   <li>SSE：{@code event: init} → {@code data:{content,thinking,…}} → {@code data:[DONE]} 结束。</li>
 *   <li><b>只累加 content 作为答案</b>；thinking 只记长度（实测 content="" 时 thinking 有值）。</li>
 *   <li>非 JSON 的 data 行忽略，绝不拼进答案。</li>
 *   <li>超时：connect 5s / total 300s。</li>
 *   <li>安全：请求体只有 model_id+messages+stream；不读/不写/不打任何 key 或凭据。</li>
 * </ul>
 *
 * <p>base 默认写死公网地址（{@link #DEFAULT_BASE}）；仍允许 UI/表单覆盖，便于灰度与本机联调。
 * 见 {@link #resolveBase(String)}。
 */
public final class AgentClient {

    /** 服务端 label（不是模型名；服务端按 label 解析，实测 200）。 */
    public static final String DEFAULT_MODEL_ID = "default";

    /** 约束#2：连接 + 首字节快速失败窗口。 */
    public static final int CONNECT_TIMEOUT_MS = 5000;
    /** 约束#2：整个 SSE 流总预算。 */
    public static final int TOTAL_TIMEOUT_MS = 300000;

    /**
     * 归集后的唯一对外地址（与 server/config.py PUBLIC_BASE 一致，写死）。
     * 需要临时指向别的环境（本机联调 / 灰度）时在 UI 的“服务地址”里覆盖即可。
     */
    public static final String DEFAULT_BASE = "https://hh.ei.ac.cn:51120";

    private static final String PATH_INFER = "/v1/infer";

    private AgentClient() {
    }

    // ------------------------------------------------------------------ 结果与异常

    /** 一次 SSE 流的结果。 */
    public static class Result {
        public String content = "";
        public int thinkingChars = 0;
        public String sessionId = null;
        public long elapsedMs = 0;
        public long firstByteMs = 0;
        public int nData = 0;
        public int attempts = 1;
    }

    /** JSON 推理结果：obj + meta。 */
    public static class JsonResult {
        public JSONObject obj;
        public Result meta;
    }

    public static class AgentsvcError extends RuntimeException {
        public AgentsvcError(String msg) {
            super(msg);
        }
    }

    public static class LLMJsonParseError extends AgentsvcError {
        public final String raw;

        public LLMJsonParseError(String msg, String raw) {
            super(msg);
            this.raw = raw;
        }
    }

    // ------------------------------------------------------------------ base 解析

    /**
     * base URL 解析。override 非空则用之；否则 {@link #DEFAULT_BASE}。
     * 与 Python 侧 resolve_base_url 对齐：去掉尾部斜杠；通配 host(0.0.0.0/::) 改写为 127.0.0.1。
     */
    public static String resolveBase(String override) {
        String base = (override != null && override.trim().length() > 0)
                ? override.trim()
                : DEFAULT_BASE;
        if (!base.contains("://")) {
            base = "http://" + base;
        }
        base = base.replace("://0.0.0.0", "://127.0.0.1")
                   .replace("://[::]", "://127.0.0.1")
                   .replace("://::/", "://127.0.0.1/");
        while (base.endsWith("/")) {
            base = base.substring(0, base.length() - 1);
        }
        return base;
    }

    // ------------------------------------------------------------------ SSE 流

    /** 构造一条 message。 */
    public static JSONObject msg(String role, String content) {
        JSONObject m = new JSONObject();
        try {
            m.put("role", role);
            m.put("content", content);
        } catch (Exception ignored) {
        }
        return m;
    }

    /**
     * 单次 SSE 调用。messages 为 {@code [{role,content},…]}。
     * 失败（连接/首字节超 connect 5s、HTTP 非 200、读中断、总超 300s）抛 {@link AgentsvcError}。
     */
    public static Result streamChat(JSONArray messages, String modelId, String base, String apiKey) {
        String endpoint = resolveBase(base) + PATH_INFER;
        HttpURLConnection conn = null;
        try {
            long t0 = System.nanoTime();

            JSONObject body = new JSONObject();
            body.put("model_id", (modelId == null || modelId.isEmpty()) ? DEFAULT_MODEL_ID : modelId);
            body.put("messages", messages); // 只有 model_id + messages + stream，绝不带任何凭据
            body.put("stream", true);       // 归集后的 /v1/infer：true = 服务端原样转发上游 SSE
            byte[] payload = body.toString().getBytes(StandardCharsets.UTF_8);

            conn = (HttpURLConnection) new URL(endpoint).openConnection();
            conn.setRequestMethod("POST");
            conn.setConnectTimeout(CONNECT_TIMEOUT_MS);
            conn.setReadTimeout(TOTAL_TIMEOUT_MS);
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json");
            conn.setRequestProperty("Accept", "text/event-stream");
            conn.setRequestProperty("Cache-Control", "no-cache");
            if (apiKey != null && apiKey.trim().length() > 0) {
                // 归集后不再需要（服务端自己带 Bearer token）；仅为兼容旧配置保留，App 侧默认为空。
                conn.setRequestProperty("Authorization", "Bearer " + apiKey.trim());
            }

            try (OutputStream os = conn.getOutputStream()) {
                os.write(payload);
            }

            int code = conn.getResponseCode(); // 阻塞到连接 + 首字节（受 connect timeout 约束）
            long firstByteMs = (System.nanoTime() - t0) / 1000000L;
            if (code != 200) {
                throw new AgentsvcError("HTTP " + code + ": " + readSome(conn.getErrorStream(), 300));
            }

            Result r = new Result();
            r.firstByteMs = firstByteMs;
            long deadline = t0 + (long) TOTAL_TIMEOUT_MS * 1000000L;

            BufferedReader rd = new BufferedReader(
                    new InputStreamReader(conn.getInputStream(), StandardCharsets.UTF_8), 8192);
            String line;
            String curEvent = "";
            while ((line = rd.readLine()) != null) {
                if (System.nanoTime() > deadline) {
                    throw new AgentsvcError("总超时(>" + (TOTAL_TIMEOUT_MS / 1000) + "s): SSE 未收到 [DONE]");
                }
                line = line.trim();
                if (line.isEmpty()) {
                    curEvent = ""; // 空行 = SSE 事件块分隔
                    continue;
                }
                if (line.startsWith(":")) {
                    continue; // 注释 / keep-alive
                }
                if (line.startsWith("event:")) {
                    curEvent = line.substring(6).trim();
                    continue;
                }
                if (line.startsWith("data:")) {
                    String data = line.substring(5).trim();
                    if ("[DONE]".equals(data)) {
                        break;
                    }
                    r.nData++;
                    JSONObject obj;
                    try {
                        obj = new JSONObject(data);
                    } catch (Exception e) {
                        continue; // 非 JSON data 忽略，绝不拼进答案
                    }
                    if ("init".equals(curEvent) || "init".equals(obj.optString("type"))) {
                        if (obj.has("session_id")) {
                            r.sessionId = obj.optString("session_id");
                        }
                        curEvent = "";
                        continue;
                    }
                    String c = obj.optString("content", "");
                    if (!c.isEmpty()) {
                        r.content += c;
                    }
                    String th = obj.optString("thinking", "");
                    if (!th.isEmpty()) {
                        r.thinkingChars += th.length(); // 思维链：只记长度
                    }
                    curEvent = "";
                }
                // id: / 其他行：忽略
            }
            r.elapsedMs = (System.nanoTime() - t0) / 1000000L;
            return r;
        } catch (AgentsvcError e) {
            throw e;
        } catch (java.net.SocketTimeoutException e) {
            throw new AgentsvcError("连接/读取超时（connect " + (CONNECT_TIMEOUT_MS / 1000)
                    + "s / total " + (TOTAL_TIMEOUT_MS / 1000) + "s）: " + e.getMessage());
        } catch (Exception e) {
            throw new AgentsvcError("SSE 调用失败: " + e.getClass().getSimpleName() + ": " + e.getMessage());
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    // ------------------------------------------------------------------ 严格 JSON 调用

    /**
     * 严格 JSON 一次性调用（四段式 prompt 由调用方构造）。
     * 解析失败<b>重试 1 次</b>（带纠错提示）；再失败抛 {@link LLMJsonParseError}（携 raw）。
     * 网络类失败不重试，直接抛 {@link AgentsvcError} 供快速回落。
     */
    public static JsonResult chatJson(String systemPrompt, String userPrompt, String modelId, String base) {
        return chatJson(systemPrompt, userPrompt, modelId, base, null);
    }

    public static JsonResult chatJson(String systemPrompt, String userPrompt, String modelId, String base,
                                      String apiKey) {
        String lastRaw = "";
        String lastErr = "";
        for (int attempt = 1; attempt <= 2; attempt++) {
            String prompt = userPrompt;
            if (attempt == 2) {
                prompt = userPrompt + "\n\n你上一次的输出不是合法 JSON（问题: " + lastErr
                        + ", 原文: " + truncate(lastRaw, 400) + "）。"
                        + "请重新输出: 只输出一个 JSON 对象本身, 不要任何解释、前缀或代码块。";
            }
            JSONArray messages = new JSONArray();
            messages.put(msg("system", systemPrompt));
            messages.put(msg("user", prompt));

            Result r = streamChat(messages, modelId, base, apiKey);
            lastRaw = r.content;
            JSONObject obj = parseJsonLoose(lastRaw);
            if (obj != null) {
                JsonResult jr = new JsonResult();
                jr.obj = obj;
                jr.meta = r;
                r.attempts = attempt;
                return jr;
            }
            lastErr = "未找到可解析的 JSON 对象";
        }
        throw new LLMJsonParseError(
                "LLM 输出非合法 JSON（已重试 1 次仍失败: " + lastErr + "）", lastRaw);
    }

    // ------------------------------------------------------------------ JSON 宽松解析（对齐 _try_parse_json）

    /** 从答案内容（不含 thinking）解析第一个完整 JSON 对象；失败返回 null。 */
    public static JSONObject parseJsonLoose(String text) {
        if (text == null) {
            return null;
        }
        String t = text.trim();
        if (t.startsWith("```")) { // 模型若违规包了 markdown 围栏，先剥掉
            t = t.replaceFirst("^```[a-zA-Z0-9]*\\s*", "");
            t = t.replaceFirst("\\s*```$", "").trim();
        }
        JSONObject direct = tryParseObject(t);
        if (direct != null) {
            return direct;
        }
        // 花括号配对扫描（字符串/转义感知），从每个 '{' 起找第一个能解析成功的对象
        int start = t.indexOf('{');
        while (start >= 0) {
            int depth = 0;
            boolean inStr = false;
            boolean esc = false;
            for (int i = start; i < t.length(); i++) {
                char ch = t.charAt(i);
                if (inStr) {
                    if (esc) {
                        esc = false;
                    } else if (ch == '\\') {
                        esc = true;
                    } else if (ch == '"') {
                        inStr = false;
                    }
                } else {
                    if (ch == '"') {
                        inStr = true;
                    } else if (ch == '{') {
                        depth++;
                    } else if (ch == '}') {
                        depth--;
                        if (depth == 0) {
                            JSONObject o = tryParseObject(t.substring(start, i + 1));
                            if (o != null) {
                                return o;
                            }
                            break;
                        }
                    }
                }
            }
            start = t.indexOf('{', start + 1);
        }
        return null;
    }

    private static JSONObject tryParseObject(String s) {
        try {
            Object o = new org.json.JSONTokener(s).nextValue();
            if (o instanceof JSONObject) {
                return (JSONObject) o;
            }
        } catch (Exception ignored) {
        }
        return null;
    }

    // ------------------------------------------------------------------ util

    private static String readSome(InputStream in, int max) {
        if (in == null) {
            return "";
        }
        try {
            byte[] buf = new byte[max];
            int n = in.read(buf);
            return n <= 0 ? "" : new String(buf, 0, n, StandardCharsets.UTF_8);
        } catch (Exception e) {
            return "";
        } finally {
            try {
                in.close();
            } catch (Exception ignored) {
            }
        }
    }

    private static String truncate(String s, int n) {
        if (s == null) {
            return "";
        }
        return s.length() <= n ? s : s.substring(0, n);
    }
}
