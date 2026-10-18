package com.hospitalhelper.controller;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.UriMatcher;
import android.database.Cursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;

import java.io.File;
import java.io.FileNotFoundException;

/**
 * ApkProvider —— 把 filesDir/updates/*.apk 以 content:// 暴露给系统安装器。
 *
 * <p>为什么自研而不用 androidx FileProvider：本 App 约束#2「零第三方依赖」
 * （见 build.gradle，只有 Chaquopy）。这里只实现安装场景需要的 openFile()。
 *
 * <p>安全：只允许读 updates/ 目录下、文件名匹配 {@code [A-Za-z0-9._+-]+\.apk} 的文件；
 * exported=false + 一次性 URI 读授权（FLAG_GRANT_READ_URI_PERMISSION）。
 */
public class ApkProvider extends ContentProvider {

    public static final String AUTHORITY = "com.hospitalhelper.controller.apkprovider";

    private static final int CODE_APK = 1;
    private static final UriMatcher MATCHER = new UriMatcher(UriMatcher.NO_MATCH);

    static {
        // 单段路径（文件名）；真正的白名单校验在 openFile 里做正则，UriMatcher 只做形状匹配
        MATCHER.addURI(AUTHORITY, "*", CODE_APK);
    }

    /** 由文件得到 content:// URI（文件名白名单校验在 openFile 里再做一次）。 */
    public static Uri uriFor(File f) {
        return Uri.parse("content://" + AUTHORITY + "/" + f.getName());
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        if (MATCHER.match(uri) != CODE_APK) {
            throw new FileNotFoundException("不支持的 URI: " + uri);
        }
        String name = uri.getLastPathSegment();
        if (name == null || !name.matches("[A-Za-z0-9._+-]+\\.apk")) {
            throw new FileNotFoundException("非法文件名: " + name);
        }
        File dir = new File(getContext().getFilesDir(), UpdateChecker.APK_DIR);
        File f = new File(dir, name);
        if (!f.isFile()) {
            throw new FileNotFoundException("文件不存在: " + name);
        }
        return ParcelFileDescriptor.open(f, ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection,
                        String[] selectionArgs, String sortOrder) {
        return null;
    }

    @Override
    public String getType(Uri uri) {
        return "application/vnd.android.package-archive";
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        return 0;
    }
}
