package com.hospitalhelper.controller;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * 历史任务存储。P0–P2 用 SharedPreferences 存 JSON 数组（零依赖、够用）。
 * P3 若任务体量变大再换文件/DB。
 *
 * <p>每条任务建议字段：{@code id, ts, title, symptom, result, device}（自由扩展）。
 */
public final class HistoryStore {

    private static final String PREF = "hh_history";
    private static final String KEY = "tasks";
    private static final int MAX = 50;

    private HistoryStore() {
    }

    /** 新增一条（置顶）。 */
    public static synchronized void save(Context c, JSONObject task) {
        JSONArray old = load(c);
        JSONArray out = new JSONArray();
        out.put(task);
        for (int i = 0; i < old.length() && out.length() < MAX; i++) {
            JSONObject o = old.optJSONObject(i);
            if (o != null) {
                out.put(o);
            }
        }
        prefs(c).edit().putString(KEY, out.toString()).apply();
    }

    /** 全部（新在前）。 */
    public static synchronized JSONArray load(Context c) {
        String s = prefs(c).getString(KEY, "[]");
        try {
            return new JSONArray(s);
        } catch (Exception e) {
            return new JSONArray();
        }
    }

    public static List<JSONObject> list(Context c) {
        JSONArray arr = load(c);
        List<JSONObject> out = new ArrayList<>();
        for (int i = 0; i < arr.length(); i++) {
            JSONObject o = arr.optJSONObject(i);
            if (o != null) {
                out.add(o);
            }
        }
        return out;
    }

    public static JSONObject get(Context c, String id) {
        if (id == null) {
            return null;
        }
        JSONArray arr = load(c);
        for (int i = 0; i < arr.length(); i++) {
            JSONObject o = arr.optJSONObject(i);
            if (o != null && id.equals(o.optString("id"))) {
                return o;
            }
        }
        return null;
    }

    public static void clear(Context c) {
        prefs(c).edit().remove(KEY).apply();
    }

    private static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences(PREF, Context.MODE_PRIVATE);
    }
}
