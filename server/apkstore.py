# -*- coding: utf-8 -*-
"""apkstore.py — APK 版本仓库（人工上传 + 索引）。

存储布局：
    server/apk/versions.json          索引（本模块读写，原子替换）
    server/apk/<file>.apk             APK 文件本体（人工 scp 或 POST /v1/app/upload）

索引结构：
    {"entries": [ {file, size, sha256, md5, version_code, version_name, package,
                   channel, notes, force, min_version_code, published_at}, ... ]}

约定：本项目发 debug 包，默认 channel="debug"；latest = 该 channel 下 version_code 最大者。
"""
import hashlib
import json
import os
import shutil
import time

import axml
import config

FIELDS = ("file", "size", "sha256", "md5", "version_code", "version_name", "package",
          "channel", "notes", "force", "min_version_code", "published_at")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def ensure_dirs():
    os.makedirs(config.APK_DIR, exist_ok=True)


def load_index():
    try:
        with open(config.APK_INDEX, encoding="utf-8") as fh:
            idx = json.load(fh)
        if not isinstance(idx, dict) or not isinstance(idx.get("entries"), list):
            return {"entries": []}
        return idx
    except Exception:
        return {"entries": []}


def save_index(idx):
    ensure_dirs()
    tmp = config.APK_INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(idx, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, config.APK_INDEX)


def _digests(path):
    h256, h5 = hashlib.sha256(), hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h256.update(chunk)
            h5.update(chunk)
    return h256.hexdigest(), h5.hexdigest()


def list_entries(channel=None):
    ents = load_index()["entries"]
    if channel:
        ents = [e for e in ents if (e.get("channel") or config.APK_CHANNEL) == channel]
    return sorted(ents, key=lambda e: int(e.get("version_code") or 0), reverse=True)


def latest(channel=None):
    ents = list_entries(channel)
    return ents[0] if ents else None


def get(filename):
    """按文件名取索引项（只允许纯文件名，防目录穿越）。"""
    name = os.path.basename(filename or "")
    if not name:
        return None
    for e in load_index()["entries"]:
        if e.get("file") == name:
            return e
    return None


def path_of(filename):
    name = os.path.basename(filename or "")
    return os.path.join(config.APK_DIR, name)


def register_file(src, filename=None, version_code=None, version_name=None, channel=None,
                  notes=None, force=None, min_version_code=None, package=None, move=False):
    """把一个 APK 登记进索引。src 可在 APK_DIR 内（直接引用）或外（复制/移动进来）。

    version_code/version_name/package 缺省时从 AXML 自动解析。
    返回索引项 dict。
    """
    ensure_dirs()
    src = os.path.abspath(src)
    if not os.path.isfile(src):
        raise FileNotFoundError("APK 文件不存在: %s" % src)
    name = os.path.basename(filename or os.path.basename(src))
    if not name.lower().endswith(".apk"):
        raise ValueError("文件名必须以 .apk 结尾: %s" % name)
    dst = os.path.join(config.APK_DIR, name)
    if os.path.abspath(src) != dst:
        if os.path.exists(dst):
            os.remove(dst)          # Windows 下 rename/copy2 不能覆盖已存在文件
        if move:
            shutil.move(src, dst)
        else:
            shutil.copy2(src, dst)

    info = axml.read_apk_manifest(dst) or {}
    meta = {
        "file": name,
        "size": os.path.getsize(dst),
        "sha256": "", "md5": "",
        "version_code": int(version_code) if version_code not in (None, "") else info.get("version_code"),
        "version_name": version_name or info.get("version_name") or "",
        "package": package or info.get("package") or "",
        "channel": channel or config.APK_CHANNEL,
        "notes": notes or "",
        "force": bool(force) if force is not None else False,
        "min_version_code": int(min_version_code) if min_version_code not in (None, "") else 0,
        "published_at": _now(),
    }
    if meta["version_code"] is None:
        # AXML 解析失败且没手填 -> 从文件名里猜（app-debug-10.apk / v0.3.0.apk）
        guess = _guess_version(name)
        if guess is not None:
            meta["version_code"] = guess
    if meta["version_code"] is None:
        raise ValueError("无法确定 version_code（AXML 解析失败且未手填 --version-code）")
    meta["sha256"], meta["md5"] = _digests(dst)

    idx = load_index()
    idx["entries"] = [e for e in idx["entries"] if e.get("file") != name]
    idx["entries"].append(meta)
    save_index(idx)
    return meta


def _guess_version(name):
    import re

    m = re.search(r"(\d{1,6})\D*\.apk$", name.lower())
    return int(m.group(1)) if m else None


def remove(filename, delete_file=True):
    name = os.path.basename(filename or "")
    idx = load_index()
    hit = [e for e in idx["entries"] if e.get("file") == name]
    idx["entries"] = [e for e in idx["entries"] if e.get("file") != name]
    save_index(idx)
    if delete_file and hit:
        try:
            os.remove(os.path.join(config.APK_DIR, name))
        except Exception:
            pass
    return bool(hit)


def public_view(entry):
    """给 App 的出参（补上绝对下载地址）。"""
    if not entry:
        return None
    out = {k: entry.get(k) for k in FIELDS}
    out["download_url"] = "%s/v1/app/download?file=%s" % (config.PUBLIC_BASE, entry["file"])
    out["sha256_short"] = (entry.get("sha256") or "")[:16]
    return out
