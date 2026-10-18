# -*- coding: utf-8 -*-
"""app/preflight.py — 端上「前置检查」之登录深检（只读导航，绝不下单）。

红线：只会走到 预约挂号(444) 列表页并读屏，**绝不**点击 去预约/提交/支付。
本模块复用已实测通过的 `slots.fetch_slots.Nav.ensure_444`（与 app.patients
里 _goto_444_then_patients 完全同款的 adb + Nav 构造），不新造任何导航分支。

判据（真实、不臆造）：
  * Nav.ensure_444() -> 'ok'      : 已进入「全部医院(444)」列表 => 视为已登录且可预约
  *                    -> 'blocked': 触发 实名认证/安全验证/风控 => 需人工在随申办完成
  *                    -> 'fail'   : 6 轮仍进不去 => 多半未登录 / 页面异常
"""
import json
import time

import devbridge

try:
    from slots import navlock as NAVLOCK
except Exception:                       # 桌面直跑
    import navlock as NAVLOCK

__all__ = ["check_login_json"]


def check_login_json(dev=None):
    """深检随申办登录态 -> 单 JSON 字符串（避免 Chaquopy 元组跨语言摩擦）。

    返回 shape：
      {ok:bool, state:'logged'|'blocked'|'fail'|'no_service'|'busy'|'error',
       kind:str, landing:str, elapsed_s:float, error:str|null}
    landing = 深检结束时随申办停在哪一页(444/auth/...); 刻意停在 444, 不再 BACK。
    """
    out = {'ok': False, 'state': 'fail', 'kind': None, 'landing': None,
           'elapsed_s': None, 'error': None}
    t0 = time.time()
    # ②/⑥: 深检也是导航。别的任务在驱动随申办时, 不排队、不抢屏, 直接如实 busy。
    if not NAVLOCK.acquire('preflight_login_check'):
        h = NAVLOCK.holder() or {}
        out['state'] = 'busy'
        out['error'] = ('随申办导航正被 %s 占用(%.0fs), 深检不排队不抢占, 稍后自动复检'
                        % (h.get('owner'), h.get('held_s') or 0))
        out['elapsed_s'] = 0.0
        return json.dumps(out, ensure_ascii=False)
    try:
        # 前置：读屏服务未绑定则直接返回（不空跑 6 轮导航）
        if not devbridge.is_ready():
            out['state'] = 'no_service'
            out['error'] = '读屏服务未绑定（请先在「无障碍」开启"挂号助手 · 读屏桥"）'
            NAVLOCK.release('preflight_login_check')
            out['elapsed_s'] = round(time.time() - t0, 1)
            return json.dumps(out, ensure_ascii=False)

        from app import patients as _pat
        from slots import fetch_slots as _fs

        adb = _pat.Adb(dev)                      # 含 ensure_foreground/launch/dump/tap/back
        st, s = _fs.Nav(adb, '').ensure_444(max_rounds=6)
        try:
            out['kind'] = _fs.page_kind(s) if s else ''
        except Exception:
            out['kind'] = ''

        if st == 'ok':
            out['ok'] = True
            out['state'] = 'logged'
        elif st == 'blocked':
            out['state'] = 'blocked'
            out['error'] = '触发实名认证/安全验证/风控，需人工在随申办完成'
        else:
            out['state'] = 'fail'
            out['error'] = '未能进入 预约挂号(444)：多半未登录或页面异常（nav=%s kind=%s）' % (st, out['kind'])

        # 收尾(2026-10-16 真机复盘 476cf991, 有 logcat 证据): **不再按 BACK**。
        # 实测 444 的上一层就是「预约挂号 · 需要如下个人信息」同意页(rid=btnSumit),
        # 进 444 之后再 BACK, 等于把随申办退到同意页然后收工 —— 用户看到的就是
        # "随申办到了 444 又退回同意页, 一直停在这里"。
        # 444 本身是稳定落点: 下次 ensure_444 第一拍 kind=444 直接复用, 不必重走搜索通道/同意页。
        # 红线不变: 只读导航, 不点任何业务按钮(同意页的「同意」由 ensure_444 按需点击)。
        out['landing'] = out['kind'] or ''
    except Exception as e:  # noqa: BLE001
        out['state'] = 'error'
        out['error'] = '%s: %s' % (type(e).__name__, e)

    out['elapsed_s'] = round(time.time() - t0, 1)
    NAVLOCK.release('preflight_login_check')
    return json.dumps(out, ensure_ascii=False)
