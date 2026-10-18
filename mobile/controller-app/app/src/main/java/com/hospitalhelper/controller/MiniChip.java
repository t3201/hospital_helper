package com.hospitalhelper.controller;

import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Rect;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.provider.Settings;
import android.util.Log;
import android.view.Gravity;
import android.view.WindowManager;
import android.widget.TextView;

/**
 * 迷你浮窗（"小胶囊"）——随申办到前台、本 App 退到后台运行任务时，在屏幕角落挂一个
 * 小小的、半透明、<b>不可触摸（FLAG_NOT_TOUCHABLE）</b>的 TYPE_APPLICATION_OVERLAY，
 * 作为"我还在跑、没退出"的用户示意。
 *
 * <p>为什么用不可触摸浮窗而非可点小窗：不可触摸意味着它<b>永不拦截</b>随申办上的真实点击，
 * 从根本上避免干扰操作；"聪明"体现在点击落在胶囊区域时（由无障碍服务观测 TYPE_VIEW_CLICKED
 * 的节点 bounds）会自动翻到屏幕另一侧，躲开用户视线/操作区。
 *
 * <p>宿主：{@link SsAccessibilityService}（授权后常驻，具备 appop SYSTEM_ALERT_WINDOW，
 * addView 走与 {@link ForegroundKeeperService} 相同的已验证通道）。
 */
public final class MiniChip {

    /** 常驻引用（服务上下文创建，进程内单例；hide 时移除）。 */
    private static TextView chip;
    /** 胶囊背景(2026-05 圈5): 描边色 = 当前**验证状态**, 不是装饰色。 */
    private static GradientDrawable chipBg;

    /**
     * 浮窗状态(2026-05 圈5): 只由**已验证**的观测驱动。
     *   run    常规进度(蓝)  verify 校验中, 动作已发出但屏幕证据未到(琥珀)
     *   ok     观测层已核实到达(绿)  fail 校验失败(红)
     */
    public static final String S_RUN = "run";
    public static final String S_VERIFY = "verify";
    public static final String S_OK = "ok";
    public static final String S_FAIL = "fail";
    private static String state = S_RUN;
    private static WindowManager wm;
    private static WindowManager.LayoutParams lp;
    private static boolean shown = false;

    /**
     * 主线程 Handler（2026-10-14 修复）：PyBridge 的 onStep/onLog 回调发生在 Python 执行线程
     * （logcat: {@code Calling: pool-4-thread-1}），在那里直接 setText/addView/updateViewLayout
     * 会抛 CalledFromWrongThreadException —— 实测 MI 5X 失败 94 次、476cf991 失败 142 次，
     * 浮窗只剩初始那句「挂号助手 · 运行中…」，逐步进展全部被异常吞掉。
     * 因此本类<b>所有</b>视图操作都在主线程执行；公开方法可在任意线程调用。
     */
    private static final Handler MAIN = new Handler(Looper.getMainLooper());

    /** 排队待刷新的文本 + 已排队的刷新任务（合并连续步骤，避免每步都刷主线程）。 */
    private static String pendingText;
    private static String pendingState;
    private static Runnable pendingUpdate;

    /** 胶囊矩形（只在主线程刷新，其他线程只读，避免跨线程读 View）。 */
    private static final Rect cachedRect = new Rect();
    private static boolean rectValid = false;

    private static boolean onMain() {
        return Looper.myLooper() == Looper.getMainLooper();
    }

    /**
     * 胶囊当前贴哪一侧：true = 右上，false = 左上。
     *
     * <p>2026-10-16 真机复盘改设计：旧版在「右上 ↔ 右下」之间翻角。右上实测
     * frame=[683,158][1052,238]（正常），但翻到右下时按同样的 56dp 边距算，
     * frame 约 [683,2109][1052,2189] —— 落在屏幕底部操作区（随申办 L2 的底部
     * 按钮/号源区就在这一带），用户看到的就是"浮窗与屏幕对不上"。现在只在
     * **顶部左右**两侧翻，永不进入下半屏；顶部左右都不是随申办的关键点击区。
     */
    private static boolean rightSide = true;
    /** 浮窗落点（dp）：默认 56dp 让开状态栏；避让时临时降到 96dp 让开页面顶栏。 */
    private static final int DEFAULT_Y_DP = 56;
    private static final int DODGE_Y_DP = 96;
    private static final long RESTORE_COOLDOWN_MS = 2500L;
    private static int topYdp = DEFAULT_Y_DP;
    private static long lastDodgeAt = 0L;

    /**
     * 收尾自动收起的「代数」：showFinal 排定一次延时 hide，并记下当时的代数；
     * 期间任何新的 show()（步骤文案）都会让代数前进，那次 hide 就作废。
     */
    private static int gen = 0;
    // showFinal 记下的代数：gen 仍等于它 => 「收尾结论」正在展示，
    // onPause/onResume/开关探针都不该把它换成通用的「运行中…」文案。
    private static int finalGen = 0;

    /** 屏幕尺寸（像素），用于 dodge 判断与放置。 */
    private static int screenW = 0;
    private static int screenH = 0;

    private MiniChip() {
    }

    public static synchronized boolean isShown() {
        return shown;
    }

    /** 显示 / 更新胶囊文本（状态沿用上一次）。**可在任意线程调用**。 */
    public static synchronized void show(Context ctx, String text) {
        showState(ctx, text, null);
    }

    /**
     * 显示 / 更新胶囊文本 + **验证状态**(2026-05 圈5)。state=null 表示沿用当前状态。
     * 可在任意线程调用（内部 post 到主线程，并合并连续更新）。
     */
    public static synchronized void showState(Context ctx, String text, String st) {
        final Context c = (ctx == null) ? null : ctx.getApplicationContext();
        if (c == null) {
            return;
        }
        gen++;   // 又来新步骤：之前排定的「收尾自动收起」作废
        final String s2 = (st == null || st.isEmpty()) ? state : st;
        if (onMain()) {
            showOnMain(c, text, s2);
            return;
        }
        pendingText = text;
        pendingState = s2;
        if (pendingUpdate != null) {
            return;      // 已排队：文本已更新，等主线程一次性消费
        }
        pendingUpdate = new Runnable() {
            @Override
            public void run() {
                String t;
                String st2;
                synchronized (MiniChip.class) {
                    t = pendingText;
                    st2 = pendingState;
                    pendingUpdate = null;
                }
                showOnMain(c, t, st2);
            }
        };
        MAIN.post(pendingUpdate);
    }

    /**
     * 任务收尾：显示一条**结论**文案，{@code holdMs} 毫秒后自动收起。
     *
     * <p>之前任务一结束就 {@code hide()}，用户从随申办回到本 App 之前最后一眼看到的
     * 还是「运行中…」，浮窗等于没给任何结果信息（尤其失败时，连为什么失败都不知道）。
     * 现在改成：先「运行结束 · 正在回到挂号助手…」，解析出结果后换成「完成 · 推荐 N 个号源」
     * 或「未成功：<原因>」，停留若干秒再消失。可在任意线程调用。
     */
    public static synchronized void showFinal(Context ctx, String text, long holdMs) {
        show(ctx, text);
        final int myGen = ++gen;
        finalGen = myGen;
        final long ms = (holdMs <= 0) ? 9000L : holdMs;
        MAIN.postDelayed(new Runnable() {
            @Override
            public void run() {
                synchronized (MiniChip.class) {
                    if (gen != myGen) {
                        return;   // 期间又有新文案/新收尾，别把新的收掉
                    }
                }
                finalGen = 0;
                hide();
            }
        }, ms);
    }

    /** 真正的显示/更新，**只在主线程**调用。 */
    private static synchronized void showOnMain(Context c, String text, String st) {
        try {
            screenW = c.getResources().getDisplayMetrics().widthPixels;
            screenH = c.getResources().getDisplayMetrics().heightPixels;
            if (wm == null) {
                wm = (WindowManager) c.getSystemService(Context.WINDOW_SERVICE);
            }
            if (wm == null) {
                return;
            }
            if (chip == null) {
                chip = new TextView(c);
                chip.setTextSize(12f);
                chip.setTextColor(Color.WHITE);
                chip.setPadding(dp(c, 12), dp(c, 6), dp(c, 12), dp(c, 6));
                chipBg = new GradientDrawable();
                chipBg.setCornerRadius(dp(c, 20));
                chip.setBackground(chipBg);
                chip.setAlpha(0.92f);
                // 逐步骤文案会变长：允许换行且限定最大宽度 = 屏宽 - 左右边距，
                // 避免长 CJK 单行冲出屏幕（WRAP_CONTENT 无约束时不换行）。
                chip.setSingleLine(false);
                chip.setEllipsize(null);
                int maxW = (int) (c.getResources().getDisplayMetrics().widthPixels * 0.86f);
                chip.setMaxWidth(maxW);
            }
            state = (st == null || st.isEmpty()) ? state : st;
            if (chipBg != null) {
                // 描边/底色 = 验证状态: 用户扫一眼就知道这句是「已核实」还是「还在猜」
                chipBg.setColor(S_FAIL.equals(state) ? 0xCC3A1216 : 0xCC101828);
                chipBg.setStroke(dp(c, 1), strokeFor(state));
            }
            chip.setText(text == null || text.isEmpty() ? "挂号助手 · 运行中" : text);
            if (lp == null) {
                int type = (Build.VERSION.SDK_INT >= 26)
                        ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                        : WindowManager.LayoutParams.TYPE_SYSTEM_ALERT;
                lp = new WindowManager.LayoutParams(
                        WindowManager.LayoutParams.WRAP_CONTENT,
                        WindowManager.LayoutParams.WRAP_CONTENT,
                        type,
                        WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                                | WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                                | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN
                                | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                        android.graphics.PixelFormat.TRANSLUCENT);
                lp.gravity = Gravity.TOP | Gravity.END;
                place(lp);
            } else {
                place(lp);
            }
            if (!shown) {
                wm.addView(chip, lp);
                shown = true;
                refreshRect();
                Log.i("HHDIAG", "MiniChip shown: \"" + chip.getText() + "\" @"
                        + (rightSide ? "右上" : "左上") + " rect=" + snapshotRect());
            } else {
                wm.updateViewLayout(chip, lp);
                refreshRect();
                Log.i("HHDIAG", "MiniChip updated: \"" + chip.getText() + "\" @"
                        + (rightSide ? "右上" : "左上") + " rect=" + snapshotRect());
            }
            // addView/updateViewLayout 之后还有一次 layout pass，等它完成后再补测矩形
            chip.post(new Runnable() {
                @Override
                public void run() {
                    refreshRect();
                }
            });
        } catch (Throwable t) {
            Log.w("HHDIAG", "MiniChip.show fail: " + t.getClass().getSimpleName() + ": " + t.getMessage());
            if (Build.VERSION.SDK_INT >= 23 && !Settings.canDrawOverlays(c)) {
                Log.i("HHDIAG", "MiniChip: canDrawOverlays=false（仍按 appop 尝试过）");
            }
        }
    }

    /** 状态 -> 描边色(圈5): 蓝=常规 琥珀=校验中 绿=已验证 红=校验失败。 */
    private static int strokeFor(String st) {
        if (S_VERIFY.equals(st)) {
            return 0xFFFFB300;
        }
        if (S_FAIL.equals(st)) {
            return 0xFFE5484D;
        }
        if (S_OK.equals(st)) {
            return 0xFF35B37E;
        }
        return 0xFF2E6BFF;
    }

    /** 当前浮窗状态(可在任意线程读)。 */
    public static synchronized String state() {
        return state;
    }

    /** 只在主线程调用：把当前 View 位置缓存成矩形，供其他线程只读。 */
    private static void refreshRect() {
        if (!shown || chip == null || chip.getWidth() == 0) {
            rectValid = false;
            cachedRect.setEmpty();
            return;
        }
        int[] loc = new int[2];
        chip.getLocationOnScreen(loc);
        cachedRect.set(loc[0], loc[1], loc[0] + chip.getWidth(), loc[1] + chip.getHeight());
        rectValid = true;
    }

    private static String snapshotRect() {
        return rectValid ? cachedRect.toShortString() : "n/a";
    }

    /**
     * 「收尾结论」胶囊是否还在展示窗口内（自检/流水线结束后那 20 秒）。
     *
     * <p>回到前台时 onResume 会习惯性收窗，onPause 又可能按「还在跑任务」重新挂一个
     * 通用文案；不做这道闸门，用户最后看到的是「挂号助手 · 运行中…」而不是结果
     * （2026-10-14 实机日志：结论 19:36:21 显示，19:36:24 被换成「运行中…」）。
     */
    public static synchronized boolean finalPending() {
        return finalGen != 0 && finalGen == gen;
    }

    public static synchronized void hide() {
        if (onMain()) {
            hideOnMain();
            return;
        }
        MAIN.post(new Runnable() {
            @Override
            public void run() {
                hideOnMain();
            }
        });
    }

    /** 只在主线程调用。 */
    private static synchronized void hideOnMain() {
        if (shown && chip != null && wm != null) {
            try {
                wm.removeView(chip);
            } catch (Throwable ignored) {
            }
        }
        shown = false;
        rectValid = false;
        cachedRect.setEmpty();
        // 保留 chip/wm/lp 以便下次复用；仅标记隐藏
    }

    /** 翻角（躲开点击区）：右上 <-> 右下。可在任意线程调用。 */
    public static synchronized void dodge() {
        if (!shown || lp == null || chip == null || wm == null) {
            return;
        }
        rightSide = !rightSide;
        topYdp = DODGE_Y_DP;
        lastDodgeAt = SystemClock.elapsedRealtime();
        place(lp);
        Log.i("HHDIAG", "MiniChip dodge -> " + (rightSide ? "右上" : "左上")
                + " y=" + topYdp + "dp rect=" + snapshotRect());
        if (onMain()) {
            applyLayout();
            return;
        }
        MAIN.post(new Runnable() {
            @Override
            public void run() {
                applyLayout();
            }
        });
    }

    /** 只在主线程调用：把 lp 应用到已显示的胶囊。 */
    private static void applyLayout() {
        try {
            wm.updateViewLayout(chip, lp);
            refreshRect();
        } catch (Throwable ignored) {
        }
    }

    /**
     * 由无障碍服务观测到一次 TYPE_VIEW_CLICKED 落点（clickRect = 被点节点屏幕 bounds）。
     * 若该落点与胶囊矩形相交，则翻角躲开；否则原地不动。
     */
    public static synchronized void onPossibleClick(Rect clickRect) {
        if (!shown || clickRect == null || clickRect.isEmpty()) {
            return;
        }
        Rect r = currentRect();
        if (r == null) {
            return;
        }
        // 上下各留一点热区余量，避免"擦边"没躲干净
        Rect hot = new Rect(r.left - dp(chipCtx(), 8), r.top - dp(chipCtx(), 16),
                r.right + dp(chipCtx(), 8), r.bottom + dp(chipCtx(), 16));
        if (Rect.intersects(hot, clickRect)) {
            // 留证据：以后再报"浮窗与屏幕对不上"，能直接看出是**谁**的点击把它推走的
            Log.i("HHDIAG", "MiniChip 避让: click=" + clickRect.toShortString()
                    + " chip=" + r.toShortString() + " hot=" + hot.toShortString());
            dodge();
            return;
        }
        // 复位：避让只是临时的。冷却过后回到默认（右上 y=56dp），
        // 否则浮窗会一路"躲"到左上，正好压住随申办顶栏的「关闭」按钮
        // （2026-10-16 真机截图实测：chip rect=[28,158][432,238] 盖住关闭键）。
        if ((topYdp != DEFAULT_Y_DP || !rightSide)
                && SystemClock.elapsedRealtime() - lastDodgeAt > RESTORE_COOLDOWN_MS) {
            topYdp = DEFAULT_Y_DP;
            rightSide = true;
            place(lp);
            Log.i("HHDIAG", "MiniChip 复位 -> 右上 y=" + DEFAULT_Y_DP + "dp rect=" + snapshotRect());
            if (onMain()) {
                applyLayout();
                return;
            }
            MAIN.post(new Runnable() {
                @Override
                public void run() {
                    applyLayout();
                }
            });
        }
    }

    private static void place(WindowManager.LayoutParams p) {
        // 永远 TOP：y=56dp 让开状态栏；左右由 rightSide 决定（dodge 只换边、不换上下）。
        // 避让时 topYdp 临时降到 96dp：随申办 H5 顶栏（关闭/返回键）在 y≈120~240px，
        // 56dp(158px) 正好压在顶栏上 —— 浮窗"盖住屏幕上的按钮"也算对不上。
        p.gravity = Gravity.TOP | (rightSide ? Gravity.END : Gravity.START);
        p.y = dp(chipCtx(), topYdp);
        p.x = dp(chipCtx(), 10);
    }

    private static Context chipCtx() {
        return chip != null ? chip.getContext() : null;
    }

    private static int dp(Context c, int v) {
        if (c == null) {
            return v;
        }
        float d = c.getResources().getDisplayMetrics().density;
        return Math.round(v * d);
    }

    // ------------------------------------------------------------------ 兜底「回到 App」按钮
    /**
     * 回前台兜底按钮（可触摸）。
     *
     * <p>HyperOS/MIUI 会静默拦截后台的 moveToFront / REORDER_TO_FRONT（调用不抛错但无效）。
     * 此按钮<b>可点击</b>：点击 = 明确的用户手势，且我们持有 SYSTEM_ALERT_WINDOW，
     * 满足 Android 后台启动 Activity 的豁免条件，故点一下必达 MainActivity。
     * 显示在屏幕下方居中，醒目、不与随申办关键操作区（右上角/中部）冲突。
     */
    private static TextView tapBtn;
    private static WindowManager btnWm;
    private static WindowManager.LayoutParams btnLp;
    private static boolean btnShown = false;

    public static synchronized void showTapToReturn(Context ctx) {
        final Context c = (ctx == null) ? null : ctx.getApplicationContext();
        if (c == null) {
            return;
        }
        if (onMain()) {
            showTapToReturnOnMain(c);
            return;
        }
        MAIN.post(new Runnable() {
            @Override
            public void run() {
                showTapToReturnOnMain(c);
            }
        });
    }

    /** 只在主线程调用。 */
    private static synchronized void showTapToReturnOnMain(Context c) {
        try {
            if (btnWm == null) {
                btnWm = (WindowManager) c.getSystemService(Context.WINDOW_SERVICE);
            }
            if (btnWm == null) {
                return;
            }
            if (tapBtn == null) {
                tapBtn = new TextView(c);
                tapBtn.setText("\u2190 \u70b9\u6211\u56de\u5230\u3000\u6302\u53f7\u52a9\u624b");
                tapBtn.setTextSize(15f);
                tapBtn.setTextColor(Color.WHITE);
                tapBtn.setGravity(Gravity.CENTER);
                tapBtn.setPadding(dp(c, 20), dp(c, 12), dp(c, 20), dp(c, 12));
                GradientDrawable bg = new GradientDrawable();
                bg.setColor(0xF2FF9800);
                bg.setCornerRadius(dp(c, 26));
                tapBtn.setBackground(bg);
                tapBtn.setOnClickListener(new android.view.View.OnClickListener() {
                    @Override
                    public void onClick(android.view.View v) {
                        try {
                            Intent it = new Intent(c, MainActivity.class);
                            it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                            c.startActivity(it);
                        } catch (Throwable t) {
                            Log.w("HHDIAG", "tapToReturn startActivity: " + t.getMessage());
                        }
                        hideTapToReturn();
                    }
                });
            }
            if (!btnShown) {
                int type = (Build.VERSION.SDK_INT >= 26)
                        ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                        : WindowManager.LayoutParams.TYPE_SYSTEM_ALERT;
                btnLp = new WindowManager.LayoutParams(
                        WindowManager.LayoutParams.WRAP_CONTENT,
                        WindowManager.LayoutParams.WRAP_CONTENT,
                        type,
                        WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                                | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN
                                | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                        android.graphics.PixelFormat.TRANSLUCENT);
                btnLp.gravity = Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL;
                btnLp.y = dp(c, 130);
                btnWm.addView(tapBtn, btnLp);
                btnShown = true;
                Log.i("HHDIAG", "TapToReturn button shown");
            }
        } catch (Throwable t) {
            Log.w("HHDIAG", "showTapToReturn fail: " + t.getClass().getSimpleName() + ": " + t.getMessage());
        }
    }

    public static synchronized void hideTapToReturn() {
        if (onMain()) {
            hideTapToReturnOnMain();
            return;
        }
        MAIN.post(new Runnable() {
            @Override
            public void run() {
                hideTapToReturnOnMain();
            }
        });
    }

    /** 只在主线程调用。 */
    private static synchronized void hideTapToReturnOnMain() {
        if (btnShown && tapBtn != null && btnWm != null) {
            try {
                btnWm.removeView(tapBtn);
            } catch (Throwable ignored) {
            }
        }
        btnShown = false;
        Log.i("HHDIAG", "TapToReturn button hidden");
    }

    /**
     * 跳转系统「显示在其他应用上层」授权页（Android 6+）。用户授权后 addView 才不被拒。
     * HyperOS/MIUI 亦可能把该开关放在「权限管理-悬浮窗」里，此 Intent 由 ROM 决定落地页。
     */
    public static void requestOverlayPermission(Context ctx) {
        try {
            if (Build.VERSION.SDK_INT < 23) {
                return;
            }
            Intent it = new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                    Uri.parse("package:" + ctx.getPackageName()));
            it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            ctx.startActivity(it);
        } catch (Throwable t) {
            Log.w("HHDIAG", "requestOverlayPermission fail: " + t.getMessage());
        }
    }

    /** 胶囊在屏幕上的当前矩形（读主线程缓存，可在任意线程调用）。返回 null 表示未显示/未测量。 */
    public static synchronized Rect currentRect() {
        if (!shown || !rectValid || cachedRect.isEmpty()) {
            return null;
        }
        return new Rect(cachedRect);
    }
}
