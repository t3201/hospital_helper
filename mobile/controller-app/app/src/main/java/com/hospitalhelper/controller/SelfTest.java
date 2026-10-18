package com.hospitalhelper.controller;

import android.content.Context;
import android.util.Log;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;

/**
 * SelfTest —— App 端「自检」入口（P4 准备活）。
 *
 * <p>目的：让<b>端上</b>直接跑一次号源抓取，不必连 PC 跑垫片；验证的是
 * Chaquopy 进程内真实调用路径（{@code python/selftest.py} ->
 * {@code fetch_slots.run_conditions} -> {@code run_condition_mode}），
 * 绕开完整 9 段 LLM 流水线。
 *
 * <p>两种触发（同一套 {@link #run(Context, String)}）：
 * <ul>
 *   <li>MainActivity「自检」按钮 —— 不传 manifest，用内置固定条件；</li>
 *   <li>debug 专用 {@code BroadcastReceiver}（仅 {@code BuildConfig.DEBUG} 才注册/导出）
 *       —— 从 {@code adb shell am broadcast ... --es manifest <json>} 带一个
 *       {@code conditions:[...]} 的 JSON manifest 覆盖默认条件，便于自动化。</li>
 * </ul>
 *
 * <p>结果落盘到 App 私有目录：raw -> {@code files/out/slots/out/raw/}、
 * 记录 -> {@code files/out/slots/out/condition_runs/}、摘要 -> {@code files/out/selftest_last.json}；
 * 摘要逐行写 logcat（固定 tag {@link #TAG}，便于 {@code adb logcat -s HH_SELFTEST}）。
 *
 * <p><b>安全（硬性）：</b>只走到号源/六字段页，绝不进入任何下单路径。
 * 触发器只把 manifest 交给 {@code selftest.py}，后者只调只读抓取的
 * {@code run_conditions}；本类自身不点击任何 UI、不碰 booking/payment。
 */
public final class SelfTest {

    /** logcat 固定 tag。 */
    public static final String TAG = "HH_SELFTEST";
    /** SharedPreferences：记住按钮上次用的 manifest（让广播可复用）。 */
    private static final String PREF = "hh_selftest";
    private static final String KEY_LAST_MANIFEST = "last_manifest";

    /**
     * 内置固定条件（按钮触发时用它）。与 PC 垫片 probe_pos_ctrl 同一条：
     * SH-084 第十人民医院 / 眼科 / 眼科门诊 / 专家门诊。只到号源页，绝不预约。
     */
    public static final String DEFAULT_MANIFEST =
            "{\"source\":\"selftest_builtin\",\"conditions\":"
            + "[{\"hospital\":\"SH-084\",\"tab\":\"眼科\",\"dept\":\"眼科门诊\","
            + "\"slot_type\":\"expert\"}]}";

    private static volatile ExecutorService io;
    private static volatile boolean running = false;

    private SelfTest() {
    }

    /** 单线程串行化（避免按钮连点 / 广播与按钮并发互踩）。 */
    private static synchronized ExecutorService io() {
        if (io == null) {
            io = Executors.newSingleThreadExecutor(new ThreadFactory() {
                @Override
                public Thread newThread(Runnable r) {
                    Thread t = new Thread(r, "HH-selftest");
                    t.setDaemon(true);
                    return t;
                }
            });
        }
        return io;
    }

    /** 上一次自检是否仍在运行（按钮置灰 / 防抖）。 */
    public static boolean isRunning() {
        return running;
    }

    /**
     * 自检结束回调（无论成败）。自检会把随申办拉到前台读屏，结束后 MainActivity
     * 靠这个回调把自己带回前台（对齐 run/pipeline 的 bringSelfToFront）。
     */
    public interface OnDone {
        void onSelfTestDone(boolean ok, String oneLine);
    }
    private static volatile OnDone sOnDone;
    private static volatile Context sApp;
    public static void setOnDoneListener(OnDone l) {
        sOnDone = l;
    }

    /** 安全调用结束回调（UI 线程交给注册方自己 post）。 */
    private static void notifyDone(boolean ok, String oneLine) {
        OnDone l = sOnDone;
        if (l == null) {
            // Activity 不在：结论还是要落到浮窗上（showFinal 会自动收起），
            // 否则浮窗会一直停在自检中途的旧文案，与屏幕实际内容不一致。
            Context a = sApp;
            if (a != null) {
                try {
                    MiniChip.showFinal(a, (ok ? "自检完成 · " : "自检未通过 · ")
                            + oneLine, 20000L);
                } catch (Throwable ignored) {
                }
            }
            Log.i(TAG, "[done] 无 Activity 监听，结论只落浮窗/logcat: " + oneLine);
            return;
        }
        try {
            l.onSelfTestDone(ok, oneLine);
        } catch (Throwable ignored) {
        }
    }

    /**
     * 触发一次自检。manifestJson 为 null 时用 {@link #DEFAULT_MANIFEST}。
     * 立即返回（异步执行），结果走 logcat({@link #TAG}) + files/out/selftest_last.json。
     */
    public static void run(final Context ctx, String manifestJson) {
        final Context app = ctx.getApplicationContext();
        sApp = app;
        final String manifest = (manifestJson == null || manifestJson.trim().isEmpty())
                ? DEFAULT_MANIFEST : manifestJson;
        if (running) {
            Log.i(TAG, "[run] 已有一次自检在跑，忽略本次触发");
            return;
        }
        running = true;
        Log.i(TAG, "[run] 自检开始（绕开 9 段 LLM 流水线；只读抓取，绝不进下单路径）");
        io().execute(new Runnable() {
            @Override
            public void run() {
                long t0 = System.currentTimeMillis();
                String payload;
                try {
                    // 只读数据资产解压（幂等），确保 manifest 可读。
                    PyBridge.init(app);
                    PyBridge.extractAssetsIfNeeded();
                    // 读屏桥门禁（含冷启 onServiceConnected 时序兜底）。
                    boolean ready = ServiceGate.ensure(app);
                    logLine("[gate] 读屏桥就绪 = " + ready);
                    // 防后台冻结，保持进程前台优先级。
                    ForegroundKeeperService.start(app);
                    try {
                        Python py = Python.getInstance();
                        PyObject mod = py.getModule("selftest");
                        PyObject out = mod.callAttr("run", manifest);
                        payload = out.toJava(String.class);
                    } finally {
                        ForegroundKeeperService.stop(app);
                    }
                } catch (Throwable t) {
                    payload = "{\"ok\":false,\"error\":\"SelfTest 异常: "
                            + t.getClass().getSimpleName() + ": " + String.valueOf(t.getMessage()) + "\"}";
                    Log.w(TAG, "[run] 自检异常 " + t, t);
                }
                writeLastJson(app, payload);
                logSummary(payload);
                long dt = System.currentTimeMillis() - t0;
                Log.i(TAG, "[run] 自检结束 elapsed=" + dt + "ms");
                running = false;
                // 通知宿主：自检把随申办拉到前台了，请把自己带回前台
                boolean ok = false;
                String oneLine = "";
                try {
                    JSONObject o = new JSONObject(payload);
                    ok = o.optBoolean("ok", false);
                    oneLine = "ok=" + ok + " rc=" + o.optInt("rc", -1)
                            + " units=" + o.optInt("units", -1)
                            + " planned=" + o.optInt("groups_planned", -1)
                            + " six=" + o.optInt("six_records_total", -1)
                            + " " + o.optLong("elapsed_ms", -1) + "ms"
                            + " err=" + o.optString("error", "");
                } catch (Exception e) {
                    ok = false;
                    oneLine = "payload 非 JSON";
                }
                notifyDone(ok, oneLine);
            }
        });
    }

    /** 把某次 manifest 记到 SharedPreferences，供按钮 / 广播复用（调试便利）。 */
    public static void rememberManifest(Context ctx, String manifestJson) {
        try {
            ctx.getApplicationContext().getSharedPreferences(PREF, Context.MODE_PRIVATE)
                    .edit().putString(KEY_LAST_MANIFEST,
                            (manifestJson == null ? "" : manifestJson)).apply();
        } catch (Exception ignored) {
        }
    }

    public static String lastRemembered(Context ctx) {
        try {
            return ctx.getApplicationContext().getSharedPreferences(PREF, Context.MODE_PRIVATE)
                    .getString(KEY_LAST_MANIFEST, "");
        } catch (Exception e) {
            return "";
        }
    }

    private static void logLine(String line) {
        Log.i(TAG, line);
        try {
            // 若 UI 在，同步到 App 日志区（PyBridge.emitLog -> 监听器 onLog -> ui.post）。
            PyBridge.emitLog("[HH_SELFTEST] " + line);
        } catch (Throwable ignored) {
        }
    }

    /** 摘要逐行打 logcat（固定 tag），同时回落到 PyBridge 日志缓冲（若 UI 在）。 */
    private static void logSummary(String payload) {
        JSONObject o;
        try {
            o = new JSONObject(payload);
        } catch (Exception e) {
            Log.w(TAG, "[summary] 摘要非 JSON: " + truncate(payload, 300));
            return;
        }
        String one = String.format(Locale.US,
                "ok=%s rc=%s units=%s planned=%s resume_skip=%s six=%s elapsed_ms=%s stop=%s err=%s",
                o.optBoolean("ok", false),
                o.optInt("rc", -1),
                o.optInt("units", -1),
                o.optInt("groups_planned", -1),
                o.optInt("resume_done_pre", -1),
                o.optInt("six_records_total", -1),
                o.optLong("elapsed_ms", -1),
                o.optString("stop_reason", "-"),
                o.optString("error", ""));
        logLine("[summary] " + one);
        // 空转哨兵：units=0 表示本次一个单元都没真跑（被 condition_runs 的 done 标记跳过 /
        // 或导航前置就退出）。不喊出来就会被当成「自检通过」，用户看到的就是「点了跟没点一样」。
        if (o.optInt("units", -1) == 0) {
            logLine("[summary] !! 空转警告：units=0（planned=" + o.optInt("groups_planned", -1)
                    + " resume_skip=" + o.optInt("resume_done_pre", -1)
                    + "），本次没有真正抓取任何单元");
        }
        logLine("[summary] raw_dir=" + o.optString("raw_dir", "-"));
        logLine("[summary] raw_files=" + o.opt("raw_files") + "  (files/out/slots/out/raw/)");
        logLine("[summary] cond_dir=" + o.optString("cond_dir", "-"));
        if (o.has("new_raw_files") && o.optJSONArray("new_raw_files") != null) {
            JSONArray arr = o.optJSONArray("new_raw_files");
            for (int i = 0; i < arr.length(); i++) {
                logLine("[summary]   new_raw[" + i + "] = " + arr.optString(i, ""));
            }
        }
    }

    private static void writeLastJson(Context ctx, String payload) {
        try {
            File dir = new File(ctx.getApplicationContext().getFilesDir(), "out");
            if (!dir.exists() && !dir.mkdirs()) {
                //noinspection ResultOfMethodCallIgnored
                dir.mkdirs();
            }
            File f = new File(dir, "selftest_last.json");
            FileOutputStream os = new FileOutputStream(f);
            try {
                os.write(payload.getBytes("UTF-8"));
            } finally {
                os.close();
            }
            logLine("[run] 摘要已落盘 " + f.getAbsolutePath());
        } catch (Exception e) {
            Log.w(TAG, "[run] 写 selftest_last.json 失败: " + e);
        }
    }

    private static String truncate(String s, int n) {
        if (s == null) {
            return "";
        }
        s = s.replace('\n', ' ').replace('\r', ' ');
        return s.length() <= n ? s : s.substring(0, n) + "…";
    }
}
