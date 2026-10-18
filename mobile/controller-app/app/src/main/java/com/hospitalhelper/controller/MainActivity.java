package com.hospitalhelper.controller;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.util.Log;
import android.view.LayoutInflater;
import android.view.View;
import android.view.MotionEvent;
import android.view.ViewGroup;
import android.widget.AdapterView;
import android.widget.Spinner;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 单 Activity + 4 区块（表单 / 实时日志 / 结果 / 历史）。
 *
 * <p>P3：9 段流水线在端上经 Chaquopy 跑 {@code python/runner.py::run}；本 Activity：
 * <ul>
 *   <li>表单：症状 / 就诊人 / 预期费用 / 位置 / <b>服务地址（hh-server）</b>。</li>
 *   <li>「开始」→ 后台线程组 spec JSON → {@code runner.run(spec, base, api_key)}。</li>
 *   <li>Python 侧 stdout 经 {@link PyBridge#emitLog} 实时刷新日志；结果 JSON 落历史。</li>
 * </ul>
 *
 * <p>2026-10-14 接口归集：LLM 走自建服务端 {@code https://hh.ei.ac.cn:51120/v1/infer}（服务端注入
 * Agent Service 的 Bearer token），地理位置走 {@code /v1/location}（服务端持有高德 key）。
 * App 侧不再填任何上游 key；{@code api_key} 仅作为兼容旧配置的透传位（默认空）。
 *
 * <p>配置持久化：URL 存 SharedPreferences（{@code hh_config}），与 Python 侧
 * {@code PyBridge.getConfig} 同源（LLM base/api_key 也由 runner 写回该配置）。
 */
public class MainActivity extends Activity {

    private static final int LOG_MAX_CHARS = 24000;
    private static final String PREF = "hh_config";
    private static final String CFG_BASE = "llm_base";
    private static final String CFG_API_KEY = "api_key";
    private static final String CFG_CAND_N = "cand_n";       // 候选集数量（Phase A probe 医院数）
    private static final String CFG_TOP_N = "top_n";         // 最终推荐科室数量（Phase B deep + 终榜）
    private static final int DEFAULT_CAND_N = 12;
    private static final int DEFAULT_TOP_N = 3;
    private static final String DEFAULT_BASE = "https://hh.ei.ac.cn:51120/";

    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final StringBuilder logBuf = new StringBuilder();
    private SelfTestReceiver debugSelfTestReceiver;   // 仅 DEBUG 构建非空
    private LlmPingReceiver debugLlmPingReceiver;     // 仅 DEBUG 构建非空（服务端连通性自检）

    private TextView serviceStatus;
    private TextView logView;           // 细节日志（实时日志页"详细日志"区，默认折叠）
    private TextView reasoningView;     // 模型推理（实时日志页"模型推理"区，恒显，主展示）
    private TextView logDetailToggle;   // "详细日志"折叠开关
    private TextView resultView;
    private LinearLayout historyBox;
    private EditText symptom;
    private EditText loctext;
    private TextView autolocStatus;
    private EditText agentsvcBase;
    private CheckBox live;
    private CheckBox miniChipToggle;
    private Button btnRun;
    private TextView estTime;                 // 预计耗时（实时估算，随勾选/费用/子tab/候选集/推荐数量 变）
    private EditText candN;                   // 设置：候选集数量（Phase A probe 医院数，默认 12）
    private EditText topN;                    // 设置：最终推荐科室数量（Phase B deep + 终榜，默认 3）

    // ---- 前置检查（表单页顶部门禁）----
    private android.widget.LinearLayout checkCard;
    private TextView checkSummary;
    private android.widget.LinearLayout checkRows;
    private Button btnRecheck;
    private android.widget.LinearLayout formBody;
    private java.util.List<Preflight.Item> preflightItems;
    private volatile boolean preflightBusy = false;   // 深检进行中
    private boolean gateReady = false;                // 门禁：必需项全部 OK 才允许操作表单
    private boolean preflightAllOk = false;           // 必需项是否全部 OK（用于"检查完成"提示与自动收起）
    private long gateOpenedAt = 0;                    // 最近一次全绿的时间（无障碍瞬时解绑时给宽限）
    private boolean preflightDismissed = false;       // 检查框已收起（通过/被门控重新弹出都会改它）
    private boolean preflightPassAnnounced = false;   // 本次通过提示是否已发过（避免 resume 反复弹）
    private int preflightGen = 0;                     // 每次 renderPreflight 自增，用于让延迟收起只在该代仍有效时执行
    private boolean lastLoginOk = false;              // 上次深检是否确认已登录（onResume 保留用）
    private String lastLoginDetail = null;            // 上次深检的原始结论文案（onResume 沿用以保留耗时等细节）
    private boolean loginProbeFailed = false;
    /** 本进程内是否已自动跑过一次登录深检：防止 onResume↔深检 乒乓循环反复拉起随申办。 */
    private boolean autoLoginProbeUsed = false;
    private long gateWatchUntil = 0L;                  // 门禁未通过时的自动重扫截止时间
    private final Runnable gateWatcher = new Runnable() {
        @Override
        public void run() {
            if (!resumed || gateReady || preflightBusy) {
                return;
            }
            if (android.os.SystemClock.elapsedRealtime() > gateWatchUntil) {
                return;   // 60s 内还没就绪就不再刷，避免无意义耗电
            }
            Log.i("HH_GATE", "gateWatcher tick（门禁未全绿，1.5s 重扫）");
            refreshPreflightResume();
            ui.postDelayed(this, 1500L);
        }
    };       // 本次进程内深检已失败过：不再自动重试（避免反复拉起随申办）

    /** 是否处于前台（onResume=true / onPause=false）：迷你浮窗仅在退到后台跑任务时挂出。 */
    private volatile boolean resumed = false;
    /** 迷你浮窗开关（存 hh_config/mini_chip，默认开）。 */
    private volatile boolean miniChipEnabled = true;
    private static final String CFG_MINI_CHIP = "mini_chip";

    private volatile boolean running = false;
    private volatile boolean collecting = false;      // 就诊人实采中（设备独占, 与流水线互斥）
    private volatile String lastResultJson = null;
    /** 就诊人 Spinner 选中的完整 dict（对齐 web spec: name_masked/age/gender/insurance_card/source...）。null = 首项「不选择」。 */
    private volatile JSONObject selectedPatient = null;
    /** Spinner 数据源（index 0 固定「不选择」，其后各就诊人），UI 线程维护。 */
    private List<JSONObject> patList = new ArrayList<>();

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        LIVE.set(this);

        PyBridge.init(getApplicationContext());
        PyBridge.setListener(new PyBridge.Listener() {
            @Override
            public void onLog(String line) {
                // 细节日志：写入"详细日志"缓冲（实时日志页默认折叠区）
                log(line);
            }

            @Override
            public void onStep(String text) {
                // 逐步骤：更新浮窗（详细日志由 Python 侧 log() 统一写入，避免重复）
                if (miniChipEnabled) {
                    MiniChip.show(getApplicationContext(), text);
                }
            }

            @Override
            public void onStepState(String text, String st) {
                // 圈5: 浮窗只由已验证状态驱动; 描边色区分 校验中/已验证/校验失败
                if (miniChipEnabled) {
                    MiniChip.showState(getApplicationContext(), text, st);
                }
            }

            @Override
            public void onReasoning(String text, String kind) {
                // 模型推理逐块（kind=think/content/meta）：实时日志页"模型推理"区，恒显
                appendReasoning(text, kind);
            }

            @Override
            public void onResult(String json) {
                lastResultJson = json;
            }
        });

        serviceStatus = findViewById(R.id.service_status);
        logView = findViewById(R.id.log);
        reasoningView = findViewById(R.id.log_reasoning);
        reasoningScroll = findViewById(R.id.log_reasoning_scroll);
        logDetailToggle = findViewById(R.id.log_detail_toggle);
        if (logDetailToggle != null && reasoningView != null) {
            // 详细日志默认折叠；点击展开/收起
            logDetailToggle.setOnClickListener(v -> {
                boolean collapsed = logView.getVisibility() == View.GONE;
                logView.setVisibility(collapsed ? View.VISIBLE : View.GONE);
                logDetailToggle.setText(collapsed ? "▲ 详细日志（点击收起）" : "▼ 详细日志（点击展开）");
            });
        }
        resultView = findViewById(R.id.result);
        historyBox = findViewById(R.id.history_box);
        symptom = findViewById(R.id.symptom);
        loctext = findViewById(R.id.loctext);
        // autoloc_status 视图已从布局移除；字段保持 null，setAutolocStatus 内部已做空判断
        agentsvcBase = findViewById(R.id.agentsvc_base);
        // 2026-10-14 接口归集：API_KEY 输入框已移除（上游凭据由 hh-server 持有）
        live = findViewById(R.id.live);
        btnRun = findViewById(R.id.btn_run);
        estTime = findViewById(R.id.est_time);
        candN = findViewById(R.id.cand_n);
        topN = findViewById(R.id.top_n);

        // 「AI代理实时读取」勾选 + 费用档位 + 子tab + 候选集/推荐数量 任意变化 => 刷新预计耗时
        live.setOnCheckedChangeListener(new android.widget.CompoundButton.OnCheckedChangeListener() {
            @Override
            public void onCheckedChanged(android.widget.CompoundButton b, boolean checked) {
                refreshEstimate();
            }
        });
        // 预期费用 / 号源子tab 的选择由 Spinner 的 onItemSelected 直接刷新预估耗时。
        android.text.TextWatcher tw = new android.text.TextWatcher() {
            @Override
            public void beforeTextChanged(CharSequence s, int a, int b, int c) {
            }

            @Override
            public void onTextChanged(CharSequence s, int a, int b, int c) {
            }

            @Override
            public void afterTextChanged(android.text.Editable s) {
                refreshEstimate();
            }
        };
        candN.addTextChangedListener(tw);
        topN.addTextChangedListener(tw);
        miniChipToggle = findViewById(R.id.mini_chip);
        miniChipToggle.setOnCheckedChangeListener(
                new android.widget.CompoundButton.OnCheckedChangeListener() {
                    @Override
                    public void onCheckedChanged(android.widget.CompoundButton b, boolean checked) {
                        miniChipEnabled = checked;
                        getSharedPreferences(PREF, MODE_PRIVATE).edit()
                                .putString(CFG_MINI_CHIP, checked ? "1" : "0").apply();
                        if (!checked) {
                            MiniChip.hide();
                        } else {
                            // 探针：尝试挂一次再收起，验证悬浮窗权限是否真的可用（HyperOS 上
                            // canDrawOverlays 可能被 MIUI 自己的开关劫持，addView 成功与否才是最终判据）。
                            // 若当前没在跑任务则随即收起，避免在设置页留一个误导性的常驻预览。
                            boolean hasTask = taskBusy();
                            if (MiniChip.finalPending()) {
                                // 结论胶囊在展示中：权限探针别去碰它（探针只是验证，不值得打断结论）
                                return;
                            }
                            MiniChip.show(getApplicationContext(), "挂号助手 · 运行中…");
                            boolean ok = MiniChip.isShown();
                            if (!hasTask) {
                                MiniChip.hide();
                            }
                            if (!ok && Build.VERSION.SDK_INT >= 23
                                    && !Settings.canDrawOverlays(MainActivity.this)) {
                                MiniChip.requestOverlayPermission(MainActivity.this);
                                Toast.makeText(MainActivity.this,
                                        "请允许「显示在其他应用上层」，运行时的角标胶囊才会出现",
                                        Toast.LENGTH_LONG).show();
                            }
                        }
                    }
                });

        setupSpinners();
        loadConfig();
        setupTabs();
        setupPreflight();

        serviceStatus.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                openAccessibilitySettings();
            }
        });

        findViewById(R.id.btn_launch).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                doLaunch();
            }
        });
        findViewById(R.id.btn_dump).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                doDump();
            }
        });
        findViewById(R.id.btn_run).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                doRunPipeline();
            }
        });
        findViewById(R.id.btn_get_patients).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                doGetPatients();
            }
        });
        findViewById(R.id.btn_autoloc).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                doAutoloc();
            }
        });
        findViewById(R.id.btn_check_update).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                doCheckUpdate();
            }
        });

        // 缓存永久有效：进页面先只读 load_cache 回填 Spinner（不碰 adb、不触发实采）。
        // 无缓存则静默保留默认「不选择」，用户可随时点「获取就诊人」实采。
        runAsync(new Runnable() {
            @Override
            public void run() {
                String resJson;
                try {
                    Python py = Python.getInstance();
                    PyObject mod = py.getModule("app.patients");
                    PyObject out = mod.callAttr("load_patients_json");
                    resJson = out.toJava(String.class);
                } catch (Throwable t) {
                    log("[就诊人] 缓存加载异常: " + t.getMessage());
                    return;
                }
                final String r = resJson;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        try {
                            JSONObject res = new JSONObject(r);
                            if (res.optBoolean("ok", false)) {
                                JSONArray arr = res.optJSONArray("patients");
                                if (arr != null && arr.length() > 0) {
                                    fillPatientSpinner(arr);
                                    log(String.format("就诊人缓存已加载：%d 位（%s）",
                                            arr.length(), res.optString("captured_at", "未知时间")));
                                }
                            }
                        } catch (Exception ignored) {
                        }
                    }
                });
            }
        });
        findViewById(R.id.btn_llm).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                doTestLlm();
            }
        });
        // P4 自检：端上直接跑一次号源抓取（只读，绕开 9 段 LLM 流水线）。
        Button btnSelf = findViewById(R.id.btn_selftest);
        if (btnSelf != null) {
            btnSelf.setOnClickListener(new View.OnClickListener() {
                @Override
                public void onClick(View v) {
                    doSelfTest();
                }
            });
        }
        ((Button) findViewById(R.id.btn_clear_hist)).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                HistoryStore.clear(MainActivity.this);
                renderHistory();
                log("历史已清空");
            }
        });

        renderHistory();
        log("就绪。服务地址 = " + (getCfg(CFG_BASE, DEFAULT_BASE))
                + "（/v1/infer、/v1/location、/v1/app；上游凭据在服务端，App 不需要 key）");
        log("流水线模式：端上 Chaquopy 跑 python/runner.py（9 段语义不变）");

        // 首次启动后台预解压只读数据资产（assets/data ≈19MB → filesDir/data），避免首跑卡顿
        runAsync(new Runnable() {
            @Override
            public void run() {
                String d = PyBridge.extractAssetsIfNeeded();
                log("数据资产就绪: " + d);
            }
        });

        // P4 debug 自检广播通道（仅 DEBUG 构建注册，见方法内 BuildConfig.DEBUG 判定）。
        registerDebugSelfTestReceiver();

        // 自检（按钮 / 广播任一通道）结束都把自己带回前台：自检期间随申办被拉到前台读屏。
        SelfTest.setOnDoneListener(new SelfTest.OnDone() {
            @Override
            public void onSelfTestDone(boolean ok, String oneLine) {
                onSelfTestDoneUi(ok, oneLine);
            }
        });
    }

    @Override
    protected void onResume() {
        super.onResume();
        resumed = true;
        // 回到挂号助手前台：收起后台运行时的迷你浮窗（已由用户接管，不再需要"未退出"示意）。
        // 例外：刚跑完的「收尾结论」要留着，否则用户看不到最终结果。
        if (!MiniChip.finalPending()) {
            MiniChip.hide();
        }
        MiniChip.hideTapToReturn();   // 兜底「回到 App」按钮：既然已回到前台，收起
        refreshServiceStatus();
        syncMiniChipToggle();
        refreshPreflightResume();
        gateWatchUntil = android.os.SystemClock.elapsedRealtime() + 60_000L;
        ui.removeCallbacks(gateWatcher);
        ui.postDelayed(gateWatcher, 1500L);
    }

    /**
     * onResume 廉价刷新检查：只跑同步扫描（不深检登录、不拉起随申办，避免来回抢焦点）。
     * 保留上次深检结论：若 a11y/安装仍 OK 且上次已登录，则登录项继续 OK；否则退回 UNKNOWN，
     * 由用户点「重新检查」再深检。
     */
    private void refreshPreflightResume() {
        if (checkCard == null || preflightBusy) {
            return;
        }
        java.util.List<Preflight.Item> items = Preflight.scan(this);
        boolean a11yOk = false;
        for (Preflight.Item it : items) {
            if (it.kind == Preflight.Kind.A11Y && it.state == Preflight.State.OK) {
                a11yOk = true;
            }
        }
        for (Preflight.Item it : items) {
            if (it.kind == Preflight.Kind.LOGIN) {
                if (a11yOk && lastLoginOk) {
                    it.state = Preflight.State.OK;
                    it.detail = (lastLoginDetail != null
                            ? lastLoginDetail + "（沿用上次深检；点「重新检查」可复查）"
                            : "已登录（沿用上次深检结论；点「重新检查」可复查）");
                } else {
                    it.state = Preflight.State.UNKNOWN;
                    it.detail = "尚未检测 → 稍后自动深检（约 6s，只读导航）";
                    lastLoginOk = false;
                }
            }
        }
        renderPreflight(items);
        // 若 onResume 的廉价扫描本身就已全通过（如：从"去开启"回来，读屏已 OK 且沿用上次登录结论），
        // 也走同一套"提示 + 自动收起"流程，避免卡片永远停在"全部已满足"。
        if (preflightAllOk) {
            schedulePassAutoDismiss();
        }
        maybeAutoProbeLogin();
    }

    /**
     * 冷启动自动补跑登录深检。
     *
     * 门禁严格（必需项非 OK 即关表单），所以必须让 UNKNOWN 自己变成 OK，
     * 否则用户只会看到"表单全灰、下拉点了没反应"。这里在首屏渲染后
     * 自动跑一次深检（约 5~6s，会短暂把随申办带到前台，只读导航、不点任何按钮），
     * 结束后 bringSelfToFront() 回到挂号助手，门禁随即落地。
     *
     * 只在 LOGIN=UNKNOWN 且读屏/安装都 OK 时触发；本次进程内失败过就不再自动重试，
     * 避免反复把随申办拉到前台（用户仍可手动点「重新检查」）。
     */
    private void maybeAutoProbeLogin() {
        // ① 观测层也要能观测自己: 深检没跑时, 日志必须说清是被谁挡住的
        //    (2026-10-16 真机复盘: 门禁 60s 重扫却始终不绿, 日志里连一行"为什么没深检"都没有)
        String block = null;
        if (preflightBusy) {
            block = "preflightBusy";
        } else if (running) {
            block = "running";
        } else if (collecting) {
            block = "collecting";
        } else if (loginProbeFailed) {
            block = "loginProbeFailed(本进程已失败过)";
        } else if (preflightItems == null) {
            block = "preflightItems=null";
        } else if (SelfTest.isRunning()) {
            block = "selftest 正在驱动随申办";
        }
        if (block != null) {
            Log.i("HH_GATE", "maybeAutoProbeLogin 跳过: " + block);
            return;
        }
        // 只有"登录未测 + 读屏/安装已就绪"才值得深检: 读屏没就绪时深检必然空跑
        boolean a11yOk = false, installOk = false, loginUnknown = false;
        for (Preflight.Item it : preflightItems) {
            if (it.kind == Preflight.Kind.A11Y && it.state == Preflight.State.OK) {
                a11yOk = true;
            }
            if (it.kind == Preflight.Kind.INSTALL && it.state == Preflight.State.OK) {
                installOk = true;
            }
            if (it.kind == Preflight.Kind.LOGIN && it.state == Preflight.State.UNKNOWN) {
                loginUnknown = true;
            }
        }
        if (!loginUnknown || !a11yOk || !installOk) {
            StringBuilder why = new StringBuilder();
            for (Preflight.Item it : preflightItems) {
                if (it.required && it.state != Preflight.State.OK) {
                    why.append(it.kind).append('=').append(it.state).append(' ');
                }
            }
            String w = why.toString().trim();
            if (!w.isEmpty()) {
                Log.i("HH_GATE", "maybeAutoProbeLogin 跳过: 前置未就绪(" + w + ")");
            }
            // w 为空 = 必需项其实都已 OK（例如刚深检成功、这一次渲染用的是旧列表），
            // 此时打「跳过: 前置未就绪()」会误导排查，直接闭嘴。
            return;
        }
        // 实测「后台弹出界面」被拒(MIUIOP(10017)=ignore, 每次 adb 覆盖安装都会被重置)时,
        // 自动深检会把随申办拉到前台、跑完却回不到本 App —— 用户就被困在随申办页面。
        // 2026-10-16 真机复盘(logcat 证据): ensure_444 kind=444 → loginProbe state=OK
        // → [回前台] 三级阶梯全部 Abort → 「系统仍拦截 → 已弹点我回到挂号助手按钮」。
        // 所以这时不自动跑, 只把话说清楚; 用户知情后仍可手动点「重新检查」。
        if (Preflight.bgLaunchBlockedFresh(this)) {
            long agoMin = (System.currentTimeMillis() - Preflight.bgLaunchBlockedAt(this))
                    / 60000L;
            Log.i("HH_GATE", "maybeAutoProbeLogin 跳过: 后台弹出实测被拒(" + agoMin + " 分钟前)");
            return;   // 文案由 annotateLoginHint 在渲染时补充；这里再渲染会死循环
        }
        // 防乒乓：本进程内只自动深检一次。onResume→深检→bringSelfToFront→onResume 会无限互踢，
        // 表现就是「随申办被反复拖到 444/同意页并一直停留」。用户仍可手动点「重新检查」。
        if (autoLoginProbeUsed) {
            log("[前置检查] 登录未确认，但本次进程已自动深检过 → 不再自动重跑（防乒乓）");
            return;
        }
        autoLoginProbeUsed = true;
        Log.i("HH_GATE", "maybeAutoProbeLogin → 触发登录深检（会拉起随申办）");
        log("[前置检查] 登录尚未确认，自动开始深检…");
        ui.postDelayed(new Runnable() {
            @Override
            public void run() {
                runFullCheck();
            }
        }, 900L);
    }

    @Override
    protected void onPause() {
        super.onPause();
        resumed = false;
        // 收尾结论正在展示：这次窗口抖动不打断它（到点自动收）。
        if (MiniChip.finalPending()) {
            return;
        }
        // 只有正在跑任务（流水线/实采就诊人/自检）且开着浮窗时，才挂小胶囊。
        if (miniChipEnabled && taskBusy()) {
            MiniChip.show(this.getApplicationContext(), "挂号助手 · 运行中…");
        } else {
            MiniChip.hide();
        }
    }

    /** 是否有设备独占任务在跑（与"唤起随申办后原 App 退后台"语义一致）。 */
    private boolean taskBusy() {
        return running || collecting || preflightBusy || SelfTest.isRunning();
    }

    private void syncMiniChipToggle() {
        if (miniChipToggle == null) {
            return;
        }
        miniChipToggle.setChecked(miniChipEnabled);
    }

    // ------------------------------------------------------------------ 配置

    private void loadConfig() {
        // 归集后服务地址是写死的公网入口。老版本手机上残留的局域网 Agent Service 地址
        // （192.168.x.x:7989 等）会让 LLM 调用连不上，表现为「跑完了但没有详细信息」，
        // 而设置页里的 API_KEY 框已删，用户自己很难发现。这里一次性迁回默认值。
        String stored = getCfg(CFG_BASE, DEFAULT_BASE);
        String eff = normalizeBase(stored);
        if (!eff.equals(stored)) {
            getSharedPreferences(PREF, MODE_PRIVATE).edit().putString(CFG_BASE, eff).apply();
            log("[配置] 服务地址已从旧版本残留 " + stored + " 迁回 " + eff);
        }
        agentsvcBase.setText(eff);
        PyBridge.setConfig(CFG_BASE, eff);
        // 归集后 App 不再持有上游凭据：清掉旧版本残留的 api_key，避免误发 Authorization。
        if (!"".equals(getCfg(CFG_API_KEY, ""))) {
            getSharedPreferences(PREF, MODE_PRIVATE).edit().putString(CFG_API_KEY, "").apply();
        }
        // 候选集数量 / 推荐数量（默认 12 / 3）
        if (candN != null) {
            candN.setText(getCfg(CFG_CAND_N, String.valueOf(DEFAULT_CAND_N)));
        }
        if (topN != null) {
            topN.setText(getCfg(CFG_TOP_N, String.valueOf(DEFAULT_TOP_N)));
        }
        refreshEstimate();
        // 浮窗开关：默认开（"1"）；仅显式存过 "0" 才关。
        String mc = getSharedPreferences(PREF, MODE_PRIVATE).getString(CFG_MINI_CHIP, "1");
        miniChipEnabled = !"0".equals(mc);
    }

    /**
     * 规整服务地址：空 → 默认；缺协议 → 补 https://；旧版局域网/本机地址（:7989 / :7988 /
     * 192.168.x / 127.0.0.1 / localhost）→ 默认。用户自己填的其它地址（自建反代等）保留不动。
     */
    private String normalizeBase(String v) {
        if (v == null) {
            return DEFAULT_BASE;
        }
        String s = v.trim();
        if (s.isEmpty()) {
            return DEFAULT_BASE;
        }
        if (!s.startsWith("http://") && !s.startsWith("https://")) {
            s = "https://" + s;
        }
        String low = s.toLowerCase(java.util.Locale.US);
        boolean legacy = low.contains(":7989") || low.contains(":7988")
                || low.contains("192.168.") || low.contains("127.0.0.1")
                || low.contains("localhost")
                || low.startsWith("http://10.") || low.startsWith("http://172.")
                || low.startsWith("http://192.");
        if (legacy) {
            return DEFAULT_BASE;
        }
        return s.endsWith("/") ? s : s + "/";
    }

    private String getCfg(String key, String def) {
        String v = getSharedPreferences(PREF, MODE_PRIVATE).getString(key, def);
        return v == null ? def : v;
    }

    private void saveConfig() {
        String base = agentsvcBase.getText().toString().trim();
        String key = "";   // 归集后不再由 App 持有上游 key（写空以覆盖旧配置）
        String cn = (candN != null) ? candN.getText().toString().trim() : "";
        String tn = (topN != null) ? topN.getText().toString().trim() : "";
        getSharedPreferences(PREF, MODE_PRIVATE).edit()
                .putString(CFG_BASE, base).putString(CFG_API_KEY, key)
                .putString(CFG_CAND_N, cn).putString(CFG_TOP_N, tn).apply();
        PyBridge.setConfig(CFG_BASE, base);
        PyBridge.setConfig(CFG_API_KEY, key);
    }

    // ------------------------------------------------------------------ APK 自更新（hh-server /v1/app）

    /** 「检查更新」按钮：GET {base}/v1/app/version（后台线程），结果回主线程弹窗。 */
    private void doCheckUpdate() {
        final String base = agentsvcBase.getText().toString().trim();
        log("检查更新：GET " + AgentClient.resolveBase(base) + UpdateChecker.PATH_VERSION
                + "?current_version_code=" + BuildConfig.VERSION_CODE);
        io.execute(new Runnable() {
            @Override
            public void run() {
                final UpdateChecker.CheckResult r =
                        UpdateChecker.check(base, BuildConfig.VERSION_CODE);
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        showUpdateCheck(base, r);
                    }
                });
            }
        });
    }

    private void showUpdateCheck(final String base, UpdateChecker.CheckResult r) {
        if (!r.ok) {
            log("✗ 检查更新失败：" + r.error);
            Toast.makeText(this, "检查更新失败：" + r.error, Toast.LENGTH_LONG).show();
            return;
        }
        UpdateChecker.Info i = r.latest;
        if (i == null) {
            log("服务端尚未上传任何 APK（/v1/app/version 无 latest）");
            return;
        }
        log(String.format(Locale.CHINA, "服务端最新 %s(%d) · 当前 %d · %d bytes · sha256=%s · 说明：%s",
                i.versionName, i.versionCode, BuildConfig.VERSION_CODE, i.size,
                i.sha256.length() > 16 ? i.sha256.substring(0, 16) : (i.sha256.isEmpty() ? "-" : i.sha256),
                i.notes.isEmpty() ? "(无)" : i.notes));
        if (!r.hasUpdate) {
            Toast.makeText(this, "已是最新版本（" + i.versionName + "）", Toast.LENGTH_SHORT).show();
            return;
        }
        new AlertDialog.Builder(this)
                .setTitle("发现新版本 " + i.versionName + "(" + i.versionCode + ")")
                .setMessage((i.notes.isEmpty() ? "" : i.notes + "\n\n")
                        + "大小：" + String.format(Locale.CHINA, "%.1f", i.size / 1048576.0) + " MB\n"
                        + "下载完成后由系统安装器接管"
                        + (r.forceUpdate ? "\n\n（服务端标记：强制更新）" : ""))
                .setPositiveButton("下载并安装", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface d, int which) {
                        startDownload(base, i);
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    /** 下载（含 sha256 校验）→ 拉起系统安装器。全程后台线程，进度写日志。 */
    private void startDownload(final String base, final UpdateChecker.Info info) {
        if (!UpdateChecker.canInstall(this)) {
            log("未授予「安装未知应用」权限：已跳系统设置，开启后再点一次「检查更新」");
            Toast.makeText(this, "请先允许本 App 安装应用", Toast.LENGTH_LONG).show();
            try {
                startActivity(UpdateChecker.installPermissionIntent(this));
            } catch (Exception e) {
                log("打不开系统设置页：" + e.getMessage());
            }
            return;
        }
        log("开始下载 " + info.file + " …");
        io.execute(new Runnable() {
            @Override
            public void run() {
                try {
                    File f = UpdateChecker.download(MainActivity.this, base, info,
                            new UpdateChecker.Progress() {
                                @Override
                                public void onProgress(final long got, final long total) {
                                    ui.post(new Runnable() {
                                        @Override
                                        public void run() {
                                            log(String.format(Locale.CHINA, "下载中 %s / %s MB",
                                                    fmtMb(got), fmtMb(total)));
                                        }
                                    });
                                }
                            });
                    final String path = f.getAbsolutePath();
                    log("下载完成、sha256 校验通过：" + path);
                    ui.post(new Runnable() {
                        @Override
                        public void run() {
                            try {
                                UpdateChecker.install(MainActivity.this, new File(path));
                                log("已拉起系统安装器（若没弹出，请检查「安装未知应用」权限）");
                            } catch (Exception e) {
                                log("拉起安装器失败：" + e);
                            }
                        }
                    });
                } catch (final Exception e) {
                    log("✗ 下载/安装失败：" + e.getMessage());
                }
            }
        });
    }

    private static String fmtMb(long bytes) {
        return String.format(Locale.CHINA, "%.1f", bytes / 1048576.0);
    }

    // ------------------------------------------------------------------ 表单

    /** 就诊人下拉首项固定值（index 0 = 不选择/代他人挂号）。 */
    private static final String PATIENT_NONE = "不选择（代他人挂号：年龄/性别由描述解析）";

    /**
     * 三个下拉选择器（预期费用 / 号源子tab / 就诊人）：原生 Spinner。
     *
     * 备注：曾怀疑"点了没反应"是 HyperOS 吞掉 Spinner 的锚定 PopupWindow，
     * 实测根因是「使用前检查」门禁把整块表单 setEnabled(false) —— 禁用态的
     * Spinner 在 AbsSpinner.onTouchEvent 里直接 return false，所以怎么点都没反应。
     * 门禁会自动跑登录深检、能真正解锁，故这里改回原生 Spinner。
     */
    private Spinner fee, slottype, patient;
    private String[] feeItems;
    private String[] slotItems;
    private String[] patItems;
    /** 与 Spinner 选中位置保持同步（buildSpecJson / 耗时估算都读这三个）。 */
    private int feeIdx = 0, slotIdx = 0, patientIdx = 0;

    private void setupSpinners() {
        feeItems = new String[]{"自动（从描述解析）", "0元（免费/最低）", "10元",
                "百元", "千元", "万元", "不限"};
        slotItems = new String[]{"全部子tab（最全）", "仅专家门诊（快速档）",
                "仅普通门诊", "仅专病门诊"};
        patItems = new String[]{PATIENT_NONE};

        fee = findViewById(R.id.fee);
        slottype = findViewById(R.id.slottype);
        patient = findViewById(R.id.patient);

        fee.setAdapter(spinnerAdapter(feeItems));
        slottype.setAdapter(spinnerAdapter(slotItems));
        patient.setAdapter(spinnerAdapter(patItems));

        fee.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            @Override
            public void onItemSelected(AdapterView<?> parent, View view, int position, long id) {
                feeIdx = position;
                refreshEstimate();
            }

            @Override
            public void onNothingSelected(AdapterView<?> parent) {
            }
        });
        slottype.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            @Override
            public void onItemSelected(AdapterView<?> parent, View view, int position, long id) {
                slotIdx = position;
                refreshEstimate();
            }

            @Override
            public void onNothingSelected(AdapterView<?> parent) {
            }
        });
        patient.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            @Override
            public void onItemSelected(AdapterView<?> parent, View view, int position, long id) {
                patientIdx = position;
                // index 0 固定为「不选择」（代他人挂号：年龄/性别由描述解析）-> patient=null
                selectedPatient = (position < patList.size()) ? patList.get(position) : null;
                Log.i("HH_PAT", "patient picked idx=" + position
                        + " -> " + (selectedPatient == null ? "NONE" : "dict"));
            }

            @Override
            public void onNothingSelected(AdapterView<?> parent) {
            }
        });
    }

    /** Spinner 适配器：闭合态用 simple_spinner_item，弹出列表用 dropdown_item。 */
    private ArrayAdapter<String> spinnerAdapter(String[] items) {
        ArrayAdapter<String> a = new ArrayAdapter<>(this,
                android.R.layout.simple_spinner_item, items);
        a.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        return a;
    }

    // ------------------------------------------------------------------ 底部 Tab
    private static final int TAB_FORM = 0, TAB_LOG = 1, TAB_RESULT = 2, TAB_HISTORY = 3, TAB_SETTINGS = 4;

    /** 5 面板 toggle：表单 / 实时日志 / 结果 / 历史 / 设置。 */
    private void setupTabs() {
        final View[] panels = new View[]{
                findViewById(R.id.panel_form), findViewById(R.id.panel_log),
                findViewById(R.id.panel_result), findViewById(R.id.panel_history),
                findViewById(R.id.panel_settings),
        };
        final View[] tabs = new View[]{
                findViewById(R.id.tab_form), findViewById(R.id.tab_log),
                findViewById(R.id.tab_result), findViewById(R.id.tab_history),
                findViewById(R.id.tab_settings),
        };
        for (int i = 0; i < tabs.length; i++) {
            final int idx = i;
            tabs[i].setOnClickListener(new View.OnClickListener() {
                @Override
                public void onClick(View v) {
                    switchTab(idx, panels, tabs);
                }
            });
        }
        switchTab(TAB_FORM, panels, tabs);
    }

    private void switchTab(int idx, final View[] panels, final View[] tabs) {
        for (int i = 0; i < panels.length; i++) {
            panels[i].setVisibility(i == idx ? View.VISIBLE : View.GONE);
        }
        for (int i = 0; i < tabs.length; i++) {
            ((TextView) tabs[i]).setTextColor(i == idx ? 0xFF2563EB : 0xFF9CA3AF);
            ((TextView) tabs[i]).setTypeface(null, i == idx ? android.graphics.Typeface.BOLD
                    : android.graphics.Typeface.NORMAL);
        }
    }

    /** 程序化切到某 Tab（如流水线结束自动跳「结果」）。可在任意线程调用。 */
    private void showTab(final int idx) {
        ui.post(new Runnable() {
            @Override
            public void run() {
                View[] panels = new View[]{
                        findViewById(R.id.panel_form), findViewById(R.id.panel_log),
                        findViewById(R.id.panel_result), findViewById(R.id.panel_history),
                        findViewById(R.id.panel_settings),
                };
                View[] tabs = new View[]{
                        findViewById(R.id.tab_form), findViewById(R.id.tab_log),
                        findViewById(R.id.tab_result), findViewById(R.id.tab_history),
                        findViewById(R.id.tab_settings),
                };
                switchTab(idx, panels, tabs);
            }
        });
    }

    // ------------------------------------------------------------------ 前置检查
    /** 表单页顶部「使用前检查」：读屏/操作权限、随申办安装、登录态（深检）门禁。 */
    private void setupPreflight() {
        checkCard = findViewById(R.id.check_card);
        checkSummary = findViewById(R.id.check_summary);
        checkRows = findViewById(R.id.check_rows);
        formBody = findViewById(R.id.form_body);
        btnRecheck = findViewById(R.id.btn_recheck);

        btnRecheck.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                runFullCheck();
            }
        });
        // 首帧只做廉价同步扫描（不导航、不拉起随申办）；登录深检留给「重新检查」。
        renderPreflight(Preflight.scan(this));
    }

    /** 完整检查：廉价扫描 + 登录深检（后者会短暂把随申办拉到前台，只读导航）。 */
    private void runFullCheck() {
        if (preflightBusy) {
            Toast.makeText(this, "检查进行中…", Toast.LENGTH_SHORT).show();
            return;
        }
        if (running || collecting) {
            Toast.makeText(this, "任务运行中（设备独占）—— 请稍后再检查", Toast.LENGTH_LONG).show();
            return;
        }
        final java.util.List<Preflight.Item> base = Preflight.scan(this);
        // 先渲染廉价结果，登录项标记为进行中
        for (Preflight.Item it : base) {
            if (it.kind == Preflight.Kind.LOGIN) {
                it.state = Preflight.State.UNKNOWN;
                it.detail = getString(R.string.check_login_running);
            }
        }
        renderPreflight(base);
        startLoginProbe(base);
    }

    /** 登录深检：后台调 app.preflight.check_login_json（复用已实测的 ensure_444 路径）。 */
    private void startLoginProbe(final java.util.List<Preflight.Item> base) {
        preflightBusy = true;
        btnRecheck.setEnabled(false);
        btnRecheck.setText("检查中…");
        withService(new Runnable() {
            @Override
            public void run() {
                String resJson;
                try {
                    Python py = Python.getInstance();
                    PyObject mod = py.getModule("app.preflight");
                    // 2026-10-14 修复: 原先硬编码 "476cf991"，MI 5X 上的深检也被标成另一台机器
                    PyObject out = mod.callAttr("check_login_json", PyBridge.deviceKey());
                    resJson = out.toJava(String.class);
                } catch (Throwable t) {
                    resJson = "{\"ok\":false,\"state\":\"error\",\"error\":\""
                            + String.valueOf(t.getMessage()).replace("\"", "'") + "\"}";
                }
                final String r = resJson;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        onLoginProbeDone(base, r);
                    }
                });
            }
        }, new Runnable() {
            @Override
            public void run() {
                // 门控失败：把登录项标为 BAD 并恢复按钮
                for (Preflight.Item it : base) {
                    if (it.kind == Preflight.Kind.LOGIN) {
                        it.state = Preflight.State.BAD;
                        it.detail = "读屏服务未就绪，无法深检登录 → 先去开启";
                    }
                }
                preflightBusy = false;
                btnRecheck.setEnabled(true);
                btnRecheck.setText(getString(R.string.btn_recheck));
                renderPreflight(base);
            }
        });
    }

    /** 深检回包：回填登录项状态，回前台，重绘 + 重新判定门禁。 */
    private void onLoginProbeDone(java.util.List<Preflight.Item> base, String resJson) {
        preflightBusy = false;
        btnRecheck.setEnabled(true);
        btnRecheck.setText(getString(R.string.btn_recheck));
        String probeState = "fail";   // try 块外的副本: 收尾判定 busy 时还要用
        try {
            JSONObject o = new JSONObject(resJson);
            String state = o.optString("state", "fail");
            probeState = state;
            String err = o.optString("error", "");
            lastLoginOk = "logged".equals(state);
            for (Preflight.Item it : base) {
                if (it.kind != Preflight.Kind.LOGIN) {
                    continue;
                }
                if ("logged".equals(state)) {
                    it.state = Preflight.State.OK;
                    it.detail = "已登录，可进入 预约挂号(444)"
                            + "（耗时 " + o.optString("elapsed_s", "?") + "s）";
                    lastLoginDetail = it.detail;
                } else if ("blocked".equals(state)) {
                    it.state = Preflight.State.BAD;
                    it.detail = "触发实名认证/安全验证，需在随申办内人工完成 → 去登录随申办";
                } else if ("busy".equals(state)) {
                    // ②: 别的任务正在驱动随申办 —— 不能判成"未登录", 保持未知并稍后复检
                    it.state = Preflight.State.UNKNOWN;
                    it.detail = "抓取/流水线正在使用随申办，稍后自动复检（"
                            + (err.isEmpty() ? "" : err) + "）";
                } else if ("no_service".equals(state)) {
                    it.state = Preflight.State.WARN;
                    it.detail = "读屏服务未绑定，无法深检 → 先去开启";
                } else {
                    it.state = Preflight.State.BAD;
                    it.detail = "多半未登录或页面异常（" + state
                            + (err.isEmpty() ? "" : ("）：" + err)) + " → 去登录随申办";
                }
            }
        } catch (Exception e) {
            for (Preflight.Item it : base) {
                if (it.kind == Preflight.Kind.LOGIN) {
                    it.state = Preflight.State.BAD;
                    it.detail = "深检结果解析失败：" + e.getMessage();
                }
            }
        }
        // 深检把随申办带到了前台，检查完把挂号助手带回前台（红线：只回自己，不点它方）
        for (Preflight.Item it : base) {
            if (it.kind == Preflight.Kind.LOGIN) {
                // busy(别人在用随申办)不是"未登录": 不把门禁判死, 也不消耗本次进程的自动补跑机会
                loginProbeFailed = (it.state != Preflight.State.OK && !"busy".equals(probeState));
                Log.i("HH_GATE", "loginProbe state=" + it.state
                        + " loginProbeFailed=" + loginProbeFailed
                        + " autoLoginProbeUsed=" + autoLoginProbeUsed);
            }
        }
        bringSelfToFront();
        renderPreflight(base);
        // 全部必需项通过：检查卡停留片刻后自动收起，不再需要用户点"关闭并使用"
        if (preflightAllOk) {
            schedulePassAutoDismiss();
        }
    }

    /** 渲染检查行 + 应用门禁。UI 线程调用。 */
    private void renderPreflight(java.util.List<Preflight.Item> items) {
        preflightItems = items;
        annotateLoginHint(items);      // 只改文案，绝不在这里再触发深检/重渲染
        checkRows.removeAllViews();
        for (Preflight.Item it : items) {
            checkRows.addView(buildCheckRow(it));
        }
        boolean wasReady = gateReady;
        preflightAllOk = Preflight.allRequiredOk(items);
        if (preflightAllOk) gateOpenedAt = android.os.SystemClock.uptimeMillis();
        // 门禁严格：必需项只要不是 OK（含 UNKNOWN/WARN）就关掉表单。
        // 之所以以前像"点了没反应"，不是因为严格，而是因为 UNKNOWN 永远不会自己
        // 变 OK —— 现在冷启动会自动跑登录深检（见 maybeAutoProbeLogin），
        // 严格 + 自动深检 才能既守住门禁又保证表单真的能解锁。
        // 唯一宽限：无障碍被系统临时解绑（本 ROM 在 uiautomator dump / 进程回收时
        // 会 unbind 再 bind，约 1.2s），给 8 秒宽限，避免表单在用户眼前反复开合；
        // 系统里真正关掉（BAD）时立即关闭。
        boolean gate = preflightAllOk;
        if (!gate) {
            List<Preflight.Item> notOk = new ArrayList<>();
            for (Preflight.Item it : items) {
                if (it.required && it.state != Preflight.State.OK) notOk.add(it);
            }
            gate = notOk.size() == 1
                    && notOk.get(0).kind == Preflight.Kind.A11Y
                    && notOk.get(0).state == Preflight.State.WARN
                    && android.os.SystemClock.uptimeMillis() - gateOpenedAt < 8000;
        }
        gateReady = gate;
        final int gen = ++preflightGen;   // 让上一次残留的延迟收起作废
        java.util.List<Preflight.Item> bad = Preflight.blocking(items);
        if (preflightAllOk) {
            checkSummary.setText(R.string.check_ok_hint);
            checkSummary.setTextColor(0xFF16A34A);
            // 首次进入通过态时提示一次（真正收起的动作由 onLoginProbeDone 决定，
            // 避免 onResume→probe 的连续渲染把"通过→收起"的时序打乱）
            if (!wasReady && !preflightPassAnnounced) {
                preflightPassAnnounced = true;
                Toast.makeText(this, R.string.check_pass_toast, Toast.LENGTH_SHORT).show();
                log("[前置检查] 全部必需项满足");
            }
        } else {
            preflightPassAnnounced = false;
            StringBuilder sb = new StringBuilder(getString(R.string.check_bad_hint)).append("\n");
            for (Preflight.Item b : bad) {
                sb.append("  • ").append(b.label).append("\n");
            }
            checkSummary.setText(sb.toString());
            checkSummary.setTextColor(0xFFDC2626);
            // ① 门禁不绿时把"卡住项+状态"写进日志: 否则现场只看到 60s 重扫, 查不出卡在哪
            StringBuilder nm = new StringBuilder();
            for (Preflight.Item b : bad) {
                nm.append(b.kind).append('=').append(b.state).append(' ');
            }
            String stuck = nm.toString().trim();
            long nowMs = android.os.SystemClock.elapsedRealtime();
            if (!stuck.equals(lastStuckLog) || nowMs - lastStuckLogAt > 10000L) {
                lastStuckLog = stuck;
                lastStuckLogAt = nowMs;
                Log.i("HH_GATE", "门禁未绿, 卡住项: " + stuck);
            }
            if (preflightDismissed) {
                // 必需项回退了（如权限被关）：重新打开检查框
                preflightDismissed = false;
            }
        }
        applyGate();
        // 只要还有必需项未确认（含"尚未检测"），就把卡片留着，方便用户点「重新检查」
        if (!preflightAllOk && !preflightDismissed) {
            checkCard.setVisibility(View.VISIBLE);
        }
        // 无障碍/安装刚变 OK 时补跑登录深检：否则严格门禁会一直关着（UNKNOWN 不会自己变 OK）
        if (!preflightAllOk) {
            maybeAutoProbeLogin();
        }
    }

    /**
     * "检查通过"后的收尾：检查卡在提示 toast 时长内展示，随后自动收起，无需再点按钮。
     * 由 {@link #onLoginProbeDone} 在确认 gateReady 后调用；
     * 仅当此后没有更新的渲染、且仍处于通过态时才真正收起（避免与 onResume 的后续渲染竞态）。
     */
    private void schedulePassAutoDismiss() {
        if (preflightDismissed) {
            return;   // 已经是收起态
        }
        final int gen = preflightGen;
        ui.postDelayed(new Runnable() {
            @Override
            public void run() {
                if (gen == preflightGen && preflightAllOk && !preflightDismissed) {
                    preflightDismissed = true;
                    checkCard.setVisibility(View.GONE);
                    log("[前置检查] 检查卡已自动收起");
                }
            }
        }, 2500L);
    }

    /** 单行：状态点 + 标签 + 详情 + 「去处理/深检」按钮。 */
    private View buildCheckRow(final Preflight.Item it) {
        android.widget.LinearLayout row = new android.widget.LinearLayout(this);
        row.setOrientation(android.widget.LinearLayout.HORIZONTAL);
        android.widget.LinearLayout.LayoutParams rlp = new android.widget.LinearLayout.LayoutParams(
                android.widget.LinearLayout.LayoutParams.MATCH_PARENT,
                android.widget.LinearLayout.LayoutParams.WRAP_CONTENT);
        rlp.topMargin = dp(6);
        row.setLayoutParams(rlp);
        row.setGravity(android.view.Gravity.CENTER_VERTICAL);

        // 状态点
        TextView dot = new TextView(this);
        dot.setText(dotFor(it.state));
        dot.setTextColor(colorFor(it.state));
        dot.setTextSize(14f);
        dot.setPadding(0, 0, dp(8), 0);
        row.addView(dot);

        // 标签 + 详情（竖排，占剩余宽度）
        android.widget.LinearLayout txt = new android.widget.LinearLayout(this);
        txt.setOrientation(android.widget.LinearLayout.VERTICAL);
        android.widget.LinearLayout.LayoutParams tlp = new android.widget.LinearLayout.LayoutParams(
                0, android.widget.LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        txt.setLayoutParams(tlp);

        TextView label = new TextView(this);
        label.setText(it.label + (it.required ? "" : "（可选）"));
        label.setTextColor(0xFF1F2937);
        label.setTextSize(13f);
        label.getPaint().setFakeBoldText(true);
        txt.addView(label);

        TextView detail = new TextView(this);
        detail.setText(it.detail == null ? "" : it.detail);
        detail.setTextColor(0xFF6B7280);
        detail.setTextSize(11f);
        txt.addView(detail);
        row.addView(txt);

        // 右侧按钮：非 OK 才给（必需项=去处理，登录=深检，可选=去授权）
        if (it.state != Preflight.State.OK) {
            Button fix = new Button(this);
            android.widget.LinearLayout.LayoutParams flp = new android.widget.LinearLayout.LayoutParams(
                    android.widget.LinearLayout.LayoutParams.WRAP_CONTENT,
                    android.widget.LinearLayout.LayoutParams.WRAP_CONTENT);
            flp.leftMargin = dp(8);
            fix.setLayoutParams(flp);
            fix.setAllCaps(false);
            fix.setTextSize(12f);
            fix.setBackgroundResource(android.R.drawable.btn_default);
            fix.setText(fixLabel(it));
            final Preflight.Kind k = it.kind;
            fix.setOnClickListener(new View.OnClickListener() {
                @Override
                public void onClick(View v) {
                    doPreflightFix(k);
                }
            });
            row.addView(fix);
        }
        return row;
    }

    private String dotFor(Preflight.State s) {
        switch (s) {
            case OK: return "✓";
            case WARN: return "!";
            case BAD: return "✕";
            default: return "…";
        }
    }

    private int colorFor(Preflight.State s) {
        switch (s) {
            case OK: return 0xFF16A34A;
            case WARN: return 0xFFD97706;
            case BAD: return 0xFFDC2626;
            default: return 0xFF6B7280;
        }
    }

    private String fixLabel(Preflight.Item it) {
        switch (it.kind) {
            case A11Y: return Preflight.canSelfEnable(this) ? "一键开启" : "去开启";
            case INSTALL: return "去安装";
            case LOGIN: return preflightBusy ? "检查中" : "去登录";
            case OVERLAY: return "去授权";
            case LOCATION: return "去授权";
            case BACK_LAUNCH: return "去开启";
            default: return "去处理";
        }
    }

    /** 逐项修复跳转（只打开系统设置/商店/随申办本身，绝不代点业务按钮）。 */
    private void doPreflightFix(Preflight.Kind k) {
        switch (k) {
            case A11Y:
                openOrEnableAccessibility();
                break;
            case INSTALL:
                openSsbInstall();
                break;
            case LOGIN:
                // 仅把随申办拉到前台让用户自己登录（不点任何按钮）
                DeviceBridge br = SsAccessibilityService.bridge();
                boolean ok = br != null && br.launch(DeviceBridge.TARGET_PKG);
                Toast.makeText(this, ok ? "请在随申办内完成登录后回本页点「重新检查」"
                        : "无法唤起随申办（可能未安装）", Toast.LENGTH_LONG).show();
                break;
            case OVERLAY:
                MiniChip.requestOverlayPermission(this);
                break;
            case LOCATION:
                try {
                    requestPermissions(new String[]{
                            android.Manifest.permission.ACCESS_FINE_LOCATION}, 1002);
                } catch (Throwable t) {
                    log("无法申请定位权限: " + t.getMessage());
                }
                break;
            case BACK_LAUNCH: {
                // 「后台弹出界面」是 MIUI 私有 appop（MIUIOP(10017)），没有公开 Intent 直达；
                // 落到应用详情页，用户点「权限管理」就能看到。同时把 adb 一行命令写进日志。
                try {
                    Intent it = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                            android.net.Uri.parse("package:" + getPackageName()));
                    it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    startActivity(it);
                } catch (Throwable t2) {
                    log("打不开应用详情页: " + t2.getMessage());
                }
                log("[前置检查] " + Preflight.bgLaunchManualPath());
                break;
            }
            default:
                break;
        }
    }

    /** 随申办未安装 → 优先应用商店详情页，兜底浏览器搜索。 */
    private void openSsbInstall() {
        String pkg = DeviceBridge.TARGET_PKG;
        try {
            Intent m = new Intent(Intent.ACTION_VIEW,
                    android.net.Uri.parse("market://details?id=" + pkg));
            m.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(m);
            return;
        } catch (Exception ignored) {
        }
        try {
            Intent w = new Intent(Intent.ACTION_VIEW, android.net.Uri.parse(
                    "https://www.baidu.com/s?wd=" + pkg));
            w.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(w);
        } catch (Exception e) {
            Toast.makeText(this, "未能打开安装入口，请手动搜索安装「随申办」",
                    Toast.LENGTH_LONG).show();
        }
    }

    /** 门禁落地：未通过时整块表单「可见但不可操作」（setEnabled 不级联，需递归）。 */
    private void applyGate() {
        if (formBody == null) {
            return;
        }
        // 任务进行中（流水线/实采/深检）不要覆写各按钮的忙态，等任务结束后再落地
        if (taskBusy()) {
            return;
        }
        setEnabledRecursive(formBody, gateReady);
        // 禁用态保留可见、轻微压暗：让用户看得见表单，也知道为什么点不动
        formBody.setAlpha(gateReady ? 1.0f : 0.55f);
        // btn_run 在 form_body 之外（固定底部），需单独按门禁启用/禁用
        if (btnRun != null) {
            btnRun.setEnabled(gateReady);
            btnRun.setAlpha(gateReady ? 1.0f : 0.45f);
        }
    }

    /**
     * 递归开关"可交互"控件：只碰 TextView 系（Button/EditText…）、AdapterView（Spinner）、
     * CompoundButton（CheckBox/Switch）。刻意只改 enabled、不改 clickable：
     * 给控件加 clickable 会吃掉触摸（曾让 Spinner 内部 TextView 吞点击，下拉框失灵）。
     */
    private void setEnabledRecursive(android.view.View v, boolean enabled) {
        if (v instanceof android.widget.TextView
                || v instanceof android.widget.AdapterView
                || v instanceof android.widget.CompoundButton) {
            v.setEnabled(enabled);
            // 注意：这里绝不能 setClickable(true)。
            // Spinner 的选中项是一个 TextView（@android:id/text1），一旦被设成 clickable，
            // 它就会把 DOWN/UP 全部消费掉，Spinner 自己再也收不到点击 -> 下拉弹窗永远弹不出来。
            // 禁用交互只需要 setEnabled(false)：View.onTouchEvent 对 disabled 的可点击控件
            // 会「消费但不响应」，门禁同样成立。
        }
        if (v instanceof android.view.ViewGroup) {
            android.view.ViewGroup g = (android.view.ViewGroup) v;
            for (int i = 0; i < g.getChildCount(); i++) {
                setEnabledRecursive(g.getChildAt(i), enabled);
            }
        }
    }


    /**
     * 「后台弹出界面」被实测拦下时，把登录那一行说清楚：为什么没自动深检、该怎么做。
     * 只改 Item.detail 供本次渲染使用，绝不跳转/重渲染 —— 否则与 renderPreflight
     * 互为回调就是死循环（2026-10-16 真机日志每 20ms 一条即为此证）。
     */
    private void annotateLoginHint(java.util.List<Preflight.Item> items) {
        if (items == null || !Preflight.bgLaunchBlockedFresh(this)) {
            return;
        }
        for (Preflight.Item it : items) {
            if (it.kind == Preflight.Kind.LOGIN && it.state == Preflight.State.UNKNOWN) {
                it.detail = "未确认；但「后台弹出界面」被拒时自动深检会把你困在随申办"
                        + " → 先开启该权限，或手动点「重新检查」";
            }
        }
    }

    /** 同一组卡住项 10s 内只记一次，避免门禁未绿时日志刷屏。 */
    private String lastStuckLog = "";
    private long lastStuckLogAt = 0L;

    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density + 0.5f);
    }

    // ------------------------------------------------------------------ 就诊人
    /**
     * 「👥 获取就诊人」：端上 devbridge 只读采集（app.patients.get_patients(fresh=True)），
     * 成功后回填 Spinner（首项固定「不选择」）。语义对齐 web：
     * <ul>
     *   <li>缓存永久有效 —— 仅 fresh=True（点按钮）才实采；页面只读 load_cache()；</li>
     *   <li>设备独占：流水线运行中 -> 拒绝（等价 web 409）；</li>
     *   <li>实采中禁重复点。</li>
     * </ul>
     */
    private void doGetPatients() {
        if (running) {
            Toast.makeText(this, "流水线运行中（设备独占）—— 结束后再获取就诊人", Toast.LENGTH_LONG).show();
            return;
        }
        if (collecting) {
            Toast.makeText(this, "就诊人实采进行中…", Toast.LENGTH_SHORT).show();
            return;
        }
        collecting = true;
        if (patient != null) {
            patient.setEnabled(false);
        }
        final Button btn = findViewById(R.id.btn_get_patients);
        btn.setEnabled(false);
        btn.setText("获取中…");
        log("");
        log("=== 获取就诊人（端上只读采集，fresh=True）===");

        withService(new Runnable() {
            @Override
            public void run() {
                String resJson;
                try {
                    Python py = Python.getInstance();
                    PyObject mod = py.getModule("app.patients");
                    // 2026-10-14 修复: 原先硬编码 "476cf991"，MI 5X 上采集的就诊人也标错机器
                    PyObject out = mod.callAttr("fetch_patients_json", PyBridge.deviceKey());
                    resJson = out.toJava(String.class);
                } catch (Throwable t) {
                    log("[error] " + t.getClass().getSimpleName() + ": " + t.getMessage());
                    resJson = "{\"ok\":false,\"error\":\"Python 异常: " + t.getMessage() + "\"}";
                }
                final String r = resJson;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        onPatientsDone(r);
                    }
                });
            }
        }, new Runnable() {
            @Override
            public void run() {
                resetPatientUi();
            }
        });
    }

    /** 就诊人实采结果 -> 回填 Spinner；无论成败都恢复按钮/Spinner 可用。 */
    private void onPatientsDone(String resJson) {
        JSONObject res;
        try {
            res = new JSONObject(resJson);
        } catch (Exception e) {
            log("就诊人结果解析失败: " + e.getMessage());
            resetPatientUi();
            return;
        }
        boolean ok = res.optBoolean("ok", false);
        String err = res.optString("error", "");
        boolean fromCache = res.optBoolean("from_cache", false);
        if (!ok) {
            log("获取就诊人失败: " + err);
            Toast.makeText(this, "获取就诊人失败: " + err, Toast.LENGTH_LONG).show();
        } else {
            JSONArray arr = res.optJSONArray("patients");
            if (arr == null || arr.length() == 0) {
                log("获取就诊人成功，但解析到 0 位就诊人");
            } else {
                fillPatientSpinner(arr);
                log(String.format("获取就诊人成功：%d 位（%s，耗时 %s s）",
                        arr.length(), fromCache ? "缓存" : "实采", res.optString("elapsed_s", "?")));
            }
        }
        resetPatientUi();
    }

    /** 把 patients[] 填入就诊人 Spinner（首项固定「不选择」）。UI 线程调用。 */
    private void fillPatientSpinner(JSONArray arr) {
        List<String> labels = new ArrayList<>();
        List<JSONObject> dicts = new ArrayList<>();
        labels.add(PATIENT_NONE);
        dicts.add(null);
        for (int i = 0; i < arr.length(); i++) {
            JSONObject p = arr.optJSONObject(i);
            if (p == null) continue;
            labels.add(p.optString("label", ("p" + (i + 1))));
            dicts.add(p);
        }
        patList = dicts;
        patItems = labels.toArray(new String[0]);
        // 有非空就诊人 -> 自动选中第一个（index 1；index 0 是"不选择"占位），避免默认落到"不选择"。
        if (patList.size() > 1 && patList.get(1) != null) {
            patientIdx = 1;
            selectedPatient = patList.get(1);
        } else {
            patientIdx = 0;
            selectedPatient = null;
        }
        patient.setAdapter(spinnerAdapter(patItems));
        patient.setSelection(patientIdx);
        Log.i("HH_PAT", "fillPatientSpinner: labels=" + labels.size()
                + " first=" + labels.get(0) + (labels.size() > 1 ? (" second=" + labels.get(1)) : "")
                + " -> selected=" + (selectedPatient == null ? "NONE" : "first"));
    }

    private void resetPatientUi() {
        collecting = false;
        if (patient != null) {
            patient.setEnabled(true);
        }
        final Button btn = findViewById(R.id.btn_get_patients);
        btn.setEnabled(true);
        btn.setText(getString(R.string.btn_get_patients));
        // 采集结束：门禁重新落地（taskBusy 已归 false）
        applyGate();
        // 采集（无论成败）后把挂号助手带回前台：随申办此前被 ensure_444 拉到前台/停在 444 列表页，
        // 用户点完「获取就诊人」不应被晾在随申办里。
        bringSelfToFront();
    }

    /**
     * 把本 App 主界面带回前台。
     *
     * <p><b>验证式阶梯</b>（HyperOS/MIUI 会静默拦截后台 moveToFront / REORDER_TO_FRONT——
     * 调用不抛错但无效，故每一步都要用 onResume 实测是否真的回到前台）：
     * <ol>
     *   <li>反射 AppTask.moveToFront（REORDER_TASKS，普通权限）→ 700ms 后验证 onResume；</li>
     *   <li>未回前台 → startActivity REORDER_TO_FRONT 自重排 → 再验证；</li>
     *   <li>仍未回前台 → 弹「点我回到挂号助手」可点浮层按钮兜底
     *       （点击 = 用户手势 + 持有 SYSTEM_ALERT_WINDOW，属后台启动豁免，必达）。</li>
     * </ol>
     * 全程不触碰随申办的任何控件（红线：只回自己，不点它方）。
     */
    private void bringSelfToFront() {
        if (resumed) {
            return;   // 本就在前台（如门控失败根本没进过随申办）
        }
        // 闸门：设备还被任务占用时不要抢回前台，否则会把正在读的随申办页面踢掉，
        // 并与「拉起随申办」形成无限乒乓（这正是之前一直停在 444/同意页的成因）。
        if (running || collecting || SelfTest.isRunning()) {
            log("[回前台] 仍有任务在运行（running/collecting/selftest），本次不回前台");
            return;
        }
        log("[回前台] 开始回前台阶梯 L1=moveToFront");
        // L1: AppTask.moveToFront（UI 线程发起）
        try {
            android.app.ActivityManager am =
                    (android.app.ActivityManager) getSystemService(Context.ACTIVITY_SERVICE);
            if (am != null) {
                List<?> tasks = am.getAppTasks();
                if (tasks != null && !tasks.isEmpty()) {
                    // AppTask.moveToFront(Bundle)：反射调用，规避部分 compileSdk 的符号缺失
                    // AppTask.moveToFront：不同 ROM 签名不同。HyperOS 上 getMethod("moveToFront", Bundle)
                    // 直接 NoSuchMethodException（实测 2026-10-14），所以按 无参 -> Bundle 两种重载依次试。
                    Object task = tasks.get(0);
                    boolean moved = false;
                    try {
                        task.getClass().getMethod("moveToFront").invoke(task);
                        moved = true;
                    } catch (Throwable ignored) {
                    }
                    if (!moved) {
                        try {
                            task.getClass().getMethod("moveToFront", android.os.Bundle.class)
                                    .invoke(task, (android.os.Bundle) null);
                            moved = true;
                        } catch (Throwable ignored) {
                        }
                    }
                    if (!moved) {
                        log("[回前台] moveToFront 未暴露（ROM 限制），转下一级阶梯");
                    }
                }
            }
        } catch (Throwable t) {
            log("[回前台] moveToFront 异常: " + t.getClass().getSimpleName() + ": " + t.getMessage());
        }
        // 给系统 700ms 调度 onResume（工作线程等待，不阻塞 UI 消息泵），再于 UI 线程复检。
        ui.postDelayed(new Runnable() {
            @Override
            public void run() {
                if (resumed) {
                    log("[回前台] moveToFront 生效，已回前台");
                    Preflight.clearBgLaunchBlocked(MainActivity.this);
                    return;
                }
                // L2: startActivity 自重排
                try {
                    Intent it = new Intent(MainActivity.this, MainActivity.class);
                    it.addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT
                            | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                    startActivity(it);
                } catch (Throwable t) {
                    log("[回前台] 自重排拉起失败: " + t.getMessage());
                }
                ui.postDelayed(new Runnable() {
                    @Override
                    public void run() {
                        if (resumed) {
                            log("[回前台] 自重排生效，已回前台");
                            Preflight.clearBgLaunchBlocked(MainActivity.this);
                            return;
                        }
                        // L2b: 无障碍服务 context 拉起（整条爬虫都靠它拉起随申办，已在国产 ROM 验证有效）
                        try {
                            DeviceBridge br = SsAccessibilityService.bridge();
                            if (br != null && br.launch(getPackageName())) {
                                log("[回前台] 已用无障碍服务通道拉起本 App");
                                // 再等一拍验证；没生效就落到按钮
                                ui.postDelayed(new Runnable() {
                                    @Override
                                    public void run() {
                                        if (resumed) {
                                            log("[回前台] 无障碍服务通道生效，已回前台");
                                            Preflight.clearBgLaunchBlocked(MainActivity.this);
                                            return;
                                        }
                                        Preflight.markBgLaunchBlocked(getApplicationContext());
                                        MiniChip.showTapToReturn(getApplicationContext());
                                        log("[回前台] 系统仍拦截 → 已弹「点我回到挂号助手」按钮，点一下即可");
                                        log("[回前台] 根因多半是 MIUI/HyperOS 的「后台弹出界面」被拒"
                                                + "（appops MIUIOP(10017)=ignore）。开启：设置 → 应用设置 → 应用管理 → "
                                                + "挂号助手 → 权限 → 后台弹出界面 → 允许。"
                                                + "注意：每次 adb 覆盖安装后该权限会被重置，需重新开启。");
                                        try {
                                            Toast.makeText(getApplicationContext(),
                                                    "自动返回被系统拦截：请在 设置→应用管理→挂号助手→权限 "
                                                            + "把「后台弹出界面」设为允许",
                                                    Toast.LENGTH_LONG).show();
                                        } catch (Throwable ignored) {
                                        }
                                    }
                                }, 700L);
                                return;
                            }
                        } catch (Throwable ignored) {
                        }
                        // L3: 可点浮层按钮兜底
                        Preflight.markBgLaunchBlocked(getApplicationContext());
                        MiniChip.showTapToReturn(getApplicationContext());
                        log("[回前台] 系统拦截了后台拉起 → 已弹「点我回到挂号助手」按钮，点一下即可");
                    }
                }, 700L);
            }
        }, 700L);
    }

    // ------------------------------------------------------------------ 自动定位
    /** 自动定位回写的地址文本（供 buildSpecJson 识别"自动定位值"，对齐 web autoLocText）。 */
    private volatile String autoLocText = null;

    /**
     * 「📍 自动定位」：读手机最后已知位置(LocationManager, WGS-84) -> GCJ-02
     * -> (有 AMAP_KEY 时) 高德逆地理成地址。语义对齐 web /api/device-loc：
     *   有地址 -> 回填 loctext（地址文本，pipeline 再用高德 geocode）；
     *   无地址 -> 回填 loc 经纬度（"lng,lat"，pipeline parse_loc 直读）。
     * 端上差异：无 adb，改用 LocationManager（只读 lastKnownLocation，不申请/不持续定位）。
     */
    private void doAutoloc() {
        final Button btn = findViewById(R.id.btn_autoloc);
        btn.setEnabled(false);
        btn.setText("定位中…");
        // getLastKnownLocation 需运行时位置权限（HyperOS/Android 12+ 未授予时返回 null）。
        // 先确保已授予，未授予则弹系统申请，用户同意后再定位。
        final java.util.List<String> missing = new java.util.ArrayList<>();
        if (this.checkSelfPermission(android.Manifest.permission.ACCESS_FINE_LOCATION)
                != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            missing.add(android.Manifest.permission.ACCESS_FINE_LOCATION);
        }
        if (missing.isEmpty()) {
            doAutolocCore();
            return;
        }
        log("申请位置权限（自动定位需要）…");
        try {
            requestPermissions(missing.toArray(new String[0]), 1001);
        } catch (Throwable t) {
            btn.setEnabled(true);
            btn.setText(getString(R.string.btn_autoloc));
            log("无法申请权限: " + t.getMessage());
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == 1001) {
            boolean ok = grantResults.length > 0
                    && grantResults[0] == android.content.pm.PackageManager.PERMISSION_GRANTED;
            if (ok) {
                log("位置权限已授予 -> 继续自动定位");
                doAutolocCore();
            } else {
                log("位置权限被拒 -> 自动定位不可用（可手填地址/经纬度）");
                setAutolocStatus("⚠ 未授予位置权限，自动定位不可用（请手填位置）", true);
                resetAutolocBtn();
            }
        }
    }

    private void doAutolocCore() {
        log("=== 📍 自动定位（读手机最后位置 -> 逆地理）===");
        runAsync(new Runnable() {
            @Override
            public void run() {
                String resJson;
                try {
                    Python py = Python.getInstance();
                    PyObject mod = py.getModule("app.auto_locate");
                    PyObject out = mod.callAttr("autolocate_json", true);
                    resJson = out.toJava(String.class);
                } catch (Throwable t) {
                    log("[error] " + t.getClass().getSimpleName() + ": " + t.getMessage());
                    resJson = "{\"ok\":false,\"error\":\"Python 异常: " + t.getMessage() + "\"}";
                }
                final String r = resJson;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        onAutolocDone(r);
                    }
                });
            }
        });
    }

    private void onAutolocDone(String resJson) {
        JSONObject res;
        try {
            res = new JSONObject(resJson);
        } catch (Exception e) {
            log("自动定位结果解析失败: " + e.getMessage());
            setAutolocStatus(getString(R.string.autoloc_fail) + "结果解析失败", true);
            resetAutolocBtn();
            return;
        }
        boolean ok = res.optBoolean("ok", false);
        String err = res.optString("error", "");
        String address = res.optString("address", "");
        Double lng = res.optDouble("lng", Double.NaN);
        Double lat = res.optDouble("lat", Double.NaN);
        boolean hasKey = res.optBoolean("has_amap_key", false);
        boolean snapped = res.optBoolean("amap_snapped", false);
        String provider = res.optString("provider", "?");
        if (!ok) {
            setAutolocStatus(getString(R.string.autoloc_fail) + err + "（请手填位置）", true);
            log("自动定位失败: " + err);
            resetAutolocBtn();
            return;
        }
        if (!address.isEmpty()) {
            loctext.setText(address);
            autoLocText = address;
            setAutolocStatus(getString(R.string.autoloc_ok)
                    + (provider + " · " + address) + (hasKey ? "" : getString(R.string.autoloc_nokey)), false);
            log("✓ 自动定位(地址): " + address + "  [" + provider + "]" + (hasKey ? "" : " [无AMAP_KEY 仅坐标]"));
        } else if (lng != Double.NaN && lat != Double.NaN) {
            // 无地址（理论上内置 key 不会走到这）：坐标写进 loctext，buildSpecJson 识别为 "lng,lat"
            String lc = lng + "," + lat;
            loctext.setText(lc);
            autoLocText = lc;
            setAutolocStatus(getString(R.string.autoloc_ok)
                    + lc + "  [provider=" + provider + "]" + getString(R.string.autoloc_nokey), false);
            log("✓ 自动定位(坐标): " + lc + "  [provider=" + provider + "] 无AMAP_KEY 仅坐标");
        } else {
            setAutolocStatus(getString(R.string.autoloc_fail) + "无坐标", true);
        }
        resetAutolocBtn();
    }

    private void setAutolocStatus(String text, boolean bad) {
        if (autolocStatus == null) return;
        autolocStatus.setText(text);
        autolocStatus.setTextColor(bad ? 0xFFCC3333 : 0xFF668866);
    }

    private void resetAutolocBtn() {
        final Button btn = findViewById(R.id.btn_autoloc);
        btn.setEnabled(true);
        btn.setText(getString(R.string.btn_autoloc));
    }

    private void refreshServiceStatus() {
        boolean ready = SsAccessibilityService.isReady();
        if (ready) {
            serviceStatus.setText(getString(R.string.svc_ready));
            serviceStatus.setBackgroundColor(0xFFDDF5DD);
            return;
        }
        // 冷启 / force-stop 之后，系统已启用本服务但 onServiceConnected 尚未回调。
        // 用系统侧 enabled_accessibility_services 判定：在系统启用列表 = 只是时序未绑定（会自愈），
        // 不在列表 = 真的没授权（用户需手动开启）。
        boolean enabledInSystem = ServiceGate.isEnabledInSystem(this);
        if (enabledInSystem) {
            serviceStatus.setText(getString(R.string.svc_binding));
            serviceStatus.setBackgroundColor(0xFFFFF3CD);
        } else {
            serviceStatus.setText(getString(R.string.svc_missing));
            serviceStatus.setBackgroundColor(0xFFFFE0E0);
        }
    }

    /**
     * 「去开启 / 一键开启」：
     * <ol>
     *   <li>若本机已获授 WRITE_SECURE_SETTINGS（一次性 adb pm grant）→ 直接改写 secure 启用列表，
     *       几秒内 onServiceConnected 会异步回来，这里先提示"正在绑定"并稍后重扫；</li>
     *   <li>否则跳到系统「辅助功能」页，并把 HyperOS 的手动路径（入口藏在最底部）用弹窗写清楚。</li>
     * </ol>
     * 只打开设置页 / 只改系统开关，绝不在随申办里点任何业务按钮（红线）。
     */
    private void openOrEnableAccessibility() {
        if (Preflight.canSelfEnable(this)) {
            boolean written = ServiceGate.bestEffortEnable(this);
            log("preflight: 一键开启无障碍 written=" + written);
            Toast.makeText(this, written
                    ? "已开启，正在绑定…（约 1-3 秒，本页会自动刷新）"
                    : "写入未生效，仍需手动开启（见下方步骤）", Toast.LENGTH_LONG).show();
            if (written) {
                // 绑定是异步的：稍后重扫，让 ✓ 自己亮起来
                ui.postDelayed(new Runnable() {
                    @Override
                    public void run() {
                        refreshServiceStatus();
                        refreshPreflightResume();
                    }
                }, 1500L);
                refreshPreflightResume();
                return;
            }
        }
        // 手动路径：先在 App 内把步骤讲清楚，用户看完再点「打开设置」跳过去操作，
        // 避免直接把人丢到辅助功能页却找不到开关（HyperOS 把第三方服务藏在页面底部）。
        try {
            new AlertDialog.Builder(this)
                    .setTitle("如何开启「挂号助手 · 读屏桥」")
                    .setMessage(Preflight.a11yManualPath())
                    .setPositiveButton("打开设置", new DialogInterface.OnClickListener() {
                        @Override
                        public void onClick(DialogInterface d, int which) {
                            openAccessibilitySettings();
                        }
                    })
                    .setNegativeButton("知道了", null)
                    .show();
        } catch (Throwable t) {
            log("preflight: 路径弹窗失败 " + t.getMessage());
            openAccessibilitySettings();
        }
    }

    private void openAccessibilitySettings() {
        // HyperOS 上 ACTION_ACCESSIBILITY_SETTINGS 会解析到 MiuiAccessibilitySettingsActivity；
        // 万一被 ROM 屏蔽，再兜底显式组件名 / 应用详情页。
        try {
            startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS));
            return;
        } catch (Throwable ignored) {
        }
        try {
            Intent i = new Intent();
            i.setClassName("com.android.settings",
                    "com.android.settings.accessibility.MiuiAccessibilitySettingsActivity");
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(i);
            return;
        } catch (Throwable ignored) {
        }
        try {
            startActivity(new Intent(Settings.ACTION_SETTINGS));
        } catch (Exception e) {
            Toast.makeText(this, R.string.toast_need_service, Toast.LENGTH_LONG).show();
        }
    }

    // ------------------------------------------------------------------ 动作

    private void doLaunch() {
        withService(new Runnable() {
            @Override
            public void run() {
                DeviceBridge br = SsAccessibilityService.bridge();
                boolean ok = br != null && br.launch(DeviceBridge.TARGET_PKG);
                log("launch(随申办) -> " + ok);
                if (!ok) {
                    log("失败：随申办可能未安装，或 Android11+ 缺少 <queries> 包可见性声明");
                }
            }
        });
    }

    private void doDump() {
        withService(new Runnable() {
            @Override
            public void run() {
                DeviceBridge br = SsAccessibilityService.bridge();
                if (br == null) {
                    log("读屏失败：服务已断开");
                    return;
                }
                JSONObject tree = br.dump();
                if (tree == null) {
                    log("读屏为 null：未取到随申办窗口 —— 确认随申办在前台、且读屏桥已授权");
                    return;
                }
                int n = NodeTree.count(tree);
                log("读屏成功：节点数 = " + n);
                setResultText(NodeTree.pretty(tree));
                showTab(TAB_RESULT);
            }
        });
    }

    /** P3 主入口：表单 → runner.run（端上 9 段流水线）。 */
    private void doRunPipeline() {
        if (running) {
            Toast.makeText(this, "流水线运行中…", Toast.LENGTH_SHORT).show();
            return;
        }
        // 门禁兜底：即便按钮态被别处复位，未就绪也拦住（并弹回检查框）
        if (!gateReady) {
            blockByGate();
            return;
        }
        if (preflightBusy) {
            Toast.makeText(this, "前置检查中（设备独占）—— 请稍候", Toast.LENGTH_SHORT).show();
            return;
        }
        final String symptomText = symptom.getText().toString().trim();
        if (symptomText.isEmpty()) {
            Toast.makeText(this, R.string.toast_need_symptom, Toast.LENGTH_LONG).show();
            return;
        }
        saveConfig();
        final String base = agentsvcBase.getText().toString().trim();
        final String key = "";   // 上游凭据在 hh-server 侧，App 不再发送 Authorization
        String locText = loctext.getText().toString().trim();
        final boolean liveOn = live.isChecked();
        final int feePick = feeIdx;
        final int slotPick = slotIdx;
        // 对齐 web go.onclick：自动定位若只写进了地址/坐标且用户未改，则以 autoLocText 为准；
        // 且位置必填（loctext 非空，可以是地址文本或 "lng,lat"）。
        String al = autoLocText;
        if (al != null && !al.isEmpty() && (locText.isEmpty() || locText.equals(al))) {
            locText = al;
        }
        if (locText.isEmpty()) {
            Toast.makeText(this, "请填位置（可点「📍 自动定位」或手填地址/经纬度）", Toast.LENGTH_LONG).show();
            return;
        }

        final String finalLocText = locText;
        ForegroundKeeperService.start(this);
        running = true;
        setRunUi(true);          // 按钮置灰 + 自动切到「实时日志」，避免重复点击
        lastResultJson = null;
        log("");
        log("=== 开始端上流水线 ===");
        withService(new Runnable() {
            @Override
            public void run() {
                String spec = buildSpecJson(symptomText, finalLocText, feePick, slotPick, liveOn);
                String res;
                try {
                    Python py = Python.getInstance();
                    PyObject mod = py.getModule("runner");
                    PyObject out = mod.callAttr("run", spec, base, key);
                    res = out.toJava(String.class);
                } catch (Throwable t) {
                    res = "{\"ok\":false,\"error\":\"Python 异常: "
                            + t.getClass().getName() + ": " + t.getMessage() + "\"}";
                    log("[error] " + t.getClass().getSimpleName() + ": " + t.getMessage());
                }
                final String r = res;
                lastResultJson = r;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        onPipelineDone(r);
                    }
                });
            }
        }, new Runnable() {
            @Override
            public void run() {
                // 门控失败：复位运行状态，避免卡在"运行中"
                running = false;
                setRunUi(false);
                ForegroundKeeperService.stop(MainActivity.this);
            }
        });
    }

    /**
     * 运行期按钮态：busy=true -> 「获取号源推荐」置灰显示"获取中…"，并自动切到「实时日志」面板；
     * busy=false -> 恢复可点文字，避免用户重复点击。
     */
    private void setRunUi(final boolean busy) {
        ui.post(new Runnable() {
            @Override
            public void run() {
                if (btnRun != null) {
                    // 结束后是否可点，仍受门禁约束（gateReady）
                    btnRun.setEnabled(!busy && gateReady);
                    btnRun.setText(busy ? "获取中…" : getString(R.string.btn_recommend));
                    btnRun.setAlpha(busy ? 0.5f : 1.0f);
                }
                if (busy) {
                    showTab(TAB_LOG);
                }
                if (!busy) {
                    applyGate();   // 依据门禁重新决定整块表单可点性
                }
            }
        });
    }

    /** 门禁拦截时的统一反馈：弹回检查框 + 提示。 */
    private void blockByGate() {
        preflightDismissed = false;
        if (checkCard != null) {
            checkCard.setVisibility(View.VISIBLE);
        }
        showTab(TAB_FORM);
        Toast.makeText(this, R.string.check_gate_toast, Toast.LENGTH_LONG).show();
        refreshPreflightResume();
    }

    private String buildSpecJson(String symptomText, String locText,
                                 int feeIdx, int slotIdx, boolean liveOn) {
        JSONObject spec = new JSONObject();
        try {
            spec.put("symptom_text", symptomText);
            // 位置：显式经纬度字段(loc)优先；否则地址文本(loctext)；loctext 若形如 "lng,lat" 也当坐标。
            // 位置：loctext 现承载「地址文本」或「lng,lat 坐标」（经纬度输入字段已简化掉）。
            if (!locText.isEmpty()) {
                // 形如 "lng,lat" → 直接给 loc；否则给地址文本（高德/内置小表解析）
                if (locText.matches("^\\s*[\\d.]+\\s*[,，]\\s*[\\d.]+\\s*$")) {
                    spec.put("loc", locText);
                } else {
                    spec.put("loc_text", locText);
                }
            }
            spec.put("fee_expect", feeExpectKey(feeIdx));
            // 实时号源 · 子tab（对齐 web slot_type -> --live-slot-type；仅 live 时 runner 生效）
            String st = slotTypeKey(slotIdx);
            if (st != null) {
                spec.put("slot_type", st);
            }
            // 就诊人（对齐 web spec_from_dict）：
            //   选中某人 -> patient=dict(含 name_masked/age/gender/insurance_card/source)，patient_selected=true
            //   首项「不选择」-> patient=null + patient_selected=false（dialogue 层据此 proxy=True）
            JSONObject pat = selectedPatient;
            if (pat != null) {
                spec.put("patient", pat);
                spec.put("patient_selected", true);
            } else {
                spec.put("patient", JSONObject.NULL);
                spec.put("patient_selected", false);
            }
            spec.put("live", liveOn);
            // 运行参数（设置页）：候选集数量 -> probe_n，推荐数量 -> top_n
            // 优先取当前输入框值（未点保存也能即时生效），兜底 SharedPreferences。
            spec.put("probe_n", parseIntSetting(fieldOrCfg(candN, CFG_CAND_N, DEFAULT_CAND_N), DEFAULT_CAND_N));
            spec.put("top_n", parseIntSetting(fieldOrCfg(topN, CFG_TOP_N, DEFAULT_TOP_N), DEFAULT_TOP_N));
        } catch (Exception ignored) {
        }
        return spec.toString();
    }

    private String fieldOrCfg(android.widget.EditText field, String key, int def) {
        if (field != null && field.getText().length() > 0) {
            return field.getText().toString();
        }
        return getCfg(key, String.valueOf(def));
    }

    private int parseIntSetting(String v, int def) {
        try {
            int n = Integer.parseInt((v == null ? "" : v).trim());
            return n > 0 ? n : def;
        } catch (Exception e) {
            return def;
        }
    }

    /**
     * 预计耗时估算（实时，仅供心理参考）。
     * 耗时主要由：候选集数量(probe_n)、推荐数量(top_n)、号源子tab、预期费用档位决定。
     * 模型：基础推理 + 实时探测 × 子tab系数 × 费用扩展系数。线性近似，给一个区间。
     */
    private void refreshEstimate() {
        if (estTime == null) {
            return;
        }
        try {
            boolean liveOn = live != null && live.isChecked();
            int probe = parseIntSetting(fieldOrCfg(candN, CFG_CAND_N, DEFAULT_CAND_N), DEFAULT_CAND_N);
            int top = parseIntSetting(fieldOrCfg(topN, CFG_TOP_N, DEFAULT_TOP_N), DEFAULT_TOP_N);

            String msg;
            if (!liveOn) {
                msg = "约 10–60 秒（离线候选集，不含实时号源核实）";
            } else {
                // 子tab 系数：全部 = 1.0（最全最慢）；单子tab ≈ 1/3（参考 main.py 帮助文档 expert≈1/3 耗时）
                double slotFactor = (slotIdx == 0) ? 1.0 : 0.34;
                // 费用档位：越高的预期费用 → 候选面越宽 → 探测范围越大。auto/不限 保守 1.0；
                // 具体档位(元/百/千/万) 按档位逐级小幅扩展（越高越宽）。
                double feeFactor = 1.0;
                switch (feeIdx) {
                    case 2: feeFactor = 1.15; break;   // 10元
                    case 3: feeFactor = 1.30; break;   // 百元
                    case 4: feeFactor = 1.45; break;   // 千元
                    case 5: feeFactor = 1.60; break;   // 万元
                    default: break;                    // 自动(0) / 不限(6) 保守
                }
                double baseProbe = 60.0;                              // 基准（默认12候选·单子tab·无费用扩展）
                double probeCost = baseProbe * (probe / 12.0) * slotFactor * feeFactor;
                double deepCost = 40.0 * (top / 3.0);                 // Phase B 深核随推荐数量增长
                double low = 35.0 + 0.55 * (probeCost + deepCost);
                double high = 55.0 + 1.35 * (probeCost + deepCost);
                msg = "≈ " + fmtMin(low) + "–" + fmtMin(high)
                        + "（候选集 " + probe + " · 推荐 " + top + " · " + slotLabel(slotIdx) + " · 费用档 " + feeLabel(feeIdx) + "）";
            }
            estTime.setText(msg);
        } catch (Exception ignored) {
        }
    }

    private String fmtMin(double s) {
        if (s < 90) {
            return (int) Math.round(s) + "秒";
        }
        double m = s / 60.0;
        if (m < 10) {
            return String.format(java.util.Locale.US, "%.1f分钟", m);
        }
        return (int) Math.round(m) + "分钟";
    }

    private String slotLabel(int idx) {
        switch (idx) {
            case 1: return "专家门诊";
            case 2: return "普通门诊";
            case 3: return "专病门诊";
            default: return "全部子tab";
        }
    }

    private String feeLabel(int idx) {
        switch (idx) {
            case 1: return "0元";
            case 2: return "10元";
            case 3: return "百元";
            case 4: return "千元";
            case 5: return "万元";
            case 6: return "不限";
            default: return "自动";
        }
    }

    private String feeExpectKey(int idx) {
        switch (idx) {
            case 1: return "zero";
            case 2: return "ten";
            case 3: return "hundred";
            case 4: return "thousand";
            case 5: return "tenk";
            case 6: return "any";
            default: return "auto";
        }
    }

    /** slottype spinner -> web slot_type（index 0 全部子tab=null 不传；仅专家/普通/专病）。 */
    private String slotTypeKey(int idx) {
        switch (idx) {
            case 1: return "expert";
            case 2: return "normal";
            case 3: return "specialty";
            default: return null;
        }
    }

    private void onPipelineDone(String resJson) {
        running = false;
        setRunUi(false);
        flushReasoning();      // 最后一段推理立刻上屏
        // 任务结束：浮窗不再误显"运行中"，但也不立刻消失 —— 先给一条收尾文案，
        // 解析出结果后再换成结论（成功 N 个 / 失败原因），十几秒后自动收起。
        MiniChip.showFinal(getApplicationContext(), "运行结束 · 正在回到挂号助手…", 20000L);
        ForegroundKeeperService.stop(this);
        JSONObject res;
        try {
            res = new JSONObject(resJson);
        } catch (Exception e) {
            log("结果解析失败: " + e.getMessage());
            bringSelfToFront();   // 解析失败也要从随申办回前台
            return;
        }
        boolean ok = res.optBoolean("ok", false);
        String err = res.optString("error", "");
        JSONObject result = res.optJSONObject("result");
        JSONObject finalObj = (result == null) ? null : result.optJSONObject("final");
        JSONArray recs = (finalObj == null) ? null : finalObj.optJSONArray("recommendations");
        int n = (recs == null) ? 0 : recs.length();
        log("[runner] rc=" + res.optInt("rc") + " ok=" + ok + (err.isEmpty() ? "" : " err=" + err));

        // 浮窗上把结论说清楚（成功几个号源 / 失败原因截断），别让用户猜。
        String why = err;
        if (why.isEmpty() && n == 0) {
            why = "无推荐号源";
        }
        if (why.length() > 90) {
            why = why.substring(0, 90) + "…";
        }
        MiniChip.showFinal(getApplicationContext(),
                ok ? ("完成 · 推荐 " + n + " 个号源") : ("未成功：" + why), 20000L);

        // 富渲染（对齐 web renderResult）：本次输入 / 推理 / 权重 / 推荐 / 实时核实 / 断言 / 排除 / 落盘。
        setResultText(ResultRenderer.render(res));
        showTab(TAB_RESULT);   // 结束自动切到「结果」面板

        // 抓取把随申办带到了前台并停在 444/号源页；任务结束要把挂号助手带回前台。
        bringSelfToFront();

        saveHistory(ok ? "挂号推荐" : "挂号推荐(失败)", summarizeRun(res, n), res);
    }

    /** 历史列表一行的摘要：top1 + 推荐数 + 断言，替代原先干瘪的「推荐=N rc=0」。 */
    private String summarizeRun(JSONObject res, int n) {
        StringBuilder s = new StringBuilder();
        JSONObject result = res.optJSONObject("result");
        JSONObject finalObj = result == null ? null : result.optJSONObject("final");
        JSONArray recs = finalObj == null ? null : finalObj.optJSONArray("recommendations");
        if (recs != null && recs.length() > 0) {
            JSONObject t1 = recs.optJSONObject(0);
            if (t1 != null) {
                s.append("Top1 ").append(t1.optString("name"))
                        .append("(").append(t1.optString("final_score")).append(") ");
            }
        }
        int chk = result != null && result.optJSONArray("checks") != null
                ? result.optJSONArray("checks").length() : 0;
        s.append("推荐").append(n).append("家");
        if (chk > 0) {
            s.append(" 断言").append(chk).append("项");
        }
        s.append(" rc=").append(res.optInt("rc"));
        if (res.has("out_dir") && !res.optString("out_dir").isEmpty()) {
            s.append(" · ").append(res.optString("out_dir"));
        }
        return s.toString();
    }

    private void doTestLlm() {
        final String base = agentsvcBase.getText().toString().trim();
        final String key = "";   // 归集后不再需要 App 侧 key
        saveConfig();
        final String symptomText = symptom.getText().toString().trim();
        runAsync(new Runnable() {
            @Override
            public void run() {
                try {
                    String sys = "只输出 JSON，不要解释、不要 markdown 代码块";
                    String user = symptomText.isEmpty()
                            ? "请输出一个 JSON 对象: {\"ok\": true, \"from\": \"android\"}"
                            : "把这句话解析成 JSON（字段: 症状, 希望等待天数, 是否三甲偏好）"
                              + "，只输出 JSON 对象本身。原句: " + symptomText;
                    long t0 = System.currentTimeMillis();
                    com.hospitalhelper.controller.AgentClient.JsonResult jr =
                            AgentClient.chatJson(sys, user, null, base, key);
                    long dt = System.currentTimeMillis() - t0;
                    log("LLM base=" + AgentClient.resolveBase(base) + " elapsed=" + dt + "ms"
                            + " attempts=" + jr.meta.attempts + " thinkingChars=" + jr.meta.thinkingChars);
                    log("LLM content=" + jr.meta.content);
                    setResultText(ResultRenderer.render(jr.obj));
                    saveHistory("LLM 推理",
                            "内容 " + jr.meta.content.length() + " 字 · 耗时 " + dt + "ms", jr.obj);
                } catch (AgentClient.AgentsvcError e) {
                    log("LLM 失败: " + e.getMessage());
                }
            }
        });
    }

    // ------------------------------------------------------------------ 历史

    /** 自检结束：把挂号助手带回前台（自检期间随申办被拉到前台读屏）。 */
    private void onSelfTestDoneUi(final boolean ok, final String oneLine) {
        flushReasoning();      // 最后一段推理立刻上屏
        ui.post(new Runnable() {
            @Override
            public void run() {
                Button btn = findViewById(R.id.btn_selftest);
                if (btn != null && !SelfTest.isRunning()) {
                    btn.setEnabled(true);
                }
                log("[自检] 结束 " + oneLine + (oneLine != null && oneLine.contains("nav_busy")
                        ? " → 不回前台（随申办仍被别的操作占用）" : " → 回前台"));
                // 自检结束时人还在随申办那边，浮窗是唯一的「结果条」：把摘要写上去再回前台。
                String brief = oneLine;
                if (brief.length() > 90) {
                    brief = brief.substring(0, 90) + "…";
                }
                MiniChip.showFinal(getApplicationContext(),
                        (ok ? "自检完成 · " : "自检未通过 · ") + brief, 20000L);
                // stop=nav_busy 意味着这次自检**一步都没走**（设备被另一个操作合法占用）。
                // 此时抢回前台会把那个操作正在读的页面踢掉，并且把 MainActivity 盖在
                // 别人正在驱动的随申办上（2026-10-16 实测：自检被 patients_fetch 挡住后
                // 回前台，紧接着的登录深检因此拿到 BAD）。
                if (oneLine != null && oneLine.contains("nav_busy")) {
                    return;
                }
                bringSelfToFront();
            }
        });
    }

    /** P4 自检：端上 in-process 跑一次号源抓取（只读；绝不进下单路径）。结果走 logcat(HH_SELFTEST) + files/out/selftest_last.json。 */
    private void doSelfTest() {
        if (SelfTest.isRunning()) {
            Toast.makeText(this, "自检运行中…（看 logcat -s HH_SELFTEST）", Toast.LENGTH_SHORT).show();
            return;
        }
        Button btn = findViewById(R.id.btn_selftest);
        if (btn != null) {
            btn.setEnabled(false);
        }
        log("=== 开始自检（端上 in-process 号源抓取；只读，不预约）===");
        // 自检也切到「实时日志」：推理框自带滚动、始终滚到底，看着"在动"就不像卡死。
        showTab(TAB_LOG);
        // 复用最近一次广播传入的 manifest（若曾传过），否则用内置固定条件。
        SelfTest.run(this, SelfTest.lastRemembered(this));
        // 自检是异步的：跑完（logcat / selftest_last.json 落地后）这里无法直接回调，
        // 故不做同步置灰复位；用 isRunning() 防抖即可。
        ui.postDelayed(new Runnable() {
            @Override
            public void run() {
                if (btn != null && !SelfTest.isRunning()) {
                    btn.setEnabled(true);
                }
            }
        }, 60_000L);
    }

    /**
     * 仅 {@code BuildConfig.DEBUG} 才注册 debug 广播接收器（runtime-registered，无 manifest 声明）。
     * release 构建里 {@code BuildConfig.DEBUG=false}，{@code SelfTestReceiver} 不会被注册，
     * 因此 {@code am broadcast} 触达不到任何目标（不会在 release 暴露自检入口）。
     */
    private void registerDebugSelfTestReceiver() {
        if (!BuildConfig.DEBUG) {
            Log.i(SelfTest.TAG, "[register] 非 DEBUG 构建：跳过自检广播接收器（不暴露）");
            return;
        }
        try {
            IntentFilter f = new IntentFilter(SelfTestReceiver.ACTION);
            debugSelfTestReceiver = new SelfTestReceiver();
            if (Build.VERSION.SDK_INT >= 33) {
                registerReceiver(debugSelfTestReceiver, f, Context.RECEIVER_EXPORTED);
            } else {
                registerReceiver(debugSelfTestReceiver, f);
            }
            Log.i(SelfTest.TAG, "[register] 自检广播接收器已注册（action=" + SelfTestReceiver.ACTION + "）");
        } catch (Exception e) {
            Log.w(SelfTest.TAG, "[register] 自检接收器注册失败: " + e);
        }
        try {
            IntentFilter f2 = new IntentFilter(LlmPingReceiver.ACTION);
            debugLlmPingReceiver = new LlmPingReceiver();
            if (Build.VERSION.SDK_INT >= 33) {
                registerReceiver(debugLlmPingReceiver, f2, Context.RECEIVER_EXPORTED);
            } else {
                registerReceiver(debugLlmPingReceiver, f2);
            }
            Log.i(LlmPingReceiver.TAG, "[register] LLM_PING 接收器已注册（action="
                    + LlmPingReceiver.ACTION + "）");
        } catch (Exception e) {
            Log.w(LlmPingReceiver.TAG, "[register] LLM_PING 接收器注册失败: " + e);
        }
    }

    private void saveHistory(String title, String sub, JSONObject result) {
        try {
            JSONObject task = new JSONObject();
            task.put("id", "t" + System.currentTimeMillis());
            task.put("ts", System.currentTimeMillis());
            task.put("title", title);
            task.put("sub", sub);
            if (result != null) {
                task.put("result", result);
            }
            HistoryStore.save(this, task);
            renderHistory();
        } catch (Exception ignored) {
        }
    }

    private void renderHistory() {
        ui.post(new Runnable() {
            @Override
            public void run() {
                historyBox.removeAllViews();
                List<JSONObject> tasks = HistoryStore.list(MainActivity.this);
                if (tasks.isEmpty()) {
                    TextView tv = new TextView(MainActivity.this);
                    tv.setText(R.string.history_placeholder);
                    historyBox.addView(tv);
                    return;
                }
                LayoutInflater inflater = LayoutInflater.from(MainActivity.this);
                SimpleDateFormat fmt = new SimpleDateFormat("MM-dd HH:mm:ss", Locale.CHINA);
                for (JSONObject t : tasks) {
                    final View row = inflater.inflate(R.layout.item_history, historyBox, false);
                    TextView title = row.findViewById(R.id.h_title);
                    TextView sub = row.findViewById(R.id.h_sub);
                    final JSONObject task = t;
                    String ts = fmt.format(new Date(t.optLong("ts", 0)));
                    title.setText(ts + "  " + t.optString("title"));
                    sub.setText(t.optString("sub"));
                    row.setOnClickListener(new View.OnClickListener() {
                        @Override
                        public void onClick(View v) {
                            openHistory(task);
                        }
                    });
                    historyBox.addView(row);
                    // 行间细分隔线
                    View div = new View(MainActivity.this);
                    div.setBackgroundColor(0x14000000);
                    historyBox.addView(div, new LinearLayout.LayoutParams(
                            LinearLayout.LayoutParams.MATCH_PARENT, 1));
                }
            }
        });
    }

    /** 点历史条目 -> 用与实时结果一致的富渲染，切到「结果」面板回看细节。 */
    private void openHistory(JSONObject task) {
        Object result = task.opt("result");
        if (result == null) {
            Toast.makeText(this, "该记录无保存结果", Toast.LENGTH_SHORT).show();
            return;
        }
        String ts = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.CHINA)
                .format(new Date(task.optLong("ts", 0)));
        String head = "〔历史回看 · " + ts + " · " + task.optString("title") + " 〕\n"
                + "────────────────────────────\n";
        setResultText(head + ResultRenderer.render(result));
        showTab(TAB_RESULT);
    }

    // ------------------------------------------------------------------ 工具

    /**
     * 冷启就绪门控（异步版）：在 io 后台线程上跑 {@link ServiceGate#ensure}，就绪后执行
     * {@code task}（同样在 io 线程），未就绪则回 UI 线程 toast + 打开系统设置。
     *
     * <p><b>为什么必须放后台线程：</b>{@code onServiceConnected()} 由系统在<b>主线程</b>派发，
     * 若在主线程上 {@code Thread.sleep} 轮询，主线程 looper 被占住，{@code onServiceConnected}
     * 永远收不到 → 服务永远绑不上 → 必然 10s 超时并 ANR。放到 io 线程后，主线程空闲，
     * 系统可正常回调，轮询即可命中。
     *
     * <p>冷启典型耗时：已绑定=0ms；刚授权/刚 force-stop 后=数百 ms（等 onServiceConnected）。
     */
    private void withService(Runnable task) {
        withService(task, null);
    }

    /**
     * 同 {@link #withService(Runnable)}，额外接受门控失败回调（在 UI 线程执行），
     * 供 doRunPipeline 复位 running / 停前台服务等清理动作。
     */
    private void withService(final Runnable task, final Runnable onFail) {
        runAsync(new Runnable() {
            @Override
            public void run() {
                long t0 = System.currentTimeMillis();
                boolean ok = ServiceGate.ensure(MainActivity.this);
                if (ok) {
                    long dt = System.currentTimeMillis() - t0;
                    log("[gate] 读屏桥就绪（耗时 " + dt + " ms）");
                    ui.post(new Runnable() {
                        @Override
                        public void run() {
                            refreshServiceStatus();
                        }
                    });
                    // 回到 io 线程执行任务（runAsync 是单线程池，已在其中，直接跑）
                    task.run();
                } else {
                    final boolean enabled = ServiceGate.isEnabledInSystem(MainActivity.this);
                    ui.post(new Runnable() {
                        @Override
                        public void run() {
                            log("[gate] 读屏桥未就绪：" + (enabled
                                    ? " 已启用但未绑定（重试或检查系统）"
                                    : " 未在系统启用列表（需手动开启）"));
                            Toast.makeText(MainActivity.this, R.string.toast_need_service, Toast.LENGTH_LONG).show();
                            openAccessibilitySettings();
                            if (onFail != null) {
                                onFail.run();
                            }
                        }
                    });
                }
            }
        });
    }

    private void runAsync(Runnable r) {
        io.execute(r);
    }

    private void log(final String line) {
        // 同步镜像到 logcat：PC 侧 `adb logcat -s HH_APP HH_GATE HH_FRONT` 可直接复盘
        // App 内日志（含回前台阶梯每一步决策），不必再靠截图。
        try {
            Log.i("HH_APP", line.replace('\n', ' '));
        } catch (Throwable ignored) {
        }
        ui.post(new Runnable() {
            @Override
            public void run() {
                logBuf.append(line).append('\n');
                if (logBuf.length() > LOG_MAX_CHARS) {
                    logBuf.delete(0, logBuf.length() - LOG_MAX_CHARS);
                }
                scheduleLogRender();
            }
        });
    }

    /** debug 回放入口：当前 Activity（可能为 null）。 */
    public static MainActivity live() {
        return LIVE.get();
    }

    public interface ReplayDone {
        void run(long elapsedMs, int renders);
    }

    private static final String[] REPLAY_FRAGMENTS = {
            "用户主诉视物模糊，", "需要先排除眼底问题；", "按症状→科室映射，",
            "眼科门诊是首选；", "再看专家号源分布，", "徐建江/于靖 号源偏紧，",
            "冯乐 副主任医师 有余量，", "综合考虑距离与费用，", "推荐顺序：于靖 > 冯乐 > 刘芳。",
            "The ranking weighs distance, ", "slot availability and title level; ",
            "slot_type=expert means consultant slots only.\n",
    };

    /**
     * debug 专用：按真实 SSE 的节奏灌假分块，单独压测推理框渲染路径。
     *
     * <p>自检走的是纯号源抓取（不调 LLM），复现不了这段渲染；旧机型 MI 5X 恰恰是在
     * 这段弹的「应用无响应」，所以给它一条可重复的压测入口：分块数 × 间隔 = 一次流式推理。
     */
    public void replayReasoning(final int chunks, final long delayMs, final ReplayDone done) {
        showTab(TAB_LOG);
        reasonRenderCount = 0;
        final long t0 = System.currentTimeMillis();
        io.execute(new Runnable() {
            @Override
            public void run() {
                appendReasoning("—— 模型推理开始 ——\n", "meta");
                for (int i = 0; i < chunks; i++) {
                    appendReasoning(REPLAY_FRAGMENTS[i % REPLAY_FRAGMENTS.length],
                            (i % 7 == 0) ? "think" : "content");
                    if (delayMs > 0) {
                        try {
                            Thread.sleep(delayMs);
                        } catch (InterruptedException e) {
                            break;
                        }
                    }
                }
                appendReasoning("\n—— 模型推理结束 ——\n", "meta");
                flushReasoning();
                if (done != null) {
                    done.run(System.currentTimeMillis() - t0, reasonRenderCount);
                }
            }
        });
    }

    /** 详细日志：缓冲立即累积，视图最多每 LOG_FRAME_MS 刷一次（一行一 setText 太贵）。 */
    private void scheduleLogRender() {
        if (logPending || logView == null) {
            return;
        }
        logPending = true;
        ui.postDelayed(new Runnable() {
            @Override
            public void run() {
                logPending = false;
                if (logView != null) {
                    logView.setText(logBuf.toString());
                }
            }
        }, LOG_FRAME_MS);
    }

    /** 模型推理（两次 LLM 调用的 thinking + 正文）。恒显、主展示区。
     *
     * <p>逐块原样追加，不做任何延迟/攒批：后端 SSE 已用 thinking/content
     * 字段区分，这里按 kind 着色即可。同一类别内原样续写
     * （不人为换行 → 不会“一行一个 token”）；仅在类别切换时补一个换行。 */
    private final StringBuilder reasonBuf = new StringBuilder();
    /** 思维链字符区间 {start, end}（随 reasonBuf 裁剪同步平移）。 */
    private final java.util.ArrayList<int[]> reasonThinkSpans = new java.util.ArrayList<>();
    private String lastReasonKind = null;

    // ---- 2026-10-14 流畅度：流式渲染节流 ----------------------------------------
    // 一次推理 SSE 有几百个分块，旧写法每块都 setText(全文)+全文扫标记+动画滚动，
    // 复杂度 O(n^2)，MI 5X 这类旧机型主线程被拖死 -> 系统弹「应用无响应，是否等待」。
    // 现在：缓冲立即累积（不丢字），视图最多每 REASON_FRAME_MS 刷一次。
    private static final long REASON_FRAME_MS = 120L;
    private static final long LOG_FRAME_MS = 200L;
    /** 推理框高度上限 = 屏高 80%（懒算一次），超出后框内滚动。 */
    private static final float REASON_MAX_H_RATIO = 0.80f;
    /** 推理框缓冲上限：框本身最多 80% 屏高（约 60~80 行），留 9000 字足够往上回看；
     *  再大只是白白让每次 setText 重排更多文字（旧机型上就是掉帧主因之一）。 */
    private static final int REASON_MAX_CHARS = 9000;
    private boolean reasonPending = false;
    private Runnable reasonFrame = null;
    private boolean reasonDirty = false;
    private boolean logPending = false;
    private int reasonMaxH = 0;
    /** 本轮"还没渲染进视图"的起点，标记行着色只扫这段，不再全文 indexOf。 */
    private int reasonUnrenderedFrom = Integer.MAX_VALUE;
    private ScrollView reasoningScroll;      // 推理框自带滚动容器
    /** 当前存活的 Activity（debug 回放广播要用；release 不触发）。 */
    private static final java.util.concurrent.atomic.AtomicReference<MainActivity> LIVE
            = new java.util.concurrent.atomic.AtomicReference<>();
    /** 渲染次数计数（压测时看"节流有没有生效"）。 */
    private volatile int reasonRenderCount = 0;

    private static final int COL_THINK = 0xFF8A8F98;   // 思维链：灰
    private static final int COL_META = 0xFF6FB7D6;    // 标记行：淡蓝

    private void appendReasoning(final String text, final String kind) {
        if (text == null || text.isEmpty()) {
            return;
        }
        ui.post(new Runnable() {
            @Override
            public void run() {
                if (reasoningView == null) {
                    return;
                }
                final String k = (kind == null || kind.isEmpty()) ? "content" : kind;
                if (BuildConfig.DEBUG) {
                    // PC 侧可读：推理逐块(kind+text) 同步到 logcat
                    Log.i("HH_REASON", "[" + k + "] " + text.replace('\n', ' '));
                }
                // 类别切换才换行；同一类别内原样续写
                if (lastReasonKind != null && !lastReasonKind.equals(k)
                        && reasonBuf.length() > 0
                        && reasonBuf.charAt(reasonBuf.length() - 1) != '\n') {
                    reasonBuf.append('\n');
                }
                lastReasonKind = k;
                final int start = reasonBuf.length();
                reasonBuf.append(text);
                final int end = reasonBuf.length();
                if (start < reasonUnrenderedFrom) {
                    reasonUnrenderedFrom = start;
                }
                if ("think".equals(k)) {
                    reasonThinkSpans.add(new int[]{start, end});
                }
                // 上限裁剪：同步平移/丢弃思维链区间
                if (reasonBuf.length() > REASON_MAX_CHARS) {
                    final int drop = reasonBuf.length() - REASON_MAX_CHARS;
                    reasonBuf.delete(0, drop);
                    final java.util.Iterator<int[]> it = reasonThinkSpans.iterator();
                    while (it.hasNext()) {
                        final int[] r = it.next();
                        r[0] -= drop;
                        r[1] -= drop;
                        if (r[1] <= 0) {
                            it.remove();
                        } else if (r[0] < 0) {
                            r[0] = 0;
                        }
                    }
                }
                reasonDirty = true;
                scheduleReasonRender();
            }
        });
    }

    /** 节流：多个分块合并成一次视图刷新（约 8 次/秒封顶）。 */
    private void scheduleReasonRender() {
        if (reasonPending || reasoningView == null) {
            return;
        }
        reasonPending = true;
        reasonFrame = new Runnable() {
            @Override
            public void run() {
                reasonFrame = null;
                renderReasoning();
            }
        };
        ui.postDelayed(reasonFrame, REASON_FRAME_MS);
    }

    /** 任务收尾时立刻刷一次，别让最后一段文字晚 120ms 才出现。 */
    private void flushReasoning() {
        // 线程安全：任何线程可调。后台线程调时转投主线程 —— renderReasoning 里 setText
        // 必须在主线程，否则 CalledFromWrongThreadException（回放压测就是这么炸的）。
        if (Looper.myLooper() != Looper.getMainLooper()) {
            ui.post(new Runnable() {
                @Override
                public void run() {
                    flushReasoning();
                }
            });
            return;
        }
        if (reasonFrame != null) {
            ui.removeCallbacks(reasonFrame);
            reasonFrame = null;
        }
        reasonPending = false;
        renderReasoning();
    }

    /** 把缓冲渲染进视图：限高 + 滚到底。节流后每秒最多 ~8 次，旧机型不再被拖死。 */
    private void renderReasoning() {
        if (Looper.myLooper() != Looper.getMainLooper()) {
            flushReasoning();
            return;
        }
        reasonPending = false;
        if (!reasonDirty || reasoningView == null) {
            return;
        }
        reasonDirty = false;
        reasonRenderCount++;
        final int from = reasonUnrenderedFrom;
        reasonUnrenderedFrom = Integer.MAX_VALUE;
        final android.text.Spannable sp =
                android.text.SpannableStringBuilder.valueOf(reasonBuf);
        final int n = sp.length();
        for (int[] r : reasonThinkSpans) {
            final int s0 = Math.max(0, r[0]);
            final int e0 = Math.min(n, r[1]);
            if (s0 >= e0) {
                continue;
            }
            sp.setSpan(new android.text.style.ForegroundColorSpan(COL_THINK),
                    s0, e0, android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            sp.setSpan(new android.text.style.StyleSpan(android.graphics.Typeface.ITALIC),
                    s0, e0, android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        }
        // 只扫本轮新增区域（旧写法每次渲染全文 indexOf 两遍，是 O(n^2) 的另一半）
        applyMetaSpans(sp, "\u2014\u2014 \u6a21\u578b\u63a8\u7406\u5f00\u59cb \u2014\u2014", from);
        applyMetaSpans(sp, "\u2014\u2014 \u6a21\u578b\u63a8\u7406\u7ed3\u675f \u2014\u2014", from);
        reasoningView.setText(sp);
        capReasonHeight();
        scrollReasonToBottom();
    }

    /** 推理框限高：不超过屏高 80%（超出后框内滚动，不再把整页撑得看不见底部）。 */
    private void capReasonHeight() {
        if (reasoningScroll == null) {
            return;
        }
        if (reasonMaxH <= 0) {
            int ref = getResources().getDisplayMetrics().heightPixels;
            View panel = findViewById(R.id.panel_log);
            if (panel != null && panel.getHeight() > 200 && panel.getHeight() < ref) {
                ref = panel.getHeight();
            }
            reasonMaxH = (int) (ref * REASON_MAX_H_RATIO);
        }
        final View child = reasoningScroll.getChildAt(0);
        if (child == null) {
            return;
        }
        // 内容真实高度要等布局完成，故放在 post 里；只在高度真的变化时才改 LayoutParams
        reasoningScroll.post(new Runnable() {
            @Override
            public void run() {
                final ViewGroup.LayoutParams lp = reasoningScroll.getLayoutParams();
                final int want = child.getMeasuredHeight();
                final int target = (want > reasonMaxH) ? reasonMaxH
                        : ViewGroup.LayoutParams.WRAP_CONTENT;
                if (lp.height != target) {
                    lp.height = target;
                    reasoningScroll.setLayoutParams(lp);
                }
            }
        });
    }

    /** 同步滚到底部。用 scrollTo 而不是 smoothScrollTo：长文本下滚动动画本身就卡。 */
    private void scrollReasonToBottom() {
        if (reasoningScroll == null) {
            return;
        }
        reasoningScroll.post(new Runnable() {
            @Override
            public void run() {
                final View child = reasoningScroll.getChildAt(0);
                final int h = (child == null) ? 0 : child.getMeasuredHeight();
                reasoningScroll.scrollTo(0, h);   // ScrollView 自己会夹到最大可滚位置
            }
        });
    }

    /** 标记行（如“—— 模型推理开始 ——”）淡蓝加粗。 */
    private static void applyMetaSpans(android.text.Spannable sp, String tag, int from) {
        final String all = sp.toString();
        int idx = all.indexOf(tag, Math.max(0, from - tag.length()));
        while (idx >= 0) {
            final int e = idx + tag.length();
            sp.setSpan(new android.text.style.ForegroundColorSpan(COL_META),
                    idx, e, android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            sp.setSpan(new android.text.style.StyleSpan(android.graphics.Typeface.BOLD),
                    idx, e, android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            idx = all.indexOf(tag, e);
        }
    }

    private static ScrollView parentScroll(View v) {
        android.view.ViewParent p = v.getParent();
        while (p != null) {
            if (p instanceof ScrollView) {
                return (ScrollView) p;
            }
            p = p.getParent();
        }
        return null;
    }

    private void setResultText(final String text) {
        ui.post(new Runnable() {
            @Override
            public void run() {
                resultView.setText(text);
            }
        });
    }

    @Override
    protected void onDestroy() {
        LIVE.compareAndSet(this, null);
        super.onDestroy();
        if (debugSelfTestReceiver != null) {
            try {
                unregisterReceiver(debugSelfTestReceiver);
            } catch (Exception ignored) {
            }
            debugSelfTestReceiver = null;
        }
        if (debugLlmPingReceiver != null) {
            try {
                unregisterReceiver(debugLlmPingReceiver);
            } catch (Exception ignored) {
            }
            debugLlmPingReceiver = null;
        }
        io.shutdownNow();
    }
}
