package com.hospitalhelper.controller;

import android.content.ComponentName;
import android.content.ContentResolver;
import android.content.Context;
import android.os.SystemClock;
import android.provider.Settings;
import android.view.accessibility.AccessibilityManager;

import java.util.ArrayList;
import java.util.List;

/**
 * 无障碍服务「就绪」门控（修冷启「未授权」误报）。
 *
 * <p>冷启 / force-stop 之后，系统往往已经在 {@code enabled_accessibility_services}
 * 里登记了 {@link SsAccessibilityService}，但 {@code onServiceConnected()} 尚未回调，
 * 静态 {@code instance} 仍为 null，{@link SsAccessibilityService#isReady()} 返回 false。
 * 若旧版 {@code ensureService()} 在这一步直接判死，就会出现「未授权」假象。
 *
 * <p>本类提供三段式判定（与 {@link SsAccessibilityService} / {@link PyBridge} 命名同包、语义一致）：
 * <ol>
 *   <li><b>轮询</b>：每 {@value #POLL_INTERVAL_MS}ms 查一次 {@code isReady()}，默认上限
 *       {@value #DEFAULT_TIMEOUT_MS}ms，覆盖「已登记但 onServiceConnected 尚未回调」的时序窗。</li>
 *   <li><b>系统角度判定</b>：读 {@code Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES}
 *       + {@link AccessibilityManager#getEnabledAccessibilityServiceList}，确认组件是否真被系统启用。</li>
 *   <li><b>主动重绑定</b>：组件已登记但仍未绑定，或尚未登记时，尽力写回 secure 设置
 *       （追加组件 + 抖一下 {@code accessibility_enabled} 主开关）触发一次重新绑定，再轮询。</li>
 * </ol>
 * 全部失败才返回 false，调用方弹提示并打开系统无障碍设置，交由用户手动开启。
 *
 * <p>注意：{@link #ensure(Context)} 内含 {@code Thread.sleep}，<b>必须在非 UI 线程调用</b>
 * （MainActivity 已放入 io 单线程池）。
 */
public final class ServiceGate {

    /** 轮询间隔。 */
    private static final long POLL_INTERVAL_MS = 300;
    /** 默认总超时。 */
    private static final long DEFAULT_TIMEOUT_MS = 10_000;

    private static final String PKG = "com.hospitalhelper.controller";
    private static final String CLS = "com.hospitalhelper.controller.SsAccessibilityService";
    /** secure 设置里的「包/.短类」形态。 */
    private static final String FLAT = PKG + "/.SsAccessibilityService";

    private ServiceGate() {
    }

    public static ComponentName component() {
        return new ComponentName(PKG, CLS);
    }

    /**
     * 组件是否已被系统启用（读 secure 列表）。
     * <p>用 {@code enabled_accessibility_services} 权威判定。
     */
    public static boolean isEnabledInSystem(Context ctx) {
        try {
            ContentResolver cr = ctx.getContentResolver();
            String cur = Settings.Secure.getString(cr, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
            if (cur != null && cur.contains(PKG + "/")) {
                return true;
            }
        } catch (Exception ignored) {
        }
        // 兜底：AccessibilityManager 侧（某些 ROM 下 secure 读不出）
        try {
            AccessibilityManager am =
                    (AccessibilityManager) ctx.getSystemService(Context.ACCESSIBILITY_SERVICE);
            if (am != null && am.isEnabled()) {
                return true;
            }
        } catch (Exception ignored) {
        }
        return false;
    }

    /**
     * 尽力把组件写进 secure 启用列表并打开主开关。
     *
     * <p>需要 {@code WRITE_SECURE_SETTINGS}；无权限（普通安装 App）时安静返回 false，
     * 交由调用方打开系统设置让用户手动开启。
     *
     * @return true 表示已成功写入（或原本就在列表中）
     */
    public static boolean bestEffortEnable(Context ctx) {
        try {
            ContentResolver cr = ctx.getContentResolver();
            String cur = Settings.Secure.getString(cr, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
            List<String> parts = new ArrayList<>();
            boolean had = false;
            if (cur != null) {
                for (String p : cur.split(":")) {
                    if (p == null || p.isEmpty()) {
                        continue;
                    }
                    if (p.contains(PKG + "/")) {
                        had = true;
                    }
                    parts.add(p);
                }
            }
            if (!had) {
                parts.add(FLAT);
            }
            StringBuilder sb = new StringBuilder();
            for (String p : parts) {
                if (sb.length() > 0) {
                    sb.append(':');
                }
                sb.append(p);
            }
            String newVal = sb.toString();
            Settings.Secure.putString(cr, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, newVal);
            Settings.Secure.putInt(cr, Settings.Secure.ACCESSIBILITY_ENABLED, 1);
            // putString 无权限时往往不抛异常而是静默失败，读回校验是否真正写入。
            String after = Settings.Secure.getString(cr, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
            return after != null && after.contains(PKG + "/");
        } catch (Exception e) {
            return false;
        }
    }

    /**
     * 主动触发一次「重新绑定」。
     *
     * <p>注意：在 Xiaomi HyperOS 上，写 {@code accessibility_enabled=0} 会清空
     * {@code enabled_accessibility_services}，反而把服务彻底踢掉。因此这里<b>只</b>做
     * 「确保组件在列表 + 开关为 1」，不做 1→0→1 抖动。冷启时序由
     * {@link #ensure(Context, long)} 的轮询兜底（onServiceConnected 会异步回调）。
     */
    private static void rebindToggle(Context ctx) {
        bestEffortEnable(ctx);
    }

    /** 默认 10s 超时。 */
    public static boolean ensure(Context ctx) {
        return ensure(ctx, DEFAULT_TIMEOUT_MS);
    }

    /**
     * 就绪门控主入口：轮询 + 系统判定 + 主动重绑定。
     *
     * @param timeoutMs 总超时（毫秒）
     * @return {@link SsAccessibilityService#isReady()} 是否最终为 true
     */
    public static boolean ensure(Context ctx, long timeoutMs) {
        if (SsAccessibilityService.isReady()) {
            return true;
        }

        long deadline = SystemClock.uptimeMillis() + Math.max(1000, timeoutMs);
        boolean triedEnable = false;
        boolean triedRebind = false;
        boolean cannotSelfHeal = false;

        while (true) {
            if (SsAccessibilityService.isReady()) {
                return true;
            }

            if (!isEnabledInSystem(ctx)) {
                if (!triedEnable) {
                    triedEnable = true;
                    boolean ok = bestEffortEnable(ctx);
                    // 写入成功（或原本就在列表）：给系统一点时间重新 bind 再轮询。
                    // 写入失败（多半是没有 WRITE_SECURE_SETTINGS）：无法自愈，快速返回 false，
                    // 避免在 UI 线程上空转 10s（ANR 风险）；由调用方打开系统设置让用户手动开启。
                    if (!ok || !isEnabledInSystem(ctx)) {
                        cannotSelfHeal = true;
                    }
                    sleep(POLL_INTERVAL_MS);
                    if (SsAccessibilityService.isReady()) {
                        return true;
                    }
                    if (cannotSelfHeal) {
                        break;
                    }
                }
            } else {
                // 已登记但仍未绑定 -> 主动重绑定一次，给系统重新 bind 的机会
                if (!triedRebind) {
                    triedRebind = true;
                    rebindToggle(ctx);
                    sleep(POLL_INTERVAL_MS);
                    if (SsAccessibilityService.isReady()) {
                        return true;
                    }
                }
            }

            long left = deadline - SystemClock.uptimeMillis();
            if (left <= 0) {
                break;
            }
            sleep(Math.min(POLL_INTERVAL_MS, left));
        }
        return SsAccessibilityService.isReady();
    }

    private static void sleep(long ms) {
        if (ms <= 0) {
            return;
        }
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
