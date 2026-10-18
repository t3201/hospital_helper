# hospital_helper — 挂号推荐运行期原型（全链路交付包）

一句话：把"疾病自述 + 就诊人信息(位置/年龄/性别) + 主观偏好"转化为
**LLM 头推理(权重+科室) → 候选医院集(stage-1) → 号源两段解析(probe/deep, 缓存优先) → 两级终排 + LLM 尾终裁（dry-run 模拟挂号）** 的完整可运行链路。

- 版本：**PC 原型 v0.2（保留作离线参考）＋ 移动端「挂号助手」v0.2.0-chaquopy（当前主交付物，端上自包含，已可装机）**
- 依赖：PC 原型 **纯标准库, 零第三方依赖**；移动端用 **Gradle + Chaquopy（内嵌 CPython 3.13）** 跑同一条流水线
- 权威契约：`ranking/RANKING_SPEC.md`（stage-1 静态排序 + 两段式接口）
- 上游数据（`ranking/`、`hospital_intel/`、`ssb_dept/`、`slots/`）**只读**, 本包内不修改
- 安全底线：**不真挂号、不提交、不支付**（450 家医院 capabilities.book=false; book 步骤 dry-run）；移动端额外红线：**只点科室/子tab/日期/返回，绝不点「去预约/提交/支付」**

## 目录地图

```
hospital_helper/
├── README.md                  ← 本文件（入口）
├── docs/                      ← 开发文档（接口, 供 APP 开发方 AI 使用）
│   ├── ARCHITECTURE.md        9 段流水线、模块职责、数据流
│   ├── PIPELINE_FLOW.md       全流程矢量流程图（Mermaid→SVG）+ 分阶段说明
│   ├── API.md                 CLI 全参考 + result.json/report.md 输出 schema + 退出码
│   ├── LLM_CONTRACT.md        LLM 接口契约（SSE 传输/prompt 结构/输出 JSON/校验/重试）
│   ├── SLOTS.md               号源 5 层解析、TTL、来源标注、实时抓取现状
│   └── DATA.md                数据字典（每个文件的 schema/用途/出处/更新方式）
├── app/                       ← 运行期原型代码（入口 app/main.py）
│   ├── main.py                9 段流水线编排 + CLI
│   ├── locate.py / amap.py    定位（高德真实 geocoding, 缓存, 离线降级）
│   ├── dialogue.py            病情采集（≤3 轮自由文本; 年龄/性别/代挂/时效/费用/水平预期）
│   ├── infer.py               头推理 = LLM 第 1 次（直出权重+科室, 含基础数据上下文）
│   ├── llm_client.py          Agent Service SSE 客户端（纯标准库）
│   ├── candidates.py          stage-1 复用（import ranking/scripts/score_hospitals.py）→ 候选集 K + SlotQuery 序列
│   ├── slots.py               号源 5 层解析（缓存优先, TTL 90min, 全标注来源）
│   ├── recommend.py           终排 + 可解释 + 回归断言 A/B/C/D + report 渲染
│   ├── finalize_llm.py        尾终裁 = LLM 第 2 次（深核白名单内定 top3, 严格校验+回退）
│   ├── book.py                模拟挂号（dry-run 默认开, 拒绝 --execute）
│   ├── web.py                 web 实测端（ThreadingHTTPServer + SSE, 纯标准库; python -m app.web）
│   ├── web/index.html         web 单页（表单 → SSE 实时日志 → 推荐表/断言/历史, 原生 JS）
│   ├── test_llm_chain.py      离线自检（不依赖 LLM/网络）
│   ├── fixtures/              demo 剧本 / geocode stub 小表 / stub 号源
│   ├── out/                   4 个已验证 demo 的参考输出（result.json + report.md）
│   └── cache/amap/            geocode 落盘缓存（TTL 30 天）
├── ranking/                   ← stage-1 引擎（只读复用）
│   ├── RANKING_SPEC.md        权威规范（§4.3 SlotQuery / §4.5 未核实回落 / §5 号源 schema）
│   ├── scripts/score_hospitals.py   静态排序引擎（import 复用, 禁止重写）
│   └── out/feature_norms.json 438 家医院特征归一缓存（指纹校验, 可自动重建）
├── hospital_intel/            ← 医院基础数据（只读）
│   ├── dept_index.json        归一化科室索引（10,334 部门: canonical/kind/覆盖医院数）
│   ├── manifests/             450 家医院 manifest（名称/等级/类型/区县/坐标/科室清单）
│   └── .env                   AMAP_KEY（机密, 勿外发; 缺省时 geocoding 降级 stub）
├── ssb_dept/triage/
│   └── SYMPTOM_LEXICON.json   症状词表（1,822 词 → 归一化科室 D-ID, 含优先级/置信度）
└── slots/                     ← 号源数据 + 实时抓取（只读, live 除外）
    ├── profiles/slot_profile.json      7,614 条 (科室,医院) 号源画像（670 observed + 6,944 档位先验）
    ├── profiles/level_defaults.json   档位先验（按 等级|类型 分层, 带 confidence, 非实时）
    ├── out/SH-*.json                  10 家抽样医院的观察快照（captured_at, TTL 90min）
    ├── fetch_slots.py (v2)        live 抓取引擎（app `--live` 走 v2 `--condition`; 见 docs/SLOTS.md）
    ├── six_fields.py                v2 伴生模块（六字段 schema, 纯标准库）
    └── fetch_slots_v1_20261004.py   离线全院批量采集工具（v1, app live 不再调用）
├── mobile/                  ← 移动端 App（当前主交付物，见下方专节）
│   └── controller-app/       挂号助手 · 端上自包含 App（Gradle + Chaquopy）
│       ├── app/src/main/java/com/hospitalhelper/controller/   16 个 Java 类（UI/设备桥/前置检查…）
│       ├── app/python/        内嵌 CPython 3.13 跑的 9 段流水线（PC 原型的端上移植）
│       └── app-debug.apk      构建产物（~34 MB, 已可装机）
├── tools/                   ← 工具脚本（fetch_slots 对照、Chaquopy/hello 打样）
├── notes/                   ← 一次性配置快照（如 HyperOS 神隐模式 allowlist 前后值）
└── tmp/                     ← 本会话临时/验证脚本（可随时清空）
```

## 快速开始

```bat
:: 前提: 本目录为工作根; Agent Service 在运行（默认 http://127.0.0.1:7988,
::       或用 RUNTIME_SERVER_URL / --agentsvc-base 指定; 实测实例在 7989）

python -m app.main --demo red_flag     :: 胸痛/红旗（LLM 直出 emergency/level 权重, 5 个目标科室）
python -m app.main --demo chronic      :: 高血压复诊（LLM 抽 price_cap=50）
python -m app.main --demo peds         :: 儿童生长发育
python -m app.main --check             :: 只对已落盘 out/*/result.json 跑回归断言 A/B/C/D（不跑流水线）
python app\test_llm_chain.py           :: 离线自检（不依赖 LLM/网络: 上下文/校验器/run_llm 同构/词表回落）

:: 交互/自由输入
python -m app.main --loc-text "徐汇区宜山路600号" --symptom "高血压复诊, 很多年了, 每次不超过50块"
python -m app.main --loc "121.4737,31.2304" --symptom "胸痛" --non-interactive   :: 管道/CI 安全

:: 结构化病例（web/表单同渠道，跳过自由文本解析，与交互同一 schema）
python -m app.main --loc-text "徐汇区宜山路600号" --spec-file spec.json --non-interactive

:: web 实测端（浏览器打开 http://127.0.0.1:8765/ 填表单 → SSE 实时日志 → 推荐表）
python -m app.web --port 8765
```

每次运行输出到 `app/out/<case>/result.json`（机读, schema 见 docs/API.md）+ `report.md`（人读, 10 章节）。

## 移动端 App「挂号助手」— 当前主交付物（`mobile/controller-app/`）

把 PC 原型整条 9 段流水线搬进手机、自包含运行，不再依赖 adb + PC：

- **架构**：Java App（包 `com.hospitalhelper.controller`）内嵌 **Chaquopy CPython 3.13**，`app/python/` 里的 `runner.py` 直接跑 PC 原型移植版流水线（`app/`、`slots/`、`ranking/` 一并打包进 APK）。
- **设备桥**：App 自建 **AccessibilityService**（`SsAccessibilityService`，标签「挂号助手 · 读屏桥」）替代 adb + uiautomator，对随申办做只读导航 + 点击/滑动/输入。
- **LLM**：走 **Agent Service** `POST /v1/infer/stream`（SSE），base 可在「设置」里改（实测 `http://192.168.2.5:7989/`）。
- **5 个底部 Tab**：`挂号`（表单）／`实时日志`／`结果`／`历史`／`设置`。
- **前置检查门禁**（`Preflight.java` + `app/python/preflight.py`）：必需项＝读屏权限＋随申办已安装＋随申办登录（深检），可选项＝悬浮窗／定位；**必需项未全绿 → 表单 disable 但保持可见**（严格门禁），全绿 → toast + 检查卡自动收起。
- **就诊人**：`获取就诊人` 通过读屏只读采集随申办「就诊人」列表（脱敏），缓存持久；首项＝「代他人挂号」。
- **运行参数**（设置）：候选集数量（Phase A `--probe-n`）、最终推荐科室数量（Phase B `--top`）、预期费用档位、号源子tab、AI 实时读取开关＋预计耗时估算。
- **安全红线**：端上自动化**只点科室/子tab/日期/返回**，**绝不点「去预约/提交/支付」**；不真挂号、不提交、不支付。

> **近期关键修复（下拉框失灵根因）**：`Spinner` 的选中项是其内部一个 `TextView`；门禁递归 `setEnabledRecursive` 过去额外调了 `setClickable(true)`，把它变成可点击 → 吞掉全部触摸，`Spinner` 本体永远收不到点击、弹窗永远弹不出。去掉 `setClickable`（禁用只靠 `setEnabled(false)`，门禁依旧严格）后，`fee / slottype / patient` 三个原生 Spinner 恢复可点击、可选。详见 `mobile/controller-app/README.md`。

### 构建 & 安装（移动端）

```bat
cd mobile\controller-app
call gradlew.bat assembleDebug          :: 需 JDK21 + Android SDK(build-tools+platform)；仓库走阿里云/Chaquopy 镜像
adb -s 476cf991 install -r app\build\outputs\apk\debug\app-debug.apk
```

产物 `app-debug.apk`（~34 MB, `versionName 0.2.0-chaquopy`）。装完/重装后**必须重新授权**（见下节 HyperOS 步骤），否则读屏/门禁会失败。

## 手机端设备前置条件（476cf991 / Redmi HyperOS 1080x2392 实测）

装机后、点「开始」前必须一次性做完，否则端上流水线会在读屏/回前台处反复失败：

```bat
set P=com.hospitalhelper.controller
:: 读屏桥（无障碍）——装完 APK 必须重新授权，否则 getWindows() 拿不到随申办窗口
adb -s 476cf991 shell settings put secure enabled_accessibility_services %P%/%P%.SsAccessibilityService
adb -s 476cf991 shell settings put secure accessibility_enabled 1

:: 【HyperOS 必须】后台弹出界面 = allow（MIUIOP 10021）。
:: 未开时 launch(随申办) 返回 true 但界面根本不弹起 => 读屏为空、搜索通道死循环。
adb -s 476cf991 shell appops set %P% 10021 allow
adb -s 476cf991 shell appops get %P% 10021        :: 期望 MIUIOP(10021): allow

:: 其余 HyperOS 常见拦截（实测需要）
adb -s 476cf991 shell appops set %P% 10017 allow   :: 后台运行/省电限制
adb -s 476cf991 shell appops set %P% SYSTEM_ALERT_WINDOW allow   :: Keeper 防冻结小窗
adb -s 476cf991 shell appops set %P% START_FOREGROUND allow
adb -s 476cf991 shell dumpsys deviceidle whitelist +%P%
```

> 2026-10-09 实测补充：HyperOS SmartPower 会把「不可见」进程挂起 2~5 分钟（logcat 里
> `SmartPower: [com.hospitalhelper.controller] process state changes: idle 204704 ms`，
> 期间 fetch.log/HHDIAG 完全静止，直到读屏服务被系统解绑才醒）。这不是 binder 卡死
> （`enter windowRoot` → `nWindows=` 均在 1~2ms 内）。除上面的 appop/白名单外，
> 还需在系统「应用信息 → 省电策略 → 无限制」里放行，或保持 Keeper 悬浮窗（SYSTEM_ALERT_WINDOW）
> 让进程维持可见 adj；神隐模式白名单：
> `settings put secure immobulus_mode_switch_restrict enable_24_allowlist_com.hospitalhelper.controller_...`
> （在原值 `enable_24_allowlist_` 后追加包名，改前先备份原值）。

## 硬不变量（改代码前必读）

1. **号源标注不得混**：`slot_source ∈ live / cache(observed 已核实) / cache(observed 已过期) / cache(level_default 档位先验) / stub / 未查询`；档位先验与 stub **永远不得**当实时号源事实呈现（RANKING_SPEC §4.5/§5）。
2. **未核实批次 → §4.5 回落**：无可核实实时号源时按 stage-1 静态排序展示, 全部显式标注"号源未核实", 不假装实时。
3. **LLM 不可用 → 受控退出**：rules 回退已废除（2026-10-04 重构）；Agent Service 不可达/输出两次未过结构校验 → 报错退出并给可操作提示, **不静默降级**。
4. **科室不编造**：LLM 科室输出必须逐字取自归一化可约科室词表（dept_index 校验 canonical+kind）; 词表外名称 → 纠错重试 → triage_term 走症状词表 → 仍无 → 报错。
5. **不真挂号**：book 步骤 dry-run 默认开, 拒绝 --execute; 不修改上游只读数据。
6. **回归断言 A/B/C/D 每次运行必跑**, 任一失败 → 非零退出（B4 回归; 断言设计为权重无关, 不受 LLM 非确定性影响）。

## 已知局限（详见 docs/ 各篇与 app/README.md）

- 特征权重由 LLM 直出（5 套场景基线权重仅作 prompt 参考, 未调参无标注数据）; 号源效用系数（0.03/天、0.4×tension）启发式。
- LLM 非确定性: 同一 case 两次运行的权重/科室可能不同; 断言不依赖具体权重值。
- 多院区未建模: 候选粒度=医院, 距离用主院区坐标（haversine）。
- live 号源抓取已接入 v2（2026-10-05）: `--live --device` 批量触发 `--condition` 统一接口, `--live-slot-type` 控采集深度（缺省全部子 tab, 约 5-15 分钟/科室; `expert` 快速档约 1/3 耗时）, `--live-timeout` 控超时上限（默认 60 分钟, 到点优雅停止, 已完成组增量落盘保留）, live 结果回写静态观察快照（迭代收敛）; v1 保留为离线全院批量采集工具。
- geocoding 无 key 时降级内置 stub 小表（仅 3 个演示坐标, method=stub 显式标注）。
