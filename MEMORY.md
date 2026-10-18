# MEMORY

## Lever 3 (L3_BOUNDED_POLL) — bounded-poll instead of fixed sleep
**What:** After a card tap (and back/expand transitions) the old code did a fixed `time.sleep(3.0)` (or 2.5/1.5) then one `adb.dump()`. L3 replaces that with `_wait_navigate(adb, timeout=3.0, interval=0.6)` which polls `page_kind()` (cheap string-regex on the dump) until it reaches a stable state (`doc`/`six`/`blocked`), returning early on first stable read and only dumping once more on timeout. Same semantics as "wait for render" but stops as soon as the page is ready instead of always waiting the full 3s.
**Why:** The fixed sleeps total ~101s per run and are either too long (wasted) or, on slow devices, too short (correctness risk). Bounded polling is both faster and more reliable. On-device dumps are ~cheap (windowRoot) so the per-poll cost is negligible vs the 3s saved.
**How to apply:** Flag `L3_BOUNDED_POLL` (module-level bool, default True) in both PC `slots/fetch_slots.py` and APP `mobile/controller-app/app/python/slots/fetch_slots.py`. The helper `_wait_navigate` is a new shared function; the call sites are the `time.sleep(3.0)` after `card` tap, and the `1.5`/`1.2` sleeps in the back-to-L2 loop. Reverting = set `L3_BOUNDED_POLL=False` to restore `time.sleep(3.0)+dump`.

## Lever 1 (L1_FAST_NO_SLOT_EXIT) — fast no-slot exit after drill-down
**What:** When the doctor-home (six) page shows `rows_before==0` and there is no "显示全部排班" expand button, the old code slept 2.5s + re-dumped before concluding no-slot. L1 makes it conclude no-slot immediately (0-row collapsed state = definitively no slots) and go straight to back.
**Why:** In the SH-084 expert run, 12/18 doctors were no-slot and each still paid the full "expand-wait + re-dump" cost. Collapsed state with 0 visible rows is a reliable no-slot signal, so the extra wait/dump is pure waste.
**How to apply:** Flag `L1_FAST_NO_SLOT_EXIT` (default True) in both copies. The change point is in `fetch_one_dept_unit`: when `rows_before==0 and expand_button is None`, skip the 2.5s sleep + re-dump and record `variant='no_slot_rows_on_home'` immediately. Reverting = `L1_FAST_NO_SLOT_EXIT=False`.
**Correctness note:** Must verify `six_records` and 有号/无号 counts don't drop vs baseline. A 0-row collapsed homepage that actually has slots is the risk case (would need the expand button to exist — but if button is None there's nothing to expand, so 0 rows = no slots).

## Lever 2 (L2_DROP_REDUNDANT_DUMP) — drop a redundant dump
**What:** In the card-tap retry path the code dumped twice (once to check page_kind, once to re-tap) when the first dump already showed we were still on L2. L2 reuses the first dump's result for the retry decision, avoiding a second full dump.
**Why:** Reduces dump count without losing the wait-for-render semantics (the first dump already confirmed page state).
**How to apply:** Flag `L2_DROP_REDUNDANT_DUMP` (default True) in both copies. Reverting = `L2_DROP_REDUNDANT_DUMP=False` (restore the extra dump).

## Runtime lever-override (selftest harness)
**What:** Added `apply_lever_overrides()` in both fetch_slots.py copies (called from selftest.py line 159, before run_conditions). It reads a JSON file of `{"L1":0/1,"L2":0/1,"L3":0/1}` and writes the module-level flags before the run, so A/B runs can be toggled WITHOUT rebuilding the APK (Chaquopy caches modules in sys.modules, so import-time-only override would only fire once).
**Why:** Enabling same-day per-lever A/B testing on-device, and giving each lever an independent runtime rollback switch (satisfies "分别可回退").
**How to apply:** Candidate paths (in order): `HH_LEVERS` env var; `/data/user/0/com.hospitalhelper.controller/files/hh_levers.json`; `~/.hh_levers.json`; `<DATA_DATA|ANDROID_DATA>/com.hospitalhelper.controller/files/hh_levers.json`. To run a lever-off baseline: `adb shell "printf '{\"L1\":0,\"L2\":1,\"L3\":1}' > /data/local/tmp/hh_levers.json"` then `run-as ... cp /data/local/tmp/hh_levers.json files/hh_levers.json`, then trigger selftest. Log line `[levers] override from ...: L1=False L2=True L3=True` confirms it fired.

## Device A/B harness (selftest on 476cf991)
**What:** Trigger via `adb shell sh /data/local/tmp/selftest_run.sh` (or `am broadcast -a com.hospitalhelper.controller.SELF_TEST -n .../.SelfTestReceiver --es manifest /data/local/tmp/st.json`). Read results from `run-as com.hospitalhelper.controller cat files/out/selftest_last.json` (has `elapsed_ms`, `six_records_total`) and `files/out/slots/out/condition_runs/run_*.json` (per-doctor `expands`, `variants.no_slot_rows_on_home`, `cost_s`, `bookable_count`).
**Why:** This is the device-side (in-process AccDeviceBridge) path the user requires for speedup measurements.
**How to apply:** Before each run, stash existing done-markers (`run-as ... sh -c 'mv files/out/slots/out/condition_runs/run_*.json files/out/slots/out/_stashed/'`) to defeat the resume/skip logic, and re-enable the a11y service after any `pm clear`/install. Logcat is flooded with ResourceWarning spam from manifest file opens — filter with `grep -v ResourceWarning`.

## CRITICAL CAVEAT: live-data drift confounds A/B
**What:** The same doctor roster / slot availability DRIFTS over time (hospital live data). In this session the all-ON run (23:36) had 13 doctors / 4 no-slot / six=68, while the all-OFF baseline (00:19) had 14 doctors / 8 no-slot / six=58. These two runs were 43 min apart and the roster genuinely changed (doctors added/removed, slots filled).
**Why:** This means a naive "OFF vs ON" comparison is INVALID — the `no_slot` and `six_records` differences (and even doctor count) are driven by live data, NOT by the levers. L1/L2/L3 only change wait/dump/exit TIMING; they cannot change which doctors exist or how many slots they have.
**How to apply:** For a clean lever-attribution you must run OFF and ON back-to-back (minutes apart) on the SAME roster. Even then, if the roster differs between the two runs, the per-lever contribution to `no_slot`/`six` counts is UNATTRIBUTABLE — only `elapsed_ms` (and per-doctor cost deltas) are meaningful. Report this honestly: the speedup is real (OFF 194.8s vs ON 181.3s, ~13.5s faster on the same manifest), but the count differences are live-data, not levers.

## nav_fail(goto_unit=fail) — DIAGNOSIS ONLY (do not fix this round)
**Root cause (environment, not the code path originally suspected):** During runs, HyperOS backgrounded the 随申办 app (`GAME_GOTO_BACKGROUND-4`) and the notification shade (`mCurrentFocus=NotificationShade`) / the read-screen accessibility service stealing focus / silently unbinding. Result: device dumps returned empty → `page_kind()` = `other` → `ensure_444` looped 6 rounds then gave up → `nav_fail`. This is an accessibility-binding / foreground-priority issue, NOT the `_scroll_l2_to_top` / `open_dept` code path the user originally suspected. Note: on this session the a11y service was disabled again (enabled_accessibility_services=null) after a `pm clear`; re-enabling (`settings put secure enabled_accessibility_services com.hospitalhelper.controller/.SsAccessibilityService`) restored the Bound state and the next run passed `ensure_444 round 1/6 kind=444` immediately.

## 接口归集服务端 hh-server（2026-10-14，server/）
**Decision:** 移动端三类上游接口全部收进一个自建后端：`server/`（Python 3 标准库 `ThreadingHTTPServer`，零第三方依赖），对外唯一地址 **https://hh.ei.ac.cn:51120**（App 内写死）。
- `POST /v1/infer`：转调 Agent Service（`HH_AGENTSVC_BASE`，生产实测 `https://ai.ei.ac.cn:7988`，Bearer 从 `server/.env` 的 `HH_AGENTSVC_TOKEN` 读），**强制** `agent_ids=['hospital_helper']`；症状描述与模型回答原样透传；`stream=true` 时上游 SSE 逐行原样转发（`event: init` → `data:{…}` → `data:[DONE]`）。端上不再有 API_KEY（设置页输入框已删）。
- `GET|POST /v1/location`：内部转高德（key 在 `server/.env` 的 `HH_AMAP_KEY`），`geo/` 抽象 provider，`HH_GEO_PROVIDER=tianditu` + `HH_TIANDITU_KEY` 即可换天地图（CGCS2000→GCJ-02 已换算），出参与端上 `app/locate.py` 对齐；结果缓存 6h（`server/cache/`）。
- APK 更新：`GET /v1/app/version|list|download`（Range 206/416 + `X-Sha256`），`POST /v1/app/upload` / `remove` 需 `X-Admin-Token`（口令在 `server/.env` 的 `HH_ADMIN_TOKEN`；留空=接口自动关闭）；人工上传，`axml.py` 从 APK 的 AndroidManifest 自动解析 versionCode/Name/package；索引 `server/apk/versions.json`；CLI `server/manage_apk.py`。
- 配置：密钥全部在 `server/.env`（git 忽略；仓库只提交 `server/.env.example`），`config.py` 里默认空串；
  缺 key 时接口 503 并提示该填哪个键；`--print-config` / `/healthz` 只报 configured+脱敏。
- 自查：`GET /`（服务卡片）、`GET /healthz`、`GET /v1/routes`（handler docstring 自动进 desc）。新增接口 = `api/foo.py` + `api/__init__.py` 里 import 一行。
- 证书：`server/certs/hh.ei.ac.cn.pem|.key`；缺失则 WARN 并降级 http（`/healthz` 里 `tls:false`）。

**Mobile 侧改动（同批）**：`AgentClient.java` `DEFAULT_BASE=https://hh.ei.ac.cn:51120` + `PATH_INFER=/v1/infer`（body 带 `stream:true`）；`app/python/app/llm_client.py` 同址（CRLF 文件，改脚本改）；`app/python/app/amap.py` 默认走服务端（`SERVER_DEFAULT`，仅显式配 key 时直连高德）；新增 `UpdateChecker.java`（查版本→下载→sha256 校验→FileProvider 安装）+ `ApkProvider.java` + `REQUEST_INSTALL_PACKAGES`；`MainActivity` 设置页加「检查更新」。`gradlew assembleDebug` 通过（versionCode 9 / 0.2.8-chaquopy，约 35.7MB）。

**上线实测（2026-10-14 晚，公网 https://hh.ei.ac.cn:51120）：18/18 全绿。**
- TLS：Let's Encrypt 通配符 `*.ei.ac.cn`，TLSv1.3 `TLS_AES_256_GCM_SHA384`，**2026-12-04 到期**（要续）。
- `POST /v1/infer` 非流式 ~0.7-1.2s（挂号助手 / thinking 计数 / usage 齐全）；`stream=true` SSE 逐行透传
  `event: init` → 443~556 个 `data:{…}` → `event: usage` → `data:[DONE]`，首字节 ~190-230ms。
- 端上 Python 路径（清掉 `RUNTIME_SERVER_URL` 后）`resolve_base_url()=https://hh.ei.ac.cn:51120`，
  `stream_chat` 1.3s、`chat_json` 出 `{"department":"呼吸内科"}` 0.9s。注意优先级
  `HH_SERVER_URL → RUNTIME_SERVER_URL → AGENTS_URL → DEFAULT_BASE`，桌面机器上有前两者会盖掉写死地址。
- `/v1/location` search/regeo 正常（缓存命中 `method=search_cached`）；APK：version/Range 206/416/整包 sha256 一致；
  错误路径 400/404/405/401 正确；20 并发 /healthz 全 200。整包 35.7MB 走公网实测 ~0.12MB/s（300s），
  自更新下载体验要在真机 Wi-Fi 下再测。
- **踩坑**：上线首日 `/v1/infer` 稳定 502 `ConnectionResetError: Connection reset by peer`，而 `/healthz` 里
  `tcp:true` 看着正常——根因是 `HH_AGENTSVC_BASE` 写成 `http://…:7988`，而那个端口是 **https**。已改 env 恢复。
  为此 `/healthz` 新增上游**真探测**：`checks.agentsvc.http_status`（非 None = 端口上确实是 HTTP 服务，
  401/404 也算通）+ `probe_path`(`HH_AGENTSVC_PROBE_PATH`，默认 `/v1/models`) + `hint` + 顶层 `upstream_ok`/`warnings`；
  `core/http_util.upstream()` 现在支持 https 上游，自签证书可 `HH_AGENTSVC_INSECURE_TLS=1`。README §8 是这套排障表。

证书 2026-12-04 前续期。

## 手机端两处实机问题修复 + 全量自测（2026-10-14 深夜，versionCode 13 / 0.2.12-chaquopy）

**问题**：① 自检 / 随申办探索跑完不回前台；② 浮窗（MiniChip）不显示或只显示「运行中…」没有详细信息。

**根因与改法**
- 回前台：MIUI/HyperOS 的 `appops MIUIOP(10017)`（后台弹出界面）**和** `SYSTEM_ALERT_WINDOW` 任一为
  `ignore` 都会让 `AppTask.moveToFront` / `startActivity(REORDER_TO_FRONT)` / 无障碍拉起全部被拦。
  两个都 allow 后 L1 `moveToFront 生效，已回前台`（~1.1s）；任一被拒只能落到 L3「点我回到挂号助手」按钮。
  **每次 adb 覆盖安装这两个权限都会被重置**，测试前必须重新 `appops set ... 10017 allow` +
  `appops set ... SYSTEM_ALERT_WINDOW allow`（+ 重新 `settings put secure enabled_accessibility_services`）。
- 浮窗没有详细信息：老版本手机上残留 `llm_base=http://192.168.2.5:7989/`（设置页 API_KEY 框已删，用户查不出来）。
  `MainActivity.loadConfig()` 加 `normalizeBase()` 一次性迁回公网地址，日志「服务地址已从旧版本残留 … 迁回 …」。
- 浮窗收尾被抖动刷掉：`onResume()` 无条件 `MiniChip.hide()` + `onPause()` 按 `taskBusy()` 重挂通用文案，
  导致「自检完成 …」2.5s 后变回「挂号助手 · 运行中…」。加 `MiniChip.finalPending()` 闸门
  （`showFinal` 记 `finalGen`，20s 自动收），`onResume`/`onPause`/开关探针都跳过这段时间。
- 新增 debug 专用 `LlmPingReceiver`（action `com.hospitalhelper.controller.LLM_PING`，tag `HH_LLM`）：
  只发一次推理、不碰随申办 UI、不下单，用来单独验「大模型这一腿」。**必须在 manifest 里声明**，
  否则 `am broadcast -n .../.LlmPingReceiver` 静默丢失（只有动态注册时不带 `-n` 才收得到）；
  声明后不带 `-n` 会双投递，故加 `IN_FLIGHT` 在飞闸门。
- 顺手修：`app/build.gradle`、`AndroidManifest.xml` 注释里有上一轮写坏的 UTF-8 尾字节（中文变 `?`），已重写干净。

**实机验证（Redmi 476cf991，v13）**
- 自检三轮 PASS：`ok=true rc=0 units=1 planned=1 six=158/148 elapsed_ms=216065~276023`；
  跑前 `raw/ 基线文件数 198→219`，产出 20+ 个 `SH-084_眼科门诊_*_dept.xml`。
- 浮窗全程有细粒度步骤：`进入医院: 第十人民医院` → `寻找tab: 眼科` → `点击科室: 眼科门诊` →
  `等待进入门诊挂号(L2)…` → `点医生: 徐鼎 主任医师` → `等待医生主页(六字段)…`，
  收尾 `自检完成 · ok=true rc=0 units=1 planned=1 six=148 276023ms err=null`，20s 后自动收，
  期间**没有**再被换成「运行中…」。
- 回前台：`[回前台] moveToFront 生效，已回前台`，`mCurrentFocus=…MainActivity`。
- LLM 单腿：`[ping] llm_base(解析) = https://hh.ei.ac.cn:51120` →
  `[ping] OK result={'department': '心血管内科'}` elapsed 1761ms / first_byte 307ms / attempts=1。

**服务端实况（公网 https://hh.ei.ac.cn:51120）**
- 21:19~21:35 之间 51120 **连接被拒**（443 也拒，`ai.ei.ac.cn:7988` 与 22 端口正常）——进程掉过一次，
  期间机上 ping 报 `ConnectionRefusedError [Errno 111]`。之后自行恢复。**要加守护/开机自启**。
- `/healthz` 现在 500：`AttributeError: module 'config' has no attribute 'AGENTSVC_PROBE_PATH'`
  —— 线上 `api/health.py` 是新的、`config.py` 还是旧的（**只同步了一半代码**）。
  其余 10 项（infer 非流式/SSE、location、app/version、download Range 206、404/400/401）全过。
  已把 `api/health.py` 改成 `getattr(config,'AGENTSVC_PROBE_PATH','/v1/models')` + 整体 try/except
  （健康接口不许 500，缺哪块就少给哪块），**但线上仍需把整个 `server/` 同步过去重启**。
- APK 发布仍卡在服务端自己的 `HH_ADMIN_TOKEN`（本地轮换过的和旧的都 401），自更新链路至今没端到端跑过。

## APK 发版 + 真机自更新端到端跑通（2026-10-14 22:00~22:25）

**发版口令**：线上 `HH_ADMIN_TOKEN` 由服务器上的 `config.py`/`.env` 持有（值不入库，只写在服务器那份 .env；
本地 `server/.env` 里那个 `hh-admin-4ac…c420` 只对本地实例有效）。探针法：合法 token + 坏 body → 400「请求体不是 APK/ZIP」，
错 token → 401，可用来在不上传 35MB 的情况下确认口令。

**发布**：`POST /v1/app/upload?file=app-debug.apk&channel=debug&notes=…`，body = APK 原始字节
（`curl -T` 等价）。v13/0.2.12-chaquopy 35,497,528 B，sha256 `386b16644c315e94…`，上行实测 **1.1 MB/s / 31s**。
线上 `/healthz` 已恢复 200（`upstream_ok=True`、`http_status=401`、`probe=/v1/models`、
apk dir `/home/zhanghua/hospital-helper/apk`）——说明服务器代码已整体同步过一次。
`/v1/app/version` 对 cur=9/12 → has_update=True，cur=13 → False；`/v1/app/list` count=1；
Range 206 / 越界 416；整包下载 sha256 与 `X-Sha256` 一致。

**真机自更新（Redmi 476cf991，故意先装回落后的 v9）**：
`pm uninstall`（注意 `adb uninstall` 在这个 HyperOS 上会报 `Must specify package name to remove a split`，
要用 `pm uninstall`）→ `install -r -g` v9 → 设置页「检查更新（/v1/app/version）」→
弹窗「发现新版本 0.2.12-chaquopy(13)」+ notes/大小 → 「下载并安装」→
`下载中 1.0/33.9 MB … 33.9/33.9`（**下行只有 ~0.11 MB/s，全程 5 分 02 秒**）→
`下载完成、sha256 校验通过：/data/user/0/…/files/updates/app-debug.apk` → `已拉起系统安装器` →
MIUI 三连弹窗（① 已升级防护能力/知道了 ② 未查询到此应用的 ICP 备案信息 → 继续安装
③ 「已了解此应用未经安全检测」勾选框 + 继续更新）→ `versionCode=13 lastUpdateTime=22:19:19`。
更新后再 ping：`llm_base=https://hh.ei.ac.cn:51120` → `OK {'department': '呼吸内科'}` 2494ms/首字节 319ms。

**自更新后的坑（重要）**：覆盖安装会把 **`SYSTEM_ALERT_WINDOW` 和 `MIUIOP(10017)` 重置成 `ignore`**
（和 adb 重装一样），于是浮窗和自动回前台又失效；无障碍服务则**不会**被重置。
=> 建议：App 在「更新后首次启动」检测 versionCode 变化，主动重走悬浮窗/后台弹出界面的授权引导
（Preflight 里已有 `BACK_LAUNCH` 项可复用）。另外 0.11 MB/s 的下行意味着 35MB 包要 5 分钟，
`UpdateChecker` 目前不支持断点续传（服务端 Range 已就绪），值得补。

**MIUI appops 语法**：`appops set <pkg> 'MIUIOP(10017)' allow` 会被拒（Unknown operation string），
必须用数字 op id：`appops set com.hospitalhelper.controller 10017 allow`。

---

## 实时日志流畅度 + 公网信息面收敛（2026-10-14 晚，v15 / 0.2.14-chaquopy）

**症状**：实时日志页流式输出卡顿，MI 5X（旧机）弹「应用无响应，是否等待」；Redmi 不弹，
但页面不滚动，看着像死机（其实没死，之后仍能正常跳随申办）。

**根因**：`MainActivity.appendReasoning` 每个 SSE 分块都做 ①全文重建 Spannable
②`applyMetaSpans` 全文 `indexOf` 两遍 ③`setText` 全量重排 ④`smoothScrollTo` 动画滚动
→ 整体 O(n²)（一次推理几百个分块）。另外推理框是 `wrap_content` 直接长在 `panel_log` 里，
文本一长整页被撑高、可视区停在上面，所以"不动了"。

**修法**（MainActivity.java + res/layout/activity_main.xml）：
- 缓冲立即累积、视图**节流**：`REASON_FRAME_MS=120ms`（推理）、`LOG_FRAME_MS=200ms`（详细日志）；
  待发帧存在 `reasonFrame`，`flushReasoning()` 可取消它并立刻渲染（任务收尾不延迟上屏）。
- 标记行着色只扫本轮新增区间（`reasonUnrenderedFrom`），不再全文 indexOf。
- 推理框包进自己的 `ScrollView @+id/log_reasoning_scroll`，代码里限高
  `min(屏高, 面板高) × 0.80`（`capReasonHeight()`），滚动用 `scrollTo`（**不要** smoothScrollTo，
  长文本下滚动动画本身就卡）。
- 推理缓冲上限 `REASON_MAX_CHARS=9000`（框最多约 80 行，留 24000 只是白白加重每次重排）。
- `flushReasoning()/renderReasoning()` 加主线程守卫：后台线程调 `setText` 会
  `CalledFromWrongThreadException` —— 这个坑是回放压测当场炸出来的（已修）。
- `doSelfTest()` 也 `showTab(TAB_LOG)`：自检时推理框可见（"在动"= 没死）。

**新增 debug 压测入口**：`ReasonReplayReceiver`，action `com.hospitalhelper.controller.REASON_REPLAY`，
extras `chunks` / `delay_ms`；把假分块按真实 SSE 节奏灌进渲染路径，单独压测这段（自检走的是
纯号源抓取、不调 LLM，复现不了）。release 包 `BuildConfig.DEBUG` 直接 return。

**实测**（同参数 chunks=1500 delay_ms=6）：
- MI 5X：`renders=50`（1500 分块 → 50 次刷新，30× 削减），**无 ANR、无 crash**；
  热态第二轮无掉帧告警，首轮那次 `Skipped ~95 frames` 是切面板+首次大布局的一次性开销。
- Redmi：`renders=58`，9.5s 跑完。
- UI dump：推理框高 1231px(MI5X) / 1544px(Redmi) = 屏高 64%（即面板高 80%，≤ 要求的 80%），
  文本尾「—— 模型推理结束 ——」可见 = 已同步滚到底。

**服务端信息面收敛**（server/，**线上还没部署**）：
- `GET /` 只回一行纯文本 `hh-server running`（GET,HEAD 都支持），不再吐接口清单 JSON；
- 接口清单改 **`POST /v1/routes`**（GET → 405），公网扫描器扫不出来；自查：
  `curl -X POST https://hh.ei.ac.cn:51120/v1/routes`；
- 404 文案去掉「GET / 查看接口清单」这种指路；`core/http_util.py` 新增 `send_text()`；README 已同步。
- 线上核对（改动未同步时）：`GET /` 仍返回 JSON、`POST /v1/routes` 仍 405 => 需要把 server/ 同步过去并重启。

**发版**：v15 / `0.2.14-chaquopy`，35,591,953 B，sha256 `2761ac7cc3a152ce…`，
已 `POST /v1/app/upload` 发到公网（`sha256_match=True`，`cur=13/14 → has_update=True`）；
两台测试机已 adb 覆盖安装（装完要重新 `appops allow SYSTEM_ALERT_WINDOW` 和 `10017`）。

**踩坑备忘**：① XML 注释里不能出现连续两个减号（我在 manifest 注释里写 adb 的 `--ei` 直接
让 ManifestMerger 解析失败）；② cmd 里 `set VAR=x && cmd` 会把 `&&` 前的空格塞进变量值，
要写 `set "VAR=x"&&`；③ 这台开发机没有全局 gradle，用项目里的 `gradlew.bat`。

## 症状描述测试用例集（2026-10-15，20 条）
**文件**：`tmp/claim/test_cases_symptoms.json`
**用途**：App「症状描述（必填）」输入框的自动化测试/抽查用例；`desc` 字段可直接喂给
`POST /v1/infer`（`https://hh.ei.ac.cn:51120`）做纯推理链路断言，也可用于真机填框跑全流程。
**结构**：`cases[]`，每条 `{id: TC-001..TC-020, desc, expected:{scenario, dept_first, dept_related[], urgency}, notes}`。
**覆盖矩阵**：
- scenario：peds ×5（TC-001/004/008/012/014）、adult ×11、elder ×4（TC-002/011/016/019）；16 岁边界见 TC-018；
- 首选科室（11 类）：儿科、心血管内科、普外科、骨科、皮肤科、眼科、耳鼻喉科、神经内科、妇科、精神科、消化内科；
- urgency：高×5（TC-002 心绞痛 / TC-003 阑尾炎 / TC-011 心衰 / TC-012 儿童急腹症 / TC-016 老年髋部）、中×9、低×6。
**断言建议**（自动化时）：
- 硬断言：`expected.dept_first` 出现在推理输出的首选科室/目标科室里；
- 软断言：`expected.dept_related` 至少命中 1 个（允许空数组）；`scenario` 与输出 peds/adult/elder 对齐
  （TC-018 的 16 岁为边界，允许 adult 或 peds 任一）；
- 反向断言（notes 里点名的误判，如 TC-009 不应首选眼科/骨科、TC-013 不应首选神经内科）。
**维护**：新增用例继续 `TC-0NN` 递增；改完用
`node -e "const c=require('./tmp/claim/test_cases_symptoms.json');console.log(c.cases.length)"` 校验 JSON。


## 2026-10-15 状态机改造 · 第①步(观测层+判定+证据链)已落地并真机验证

- **新增观测层** `mobile/controller-app/app/python/slots/page_state.py`(与 `slots/page_state.py` 字节一致):
  `observe(dump) -> {level, hospital, tab, dept, subtab_active, subtabs, days, empty}`;
  判定谓词 `dept_consistent / matches / active_name`; `raw_nodes()` 保留 `selected/clickable/resource-id/depth`。
- **硬信号**: L2 子 tab 的 `van-tabs-<N>-<idx>` 节点里**恰好一个** `selected="true"`(语料 368/493 命中);
  旧 `parse_nodes` 丢弃该属性 => 旧代码在结构上无法验证子 tab 是否真的切换(=「普通门诊进不去」的根因)。
- **fetch_slots.py 接入点**: `Nav.obs()/verify()`(期望 vs 实测, 最多 3 次重读)、`_tap_subtab_and_wait`、
  `_verify_subtab_active`、L2 身份门(probe/full 两处)、`enter()` L1 断言、医生主页身份门。
  统一入口 `obs_of()` 注入 `six_match`(不注入则医生主页只能判成 `doc`, `level='six'` 断言永不成立)。
- **身份门带重读保护**: 判不符前先等 1.2s 重读 2 次(防 SPA 标题滞后), 仍不符才 `error_reason=dept_mismatch` 交上层回退。
- **广播改为"验证后"发出**: `run_condition_mode` 的组进度 `_step` 移到 `goto_unit` 返回 ok 之后;
  `Nav.enter()` 的「已进入医院列表」移到 `ensure_444` 之后 —— 真机日志实测顺序
  `18:15:52 等待进入门诊挂号(L2)` → `18:15:54 » 第十人民医院 · 眼科 / 眼科门诊 (1/1)`(浮窗不再超前)。
- **证据链**: `out/slots/out/trace/<ts>_cond/{events.jsonl, dumps/NNN_<state>.xml}`, 每条含
  `state/action/expected/observed/diffs/ok/dump`; `trace_begin()` 在 `run_condition_mode` 起始调用。

### 语料对账(1322 份 dump, `tools/page_state_check.py`)
- level 分布: six 827 / L2 368 / L1 2 / login 125(旧 `page_kind` 把登录页判成 `other`)。
- 可核对身份的 959 份(L2+医生主页)中 **102 份(10.6%)**文件名声称的科室在页面"身份位"上不存在。
- **256 份 dump 内容逐字节相同却被存成不同科室的证据**(7 组; 最大一组 125 份其实是同一个登录页;
  `SH-001_产科_*` 与 `SH-001_产科咨询门诊_*` 完全一致, 标题与科室卡片都是「炎症性肠病专病门诊」)
  => 历史库里"某院某科室无号"可能来自别的科室甚至登录页的屏幕。
- 坑: diag 语料含 `SH-001_c5_182701.xml` 这类**诊断占位名**, 朴素取 `parts[1]` 当科室会虚增 236 份;
  两个工具统一改用 `page_state_check.fname_ctx()`。

### 回归与真机
- `tools/page_state_check.py` PASS(硬不变量 0 违反); `tools/state_verify_regress.py` PASS:
  A1 目标=页面 selected => ok; **A2 目标=别的 tab => 不得为 ok(旧版这里会误判 ok)**;
  B1 抓到 102 份错标; C1/C2 `Nav.verify` 匹配/不匹配。
- **v16 / 0.2.15-chaquopy**(APK 35,724,550 B, versionCode 16)装到 476cf991(kunzite):
  两次自检 `groups=1 six_records=31` 与 `six_records=158`, `stop=None`; **身份误报 0**(不符 0 / 待确认 0 / verify 不符 0)。
- 真机证据链 `trace/20261015T182311_cond`: **23 事件 / 23 dump, 0 个判定失败**
  (L1 期望=第十人民医院 ✓; enter_l2 实测 title='眼科门诊' subtab='专家门诊'(selected) tabs=[专家,普通,专病] days=30 ✓;
  doctor_home ×21 实测 level=six hosp=第十人民医院 dept=眼科门诊 ✓)。

### 仍未做(下一步)
- ② `act(expect, action)` 原语替换其余 tap+sleep; ③ 状态机正式接管 `run_condition_mode`/`_step`(MiniChip 只读已验证状态);
- 真机尚未走 **子 tab(selected)** 路径: full 模式不点子 tab, 需要一个能触发 probe 模式的条件跑(目前只有 SELF_TEST 广播);
- 观测 brief 里 `doctor` 字段仍为空(医生名核对目前靠 `six_fields.find_page_doctor`, 未并入观测层);
- PC 侧 `slots/fetch_slots.py`(3140 行)未与端侧(3951 行)同步;
- 遗留旧项: UpdateChecker 断点续传、每次重装后重新放行 MIUI 权限(10017 / SYSTEM_ALERT_WINDOW)。
  (~~部署 server/~~、~~51120 双进程清理+守护自启~~、~~轮换 `HH_ADMIN_TOKEN`~~ 已于 261016 早完成, 见下节)

## 2026-10-15 (晚) 状态机接管导航 + auth/login 拆分 + 端侧回归
- page_state.level() 把「小程序授权同意页」(AUTH_T + 同意) 拆成 level='auth', 与需要凭据的 login 分开; 旧规则 `AUTH_T or LOGIN_KW` 一律判 login, 导致状态机把可自助页当停止条件 (stop=blocked)。
- nav_machine: STOP_LEVELS 只剩 {blocked, login}; 新增 EDGES 边 agree_auth (auth->444, cost 1.5, 宽落点期望); fetch_slots goto_unit actions 加 'agree_auth': nav.agree_auth(adb.dump())。
- nav_machine 新增: 每条边最多试 2 次后 banned (防表内死循环); PARENT={L2:L1,L1:444,444:home,...} => 「层级对但身份不对」时退一层重进而不是原地 stay; prune_for_level 对非 L2/L1/six 层级裁掉 dept/subtab/hospital (否则边永远验不过)。
- 修 next_edge 真 bug: cur==tgt 且 stay 被禁时回溯到起点 prev[cur]=None => TypeError; 现返回 None 交上层如实报 no_edge。
- 根因类修复: PC 根目录 slots/ 是镜像, 之前 fetch_slots 落后 (3140 vs 3791 行), 已用 mobile 副本同步 (nav_machine/fetch_slots MD5 一致); page_state.py 两份仍一致。
- 离线闸门: tools/state_verify_regress.py 新增 E8/E9 (auth 页走 agree_auth 边 / 同意点不动 2 次后禁用), 全 PASS; tools/page_state_check.py 1322 份语料 PASS (six 827 / L2 368 / L1 2 / login 125)。
- APK: versionCode 19->20, versionName 0.2.19-chaquopy, 已装 476cf991; 重装会把 SYSTEM_ALERT_WINDOW 打回 ignore, 需 `adb shell appops set com.hospitalhelper.controller SYSTEM_ALERT_WINDOW allow`。
- 触发端侧跑必须用显式组件: `am broadcast -a com.hospitalhelper.controller.SELF_TEST -n com.hospitalhelper.controller/.SelfTestReceiver --es manifest '<json>'`; 只带 action 的隐式广播 MIUI/Android13+ 不投递; JSON 必须无空格(separators) 并用单引号包住, 否则设备 shell 会按空格切碎。
- 读端侧私有文件: `adb -s 476cf991 exec-out run-as com.hospitalhelper.controller cat files/out/slots/out/fetch.log` (直接 pull 会被拒)。
- 端侧 v20 回归: probe SH-084 眼科|眼科门诊 ok 19.5s subs=[专家,普通,专病] 普通门诊 status=ok verified=tap; probe SH-207 内科|发热门诊 ok 36.9s status=no_slot stop=None (旧版这条必挂 cannot_reach_444); full SH-084 expert six=158 new_raw=21 cost=229.6s stop=None。
- 端侧证据链 journal 20261015T215036_cond: 47 判定事件 / 1 失败, 失败那条是 L1 进入时 level=unknown 的半渲染帧, 下一次重读即自愈 (doctor_landing 21 ok + doctor_home 21 ok)。
- 遗留弱点(未修): search channel 首页入口 tap (473,786) 连续 3 次未落到搜索页 -> 输入框 NOT FOUND, 靠 ensure_444 的 back 链兜回 444。

## 2026-10-16 (凌晨) 页面/状态一致性 ①–⑥ 收口 + 端侧真跑复核
- **① PageIdentity**: 医生身份 744/827 页可核, how={title:482, fav:262, dept:83}; 职称残留 **0**
  (`C_doctor_title_residue`)。口径根因: `find_page_doctor` 的 norm 故意保留"姓名+职称"是 **L2 卡片比对**需要的,
  但原样写进六字段记录就错 => 在**记录唯一出口** `six_fields.build_six_record` 清洗, 新增 `clean_person_name`
  + `就诊医生_原文`(留证不丢信息); `page_state.strip_title` 改为**委托 six_fields**(观测层/记录层同一实现,
  测试 L2 断言 page_state 不再自带职称表)。
- **② 状态机**: 塌陷帧(页面视觉正常但 WebView 虚拟树 bounds 全 0, 连读 5 帧字节级相同 => 重读永远不会好)
  升级阶梯 = 重读(表预算) → `nudge`(轻扫内容区强制重排, 不碰日期/卡片/子tab) → 按转移表 `reset` 退层重进;
  阶梯写在 STATES 表里(UNKNOWN 带 `'nudges'` 预算), 读不出的帧本身也归档为 `state='machine:unknown_frame'`。
  `_nudge()` **只做一次轻扫, 不内嵌 act 判定**(否则一次自愈记两次熔断判定), 判定交给状态机那一次 act 轮询。
- **③ `act(expect, action)`**: 唯一轮询原语 `fetch_slots.act`(超时/重试全部取自 `nav_machine` 预算表)。
- **④ 证据链**: 新增 `tools/trace_check.py` 自动复核(重读归档 dump → 重推 identity → 对比声明)。
  端上 run `20261016T020016_cond`: 74 事件 **一致=70 矛盾=0 可疑=0 未核实=4 无帧=0**;
  4 条"未核实"都是 doctor 软信号未在帧里显示(不判死, 但也不谎称已核实)。
- **⑤ MiniChip**: 只由已验证状态驱动(`emit_step_state` run/verify/ok/fail), 端侧日志实证
  `» 校验中 · 点医生:刘芳 主任医师` → `» 已核实 · 点医生:刘芳 主任医师 · 眼科门诊 · 刘芳`(浮窗里是清洗后人名)。
- **⑥ 唯一熔断**: 删掉 `RESET_FULL_BUDGET`/`SEARCH_RESTART_BUDGET`/`EMPTY_DUMP_ERROR_BREAK`/`UNIT_ERROR_BREAK`
  (审计确认已无执行点, 只剩日志与注释), 熔断依据只剩**连续 `VERIFY_BREAK_N=3` 次 verify 失败**;
  `verify_judge` 是唯一记账点(每条边结论计一次, 不按轮询/attempt 重复计); 静态守卫 K8 保证旧配额名只出现在注释里。
- **并发唯一导航所有者** `slots/navlock.py`: `run_conditions` 变成 gate(拿不到锁 => `rc=5 stop_reason=nav_busy:<holder>`,
  不排队不抢占, 同线程嵌套 depth 计数), `runner`/`preflight`/`patients` 共用同一把锁; 修的是"僵尸 full 跑与新 probe 同时导航"。
- **端侧**: versionName `0.2.27-chaquopy`(装 476cf991); **APK 内容校验**(读 `assets/chaquopy/app.imy`)确认打包的
  就是最新 4 份模块(避免"改了源码但装的是旧包"); full 跑 `group ok: 眼科|眼科门诊 six=268 cost=459.1s stop=None`;
  记录体检 268 行 / 27 个医生 / **职称残留 0** / **缺 `_原文` 0**。
- **离线闸门**: `tools/state_verify_regress.py` 全绿(A–L: K7 塌陷阶梯跑在**真机 fixture** 上、K8 唯一熔断守卫、
  L 记录层人名清洗); `tools/page_state_check.py` PASS。
- **观察(未改)**: 专病门诊 子tab 的卡片标题是专病名(眼科-干眼病 / 白内障复查 / 屈光不正(近视)), 这些值现在会进
  `就诊医生` 字段(L2 卡片驱动路径的既有口径); 是否应记为 None 待定, 未擅自改。
- 遗留旧项: 部署 `server/` 到 115.190.233.51、51120 双进程清理+守护自启、UpdateChecker 断点续传、轮换发版口令 `HH_ADMIN_TOKEN`(旧值不入库)、
  每次重装后重新放行 MIUI 权限(10017 / SYSTEM_ALERT_WINDOW)。

## 2026-10-16 (凌晨) 服务端部署准备(代码就绪, 卡在 SSH 凭据)
- 新增 `server/deploy.sh`: 幂等重启 —— 先清掉监听 51120 的**所有**进程(修"51120 上挂两个进程"), 再起一个,
  再 6s 内 `healthz` 自检(失败打最近 20 行日志); 带 `--stop/--status`。`bash -n` 语法过。
- 新增 `server/deploy_watchdog.sh`(cron 兜底: 每 5 分钟探 healthz, 60s 锁防与手工重启打架; `@reboot` 开机自启)
  与 `server/hh-server.service`(`Restart=always`, `HH_ENV_FILE`, 日志 append); README 新增 2.2 节写清"改完代码只允许
  `deploy.sh --restart` 或 `systemctl restart`, 不许手工 nohup"。
- 新增 `tools/deploy_server.py`: 逐文件 md5 比对只传差异, **绝不覆盖**远端 `.env`/`certs`/`apk`/日志,
  传完自动 `deploy.sh --restart` + 跑体检; `--rotate-admin-token`(先备份 .env 再 sed, 新口令只打印一次)、
  `--publish-apk`(curl -T + X-Admin-Token)。`--dry-run` 列出 25 个待同步文件。
- 新增 `tools/server_smoke.py`(16 项线上体检)。**线上基线 15 绿 1 红**: 红的是 A6
  `线上已发布 APK 版本 == 本地构建` —— 线上 `version_code=9 / 0.2.8-chaquopy`, 本地 `28 / 0.2.27-chaquopy`
  => 自更新链路现在**永远不会触发**(cur=28 > latest=9, `/v1/app/version` 返回 has_update=False)。
  其余全绿: healthz ok(uptime≈26.9h)、上游 agentsvc 11ms、infer 非流式/SSE、坏 JSON 400、location、
  app/version|list|entries、download 206 + X-Sha256、Range 断点、404/401。
- **部署被凭据卡住(需要用户动作, 我没有绕过 host key 校验)**:
  `zhanghua@115.190.233.51:22` 与内网 `zhanghua@172.28.70.13` 均 `Permission denied (publickey,password)`
  (本机 `id_ed25519` 未被授权); 且 `172.28.70.13` 的 host key 与 known_hosts 里那条 ECDSA 不一致
  (`REMOTE HOST IDENTIFICATION HAS CHANGED`; 新 ED25519 指纹 `SHA256:c+2Ka7UhlnqfGUttREKH8xxBBc51JpbtXr19qxDPE/o`,
  旧 `SHA256:iDv/awmPX8p/Pn5Rg4yzYe8LW5iyiwucKdhZNXM7pTw`) => 机器大概率重装/换过。
  二选一即可继续: ①把 `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFR/tQtCB6iWfkdkTlxKvaKhjCQoZhTEH1lR8i4aVWKL zhanghua@GAYA5`
  放进服务器 `~/.ssh/authorized_keys`(并确认新 host key 是自己改的), 之后我一条命令跑完 同步→重启→体检→发版→轮换口令;
  ②你在能输密码的终端里执行 `python tools/deploy_server.py`。
- `server/check_secrets.py` 现在 **0 命中**(MEMORY.md 里的口令字面值与生成器前缀都改成中性写法)。

## 2026-10-16 (早) 服务端部署完成: 免密 / 属主改 zhanghua / 守护自启 / 发版 / 轮换口令
- **免密打通**: 真实目标是内网 `zhanghua@172.28.233.51`(= 公网 115.190.233.51 同一台, 之前试的 `172.28.70.13` 是另一台机器);
  本机 `id_ed25519` 公钥已进 `~/.ssh/authorized_keys`, `ssh -o BatchMode=yes` 直连可用。
- **线上代码本来就没落后**: 远端与本地所有 `.py` 的 md5 逐一相同(`server.py 1ccc990d…` 等), 所以这轮做的是补部署件 + 发版 + 守护, 不是刷代码。
  远端根目录 = `server/` 包的**内容**(扁平: `hospital-helper/{server.py,api/,core/,geo/,apk/,certs/,.env}`); `tools/deploy_server.py --host` 默认值已改为 172.28.233.51。
- **坑1 sshd 限流**: 逐文件 md5 要 25 次 ssh, 撞上 MaxStartups + 该机正被公网爆破 => 连接卡死; 改成**一次 ssh 取回全部 md5** 本地比对。
- **坑2 属主**: 线上进程属主是 **root**(`root -bash` → `python3 server.py`), 且 `hospital-helper` 目录 `root:root 755`
  => zhanghua 既写不进(远端 tar 报 Permission denied)也杀不掉, 而 `ss -ltnp` 非 root 看不见它 => deploy.sh 会误判"没在跑"然后抢启第二个实例(**这就是 51120 双进程的成因**)。
  - 处置: `chown -R zhanghua:zhanghua` 整棵树, 服务改由 **zhanghua** 运行(51120>1024 不需要特权, 因此**不必**给 zhanghua 加 sudo);
    `deploy.sh` 新增 `port_listening()`(直接 connect 一次, 与属主无关, 一手证据) + `stuck_report()`(提示该找哪个属主停)。
- **守护/自启(无 sudo, systemd 装不了)**: zhanghua crontab `*/5 * * * * sh …/deploy_watchdog.sh` + `@reboot …`;
  实测 `pkill` → DOWN → watchdog 自动拉起(pid 2476765, healthz ok), 自愈链路成立。
- **发版修好 A6**: `--publish-apk` 上线 `version_code=28 / 0.2.27-chaquopy`(35,550,936 B, sha256 `dbd00460…`), 线上=本地=28, 自更新链路不再"永远不触发"。
- **发版口令已轮换**: 远端 `.env` 与本地 `server/.env` 同步换(旧值备份 `server/.env.bak` / 远端 `.env.bak.<epoch>`), 明文不进终端;
  新增 `tools/check_token_rotation.py`: 旧口令 → **401**, 新口令 → **400**(过鉴权、坏 body) => PASS。端侧不使用该口令(mobile 源码无 `Admin-Token`), 轮换不影响 App。
- **体检 18 项 0 失败**(`tools/server_smoke.py`; E3 现在也从 `server/.env` 取口令跑通); `check_secrets.py` 扫 2648 文件仍 0 命中。
- **端侧→服务端实测**: 476cf991 上 `LLM_PING` → `llm_base=https://hh.ei.ac.cn:51120` → `{"department":"呼吸内科"}`, elapsed 1304ms / 首字节 307ms。
- `tools/deploy_server.py` 顺手修三处: `mkdir` 那行 6 个 `%s` 只给 5 参数、发版行占位符不匹配、`_sh` 回显命令行时把 `X-Admin-Token` 明文打进日志(现 `_mask()` 打码)。
- **安全提醒(未擅自改)**: 该机 22 端口持续被公网爆破(`auth.log` 全是 `Failed password for invalid user admin/user`), sshd 仍开 password 认证;
  建议 `PasswordAuthentication no` + fail2ban —— 会锁掉密码登录, 等你确认再动。


## 2026-10-16 门禁永不全绿 + 自检被系统权限自锁（真机 476cf991 已修）

用户实测：打开 App 走自检 → 随申办进 444 → 退回「预约挂号个人信息同意页」并停留。
两个缺陷，均已定位并真机验证：

① 深检收尾误按 BACK（前一段已修）：preflight 到 444 后的 cleanup 调 adb.back()，
   把随申办退回到同意页 → 删除该 BACK，改为如实上报 landing。
② 门禁死锁（本段修）：
   - 「后台弹出界面」被拦的证据 hh_config/bg_launch_blocked 是永久时间戳，
     用户事后开了权限也永远不会自动深检 → LOGIN 永久 UNKNOWN → 严格门禁永不绿。
     修：Preflight.BG_BLOCK_TTL_MS = 10min + bgLaunchBlockedFresh()，过期转「未验证」
     并提示「会再自动试一次」；成功回前台仍由 bringSelfToFront 清掉证据。
   - 我自己引入的死循环：maybeAutoProbeLogin(跳过分支) → renderPreflight →
     maybeAutoProbeLogin …（真机日志每 20ms 两条，等同冻结 UI）。
     修：文案补充挪进 renderPreflight 的 annotateLoginHint()（只改 Item.detail），
     跳过分支只 return；卡住项日志加 10s 限流（lastStuckLog/lastStuckLogAt）。

新增 N 组静态回归 N0–N9（tools/state_verify_regress.py），REGRESS_RC=0 全 PASS。

## MIUI/HyperOS 覆盖安装会重置的三项权限（每次 adb install -r 后必须重开）
1. 读屏（无障碍）—— 必须写全限定组件名，简写 .SsAccessibilityService 会被系统抹掉：
   settings put secure enabled_accessibility_services
     com.hospitalhelper.controller/com.hospitalhelper.controller.SsAccessibilityService
   settings put secure accessibility_enabled 1
   验证：dumpsys accessibility 的 Bound services 出现「挂号助手 · 读屏桥」= 已绑定。
   注意：am force-stop 同样会清掉该条目（不是只有 install -r）。
2. MIUIOP(10017) 后台弹出界面：appops set com.hospitalhelper.controller 10017 allow
3. SYSTEM_ALERT_WINDOW：appops set com.hospitalhelper.controller SYSTEM_ALERT_WINDOW allow
关键顺序：appops/settings 必须在 install 完成之后单独执行；和 install 写在同一条批量
命令里会被安装流程重新重置回 ignore（本段实测踩过两次）。

## 待办（仍未处理）
- 服务端 apk/ 仍是 versionCode 28（0.2.27），本机已是 30/0.2.29-chaquopy → server_smoke A6 会再报陈旧，
  需重新发布 APK 并核对 /v1/app/version。
- K7 真实塌陷自愈、就诊医生/专病取值口径、SSH 加固（PasswordAuthentication no + fail2ban）
  仍待用户决定。
