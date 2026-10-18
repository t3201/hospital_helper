package com.hospitalhelper.controller;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Path;
import android.graphics.Rect;
import android.os.Build;
import android.os.Bundle;
import android.os.SystemClock;
import android.util.DisplayMetrics;
import android.util.Log;
import android.view.Display;
import android.view.WindowManager;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import android.view.inputmethod.EditorInfo;

import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * {@link DeviceBridge} 的端上实现 —— 全部基于 AccessibilityService（约束#2：零第三方依赖）。
 *
 * <p>关键实现决策（对应平台核对给的硬约束）：
 * <ul>
 *   <li>#3 读屏用 {@code getWindows()} 遍历 {@link AccessibilityWindowInfo} 并
 *       按 {@code getPackageName()==随申办} 过滤取 root；空窗先短重试（切换瞬间的空窗），
 *       最后才用 {@code getRootInActiveWindow()} 且<b>必须</b>校验包名==随申办
 *       （否则会把控制器自己的树喂给上层）。</li>
 *   <li>#4 输入文字用 {@code ACTION_SET_TEXT}（中英文通吃），不用 ADBKeyboard/自研 IME。</li>
 *   <li>#5 回车搜索：对焦点 EditText 发 {@code ACTION_PERFORM_EDITOR_ACTION}，或点“搜索”节点。</li>
 *   <li>#6 点击优先向上找 clickable 祖先 {@code ACTION_CLICK}，否则 {@code dispatchGesture} 单点；
 *       滑动 {@code dispatchGesture(Path+StrokeDescription)}；返回 {@code GLOBAL_ACTION_BACK}。</li>
 *   <li>#7 launch 用 {@code getLaunchIntentForPackage}+NEW_TASK（API27 可用），{@code CLEAR_TOP} 等效重启。</li>
 *   <li>#8 等待：事件驱动（service 的 EVENT_LOCK）+ 400ms 轮询兜底 + 超时。</li>
 * </ul>
 */
public class AccDeviceBridge implements DeviceBridge {

    private final AccessibilityService svc;

    // ------------------------------------------------------------------ binder 防僵死闸门
    // 2026-10-09 实测(476cf991/HyperOS): 服务被 MIUI 解绑/重绑时 getWindows()/getRoot() 这类
    // binder 调用会阻塞 2~5 分钟, 整条采集线卡在 dump() 里。策略:
    //   1) 调用前打 HHDIAG `enter windowRoot`, 返回后打 `nWindows=` => 能分辨卡在 binder 前还是内;
    //   2) 调用前先判 svc != null && 已绑定, 否则**立即返回空**(不等);
    //   3) getWindows()+getRoot()+序列化 全放进 Executor, Future.get(4s), 超时=>cancel+返回空
    //      (让上层走 launch/重试), **绝不无限等**;
    //   4) 连续 2 次超时 => 打一条明确日志 `windowRoot timeout(binder?)`, 并进入短暂冷却。
    private static final long BINDER_TIMEOUT_MS = 4000L;
    private static final long COOLDOWN_MS = 3000L;
    private static final AtomicInteger TIMEOUT_STREAK = new AtomicInteger(0);
    private static volatile long cooldownUntil = 0L;

    // 空窗重试预算(2026-10-14 诊断 0043d38b0504/MI 5X/Android 8.1)：见 windowRootNow() 注释。
    // 必须远小于 BINDER_TIMEOUT_MS，否则重试本身会把 binder 闸门拖超时。
    private static final int WINDOW_RETRY_MAX = 2;
    private static final long WINDOW_RETRY_SLEEP_MS = 350L;
    private static final long WINDOW_RETRY_BUDGET_MS = 1500L;

    /** 专用小线程池: binder 卡死时最多占用 2 个线程; 调用方最多等 BINDER_TIMEOUT_MS。 */
    private static final ExecutorService BINDER_POOL = Executors.newFixedThreadPool(2, new ThreadFactory() {
        @Override
        public Thread newThread(Runnable r) {
            Thread t = new Thread(r, "hh-windowroot");
            t.setDaemon(true);
            return t;
        }
    });

    public AccDeviceBridge(AccessibilityService svc) {
        this.svc = svc;
    }

    /** svc 是否仍与系统绑定(MIUI 解绑后 binder 调用会阻塞/抛异常)。 */
    private boolean bound() {
        AccessibilityService s = svc;
        if (s == null) {
            return false;
        }
        if (s instanceof SsAccessibilityService) {
            return ((SsAccessibilityService) s).isBound();
        }
        return true;
    }

    /**
     * 把可能阻塞的 binder 调用放进 {@link #BINDER_POOL}，最多等 {@link #BINDER_TIMEOUT_MS}；
     * 超时 → cancel 并返回 null（上层据此走 launch/重试），**绝不无限等**。
     */
    private <T> T binderGuard(String tag, Callable<T> call) {
        if (!bound()) {
            Log.i("HHDIAG", tag + ": svc==null/未绑定 => 立即返回空(不等 binder)");
            return null;
        }
        long now = SystemClock.uptimeMillis();
        if (TIMEOUT_STREAK.get() >= 2 && now < cooldownUntil) {
            Log.i("HHDIAG", tag + ": 冷却中(连续超时 " + TIMEOUT_STREAK.get() + " 次) => 立即返回空");
            return null;
        }
        Future<T> f = null;
        try {
            f = BINDER_POOL.submit(call);
            T v = f.get(BINDER_TIMEOUT_MS, TimeUnit.MILLISECONDS);
            if (v != null) {
                TIMEOUT_STREAK.set(0);
                cooldownUntil = 0L;
            }
            return v;
        } catch (TimeoutException e) {
            if (f != null) {
                f.cancel(true);   // binder 调用不可中断: 只放弃等待, 线程等 binder 自行返回后回收
            }
            int n = TIMEOUT_STREAK.incrementAndGet();
            cooldownUntil = SystemClock.uptimeMillis() + COOLDOWN_MS;
            Log.i("HHDIAG", tag + ": >" + BINDER_TIMEOUT_MS + "ms 超时 => 取消并返回空(连续 " + n + " 次)");
            if (n >= 2) {
                Log.i("HHDIAG", "windowRoot timeout(binder?) 连续 " + n + " 次 => 冷却 " + COOLDOWN_MS + "ms");
            }
            return null;
        } catch (Exception e) {
            Log.i("HHDIAG", tag + " exc: " + e);
            return null;
        }
    }

    // ------------------------------------------------------------------ launch

    @Override
    public boolean launch(String pkg) {
        try {
            PackageManager pm = svc.getPackageManager();
            Intent it = pm.getLaunchIntentForPackage(pkg);
            if (it == null) {
                Log.i("HHDIAG", "launch(" + pkg + ") -> false  (getLaunchIntentForPackage==null)");
                return false; // 未安装 / 未做 <queries> 可见性声明
            }
            // NEW_TASK：从服务外启动必须有；CLEAR_TOP：等效 force-stop 重启（force-stop 无对等）
            it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
            svc.startActivity(it);
            Log.i("HHDIAG", "launch(" + pkg + ") -> true  intent=" + it);
            return true;
        } catch (Exception e) {
            Log.i("HHDIAG", "launch(" + pkg + ") -> false  exception=" + e);
            return false;
        }
    }

    // ------------------------------------------------------------------ dump / 取根

    /**
     * 约束#3：遍历所有窗口，按 root 的 packageName == 随申办 过滤取 root。
     * 取不到返回 null（调用方如实报错）。
     *
     * <p>2026-10-09：真正的 binder 工作在 {@link #windowRootNow()} 里，这里只加超时闸门。
     */
    private AccessibilityNodeInfo windowRoot() {
        return binderGuard("windowRoot", new Callable<AccessibilityNodeInfo>() {
            @Override
            public AccessibilityNodeInfo call() {
                return windowRootNow();
            }
        });
    }

    /**
     * windowRoot 的实际 binder 工作（只在 {@link #binderGuard} 的工作线程里调用）。
     *
     * <p>2026-10-14 诊断(0043d38b0504 / MI 5X / Android 8.1)：{@code getWindows()} 在 activity
     * 切换瞬间会返回「systemui + 两个 pkg=null,root=null」的快照（493 次里 60 次，且成串爆发：
     * 11:49 那一分钟 30/30 全空）。上层据此判 kind=other 并整机重启 —— MI5X 23 分钟内 launch()
     * 71 次，自我放大，最终号源只剩档位先验/未核实。空窗绝大多数是 &lt;1s 的过渡态，故在
     * {@link #binderGuard} 的 4s 预算内补最多 {@link #WINDOW_RETRY_MAX} 次短重试；仍拿不到再用
     * {@link #activeRootChecked()}（严格校验包名）兜底，最后才返回 null。
     */
    private AccessibilityNodeInfo windowRootNow() {
        long t0 = SystemClock.uptimeMillis();
        AccessibilityNodeInfo best = windowRootOnce();
        if (best != null) {
            return best;
        }
        for (int r = 0; r < WINDOW_RETRY_MAX; r++) {
            long spent = SystemClock.uptimeMillis() - t0;
            if (spent > WINDOW_RETRY_BUDGET_MS) {
                Log.i("HHDIAG", "windowRoot: 空窗重试放弃(预算 " + WINDOW_RETRY_BUDGET_MS
                        + "ms, 已用 " + spent + "ms)");
                break;
            }
            SystemClock.sleep(WINDOW_RETRY_SLEEP_MS);
            best = windowRootOnce();
            if (best != null) {
                Log.i("HHDIAG", "windowRoot: 空窗重试 " + (r + 1) + "/" + WINDOW_RETRY_MAX
                        + " 命中 => 上一轮是切换瞬间的空窗");
                return best;
            }
        }
        best = activeRootChecked();
        if (best != null) {
            Log.i("HHDIAG", "windowRoot: 窗口扫描未命中, activeRoot 兜底命中随申办");
            return best;
        }
        Log.i("HHDIAG", "windowRoot: 无随申办窗口(重试后仍无) => 返回空");
        return null;
    }

    /** 单次窗口扫描：{@code getWindows()} + 按 {@code root.package==随申办} 打分取最优。 */
    private AccessibilityNodeInfo windowRootOnce() {
        List<AccessibilityWindowInfo> ws;
        Log.i("HHDIAG", "enter windowRoot");   // 之后没有 nWindows= 这行 => 卡在 getWindows() 里
        try {
            ws = svc.getWindows();
        } catch (Exception e) {
            Log.i("HHDIAG", "windowRoot: getWindows() exc=" + e);
            return null;
        }
        if (ws == null || ws.isEmpty()) {
            Log.i("HHDIAG", "windowRoot: getWindows() -> null/empty");
            return null;
        }
        Log.i("HHDIAG", "windowRoot: nWindows=" + ws.size());   // getWindows() 已返回(卡点在 binder 内 => 无此行)
        // HHDIAG：打印 getWindows() 里每个窗口的包名（用 root 的 getPackageName()，
        // 与下面匹配逻辑同一信号；AccessibilityWindowInfo.getPackageName() 不在本 compileSdk）
        StringBuilder wl = new StringBuilder();
        for (int i = 0; i < ws.size(); i++) {
            AccessibilityWindowInfo wi = ws.get(i);
            AccessibilityNodeInfo ri = safeRoot(wi);
            CharSequence rp;
            try {
                rp = ri == null ? null : ri.getPackageName();
            } catch (Exception e) {
                rp = null;
            }
            wl.append(i).append("[pkg=").append(rp)
                    .append(",root=").append(ri == null ? "null" : "ok")
                    .append(",type=").append(safeType(wi)).append("] ");
        }
        Log.i("HHDIAG", "windowRoot: windows=" + wl.toString().trim());
        // 首选：root.package == 随申办。实测同一时刻可能有 2 个随申办窗口(nWindows=5)，
        // 取第一个可能是后台/旧页面窗口(WebView 内容不在里面 => et_search/EditText 永远找不到)，
        // 因此按 focused(4) > active(2) > 根节点铺满全屏(1) 打分取最优。
        AccessibilityNodeInfo best = null;
        int bestScore = -1;
        for (AccessibilityWindowInfo w : ws) {
            AccessibilityNodeInfo r = safeRoot(w);
            if (r == null) {
                continue;
            }
            CharSequence p = null;
            try {
                p = r.getPackageName();
            } catch (Exception ignored) {
            }
            if (p == null || !TARGET_PKG.equals(p.toString())) {
                continue;
            }
            int score = 0;
            try {
                if (w.isFocused()) score += 4;
            } catch (Exception ignored) {
            }
            try {
                if (w.isActive()) score += 2;
            } catch (Exception ignored) {
            }
            Rect rb = new Rect();
            try {
                r.getBoundsInScreen(rb);
            } catch (Exception ignored) {
            }
            if (rb.width() * rb.height() > 100000) score += 1;
            if (score > bestScore) {
                bestScore = score;
                best = r;
            }
        }
        if (best != null) {
            return best;
        }
        // 2026-10-09 复测教训：这里的「任意非本应用 application 窗口」兜底会把
        // 桌面(com.miui.home)/浏览器(com.android.browser)的树喂给上层，
        // 于是按 content-desc="搜索框" 命中桌面搜索条 (539,2217) 并点开浏览器。
        // 随申办窗口不在可见窗口里 = 它不在前台，必须返回空让上层 launch/重试。
        String top = null;
        for (AccessibilityWindowInfo w : ws) {
            if (safeType(w) == AccessibilityWindowInfo.TYPE_APPLICATION) {
                AccessibilityNodeInfo r = safeRoot(w);
                if (r != null) {
                    try {
                        top = String.valueOf(r.getPackageName());
                    } catch (Exception ignored) {
                    }
                    break;
                }
            }
        }
        Log.i("HHDIAG", "windowRoot: 本轮无随申办窗口(前台应用=" + top + ")");
        return null;
    }

    /**
     * 兜底：{@code getRootInActiveWindow()}，**仅当包名 == 随申办** 才采用。
     *
     * <p>Android 8.x 上 {@code AccessibilityWindowInfo.getPackageName()} 不可用（API 30），且非活动
     * 窗口的 {@code getRoot()} 常返回 null，导致窗口扫描无法判定归属；活动窗口仍可用本方法取到。
     * 类注释里的约束#3（不用 getRootInActiveWindow，会拿到控制器自己）由这里的包名校验保证：
     * 非随申办的树一律不回传。
     */
    private AccessibilityNodeInfo activeRootChecked() {
        try {
            AccessibilityNodeInfo r = svc.getRootInActiveWindow();
            if (r == null) {
                return null;
            }
            CharSequence p = r.getPackageName();
            if (p == null || !TARGET_PKG.equals(p.toString())) {
                Log.i("HHDIAG", "windowRoot: activeRoot pkg=" + p + " != 随申办 => 不采用");
                return null;
            }
            return r;
        } catch (Exception e) {
            Log.i("HHDIAG", "windowRoot: activeRoot exc: " + e);
            return null;
        }
    }

    private AccessibilityNodeInfo safeRoot(AccessibilityWindowInfo w) {
        try {
            return w.getRoot();
        } catch (Exception e) {
            return null;
        }
    }

    private int safeType(AccessibilityWindowInfo w) {
        try {
            return w.getType();
        } catch (Exception e) {
            return -1;
        }
    }

    /**
     * 读屏。{@code getWindows()+getRoot()+序列化} 整体在 {@link #binderGuard} 的工作线程里跑，
     * 最多 {@link #BINDER_TIMEOUT_MS}；超时/未绑定 → null（Python 侧按「读屏为空」走 launch/重试）。
     */
    @Override
    public JSONObject dump() {
        return binderGuard("dump", new Callable<JSONObject>() {
            @Override
            public JSONObject call() {
                AccessibilityNodeInfo root = windowRootNow();
                if (root == null) {
                    return null;
                }
                return NodeTree.dump(root, rotationNow());
            }
        });
    }

    private int rotationNow() {
        try {
            WindowManager wm = (WindowManager) svc.getSystemService(Context.WINDOW_SERVICE);
            if (wm != null) {
                Display d = wm.getDefaultDisplay();
                if (d != null) {
                    return d.getRotation();
                }
            }
        } catch (Exception ignored) {
        }
        return 0;
    }

    // ------------------------------------------------------------------ findNode

    @Override
    public NodeRef findNode(String text, String desc, String cls, Rect near) {
        if (text == null && desc == null && cls == null && near == null) {
            return null;
        }
        // 取根 + 遍历子节点同样是 binder 调用 => 一并放进超时闸门里
        return binderGuard("findNode", new Callable<NodeRef>() {
            @Override
            public NodeRef call() {
                return findNodeNow(text, desc, cls, near);
            }
        });
    }

    private NodeRef findNodeNow(String text, String desc, String cls, Rect near) {
        AccessibilityNodeInfo root = windowRootNow();
        if (root == null) {
            return null;
        }
        List<AccessibilityNodeInfo> all = new ArrayList<>();
        collect(root, all, 0, NodeTree.DEFAULT_MAX_DEPTH);

        NodeRef best = null;
        double bestScore = Double.MAX_VALUE;
        for (AccessibilityNodeInfo n : all) {
            String t = s(n.getText());
            String d = s(n.getContentDescription());
            String c = s(n.getClassName());
            if (text != null && !(t.equals(text) || t.contains(text))) {
                continue;
            }
            if (desc != null && !(d.contains(desc) || t.contains(desc))) {
                continue;
            }
            if (cls != null && !c.contains(cls)) {
                continue;
            }
            Rect r = new Rect();
            try {
                n.getBoundsInScreen(r);
            } catch (Exception ignored) {
                continue;
            }
            if (near != null && !Rect.intersects(near, r)) {
                continue;
            }
            double score = near != null
                    ? dist2(r.centerX(), r.centerY(), near.centerX(), near.centerY())
                    : (double) r.width() * r.height();
            if (best == null || score < bestScore) {
                bestScore = score;
                best = new NodeRef(n, t, d, c, r);
            }
        }
        return best;
    }

    private void collect(AccessibilityNodeInfo n, List<AccessibilityNodeInfo> out, int depth, int maxDepth) {
        if (n == null || depth > maxDepth) {
            return;
        }
        out.add(n);
        int cc;
        try {
            cc = n.getChildCount();
        } catch (Exception e) {
            return;
        }
        for (int i = 0; i < cc; i++) {
            AccessibilityNodeInfo c = null;
            try {
                c = n.getChild(i);
            } catch (Exception ignored) {
            }
            if (c != null) {
                collect(c, out, depth + 1, maxDepth);
            }
        }
    }

    // ------------------------------------------------------------------ 点击 / 滑动 / 返回

    @Override
    public boolean click(NodeRef node) {
        if (node == null) {
            return false;
        }
        AccessibilityNodeInfo cur = node.aw;
        while (cur != null) { // 约束#6：向上找 clickable 祖先
            try {
                if (cur.isClickable() && cur.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                    return true;
                }
                cur = cur.getParent();
            } catch (Exception e) {
                break;
            }
        }
        // 兜底：按 bounds 中心 dispatchGesture 单点
        return node.cx() >= 0 && tap(node.cx(), node.cy());
    }

    @Override
    public boolean click(int x, int y) {
        return tap(x, y);
    }

    @Override
    public boolean tap(int x, int y) {
        if (x < 0 || y < 0) {
            return false;
        }
        try {
            Path p = new Path();
            p.moveTo(x, y);
            GestureDescription.StrokeDescription s =
                    new GestureDescription.StrokeDescription(p, 0, 50);
            GestureDescription g = new GestureDescription.Builder().addStroke(s).build();
            return svc.dispatchGesture(g, null, null);
        } catch (Exception e) {
            return false;
        }
    }

    @Override
    public boolean swipe(int x1, int y1, int x2, int y2, long durationMs) {
        try {
            Path p = new Path();
            p.moveTo(x1, y1);
            p.lineTo(x2, y2);
            long dur = Math.max(1, durationMs);
            GestureDescription.StrokeDescription s =
                    new GestureDescription.StrokeDescription(p, 0, dur);
            GestureDescription g = new GestureDescription.Builder().addStroke(s).build();
            return svc.dispatchGesture(g, null, null);
        } catch (Exception e) {
            return false;
        }
    }

    @Override
    public boolean back() {
        try {
            return svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK);
        } catch (Exception e) {
            return false;
        }
    }

    // ------------------------------------------------------------------ 输入

    @Override
    public boolean inputText(NodeRef node, String text) {
        if (node == null || text == null) {
            return false;
        }
        AccessibilityNodeInfo target = node.aw;
        if (target == null) {
            return false;
        }
        if (!target.isEditable()) { // 命中容器时向下找一个可编辑子节点
            AccessibilityNodeInfo e = findEditable(target, 0, 6);
            if (e != null) {
                target = e;
            }
        }
        try {
            target.performAction(AccessibilityNodeInfo.ACTION_FOCUS);
            Bundle b = new Bundle();
            // 约束#4：中英文通吃，不依赖 ADBKeyboard / 自研 IME
            b.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
            return target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, b);
        } catch (Exception e) {
            return false;
        }
    }

    private AccessibilityNodeInfo findEditable(AccessibilityNodeInfo n, int depth, int maxDepth) {
        if (n == null || depth > maxDepth) {
            return null;
        }
        try {
            if (n.isEditable()) {
                return n;
            }
            int cc = n.getChildCount();
            for (int i = 0; i < cc; i++) {
                AccessibilityNodeInfo e = findEditable(n.getChild(i), depth + 1, maxDepth);
                if (e != null) {
                    return e;
                }
            }
        } catch (Exception ignored) {
        }
        return null;
    }

    @Override
    public boolean pressEnter(NodeRef node) {
        if (node == null || node.aw == null) {
            return false;
        }
        AccessibilityNodeInfo n = node.aw;
        try {
            n.performAction(AccessibilityNodeInfo.ACTION_FOCUS);
        } catch (Exception ignored) {
        }
        // 说明：原“路径1”用 AccessibilityNodeInfo.ACTION_PERFORM_EDITOR_ACTION /
        // ACTION_ARGUMENT_EDITOR_ACTION，但这两个常量并不存在于 Android framework
        // （android-27 与 android-34 的 android.jar 均无），属无效引用，已移除。
        // 回车/搜索一律走“路径2”：点击界面上的“搜索”节点。
        // 路径 2：点“搜索”节点
        NodeRef search = findNode("搜索", "搜索", null, null);
        if (search != null) {
            return click(search);
        }
        return false;
    }

    // ------------------------------------------------------------------ 等待

    @Override
    public boolean waitForText(String text, long timeoutMs) {
        if (text == null) {
            return false;
        }
        long deadline = SystemClock.uptimeMillis() + Math.max(0, timeoutMs);
        while (true) {
            JSONObject d = dump();
            if (d != null && NodeTree.containsText(d, text)) {
                return true;
            }
            long left = deadline - SystemClock.uptimeMillis();
            if (left <= 0) {
                return false;
            }
            // 事件驱动：service 每次收到 WINDOW_* 事件会 notifyAll；同时 400ms 轮询兜底 + 超时
            synchronized (SsAccessibilityService.eventLock()) {
                try {
                    SsAccessibilityService.eventLock().wait(Math.min(400, left));
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return false;
                }
            }
        }
    }

    // ------------------------------------------------------------------ 屏幕 / 时间

    @Override
    public int[] screenSize() {
        try {
            WindowManager wm = (WindowManager) svc.getSystemService(Context.WINDOW_SERVICE);
            if (wm == null) {
                return new int[]{0, 0};
            }
            if (Build.VERSION.SDK_INT >= 30) {
                Rect b = wm.getCurrentWindowMetrics().getBounds();
                return new int[]{b.width(), b.height()};
            }
            Display d = wm.getDefaultDisplay();
            DisplayMetrics m = new DisplayMetrics();
            d.getRealMetrics(m);
            return new int[]{m.widthPixels, m.heightPixels};
        } catch (Exception e) {
            return new int[]{0, 0};
        }
    }

    @Override
    public long now() {
        return System.currentTimeMillis();
    }

    // ------------------------------------------------------------------ util

    private static double dist2(int x1, int y1, int x2, int y2) {
        double dx = x1 - x2;
        double dy = y1 - y2;
        return dx * dx + dy * dy;
    }

    private static String s(CharSequence cs) {
        return cs == null ? "" : cs.toString();
    }
}
