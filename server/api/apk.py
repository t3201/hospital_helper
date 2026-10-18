# -*- coding: utf-8 -*-
"""api/apk.py — APK 自更新接口（人工上传，App 侧检查更新 + 下载安装）。

    GET  /v1/app/version[?channel=debug&current_version_code=9]
         -> {ok, latest:{version_code, version_name, size, sha256, download_url, notes, force},
             has_update:bool, current_version_code:..}
    GET  /v1/app/download?file=xxx.apk        （支持 Range 断点续传；也支持 /v1/app/download/<name>）
    GET  /v1/app/list[?channel=debug]         全部历史版本（运维/自测）
    POST|PUT /v1/app/upload                   人工上传（需 X-Admin-Token: <config.ADMIN_TOKEN>）
         curl -T app-debug.apk -H 'X-Admin-Token: ...' \
              'https://hh.ei.ac.cn:51120/v1/app/upload?filename=app-debug-0.3.0.apk&notes=修bug'
         version_code/version_name/package 缺省时自动从 APK 的 AndroidManifest.xml 解析。
    DELETE /v1/app/remove?file=xxx.apk        下架（需 X-Admin-Token）

说明：APK 也可以人工 scp 到 server/apk/ 后用 `python manage_apk.py add <file>` 登记，
      两条路径等价（都写同一份 versions.json）。
"""
import json
import os
import re
import tempfile

import apkstore
import config
from core import audit
from core.http_util import HttpError
from core.router import route

_CHUNK = 1 << 20
_NAME_RE = re.compile(r"^[A-Za-z0-9._+-]+\.apk$")


def _require_admin(ctx):
    if not config.ADMIN_TOKEN:
        # 安全默认：没配口令就把写接口关掉（而不是允许空口令）
        raise HttpError(503, "服务端未配置发版口令，上传/下架接口已关闭"
                             "（请在 server/.env 里填 HH_ADMIN_TOKEN）")
    tok = ctx.header("X-Admin-Token") or ctx.header("Authorization", "").replace("Bearer ", "")
    if not tok:
        tok = ctx.q("token", "")
    if tok != config.ADMIN_TOKEN:
        raise HttpError(401, "缺少或错误的 X-Admin-Token（APK 上传/下架需要管理口令）")


def _safe_name(ctx, params):
    name = (params.get("file") or "").strip()
    if not name:
        name = (ctx.q("file") or ctx.q("filename") or ctx.header("X-File-Name") or "").strip()
    name = os.path.basename(name)
    if not _NAME_RE.match(name):
        raise HttpError(400, "非法文件名（只允许 [A-Za-z0-9._+-].apk）: %r" % name)
    return name


# ====================================================================== 版本查询
@route("GET", r"^/v1/app/version/?$")
@route("GET", r"^/v1/app/update-check/?$")
def app_version(ctx):
    """APK 最新版本 / 是否有更新（App 启动自检调用，current_version_code 决定 has_update）。"""
    channel = ctx.q("channel", config.APK_CHANNEL)
    entry = apkstore.latest(channel)
    cur = None
    try:
        cur = int(ctx.q("current_version_code", ""))
    except (TypeError, ValueError):
        cur = None
    if entry is None:
        ctx.send_json({"ok": False, "error": "尚未上传任何 APK（server/apk/versions.json 为空）",
                       "channel": channel, "latest": None,
                       "has_update": False, "current_version_code": cur}, 404)
        return None
    out = apkstore.public_view(entry)
    has_update = (cur is None) or (int(entry.get("version_code") or 0) > cur)
    force = bool(entry.get("force")) or (
        cur is not None and int(entry.get("min_version_code") or 0) > cur)
    ctx.send_json({"ok": True, "channel": channel, "latest": out,
                   "has_update": has_update, "force_update": force,
                   "current_version_code": cur}, 200)
    return None


@route("GET", r"^/v1/app/list/?$")
def app_list(ctx):
    """APK 历史版本列表（运维/自查）。"""
    channel = ctx.q("channel", "")
    ents = [apkstore.public_view(e) for e in apkstore.list_entries(channel or None)]
    ctx.send_json({"ok": True, "count": len(ents), "channel": channel or config.APK_CHANNEL,
                   "entries": ents}, 200)


# ====================================================================== 下载
@route("GET,HEAD", r"^/v1/app/download/?$")
@route("GET,HEAD", r"^/v1/app/download/(?P<file>[A-Za-z0-9._%+-]+\.apk)$")
def app_download(ctx, params=None):
    """下载 APK（支持 Range 断点续传；App 侧下载后按 sha256 校验）。"""
    params = params or ctx.params or {}
    name = _safe_name(ctx, params)
    entry = apkstore.get(name)
    path = apkstore.path_of(name)
    if entry is None and not os.path.isfile(path):
        ctx.send_error_json(404, "APK 不存在: %s（请先上传/登记）" % name)
        return None
    size = os.path.getsize(path)
    start, end = 0, size - 1
    status = 200
    rng = ctx.header("Range", "")
    m = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip()) if rng else None
    if m:
        a, b = m.group(1), m.group(2)
        if a:
            start = int(a)
            end = int(b) if b else size - 1
        elif b:
            start = max(0, size - int(b))
            end = size - 1
        if start >= size or end >= size or start > end:
            ctx.handler.send_response(416)
            ctx.handler.send_header("Content-Range", "bytes */%d" % size)
            ctx.handler.send_header("Content-Length", "0")
            ctx.handler.end_headers()
            ctx.sent = True
            ctx.status = 416
            return None
        status = 206

    h = ctx.handler
    h.send_response(status)
    h.send_header("Content-Type", "application/vnd.android.package-archive")
    h.send_header("Content-Length", str(end - start + 1))
    h.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
    h.send_header("Accept-Ranges", "bytes")
    if status == 206:
        h.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
    if entry:
        h.send_header("X-Sha256", entry.get("sha256", ""))
        h.send_header("X-Version-Code", str(entry.get("version_code", "")))
        h.send_header("X-Version-Name", str(entry.get("version_name", "")))
    h.end_headers()
    ctx.sent = True
    ctx.status = status
    sent = 0
    if ctx.head:
        ctx.status = status
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                buf = fh.read(min(_CHUNK, remaining))
                if not buf:
                    break
                h.wfile.write(buf)
                remaining -= len(buf)
                sent += len(buf)
    except (BrokenPipeError, ConnectionResetError):
        audit.warn("apk download aborted by client: %s (%d/%d bytes)" % (name, sent, size))
        return None
    except Exception as e:  # noqa
        audit.error("apk download failed: %s: %s" % (name, e))
        return None
    audit.info("apk served: %s %d bytes (range %d-%d)" % (name, sent, start, end))
    return None


# ====================================================================== 上传 / 下架
@route("POST,PUT", r"^/v1/app/upload/?$")
def app_upload(ctx):
    """人工上传 APK（需 X-Admin-Token；version_code 等缺省时从 AndroidManifest 解析）。"""
    _require_admin(ctx)
    limit = (ctx.route_spec or {}).get("body_limit") or config.APK_MAX_BYTES
    raw = ctx.body_bytes(limit)
    if not raw:
        raise HttpError(400, "请求体为空（请用 curl -T 直接 PUT APK 二进制）")
    if len(raw) < 4 or raw[:2] != b"PK":
        raise HttpError(400, "请求体不是 APK/ZIP（头 2 字节应为 PK）")
    name = ""
    try:
        name = _safe_name(ctx, ctx.params or {})
    except HttpError:
        name = ""
    if not name:
        ct = ctx.header("Content-Type", "")
        m = re.search(r"filename=([^;]+)", ct)
        cand = os.path.basename((m.group(1) if m else "").strip().strip('"'))
        name = cand if _NAME_RE.match(cand) else "app-upload-%d.apk" % int(ctx.started)

    apkstore.ensure_dirs()
    fd, tmp = tempfile.mkstemp(suffix=".apk", dir=config.APK_DIR)
    fv = ctx.q("force", "").strip().lower()
    force = True if fv in ("1", "true", "yes", "on") else (False if fv else None)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        meta = apkstore.register_file(
            tmp,
            filename=name,
            version_code=ctx.q("version_code", ""),
            version_name=ctx.q("version_name", ""),
            channel=ctx.q("channel", ""),
            notes=ctx.q("notes", ""),
            force=force,
            min_version_code=ctx.q("min_version_code", ""),
            package=ctx.q("package", ""),
            move=True,
        )
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
    audit.info("apk uploaded: %s v%s(%s) %d bytes"
               % (meta["file"], meta["version_name"], meta["version_code"], meta["size"]))
    ctx.send_json({"ok": True, "entry": apkstore.public_view(meta),
                   "latest": apkstore.public_view(apkstore.latest(meta["channel"]))}, 200)
    return None


@route("POST,DELETE", r"^/v1/app/remove/?$")
def app_remove(ctx):
    """下架 APK（需 X-Admin-Token）。"""
    _require_admin(ctx)
    name = _safe_name(ctx, ctx.params or {})
    ok = apkstore.remove(name)
    if not ok:
        ctx.send_error_json(404, "索引里没有该 APK: %s" % name)
        return None
    audit.warn("apk removed from index: %s" % name)
    ctx.send_json({"ok": True, "removed": name,
                   "latest": apkstore.public_view(apkstore.latest())}, 200)
    return None
