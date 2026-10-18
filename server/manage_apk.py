#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""manage_apk.py — APK 仓库命令行（人工上传的第二条路径）。

    python manage_apk.py add app-debug.apk --notes "修 xxx" [--version-code 10 --version-name 0.3.0]
    python manage_apk.py list
    python manage_apk.py latest
    python manage_apk.py rm app-debug.apk
    python manage_apk.py probe app-debug.apk      # 看 AXML 解析出的包名/版本

与 HTTP 上传（POST /v1/app/upload）写同一份 server/apk/versions.json，二者等价。
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import apkstore                                    # noqa: E402
import axml                                        # noqa: E402
import config                                      # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="APK 版本仓库管理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="登记一个 APK 文件")
    p_add.add_argument("path")
    p_add.add_argument("--as", dest="as_name", default=None, help="入库文件名（默认同名）")
    p_add.add_argument("--version-code", type=int, default=None)
    p_add.add_argument("--version-name", default=None)
    p_add.add_argument("--channel", default=None)
    p_add.add_argument("--notes", default=None)
    p_add.add_argument("--force", action="store_true", help="标记为强制更新")
    p_add.add_argument("--min-version-code", type=int, default=None,
                       help="低于此 version_code 的客户端必须升级")
    p_add.add_argument("--move", action="store_true", help="移动而非复制")

    p_ls = sub.add_parser("list", help="列出全部版本")
    p_ls.add_argument("--channel", default=None)

    p_latest = sub.add_parser("latest", help="显示最新版本")
    p_latest.add_argument("--channel", default=None)

    p_rm = sub.add_parser("rm", help="下架并删除文件")
    p_rm.add_argument("file")

    sub.add_parser("path", help="显示仓库路径")

    p_probe = sub.add_parser("probe", help="解析 APK 的 AndroidManifest（不入库）")
    p_probe.add_argument("path")

    args = ap.parse_args(argv)

    if args.cmd == "add":
        meta = apkstore.register_file(
            args.path, filename=args.as_name, version_code=args.version_code,
            version_name=args.version_name, channel=args.channel, notes=args.notes,
            force=True if args.force else None, min_version_code=args.min_version_code,
            move=args.move)
        print(json.dumps(apkstore.public_view(meta), ensure_ascii=False, indent=2))
        latest = apkstore.latest(meta["channel"])
        print("latest[%s] = %s (versionCode=%s)"
              % (meta["channel"], latest["file"], latest["version_code"]))
        return 0
    if args.cmd == "list":
        ents = apkstore.list_entries(args.channel)
        if not ents:
            print("(空) 仓库: %s" % config.APK_DIR)
            return 0
        for e in ents:
            print("%-34s v%-4s %-16s %10d B  %s  %s"
                  % (e["file"], e.get("version_code"), e.get("version_name"), e.get("size"),
                     e.get("published_at"), (e.get("notes") or "")[:40]))
        return 0
    if args.cmd == "latest":
        e = apkstore.latest(args.channel)
        print(json.dumps(apkstore.public_view(e), ensure_ascii=False, indent=2) if e else "(无)")
        return 0
    if args.cmd == "rm":
        ok = apkstore.remove(args.file)
        print("已下架: %s" % args.file if ok else "索引里没有: %s" % args.file)
        return 0 if ok else 1
    if args.cmd == "path":
        print(config.APK_DIR)
        print(config.APK_INDEX)
        return 0
    if args.cmd == "probe":
        p = os.path.abspath(args.path)
        print("file=%s size=%d" % (p, os.path.getsize(p)))
        print(json.dumps(axml.read_apk_manifest(p), ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
