package com.hospitalhelper.controller;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Build;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * PyBridge —— Python(Chaquopy) ↔ Java 的静态门面（P3）。
 *
 * <p>契约：<b>边界只用 String / int / boolean</b>，绝不传 Java 对象。设备原语全部转发到
 * {@link SsAccessibilityService#bridge()}（{@link AccDeviceBridge}）。
 *
 * <p>对 Python 暴露的方法（见 python/BRIDGE.md）：
 * <pre>
 *   launch() dumpJson() tap(..) swipe(..) back() keyevent(int) pressEnter()
 *   inputText(String) waitForText(String,long) screenSize() now() isReady()
 *   emitLog(String) setResult(String) getConfig/setConfig(String,String)
 *   dataDir() outDir() extractAssetsIfNeeded()
 * </pre>
 *
 * <p>{@link #dumpJson()} 名保留（任务约定），但返回 <b>与 uiautomator dump 同构的 XML 文本</b>
 * （{@code <hierarchy><node .../></hierarchy>}），以便端上 Python 侧既有的 XML 正则解析零改动。
 */
public final class PyBridge {

    /** UI 回调：日志逐行 / 结果 JSON。由 MainActivity 注册，PyBridge 只转发。
     *
     * <p>三类通道（Python → Java，边界只用 String）：
     * <ul>
     *   <li>{@link #onStep}  浮窗逐步骤（寻找tab:xx / 点击科室:yy / 进入专家门诊…）→ 更新 MiniChip；
     *   <li>{@link #onReasoning} 模型推理（两次 LLM 调用，thinking/content 逐块原样转发）→ 实时日志页"模型推理"区，**恒显**；
     *   <li>{@link #onLog}  细节日志 → 实时日志页"详细日志"区，默认折叠。</li>
     * </ul>
     */
    public interface Listener {
        void onLog(String line);

        /** 逐步骤提示（浮窗）。 */
        void onStep(String text);

        /**
         * 带状态的逐步骤提示（2026-05 ⑤：浮窗只由**已验证**状态驱动）。
         *
         * <p>state = {@code run}(常规进度) / {@code verify}(校验中：动作已发出、还没被
         * 屏幕证据证实) / {@code ok}(观测层已核实到达) / {@code fail}(校验失败)。
         * 默认实现退回 {@link #onStep}，保证旧宿主不受影响。
         */
        default void onStepState(String text, String state) {
            onStep(text);
        }

        /** 模型推理逐块内容，恒显。
         *  kind: 'think'(思维链) / 'content'(正文) / 'meta'(标记行)。 */
        void onReasoning(String text, String kind);

        void onResult(String json);
    }

    private static final String PREF = "hh_config";
    private static final String DATA_SUBDIR = "data";
    private static final String OUT_SUBDIR = "out";
    private static final String ASSET_DATA_ROOT = "data";
    private static final String VERSION_ASSET = "data/_version";

    private static volatile Context appCtx;
    private static volatile Listener listener;

    private PyBridge() {
    }

    /** Activity/Application 启动时调用一次（存 applicationContext，避免泄漏）。 */
    public static void init(Context ctx) {
        if (ctx != null) {
            appCtx = ctx.getApplicationContext();
        }
    }

    public static void setListener(Listener l) {
        listener = l;
    }

    private static Context ctx() {
        Context c = appCtx;
        if (c == null) {
            throw new IllegalStateException("PyBridge.init(Context) 未调用");
        }
        return c;
    }

    private static DeviceBridge bridge() {
        return SsAccessibilityService.bridge();
    }

    // ------------------------------------------------------------------ 设备原语

    /** 唤起随申办（CLEAR_TOP 等效重启）。 */
    public static boolean launch() {
        DeviceBridge b = bridge();
        return b != null && b.launch(DeviceBridge.TARGET_PKG);
    }

    /** 无障碍服务是否已 Bound（读屏桥可用性）。 */
    public static boolean isReady() {
        return SsAccessibilityService.isReady();
    }

    /** 读屏 -> uiautomator 同构 XML；无窗口返回 ""（Python 侧据此报错）。 */
    public static String dumpJson() {
        DeviceBridge b = bridge();
        if (b == null) {
            return "";
        }
        JSONObject tree = b.dump();
        if (tree == null) {
            return "";
        }
        return toXml(tree);
    }

    public static boolean tap(int x, int y) {
        DeviceBridge b = bridge();
        return b != null && b.tap(x, y);
    }

    public static boolean swipe(int x1, int y1, int x2, int y2, int durMs) {
        DeviceBridge b = bridge();
        return b != null && b.swipe(x1, y1, x2, y2, Math.max(1, durMs));
    }

    public static boolean back() {
        DeviceBridge b = bridge();
        return b != null && b.back();
    }

    /** 按键映射：3=HOME、4=BACK、66=ENTER；其余 no-op。 */
    public static boolean keyevent(int k) {
        DeviceBridge b = bridge();
        if (b == null) {
            return false;
        }
        switch (k) {
            case 3:
                return home();
            case 4:
                return b.back();
            case 66:
                return pressEnter();
            default:
                return false;
        }
    }

    /** HOME（AccDeviceBridge 未暴露 GLOBAL_ACTION_HOME 时回退 launch 无效）。 */
    private static boolean home() {
        try {
            SsAccessibilityService s = SsAccessibilityService.get();
            if (s == null) {
                return false;
            }
            return s.performGlobalAction(android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_HOME);
        } catch (Exception e) {
            return false;
        }
    }

    /** 回车/搜索：优先焦点 EditText，其次页面上「搜索」节点。 */
    public static boolean pressEnter() {
        DeviceBridge b = bridge();
        if (b == null) {
            return false;
        }
        DeviceBridge.NodeRef n = b.findNode(null, null, "EditText", null);
        if (n == null) {
            n = b.findNode("搜索", "搜索", null, null);
        }
        return n != null && b.pressEnter(n);
    }

    /** 输入文本（ACTION_SET_TEXT，中英文通吃，不依赖输入法）。 */
    public static boolean inputText(String text) {
        DeviceBridge b = bridge();
        if (b == null || text == null) {
            return false;
        }
        DeviceBridge.NodeRef n = b.findNode(null, null, "EditText", null);
        if (n == null) {
            return false;
        }
        return b.inputText(n, text);
    }

    public static boolean waitForText(String text, long timeoutMs) {
        DeviceBridge b = bridge();
        return b != null && b.waitForText(text, timeoutMs);
    }

    /** 屏幕尺寸 "w,h"（Python 侧切分）。 */
    public static String screenSize() {
        DeviceBridge b = bridge();
        int[] wh = (b != null) ? b.screenSize() : null;
        if (wh == null) {
            return "1080,2392";
        }
        return wh[0] + "," + wh[1];
    }

    /**
     * 最后已知位置（LocationManager，WGS-84）："lat,lng,provider,ageMs"；
     * 无可用 provider / 无最后位置 / 未授权 → 返回 ""（Python 侧据此判失败）。
     * 只读 lastKnownLocation，不启动持续定位、不申请权限（权限已在 manifest 声明，
     * 未授权时 getLastKnownLocation 返回 null，安全）。
     */
    public static String lastKnownLocation() {
        try {
            Context c = ctx().getApplicationContext();
            android.location.LocationManager lm =
                    (android.location.LocationManager) c.getSystemService(Context.LOCATION_SERVICE);
            if (lm == null) {
                return "";
            }
            String bestProv = null;
            android.location.Location best = null;
            for (String prov : new String[]{"fused", "gps", "network"}) {
                boolean ok;
                try {
                    ok = lm.isProviderEnabled(prov);
                } catch (IllegalArgumentException e) {
                    continue;  // provider 不存在（如无 fused）
                }
                if (!ok) {
                    continue;
                }
                android.location.Location loc;
                try {
                    loc = lm.getLastKnownLocation(prov);
                } catch (SecurityException e) {
                    return "";
                }
                if (loc != null && (best == null || loc.getTime() > best.getTime())) {
                    best = loc;
                    bestProv = prov;
                }
            }
            if (best == null) {
                return "";
            }
            long ageMs = Math.max(0L, System.currentTimeMillis() - best.getTime());
            return best.getLatitude() + "," + best.getLongitude() + "," + bestProv + "," + ageMs;
        } catch (Exception e) {
            return "";
        }
    }

    /** 设备本地时间 ISO-8601（Python datetime.fromisoformat 直读）。 */
    public static String now() {
        long ms = System.currentTimeMillis();
        SimpleDateFormat f = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS", Locale.US);
        return f.format(new Date(ms));
    }

    // ------------------------------------------------------------------ UI 回调

    public static void emitLog(String line) {
        Listener l = listener;
        if (l != null && line != null) {
            l.onLog(line);
        } else if (l == null && line != null) {
            // Activity 不在（进程被 ROM 回收后由广播/服务重新拉起）：日志仍要进 logcat，
            // 否则 PC 侧复盘时"什么都没发生"，也没法核对浮窗说的是不是屏上真的事。
            Log.i("HH_APP", line.replace('\n', ' '));
        }
    }

    /** 逐步骤提示（浮窗）：寻找tab:xx / 点击科室:yy / 进入专家门诊… */
    public static void emitStep(String text) {
        Listener l = listener;
        if (l != null && text != null) {
            l.onStep(text);
            return;
        }
        // 没有 Activity 也要让浮窗跟着屏幕走：停在旧文案上的浮窗 = "浮窗与屏幕对不上"。
        if (text != null) {
            if (miniChipOn()) {
                try {
                    MiniChip.show(ctx(), text);
                } catch (Throwable ignored) {
                }
            }
            Log.i("HH_APP", "[step] " + text.replace('\n', ' '));
        }
    }

    /**
     * 带状态的逐步骤提示(2026-05 圈5)。state: run / verify(校验中) / ok(已验证) / fail(校验失败)。
     *
     * <p>约定: **未验证的目标不播报**。Python 侧只在 act() 判定通过后才发 ok/组进度;
     * 动作已发出但尚未被屏幕证据证实时发 verify, 判定失败发 fail —— 浮窗因此与屏幕一致。
     */
    public static void emitStepState(String text, String state) {
        Listener l = listener;
        if (l == null || text == null) {
            if (text != null) {
                if (miniChipOn()) {
                    try {
                        MiniChip.showState(ctx(), text, state == null ? "run" : state);
                    } catch (Throwable ignored) {
                    }
                }
                Log.i("HH_APP", "[step:" + (state == null ? "run" : state) + "] "
                        + text.replace('\n', ' '));
            }
            return;
        }
        l.onStepState(text, state == null ? "run" : state);
    }

    /** 迷你浮窗开关（hh_config/mini_chip，默认开）。Activity 不在时由这里自己判。 */
    private static boolean miniChipOn() {
        try {
            Context c = appCtx;
            if (c == null) {
                return false;
            }
            return !"0".equals(c.getSharedPreferences(PREF, Context.MODE_PRIVATE)
                    .getString("mini_chip", "1"));
        } catch (Throwable ignored) {
            return false;
        }
    }

    /** 模型推理逐块内容，实时日志页"模型推理"区恒显。
     *  kind: 'think'(思维链) / 'content'(正文) / 'meta'(标记行)；null 视作 content。 */
    public static void emitReasoning(String text, String kind) {
        Listener l = listener;
        if (l != null && text != null) {
            l.onReasoning(text, kind == null ? "content" : kind);
        }
    }

    public static void setResult(String json) {
        Listener l = listener;
        if (l != null) {
            l.onResult(json == null ? "null" : json);
        }
    }

    // ------------------------------------------------------------------ 配置

    public static String getConfig(String key) {
        if (key == null) {
            return "";
        }
        SharedPreferences sp = ctx().getSharedPreferences(PREF, Context.MODE_PRIVATE);
        String v = sp.getString(key, "");
        if (v != null && !v.isEmpty()) {
            return v;
        }
        // 内置 AMAP key（轻度加密：XOR 密钥 + Base64），无 SP 配置时兜底
        if ("amap_key".equals(key)) {
            return builtinAmapKey();
        }
        return "";
    }

    /** 轻度加密：key XOR 掩码 → Base64。运行时反解后返回明文。 */
    private static String builtinAmapKey() {
        final byte[] XK = "hh-amap-v1".getBytes();
        try {
            byte[] enc = android.util.Base64.decode("C1obUAgEFh4VB1hYFFdaB0VOQgAKDR5VWVARSBNSXlw=", 0);
            byte[] dec = new byte[enc.length];
            for (int i = 0; i < enc.length; i++) {
                dec[i] = (byte) (enc[i] ^ XK[i % XK.length]);
            }
            return new String(dec, "UTF-8");
        } catch (Exception e) {
            return "";
        }
    }

    public static void setConfig(String key, String value) {
        if (key == null) {
            return;
        }
        SharedPreferences sp = ctx().getSharedPreferences(PREF, Context.MODE_PRIVATE);
        sp.edit().putString(key, value == null ? "" : value).apply();
    }

    // ------------------------------------------------------------------ 设备标识

    /**
     * 设备标识（用于日志与断点续跑 resume key）。
     *
     * <p>2026-10-14 修复：runner.py / selftest.py 曾硬编码 {@code '476cf991'}，导致 MI 5X
     * (0043d38b0504) 上产生的日志与 resume 状态都被标成另一台机器，跨机统计完全串台。
     *
     * <p>优先级：SP 覆盖（key={@code device}，运维可写死 adb 序列号）→ 序列号（API&lt;29 可读，
     * MI5X 走这条）→ {@code MODEL-DEVICE-fingerprint8}（HyperOS/API29+ 拿不到序列号时的稳定替代）。
     */
    public static String deviceKey() {
        try {
            String ov = getConfig("device");
            if (ov != null && !ov.trim().isEmpty()) {
                return sanitizeKey(ov.trim());
            }
            String serial;
            if (Build.VERSION.SDK_INT >= 26) {
                try {
                    serial = Build.getSerial();
                } catch (SecurityException e) {
                    serial = Build.SERIAL;      // 未申请 READ_PHONE_STATE 时的旧路径
                }
            } else {
                serial = Build.SERIAL;
            }
            if (serial != null && !serial.trim().isEmpty()
                    && !"unknown".equalsIgnoreCase(serial.trim())) {
                return sanitizeKey(serial.trim());
            }
            String tag = Integer.toHexString(Math.abs(String.valueOf(Build.FINGERPRINT).hashCode()));
            return sanitizeKey(Build.MODEL + "-" + Build.DEVICE + "-" + tag);
        } catch (Throwable t) {
            return "unknown-device";
        }
    }

    private static String sanitizeKey(String s) {
        return s.replaceAll("[^A-Za-z0-9._-]", "_");
    }

    // ------------------------------------------------------------------ 目录

    /** 只读数据根：assets/data 解压到 filesDir/data（幂等，版本戳变化才重解压）。 */
    public static String dataDir() {
        extractAssetsIfNeeded();
        return new File(ctx().getFilesDir(), DATA_SUBDIR).getAbsolutePath();
    }

    /** 可写产物根：filesDir/out（自动创建）。 */
    public static String outDir() {
        File f = new File(ctx().getFilesDir(), OUT_SUBDIR);
        if (!f.exists()) {
            //noinspection ResultOfMethodCallIgnored
            f.mkdirs();
        }
        return f.getAbsolutePath();
    }

    /**
     * 把 APK assets/data/** 解压到 filesDir/data/**（幂等）。
     * 判断依据：assets/data/_version 与 filesDir/data/_version 内容一致则跳过。
     *
     * @return 数据根绝对路径
     */
    public static String extractAssetsIfNeeded() {
        Context c = ctx();
        File root = new File(c.getFilesDir(), DATA_SUBDIR);
        try {
            String want = readAssetText(VERSION_ASSET);
            File stamp = new File(root, "_version");
            if (stamp.exists() && want != null && want.equals(readFileText(stamp))) {
                return root.getAbsolutePath();
            }
            if (root.exists()) {
                deleteRecursive(root);
            }
            //noinspection ResultOfMethodCallIgnored
            root.mkdirs();
            copyAssetDir(ASSET_DATA_ROOT, root);
            if (want != null) {
                writeFileText(stamp, want);
            }
        } catch (Exception e) {
            emitLog("[PyBridge] extractAssetsIfNeeded 失败: " + e);
        }
        return root.getAbsolutePath();
    }

    private static void copyAssetDir(String assetPath, File dst) throws Exception {
        Context c = ctx();
        String[] children = c.getAssets().list(assetPath);
        if (children == null || children.length == 0) {
            // 叶子：拷贝文件
            InputStream in = c.getAssets().open(assetPath);
            File out = new File(dst.getParentFile(), dst.getName());
            try (OutputStream os = new FileOutputStream(out)) {
                byte[] buf = new byte[8192];
                int n;
                while ((n = in.read(buf)) > 0) {
                    os.write(buf, 0, n);
                }
            } finally {
                in.close();
            }
            return;
        }
        //noinspection ResultOfMethodCallIgnored
        dst.mkdirs();
        for (String child : children) {
            copyAssetDir(assetPath + "/" + child, new File(dst, child));
        }
    }

    private static String readAssetText(String assetPath) {
        try (InputStream in = ctx().getAssets().open(assetPath)) {
            return readStream(in);
        } catch (Exception e) {
            return null;
        }
    }

    private static String readFileText(File f) {
        try (InputStream in = new java.io.FileInputStream(f)) {
            return readStream(in);
        } catch (Exception e) {
            return null;
        }
    }

    private static String readStream(InputStream in) throws Exception {
        java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
        byte[] buf = new byte[4096];
        int n;
        while ((n = in.read(buf)) > 0) {
            bos.write(buf, 0, n);
        }
        return new String(bos.toByteArray(), "UTF-8");
    }

    private static void writeFileText(File f, String s) throws Exception {
        try (OutputStream os = new FileOutputStream(f)) {
            os.write(s.getBytes("UTF-8"));
        }
    }

    private static void deleteRecursive(File f) {
        if (f == null || !f.exists()) {
            return;
        }
        if (f.isDirectory()) {
            File[] kids = f.listFiles();
            if (kids != null) {
                for (File k : kids) {
                    deleteRecursive(k);
                }
            }
        }
        //noinspection ResultOfMethodCallIgnored
        f.delete();
    }

    // ------------------------------------------------------------------ JSON 树 -> uiautomator XML

    private static String toXml(JSONObject root) {
        StringBuilder sb = new StringBuilder(8192);
        sb.append("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n");
        sb.append("<hierarchy rotation=\"").append(root.optInt("rotation", 0)).append("\">\n");
        appendNode(sb, root, 1);
        sb.append("</hierarchy>\n");
        return sb.toString();
    }

    private static void appendNode(StringBuilder sb, JSONObject n, int depth) {
        for (int i = 0; i < depth; i++) {
            sb.append("  ");
        }
        sb.append("<node")
                .append(" index=\"").append(n.optInt("index", 0)).append("\"")
                .append(" text=\"").append(esc(n.optString("text", ""))).append("\"")
                .append(" resource-id=\"").append(esc(n.optString("resource-id", ""))).append("\"")
                .append(" class=\"").append(esc(n.optString("class", ""))).append("\"")
                .append(" package=\"").append(esc(n.optString("package", ""))).append("\"")
                .append(" content-desc=\"").append(esc(n.optString("content-desc", ""))).append("\"")
                .append(" checkable=\"").append(n.optBoolean("checkable")).append("\"")
                .append(" checked=\"").append(n.optBoolean("checked")).append("\"")
                .append(" clickable=\"").append(n.optBoolean("clickable")).append("\"")
                .append(" enabled=\"").append(n.optBoolean("enabled")).append("\"")
                .append(" focusable=\"").append(n.optBoolean("focusable")).append("\"")
                .append(" focused=\"").append(n.optBoolean("focused")).append("\"")
                .append(" scrollable=\"").append(n.optBoolean("scrollable")).append("\"")
                .append(" long-clickable=\"").append(n.optBoolean("long-clickable")).append("\"")
                .append(" password=\"").append(n.optBoolean("password")).append("\"")
                .append(" selected=\"").append(n.optBoolean("selected")).append("\"")
                .append(" bounds=\"").append(esc(n.optString("bounds", "[0,0][0,0]"))).append("\"");
        JSONArray kids = n.optJSONArray("node");
        if (kids == null || kids.length() == 0) {
            sb.append(" />\n");
            return;
        }
        sb.append(">\n");
        for (int i = 0; i < kids.length(); i++) {
            JSONObject c = kids.optJSONObject(i);
            if (c != null) {
                appendNode(sb, c, depth + 1);
            }
        }
        for (int i = 0; i < depth; i++) {
            sb.append("  ");
        }
        sb.append("</node>\n");
    }

    private static String esc(String s) {
        if (s == null) {
            return "";
        }
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\"", "&quot;");
    }
}
