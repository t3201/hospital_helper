# -*- coding: utf-8 -*-
"""devbridge.py — 端上「设备桥」Python 侧门面（P3）。

语义上等价于旧 `Adb` 类 / uiautomator，但**不再走 adb/subprocess**：
所有原语都经 `com.hospitalhelper.controller.PyBridge` 静态方法落到
AccessibilityService（AccDeviceBridge / SsAccessibilityService）。

设计约束：
  * 只 `from java import jclass` 取 PyBridge；Python↔Java 边界只用 String/int。
  * dump() 返回 **uiautomator 同构的 XML 文本**（`<hierarchy><node .../>...`），
    这样 patients.py / fetch_slots.py 里既有的 `re.findall(r'<node')` 解析零改动。
  * 桌面（无 Chaquopy）下 import 不报错；真正调用原语时才抛 RuntimeError，
    以便本机做「import 全模块 + 非设备段」自检。
"""
import datetime as dt
import os

__all__ = [
    "DeviceError", "available", "pybridge",
    "is_ready", "dump", "tap", "back", "keyevent", "swipe",
    "input_text", "launch", "device_date", "screen_size", "now",
    "wait_for_text", "emit_log", "emit_step", "emit_reasoning", "set_result",
    "get_config", "set_config", "wake", "keep_screen_on", "press_enter",
    "device_key",
]


class DeviceError(RuntimeError):
    """设备桥不可用 / 原语执行失败（调用方须如实报错，不得静默降级）。"""


def pybridge():
    """返回 PyBridge jclass（Chaquopy）；桌面返回 None。"""
    try:
        from java import jclass
    except Exception:
        return None
    try:
        return jclass("com.hospitalhelper.controller.PyBridge")
    except Exception as e:  # JVM 在但类缺失
        raise DeviceError("PyBridge 类缺失: %s" % e)


def available():
    return pybridge() is not None


def _pb():
    pb = pybridge()
    if pb is None:
        raise DeviceError("devbridge 仅在端上（Chaquopy）可用；"
                          "桌面自检请勿调用设备原语")
    return pb


# ------------------------------------------------------------------ 设备原语
def is_ready():
    """无障碍服务是否已 Bound（读屏桥是否可用）。"""
    pb = pybridge()
    if pb is None:
        return False
    try:
        return bool(pb.isReady())
    except Exception:
        return False


def launch(pkg=None):
    """唤起随申办（CLEAR_TOP 等效重启）。pkg 仅作语义提示；端上目标固定。"""
    return bool(_pb().launch())


def dump():
    """读屏。返回 uiautomator 同构 XML 文本（无窗口时抛 DeviceError）。"""
    xml = _pb().dumpJson()
    if not xml:
        raise DeviceError("读屏为空：未取到随申办窗口（未授权 / 未在前台）")
    return xml


def tap(x, y):
    return bool(_pb().tap(int(x), int(y)))


def back():
    return bool(_pb().back())


def keyevent(k):
    """按键。PyBridge 映射：4→BACK、3→HOME、66→ENTER；其余 no-op。"""
    return bool(_pb().keyevent(int(k)))


def swipe(x1, y1, x2, y2, dur=350):
    return bool(_pb().swipe(int(x1), int(y1), int(x2), int(y2), int(dur)))


def input_text(s):
    """CJK/英文均走 ACTION_SET_TEXT（不依赖 ADBKeyboard）。"""
    return bool(_pb().inputText(s))


def press_enter():
    return bool(_pb().pressEnter())


def wait_for_text(text, timeout_ms):
    return bool(_pb().waitForText(text, int(timeout_ms)))


def screen_size():
    """-> (w, h)。PyBridge.screenSize() 返回 \"w,h\"。"""
    v = _pb().screenSize() or ""
    try:
        w, h = v.split(",")
        return int(w), int(h)
    except Exception:
        return 1080, 2392


def now():
    """设备本地时间（ISO-8601 字符串）-> datetime。PyBridge.now() 返回 ISO。"""
    try:
        return dt.datetime.fromisoformat(_pb().now())
    except Exception:
        return dt.datetime.now()


def last_known_location():
    """最后已知位置（LocationManager，WGS-84）-> (lat, lng, provider, age_ms)；
    无可用 provider / 无最后位置 / 未授权 -> None。桌面（无 PyBridge）-> None。
    只读 lastKnownLocation，不启动持续定位。"""
    try:
        pb = _pb()
    except Exception:
        return None
    if pb is None:
        return None
    try:
        v = pb.lastKnownLocation() or ""
    except Exception:
        return None
    if not v:
        return None
    parts = v.split(",")
    if len(parts) < 4:
        return None
    try:
        return float(parts[0]), float(parts[1]), parts[2], int(parts[3])
    except Exception:
        return None


def device_date():
    """设备日期时间；失败退化本机时钟。"""
    try:
        return now()
    except DeviceError:
        return dt.datetime.now()


# ------------------------------------------------------------------ 端上辅助（非设备原语）
def emit_log(line):
    """把流水线 stdout 逐行回吐给 UI（桌面 no-op）。"""
    pb = pybridge()
    if pb is None:
        return
    try:
        pb.emitLog(str(line))
    except Exception:
        pass


def emit_step(text):
    """逐步骤提示 → 浮窗（MiniChip）。桌面 no-op。

    用法（fetch_slots.py 导航层）：
      devbridge.emit_step('寻找tab:%s' % tab)
      devbridge.emit_step('点击科室:%s' % dept)
      devbridge.emit_step('进入%s' % subtab_zh)
    """
    pb = pybridge()
    if pb is None:
        return
    try:
        pb.emitStep(str(text))
    except Exception:
        pass


def emit_step_state(text, state='run'):
    """带状态的逐步骤提示 -> 浮窗(MiniChip)。桌面 no-op。

    state: 'run' 常规 / 'verify' 校验中(动作已发出, 屏幕证据未到) /
           'ok' 观测层已核实 / 'fail' 校验失败。
    旧版 PyBridge 没有 emitStepState 时自动退回 emitStep, 保证兼容。
    """
    pb = pybridge()
    if pb is None:
        return
    try:
        pb.emitStepState(str(text), str(state or 'run'))
    except AttributeError:
        try:
            pb.emitStep(str(text))
        except Exception:
            pass
    except Exception:
        pass


def emit_reasoning(text, kind='content'):
    """模型推理逐块转发 → 实时日志页“模型推理”区（恒显）。桌面 no-op。

    kind 由后端 SSE 字段决定（调用方原样透传，不做攒批/延迟）:
      'think'   → 思维链（UI 灰字 + 斜体）
      'content' → 正文（UI 常规色）
      'meta'    → 分隔/标记行（如“—— 模型推理开始 ——”）
    """
    pb = pybridge()
    if pb is None:
        return
    try:
        pb.emitReasoning(str(text), str(kind or 'content'))
    except Exception:
        pass


def set_result(json_str):
    pb = pybridge()
    if pb is None:
        return
    try:
        pb.setResult(str(json_str))
    except Exception:
        pass


def get_config(key, default=None):
    pb = pybridge()
    if pb is None:
        return default
    try:
        v = pb.getConfig(str(key))
        return v if v not in (None, "") else default
    except Exception:
        return default


def set_config(key, value):
    pb = pybridge()
    if pb is None:
        return
    try:
        pb.setConfig(str(key), "" if value is None else str(value))
    except Exception:
        pass


def device_key():
    """本机标识（日志 / 断点续跑 resume key）。

    2026-10-14 修复：runner.py / selftest.py 曾硬编码 '476cf991'，导致 MI 5X(0043d38b0504)
    上跑出来的日志写着另一台机器（`device=476cf991`、`resume: … on 476cf991`），跨机统计串台。

    顺序：SP 覆盖(hh_config.device) → Java PyBridge.deviceKey()（序列号或型号指纹）
    → 桌面回退 env HH_DEVICE / DEVICE_SERIAL / 'local'。
    """
    pb = pybridge()
    if pb is not None:
        try:
            v = pb.deviceKey()
            if v:
                return str(v)
        except Exception:
            pass
    for k in ('HH_DEVICE', 'DEVICE_SERIAL'):
        v = (os.environ.get(k) or '').strip()
        if v:
            return v
    return 'local'


def wake():
    """尽力点亮/解锁兜底（端上由服务/系统设置承担；此处仅日志）。"""
    emit_log("[devbridge] wake: 端上保持前台由 ForegroundKeeperService 承担")


def keep_screen_on(_on=True):
    emit_log("[devbridge] keep_screen_on=%s（端上由系统/前台服务承担）" % _on)
