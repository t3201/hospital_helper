#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""server.py — hospital_helper 服务端入口（Python 原生 ThreadingHTTPServer，零第三方依赖）。

对外唯一地址：https://hh.ei.ac.cn:51120   （App 内写死同一个值）

    python server.py                     # 默认 0.0.0.0:51120，自动启用 certs/ 下的证书
    python server.py --no-tls            # 本机联调（或前置 nginx 终结 TLS）
    python server.py --port 8080 --no-tls
    python server.py --self-signed       # 证书缺失时自动自签一张（仅联调，App 需信任该证书）

接口一览见 api/ 各模块，或 GET / 服务卡片。
"""
import argparse
import os
import shutil
import ssl
import subprocess
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config                                    # noqa: E402
import apkstore                                  # noqa: E402
import api as api_pkg                            # noqa: E402
from core import audit, router                   # noqa: E402
from core.http_util import (ClientGone, Ctx, HttpError,  # noqa: E402
                            UpstreamError, new_ssl_context)


# ====================================================================== 请求分发
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = config.SERVER_NAME
    sys_version = ""
    timeout = config.INFER_TIMEOUT_S + 30      # 单连接读超时（SSE 长流需要宽裕）

    # 静音 BaseHTTPRequestHandler 自带的 stderr 日志，统一走 audit
    def log_message(self, fmt, *args):
        pass

    # ---- 各方法统一进分发器
    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def _dispatch(self):
        parts = urlsplit(self.path or "/")
        path = parts.path or "/"
        qs = parse_qs(parts.query or "", keep_blank_values=False)
        query = {k: v[0] for k, v in qs.items() if v}
        ip = (self.headers.get("X-Forwarded-For") or self.client_address[0] if self.client_address else "-")
        ip = str(ip).split(",")[0].strip()
        ua = self.headers.get("User-Agent", "")

        fn, params, opts = router.resolve(self.command, path)
        ctx = Ctx(self, self.command, path, query, opts)
        ctx.params = params or {}
        ctx.head = (self.command == "HEAD")
        try:
            if fn is None:
                # 不回接口清单、也不提示去哪看清单：公网扫描只拿到一句"未知接口"
                ctx.send_error_json(404, "未知接口: %s %s" % (self.command, path))
            elif fn == "405":
                ctx.send_error_json(405, "%s 不支持 %s（可用: %s）"
                                    % (path, self.command, "/".join(params)))
            else:
                fn(ctx)
                if not ctx.sent:
                    ctx.send_json({"ok": True}, 200)
        except HttpError as e:
            if not ctx.sent:
                ctx.send_error_json(e.status, e.msg, **e.extra)
            else:
                self.close_connection = True
                audit.error("%s %s -> 响应已发出后报错: %s" % (self.command, path, e.msg))
        except UpstreamError as e:
            if not ctx.sent:
                ctx.send_error_json(502, e.msg, upstream_status=e.upstream_status)
            else:
                self.close_connection = True
        except ClientGone:
            audit.warn("client gone: %s %s" % (self.command, path))
            self.close_connection = True
        except BrokenPipeError:
            self.close_connection = True
        except Exception as e:  # noqa
            tb = traceback.format_exc()
            audit.error("unhandled %s %s: %s\n%s" % (self.command, path, e, tb.strip()))
            if not ctx.sent:
                try:
                    ctx.send_error_json(500, "服务端内部错误: %s: %s" % (type(e).__name__, e))
                except Exception:
                    self.close_connection = True
            else:
                self.close_connection = True
        finally:
            audit.req_line(self.command, path, ctx.status, ctx.elapsed_ms(), ip=ip, ua=ua)

    # 大文件上传时 curl 会发 Expect: 100-continue
    def handle_expect_100(self):
        self.send_response(100)
        self.end_headers()
        return True


# ====================================================================== TLS
def ensure_tls(args):
    """返回 ssl.SSLContext 或 None（None = 明文 http）。"""
    cert, key = args.tls_cert, args.tls_key
    if args.no_tls:
        return None
    if not (os.path.isfile(cert) and os.path.isfile(key)):
        if args.self_signed:
            if _make_self_signed(cert, key):
                audit.warn("已生成自签证书（仅联调用；正式环境请换 hh.ei.ac.cn 的真实证书）: %s" % cert)
            else:
                audit.error("自签证书生成失败，降级为 http")
                return None
        else:
            audit.warn("未找到证书（%s / %s）-> 以 http 启动。"
                       "正式部署请把 hh.ei.ac.cn 的证书放到该路径，或用 nginx 终结 TLS 后反代本服务。"
                       % (cert, key))
            return None
    try:
        return new_ssl_context(cert, key)
    except Exception as e:  # noqa
        audit.error("加载证书失败: %s: %s（降级为 http）" % (type(e).__name__, e))
        return None


def _make_self_signed(cert, key):
    """用 openssl 自签一张（含 hh.ei.ac.cn 的 SAN），仅用于联调。"""
    if not shutil.which("openssl"):
        audit.error("系统无 openssl，无法自签证书")
        return False
    os.makedirs(os.path.dirname(os.path.abspath(cert)), exist_ok=True)
    cmd = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
           "-keyout", key, "-out", cert, "-days", "3650",
           "-subj", "/CN=%s" % config.PUBLIC_HOST,
           "-addext", "subjectAltName=DNS:%s,DNS:localhost,IP:127.0.0.1" % config.PUBLIC_HOST]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:  # noqa
        audit.error("openssl 失败: %s" % e)
        return False


# ====================================================================== main
def main(argv=None):
    ap = argparse.ArgumentParser(description="hh-server（mobile 接口归集）")
    ap.add_argument("--host", default=config.LISTEN_HOST)
    ap.add_argument("--port", type=int, default=config.LISTEN_PORT)
    ap.add_argument("--tls-cert", default=config.TLS_CERT)
    ap.add_argument("--tls-key", default=config.TLS_KEY)
    ap.add_argument("--no-tls", action="store_true", help="明文 http（本机联调 / 前置 nginx）")
    ap.add_argument("--self-signed", action="store_true", default=config.TLS_AUTO_SELF_SIGNED,
                    help="证书缺失时自动 openssl 自签（仅联调）")
    ap.add_argument("--print-config", action="store_true", help="打印生效配置后退出")
    args = ap.parse_args(argv)

    audit.init()
    api_pkg.register()
    apkstore.ensure_dirs()

    if args.print_config:
        import json
        keys = ("PUBLIC_BASE", "LISTEN_HOST", "LISTEN_PORT", "AGENTSVC_BASE", "AGENT_IDS",
                "DEFAULT_MODEL_ID", "GEO_PROVIDER", "GEO_OUTPUT_CRS", "APK_DIR", "APK_CHANNEL",
                "TLS_CERT", "TLS_KEY")
        out = {k: (list(getattr(config, k)) if isinstance(getattr(config, k), tuple)
                   else getattr(config, k)) for k in keys}
        out["env_files"] = [s["path"] for s in config.ENV_SOURCES]
        out["secrets"] = {k: {"required": v["required"], "configured": v["configured"],
                              "env": v["env"], "masked": v["masked"]}
                          for k, v in config.secret_status().items()}
        out["missing_required"] = config.missing_secrets()
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    ctx = ensure_tls(args)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    if ctx is not None:
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    scheme = "https" if ctx is not None else "http"

    audit.info("=" * 72)
    audit.info("hh-server 启动: %s://%s:%d   （对外发布: %s）"
               % (scheme, args.host, args.port, config.PUBLIC_BASE))
    audit.info("  上游 Agent Service: %s  agent_ids=%s  token=%s"
               % (config.AGENTSVC_BASE, ",".join(config.AGENT_IDS),
                  "已配置" if config.AGENTSVC_TOKEN else "未配置(!)"))
    audit.info("  地理位置 provider : %s  出参坐标系=%s  key=%s"
               % (config.GEO_PROVIDER, config.GEO_OUTPUT_CRS,
                  "已配置" if (config.AMAP_KEY or config.TIANDITU_KEY) else "未配置"))
    audit.info("  APK 仓库          : %s  channel=%s  发版口令=%s"
               % (config.APK_DIR, config.APK_CHANNEL,
                  "已配置" if config.ADMIN_TOKEN else "未配置（上传/下架接口关闭）"))
    audit.info("  配置文件          : %s"
               % (", ".join(s["path"] for s in config.ENV_SOURCES) or "（未找到 .env，仅用环境变量/默认值）"))
    audit.info("  路由              : %d 条（GET / 看清单）" % len(router.ROUTES))
    audit.info("=" * 72)
    for name in config.missing_secrets():
        audit.warn("缺少必需密钥 %s：相关接口会返回 503。请在 server/.env（可从 .env.example 复制）里填 %s。"
                   % (name, config.secret_status()[name]["env"]))
    if not config.ADMIN_TOKEN:
        audit.warn("未配置 HH_ADMIN_TOKEN：POST /v1/app/upload、/v1/app/remove 已关闭；"
                   "需要发版时在 server/.env 里填随机口令。")
    if ctx is None:
        audit.warn("当前以 http 明文提供服务，仅适合本机联调或前置 nginx 终结 TLS；"
                   "对外 https://hh.ei.ac.cn:%d 需要 certs/ 下的证书。" % config.PUBLIC_PORT)

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        audit.info("收到 Ctrl-C，正在关闭…")
    finally:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
