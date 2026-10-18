# BRIDGE.md — Python ⇄ Java 接口契约 · 文件布局 · 数据资产（P3）

> 架构：**9 段流水线放端上、用 Python 实现**，经 Chaquopy 17（内嵌 CPython 3.13）跑；
> 设备操作全部改走 `AccessibilityService`（不再 adb/uiautomator）。
> 本文件是 Python 与 Java 两侧的唯一契约。冻结工程（`D:\Sources\hospital_helper\app …`）
> **只读拷贝、绝不回改**；端上改动只发生在 `mobile/controller-app/`。

---

## 1. Java ⇄ Python 边界（只用 String / int / boolean，绝不传 Java 对象）

### 1.1 Java → Python（唯一调用点）

```java
// MainActivity（后台线程）
Python py = Python.getInstance();
PyObject mod = py.getModule("runner");
PyObject out = mod.callAttr("run", spec_json /*String*/, base /*String*/, api_key /*String*/);
String res = out.toJava(String.class);   // JSON 字符串
```

`runner.run(spec_json, base, api_key) -> result_json`

- `spec_json`（UTF-8）：`{symptom_text, loc|loc_text, fee_expect, patient|null, patient_selected, metro_priority, live}`
  （与 `app/web.py` 表单同一 schema）。
- `result_json`：`{ok:bool, rc:int, error:str|null, out_dir:str, result:{…完整 result.json…}|null, checks:[…]}`
  —— 任何 Python 异常都包进该 JSON，**不向 Java 抛**。

### 1.2 Python → Java（`devbridge.py` 经 `from java import jclass` 调 `PyBridge` 静态方法）

| PyBridge（Java，static） | 返回 | 语义 / 端上实现 |
|---|---|---|
| `init(Context)` / `setListener(Listener)` | — | Activity 启动时注册（日志/结果回调） |
| `launch()` | boolean | 唤起随申办（`getLaunchIntentForPackage`+CLEAR_TOP ≈ 旧 `monkey`） |
| `isReady()` | boolean | 无障碍服务是否 Bound |
| `dumpJson()` | String | **读屏 → 与 uiautomator dump 同构的 XML 文本**（见 §3 命名说明） |
| `tap(int,int)` | boolean | `dispatchGesture` 单点（≈`input tap`） |
| `swipe(int,int,int,int,int)` | boolean | `dispatchGesture` 路径（≈`input swipe`） |
| `back()` | boolean | `GLOBAL_ACTION_BACK`（≈`keyevent 4`） |
| `keyevent(int)` | boolean | 3→HOME、4→BACK、66→ENTER（其余 no-op） |
| `pressEnter()` | boolean | 焦点 EditText / 页面「搜索」节点（≈`keyevent 66`） |
| `inputText(String)` | boolean | `ACTION_SET_TEXT`（中英文通吃，**不依赖 ADBKeyboard**） |
| `waitForText(String,long)` | boolean | 事件驱动（WINDOW_\*）+ 400ms 轮询 + 超时 |
| `screenSize()` | String | `"w,h"`（≈`wm size`） |
| `now()` | String | 设备本地时间 ISO-8601（≈`shell date`） |
| `emitLog(String)` | — | 逐行回吐 UI 日志（`runner` 捕获 stdout → 此） |
| `setResult(String)` | — | 结果 JSON 推送 UI |
| `getConfig(String)` / `setConfig(String,String)` | String / — | SharedPreferences `hh_config`（`llm_base`/`api_key`/`amap_key`） |
| `dataDir()` | String | 只读数据根：`assets/data` 解压到 `filesDir/data` |
| `outDir()` | String | 可写产物根：`filesDir/out` |
| `extractAssetsIfNeeded()` | String | 幂等解压（`_version` 戳变化才重解压） |

> `isReady()` / `keyevent(int)` 是在任务给定门面之上的**最小必要扩展**：前者供
> `devbridge.is_ready()` / `Adb.get_state()` 判定读屏桥可用性；后者是 `devbridge.keyevent(k)`
> 原语的落点。二者边界仍只走 String/int/boolean。

### 1.3 `devbridge.py`（Python 侧门面，等价旧 `Adb`）

`devbridge.dump()/tap()/back()/keyevent()/swipe()/input_text()/launch()/device_date()/screen_size()`
+ `is_ready()/emit_log()/set_result()/get_config()/set_config()/wait_for_text()/press_enter()`。
`dump()` 直接返回 XML，供 `patients.nodes()` / `fetch_slots.parse_nodes()` 既有正则零改动。
桌面（无 Chaquopy）下 `import devbridge` 不报错，仅调用原语时抛 `DeviceError`。

---

## 2. 端上文件布局

```
mobile/controller-app/
├─ app/build.gradle                 # Chaquopy 17.0.0 / srcDir "python" / CPython 3.13
├─ app/src/main/AndroidManifest.xml # application=PyApplication；SsAccessibilityService；已删 PySmokeActivity
├─ app/src/main/java/com/hospitalhelper/controller/
│   ├─ PyBridge.java                # ★新增：Python↔Java 静态门面（§1.2）
│   ├─ MainActivity.java            # ★改：+Agent Service URL/API_KEY；「开始」后台跑 runner.run
│   ├─ AgentClient.java             # ★改：chatJson/streamChat 支持 Bearer api_key
│   ├─ AccDeviceBridge / SsAccessibilityService / DeviceBridge / NodeTree / TaskEngine /
│   │   HistoryStore / ForegroundKeeperService     # 复用（不动）
│   └─ (PySmokeActivity.java 已删除)
├─ app/src/main/res/…               # ★改：activity_main.xml(+api_key)；strings.xml；删 activity_py_smoke.xml
├─ app/src/main/assets/data/…       # ★新增：只读数据资产（19MB，见 §4）
└─ app/python/                      # Chaquopy srcDir（Python 代码根）
    ├─ hhpaths.py                   # ★新增：配置/路径引导（DATA_DIR/OUT_DIR；sys.path bootstrap）
    ├─ devbridge.py                 # ★新增：设备桥 Python 门面（§1.3）
    ├─ runner.py                    # ★新增：唯一入口 run(spec_json,base,api_key)
    ├─ BRIDGE.md                    # 本文件
    ├─ app/                         # 业务包（拷贝自冻结 app/，按端上改造）
    │   ├─ main.py                  # 9 段流水线；--out 默认走 OUT_DIR；删 --loc-adb
    │   ├─ slots.py                 # live/probe：去 subprocess → in-process fetch_slots.run_conditions
    │   ├─ infer.py finalize_llm.py candidates.py recommend.py dialogue.py book.py
    │   ├─ llm_client.py            # base/api_key 从配置读；api_key 非空加 Authorization: Bearer
    │   ├─ locate.py                # 去 --loc-adb/dumpsys；手填坐标或高德/stub
    │   ├─ patients.py              # Adb→devbridge（就诊人只读采集）
    │   └─ amap.py                  # .env/缓存路径→hhpaths；key 可来自配置
    ├─ slots/                       # fetch_slots.py / six_fields.py（拷贝+改造）
    └─ ranking/scripts/score_hospitals.py   # stage-1（拷贝+改造：ROOT/OUT 走 hhpaths）
```

**关键路径约定**
- 只读数据：`hhpaths.DATA_DIR` = `PyBridge.dataDir()`（assets 解压 → `filesDir/data`，
  镜像旧工作区布局：`ssb_dept/ hospital_intel/ slots/ app/ ranking/`）；`hhpaths.WS == DATA_DIR`。
- 可写产物：`hhpaths.OUT_DIR` = `PyBridge.outDir()`（`filesDir/out`）：
  `out/run/result.json`、`out/run/report.md`、`out/ranking/out/feature_norms.json`、
  `out/slots/out/…`（观察快照与 `condition_runs/`）、`out/cache/amap/*`。
- 日志：`runner` 捕获 stdout 逐行 → `PyBridge.emitLog` → MainActivity 日志区；`fetch_slots.log()` 仍写 `out/slots/out/fetch.log`。
- 兼容 CPython 3.13：无 `datetime.utcnow` / `distutils` / `imp` / `pipes` 等弃用项（已核）。

---

## 3. `dumpJson()` 命名说明

契约名叫 `dumpJson()`，但**载荷是与 uiautomator dump 同构的 XML 文本**
（`<hierarchy rotation="N"><node text=".. " class=".." bounds="[l,t][r,b]" …/></hierarchy>`）。
原因：端上 `patients.py`/`fetch_slots.py` 的解析器沿用旧 XML 正则，返回 XML 可**零改动**复用；
JSON 树（`AccDeviceBridge.dump()`）仅作中间态，由 `PyBridge` 序列化为 XML。

---

## 4. 数据资产清单与体积（`app/src/main/assets/data/`）

| 资产 | 来源（冻结，只读拷贝） | 体积 |
|---|---|---|
| `hospital_intel/dept_index.json` | `hospital_intel/dept_index.json` | ~3.2 MB |
| `hospital_intel/manifests/*.json`（451 个） | `hospital_intel/manifests/` | ~11 MB |
| `ssb_dept/triage/SYMPTOM_LEXICON.json` | `ssb_dept/triage/SYMPTOM_LEXICON.json` | ~892 KB |
| `slots/profiles/slot_profile.json` | `slots/profiles/slot_profile.json` | ~3.5 MB |
| `slots/profiles/level_defaults.json` | `slots/profiles/level_defaults.json` | ~73 KB |
| `app/fixtures/{demo_cases,geocode_stub,stub_slots}.json` | `app/fixtures/` | ~16 KB |
| `_version` | 生成戳（`p3-data-v1`） | — |
| **合计** | | **≈ 19 MB**（APK 内 assets，首次启动解压到 filesDir） |

> 未打包：`hospital_intel/.env`（含密钥，绝不入库；高德 key 改由配置 `amap_key` 注入）、
> `slots/out/`（历史观测快照，属产物）、`app/cache|out/`（PC 运行产物）、`ranking/out/feature_norms.json`
> （端上首次运行由 `score_hospitals.load_or_build_norms` 重建到 `out/ranking/out/`）。

---

## 5. 端上改造要点（相对冻结工程的差异）

| 模块 | 改动 |
|---|---|
| `devbridge.py`（新） | 全部设备原语 → `PyBridge` 静态方法；`dump()` 返回 XML |
| `patients.py::Adb` | `subprocess.run(['adb',…])` → `devbridge.*`；`run()` 仅保留极小兼容面 |
| `slots/fetch_slots.py::Adb` | 同上；新增 `run_conditions(...)`（in-process 抓取入口） |
| `slots/fetch_slots.py` | `WS/OUT_DIR/MANIFEST_GLOB` → `hhpaths`；`_ensure_ime*` → no-op（走 `ACTION_SET_TEXT`） |
| `app/slots.py::prepare_live/prepare_probe` | 去 `subprocess.run([sys.executable, FETCH_V2, …])` → `from slots import fetch_slots; fetch_slots.run_conditions(…)` |
| `app/locate.py` | 删 `--loc-adb` / `locate_from_adb` / dumpsys；保留 `--loc` 与 `--loc-text`（高德→stub） |
| `app/llm_client.py` | `resolve_api_key()`；`stream_chat/chat_json` 加 `api_key`；非空加 `Authorization: Bearer` |
| `app/{main,amap,dialogue,candidates}.py` | 路径常量 → `hhpaths`（只读分离 / 产物写 OUT_DIR） |
| `ranking/scripts/score_hospitals.py` | `ROOT/OUT_DIR/EVAL_DIR` → `hhpaths` |
| `runner.py`（新） | 组 args/namespace 调 `main.run_pipeline`；stdout 逐行回吐；返回 result_json |

**9 段语义不变**：①位置 → ②病情 → ③权重推理(LLM#1) → ④候选集 stage-1 → ⑤号源(probe/deep) →
⑥终裁(LLM#2，仅 live 深核时) → ⑦模拟挂号(dry-run) → ⑧A/B/C/D 断言 → ⑨落盘；号源 provenance
与「未核实不决定生死」铁律均保留；无 live 时 ⑥ 自动回退确定性 top3。

---

## 6. 最小闭环自检（桌面，无设备/无 Agent Service）

`/tmp/smoke_p3.py`（本机验证，用 LLM 打桩）：
- **A** import 全模块不报错；assets 解压目录含 `dept_index/manifests/_index/SYMPTOM_LEXICON`。
- **B** 表单 → `infer.infer_llm` → 头推理出目标科室（`scenario=red_flag`、`llm_depts=['心血管内科','内科']`）。
- **C** `runner.run(...)` 端上入口跑完 9 段 → 出 3 家推荐，A/B/C/D 断言全 PASS。

真机（476cf991）验证项：无障碍 Bound → `dumpJson` 与 uiautomator 同构 → `inputText/pressEnter` 检索 →
`runner.run` 两次真 LLM（Agent Service `http://192.168.2.5:7989/`）→ 推荐 1–3 家。
