package com.hospitalhelper.controller;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.util.Log;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;

/**
 * LlmPingReceiver —— debug 专用「服务端连通性自检」（仅 {@code BuildConfig.DEBUG} 才注册）。
 *
 * <p>为什么要它：换到归集服务端（{@code https://hh.ei.ac.cn:51120}）以后，「大模型这一腿」
 * 走的是端上 {@code app/llm_client.py} -> {@code POST /v1/infer}。但「自检」按钮刻意绕开
 * 9 段 LLM 流水线（只读抓号源），所以号源跑通并不代表 LLM 通。这个通道<b>不碰随申办 UI、
 * 不做任何自动化、不下单</b>，只发一次推理请求，把端上真实调用的 base、耗时、返回科室
 * 打到 logcat，便于 {@code adb} 一键回归。
 *
 * <pre>
 *   adb -s &lt;serial&gt; shell am broadcast \
 *       -a com.hospitalhelper.controller.LLM_PING \
 *       -n com.hospitalhelper.controller/.LlmPingReceiver
 *
 *   # 换症状 / 换服务端地址（可选）：
 *   adb -s &lt;serial&gt; shell am broadcast \
 *       -a com.hospitalhelper.controller.LLM_PING \
 *       -n com.hospitalhelper.controller/.LlmPingReceiver \
 *       --es text "胸闷气短两周" --es base https://hh.ei.ac.cn:51120
 * </pre>
 *
 * <p>结果看 {@code adb logcat -s HH_LLM}：{@code [ping] llm_base(解析)=…}、
 * {@code [ping] OK department=…}、{@code [ping] FAIL <异常>}。
 *
 * <p>release 构建里这个接收器<b>根本不注册</b>，{@code am broadcast} 打不到目标。
 */
public final class LlmPingReceiver extends BroadcastReceiver {

    public static final String ACTION = "com.hospitalhelper.controller.LLM_PING";
    public static final String TAG = "HH_LLM";
    public static final String EXTRA_TEXT = "text";
    public static final String EXTRA_BASE = "base";

    /**
     * 在飞闸门。manifest 里也声明了本接收器（为了让 {@code am broadcast -n} 能精确投递），
     * 而 MainActivity 又动态注册了一次 —— 不带 {@code -n} 的广播会两边各命中一次，
     * 一次广播就会打两次推理。这里只允许同时在飞一次。
     */
    private static final java.util.concurrent.atomic.AtomicBoolean IN_FLIGHT =
            new java.util.concurrent.atomic.AtomicBoolean(false);

    @Override
    public void onReceive(Context context, Intent intent) {
        final Context app = (context == null) ? null : context.getApplicationContext();
        if (app == null || !BuildConfig.DEBUG) {
            Log.w(TAG, "[ping] 非 DEBUG 构建或无 context，忽略");
            return;
        }
        if (intent == null || !ACTION.equals(intent.getAction())) {
            return;
        }
        final String text = extra(intent, EXTRA_TEXT, "咳嗽三天，夜间加重，无发热");
        final String base = extra(intent, EXTRA_BASE, "");
        if (!IN_FLIGHT.compareAndSet(false, true)) {
            Log.i(TAG, "[ping] 已有一次 ping 在飞，忽略本次触发");
            return;
        }
        Log.i(TAG, "[ping] 收到 LLM_PING（只发一次推理请求；不碰随申办 UI、不下单）");
        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    ping(app, text, base);
                } finally {
                    IN_FLIGHT.set(false);
                }
            }
        }, "HH-llm-ping").start();
    }

    private static String extra(Intent it, String key, String def) {
        String v = it.hasExtra(key) ? it.getStringExtra(key) : null;
        return (v == null || v.trim().isEmpty()) ? def : v.trim();
    }

    /** 端上真路径：app/llm_client.chat_json -> 服务端 /v1/infer（服务端注入上游凭据）。 */
    private static void ping(Context app, String text, String base) {
        long t0 = System.currentTimeMillis();
        try {
            PyBridge.init(app);
            PyBridge.extractAssetsIfNeeded();
            Python py = Python.getInstance();
            PyObject mod = py.getModule("app.llm_client");
            Log.i(TAG, "[ping] llm_base(解析) = " + mod.callAttr("resolve_base_url"));
            String sys = "你是挂号助手的分诊器。只输出一个 JSON 对象，形如 {\"department\":\"科室名\"}，"
                    + "不要解释、不要代码块。";
            String user = "症状：" + text + "\n给出最合适的挂号科室。";
            PyObject r;
            if (base.isEmpty()) {
                r = mod.callAttr("chat_json", sys, user);
            } else {
                r = mod.callAttr("chat_json", sys, user, "default", base);
            }
            // chat_json 返回 (obj, meta)；Chaquopy 的 PyObject 用 asList() 取元组元素。
            java.util.List<PyObject> tup = r.asList();
            PyObject obj = tup.get(0);
            PyObject meta = tup.get(1);
            Log.i(TAG, "[ping] OK result=" + obj);
            Log.i(TAG, "[ping] meta=" + meta);
        } catch (Throwable t) {
            Log.w(TAG, "[ping] FAIL " + t.getClass().getName() + ": " + t.getMessage());
        }
        Log.i(TAG, "[ping] elapsed=" + (System.currentTimeMillis() - t0) + "ms");
    }
}
