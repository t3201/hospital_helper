package com.hospitalhelper.controller;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.graphics.PixelFormat;
import android.os.Build;
import android.os.IBinder;
import android.provider.Settings;
import android.util.Log;
import android.view.Gravity;
import android.view.WindowManager;
import android.widget.FrameLayout;

/**
 * 前台服务兜底（约束#7）。
 *
 * <p>Redmi/HyperOS 等国产 ROM 对“后台启动 Activity”限制严格：从控制器 App 唤起随申办后，
 * 若控制器进程被回收，后续读屏/点击会断。跑任务时挂一个前台服务把进程拉成前台优先级。
 *
 * <p>这不是必须路径：短任务（P0–P2 哑回放）不挂也能跑；长流水线（P3）建议挂上。
 */
public class ForegroundKeeperService extends Service {

    private static final String CHANNEL = "hh_keeper";
    private static final int NOTIF_ID = 0x4848;

    /**
     * 防冻结小窗（HyperOS/MIUI SmartPower 实测会把不可见进程挂起 2~5 分钟，
     * 表现为 dump/日志整段时间完全静止，直到读屏服务被解绑才醒）。
     * 挂一个 2x2 透明不可触摸的 TYPE_APPLICATION_OVERLAY，让进程保持「可见」adj。
     */
    private FrameLayout overlay;

    public static void start(Context c) {
        try {
            Intent it = new Intent(c, ForegroundKeeperService.class);
            if (Build.VERSION.SDK_INT >= 26) {
                c.startForegroundService(it);
            } else {
                c.startService(it);
            }
        } catch (Exception ignored) {
        }
    }

    public static void stop(Context c) {
        try {
            c.stopService(new Intent(c, ForegroundKeeperService.class));
        } catch (Exception ignored) {
        }
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL, "控制器后台保活", NotificationManager.IMPORTANCE_LOW);
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null) {
                nm.createNotificationChannel(ch);
            }
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        try {
            Notification.Builder b = (Build.VERSION.SDK_INT >= 26)
                    ? new Notification.Builder(this, CHANNEL)
                    : new Notification.Builder(this);
            b.setSmallIcon(android.R.drawable.stat_sys_download)
                    .setContentTitle(getString(R.string.app_name))
                    .setContentText("正在运行设备控制任务…")
                    .setOngoing(true);
            startForeground(NOTIF_ID, b.build());
        } catch (Exception ignored) {
        }
        ensureOverlay();
        return START_STICKY;
    }

    private void ensureOverlay() {
        if (overlay != null) return;
        try {
            // HyperOS 的 Settings.canDrawOverlays 会被 MIUI 自己的悬浮窗开关劫持（appop 已 allow 仍返回 false），
            // 因此只记录状态，真正能否 addView 交给 WindowManager（appop SYSTEM_ALERT_WINDOW=allow 即可）。
            if (Build.VERSION.SDK_INT >= 23 && !Settings.canDrawOverlays(this)) {
                Log.i("HHDIAG", "keeper overlay: canDrawOverlays=false，仍按 appop 尝试 addView");
            }
            WindowManager wm = (WindowManager) getSystemService(WINDOW_SERVICE);
            if (wm == null) return;
            FrameLayout v = new FrameLayout(this);
            v.setBackgroundColor(0x00000000);
            int type = (Build.VERSION.SDK_INT >= 26)
                    ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                    : WindowManager.LayoutParams.TYPE_SYSTEM_ALERT;
            WindowManager.LayoutParams lp = new WindowManager.LayoutParams(
                    2, 2, type,
                    WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                            | WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                            | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN
                            | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                    PixelFormat.TRANSLUCENT);
            lp.gravity = Gravity.TOP | Gravity.START;
            lp.x = 0;
            lp.y = 0;
            wm.addView(v, lp);
            overlay = v;
            Log.i("HHDIAG", "keeper overlay added (anti-freeze)");
        } catch (Throwable t) {
            Log.w("HHDIAG", "keeper overlay fail: " + t.getClass().getSimpleName() + ": " + t.getMessage());
        }
    }

    private void releaseOverlay() {
        try {
            if (overlay != null) {
                ((WindowManager) getSystemService(WINDOW_SERVICE)).removeView(overlay);
                overlay = null;
            }
        } catch (Throwable ignored) {
        }
    }

    @Override
    public void onDestroy() {
        releaseOverlay();
        super.onDestroy();
    }
}
