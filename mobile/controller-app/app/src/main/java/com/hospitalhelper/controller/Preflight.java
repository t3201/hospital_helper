package com.hospitalhelper.controller;

import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Build;
import android.provider.Settings;

import java.util.ArrayList;
import java.util.List;

/**
 * 「前置检查」模型：把"能不能正常读屏 / 操作 / 用随申办"拆成若干可判定项，
 * 供表单页顶部的检查框渲染问题清单 + 一键跳转修复。
 *
 * <p>只读判定，绝不点任何业务按钮（红线）。登录项是「深检」，需 Java 侧异步调
 * {@code app.preflight.check_login_json}（复用已实测的 Nav.ensure_444），本类里
 * 只先置为 {@link State#UNKNOWN}，由 {@link MainActivity} 深检后回填。
 */
public final class Preflight {

    public enum Kind {
        A11Y,          // 无障碍：读别的 App 屏幕 + 执行点击/滑动/输入
        INSTALL,       // 随申办是否安装
        LOGIN,         // 随申办登录态（深检）
        OVERLAY,       // 悬浮窗权限（胶囊提醒，可选）
        LOCATION,      // 定位权限（自动定位，可选）
        BACK_LAUNCH    // 后台弹出界面（任务跑完自动回到本 App，HyperOS 默认拒绝）
    }

    public enum State { OK, WARN, BAD, UNKNOWN }

    public static final class Item {
        public final Kind kind;
        public final String label;
        public final boolean required;   // true = 不满足则禁用表单
        public State state;
        public String detail;

        Item(Kind kind, String label, boolean required, State state, String detail) {
            this.kind = kind;
            this.label = label;
            this.required = required;
            this.state = state;
            this.detail = detail;
        }
    }

    private Preflight() {
    }

    /** 便捷项构造。 */
    private static Item it(Kind k, String label, boolean req, State s, String d) {
        return new Item(k, label, req, s, d);
    }

    /**
     * 廉价同步扫描（主线程/工作线程皆可，均为本地 API，无网络无导航）。
     * 登录项先返回 UNKNOWN，交给深检。
     */
    public static List<Item> scan(Context ctx) {
        List<Item> out = new ArrayList<>();

        // 1) 无障碍：读屏 + 操作（必需）
        if (SsAccessibilityService.isReady()) {
            out.add(it(Kind.A11Y, "读屏与操作权限", true, State.OK,
                    "已连接（可读其他 App 屏幕并执行点击/滑动/输入）"));
        } else if (ServiceGate.isEnabledInSystem(ctx)) {
            out.add(it(Kind.A11Y, "读屏与操作权限", true, State.WARN,
                    "系统里已开启，正在绑定…（稍等或点「重新检查」）→ 去无障碍"));
        } else {
            out.add(it(Kind.A11Y, "读屏与操作权限", true, State.BAD,
                    canSelfEnable(ctx)
                            ? "未授权 → 点「一键开启」（本机已有改系统设置的权限）"
                            : "未授权：无法读取随申办、无法操作\nHyperOS 路径：辅助功能 → 拉到最底部「权限管控」→「已下载的应用」→「挂号助手 · 读屏桥」"));
        }

        // 2) 随申办安装（必需）
        if (isSsbInstalled(ctx)) {
            out.add(it(Kind.INSTALL, "随申办", true, State.OK, "已安装"));
        } else {
            out.add(it(Kind.INSTALL, "随申办", true, State.BAD,
                    "未检测到「随申办」，无法挂号 → 去应用商店安装"));
        }

        // 3) 登录态（必需，深检占位）
        out.add(it(Kind.LOGIN, "随申办登录", true, State.UNKNOWN,
                "尚未检测 → 点「重新检查」自动深检"));

        // 4) 悬浮窗（可选：仅影响运行中的小胶囊）
        boolean ovl = canOverlay(ctx);
        out.add(it(Kind.OVERLAY, "悬浮窗提醒", false,
                ovl ? State.OK : State.WARN,
                ovl ? "已授权：运行时可在角落显示小胶囊"
                        : "未授权：运行中的小胶囊不会出现（不影响挂号）→ 去授权"));

        // 5) 定位（可选：影响自动定位，可手填地址）
        boolean loc = hasLocation(ctx);
        out.add(it(Kind.LOCATION, "定位", false,
                loc ? State.OK : State.WARN,
                loc ? "已授权：可一键自动定位"
                        : "未授权：自动定位不可用，可手填地址 → 去授权"));

        // 6) 后台弹出界面（可选但强烈建议：自检/挂号跑完要把本 App 带回前台）。
        //    这项没法用公开 API 读（MIUI 私有 appop MIUIOP(10017)），所以用「实测证据」判定：
        //    bringSelfToFront() 的三级阶梯若落到最后的「点我回到挂号助手」按钮，就记一次证据。
        long blockedAt = bgLaunchBlockedAt(ctx);
        boolean fresh = bgLaunchBlockedFresh(ctx);
        if (fresh) {
            out.add(it(Kind.BACK_LAUNCH, "自动回到本 App", false, State.WARN,
                    "上次任务结束时被系统拦（MIUI「后台弹出界面」= 拒绝）→ 去开启\n"
                            + "现场只能点屏幕下方的「点我回到 挂号助手」按钮兜底"));
        } else {
            out.add(it(Kind.BACK_LAUNCH, "自动回到本 App", false, State.UNKNOWN,
                    blockedAt > 0
                            ? "上次被拦已超过 " + (BG_BLOCK_TTL_MS / 60000) + " 分钟 → 会再自动试一次"
                            : "未验证：若自检/挂号跑完没自动回到本 App，就是这项被拦 → 去开启"));
        }

        return out;
    }

    /** 所有必需项是否都为 OK（WARN/UNKNOWN/BAD 一律视为未就绪，禁用表单）。 */
    public static boolean allRequiredOk(List<Item> items) {
        if (items == null || items.isEmpty()) {
            return false;
        }
        for (Item i : items) {
            if (i.required && i.state != State.OK) {
                return false;
            }
        }
        return true;
    }

    /** 汇总未就绪的必需项标签，用于顶部一句话提示。 */
    public static List<Item> blocking(List<Item> items) {
        List<Item> b = new ArrayList<>();
        for (Item i : items) {
            if (i.required && i.state != State.OK) {
                b.add(i);
            }
        }
        return b;
    }

    // ---------------------------------------------------------------- 判定原语

    /** 随申办是否安装：Android11+ 需 <queries>（已在 manifest 声明）。 */
    public static boolean isSsbInstalled(Context ctx) {
        try {
            PackageManager pm = ctx.getPackageManager();
            if (pm.getPackageInfo(DeviceBridge.TARGET_PKG, 0) != null) {
                return true;
            }
        } catch (PackageManager.NameNotFoundException e) {
            return false;
        } catch (Exception ignored) {
        }
        // 兜底：能拿到启动 Intent 也算装了
        try {
            return ctx.getPackageManager()
                    .getLaunchIntentForPackage(DeviceBridge.TARGET_PKG) != null;
        } catch (Exception e) {
            return false;
        }
    }

    /** 无障碍是否就绪（已绑定）。未绑定但系统已登记 → 交由 UI 侧走深检/重绑。 */
    public static boolean a11yReady() {
        return SsAccessibilityService.isReady();
    }

    /**
     * 悬浮窗权限。HyperOS 下 {@code Settings.canDrawOverlays} 可能被 MIUI 自身开关劫持，
     * 故仅作可选提示，不作为禁用表单的硬判据。
     */
    public static boolean canOverlay(Context ctx) {
        if (Build.VERSION.SDK_INT < 23) {
            return true;
        }
        try {
            return Settings.canDrawOverlays(ctx);
        } catch (Exception e) {
            return false;
        }
    }

    /**
     * 能否本机直接改写无障碍开关（= 是否已被授予 WRITE_SECURE_SETTINGS）。
     *
     * <p>该权限是 signature|privileged：普通安装默认拿不到，需要用户电脑上执行一次
     * {@code adb shell pm grant com.hospitalhelper.controller android.permission.WRITE_SECURE_SETTINGS}。
     * 拿到后「去开启」就能一键生效；拿不到则退回「跳系统设置 + 显示手动步骤」。
     */
    public static boolean canSelfEnable(Context ctx) {
        try {
            return ctx.checkSelfPermission("android.permission.WRITE_SECURE_SETTINGS")
                    == PackageManager.PERMISSION_GRANTED;
        } catch (Throwable t) {
            return false;
        }
    }

    /** HyperOS/MIUI 手动开启路径（按钮跳转后用户仍找不到开关时的兜底文案）。 */
    public static String a11yManualPath() {
        return "HyperOS / MIUI 手动开启步骤：\n\n"
                + "1. 打开【设置】→【辅助功能】（部分机型叫【无障碍】）\n"
                + "2. 一直【拉到页面最底部】，找到「权限管控」分组\n"
                + "3. 点【已下载的应用】\n"
                + "4. 在列表里找【挂号助手 · 读屏桥】（显示「已关闭」）\n"
                + "5. 打开【使用「挂号助手 · 读屏桥」】开关\n"
                + "6. 弹出「危险」提示：先【勾选】「我已知晓可能存在的风险…」，\n"
                + "   再等 5 秒倒计时结束，点【确定】\n\n"
                + "提示：第 6 步必须「先勾选、再点确定」，且「确定」按钮有 5 秒倒计时；\n"
                + "只点开关不勾选是不会生效的。\n\n"
                + "一次性免翻页方案（可选，电脑上执行一次即可）：\n"
                + "  adb shell pm grant com.hospitalhelper.controller "
                + "android.permission.WRITE_SECURE_SETTINGS\n"
                + "之后本页的「一键开启」就能直接打开读屏桥。";
    }

    /** 「后台弹出界面」被拦的证据（0 = 没有证据）。存 hh_config/bg_launch_blocked。 */
    public static long bgLaunchBlockedAt(Context ctx) {
        try {
            return ctx.getApplicationContext()
                    .getSharedPreferences("hh_config", Context.MODE_PRIVATE)
                    .getLong("bg_launch_blocked", 0L);
        } catch (Throwable t) {
            return 0L;
        }
    }

    /**
     * 拦截证据的有效期。MIUI「后台弹出界面」每次 adb 覆盖安装都会被重置回 ignore，
     * 用户事后开启是常态；证据若永久生效，本 App 就再也不会自动深检登录（门禁永久灰）。
     * 所以超过这个时间就把证据当「可能已过期」，再给自动深检一次机会：
     * 真被拦会重新记证据，放行则由 bringSelfToFront 清掉证据。
     */
    public static final long BG_BLOCK_TTL_MS = 10 * 60 * 1000L;

    /** 证据是否仍然新鲜（新鲜才据此放弃自动深检；过期允许再试一次）。 */
    public static boolean bgLaunchBlockedFresh(Context ctx) {
        long at = bgLaunchBlockedAt(ctx);
        return at > 0 && (System.currentTimeMillis() - at) < BG_BLOCK_TTL_MS;
    }

    /** 记一次「自动回前台被拦」证据（bringSelfToFront 落到兜底按钮时调用）。 */
    public static void markBgLaunchBlocked(Context ctx) {
        try {
            ctx.getApplicationContext()
                    .getSharedPreferences("hh_config", Context.MODE_PRIVATE)
                    .edit().putLong("bg_launch_blocked", System.currentTimeMillis()).apply();
        } catch (Throwable ignored) {
        }
    }

    /** 阶梯成功回到前台 → 清掉证据（说明这次系统放行了）。 */
    public static void clearBgLaunchBlocked(Context ctx) {
        try {
            ctx.getApplicationContext()
                    .getSharedPreferences("hh_config", Context.MODE_PRIVATE)
                    .edit().remove("bg_launch_blocked").apply();
        } catch (Throwable ignored) {
        }
    }

    /** MIUI/HyperOS 手动开启路径 + 一次性 adb 方案（跳转后用户仍找不到开关时的兜底文案）。 */
    public static String bgLaunchManualPath() {
        return "MIUI / HyperOS「后台弹出界面」开启步骤：\n\n"
                + "1. 设置 → 应用设置 → 应用管理 → 挂号助手\n"
                + "2. 权限管理 → 找到「后台弹出界面」→ 改为「允许」\n"
                + "3. 顺手确认「显示在其他应用上层」（悬浮窗胶囊）也是允许\n\n"
                + "注意：每次 adb 覆盖安装后该权限会被重置，需要重新开启。\n\n"
                + "一次性免翻页方案（电脑上执行一次）：\n"
                + "  adb shell appops set com.hospitalhelper.controller 10017 allow";
    }

    /** 是否具备（近似）定位权限。 */
    public static boolean hasLocation(Context ctx) {
        try {
            return ctx.checkSelfPermission(android.Manifest.permission.ACCESS_FINE_LOCATION)
                    == PackageManager.PERMISSION_GRANTED
                    || ctx.checkSelfPermission(android.Manifest.permission.ACCESS_COARSE_LOCATION)
                    == PackageManager.PERMISSION_GRANTED;
        } catch (Exception e) {
            return false;
        }
    }
}
