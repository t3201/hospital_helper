# server/ — 移动端接口归集服务（hh-server）

把原先散在 App 里的三类上游（Agent Service、高德 Web API、APK 分发）收进**一个**后端：
Python 3 原生 `ThreadingHTTPServer`，**零第三方依赖**（只用标准库），直接对公网发布
`https://hh.ei.ac.cn:51120`。App 侧从此只认这一个地址，不再持有任何第三方 key。

```
App ── https://hh.ei.ac.cn:51120 ─┬─ POST /v1/infer      → Agent Service (localhost:7988, Bearer 走 .env, agent_ids=['hospital_helper'])
                                   ├─ GET/POST /v1/location → 高德 Web API（key 在 .env；以后可换天地图）
                                   ├─ GET /v1/app/*        → APK 更新（人工上传，支持 Range 续传）
                                   └─ GET / · /healthz · POST /v1/routes  → 自查
```

---

## 1. 目录

```
server/
├── server.py          入口：ThreadingHTTPServer + TLS 装载 + 路由分发（python server.py）
├── config.py          集中配置：对外地址 / 超时 / 缓存 / APK；**密钥只从 .env 读，代码里默认空**
├── core/
│   ├── router.py      极简路由表：@route("POST", r"^/v1/foo/?$")；describe() 供 /v1/routes
│   ├── http_util.py   请求解析、send_json、SSE 逐行转发（sse_start/sse_write）、upstream() 上游调用
│   └── audit.py       日志（stdout，HH_LOG_FILE 可落盘）
├── geo/               地理位置 provider（可替换）
│   ├── base.py        GeoProvider 抽象 + GeoHit 统一出参 + GeoError
│   ├── amap.py        高德实现（当前默认）
│   ├── tianditu.py    天地图实现（预留，CGCS2000→GCJ-02 已换算）
│   ├── coords.py      WGS84/GCJ02/CGCS2000 换算
│   └── __init__.py    provider 工厂 + 6h 结果缓存（server/cache/）
├── api/
│   ├── infer.py       /v1/infer（JSON 与 SSE 两种，症状描述与模型回答原样透传）
│   ├── location.py    /v1/location、/v1/location/providers
│   ├── apk.py         /v1/app/version|list|download|upload|remove
│   ├── health.py      GET /（一行 running）、/healthz、POST /v1/routes
│   └── __init__.py    register()：新增接口在这里 import 一行
├── apkstore.py        APK 仓库（server/apk/versions.json 索引 + sha256/md5）
├── axml.py            AndroidManifest.xml 二进制解析（上传时自动取 versionCode/versionName/package）
├── manage_apk.py      命令行发布工具：add / list / latest / rm / path / probe
├── check_secrets.py   开源前自查：.env 里的密钥有没有漏进源码（命中退出码 1）
├── .env               真实密钥（git 忽略，不提交）
├── .env.example       配置模板（提交进仓库，cp 成 .env 再填）
├── apk/               APK 落盘目录（.gitignore）
└── certs/             hh.ei.ac.cn.pem / .key
```

---

## 2. 启动

### 2.1 先建配置文件 `server/.env`（密钥只在这里，代码里没有）

```bash
cp server/.env.example server/.env      # 然后填值；server/.gitignore 已忽略 .env
```

| 键 | 说明 |
|---|---|
| `HH_AGENTSVC_TOKEN` | **必填**。上游 Agent Service 的 Bearer token；没配 → `/v1/infer` 返回 503 |
| `HH_AMAP_KEY` | **必填**（provider=amap 时）。没配 → `/v1/location` 返回 503 |
| `HH_TIANDITU_KEY` | 换天地图时填（同时 `HH_GEO_PROVIDER=tianditu`） |
| `HH_ADMIN_TOKEN` | APK 上传/下架口令。**留空 = 这两个接口自动关闭（503）**，安全默认 |
| `HH_AGENTSVC_BASE` / `HH_AGENT_IDS` / `HH_GEO_PROVIDER` … | 非密钥的部署项也放这里 |

* 查找顺序：`$HH_ENV_FILE` → `server/.env` → `<repo>/.env` → `hospital_intel/.env`（只兜底取地理 key）。
* 优先级：**进程环境变量 > .env**（systemd 里 `Environment=` 可临时覆盖）。
* `HH_` 前缀可省略（写 `AMAP_KEY=…` 也认）；支持 `#` 注释、`export` 前缀、引号。
* 自检：`python server.py --print-config` 打印生效配置与 `.env` 命中路径，密钥一律脱敏；
  `GET /healthz` 里 `config.secrets.*.configured` / `missing_required` 同样只报布尔，不回显明文。

```bash
python server.py                       # 默认 0.0.0.0:51120，证书在 certs/
python server.py --print-config        # 打印生效配置（token/key 打码）
python server.py --no-tls --host 127.0.0.1 --port 51120   # 本机联调（纯 http）
python server.py --self-signed         # 没有正式证书时自签一张（仅联调，App 会校验失败）
```

* 证书缺失 → 打一条 WARN 并**降级为 http**（`/healthz` 里 `tls:false` 可核对），不会崩。
* 证书路径：`server/certs/hh.ei.ac.cn.pem` + `hh.ei.ac.cn.key`（可用 `HH_TLS_CERT` / `HH_TLS_KEY` 改）。

Linux 常驻示例（systemd）：

```ini
# /etc/systemd/system/hh-server.service
[Unit]
Description=hh-server (mobile api gateway)
After=network.target

[Service]
WorkingDirectory=/opt/hospital_helper/server
ExecStart=/usr/bin/python3 /opt/hospital_helper/server/server.py
Environment=HH_ADMIN_TOKEN=换成你自己的随机串
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

### 2.2 部署 / 重启（用仓库里的脚本，别再手工 nohup）

> 事故记录：2026-10-14 线上 51120 上**同时挂着两个进程**（手工 nohup 起第二个时第一个没死），
> 请求被随机分走，表现为"改了代码但线上还是旧行为"；同一天还发生过进程掉了没人拉起（21:19~21:35 连接被拒）。
> 所以：**改完代码只允许 `deploy.sh --restart`（它会先清掉端口上所有旧进程）或 `systemctl restart hh-server`。**

```bash
sh deploy.sh --restart     # 清端口 -> 起一个 -> 6s 内 healthz 自检(失败会打最近 20 行日志)
sh deploy.sh --status      # 看端口上是谁 + healthz
sh deploy.sh --stop
```

* 没有 root 装不了 systemd 时，用 cron 兜底（开机自启 + 每 5 分钟健康探测）：
  `crontab -e` 加两行：
  ```
  */5 * * * * /home/zhanghua/hospital-helper/deploy_watchdog.sh >/dev/null 2>&1
  @reboot     sleep 20; /home/zhanghua/hospital-helper/deploy_watchdog.sh
  ```
* 从开发机一条命令部署（只同步代码，**绝不覆盖**远端 `.env`/`certs`/`apk`/日志；传完自动重启 + 跑体检）：
  ```bash
  python tools/deploy_server.py --dry-run          # 先看要传哪些文件
  python tools/deploy_server.py                    # 同步 + 重启 + 16 项线上体检
  python tools/deploy_server.py --rotate-admin-token
  HH_ADMIN_TOKEN=… python tools/deploy_server.py --publish-apk mobile/…/app-debug.apk --notes "…"
  ```
* 线上体检（改完必须全绿，"只同步了一半代码"就是靠它抓的）：`python tools/server_smoke.py`
  （含 `/healthz`、infer 非流式/SSE、坏 body 400、location、`/v1/app/version|list|download`、Range 206、
  401/404、以及"线上已发布 APK 版本是否等于本地构建版本"）。

需要放行的端口：**51120/tcp**。上游 Agent Service 地址由 `HH_AGENTSVC_BASE` 决定
（生产实测：`https://ai.ei.ac.cn:7988`；本机部署则 `http://localhost:7988`）。

---

## 3. 接口

### 3.1 `POST /v1/infer` — 大模型推理网关

请求（`messages` 与 `prompt` 二选一；症状描述**原样**进 messages，不改写）：

```json
{ "messages": [{"role":"user","content":"发烧三天，咳嗽有痰"}],
  "model": "default",        // 可选，Agent Service 侧的 label，默认 config.DEFAULT_MODEL_ID
  "stream": false }          // true = 原样转发上游 SSE
```

服务端**强制注入**、客户端不可覆盖：`agent_ids=['hospital_helper']`、`Authorization: Bearer <server/.env 里的 HH_AGENTSVC_TOKEN>`。

非流式出参（回答取上游 `content`，不裁剪、不二次加工）：

```json
{ "ok": true, "content": "…", "thinking": "…", "thinking_chars": 0,
  "model_id": "default", "agent_ids": ["hospital_helper"],
  "agent_nickname": "挂号助手", "agent_id": "hospital_helper",
  "session_id": null, "usage": {…}, "elapsed_ms": 2638,
  "provider": "agentsvc", "stream": false }
```

流式（`stream:true` 或 `Accept: text/event-stream`）：上游 SSE **逐行原样转发**
（`event: init` → `data:{role,content,thinking,…}` → `data:[DONE]`），已带
`X-Accel-Buffering: no`，前置 nginx 无需额外配置。

```bash
curl -s https://hh.ei.ac.cn:51120/v1/infer -H 'Content-Type: application/json' \
     -d '{"prompt":"只回复两个字：收到"}'
curl -Ns https://hh.ei.ac.cn:51120/v1/infer -H 'Content-Type: application/json' \
     -d '{"messages":[{"role":"user","content":"说 OK"}],"stream":true}'
```

### 3.2 `GET|POST /v1/location` — 地理位置

入参：`text`（地址/POI）**或** `lng`+`lat`（逆地理；`text="121.47,31.23"` 也认）；
可选 `city`、`radius`（默认 1500m）、`mode=auto|search|geocode|regeo`、`crs`。

出参（与 App 侧 `app/locate.py` 的 `resolve()` 对齐）：

```json
{ "ok": true, "provider": "amap", "mode": "search", "method": "search",
  "lng": 121.422376, "lat": 31.178477, "crs": "GCJ-02", "src_crs": "GCJ-02",
  "name": "…", "address": "宜山路600号", "adname": "徐汇区", "adcode": "310115",
  "city": "上海市", "district": "徐汇区", "elapsed_ms": 210 }
```

```bash
curl -s 'https://hh.ei.ac.cn:51120/v1/location?text=徐汇区宜山路600号'
curl -s 'https://hh.ei.ac.cn:51120/v1/location?lng=121.42&lat=31.18&radius=1500'
curl -s  https://hh.ei.ac.cn:51120/v1/location/providers   # 当前 provider / 可切换列表
```

同一关键词/坐标结果缓存 6h（`HH_GEO_CACHE_TTL`，落 `server/cache/`）。

### 3.3 APK 更新

| 路由 | 说明 |
|---|---|
| `GET /v1/app/version?current_version_code=9` | 最新版本 + `has_update` / `force_update`（别名 `/v1/app/update-check`） |
| `GET /v1/app/download?file=x.apk` | 下载，支持 `Range`（206/416），响应头带 `X-Sha256` / `X-Version-Code` |
| `GET /v1/app/list` | 历史版本列表 |
| `POST\|PUT /v1/app/upload` | 人工上传，需 `X-Admin-Token` |
| `POST\|DELETE /v1/app/remove?file=x.apk` | 下架，需 `X-Admin-Token` |

```bash
curl -s 'https://hh.ei.ac.cn:51120/v1/app/version?current_version_code=8'
```

---

## 4. 发版（人工上传 APK）

**方式 A（推荐，不用登服务器）**

```bash
curl -T mobile/controller-app/app/build/outputs/apk/debug/app-debug.apk \
     -H "X-Admin-Token: <server/.env 里的 HH_ADMIN_TOKEN>" \
     'https://hh.ei.ac.cn:51120/v1/app/upload?filename=hh-controller-0.2.9.apk&notes=修bug'
```

`version_code` / `version_name` / `package` 缺省时自动从 APK 的 `AndroidManifest.xml` 解析
（`axml.py`），并记 sha256/md5。可选 query：`channel`、`force=1`（强制更新）、`min_version_code`。

**方式 B（scp 到服务器后登记）**

```bash
python manage_apk.py add apk/hh-controller-0.2.9.apk --notes "修bug" --channel debug
python manage_apk.py list | latest | rm <file> | probe <apk>
```

**App 侧**：设置页「检查更新」→ `GET /v1/app/version`（比对 `versionCode`）→ 下载 →
sha256 校验 → 拉起安装（`ApkProvider` FileProvider，需 `REQUEST_INSTALL_PACKAGES`）。
`force_update=true` 时弹不可取消对话框。

---

## 5. 配置（`config.py`；密钥放 `server/.env`，其余可用 `HH_*` 环境变量覆盖）

| 键（.env 或同名环境变量） | 默认 | 用途 |
|---|---|---|
| `HH_HOST` / `HH_PORT` | `0.0.0.0` / `51120` | 监听 |
| `HH_TLS_CERT` / `HH_TLS_KEY` / `HH_TLS_SELF_SIGNED` | `certs/hh.ei.ac.cn.pem/.key` | TLS |
| `HH_AGENTSVC_BASE` | `http://localhost:7988` | Agent Service（生产实测 `https://ai.ei.ac.cn:7988`）|
| `HH_AGENTSVC_TOKEN` | **空（.env 必填）** | 上游 Bearer；未配 → `/v1/infer` 返回 503 |
| `HH_AGENT_IDS` | `hospital_helper` | 固定 agent（客户端不可覆盖） |
| `HH_AGENTSVC_INFER_PATH` | `/v1/infer/stream` | 上游流式路径 |
| `HH_AGENTSVC_PROBE_PATH` | `/v1/models` | `/healthz` 探上游用的只读路径（见 §8）|
| `HH_AGENTSVC_INSECURE_TLS` | `0` | 上游是 https 且自签证书时设 `1` 跳过证书校验 |
| `HH_INFER_TIMEOUT` / `HH_INFER_CONNECT_TIMEOUT` | 300 / 10 s | 超时 |
| `HH_DEFAULT_MODEL_ID` | `default` | 模型 label |
| `HH_GEO_PROVIDER` | `amap` | 地理 provider |
| `HH_AMAP_KEY` / `HH_TIANDITU_KEY` | **空（按 provider 必填其一）** | 地理凭证；未配则 /v1/location 返回 503 |
| `HH_GEO_CRS` / `HH_GEO_CACHE_TTL` | `GCJ-02` / 21600 s | 出参坐标系 / 缓存 |
| `HH_ADMIN_TOKEN` | **空 = 上传/下架接口自动关闭（503）** | APK 发版口令（要发版就填随机串） |
| `HH_APK_DIR` / `HH_APK_CHANNEL` / `HH_APK_MAX_BYTES` | `apk/` / `debug` / 200MB | APK 仓库 |
| `HH_REQUEST_LOG` / `HH_LOG_FILE` / `HH_MAX_BODY` | on / stdout / 4MB | 运行时 |
| `HH_ENV_FILE` | 未设 | 指定单个 .env 文件（覆盖默认查找顺序） |

> **密钥不进代码**（2026-10-14 起）：`config.py` 里三个密钥默认值都是空串，只从 `.env` /
> 环境变量读取；`server/.env` 已被 git 忽略，仓库里只提交 `server/.env.example`。
> 开源前自查一条命令：`python check_secrets.py`（把 .env 里每个值当明文在全部源码里搜一遍，
> 命中就报 文件:行号 并以退出码 1 提醒；`--show-keys` 可列出待查密钥的脱敏前缀）。

---

## 6. 以后加接口（三步）

```python
# api/foo.py
from core.router import route
from core.http_util import upstream

@route("POST", r"^/v1/foo/?$")
def foo(ctx):
    """一句话说明——会自动出现在 POST /v1/routes 的清单里。"""
    body = ctx.json()
    ctx.send_json({"ok": True, "echo": body}, 200)
```

1. 新建 `api/foo.py`；
2. 在 `api/__init__.py` 的 `register()` 里 `from api import foo`；
3. 需要上游就用 `core.http_util.upstream()`，需要缓存复用 `geo/__init__.py` 的缓存写法。

`POST /v1/routes`（机器可读清单）自动生效。
注意：`GET /` 只回一行 `hh-server running`，**不再吐接口清单** —— 公网上一张接口地图
等于给扫描器递刀，清单只走 POST（`curl -X POST https://<host>:51120/v1/routes`）。

## 7. 换天地图

`geo/` 已按 provider 抽象：`HH_GEO_PROVIDER=tianditu` + `HH_TIANDITU_KEY=<key>` 即切换，
`/v1/location` 出参不变（天地图 CGCS2000 自动转 GCJ-02），**App 侧零改动**。

## 8. 排障：`/v1/infer` 报 502 怎么定位

先只看一条命令，不用猜：

```bash
curl -s https://hh.ei.ac.cn:51120/healthz | python -c "import json,sys;print(json.load(sys.stdin)['checks']['agentsvc'])"
```

`checks.agentsvc` 里三个字段足够分流：

| 现象 | 含义 | 处理 |
|---|---|---|
| `tcp:false` | 端口没监听 | 上游没起 / 端口写错 |
| `tcp:true, http_status:null, hint:"端口通但连接被重置…"` | 端口上**不是 HTTP**（最常见：上游是 https 端口却填了 `http://`，或端口被别的服务占着） | 把 `HH_AGENTSVC_BASE` 的 scheme/端口改对 |
| `tcp:true, http_status:null, hint:"…证书校验失败…"` | 上游 https 证书与域名不符 / 自签 | 换正确域名，或 `HH_AGENTSVC_INSECURE_TLS=1` |
| `http_status:401/403` | HTTP 层通、只是探测路径要鉴权 | 正常，`/v1/infer` 会带 token |
| `http_status:200/404` | 上游活着 | 看 `/v1/infer` 返回里的 `upstream_status` |

真实案例（2026-10-14 上线当天）：`HH_AGENTSVC_BASE` 写成 `http://localhost:7988`，
而该机器的 7988 上其实是 **https** 服务 → `/v1/infer` 稳定 502
`上游请求异常: ConnectionResetError: [Errno 104] Connection reset by peer`，
但 `/healthz` 里 `tcp:true` 看着"一切正常"。改成 `https://ai.ei.ac.cn:7988` 后恢复；
`/healthz` 现在会直接给出 `http_status` 与 `hint`，并把它写进 `warnings`。

## 9. 注意

* 上游不可达 → `502 {"ok":false,"error":…,"upstream_status":…,"retryable":true}`，错误不外泄 token。
* 客户端断连（SSE/大文件）只记 WARN，不刷栈。
* `server/apk/`、`server/cache/`、`server/certs/*.pem|key` 已在 `server/.gitignore`。
* 本机联调：`python server.py --no-tls --host 127.0.0.1 --port 51120`，App 里把
  `AgentClient.DEFAULT_BASE` / `llm_client.DEFAULT_BASE` / `amap.SERVER_DEFAULT` 临时指到本机。
