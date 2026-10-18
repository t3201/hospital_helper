# -*- coding: utf-8 -*-
"""http_util.py — 请求上下文 + JSON/SSE 读写 + 上游 HTTP 调用（纯标准库）。

约定：
  * 出参一律带 Content-Length（除 SSE / 文件流），因此可安全启用 HTTP/1.1 keep-alive。
  * 错误统一 {"ok": false, "error": "..."}；HTTP 状态码语义化（400/401/404/413/502/500）。
  * 上游调用失败一律归一为 UpstreamError，由 api 层翻译成 502 + 可读 error。
"""
import gzip
import io
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

import config


# ====================================================================== 上游调用
class UpstreamError(RuntimeError):
    """上游（Agent Service / 高德 / 天地图）不可用或返回非 2xx。

    upstream_status: 上游 HTTP 状态码；网络层失败为 None。
    body:            上游响应体片段（已脱敏，最多 800 字），便于排查配额/参数错误。
    """

    def __init__(self, msg, upstream_status=None, body=None, elapsed_ms=None):
        RuntimeError.__init__(self, msg)
        self.msg = msg
        self.upstream_status = upstream_status
        self.body = (body or "")[:800]
        self.elapsed_ms = elapsed_ms


def _mask(text):
    """日志/错误体脱敏：把已知密钥形态打码。"""
    if not text:
        return ""
    out = str(text)
    for secret in (config.AGENTSVC_TOKEN, config.AMAP_KEY, config.TIANDITU_KEY, config.ADMIN_TOKEN):
        if secret and len(secret) >= 6:
            out = out.replace(secret, secret[:4] + "***" + secret[-2:])
    return out


def _decompress(raw, encoding):
    enc = (encoding or "").lower()
    try:
        if enc == "gzip":
            return gzip.decompress(raw)
        if enc in ("deflate",):
            return zlib.decompress(raw)
        if enc == "br":
            import brotli  # 标准库无 brotli；缺失时原样返回
            return brotli.decompress(raw)
    except Exception:
        return raw
    return raw


def _upstream_ssl_context(url):
    """https 上游的 SSL 上下文；HH_AGENTSVC_INSECURE_TLS=1 时跳过自签证书校验。"""
    if not url.startswith("https:"):
        return None
    ctx = ssl.create_default_context()
    if getattr(config, "AGENTSVC_INSECURE_TLS", False):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def upstream(url, method="GET", body=None, headers=None, timeout=30, stream=False):
    """通用上游请求。

    body:   bytes | str | dict(dict 自动 JSON 编码)
    stream: True  -> 返回 (response, elapsed_ms)，由调用方自行 read/迭代（SSE 用）
            False -> 返回 (status, headers, text, elapsed_ms)
    """
    if isinstance(body, dict):
        body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    elif isinstance(body, str):
        body = body.encode("utf-8")
    hdrs = {"User-Agent": config.SERVER_NAME, "Accept": "*/*"}
    if body is not None:
        hdrs["Content-Type"] = "application/json; charset=utf-8"
    for k, v in (headers or {}).items():
        hdrs[k] = v
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    t0 = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=_upstream_ssl_context(url))
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read()
        except Exception:
            pass
        raw = _decompress(raw, e.headers.get("Content-Encoding"))
        raise UpstreamError("上游 HTTP %s: %s" % (e.code, _mask((raw or b"").decode("utf-8", "replace"))[:300]),
                            upstream_status=e.code, body=_mask((raw or b"").decode("utf-8", "replace")),
                            elapsed_ms=int((time.time() - t0) * 1000))
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        raise UpstreamError("上游连接失败: %s (%s)" % (reason, url), elapsed_ms=int((time.time() - t0) * 1000))
    except (socket.timeout, TimeoutError):
        raise UpstreamError("上游超时(>%ss): %s" % (timeout, url), elapsed_ms=int((time.time() - t0) * 1000))
    except Exception as e:  # noqa
        raise UpstreamError("上游请求异常: %s: %s" % (type(e).__name__, e), elapsed_ms=int((time.time() - t0) * 1000))
    elapsed = int((time.time() - t0) * 1000)
    if stream:
        return resp, elapsed
    raw = resp.read()
    raw = _decompress(raw, resp.headers.get("Content-Encoding"))
    try:
        resp.close()
    except Exception:
        pass
    return resp.status, dict(resp.headers), raw.decode("utf-8", "replace"), elapsed


def upstream_json(url, method="GET", body=None, headers=None, timeout=30):
    """上游请求 + JSON 解析（解析失败按 UpstreamError 抛出）。"""
    status, _hdrs, text, elapsed = upstream(url, method=method, body=body, headers=headers, timeout=timeout)
    try:
        return json.loads(text), elapsed
    except Exception as e:  # noqa
        raise UpstreamError("上游返回非 JSON: %s" % e, upstream_status=status, body=_mask(text), elapsed_ms=elapsed)


# ====================================================================== 请求上下文
class Ctx:
    """一次请求的上下文；handler 通过它读入参、写出参。"""

    def __init__(self, handler, method, path, query, spec=None):
        self.handler = handler            # BaseHTTPRequestHandler
        self.method = method
        self.path = path
        self.query = query                # dict[str, str]（取第一个值）
        self.route_spec = spec            # 命中的路由（可带 limit 等元信息）
        self.params = {}                  # 路由捕获组
        self._body = None
        self._json = None
        self._json_err = None
        self.status = 200
        self.started = time.time()
        self.sent = False           # 响应头是否已发出（发出后不能再改状态码）
        self.head = False           # HEAD 请求：只发头不发体

    # ---- 入参
    def header(self, name, default=""):
        try:
            return self.handler.headers.get(name, default) or default
        except Exception:
            return default

    def q(self, name, default=""):
        v = self.query.get(name)
        return v if v not in (None, "") else default

    def is_tls(self):
        return isinstance(self.handler.request, ssl.SSLSocket)

    def body_bytes(self, limit=None):
        if self._body is not None:
            return self._body
        n = self.handler.headers.get("Content-Length")
        try:
            n = int(n) if n else 0
        except ValueError:
            n = 0
        limit = limit or config.MAX_BODY_BYTES
        if n > limit:
            raise HttpError(413, "请求体过大(%s > %s)" % (n, limit))
        try:
            self._body = self.handler.rfile.read(n) if n > 0 else b""
        except Exception as e:  # noqa
            raise HttpError(400, "读取请求体失败: %s" % e)
        return self._body

    def body_text(self, limit=None):
        return self.body_bytes(limit).decode("utf-8", "replace")

    def json(self, required=True):
        """解析 JSON 请求体。required=True 时空体/非法体抛 400。"""
        if self._json is None and self._json_err is None:
            raw = self.body_text()
            if not raw.strip():
                if required:
                    raise HttpError(400, "请求体不能为空（需 JSON）")
                self._json = {}
            else:
                try:
                    obj = json.loads(raw)
                except Exception as e:  # noqa
                    self._json_err = "请求体不是合法 JSON: %s" % e
                    raise HttpError(400, self._json_err)
                if not isinstance(obj, dict):
                    raise HttpError(400, "请求体应为 JSON 对象")
                self._json = obj
        return self._json or {}

    def want(self, *names):
        """按顺序取第一个非空字段（JSON 优先，其次 query）。"""
        j = {}
        try:
            j = self.json(required=False) or {}
        except HttpError:
            j = {}
        for n in names:
            v = j.get(n)
            if v not in (None, "", []):
                return v
        for n in names:
            v = self.q(n, "")
            if v:
                return v
        return None

    # ---- 出参
    def send_json(self, obj, status=200):
        payload = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.handler.send_response(status)
        self.handler.send_header("Content-Type", "application/json; charset=utf-8")
        self.handler.send_header("Content-Length", str(len(payload)))
        self.handler.send_header("Cache-Control", "no-store")
        self.handler.end_headers()
        if not self.head:
            try:
                self.handler.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass
        self.status = status
        self.sent = True

    def send_text(self, text, status=200, content_type="text/plain; charset=utf-8"):
        """纯文本响应（根路径探针用）。

        根路径不再回 JSON 接口清单：公网上一张"接口地图"等于给扫描器递刀，
        清单挪到 POST /v1/routes（见 api/health.py）。
        """
        payload = text.encode("utf-8")
        self.handler.send_response(status)
        self.handler.send_header("Content-Type", content_type)
        self.handler.send_header("Content-Length", str(len(payload)))
        self.handler.send_header("Cache-Control", "no-store")
        self.handler.end_headers()
        if not self.head:
            try:
                self.handler.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass
        self.status = status
        self.sent = True

    def send_error_json(self, status, msg, **extra):
        obj = {"ok": False, "error": _mask(msg), "status": status}
        obj.update(extra)
        self.send_json(obj, status)

    # ---- SSE
    def sse_start(self):
        self.handler.send_response(200)
        self.handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.handler.send_header("Cache-Control", "no-cache, no-transform")
        # SSE 没有 Content-Length，也不能用 chunked（BaseHTTPRequestHandler 不支持）：
        # 只能以“连接关闭”作为响应结束标志，必须显式 Connection: close。
        self.handler.send_header("Connection", "close")
        self.handler.send_header("X-Accel-Buffering", "no")     # nginx 侧不缓冲
        self.handler.end_headers()
        self.handler.close_connection = True
        self.status = 200
        self.sent = True

    def sse_write(self, data):
        """写一段原始 SSE 文本（调用方保证帧格式），立即 flush。"""
        if isinstance(data, str):
            data = data.encode("utf-8")
        try:
            self.handler.wfile.write(data)
            self.handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise ClientGone()
        except Exception:
            raise ClientGone()

    def elapsed_ms(self):
        return int((time.time() - self.started) * 1000)


class HttpError(Exception):
    def __init__(self, status, msg, **extra):
        Exception.__init__(self, msg)
        self.status = status
        self.msg = msg
        self.extra = extra


class ClientGone(Exception):
    """客户端提前断开（SSE 常见，不算错误）。"""


# ====================================================================== 工具
def read_stream_lines(resp, timeout=None):
    """按行迭代上游 SSE（返回 bytes 行）。timeout 为 None 时沿用 socket 超时。"""
    for line in resp:
        yield line


def split_url(base, path):
    return (base.rstrip("/") + "/" + path.lstrip("/")) if base else path


def new_ssl_context(cert, key, purpose=ssl.PROTOCOL_TLS_SERVER):
    ctx = ssl.SSLContext(purpose)
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    except Exception:
        pass
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    return ctx


def bytes_io(data):
    return io.BytesIO(data)
