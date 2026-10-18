package com.hospitalhelper.controller;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.util.Log;

import java.io.File;
import java.io.FileInputStream;
import java.io.ByteArrayOutputStream;
import java.nio.charset.Charset;

/**
 * SelfTestReceiver —— debug 专用触发通道（仅 {@code BuildConfig.DEBUG} 才注册/导出）。
 *
 * <p>自动化触发（release 里这个接收器<b>根本不存在</b>，不会暴露）：
 * <pre>
 *   # 用内置固定条件（SH-084 眼科/眼科门诊/专家门诊）：
 *   adb -s 476cf991 shell am broadcast -a com.hospitalhelper.controller.SELF_TEST \
 *       -n com.hospitalhelper.controller/.SelfTestReceiver
 *
 *   # 带自定义 manifest（JSON；--es 传值，注意 shell 引号转义）：
 *   adb -s 476cf991 shell am broadcast -a com.hospitalhelper.controller.SELF_TEST \
 *       -n com.hospitalhelper.controller/.SelfTestReceiver \
 *       --es manifest "{\"conditions\":[{\"hospital\":\"SH-084\",\"tab\":\"眼科\",\"dept\":\"眼科门诊\",\"slot_type\":\"expert\"}]}"
 *
 *   # 推荐：manifest 走文件（Android 的 mksh + adb 会把 JSON 里的引号/逗号/CJK 搅碎，
 *   # 2026-10-15 实测 --es 传 JSON 必坏 => 改成先 push 文件再广播路径）：
 *   adb -s 476cf991 push manifest.json /sdcard/Android/data/com.hospitalhelper.controller/files/selftest_manifest.json
 *   adb -s 476cf991 shell am broadcast -a com.hospitalhelper.controller.SELF_TEST \
 *       -n com.hospitalhelper.controller/.SelfTestReceiver \
 *       --es manifest_file /sdcard/Android/data/com.hospitalhelper.controller/files/selftest_manifest.json
 *
 *   # 最稳（实测可用）：扁平形式，无空格/引号(分隔符用逗号, 不能用分号), CJK 用 &#92;uXXXX（本类负责解码并拼 JSON）：
 *   adb -s 476cf991 shell am broadcast -a com.hospitalhelper.controller.SELF_TEST \
 *       -n com.hospitalhelper.controller/.SelfTestReceiver \
 *       --es manifest mode=probe,hospital=SH-084,tab=&#92;u773c&#92;u79d1,dept=&#92;u773c&#92;u79d1&#92;u95e8&#92;u8bca,slot_type=normal
 * </pre>
 *
 * <p>action = {@value #ACTION}；extra {@code "manifest"} = JSON manifest 字符串，
 * 或上面那种扁平 {@code k=v,k=v} 形式(逗号安全, 分号会被 sh 切断)（不以 '{' 开头即按扁平解析）；
 * extra {@code "manifest_file"} = 磁盘上的 JSON manifest 路径（优先读文件，读不到再退回 extra）。
 * 触发后 {@link SelfTest#run} 立即返回（异步），结果看
 * {@code adb logcat -s HH_SELFTEST} 与 {@code files/out/selftest_last.json}。
 *
 * <p>安全：只读抓取，不进入任何下单路径（见 {@link SelfTest}）。
 */
public final class SelfTestReceiver extends BroadcastReceiver {

    public static final String ACTION = "com.hospitalhelper.controller.SELF_TEST";
    public static final String EXTRA_MANIFEST = "manifest";
    public static final String EXTRA_MANIFEST_FILE = "manifest_file";

    @Override
    public void onReceive(Context context, Intent intent) {
        Context app = context.getApplicationContext();
        if (!BuildConfig.DEBUG) {
            // 双保险：即便被误注册也只跑 debug 构建。
            Log.w(SelfTest.TAG, "[recv] 非 DEBUG 构建，忽略自检广播");
            return;
        }
        if (intent == null || !ACTION.equals(intent.getAction())) {
            return;
        }
        String manifest = intent.hasExtra(EXTRA_MANIFEST) ? intent.getStringExtra(EXTRA_MANIFEST) : null;
        String file = intent.hasExtra(EXTRA_MANIFEST_FILE) ? intent.getStringExtra(EXTRA_MANIFEST_FILE) : null;
        String src = "extra";
        if (file != null && !file.trim().isEmpty()) {
            String fromFile = readFile(file.trim());
            if (fromFile != null && !fromFile.trim().isEmpty()) {
                manifest = fromFile;
                src = "file:" + file.trim();
            } else {
                Log.w(SelfTest.TAG, "[recv] manifest_file 读不到: " + file.trim() + "（退回 extra/内置条件）");
            }
        }
        // 扁平形式（shell 安全）-> JSON：mode=probe;hospital=SH-084;tab=\u773c\u79d1;...
        if (manifest != null && !manifest.trim().isEmpty() && !manifest.trim().startsWith("{")) {
            String json = flatToJson(manifest);
            if (json != null) {
                manifest = json;
                src = "flat";
            }
        }
        if (manifest != null && !manifest.isEmpty()) {
            SelfTest.rememberManifest(app, manifest);
            Log.i(SelfTest.TAG, "[recv] 收到 SELF_TEST 广播（manifest 来源=" + src + "）");
        } else {
            Log.i(SelfTest.TAG, "[recv] 收到 SELF_TEST 广播（用内置固定条件）");
        }
        SelfTest.run(app, manifest);
    }

    /**
     * 扁平 k=v;k=v -> JSON manifest。
     *
     * <p>为什么要这一层: Android 的 mksh + adb 在 Windows 侧会把 JSON 里的引号吃掉,
     * 逗号处断词, CJK 字节还会乱码 (2026-10-15 实测 --es 传 JSON 必坏). 扁平形式
     * 没有空格/引号/逗号, 中文写成 {@code &#92;uXXXX}, 到这里再还原成真正的 JSON.
     */
    static String flatToJson(String flat) {
        try {
            org.json.JSONObject cond = new org.json.JSONObject();
            org.json.JSONObject top = new org.json.JSONObject();
            top.put("source", "selftest_flat");
            for (String kv : flat.split("[,;]")) {
                if (kv.trim().isEmpty()) {
                    continue;
                }
                int eq = kv.indexOf('=');
                if (eq <= 0) {
                    continue;
                }
                String k = unescape(kv.substring(0, eq).trim());
                String v = unescape(kv.substring(eq + 1).trim());
                if ("mode".equals(k)) {
                    top.put(k, v);
                } else if ("limit".equals(k)) {
                    top.put(k, Integer.parseInt(v));
                } else if ("resume".equals(k)) {
                    top.put(k, Boolean.parseBoolean(v));
                } else if ("hospital".equals(k) || "tab".equals(k) || "dept".equals(k)
                        || "slot_type".equals(k) || "date".equals(k)) {
                    cond.put(k, v);
                } else {
                    Log.w(SelfTest.TAG, "[recv] 忽略未知 manifest 键: " + k);
                }
            }
            if (cond.length() > 0) {
                org.json.JSONArray arr = new org.json.JSONArray();
                arr.put(cond);
                top.put("conditions", arr);
            }
            return top.toString();
        } catch (Exception e) {
            Log.w(SelfTest.TAG, "[recv] 扁平 manifest 解析失败: " + e);
            return null;
        }
    }

    /** 还原 {@code &#92;uXXXX}（shell 里传中文的唯一可靠写法）。 */
    static String unescape(String s) {
        if (s == null || s.indexOf('\\') < 0) {
            return s;
        }
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '\\' && i + 5 < s.length() && s.charAt(i + 1) == 'u') {
                try {
                    sb.append((char) Integer.parseInt(s.substring(i + 2, i + 6), 16));
                    i += 5;
                    continue;
                } catch (NumberFormatException ignored) {
                }
            }
            sb.append(c);
        }
        return sb.toString();
    }

    /** 读 UTF-8 文本（debug 通道，路径由 adb 指定；读不到返回 null，不抛）。 */
    private static String readFile(String path) {
        try {
            File f = new File(path);
            if (!f.isFile() || !f.canRead()) {
                return null;
            }
            FileInputStream in = null;
            try {
                in = new FileInputStream(f);
                ByteArrayOutputStream bos = new ByteArrayOutputStream();
                byte[] buf = new byte[8192];
                int n;
                while ((n = in.read(buf)) > 0) {
                    bos.write(buf, 0, n);
                }
                return new String(bos.toByteArray(), Charset.forName("UTF-8"));
            } finally {
                if (in != null) {
                    try {
                        in.close();
                    } catch (Exception ignored) {
                    }
                }
            }
        } catch (Exception e) {
            Log.w(SelfTest.TAG, "[recv] manifest_file 读取异常: " + e);
            return null;
        }
    }
}
