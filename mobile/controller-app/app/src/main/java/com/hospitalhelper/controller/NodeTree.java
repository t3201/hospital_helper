package com.hospitalhelper.controller;

import android.graphics.Rect;
import android.view.accessibility.AccessibilityNodeInfo;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * AccessibilityNodeInfo → 与 uiautomator dump 同构的 JSON 节点树。
 *
 * <p>字段名刻意与 uiautomator XML 的 attribute 对齐：index / text / resource-id / class /
 * package / content-desc / checkable / checked / clickable / enabled / focusable / focused /
 * scrollable / long-clickable / password / selected / bounds。子节点放在 "node"（数组）。
 *
 * <p>顶层对象额外带 "rotation"（uiautomator 的 hierarchy rotation）。
 */
public final class NodeTree {

    /** 递归深度上限，防止 WebView 超深树把内存撑爆。 */
    public static final int DEFAULT_MAX_DEPTH = 60;

    private NodeTree() {
    }

    /** dump 顶层：root 节点对象 + rotation。root 为 null 时返回 null。 */
    public static JSONObject dump(AccessibilityNodeInfo root, int rotation) {
        if (root == null) {
            return null;
        }
        JSONObject o = snapshot(root, 0, 0, DEFAULT_MAX_DEPTH);
        try {
            o.put("rotation", rotation);
        } catch (Exception ignored) {
        }
        return o;
    }

    /** 单节点快照。index = 在父节点中的序号（与 uiautomator 一致）。 */
    public static JSONObject snapshot(AccessibilityNodeInfo n, int index, int depth, int maxDepth) {
        JSONObject o = new JSONObject();
        if (n == null) {
            return o;
        }
        Rect r = new Rect();
        try {
            n.getBoundsInScreen(r);
        } catch (Exception ignored) {
        }
        try {
            o.put("index", index);
            o.put("text", s(n.getText()));
            o.put("resource-id", n.getViewIdResourceName() == null ? "" : n.getViewIdResourceName());
            o.put("class", s(n.getClassName()));
            o.put("package", s(n.getPackageName()));
            o.put("content-desc", s(n.getContentDescription()));
            o.put("checkable", n.isCheckable());
            o.put("checked", n.isChecked());
            o.put("clickable", n.isClickable());
            o.put("enabled", n.isEnabled());
            o.put("focusable", n.isFocusable());
            o.put("focused", n.isFocused());
            o.put("scrollable", n.isScrollable());
            o.put("long-clickable", n.isLongClickable());
            o.put("password", n.isPassword());
            o.put("selected", n.isSelected());
            o.put("bounds", "[" + r.left + "," + r.top + "][" + r.right + "," + r.bottom + "]");
        } catch (Exception ignored) {
        }

        if (depth < maxDepth) {
            JSONArray kids = new JSONArray();
            int cc = 0;
            try {
                cc = n.getChildCount();
            } catch (Exception ignored) {
            }
            for (int i = 0; i < cc; i++) {
                AccessibilityNodeInfo c = null;
                try {
                    c = n.getChild(i);
                } catch (Exception ignored) {
                }
                if (c == null) {
                    continue;
                }
                kids.put(snapshot(c, i, depth + 1, maxDepth));
            }
            if (kids.length() > 0) {
                try {
                    o.put("node", kids);
                } catch (Exception ignored) {
                }
            }
        }
        return o;
    }

    /** 递归统计节点总数。 */
    public static int count(JSONObject node) {
        if (node == null) {
            return 0;
        }
        int n = 1;
        JSONArray kids = node.optJSONArray("node");
        if (kids != null) {
            for (int i = 0; i < kids.length(); i++) {
                JSONObject c = kids.optJSONObject(i);
                if (c != null) {
                    n += count(c);
                }
            }
        }
        return n;
    }

    /** 递归在 text / content-desc 里做包含匹配。 */
    public static boolean containsText(JSONObject node, String text) {
        if (node == null || text == null || text.isEmpty()) {
            return false;
        }
        String t = node.optString("text", "");
        String d = node.optString("content-desc", "");
        if (t.contains(text) || d.contains(text)) {
            return true;
        }
        JSONArray kids = node.optJSONArray("node");
        if (kids != null) {
            for (int i = 0; i < kids.length(); i++) {
                JSONObject c = kids.optJSONObject(i);
                if (c != null && containsText(c, text)) {
                    return true;
                }
            }
        }
        return false;
    }

    /** 扁平化（DFS 顺序），便于外部遍历/调试。 */
    public static List<JSONObject> flatten(JSONObject root) {
        List<JSONObject> out = new ArrayList<>();
        collect(root, out);
        return out;
    }

    private static void collect(JSONObject node, List<JSONObject> out) {
        if (node == null) {
            return;
        }
        out.add(node);
        JSONArray kids = node.optJSONArray("node");
        if (kids != null) {
            for (int i = 0; i < kids.length(); i++) {
                collect(kids.optJSONObject(i), out);
            }
        }
    }

    /** 安全格式化（缩进）；失败退回紧凑串。 */
    public static String pretty(JSONObject o) {
        if (o == null) {
            return "null";
        }
        try {
            return o.toString(2);
        } catch (Exception e) {
            return o.toString();
        }
    }

    private static String s(CharSequence cs) {
        return cs == null ? "" : cs.toString();
    }
}
