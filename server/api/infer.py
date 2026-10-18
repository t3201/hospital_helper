# -*- coding: utf-8 -*-
"""api/infer.py — 大模型推理网关 /v1/infer。

对外（App / 以后其它客户端）只暴露这一个入口；App 不再知道 Agent Service 的地址与 token。

    POST /v1/infer
    {
      "messages": [{"role":"system","content":"..."}, {"role":"user","content":"症状描述..."}],
      "prompt":   "…",          # 与 messages 二选一（会自动组装成 user 消息）
      "system":   "…",          # 可选，配合 prompt 使用
      "model":    "default",    # 可选；Agent Service 侧的 **label**，默认取 config.DEFAULT_MODEL_ID
      "stream":   false         # true=原样转发上游 SSE；false=服务端聚合后返回 JSON
    }

服务端固定注入（客户端不可覆盖）：
    agent_ids = config.AGENT_IDS  ->  ['hospital_helper']
    Authorization: Bearer config.AGENTSVC_TOKEN

**原样透传**：用户症状描述放进 messages 不做任何改写；模型答案取上游 content 字段，
不做裁剪、不做二次加工（stream=true 时连 SSE 帧都逐行原样转发）。
"""
import json

import config
from core import audit
from core.http_util import ClientGone, HttpError, UpstreamError, upstream
from core.router import route

MAX_MESSAGES = 40


def _norm_messages(body):
    """把请求组装成上游 messages（保持原文，不改写）。"""
    msgs = body.get("messages")
    if isinstance(msgs, list) and msgs:
        out = []
        for m in msgs[:MAX_MESSAGES]:
            if not isinstance(m, dict):
                continue
            role = (m.get("role") or "user").strip()
            content = m.get("content")
            if content is None:
                continue
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            out.append({"role": role, "content": content})
        if out:
            return out
    # 便捷形态：prompt / text / q / symptom
    text = None
    for k in ("prompt", "text", "q", "symptom", "symptom_text", "message"):
        v = body.get(k)
        if isinstance(v, str) and v.strip():
            text = v
            break
    if text is None:
        raise HttpError(400, "缺少 messages 或 prompt（症状描述不能为空）")
    out = []
    sysp = body.get("system") or body.get("system_prompt")
    if isinstance(sysp, str) and sysp.strip():
        out.append({"role": "system", "content": sysp})
    out.append({"role": "user", "content": text})
    return out


def _require_token():
    """上游 token 只来自 .env / 环境变量；没配就快速失败，不去打上游。"""
    if not config.AGENTSVC_TOKEN:
        raise HttpError(503, "服务端未配置 Agent Service token"
                             "（请在 server/.env 里填 HH_AGENTSVC_TOKEN）",
                        provider="agentsvc", retryable=False)


def _upstream_headers(accept):
    return {
        "Authorization": "Bearer " + config.AGENTSVC_TOKEN,
        "Accept": accept,
        "Content-Type": "application/json; charset=utf-8",
    }


def _want_stream(ctx, body):
    v = body.get("stream")
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip():
        return v.strip().lower() in ("1", "true", "yes", "on")
    q = ctx.q("stream", "").lower()
    if q:
        return q in ("1", "true", "yes", "on")
    if "text/event-stream" in ctx.header("Accept", ""):
        return True
    return False


# ====================================================================== 非流式
def _infer_json(ctx, payload, timeout):
    url = config.AGENTSVC_BASE.rstrip("/") + "/v1/infer"
    _status, _hdrs, data, elapsed = upstream(url, method="POST", body=payload,
                                             headers=_upstream_headers("application/json"),
                                             timeout=timeout)
    try:
        obj = json.loads(data)
    except Exception as e:  # noqa
        raise UpstreamError("Agent Service 返回非 JSON: %s" % e, upstream_status=200, body=data)
    if not obj.get("success", True):
        raise UpstreamError("Agent Service 返回失败: %s"
                            % (obj.get("error") or obj.get("message") or json.dumps(obj, ensure_ascii=False)[:300]),
                            upstream_status=200)
    msgs = obj.get("messages") or []
    content, thinking, nickname, agent_id = "", "", "", ""
    for m in msgs:
        if not isinstance(m, dict):
            continue
        if (m.get("role") or "assistant") != "assistant":
            continue
        c = m.get("content")
        if isinstance(c, str):
            content += c                       # 答案原样累加
        th = m.get("thinking")
        if isinstance(th, str):
            thinking += th
        nickname = m.get("name") or nickname
        agent_id = m.get("agent_id") or agent_id
    stat = obj.get("stat") or {}
    return {
        "ok": True,
        "content": content,
        "thinking": thinking,
        "thinking_chars": len(thinking),
        "model_id": payload.get("model_id"),
        "agent_ids": payload.get("agent_ids"),
        "agent_nickname": nickname,
        "agent_id": agent_id,
        "session_id": obj.get("session_id"),
        "usage": stat,
        "elapsed_ms": elapsed,
        "provider": "agentsvc",
        "stream": False,
    }


# ====================================================================== 流式（SSE 原样转发）
def _infer_stream(ctx, payload, timeout):
    url = config.AGENTSVC_BASE.rstrip("/") + config.INFER_STREAM_PATH
    resp, _t0 = upstream(url, method="POST", body=payload,
                         headers=_upstream_headers("text/event-stream"),
                         timeout=timeout, stream=True)
    ctx.sse_start()
    content_chars = thinking_chars = n_frames = 0
    saw_done = False
    try:
        for raw in resp:
            ctx.sse_write(raw)                 # 逐行原样转发，不重新组帧
            try:
                line = raw.decode("utf-8", "replace").strip()
            except Exception:
                continue
            if line.startswith("data:"):
                data = line[5:].strip()
                if data == "[DONE]":
                    saw_done = True            # 上游已给结束标记，不再补发
                    break
                n_frames += 1
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    c = obj.get("content")
                    if isinstance(c, str):
                        content_chars += len(c)
                    th = obj.get("thinking")
                    if isinstance(th, str):
                        thinking_chars += len(th)
    except ClientGone:
        audit.warn("client gone during SSE /v1/infer (frames=%d)" % n_frames)
        return None
    finally:
        try:
            resp.close()
        except Exception:
            pass
    if not saw_done:
        ctx.sse_write("data: [DONE]\n\n")      # 上游异常收尾时补一个终止标记
    audit.info("infer stream done: frames=%d content_chars=%d thinking_chars=%d %dms"
               % (n_frames, content_chars, thinking_chars, ctx.elapsed_ms()))
    return None


# ====================================================================== 路由
@route("POST", r"^/v1/infer/?$")
def infer(ctx):
    """大模型推理网关：症状描述原样透传给 Agent Service（agent_ids 固定 hospital_helper）。"""
    body = ctx.json()
    _require_token()
    messages = _norm_messages(body)
    model_id = (body.get("model") or body.get("model_id") or config.DEFAULT_MODEL_ID)
    timeout = config.INFER_TIMEOUT_S
    try:
        t = int(body.get("timeout") or 0)
        if 0 < t <= config.INFER_TIMEOUT_S:
            timeout = t
    except (TypeError, ValueError):
        pass
    payload = {
        "model_id": model_id,
        "messages": messages,
        "agent_ids": list(config.AGENT_IDS),      # 服务端强制，客户端不可指定
    }
    stream = _want_stream(ctx, body)
    audit.info("infer stream=%s model=%s agents=%s msgs=%d chars=%d"
               % (stream, model_id, ",".join(config.AGENT_IDS), len(messages),
                  sum(len(m["content"]) for m in messages)))
    try:
        if stream:
            _infer_stream(ctx, payload, timeout)
        else:
            out = _infer_json(ctx, payload, timeout)
            audit.info("infer ok: content_chars=%d thinking_chars=%d %dms"
                       % (len(out["content"]), out["thinking_chars"], out["elapsed_ms"]))
            ctx.send_json(out, 200)
    except UpstreamError as e:
        audit.error("infer upstream failed: %s" % e.msg)
        if not ctx.sent:
            ctx.send_error_json(502, e.msg, upstream_status=e.upstream_status,
                                provider="agentsvc", retryable=True)
    except ClientGone:
        pass
    return None


@route("GET", r"^/v1/infer/?$")
def infer_hint(ctx):
    """用法提示（GET /v1/infer 只用于人肉自查，真正调用是 POST）。"""
    ctx.send_json({
        "ok": True,
        "usage": "POST /v1/infer  body={messages|prompt, system?, model?, stream?}",
        "agent_ids": list(config.AGENT_IDS),
        "default_model_id": config.DEFAULT_MODEL_ID,
        "token_configured": bool(config.AGENTSVC_TOKEN),   # 明文只存在于 server/.env
        "stream": "stream=true 时原样转发上游 SSE（event: init / data:{content,thinking} / data:[DONE]）",
    }, 200)
