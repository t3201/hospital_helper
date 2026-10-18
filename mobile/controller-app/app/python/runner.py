# -*- coding: utf-8 -*-
"""runner.py — Chaquopy 入口（P3）。

Java 侧唯一调用点：
    Python.getInstance().getModule("runner").callAttr("run", spec_json, base, api_key)
      -> JSON 字符串 {ok, rc, error, out_dir, result, checks}

职责：
  * 组装 main.parse_args 需要的 namespace（表单 -> spec-file + 位置坐标/地址）。
  * 把端上配置（归集服务端 base）写回 PyBridge 配置，供 llm_client/amap 读取；
    api_key 参数仅为兼容旧调用保留（归集后 App 不再持有上游凭据，服务端自己带 token）。
  * 捕获 run_pipeline 的 stdout 逐行回吐给 UI（PyBridge.emitLog）。
  * 保持 9 段流水线语义不变（2 次 LLM、号源 provenance、A/B/C/D 断言）。

spec_json 结构（与 app/web.py 表单同 schema，UTF-8）:
  {
    "symptom_text": "...",              # 必填
    "loc": "121.47,31.23" | null,       # 二选一（GCJ-02）
    "loc_text": "徐汇区宜山路600号" | null,
    "fee_expect": "auto|zero|ten|hundred|thousand|tenk|any",
    "patient": {...} | null,            # 随申办就诊人（页面已脱敏）
    "patient_selected": true|false,
    "metro_priority": false,
    "live": false                       # 端上 live 抓取（慢，无障碍桥）
  }
"""
import io
import json
import os
import sys
import traceback

import devbridge

try:
    from slots import navlock as NAVLOCK
except Exception:                       # 桌面直跑
    import navlock as NAVLOCK
import hhpaths
from app import main as app_main

CASE_SUBDIR = "run"          # 产物落 OUT_DIR/run/
SPEC_NAME = "run_spec.json"
# 2026-10-14 修复：不再硬编码某台机器（旧值 "476cf991" 会把 MI 5X 0043d38b0504 上跑出来的
# 日志/断点续跑状态标成另一台手机）。端上用 devbridge.device_key() 取真实机器标识；
# spec 里显式带 device 时仍然优先（便于 PC 端指定 adb 序列号）。
DEFAULT_DEVICE = None


class _LiveOut(io.TextIOBase):
    """stdout 逐行回吐：既缓存供失败回放，又实时交给 PyBridge.emitLog。"""

    def __init__(self):
        self.lines = []
        self._buf = ""

    def write(self, s):
        if not isinstance(s, str):
            s = str(s)
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.lines.append(line)
            devbridge.emit_log(line)
        return len(s)

    def flush(self):
        if self._buf:
            self.lines.append(self._buf)
            devbridge.emit_log(self._buf)
            self._buf = ""


def _build_argv(spec, spec_file, base, api_key):
    argv = [
        "--spec-file", spec_file,
        "--out", hhpaths.OUT_DIR,
        "--non-interactive",
        "--llm-model", "default",
    ]
    if base:
        argv += ["--agentsvc-base", base]
    loc = (spec.get("loc") or "").strip() if isinstance(spec.get("loc"), str) else spec.get("loc")
    loc_text = (spec.get("loc_text") or "").strip() if isinstance(spec.get("loc_text"), str) else spec.get("loc_text")
    if loc:
        argv += ["--loc", str(loc)]
    elif loc_text:
        argv += ["--loc-text", str(loc_text)]
    if spec.get("live"):
        dev = spec.get("device") or DEFAULT_DEVICE or devbridge.device_key()
        argv += ["--live", "--device", str(dev)]
        st = spec.get("slot_type")
        if st in ("expert", "normal", "specialty"):
            argv += ["--live-slot-type", st]
    # 运行参数：候选集数量 -> --probe-n；推荐数量 -> --top
    probe_n = spec.get("probe_n")
    if probe_n is not None:
        try:
            probe_n = int(probe_n)
            if probe_n >= 1:
                argv += ["--probe-n", str(probe_n)]
        except (TypeError, ValueError):
            pass
    top_n = spec.get("top_n")
    if top_n is not None:
        try:
            top_n = int(top_n)
            if top_n >= 1:
                argv += ["--top", str(top_n)]
        except (TypeError, ValueError):
            pass
    return argv


def run(spec_json, base="", api_key="", case_id=None):
    """端上流水线入口。任何异常都包装进返回 JSON，不向 Java 抛。"""
    base = (base or "").strip()
    api_key = api_key or ""
    # 把端上配置写回 PyBridge（llm_client.resolve_base_url/resolve_api_key 会读它）
    devbridge.set_config("llm_base", base)
    devbridge.set_config("api_key", api_key)

    err = None
    rc = 1
    result = None
    out = _LiveOut()
    try:
        spec = json.loads(spec_json) if isinstance(spec_json, str) else dict(spec_json or {})
    except Exception as e:
        return json.dumps({"ok": False, "rc": 1, "error": "spec_json 非法 JSON: %s" % e,
                           "result": None, "checks": None}, ensure_ascii=False)

    if not (spec.get("symptom_text") or "").strip():
        return json.dumps({"ok": False, "rc": 1, "error": "symptom_text 不能为空",
                           "result": None, "checks": None}, ensure_ascii=False)

    out_dir = hhpaths.ensure_out(CASE_SUBDIR)
    spec_file = hhpaths.out(SPEC_NAME)
    try:
        with open(spec_file, "w", encoding="utf-8") as fh:
            json.dump(spec, fh, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "rc": 1, "error": "写 spec 失败: %s" % e,
                           "result": None, "checks": None}, ensure_ascii=False)

    argv = _build_argv(spec, spec_file, base, api_key)
    devbridge.emit_log("[runner] 启动 9 段流水线；base=%s api_key=%s live=%s"
                       % (base or "(默认)", "已设置" if api_key else "(空)",
                          bool(spec.get("live"))))
    # ②/⑥: live 流水线会驱动随申办; 已有别的导航所有者时如实拒绝, 不并发抢屏
    if spec.get("live") and not NAVLOCK.acquire("pipeline"):
        h = NAVLOCK.holder() or {}
        return json.dumps({"ok": False, "rc": 5,
                           "error": "随申办导航正被 %s 占用(%.0fs), 流水线不排队不抢占"
                                    % (h.get("owner"), h.get("held_s") or 0),
                           "result": None, "checks": None}, ensure_ascii=False)
    if spec.get("live"):
        _nav_owned = True
    else:
        _nav_owned = False
    old_stdout = sys.stdout
    try:
        sys.stdout = out
        try:
            args = app_main.parse_args(argv)
            rc = app_main.run_pipeline(args, out_subdir=CASE_SUBDIR) or 0
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
            err = None if rc in (0, 2) else str(e.code)
        except BaseException as e:  # noqa
            rc = 1
            err = "%s: %s" % (type(e).__name__, str(e)[:300])
            traceback.print_exc()
    finally:
        sys.stdout = old_stdout
        out.flush()
        if _nav_owned:
            NAVLOCK.release("pipeline")

    # 读回产物
    try:
        with open(hhpaths.out(CASE_SUBDIR, "result.json"), encoding="utf-8") as fh:
            result = json.load(fh)
    except Exception as e:
        if err is None:
            err = "读取 result.json 失败: %s" % e

    summary = {
        "ok": bool(result) and err is None,
        "rc": rc,
        "error": err,
        "out_dir": out_dir,
        "result": result,
        "checks": (result or {}).get("checks"),
    }
    payload = json.dumps(summary, ensure_ascii=False, default=str)
    devbridge.set_result(payload)
    devbridge.emit_log("[runner] 完成 rc=%s ok=%s%s"
                       % (rc, summary["ok"], (" err=%s" % err) if err else ""))
    return payload
