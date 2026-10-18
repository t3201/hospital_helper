# -*- coding: utf-8 -*-
"""hhpaths.py — 端上路径/配置引导（P3）。

职责（唯一）：
  * 解析 只读数据根 DATA_DIR（assets 解压到 filesDir/data 后由 Java 传入）
    与 可写产物根 OUT_DIR（filesDir/out）。
  * 把 `slots/`、`ranking/scripts/` 加入 sys.path，使 `import fetch_slots` /
    `import six_fields` / `import score_hospitals` 与旧工程同口径可用。
  * 作为 PyBridge 的薄包装：桌面（无 Chaquopy）下退化为环境变量/本地目录，
    便于离线自检与单元测试。

不写任何业务逻辑；数据只读，产物只写 OUT_DIR（唯一例外见 app/slots.py 注释）。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
PY_DIR = _HERE  # 代码根（部署在 APK assets/python）

__all__ = [
    "PY_DIR", "DATA_DIR", "OUT_DIR", "WS",
    "data", "out", "ensure_out", "config", "bootstrap_paths",
]


def bootstrap_paths():
    """把端上子目录加入 sys.path（幂等），对齐旧工程的裸 import 口径。"""
    for sub in ("slots", os.path.join("ranking", "scripts")):
        p = os.path.join(_HERE, sub)
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)


bootstrap_paths()


# ----------------------------------------------------------------- PyBridge 薄包装
def _pybridge():
    """返回 PyBridge jclass；桌面/无 Chaquopy 时返回 None。"""
    try:
        from java import jclass  # Chaquopy 提供
        return jclass("com.hospitalhelper.controller.PyBridge")
    except Exception:
        return None


def config(key, default=None):
    """读端上配置（SharedPreferences 经 PyBridge.getConfig），桌面退化环境变量。"""
    pb = _pybridge()
    if pb is not None:
        try:
            v = pb.getConfig(key)
            if v:
                return v
        except Exception:
            pass
    v = os.environ.get("HH_" + str(key).upper())
    return v if v else default


def _resolve_dir(dedicated, key, env_key, fallback):
    pb = _pybridge()
    if pb is not None:
        try:
            v = getattr(pb, dedicated)()
            if v:
                return v
        except Exception:
            pass
    v = config(key)
    if v:
        return v
    v = os.environ.get(env_key)
    return v if v else fallback


# 只读数据根（镜像旧工程布局：ssb_dept/ hospital_intel/ slots/ app/ ranking/）
DATA_DIR = _resolve_dir("dataDir", "data_dir", "HH_DATA_DIR",
                        os.path.join(_HERE, "data"))
# 可写产物根
OUT_DIR = _resolve_dir("outDir", "out_dir", "HH_OUT_DIR",
                       os.path.join(_HERE, "out"))
# 旧工程变量名兼容：WS == 工作区根（数据侧）
WS = DATA_DIR


def data(*parts):
    return os.path.join(DATA_DIR, *parts)


def out(*parts):
    return os.path.join(OUT_DIR, *parts)


def ensure_out(*parts):
    p = out(*parts)
    os.makedirs(p, exist_ok=True)
    return p
