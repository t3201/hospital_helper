package com.hospitalhelper.controller;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

/**
 * ReasonReplayReceiver —— 只在 debug 包里存在的「推理流回放」开关（2026-10-14）。
 *
 * <p>用途：实时日志页的模型推理框是卡顿重灾区（旧机型 MI 5X 曾因每个 SSE 分块
 * 全量 setText 直接把主线程拖到弹「应用无响应」）。但自检(SelfTestReceiver)走的是
 * 纯号源抓取、不调 LLM，没法复现那段渲染。这里用假分块按真实 SSE 的节奏灌进
 * {@code appendReasoning}，把渲染路径单独压测出来，改没改好一跑就知道。
 *
 * <p>只读 UI，不碰随申办、不碰预约。release 包不注册（见 AndroidManifest 注释 + 下面的 BuildConfig 判断）。
 *
 * <p>用法：
 * <pre>
 *   adb shell am broadcast -a com.hospitalhelper.controller.REASON_REPLAY \
 *        -n com.hospitalhelper.controller/.ReasonReplayReceiver \
 *        --ei chunks 1500 --ei delay_ms 6
 * </pre>
 * 观察：{@code adb logcat -s HH_REPLAY}、{@code adb shell dumpsys gfxinfo <pkg> framestats}，
 * 以及「有没有弹应用无响应」。
 */
public class ReasonReplayReceiver extends BroadcastReceiver {

    public static final String ACTION = "com.hospitalhelper.controller.REASON_REPLAY";
    public static final String EXTRA_CHUNKS = "chunks";
    public static final String EXTRA_DELAY_MS = "delay_ms";
    private static final String TAG = "HH_REPLAY";

    /** 假推理文本：中英混排 + 换行，尽量贴近真实 thinking 流的形状。 */
    private static final String[] FRAGMENTS = {
            "用户主诉视物模糊，", "需要先排除眼底问题；", "按症状→科室映射，",
            "眼科门诊是首选；", "再看专家号源分布，", "徐建江/于靖 号源偏紧，",
            "冯乐 副主任医师 有余量，", "综合考虑距离与费用，", "推荐顺序：于靖 > 冯乐 > 刘芳。",
            "The ranking weighs distance, ", "slot availability and title level; ",
            "slot_type=expert means only consultant slots count.\n",
    };

    @Override
    public void onReceive(Context context, Intent intent) {
        if (!BuildConfig.DEBUG) {
            return;   // release 包不暴露任何调试入口
        }
        final int chunks = Math.max(1, intent.getIntExtra(EXTRA_CHUNKS, 1500));
        final long delay = Math.max(0, intent.getIntExtra(EXTRA_DELAY_MS, 6));
        final MainActivity act = MainActivity.live();
        if (act == null) {
            Log.w(TAG, "[replay] Activity 不在（先 am start MainActivity 再广播）");
            return;
        }
        Log.i(TAG, "[replay] 开始：chunks=" + chunks + " delay_ms=" + delay
                + "（切到实时日志面板，看推理框是否跟手滚动）");
        act.replayReasoning(chunks, delay, new MainActivity.ReplayDone() {
            @Override
            public void run(long elapsedMs, int rendered) {
                Log.i(TAG, "[replay] 结束：elapsed=" + elapsedMs + "ms chunks=" + chunks
                        + " renders=" + rendered + "（renders << chunks 即节流生效）");
            }
        });
    }
}
