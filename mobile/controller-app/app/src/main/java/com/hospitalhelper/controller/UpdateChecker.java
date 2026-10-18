package com.hospitalhelper.controller;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;

import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;

/**
 * hh-server APK 自更新客户端（GET {base}/v1/app/version → GET {base}/v1/app/download?file=…）。
 *
 * <p>2026-10-14 接口归集：APK 更新接口与 /v1/infer、/v1/location 同源于
 * {@code https://hh.ei.ac.cn:51120}。APK 由人工上传（{@code POST /v1/app/upload} 或
 * {@code python manage_apk.py add}），App 只做「检查 → 下载 → 拉起系统安装」。
 *
 * <p>约束#2：零第三方依赖 —— HttpURLConnection + org.json；下载落
 * {@code filesDir/updates/<file>}，经 {@link ApkProvider}（自研 ContentProvider，非 androidx）
 * 以 content:// 授权给系统安装器（Android 7+ 禁用 file:// URI）。
 *
 * <p>安全：下载后按服务端登记的 sha256 校验；不匹配则删除并报错，绝不安装。
 */
public final class UpdateChecker {

    public static final String PATH_VERSION = "/v1/app/version";
    public static final String PATH_DOWNLOAD = "/v1/app/download?file=";
    public static final int CONNECT_TIMEOUT_MS = 8000;
    public static final int READ_TIMEOUT_MS = 60000;
    /** 下载目录（相对 filesDir）。 */
    public static final String APK_DIR = "updates";

    private UpdateChecker() {
    }

    /** 服务端 versions.json 里的一条（public_view 的子集）。 */
    public static class Info {
        public String file = "";
        public int versionCode;
        public String versionName = "";
        public long size;
        public String sha256 = "";
        public String notes = "";
        public boolean force;
        public int minVersionCode;
        public String channel = "";
        public String publishedAt = "";
    }

    /** 一次版本检查的结果。 */
    public static class CheckResult {
        public boolean ok;
        public boolean hasUpdate;
        public boolean forceUpdate;
        public int currentVersionCode;
        public Info latest;
        public String error = "";
        public String raw = "";
    }

    public interface Progress {
        /** got/total 均为字节数；total<=0 表示服务端未给 Content-Length。 */
        void onProgress(long got, long total);
    }

    // ------------------------------------------------------------------ 检查

    /** GET /v1/app/version?current_version_code=…（同步，调用方放后台线程）。 */
    public static CheckResult check(String base, int currentVersionCode) {
        CheckResult r = new CheckResult();
        r.currentVersionCode = currentVersionCode;
        String url = AgentClient.resolveBase(base) + PATH_VERSION
                + "?current_version_code=" + currentVersionCode;
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(url).openConnection();
            conn.setRequestMethod("GET");
            conn.setConnectTimeout(CONNECT_TIMEOUT_MS);
            conn.setReadTimeout(READ_TIMEOUT_MS);
            conn.setRequestProperty("Accept", "application/json");
            int code = conn.getResponseCode();
            InputStream in = (code >= 200 && code < 300) ? conn.getInputStream() : conn.getErrorStream();
            String body = readAll(in);
            r.raw = body;
            JSONObject o;
            try {
                o = new JSONObject(body);
            } catch (Exception e) {
                r.error = "响应不是 JSON(HTTP " + code + "): " + trim(body, 200);
                return r;
            }
            if (code != 200 || !o.optBoolean("ok", false)) {
                r.error = o.optString("error", "HTTP " + code);
                return r;
            }
            r.ok = true;
            r.hasUpdate = o.optBoolean("has_update", false);
            r.forceUpdate = o.optBoolean("force_update", false);
            JSONObject l = o.optJSONObject("latest");
            if (l != null) {
                Info i = new Info();
                i.file = l.optString("file");
                i.versionCode = l.optInt("version_code");
                i.versionName = l.optString("version_name");
                i.size = l.optLong("size");
                i.sha256 = l.optString("sha256");
                i.notes = l.optString("notes");
                i.force = l.optBoolean("force");
                i.minVersionCode = l.optInt("min_version_code");
                i.channel = l.optString("channel");
                i.publishedAt = l.optString("published_at");
                r.latest = i;
            }
            return r;
        } catch (Exception e) {
            r.error = "检查更新失败: " + e.getClass().getSimpleName() + ": " + e.getMessage();
            return r;
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    // ------------------------------------------------------------------ 下载

    /**
     * 下载 APK 到 filesDir/updates/&lt;file&gt;，并按登记的 sha256 校验（同步，放后台线程）。
     * 下载地址一律用 App 当前 base 重新拼（服务端 download_url 字段写的是公网基址，
     * 本机/灰度联调时会不一致）。
     */
    public static File download(Context ctx, String base, Info info, Progress cb) throws Exception {
        String name = sanitize(info.file);
        String url = AgentClient.resolveBase(base) + PATH_DOWNLOAD + name;
        File dir = new File(ctx.getFilesDir(), APK_DIR);
        if (!dir.isDirectory() && !dir.mkdirs()) {
            throw new Exception("无法创建下载目录: " + dir.getAbsolutePath());
        }
        File out = new File(dir, name);
        File tmp = new File(dir, name + ".part");

        HttpURLConnection conn = null;
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        long total = 0L;
        try {
            conn = (HttpURLConnection) new URL(url).openConnection();
            conn.setRequestMethod("GET");
            conn.setConnectTimeout(CONNECT_TIMEOUT_MS);
            conn.setReadTimeout(READ_TIMEOUT_MS);
            int code = conn.getResponseCode();
            if (code != 200 && code != 206) {
                throw new Exception("下载失败 HTTP " + code + ": " + trim(readAll(conn.getErrorStream()), 200));
            }
            long expect = conn.getContentLengthLong();
            InputStream in = conn.getInputStream();
            OutputStream os = new FileOutputStream(tmp);
            byte[] buf = new byte[64 * 1024];
            long lastReport = 0L;
            int n;
            try {
                while ((n = in.read(buf)) > 0) {
                    os.write(buf, 0, n);
                    md.update(buf, 0, n);
                    total += n;
                    if (cb != null && (total - lastReport >= 1024 * 1024)) {
                        lastReport = total;
                        cb.onProgress(total, expect > 0 ? expect : info.size);
                    }
                }
            } finally {
                os.flush();
                os.close();
                try {
                    in.close();
                } catch (Exception ignored) {
                }
            }
            if (expect > 0 && total != expect) {
                throw new Exception("下载不完整: " + total + "/" + expect + " bytes");
            }
            String got = hex(md.digest());
            if (info.sha256 != null && info.sha256.length() > 0
                    && !info.sha256.equalsIgnoreCase(got)) {
                throw new Exception("sha256 不匹配（已丢弃）: 期望 " + info.sha256 + " 实际 " + got);
            }
            if (tmp.exists() && !tmp.renameTo(out)) {
                throw new Exception("重命名失败: " + out.getAbsolutePath());
            }
            if (cb != null) {
                cb.onProgress(total, total);
            }
            return out;
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
            if (tmp.exists()) {
                // 校验失败/异常路径：清掉半截文件
                try {
                    tmp.delete();
                } catch (Exception ignored) {
                }
            }
        }
    }

    // ------------------------------------------------------------------ 安装

    /** 是否已授予「安装未知应用」权限（O+ 才需要）。 */
    public static boolean canInstall(Context ctx) {
        if (Build.VERSION.SDK_INT < 26) {
            return true;
        }
        try {
            return ctx.getPackageManager().canRequestPackageInstalls();
        } catch (Exception e) {
            return false;
        }
    }

    /** 跳系统设置页，让用户给本 App 开「安装未知应用」。 */
    public static Intent installPermissionIntent(Context ctx) {
        Intent it = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES);
        it.setData(Uri.parse("package:" + ctx.getPackageName()));
        it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        return it;
    }

    /** 拉起系统安装器（content:// 授权，Android 7+ 不能用 file://）。 */
    public static void install(Context ctx, File apk) {
        Uri uri = ApkProvider.uriFor(apk);
        Intent it = new Intent(Intent.ACTION_VIEW);
        it.setDataAndType(uri, "application/vnd.android.package-archive");
        it.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        ctx.startActivity(it);
    }

    // ------------------------------------------------------------------ util

    static String sanitize(String name) throws Exception {
        String s = (name == null ? "" : name.trim());
        int slash = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
        if (slash >= 0) {
            s = s.substring(slash + 1);
        }
        if (!s.matches("[A-Za-z0-9._+-]+\\.apk")) {
            throw new Exception("非法 APK 文件名: " + s);
        }
        return s;
    }

    private static String readAll(InputStream in) {
        if (in == null) {
            return "";
        }
        java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream();
        try {
            byte[] buf = new byte[4096];
            int n;
            while ((n = in.read(buf)) > 0) {
                bo.write(buf, 0, n);
            }
        } catch (Exception ignored) {
        } finally {
            try {
                in.close();
            } catch (Exception ignored) {
            }
        }
        return new String(bo.toByteArray(), java.nio.charset.StandardCharsets.UTF_8);
    }

    private static String hex(byte[] bytes) {
        StringBuilder sb = new StringBuilder();
        for (byte b : bytes) {
            sb.append(String.format("%02x", b));
        }
        return sb.toString();
    }

    private static String trim(String s, int n) {
        if (s == null) {
            return "";
        }
        return s.length() <= n ? s : s.substring(0, n);
    }
}
