package com.hospitalhelper.controller;

import org.json.JSONObject;

/**
 * 端上编排状态机。
 *
 * <p><b>本期（P0–P2）只做骨架 + 哑回放</b>：唤起随申办 → 等待 → 读屏 → 打印节点树。
 * 9 段完整流水线留待 <b>P3</b>（下方 {@link #PIPELINE_P3} 已列出占位）。
 *
 * <p>所有方法假定在后台线程调用（读屏/等待会阻塞）；UI 更新由调用方切主线程。
 */
public class TaskEngine {

    /** P3 待实现的 9 段流水线（占位，勿删）。 */
    public static final String[] PIPELINE_P3 = {
            "1 表单解析（症状/就诊人/费用/位置）",
            "2 LLM 头推理（权重向量 + 目标科室）",
            "3 唤起随申办",
            "4 读屏（getWindows → 随申办 root）",
            "5 导航到 预约挂号",
            "6 搜索医院（inputText + pressEnter）",
            "7 抓号源（滑动 + 读屏枚举）",
            "8 排名（本地权重计算）",
            "9 LLM 终裁（白名单内 top3）+ 模拟挂号步骤",
    };

    /** 状态枚举（骨架）。 */
    public enum State {
        INIT, LAUNCH, WAIT, DUMP, P0_REPLAY_DONE, ERROR
    }

    /** 回调。全部在调用线程触发，由调用方负责切主线程。 */
    public interface Listener {
        void onLog(String line);

        void onState(String state, String note);

        void onResult(JSONObject result);

        void onError(String message);
    }

    private final Listener L;

    public TaskEngine(Listener listener) {
        this.L = listener;
    }

    /**
     * 哑回放：唤起随申办 → 等待 → 读屏 → 回吐节点 JSON。
     *
     * @return 读屏得到的节点树（失败为 null）
     */
    public JSONObject runDumbReplay(DeviceBridge bridge, String targetPkg) {
        if (bridge == null) {
            state(State.ERROR, "读屏服务未授权");
            L.onError("AccessibilityService 未连接 —— 请先在系统设置里开启读屏桥");
            return null;
        }

        // 3 唤起随申办
        state(State.LAUNCH, "唤起 " + targetPkg);
        boolean launched = bridge.launch(targetPkg);
        L.onLog("launch(" + targetPkg + ") -> " + launched);
        if (!launched) {
            state(State.ERROR, "唤起失败");
            L.onError("无法唤起随申办：未安装？或 Android11+ 的 <queries> 包可见性缺失？");
            return null;
        }

        // 4 等待窗口就绪（事件驱动 + 轮询兜底 + 超时）
        state(State.WAIT, "等待窗口就绪");
        boolean seen = bridge.waitForText("随申办", 3000); // 首页未必含“随申办”三字，仅尽力
        L.onLog("waitForText(随申办, 3000ms) -> " + seen);
        sleepQuiet(1500);

        // 4 读屏
        state(State.DUMP, "读屏");
        JSONObject tree = bridge.dump();
        if (tree == null) {
            state(State.ERROR, "读屏为空");
            L.onError("读屏为 null —— 未取到随申办窗口（服务未授权 / 随申办未在前台）");
            return null;
        }
        int n = NodeTree.count(tree);
        L.onLog("读屏成功：节点数 = " + n);
        L.onResult(tree);

        state(State.P0_REPLAY_DONE, "哑回放完成（完整 9 段流水线见 PIPELINE_P3）");
        return tree;
    }

    private void state(State s, String note) {
        L.onState(s.name(), note);
    }

    private static void sleepQuiet(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
