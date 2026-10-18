# -*- coding: utf-8 -*-
"""check_secrets.py — 开源前自查：确认 .env 里的密钥没有出现在要提交的代码里。

用法（在 server/ 目录或仓库根都行）：
    python check_secrets.py            # 扫全仓，命中则退出码 1
    python check_secrets.py --show-keys

做法：把 .env（server/.env、<repo>/.env、hospital_intel/.env）里每个**值**当作
明文 pattern，在文本源码里搜一遍。命中就打印 文件:行号（值本身脱敏显示）。
本脚本只读，不改任何文件；输出里也不会回显完整密钥。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

ENV_FILES = [
    os.path.join(HERE, ".env"),
    os.path.join(REPO, ".env"),
    os.path.join(REPO, "hospital_intel", ".env"),
]

SKIP_DIRS = {".git", ".gradle", "build", "__pycache__", ".idea", "node_modules",
             "dist", ".venv", "venv", "apk", "cache", "certs", "tmp"}
EXTS = {".py", ".java", ".kt", ".xml", ".gradle", ".md", ".json", ".txt", ".yml",
        ".yaml", ".properties", ".html", ".js", ".ts", ".sh", ".bat", ".cfg", ".ini",
        ".example", ".env"}
MIN_LEN = 8


def _is_secret_key(key):
    """只把 TOKEN/KEY/SECRET/PASSWORD 之类的键当密钥；AGENT_IDS=hospital_helper 这种是普通配置。"""
    k = key.upper()
    if k.endswith("_ENV_FILE") or k == "HH_ENV_FILE":
        return False
    return any(t in k for t in ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH"))


def _parse(path):
    out = {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("'\"")
                if v:
                    out[k] = v
    except Exception:
        pass
    return out


def main():
    show_keys = "--show-keys" in sys.argv
    secrets = {}
    for path in ENV_FILES:
        for k, v in _parse(path).items():
            if len(v) >= MIN_LEN and _is_secret_key(k):
                secrets.setdefault(v, set()).add(os.path.relpath(path, REPO) + ":" + k)
    if not secrets:
        print("没找到任何 .env 密钥（跳过扫描）")
        return 0

    print("待查密钥 %d 个（来自 %s）" % (len(secrets), ", ".join(
        os.path.relpath(p, REPO) for p in ENV_FILES if os.path.isfile(p))))
    if show_keys:
        for v, src in secrets.items():
            print("  %s***  <- %s" % (v[:4], ", ".join(sorted(src))))

    hits = []
    scanned = 0
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".git")]
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in EXTS:
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, REPO)
            if os.path.basename(path).startswith(".env") and os.path.dirname(path) != REPO:
                pass  # .env 本身也扫（下面排除真正的 .env 文件）
            if os.path.basename(path) == ".env" or os.path.basename(path).startswith(".env."):
                continue          # 密钥源文件本身不算命中
            try:
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
            except Exception:
                continue
            scanned += 1
            for v, src in secrets.items():
                if v in text:
                    line_no = text[:text.index(v)].count("\n") + 1
                    hits.append((rel, line_no, v[:4], "; ".join(sorted(src))))

    print("已扫描文本文件 %d 个" % scanned)
    if not hits:
        print("OK：源码里没有 .env 密钥明文，可以开源。")
        return 0
    print("!! 命中 %d 处（开源前必须清掉）：" % len(hits))
    for rel, line_no, head, src in hits:
        print("   %s:%d  (%s***  来源 %s)" % (rel, line_no, head, src))
    return 1


if __name__ == "__main__":
    sys.exit(main())
