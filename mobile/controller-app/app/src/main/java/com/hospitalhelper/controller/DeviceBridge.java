package com.hospitalhelper.controller;

import android.graphics.Rect;
import android.view.accessibility.AccessibilityNodeInfo;

import org.json.JSONObject;

/**
 * 设备桥：把旧 adb 原语（tap / swipe / back / input / dump / launch …）抽象为端上可调用接口。
 *
 * <p>唯一实现 = {@link AccDeviceBridge}（基于 AccessibilityService）。P0–P2 只落地设备原语，
 * 业务流程（9 段流水线）在 {@link TaskEngine}，P3 再接。
 *
 * <p>与旧 Python 侧的对应关系（见 docs/MOBILE_ANDROID_PLAN.md §2）：
 * <pre>
 *   launch(pkg)          &lt;- monkey -p PKG -c LAUNCHER 1（force-stop 无对等，用 CLEAR_TOP 等效重启）
 *   dump()               &lt;- uiautomator dump（输出与 XML 同构的节点 JSON）
 *   findNode(...)        &lt;- 旧代码里的正则/keyword 定位（改为结构化节点定位）
 *   click(node|x,y)      &lt;- input tap x y（优先 ACTION_CLICK，兜底 dispatchGesture）
 *   tap(x,y)             &lt;- input tap x y（纯坐标）
 *   swipe(...)           &lt;- input swipe x1 y1 x2 y2 dur
 *   back()               &lt;- input keyevent 4
 *   inputText(node,text) &lt;- input_cjk（改为 ACTION_SET_TEXT，中英文通吃）
 *   pressEnter(node)     &lt;- 无 GLOBAL_ACTION_ENTER，改用 EDITOR_ACTION / 点“搜索”节点
 *   waitForText(...)     &lt;- 事件驱动 + 轮询兜底 + 超时
 *   screenSize()         &lt;- wm size
 *   now()                &lt;- shell date
 * </pre>
 */
public interface DeviceBridge {

    /** 目标被操作 App：随申办（上海挂号）。 */
    String TARGET_PKG = "mobi.w3studio.apps.android.shsmy.phone";

    // ------------------------------------------------------------------ 设备原语

    /** 唤起 App（等价 monkey -p PKG -c LAUNCHER 1）。CLEAR_TOP 等效 force-stop 重启。 */
    boolean launch(String pkg);

    /**
     * 读屏：返回与 uiautomator dump 同构的节点 JSON。
     * 字段含 text / content-desc / class / bounds / clickable / scrollable / package …，children 放 "node"。
     * 取不到随申办窗口时返回 {@code null}（调用方须如实报错，不得当作空页面）。
     */
    JSONObject dump();

    /**
     * 按 text / desc / class / Rect 定位节点（所有非空条件取交集）。
     * 多个命中时：给了 near 取离 near 最近的；否则取面积最小的（更精确的叶子）。
     * 全部条件为空时返回 null。
     */
    NodeRef findNode(String text, String desc, String cls, Rect near);

    /** 点击节点：优先向上找 clickable 祖先 performAction(ACTION_CLICK)；否则按 bounds 中心 dispatchGesture。 */
    boolean click(NodeRef node);

    /** 坐标点击（等价 input tap x y）。 */
    boolean click(int x, int y);

    /** 坐标点按（底层 dispatchGesture 单点，dur=50ms）。 */
    boolean tap(int x, int y);

    /** 滑动（等价 input swipe，dispatchGesture Path + StrokeDescription）。 */
    boolean swipe(int x1, int y1, int x2, int y2, long durationMs);

    /** 返回（等价 input keyevent 4）→ performGlobalAction(GLOBAL_ACTION_BACK)。 */
    boolean back();

    /** 输入文字（ACTION_SET_TEXT + ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE，中英文通吃，不依赖 ADBKeyboard）。 */
    boolean inputText(NodeRef node, String text);

    /** 回车/搜索（API27 无 GLOBAL_ACTION_ENTER）：对焦点 EditText 发 EDITOR_ACTION，或点“搜索”节点。 */
    boolean pressEnter(NodeRef node);

    /** 事件驱动（WINDOW_STATE/CONTENT_CHANGED）+ postDelayed 轮询兜底 + 超时：等待页面上出现 text。 */
    boolean waitForText(String text, long timeoutMs);

    /** 屏幕尺寸（等价 wm size），返回 [w, h]。 */
    int[] screenSize();

    /** 设备时间毫秒（等价 shell date）。 */
    long now();

    // ------------------------------------------------------------------ 节点引用

    /**
     * 一次读屏里命中的节点引用。{@code aw} 为底层 AccessibilityNodeInfo（纯坐标引用时可为 null）。
     */
    final class NodeRef {
        public final AccessibilityNodeInfo aw;
        public final String text;
        public final String desc;
        public final String cls;
        public final Rect bounds;

        public NodeRef(AccessibilityNodeInfo aw, String text, String desc, String cls, Rect bounds) {
            this.aw = aw;
            this.text = text;
            this.desc = desc;
            this.cls = cls;
            this.bounds = bounds;
        }

        public boolean clickable() {
            return aw != null && aw.isClickable();
        }

        public int cx() {
            return bounds != null ? bounds.centerX() : -1;
        }

        public int cy() {
            return bounds != null ? bounds.centerY() : -1;
        }

        public int width() {
            return bounds != null ? bounds.width() : 0;
        }

        public int height() {
            return bounds != null ? bounds.height() : 0;
        }

        @Override
        public String toString() {
            return "NodeRef{text=" + text + ", desc=" + desc + ", cls=" + cls + ", bounds=" + bounds + "}";
        }
    }
}
