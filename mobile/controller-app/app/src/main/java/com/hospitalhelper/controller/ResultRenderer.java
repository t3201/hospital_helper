package com.hospitalhelper.controller;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 端上「结果」富渲染器 —— 对齐 web {@code app/web/index.html} 的 {@code renderResult(r)}。
 *
 * <p>web 用 HTML 表格呈现，端上无法直接复用，这里以「等宽字体分节文本」等价重建同一信息层级：
 * 本次输入 → 推理 → 权重向量 → 推荐（两级排序）→ 回归断言 → 显式排除 → 落盘。
 * 实时运行完成、以及从「历史」点开任意一次运行，都走这里，保证两处观感一致且都有实质内容。
 *
 * <p>入参 {@code r} = 流水线产出的 result 对象（含 spec/inference/stage1/final/checks/booking/meta）；
 * 若传入的是运行 wrapper（含 result 字段），会自动下钻。非推荐类结果（如 LLM 试跑 JSON）
 * 原样 pretty-print。
 */
public final class ResultRenderer {

    /** feature key -> 中文名（与 web FEATURE_ZH 同步）。 */
    private static final Map<String, String> FEATURE_ZH = new LinkedHashMap<>();
    static {
        FEATURE_ZH.put("match", "科室命中");
        FEATURE_ZH.put("level", "医院等级");
        FEATURE_ZH.put("breadth", "科室齐度");
        FEATURE_ZH.put("distance", "距离就近");
        FEATURE_ZH.put("metro", "地铁可达");
        FEATURE_ZH.put("bus", "公交站点");
        FEATURE_ZH.put("key_specialty", "特色科室加分");
        FEATURE_ZH.put("emergency", "急诊能力");
        FEATURE_ZH.put("chronic", "慢病专病随访");
        FEATURE_ZH.put("chronic_fit", "分级诊疗承接");
        FEATURE_ZH.put("age_fit", "人群适配(儿科能力)");
        FEATURE_ZH.put("price", "费用友好度");
    }

    private ResultRenderer() {
    }

    /** 顶层入口：给定历史条目里的 result（wrapper 或 inner），返回可读富文本。 */
    public static String render(Object resultNode) {
        if (!(resultNode instanceof JSONObject)) {
            return "（该记录无结构化结果）";
        }
        JSONObject o = (JSONObject) resultNode;
        // 下钻：wrapper -> inner result
        JSONObject r = o;
        if (o.has("result") && o.opt("result") instanceof JSONObject
                && (o.has("rc") || o.has("ok"))) {
            r = o.optJSONObject("result");
            if (r == null) {
                r = o;
            }
        }
        // 非推荐类（LLM 试跑等）：没有 final -> 直接 pretty
        boolean isRecommend = r.has("final") || r.has("inference") || r.has("stage1");
        if (!isRecommend) {
            return "LLM JSON:\n" + NodeTree.pretty(r);
        }

        StringBuilder sb = new StringBuilder();
        appendHeader(sb, o, r);
        appendInput(sb, r);
        appendInference(sb, r);
        appendWeights(sb, r);
        appendRecommendations(sb, r);
        appendLiveCoverage(sb, r);
        appendChecks(sb, r);
        appendExcluded(sb, r);
        appendBooking(sb, r);
        appendFooter(sb, r);
        return sb.toString();
    }

    // ------------------------------------------------------------------ 各节

    private static void appendHeader(StringBuilder sb, JSONObject wrapper, JSONObject r) {
        boolean ok = wrapper.has("ok") ? wrapper.optBoolean("ok", false) : true;
        sb.append(ok ? "✓ 完成" : "✗ 未完成");
        if (wrapper.has("rc")) {
            sb.append("  rc=").append(wrapper.optInt("rc"));
        }
        String err = optStr(wrapper, "error");
        if (!err.isEmpty()) {
            sb.append("\n错误: ").append(err);
        }
        sb.append('\n');
    }

    /** optString 会把 JSON null 渲染成字面量 "null"；这里统一归一为真正缺失。 */
    private static String optStr(JSONObject o, String k) {
        if (o == null || o.isNull(k)) {
            return "";
        }
        String v = o.optString(k, "");
        return "null".equals(v) ? "" : v;
    }

    private static void appendInput(StringBuilder sb, JSONObject r) {
        JSONObject sp = r.optJSONObject("spec");
        if (sp == null) {
            sp = new JSONObject();
        }
        JSONObject pf = sp.optJSONObject("prefs");
        if (pf == null) {
            pf = new JSONObject();
        }
        JSONObject pt = sp.optJSONObject("patient");
        String ptTxt;
        if (pt != null) {
            ptTxt = pt.optString("name_masked", "?") + " · "
                    + (pt.has("age") ? pt.optInt("age") + "岁" : "年龄未知") + " · "
                    + zhGender(pt.optString("gender", ""))
                    + (pt.optBoolean("insurance_card", false) ? " · 医保卡" : " · 无医保卡")
                    + (pt.has("gender_source") ? "（" + pt.optString("gender_source") + "）" : "");
        } else if (!sp.isNull("patient") && sp.has("patient")) {
            // patient 存在但非对象（被清空）
            ptTxt = "未选择（代他人挂号：年龄/性别由描述解析）";
        } else if (sp.optBoolean("patient_selected", true) == false) {
            ptTxt = "未选择（代他人挂号：年龄/性别由描述解析）";
        } else {
            ptTxt = "—（旧运行: 无就诊人字段）";
        }
        sb.append("\n【本次输入】\n");
        sb.append("就诊人: ").append(ptTxt).append('\n');
        String feeLabel = pf.optString("fee_expect_label", "auto");
        sb.append("预期费用: ").append(feeLabel);
        if (!pf.isNull("price_cap_cny") && pf.has("price_cap_cny")) {
            sb.append("（硬上限 ").append(pf.opt("price_cap_cny")).append(" 元）");
        }
        sb.append('\n');
        if (!sp.isNull("urgency_days")) {
            sb.append("时效默认: ").append(sp.opt("urgency_days"))
              .append("天（LLM 从描述改写）\n");
        }
        String symp = sp.optString("symptom_text", "");
        if (!symp.isEmpty()) {
            sb.append("症状: ").append(clip(symp, 120)).append('\n');
        }
    }

    private static void appendInference(StringBuilder sb, JSONObject r) {
        JSONObject inf = r.optJSONObject("inference");
        JSONObject st = r.optJSONObject("stage1");
        if (inf == null && st == null) {
            return;
        }
        if (inf == null) {
            inf = new JSONObject();
        }
        sb.append("\n【推理（Agent Service 直出）】\n");
        sb.append("scenario=").append(inf.optString("scenario", "?"));
        String zh = inf.optString("scenario_zh", "");
        if (!zh.isEmpty()) {
            sb.append("（").append(zh).append("）");
        }
        // 主词
        String term = inf.optString("triage_term", "");
        if (term.isEmpty() && st != null && st.optJSONObject("triage") != null) {
            term = st.optJSONObject("triage").optString("term", "");
        }
        if (!term.isEmpty()) {
            sb.append(" · 主词=").append(term);
        }
        // K
        if (st != null && !st.isNull("K")) {
            sb.append(" · K=").append(st.opt("K"));
        }
        sb.append('\n');
        // 目标科室 + 层级
        JSONArray depts = inf.optJSONArray("llm_depts");
        if (depts != null && depts.length() > 0) {
            JSONObject tiers = inf.optJSONObject("llm_dept_tiers");
            sb.append("目标科室: ");
            List<String> parts = new ArrayList<>();
            for (int i = 0; i < depts.length(); i++) {
                String d = depts.optString(i);
                String tier = "?";
                if (tiers != null && tiers.has(d)) {
                    int t = tiers.optInt(d, 1);
                    tier = (t == 1 ? "首选" : t == 2 ? "相关" : "弱相关");
                }
                parts.add(d + "·" + tier);
            }
            sb.append(join(parts, "、")).append('\n');
        } else {
            sb.append("目标科室: (词表)\n");
        }
    }

    private static void appendWeights(StringBuilder sb, JSONObject r) {
        JSONObject inf = r.optJSONObject("inference");
        JSONObject w = inf == null ? null : inf.optJSONObject("weights");
        if (w == null || w.length() == 0) {
            return;
        }
        double max = 0;
        java.util.Iterator<String> it = w.keys();
        while (it.hasNext()) {
            double v = w.optDouble(it.next(), 0);
            if (v > max) {
                max = v;
            }
        }
        sb.append("\n【权重向量】（行和=1; price=费用友好度, 仅对已核实号源生效）\n");
        // 按权重降序展示更直观
        List<String> keys = new ArrayList<>();
        it = w.keys();
        while (it.hasNext()) {
            keys.add(it.next());
        }
        final JSONObject wf = w;
        java.util.Collections.sort(keys, new java.util.Comparator<String>() {
            @Override
            public int compare(String a, String b) {
                return Double.compare(wf.optDouble(b, 0), wf.optDouble(a, 0));
            }
        });
        for (String k : keys) {
            double v = wf.optDouble(k, 0);
            int bar = max > 0 ? (int) Math.round(v / max * 14) : 0;
            String zh = FEATURE_ZH.containsKey(k) ? FEATURE_ZH.get(k) : k;
            sb.append("  ").append(padR(zh, 14))
              .append(String.format("%6.3f ", v))
              .append(repeat('█', bar)).append('\n');
        }
    }

    private static void appendRecommendations(StringBuilder sb, JSONObject r) {
        JSONObject fin = r.optJSONObject("final");
        if (fin == null) {
            return;
        }
        JSONArray recs = fin.optJSONArray("recommendations");
        if (recs == null || recs.length() == 0) {
            sb.append("\n【推荐】\n（无推荐）\n");
            return;
        }
        sb.append("\n【推荐（两级排序: ① 号源状态 已核实有号 > 未核实 > 已核实无号 ② 同状态 final=静态分×号源效用）】\n");
        for (int i = 0; i < recs.length(); i++) {
            JSONObject x = recs.optJSONObject(i);
            if (x == null) {
                continue;
            }
            String dep = x.has("dept_tab")
                    ? x.optString("dept_tab") + "/" + x.optString("dept_raw", x.optString("dept", ""))
                    : x.optString("dept", "—");
            sb.append('\n')
              .append(x.optInt("final_rank", i + 1)).append(") ")
              .append(x.optString("name")).append("  [")
              .append(x.optString("level")).append('/').append(x.optString("district")).append("]\n");
            sb.append("   科室: ").append(dep)
              .append("   距离: ").append(dist(x)).append('\n');
            sb.append("   静态").append(x.optString("static_score", "?"))
              .append(" × 号源u").append(x.optString("slot_utility", "?"))
              .append(" = 终排").append(x.optString("final_score", "?")).append('\n');
            sb.append("   最早可约 ").append(earliest(x))
              .append(" · 费档 ").append(x.optString("fee_tier", "—"))
              .append(" · ").append(x.optString("slot_source", "?")).append('\n');
            String doc = x.optString("llm_doctor", "");
            if (!doc.isEmpty()) {
                sb.append("   终裁医生: ").append(doc);
                if (x.has("llm_reason")) {
                    sb.append(" (").append(x.optString("llm_reason")).append(')');
                }
                sb.append('\n');
            }
            String why = x.optString("why", "");
            if (!why.isEmpty()) {
                sb.append("   why: ").append(clip(why, 160)).append('\n');
            }
        }
        if (fin.optBoolean("fallback_all_unverified", false)) {
            sb.append("\n⚠ 全部未核实 → §4.5 回落静态排序（RANKING_SPEC）\n");
        }
    }

    private static void appendLiveCoverage(StringBuilder sb, JSONObject r) {
        JSONObject fin = r.optJSONObject("final");
        JSONObject lc = fin == null ? null : fin.optJSONObject("live_coverage");
        if (lc == null) {
            return;
        }
        JSONObject probe = lc.optJSONObject("probe");
        JSONObject deep = lc.optJSONObject("deep");
        sb.append("\n【实时核实】\n");
        if (probe != null || deep != null) {
            if (probe != null && !probe.isNull("n_routes")) {
                sb.append("  Phase A 快速探测 ")
                  .append(probe.optInt("n_parsed")).append('/').append(probe.opt("n_routes"))
                  .append(" 院(L2 单页)").append(stopReason(probe)).append('\n');
            }
            if (deep != null && !deep.isNull("n_routes")) {
                sb.append("  Phase B 深核 ")
                  .append(deep.optInt("n_parsed")).append('/').append(deep.opt("n_routes"))
                  .append(" 院(逐医生六字段)").append(stopReason(deep)).append('\n');
            }
        } else if (!lc.isNull("n_routes")) {
            sb.append("  实时核实 ").append(lc.optInt("n_parsed")).append('/')
              .append(lc.opt("n_routes")).append(" 家").append(stopReason(lc)).append('\n');
        } else {
            sb.append("  （无覆盖统计）\n");
        }
    }

    private static void appendChecks(StringBuilder sb, JSONObject r) {
        JSONArray checks = r.optJSONArray("checks");
        if (checks == null) {
            return;
        }
        int fail = 0;
        for (int i = 0; i < checks.length(); i++) {
            JSONObject c = checks.optJSONObject(i);
            if (c != null && !c.optBoolean("ok", false)) {
                fail++;
            }
        }
        sb.append("\n【回归断言】").append(checks.length()).append(" 项 ");
        sb.append(fail == 0 ? "全部 PASS" : (fail + " FAIL")).append('\n');
        for (int i = 0; i < checks.length(); i++) {
            JSONObject c = checks.optJSONObject(i);
            if (c == null) {
                continue;
            }
            sb.append("  ").append(c.optBoolean("ok", false) ? "✓ " : "✗ ")
              .append(c.optString("id")).append(" ")
              .append(clip(c.optString("name"), 80));
            String detail = c.optString("detail", "");
            if (!detail.isEmpty()) {
                sb.append(" → ").append(clip(detail, 60));
            }
            sb.append('\n');
        }
    }

    private static void appendExcluded(StringBuilder sb, JSONObject r) {
        JSONObject fin = r.optJSONObject("final");
        JSONArray ex = fin == null ? null : fin.optJSONArray("excluded");
        if (ex == null || ex.length() == 0) {
            return;
        }
        int show = Math.min(ex.length(), 12);
        sb.append("\n【显式排除 ").append(ex.length()).append(" 家】");
        if (ex.length() > show) {
            sb.append("（前 ").append(show).append("）");
        }
        sb.append('\n');
        for (int i = 0; i < show; i++) {
            JSONObject x = ex.optJSONObject(i);
            if (x == null) {
                continue;
            }
            sb.append("  · ").append(x.optString("name", x.optString("hospital_id", "?")))
              .append("  ").append(dist(x)).append('\n')
              .append("    ").append(clip(x.optString("reason", ""), 90)).append('\n');
        }
    }

    private static void appendBooking(StringBuilder sb, JSONObject r) {
        JSONObject bk = r.optJSONObject("booking");
        if (bk == null) {
            return;
        }
        sb.append("\n【模拟挂号（dry-run，不实际下单）】\n");
        JSONArray steps = bk.optJSONArray("steps");
        if (steps != null) {
            for (int i = 0; i < steps.length(); i++) {
                sb.append("  ").append(steps.optString(i)).append('\n');
            }
        }
        String refused = bk.optString("refused", "");
        if (!refused.isEmpty()) {
            sb.append("  ⛔ ").append(refused).append('\n');
        }
    }

    private static void appendFooter(StringBuilder sb, JSONObject r) {
        JSONObject meta = r.optJSONObject("meta");
        if (meta == null) {
            return;
        }
        sb.append("\n落盘: ").append(meta.optString("mode", ""))
          .append(" · ").append(meta.optString("generated_at", "")).append('\n');
    }

    // ------------------------------------------------------------------ 小工具

    private static String earliest(JSONObject x) {
        if (!x.isNull("earliest_date") && x.has("earliest_date")) {
            String d = x.optString("earliest_date", "");
            if (!d.isEmpty()) {
                return d;
            }
        }
        if (x.optBoolean("verified", false)) {
            return "no_slot".equals(x.optString("slot_status")) ? "无号(已核实)" : "—";
        }
        return "未核实";
    }

    private static String dist(JSONObject x) {
        if (x.has("distance_km") && !x.isNull("distance_km")) {
            return x.opt("distance_km") + "km";
        }
        return "?";
    }

    private static String stopReason(JSONObject o) {
        String s = o.optString("stop_reason", "");
        return s.isEmpty() ? "" : " [" + s + "]";
    }

    private static String zhGender(String g) {
        if ("male".equals(g)) {
            return "男";
        }
        if ("female".equals(g)) {
            return "女";
        }
        return "性别未定";
    }

    private static String clip(String s, int n) {
        if (s == null) {
            return "";
        }
        s = s.replaceAll("\\s+", " ").trim();
        return s.length() <= n ? s : s.substring(0, n) + "…";
    }

    private static String padR(String s, int n) {
        StringBuilder b = new StringBuilder(s);
        while (b.length() < n) {
            b.append(' ');
        }
        return b.toString();
    }

    private static String repeat(char c, int n) {
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < n; i++) {
            b.append(c);
        }
        return b.toString();
    }

    private static String join(List<String> xs, String sep) {
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < xs.size(); i++) {
            if (i > 0) {
                b.append(sep);
            }
            b.append(xs.get(i));
        }
        return b.toString();
    }
}
