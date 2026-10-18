package com.hospitalhelper.controller;

import android.accessibilityservice.AccessibilityService;
import android.content.Intent;
import android.util.Log;
import android.view.accessibility.AccessibilityEvent;

/**
 * 读屏桥服务本体。持有 {@link AccDeviceBridge}，并作为事件源：
 * 每次收到 WINDOW_STATE_CHANGED / WINDOW_CONTENT_CHANGED 就唤醒等待方
 * （{@link AccDeviceBridge#waitForText} 的事件驱动部分）。
 *
 * <p>用户在「系统设置 → 无障碍」里一次性授权后，onServiceConnected 被调用，
 * 静态 {@link #get()} 才会非空。控制器 Activity 用它来判断服务是否可用。
 */
public class SsAccessibilityService extends AccessibilityService {

    /** 事件等待锁：AccDeviceBridge 与控制器共享。 */
    private static final Object EVENT_LOCK = new Object();

    private static volatile SsAccessibilityService instance;

    /** 与系统的绑定状态：onServiceConnected=true，onUnbind/onDestroy=false（MIUI 会反复解绑/重绑）。 */
    private volatile boolean bound = false;

    private AccDeviceBridge bridge;

    /** 供 {@link AccDeviceBridge} 在 binder 调用前判断：未绑定直接返回空，不去等一个必然阻塞的调用。 */
    public boolean isBound() {
        return bound;
    }

    /** 供控制器判断授权状态。 */
    public static SsAccessibilityService get() {
        return instance;
    }

    public static boolean isReady() {
        return instance != null;
    }

    /** 事件等待锁（waitForText 用）。 */
    public static Object eventLock() {
        return EVENT_LOCK;
    }

    /** 便捷取用设备桥；未授权返回 null。 */
    public static DeviceBridge bridge() {
        SsAccessibilityService s = instance;
        return s == null ? null : s.bridge;
    }

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        bridge = new AccDeviceBridge(this);
        bound = true;
        instance = this;
        Log.i("HHDIAG", "acc onServiceConnected: bound=true");
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        // 约束#8：事件驱动。任何窗口状态/内容变化都唤醒等待方。
        synchronized (EVENT_LOCK) {
            EVENT_LOCK.notifyAll();
        }
        // 迷你浮窗"自行躲开"：观测到随申办区域有点击落点、且落点压在胶囊上 -> 翻角。
        // 胶囊本身不可触摸（永不拦截真实点击），这里只是让它别挡住用户视线/操作区。
        if (event != null && event.getEventType() == AccessibilityEvent.TYPE_VIEW_CLICKED) {
            android.view.accessibility.AccessibilityNodeInfo src = null;
            try {
                src = event.getSource();
            } catch (Throwable ignored) {
            }
            if (src != null) {
                android.graphics.Rect b = new android.graphics.Rect();
                src.getBoundsInScreen(b);
                MiniChip.onPossibleClick(b);
            }
        }
    }

    @Override
    public void onInterrupt() {
        // no-op
    }

    @Override
    public boolean onUnbind(Intent intent) {
        bound = false;
        if (instance == this) {
            instance = null;
            bridge = null;
        }
        Log.i("HHDIAG", "acc onUnbind: bound=false");
        return super.onUnbind(intent);
    }

    @Override
    public void onDestroy() {
        bound = false;
        if (instance == this) {
            instance = null;
        }
        bridge = null;
        Log.i("HHDIAG", "acc onDestroy: bound=false");
        super.onDestroy();
    }
}
