# -*- coding: utf-8 -*-
"""config.py — 服务端集中配置（**不含任何密钥**）。

设计原则（2026-10-14 归集移动端接口；同日把密钥移出代码）：
  * **对外地址写死**：https://hh.ei.ac.cn:51120（本服务唯一公网入口，App 内也写死同一地址）。
  * **密钥不进代码**：Agent Service 的 Bearer token、高德/天地图 key、APK 上传口令
    一律从 `.env` 文件或进程环境变量读取，代码里默认值为空。本目录准备开源，
    因此 `server/.env` 必须在 .gitignore 内；仓库里只提交 `server/.env.example`。
  * 读取优先级：**进程环境变量 > 先出现的 .env 文件**。所有配置项都可用 HH_* 环境变量覆盖。

.env 文件位置（按顺序查找，先命中者优先；也可用 HH_ENV_FILE 指定单个文件）：
    1) $HH_ENV_FILE
    2) server/.env                 <- 推荐：与代码同目录，部署时只拷这一个文件
    3) <repo>/.env                 <- 仓库根，便于和本地其它脚本共用
    4) <repo>/hospital_intel/.env  <- 仅作 AMAP_KEY / TIANDITU_KEY 的开发期兜底（只取这两个键）

.env 写法（KEY=VALUE，# 注释，允许 export 前缀与引号；HH_ 前缀可省略）：
    HH_AGENTSVC_TOKEN=as_xxxx
    HH_AMAP_KEY=xxxxxxxx
    HH_ADMIN_TOKEN=换成随机串

字段命名约定：HH_ 前缀 = 环境变量/.env 覆盖名。
"""
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)


# ====================================================================== .env 装载
def _parse_env_file(path):
    """极简 dotenv 解析（标准库实现，不引第三方依赖）。"""
    out = {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key, val = key.strip(), val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                    out[key] = val
    except Exception:
        return {}
    return out


def _env_file_candidates():
    picked = (os.environ.get("HH_ENV_FILE") or "").strip()
    if picked:
        return [os.path.abspath(picked)]
    return [
        os.path.join(_HERE, ".env"),
        os.path.join(_REPO, ".env"),
    ]


# 开发期兜底：只从 hospital_intel/.env 里取地理 key（该文件本身也被 git 忽略）
_KEY_ONLY_FILES = [(os.path.join(_REPO, "hospital_intel", ".env"),
                    ("AMAP_KEY", "TIANDITU_KEY"))]

ENV_SOURCES = []      # [{path, keys}] 实际生效的 .env 文件（只记路径与键名，不记值）


def _load_dotenv():
    """把 .env 内容并入 os.environ（进程已有的环境变量优先，不覆盖）。"""
    sources = []
    for path in _env_file_candidates():
        if not os.path.isfile(path):
            continue
        data = _parse_env_file(path)
        if not data:
            continue
        hit = []
        for key, val in data.items():
            names = [key] if key.startswith("HH_") else ["HH_" + key, key]
            for name in names:
                if name not in os.environ:
                    os.environ[name] = val
                    hit.append(name)
        if hit:
            sources.append({"path": path, "keys": sorted(set(hit))})

    for path, wanted in _KEY_ONLY_FILES:
        if not os.path.isfile(path):
            continue
        data = _parse_env_file(path)
        hit = []
        for key in wanted:
            val = data.get("HH_" + key) or data.get(key)
            if not val:
                continue
            for name in ("HH_" + key, key):
                if name not in os.environ:
                    os.environ[name] = val
                    hit.append(name)
        if hit:
            sources.append({"path": path, "keys": sorted(set(hit)), "partial": True})
    return sources


ENV_SOURCES = _load_dotenv()


# ====================================================================== 读取工具
def _env(name, default):
    v = (os.environ.get("HH_" + name) or "").strip()
    return v if v else default


def _env_int(name, default):
    try:
        return int(_env(name, default))
    except (TypeError, ValueError):
        return int(default)


def _env_bool(name, default):
    v = _env(name, "" if default is None else str(default))
    if v == "":
        return bool(default)
    return v.lower() in ("1", "true", "yes", "on")


def _mask(v, head=4, tail=4):
    v = v or ""
    if not v:
        return ""
    if len(v) <= head + tail:
        return "*" * len(v)
    return "%s***%s" % (v[:head], v[-tail:])


# ====================================================================== 对外
PUBLIC_HOST = "hh.ei.ac.cn"
PUBLIC_PORT = 51120
PUBLIC_BASE = "https://%s:%d" % (PUBLIC_HOST, PUBLIC_PORT)   # App 内写死的同一个值

LISTEN_HOST = _env("HOST", "0.0.0.0")
LISTEN_PORT = _env_int("PORT", PUBLIC_PORT)

# TLS：本服务直接对公网发布 https，因此默认自带证书。
#   证书缺失时自动降级为 http（便于本机联调 / 前置 nginx 终结 TLS），并在启动日志里告警。
TLS_CERT = _env("TLS_CERT", os.path.join(_HERE, "certs", "hh.ei.ac.cn.pem"))
TLS_KEY = _env("TLS_KEY", os.path.join(_HERE, "certs", "hh.ei.ac.cn.key"))
TLS_AUTO_SELF_SIGNED = _env_bool("TLS_SELF_SIGNED", False)   # 证书缺失时自签一张（仅联调）

# ====================================================================== 上游：大模型
# Agent Service（本机部署；App 侧不再知道它的地址与 token）
AGENTSVC_BASE = _env("AGENTSVC_BASE", "http://localhost:7988")
AGENTSVC_TOKEN = _env("AGENTSVC_TOKEN", "")        # <- .env: HH_AGENTSVC_TOKEN（必填）
# 指定 agent（多 agent 时按顺序取第一个）；App 侧不允许覆盖，防止越权调用其它 agent。
AGENT_IDS = tuple(_env("AGENT_IDS", "hospital_helper").split(","))
INFER_STREAM_PATH = _env("AGENTSVC_INFER_PATH", "/v1/infer/stream")
INFER_TIMEOUT_S = _env_int("INFER_TIMEOUT", 300)          # 与 App 侧总超时对齐
INFER_CONNECT_TIMEOUT_S = _env_int("INFER_CONNECT_TIMEOUT", 10)
# 上游是 https 且自签证书时（本机部署常见），设 HH_AGENTSVC_INSECURE_TLS=1 跳过证书校验。
AGENTSVC_INSECURE_TLS = _env_bool("AGENTSVC_INSECURE_TLS", False)
# /healthz 探上游用的只读路径：只要拿到 HTTP 状态码，就说明这个端口上确实是 HTTP 服务
# （端口通但连接被 reset，多半是端口写错 / 那是个 https 端口 / 被别的服务占着）。
AGENTSVC_PROBE_PATH = _env("AGENTSVC_PROBE_PATH", "/v1/models")
# model_id：Agent Service 侧的**标签**（不是真实模型 id），默认 'default' 由服务端解析。
DEFAULT_MODEL_ID = _env("DEFAULT_MODEL_ID", "default")

# ====================================================================== 上游：地理位置
# 内部实现可切换：amap（高德，当前）/ tianditu（天地图，预留）。见 geo/__init__.py。
GEO_PROVIDER = _env("GEO_PROVIDER", "amap")
GEO_OUTPUT_CRS = _env("GEO_CRS", "GCJ-02")   # 出参坐标系；天地图原始为 CGCS2000，会自动换算
AMAP_KEY = _env("AMAP_KEY", "")              # <- .env: HH_AMAP_KEY
TIANDITU_KEY = _env("TIANDITU_KEY", "")      # <- .env: HH_TIANDITU_KEY
GEO_TIMEOUT_S = _env_int("GEO_TIMEOUT", 15)
GEO_CACHE_TTL_S = _env_int("GEO_CACHE_TTL", 6 * 3600)     # 同一关键词/坐标缓存 6h

# ====================================================================== APK 自更新
APK_DIR = _env("APK_DIR", os.path.join(_HERE, "apk"))
APK_INDEX = _env("APK_INDEX", os.path.join(APK_DIR, "versions.json"))
APK_CHANNEL = _env("APK_CHANNEL", "debug")                # 默认渠道（本项目发 debug 包）
# 上传/删除接口的管理口令（X-Admin-Token）。未配置时这两个接口直接 503 关闭。
ADMIN_TOKEN = _env("ADMIN_TOKEN", "")                     # <- .env: HH_ADMIN_TOKEN（要发版就必填）
APK_MAX_BYTES = _env_int("APK_MAX_BYTES", 200 * 1024 * 1024)

# ====================================================================== 运行时
MAX_BODY_BYTES = _env_int("MAX_BODY", 4 * 1024 * 1024)    # 普通 JSON 请求体上限（APK 上传除外）
REQUEST_LOG = _env_bool("REQUEST_LOG", True)
LOG_FILE = _env("LOG_FILE", "")                            # 空 = 只打 stdout
SERVER_NAME = "hh-server/1.0"


# ====================================================================== 自检辅助
def secret_status():
    """密钥配置状态（只返回布尔与脱敏值，绝不返回明文）。"""
    return {
        "AGENTSVC_TOKEN": {"required": True,
                           "configured": bool(AGENTSVC_TOKEN),
                           "env": "HH_AGENTSVC_TOKEN", "masked": _mask(AGENTSVC_TOKEN)},
        "AMAP_KEY": {"required": GEO_PROVIDER == "amap",
                     "configured": bool(AMAP_KEY),
                     "env": "HH_AMAP_KEY", "masked": _mask(AMAP_KEY)},
        "TIANDITU_KEY": {"required": GEO_PROVIDER == "tianditu",
                         "configured": bool(TIANDITU_KEY),
                         "env": "HH_TIANDITU_KEY", "masked": _mask(TIANDITU_KEY)},
        "ADMIN_TOKEN": {"required": False,        # 不配 = 上传/删除接口关闭（安全默认）
                        "configured": bool(ADMIN_TOKEN),
                        "env": "HH_ADMIN_TOKEN", "masked": _mask(ADMIN_TOKEN)},
    }


def missing_secrets():
    """缺失的必需密钥名（启动告警 / /healthz 用）。"""
    st = secret_status()
    return [k for k, v in st.items() if v["required"] and not v["configured"]]
