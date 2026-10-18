# -*- coding: utf-8 -*-
"""audit.py — 统一日志（stdout + 可选文件），密钥脱敏。

日志行格式（便于 grep / journalctl）：
    2026-10-14T17:02:11 INFO  req  POST /v1/infer 200 1234ms ip=1.2.3.4 ua=App/0.2.8
    2026-10-14T17:02:11 WARN  upstream 高德 place/text HTTP 429 ...
"""
import os
import sys
import threading
import time

import config

_lock = threading.Lock()
_fp = None


def init():
    global _fp
    if config.LOG_FILE:
        try:
            d = os.path.dirname(os.path.abspath(config.LOG_FILE))
            if d:
                os.makedirs(d, exist_ok=True)
            _fp = open(config.LOG_FILE, "a", encoding="utf-8", buffering=1)
        except Exception as e:  # noqa
            log("WARN", "log file open failed: %s" % e)


def _mask(text):
    text = str(text)
    for secret in (config.AGENTSVC_TOKEN, config.AMAP_KEY, config.TIANDITU_KEY, config.ADMIN_TOKEN):
        if secret and len(secret) >= 6:
            text = text.replace(secret, secret[:4] + "***" + secret[-2:])
    return text


def log(level, msg):
    line = "%s %-5s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), level, _mask(msg))
    with _lock:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception:
            pass
        if _fp is not None:
            try:
                _fp.write(line + "\n")
            except Exception:
                pass


def info(msg):
    log("INFO", msg)


def warn(msg):
    log("WARN", msg)


def error(msg):
    log("ERROR", msg)


def req_line(method, path, status, elapsed_ms, ip="", ua="", note=""):
    if not config.REQUEST_LOG:
        return
    log("INFO", "req %s %s %s %sms ip=%s ua=%s%s"
        % (method, path, status, elapsed_ms, ip, (ua or "-")[:60], (" " + note) if note else ""))
