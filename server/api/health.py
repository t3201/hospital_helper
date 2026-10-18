# -*- coding: utf-8 -*-
"""api/health.py — 服务卡片 / 健康检查 / 路由自描述。"""
import socket
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

import apkstore
import config
from core.router import describe, route

_STARTED = time.time()
VERSION = "1.0"


def _tcp_ok(host, port, timeout=1.0):
    if not host:
        return False
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def _upstream_probe(base, path, timeout=2.5):
    """真探测上游：GET {base}{path}。

    只要拿到 HTTP 状态码（哪怕 401/404），就说明这个端口上确实是 HTTP 服务；
    拿不到状态码时给出 hint，用来区分"端口写错 / 那是 https 端口 / 证书不对 / 被别的服务占着"。
    """
    url = base.rstrip("/") + path
    ctx = None
    if url.startswith("https:"):
        ctx = ssl.create_default_context()
        if getattr(config, "AGENTSVC_INSECURE_TLS", False):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": config.SERVER_NAME}, method="GET")
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            resp.read(64)
            return {"http_status": resp.status, "ms": int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        return {"http_status": e.code, "ms": int((time.time() - t0) * 1000)}
    except Exception as e:  # noqa
        exc = e.reason if isinstance(e, urllib.error.URLError) else e
        hint = None
        if isinstance(exc, ssl.SSLCertVerificationError) or "certificate" in str(exc).lower():
            hint = "上游是 https 但证书校验失败：确认域名与证书一致，或设 HH_AGENTSVC_INSECURE_TLS=1"
        elif isinstance(exc, ssl.SSLError):
            hint = "上游像是 https 端口但 TLS 握手失败"
        elif isinstance(exc, (ConnectionResetError, BrokenPipeError)) or "reset" in str(exc).lower():
            hint = "端口通但连接被重置：多半端口写错、那是个 https 端口、或被别的服务占着"
        elif isinstance(exc, (ConnectionRefusedError, socket.timeout, TimeoutError)) or "refused" in str(exc).lower():
            hint = "端口没监听或无响应：确认 Agent Service 是否在跑、端口对不对"
        return {"http_status": None, "ms": int((time.time() - t0) * 1000),
                "error": "%s: %s" % (type(exc).__name__, exc), "hint": hint}


@route("GET", r"^/healthz/?$")
@route("GET", r"^/health/?$")
def healthz(ctx):
    """健康检查：上游 agentsvc 连通性、geo provider/key、APK 最新版本、密钥是否已配置（不含明文）。

    这是排查用的**兜底接口**，任何情况下都得给出可读结果，不能 500。
    2026-10-14 实机踩过一次：线上只同步了 api/health.py、config.py 还是旧的，
    于是 /healthz 直接 `AttributeError: module 'config' has no attribute 'AGENTSVC_PROBE_PATH'`，
    最需要看健康卡片的时候反而看不到。所以这里 getattr 兜底 + 整体 try/except。
    """
    try:
        return _healthz(ctx)
    except Exception as e:  # noqa
        ctx.send_json({
            "ok": False,
            "service": "hh-server",
            "version": VERSION,
            "error": "%s: %s" % (type(e).__name__, e),
            "hint": "健康检查自身出错（多半是服务端代码只同步了一半：config.py 与 api/ "
                    "版本不匹配）。把 server/ 整个同步过去再重启即可。",
        }, 200)
        return None


def _healthz(ctx):
    probe_path = getattr(config, "AGENTSVC_PROBE_PATH", "/v1/models")
    u = urlparse(config.AGENTSVC_BASE)
    tcp = _tcp_ok(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    probe = (_upstream_probe(config.AGENTSVC_BASE, probe_path) if tcp
             else {"http_status": None, "ms": 0, "error": "tcp connect failed",
                   "hint": "端口没监听：确认 Agent Service 是否在跑、端口对不对"})
    agentsvc = {"base": config.AGENTSVC_BASE, "tcp": tcp,
                "http_status": probe.get("http_status"),      # 非 None = 这个端口上确实是 HTTP 服务
                "probe_path": probe_path, "probe_ms": probe.get("ms"),
                "agent_ids": list(config.AGENT_IDS)}
    if probe.get("error"):
        agentsvc["probe_error"] = probe["error"]
    if probe.get("hint"):
        agentsvc["hint"] = probe["hint"]
    upstream_ok = probe.get("http_status") is not None
    latest = apkstore.latest() or {}
    # 这几项依赖较新的 config.py（secret_status / missing_secrets / ENV_SOURCES）。
    # 线上只同步了一半代码时，宁可少给这几块，也别把整张健康卡片弄没了。
    try:
        secrets = {k: {"required": v["required"], "configured": v["configured"], "env": v["env"]}
                   for k, v in config.secret_status().items()}
        missing = config.missing_secrets()
        env_files = [s["path"] for s in config.ENV_SOURCES]
        admin_api = bool(config.ADMIN_TOKEN)
    except AttributeError:
        secrets, missing, env_files, admin_api = {}, [], [], None
    warnings = (["未配置必需密钥: %s" % ", ".join(missing)] if missing else [])
    if not upstream_ok:
        warnings.append("上游 Agent Service 不可用: %s（base=%s）"
                        % (probe.get("hint") or probe.get("error"), config.AGENTSVC_BASE))
    ctx.send_json({
        "ok": True,
        "service": "hh-server",
        "version": VERSION,
        "uptime_s": int(time.time() - _STARTED),
        "public_base": config.PUBLIC_BASE,
        "tls": ctx.is_tls(),
        "upstream_ok": upstream_ok,
        "config": {
            "env_files": env_files,
            "secrets": secrets,               # 只报是否配置，绝不回显明文
            "missing_required": missing,
            "admin_api_enabled": admin_api,
        },
        "warnings": warnings,
        "checks": {
            "agentsvc": agentsvc,
            "geo": {"provider": config.GEO_PROVIDER,
                    "key_configured": bool(config.AMAP_KEY or config.TIANDITU_KEY)},
            "apk": {"dir": config.APK_DIR,
                    "channel": config.APK_CHANNEL,
                    "count": len(apkstore.list_entries()),
                    "latest": {"file": latest.get("file"),
                               "version_code": latest.get("version_code"),
                               "version_name": latest.get("version_name")} if latest else None},
        },
    }, 200)
    return None


@route("POST", r"^/v1/routes/?$")
def routes(ctx):
    """全部路由自描述（机器可读，新增接口自动出现）。

    2026-10-14：只开 POST。清单本身不含密钥，但把"有哪些接口、路径正则、handler 名"
    挂在可被浏览器/扫描器随手 GET 的位置，等于公开一张攻击地图。
    自查用：curl -X POST https://hh.ei.ac.cn:51120/v1/routes
    """
    ctx.send_json({"ok": True, "routes": describe()}, 200)


@route("GET,HEAD", r"^/?$")
def index(ctx):
    """根路径只回一行 running —— 运维肉眼确认服务活着即可，不透露任何接口信息。"""
    ctx.send_text("hh-server running\n", 200)
    return None
