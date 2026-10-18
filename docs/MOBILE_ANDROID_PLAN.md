# 移动端（Android App）技术方案 — 工作稿 v1

> 目的：把 PC 原型（旧 adb + 挂号推荐 web）迁到一个**手机端 App**，摆脱电脑/USB。
> 旧系统（app/、slots/、ranking/、hospital_intel/、ssb_dept/、app/web.py）**冻结保留，不改动**。

## 0. 现场核对结论（先回答用户的问题）

**项目内的 Python 脚本用的是「直接命令行 adb」，不是 MCP。**

- `app/patients.py::Adb` → `subprocess.run(['adb','-s',self.dev]+a)`（读「就诊人」/只读采屏）
- `slots/fetch_slots.py::Adb` → 同上（live 抓号：dump/tap/swipe/input_cjk/back）
- `slots/fetch_slots_v1_20261004.py::Adb` → 同上（离线全量批量采集）
- `app/locate.py::locate_from_adb` → `adb -s <dev> shell dumpsys location`
- `app/slots.py` → 子进程调 `slots/fetch_slots.py`（该脚本内部再直连 adb）

**系统里另外存在**一个独立的 “Android Use MCP” server：`D:\cc\agents\accessories\android_use_mcp.py`（端口 **7998**，跑的是 `uvx/…`，由 `D:\cc\start_android_mcp.bat` 拉起），工具集：
`run_adb_command / save_file_to_phone_gallery / read_file_from_phone_gallery / find_and_click(截图+OCR定位+input tap) / show_image / read_verification_code`。
它由 **Agent Service**（`D:\cc\agents\app.py`，端口 **7988/7989**）的 agent 运行时调用（MCP 发现/注册在 `/v1/tools/mcp`）。
**但 hospital_helper 的脚本并不经过它**——两条链路是分开的。

另一路：`app/llm_client.py` 通过 **Agent Service** 的 `POST /v1/infer/stream`（SSE）做 LLM 推理（整个流水线恰好 2 次：头推理 + 尾终裁）。

> 当前在跑：Agent Service 7988(127.0.0.1)/7989(0.0.0.0)、Android Use MCP 7998、挂号推荐 web 8765。

## 1. 目标

新写一个**最简 Android App**，复刻 挂号推荐 web 的功能（**表单 / 实时日志 / 结果 / 历史** 四个页面），
LLM 仍走 **Agent Service**（`/v1/infer/stream`），**所有设备操作改成 App 内的 `AccessibilityService`**
（唤起随申办、读屏、输入、点击、滑动、等待）。最终打包 APK，`adb install` 到真机测试。

目标 App（被操作方）：随申办 `mobi.w3studio.apps.android.shsmy.phone`（已登录 + 已配置）。

## 2. adb 原语 → AccessibilityService 映射（要落地的能力面）

| # | 现状（adb，来自 Adb 类） | 语义 | AccessibilityService 实现 |
|---|---|---|---|
| 1 | `monkey -p PKG -c LAUNCHER 1` / `am force-stop PKG` | 唤起/杀死随申办 | `packageManager.getLaunchIntentForPackage(PKG)` + `startActivity`；`force-stop` 无对应，用 `GLOBAL_ACTION_HOME`/重启或忽略 |
| 2 | `uiautomator dump` → XML(text/class/bounds) | 读屏 | `getRootInActiveWindow()` 递归 `AccessibilityNodeInfo` → 节点快照(text/contentDescription/className/boundsInScreen/clickable/scrollable/visible) |
| 3 | `input tap x y` | 点击 | 优先 `node.performAction(ACTION_CLICK)`；否则 `dispatchGesture(单点 Path)` |
| 4 | `input keyevent 4` | 返回 | `performGlobalAction(GLOBAL_ACTION_BACK)` |
| 5 | `input keyevent k` | 按键 | 多数无对应；HOME/RECENTS/BACK 用 `performGlobalAction` |
| 6 | `input swipe x1 y1 x2 y2 dur` | 滑动 | `dispatchGesture(Path + StrokeDescription, dur)` |
| 7 | `input_cjk`（ADBKeyboard 广播 `ADB_INPUT_B64`/`ADB_CLEAR_TEXT`） | 输入中文 | `node.performAction(ACTION_SET_TEXT, bundle)`；不行则 `ACTION_FOCUS` + 注入 IME（自研 IME 或 `ACTION_PASTE`） |
| 8 | `shell date` | 设备时间 | `System.currentTimeMillis()/Calendar` |
| 9 | `wm size` | 屏幕尺寸 | `windowManager.currentWindowMetrics.bounds` |

## 3. LLM 接口（保持不变）

`POST {base}/v1/infer/stream`，body `{model_id, messages}`（无 session_id），SSE：`event: init` → `data:{content,thinking,…}` → `data:[DONE]`。
累计 `content` 为答案，`thinking` 只记长度。base 解析同 `app/llm_client.py::resolve_base_url`（RUNTIME_SERVER_URL/AGENTS_URL/默认 127.0.0.1:7988）。
两次调用：头推理（权重+目标科室 JSON）与尾终裁（白名单内定 top3）。契约见 `docs/LLM_CONTRACT.md`。

## 4. 页面（复刻 app/web/index.html）

1. **表单**：症状描述 / 就诊人下拉（AccessibilityService 只读采集，替代 /api/patients）/ 费用期望档 / 位置（手填坐标或地址，替代 /api/device-loc）/ --live 开关。
2. **实时日志**：流水线 stdout 逐行滚动（对应 /api/stream）。
3. **结果**：推荐 1–3 家（why+evidence）+ Top20 + 号源来源标注 + 模拟挂号步骤（对应 /api/result、/api/report）。
4. **历史**：历史任务列表 + 详情（对应 /api/jobs）。

## 5. 约束与风险

- 干活的机器**未发现 Android SDK / Gradle**（有 JDK21、Python3.11、adb）。构建 APK 需评估/搭建工具链（cmdline-tools + build-tools + platform；可能需要联网）。
- 真机：**本次只用 Redmi 25098RA98C（1080x2392, serial `476cf991`）**——另一台 MI 5X（`0043d38b0504`）正在跑任务，**禁止占用/打断**。
- 因此本次所有 `adb` 命令一律带 `-s 476cf991`；构建 targetSdk 需能被该机（较新 Android/HyperOS）安装。
- 无障碍需用户一次性在系统设置授权；国产 ROM（MIUI/HyperOS）可能多一层「允许执行操作」勾选；息屏/锁屏时手势同样受限。
- 读屏节点是 WebView 内容，节点 class 可能随机型不同（旧机 `android.view.View`）。

## 6. 待确认

- 流水线「在哪跑」：(A) 端上自包含（推荐，符合“摆脱电脑/目标移动端”）；(B) 手机只做“设备代理”，重排逻辑留服务端（需服务端回调手机的桥）。
- (A) 之下再定实现：(A1) Kotlin 原生 port 打分/候选；(A2) Chaquopy 内嵌 CPython 直接复用 app/*.py（最忠实，APK 增约 10–20MB，需 Gradle 插件）。

## 7. 进展（P0–P2 已在真机 476cf991 验收通过）

- **源码**：`mobile/controller-app/`（Java，零 Gradle）；`build.ps1` 一键打包；产物 `hh-controller.apk`（~37.5KB，debug 签名）。
- **工具链**：`D:\android-sdk`（build-tools 30.0.3/34.0.0、platforms android-27/34、cmdline-tools）。
- **踩坑（重要）**：① `d8`（28–34）读 JDK21 javac 写的 `MethodParameters(<no name>)` **NPE** → 改用 `dx`（build-tools 30.0.3）；② `build.ps1` 缺 UTF-8 BOM → PS5.1 按 GBK 读坏；③ `accessibility_service_config` 的 `typeWindowViewChanged` 是**非法 token**；④ CodeMaster 幻觉常量 `ACTION_PERFORM_EDITOR_ACTION`/`ACTION_ARGUMENT_EDITOR_ACTION`（framework 里不存在）已删。
- **无障碍**：`settings put secure enabled_accessibility_services … + accessibility_enabled=1` 在 HyperOS 上**ADB 直接生效**（无需 UI 手点）；`dumpsys accessibility` 显示 Bound。
- **连通（关键限制）**：PC 无物理网卡（仅 Hyper-V 网桥 192.168.2.5 + WireGuard），手机 ping 不通；开防火墙规则需管理员。当前靠 **`adb reverse tcp:7989 tcp:7989`**（USB）→ App base = `http://127.0.0.1:7989`。**要真正摆脱 USB，仍需可路由的 LAN + 放行 TCP 7989。**
- **P0 读屏**：App 抓 111 节点 vs uiautomator 109；根节点 bounds/class/package/rotation 一致；一致率 98.2%；WebView 容器 text 为空属正常（文本在子 View）。
- **P2 LLM**：App 内经 SSE 跑通一次 JSON 推理（`model_id=default`）。

### 7.1 P3 端上流水线（Gradle + Chaquopy，CPython 3.13）
- 包名 `com.hospitalhelper.controller`；python srcDir = `app/python/`；数据资产 `app/src/main/assets/data/`（~19MB）；APK **~35MB**；一次 `gradlew assembleDebug` ≈ 26s（热缓存）。
- 端上 Python 由 `runner.run(spec, base, key)` 驱动 9 段；`devbridge.py` 经 `PyBridge.java` 调 AccessibilityService。
- **非-live 端到端 ✅**：`rc=0`，出 3 家推荐（中山医院/上海市中医医院·芷江院区/新华医院），`slot_source=cache/level_default(未核实)`，落历史。
- **Agent Service**：App 配置项 URL+API_KEY；默认 `http://192.168.2.5:7989/`、key 空（未配 auth_token.json 时鉴权关）；**手机直连 192.168.2.5:7989 返回 200**（TCP 放行，无需 adb reverse）。
- **设备操作（AccessibilityService）已验证**：读屏、CJK 输入 `ACTION_SET_TEXT`、tap/swipe/back；**launch/拉前台已修复**（`Adb.dump()` 空窗口不再抛、新增 `ensure_foreground()`），HHDIAG 证实窗口列表出现 `mobi.w3studio…` 且 `launch->true`。
- **live 采号仍 0 行**：随申办**已登录**（我在 476cf991 dump 到它停在「预约挂号」页：搜索框/华东医院/相关医生·专病门诊），但端上没走到 `全部医院(444)` 列表页 → `ensure_444` 各条件组 `nav_fail`。这是**移植后导航状态机**的适配问题（原 adb 版假设人已把随申办停在 444）。
