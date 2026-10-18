# -*- coding: utf-8 -*-
"""slots/navlock.py — 随申办导航的「单一所有者」锁（②状态机 / ⑥唯一熔断的地基）。

为什么必须有它（2026-10-15 23:24 实机事故的根因）：
  同一进程里有 4 个入口都会驱动随申办导航：
    * runner.run                 —— 9 段流水线（live 抓取）
    * fetch_slots.run_conditions —— 号源抓取（自测 / PC 驱动）
    * app.preflight.check_login_json —— 登录深检（冷启动自动补跑）
    * app.patients.fetch_patients_json —— 就诊人采集
  它们各自独立「点屏幕」。一旦并发（自测跑到一半，MainActivity onResume 的自动
  登录深检也起来了），后者会把前者刚到达的页面 BACK 掉：full run 停在 L2 医生
  列表时，深检的 ensure_444 一路 BACK，于是「点子tab 普通门诊」实际落在 L1
  选科室。观测层与状态机都没错 —— 是**导航所有权被抢**。Java 侧 SelfTest.running
  只挡自测自己，挡不住另一条 Java 线程，所以必须在 Python 侧（所有导航的汇聚点）
  立一条唯一规则。

唯一规则：
  * 同一线程重入 = 允许（流水线内部会嵌套调用 patients / preflight 同款导航）；
  * 不同线程 = 拒绝。调用方必须**如实返回 busy**（不排队、不抢占、不 back 抢屏），
    由上层（状态机 / UI）决定何时重试。
  * 熔断只看「连续 verify 失败」（⑥），导航所有权不是配额，是互斥：
    拿不到就立刻退出，绝不产生第二次导航动作。
"""
import threading
import time

__all__ = ['acquire', 'release', 'holder', 'held', 'force_release', 'describe']

_MU = threading.Lock()
_owner = None          # (thread_ident, owner_name)
_depth = 0
_t0 = 0.0

# 超过这个时长仍持有所有权 = 疑似僵尸（线程被杀 / 死锁 / 卡在无超时的 binder 调用上），
# acquire() 会**如实记录后接管**（见 acquire），而不是无限拒绝。
# 取 20 分钟：单次 full 抓取实测 ~4 分钟，9 段流水线 live 也远小于此；
# 而 2026-10-16 实测的僵尸持有长达 3257s（54 分钟），40 分钟的旧阈值救不回来。
STALE_S = 1200.0


def acquire(owner, log=None):
    """拿到导航所有权 -> True；已被别的线程占用 -> False（调用方须如实退出）。

    同线程重入只加深计数，不报错：这样 runner.run(live) 内部再调 patients/preflight
    不会被自己挡住。
    """
    global _owner, _depth, _t0
    tid = threading.get_ident()
    with _MU:
        if _owner is not None and _owner[0] == tid:
            held_s = time.time() - _t0
            if held_s > STALE_S and _depth >= 1:
                # 同线程也可能是**泄漏的旧持有**(早期 patients_fetch 没有 finally, 见 2026-10-16
                # 实测 3257s): 继续加深只会让计数永不归零。判陈旧 => 重新起算, 并留痕。
                if log:
                    log('[navlock] 同线程持有者 %s 已 %.0fs > %.0fs => 判为泄漏, 重新起算'
                        % (_owner[1], held_s, STALE_S))
                _depth, _t0 = 1, time.time()
                return True
            _depth += 1
            return True
        if _owner is not None:
            held_s = time.time() - _t0
            if held_s > STALE_S:
                # 僵尸持有者(线程被杀 / 卡在无超时的 binder 调用上): 继续拒绝会让整机
                # 永久不可用 —— 2026-10-16 实测 patients_fetch 持有 3257s, 之后每次
                # 自检都 rc=5(stop=nav_busy:patients_fetch), 且 force_release 无人调用。
                # 规则: 超过 STALE_S 判为僵尸, **如实记录后接管**, 而不是无限拒绝。
                if log:
                    log('[navlock] 持有者 %s 已 %.0fs > %.0fs => 判为僵尸, 由 %s 接管'
                        % (_owner[1], held_s, STALE_S, owner))
                _owner, _depth, _t0 = (tid, str(owner)), 1, time.time()
                return True
            if log:
                log('[navlock] 拒绝进入 %s：随申办导航正被 %s 占用 %.0fs'
                    % (owner, _owner[1], held_s))
            return False
        _owner, _depth, _t0 = (tid, str(owner)), 1, time.time()
        return True


def release(owner=None):
    """释放一层所有权；计数归零才真正放开。只认当前线程，误调无害（不报错）。"""
    global _owner, _depth
    tid = threading.get_ident()
    with _MU:
        if _owner is None or _owner[0] != tid:
            return False
        _depth -= 1
        if _depth <= 0:
            _owner, _depth = None, 0
        return True


def holder():
    """当前所有者 -> {'owner':str,'held_s':float,'depth':int}；空闲 -> None。"""
    with _MU:
        if _owner is None:
            return None
        return {'owner': _owner[1], 'held_s': round(time.time() - _t0, 1),
                'depth': _depth}


def held():
    return holder() is not None


def force_release(reason=''):
    """看门狗/调试用：强制接管僵尸持有者（超过 STALE_S 才应被调用）。"""
    global _owner, _depth
    with _MU:
        prev = _owner[1] if _owner else None
        _owner, _depth = None, 0
        return {'prev': prev, 'reason': reason}


def describe():
    h = holder()
    return '空闲' if h is None else ('%s(%.0fs, depth=%d)'
                                     % (h['owner'], h['held_s'], h['depth']))
