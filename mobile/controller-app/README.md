# controller-app — 挂号助手（移动端 App）

把 PC 原型（`hospital_helper` 的 adb + web 链路）整体搬进手机的**自包含 Android App**：端内嵌 CPython，直接跑同一条 9 段流水线；设备操作用自建 AccessibilityService 替代 adb + uiautomator。PC 原型的旧链路（`app/`、`slots/`、`ranking/`、`hospital_intel/`、`app/web.py`）**冻结不动**，仍作离线参考与对照。

- 目标被操作 App：随申办 `mobi.w3studio.apps.android.shsmy.phone`
- 包名 `com.hospitalhelper.controller`，版本 `0.2.8-chaquopy`（`versionCode 9`）
- **接口归集（2026-10-14）**：端上不再直连任何上游，三类接口统一走自建服务端
  `https://hh.ei.ac.cn:51120`（写死，见 `../server/README.md`）：
  LLM `POST /v1/infer`（原 Agent Service `http://192.168.2.5:7989` + API_KEY 已废弃，端上无 key）、
  定位 `GET/POST /v1/location`（原高德 key 不再下发到端上）、APK 自更新 `GET /v1/app/version|download`
- 技术栈：**Java + Gradle + Chaquopy（内嵌 CPython 3.13）**；`app/python/` 是端上跑的流水线移植版
- **安全红线**：端上自动化**只点科室/子tab/日期/返回**，**绝不点「去预约/提交/支付」**；不真挂号、不提交、不支付。

---

## 1. 目录

```
mobile/controller-app/
├── build.gradle                 根：声明插件版本（AGP 8.2.2 + Chaquopy 17.0.0, apply false）
├── settings.gradle              仓库：阿里云公共/谷歌镜像 + Chaquopy 镜像；include ':app'
├── gradle.properties            JVM 参数、android.useAndroidX=false
├── gradlew / gradlew.bat        Gradle 包装（构建用这个，不用 build.ps1）
├── local.properties             本机 SDK 路径（机器相关，勿外发）
├── build.ps1 / build.py / do_build.bat   早期零-Gradle 打样脚本（现以 Gradle 为准，可当历史参考）
├── _legacy/                     P0 骨架期旧 APK 与资源（历史存档）
└── app/
    ├── build.gradle             com.android.application + com.chaquo.python；compileSdk 34, minSdk 24, targetSdk 34；
    │                            ABI 只留 arm64-v8a / x86_64；chaquopy.defaultConfig.version "3.13"
    ├── python/                  ← 内嵌 Python（Chaquopy 会打包进 APK）
    │   ├── runner.py            Java 唯一调用点：getModule("runner").run(spec, base, key) -> JSON
    │   ├── devbridge.py         端上设备桥（供 Python 侧调）
    │   ├── hhpaths.py           端上路径/落盘目录
    │   ├── selftest.py          端上自检（只读挂号源）
    │   ├── app/                 PC 原型流水线移植（main/infer/slots/recommend/finalize_llm/patients/
    │   │                        preflight/auto_locate/locate/dialogue/book/candidates/llm_client/amap…）
    │   ├── ranking/             stage-1 引擎（score_hospitals.py，import 复用）
    │   └── slots/               号源（fetch_slots.py v2 + six_fields.py）
    └── src/main/
        ├── AndroidManifest.xml  权限/queries/无障碍 service
        ├── java/com/hospitalhelper/controller/
        │   ├── MainActivity.java            UI：5 Tab（挂号/实时日志/结果/历史/设置）+ 前置检查门禁
        │   ├── Preflight.java               前置检查（读屏/安装/登录/悬浮窗/定位；严格门禁）
        │   ├── PyBridge.java                Java↔Chaquopy 桥（run/日志回吐/配置）
        │   ├── AccDeviceBridge.java         AccessibilityService 版设备桥（点击/滑动/输入/取树）
        │   ├── SsAccessibilityService.java  读屏桥服务本体 + 事件源
        │   ├── NodeTree.java                AccessibilityNodeInfo → uiautomator 同构 JSON
        │   ├── DeviceBridge.java            设备桥接口
        │   ├── AgentClient.java             SSE / 严格 JSON 客户端（LLM，走 /v1/infer）
        │   ├── UpdateChecker.java           APK 自更新：查 /v1/app/version + 下载 + sha256 校验
        │   ├── ApkProvider.java             FileProvider：把下载好的 APK 交给系统安装器
        │   ├── HistoryStore.java            历史（持久化，富渲染可回放）
        │   ├── ResultRenderer.java          结果/历史富渲染
        │   ├── TaskEngine.java              任务状态机（流水线/就诊人采集/前置检查互斥）
        │   ├── ForegroundKeeperService.java 前台服务兜底（国产 ROM 后台启动受限）
        │   ├── MiniChip.java                运行中悬浮小胶囊（示意未退出）
        │   ├── ServiceGate.java             无障碍自授/自启（有 WRITE_SECURE_SETTINGS 时一键开启）
        │   ├── SelfTest.java                端上自检
        │   └── SelfTestReceiver.java        自检广播接收
        └── res/
            ├── layout/activity_main.xml     主界面（5 Tab + 表单 + 检查卡）
            ├── values/strings.xml           文案（Tab、表单、检查卡、设置…）
            ├── values/colors.xml / styles.xml  样式
            ├── xml/accessibility_service_config.xml  无障碍能力位
            └── drawable/                    背景/箭头等
```

---

## 2. 构建（Gradle + Chaquopy）

前置：JDK 21 + Android SDK（`build-tools` + `platform android-34`）。仓库走阿里云/Chaquopy 镜像。

```bat
cd mobile\controller-app
call gradlew.bat assembleDebug --console=plain
:: 产物: app\build\outputs\apk\debug\app-debug.apk   (~34 MB)
```

> 说明：`app/python/` 下 CPython 依赖（CPython 运行时/启动器）由 Chaquopy 插件自动注入；
> Chaquopy 17 只把 API jar 加到**运行期**，未加到**编译期** classpath，
> 故 `app/build.gradle` 用 `compileOnly 'com.chaquo.python.runtime:chaquopy_java:17.0.0'` 补齐（不重复打包、避免 duplicate class）。
> 早期 `build.ps1`（零-Gradle aapt2+d8+apksigner）已不用于正式出包，仅作打样参考。

---

## 3. 安装 & 授权

```bat
adb -s 476cf991 install -r app\build\outputs\apk\debug\app-debug.apk
```

装完/重装后**必须重新授权**（HyperOS/MIUI 会清无障碍绑定与悬浮窗 appop），否则读屏与门禁都会失败：

```bat
set P=com.hospitalhelper.controller
adb -s 476cf991 shell settings put secure enabled_accessibility_services %P%/%P%.SsAccessibilityService
adb -s 476cf991 shell settings put secure accessibility_enabled 1
adb -s 476cf991 shell appops set %P% SYSTEM_ALERT_WINDOW allow     :: 悬浮窗提醒(Keeper)
adb -s 476cf991 shell appops set %P% START_FOREGROUND allow
adb -s 476cf991 shell dumpsys deviceidle whitelist +%P%
:: 可选：有 WRITE_SECURE_SETTINGS 时，App 内「去开启」可一键自授
adb -s 476cf991 shell pm grant %P% android.permission.WRITE_SECURE_SETTINGS
```

> HyperOS 手动开无障碍的路径：辅助功能 →（滚到底）权限管控 → 已下载的应用 →「挂号助手 · 读屏桥」(已关闭) → 开关。
> 弹「危险」风控框需**勾选风险确认 + 等 5 秒倒计时**后「确定」才可点，否则不生效。
> 详情与实测坑见根 `README.md` 的「手机端设备前置条件」与 `MEMORY.md`。

---

## 4. 功能面（5 Tab）

| Tab | 内容 |
|---|---|
| **挂号** | 症状描述（必填）／预期费用（Spinner）／号源子tab（Spinner）／位置（必填，可自动定位）／就诊人（Spinner，「获取就诊人」只读采集随申办列表，脱敏，首项=代他人挂号）／AI 实时读取开关＋预计耗时／「获取号源推荐」（固定底部，受门禁） |
| **实时日志** | 流水线 stdout 逐行回吐（`PyBridge.emitLog`） |
| **结果** | 富渲染推荐表 + A/B/C/D 断言 |
| **历史** | 持久化历史，可回放完整详情（非摘要） |
| **设置** | 归集服务端地址（只读提示 `https://hh.ei.ac.cn:51120/`，已写死；API_KEY 输入框已删除）/ 悬浮窗提醒 / 候选集数量 / 最终推荐科室数量 / **检查更新**（APK 自更新）/ 调试按钮（唤起随申办、读屏、测试 LLM、自检） |

**前置检查门禁**（`Preflight.java` + `app/python/app/preflight.py`）：
- 必需项：读屏权限、随申办已安装、随申办登录（深检，只读导航，会短暂把随申办拉到前台）。
- 可选项：悬浮窗提醒、定位。
- **严格门禁**：必需项只要有一项非 OK（含 UNKNOWN/WARN）→ 表单**递归 `setEnabled(false)` 禁用但保持可见**（`setEnabledRecursive` + alpha 0.55）；`btn_run` 在 `form_body` 之外，单独按门禁启停。全绿 → toast「检查完成，可以使用了」＋检查卡自动收起（带 generation 计数防抖动）。
- 冷启动自动登录深检（`maybeAutoProbeLogin`）：仅当 登录=UNKNOWN 且 读屏+安装 OK 时触发，避免 UNKNOWN 永久卡死门禁。

---

## 5. 近期关键修复（下拉框失灵根因）

**现象**：`fee / slottype / patient` 三个原生 `Spinner` 恢复后仍「点了没反应」——touch DOWN 到达 Activity、命中链走到 Spinner、`handled=true`，但 Spinner 的 `OnTouchListener` 不触发、弹窗永远弹不出。

**根因**：`Spinner` 的选中项渲染在它内部一个 `TextView`（`@android:id/text1`）上。门禁递归 `setEnabledRecursive` 过去除了 `setEnabled` 还调了 **`setClickable(enabled)`**——门禁一开就把那个内部 TextView 变成 `clickable=true`，于是它把 DOWN/UP 全部吞掉，Spinner 本体再也收不到点击 → 弹窗永远弹不出来。

**修复**：`setEnabledRecursive` 只保留 `setEnabled`，**去掉 `setClickable`**。禁用交互只靠 `setEnabled(false)` 同样成立（disabled 的可点击控件会「消费但不响应」，门禁依旧严格）。三个原生 Spinner 随之恢复可点击、可选（真机 9/9 通过：fee 7 项、slottype 4 项、patient 3 项，选择均生效）。

**附带加固**：本 ROM 会在 `uiautomator dump`/进程回收时把无障碍服务 unbind→再 bind（约 1.2s）。严格门禁曾因此让表单在用户眼前反复开合；现在「已启用但瞬时未绑定」（WARN）给 8 秒宽限（`gateOpenedAt`），只有系统里**真正关掉**（BAD）才立即关闭。

---

## 6. 端上自测

- **冷启动**（无障碍 OFF）：表单应**可见但禁用**，检查卡列出未满足必需项。
- **授权后**：约 12s 自动登录深检通过 → 检查卡收起、表单启用；三个 Spinner 点开均有原生列表、可选。
- **自检**（设置 Tab → 自检）：端上只读挂号源，验证读屏桥/回前台链路。

---

## 7. 已知约束 / 待办

- 本 ROM `uiautomator dump` 会解绑无障碍（约 1.2s 后自绑回）——验证脚本每次 dump 后需等它重绑、门禁重开再点。
- HyperOS SmartPower 会挂起「不可见」进程 2~5 分钟（需白名单 / Keeper 悬浮窗维持可见 adj，见根 README）。
- LLM 不可达 → 受控退出（不静默降级）；科室不编造（归一化词表校验 + 纠错重试）。
- 发布需出 release 签名包（`gradlew assembleRelease`）。

---

## 8. 接口归集（2026-10-14）与 APK 自更新

服务端代码在仓库根 `server/`（Python 原生 threaded http server，零依赖），部署后对外只有
**一个**地址：`https://hh.ei.ac.cn:51120`。端上三处写死同一常量：

| 端上位置 | 原实现 | 现实现 |
|---|---|---|
| `AgentClient.java`（`DEFAULT_BASE` / `PATH_INFER`） | 直连 Agent Service `http://192.168.2.5:7989/v1/infer/stream` + `Authorization: Bearer <API_KEY>` | `POST https://hh.ei.ac.cn:51120/v1/infer`（`stream:true`），**端上不持 key** |
| `app/python/app/llm_client.py` | 同上（Python 侧 SSE） | 同上（`DEFAULT_BASE='https://hh.ei.ac.cn:51120'`，`/v1/infer`） |
| `app/python/app/amap.py` | 端上直连高德 `restapi.amap.com`（需 AMAP_KEY） | `GET/POST /v1/location`（服务端转调高德；显式配了 key 时才直连，便于桌面离线对照） |

* 设置页的 `agentsvc_base` 输入框与 API_KEY 输入框已移除；`MainActivity.saveConfig()` 仍写
  `api_key=""` 以兼容旧 SharedPreferences。`--agentsvc-base` / `HH_SERVER_URL` 只是本地联调覆盖口。
* **APK 自更新**：设置 Tab →「检查更新」→ `GET /v1/app/version?current_version_code=<BuildConfig.VERSION_CODE>`
  → 有新版则下载（`/v1/app/download`，支持 Range）→ **sha256 校验** → 经 `ApkProvider`（FileProvider）
  拉起系统安装器；`force_update=true` 时对话框不可取消。需要 `REQUEST_INSTALL_PACKAGES` 权限。
* 发版：`curl -T app-debug.apk -H 'X-Admin-Token: …' 'https://hh.ei.ac.cn:51120/v1/app/upload?filename=…&notes=…'`
  （`versionCode/Name/package` 由服务端从 APK 的 `AndroidManifest.xml` 自动解析）。详见 `server/README.md`。
* 本机联调：`python server\server.py --no-tls --host 127.0.0.1 --port 51120`，再把端上三处常量临时指向
  `http://<局域网IP>:51120`（App `usesCleartextTraffic=true`，明文可通）。
