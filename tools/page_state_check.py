# -*- coding: utf-8 -*-
"""page_state_check.py — 观测层离线回归(不碰设备, 用真机 dump 语料给 page_state 打分)

用法:
  python tools/page_state_check.py                     # 默认语料 slots/out/raw + slots/out/diag
  python tools/page_state_check.py --dir <dir> ...      # 追加语料目录
  python tools/page_state_check.py --verbose            # 打印不一致样本(前 20 条)

打分口径(全部是「可核实」的断言, 不接受主观判断):
  A. level 混淆矩阵: page_state.level vs 旧 page_kind(端侧真代码, 桩掉 devbridge 后 import)
  B. 子 tab 硬判据: 含 >=2 子tab 文本的 dump 里, selected 命中数分布; six 页必须 0 命中
  C. 身份抽取: L1 医院名 / 医生主页身份行 / L2 科室名(标题) 的非空率
  D. 文件名声称的上下文 vs dump 实测(=「状态与页面是否一致」的历史底稿)
退出码: 0=关键不变量全部成立; 1=有不变量被破坏
"""
import argparse
import glob
import hashlib
import io
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, 'mobile', 'controller-app', 'app', 'python')
sys.path.insert(0, APP)

_stub = types.ModuleType('devbridge')
_stub.__getattr__ = lambda n: (lambda *a, **k: None)
sys.modules.setdefault('devbridge', _stub)

from slots import fetch_slots as F                # noqa: E402
from slots import page_state as PS                # noqa: E402
from slots import six_fields                      # noqa: E402

SUBS = PS.SUB_TABS


def load_corpus(dirs):
    files = []
    for d in dirs:
        files += sorted(glob.glob(os.path.join(d, '*.xml')))
    return files


def fname_ctx(path):
    """SH-084_眼科门诊_于靖主任医师_20261014T193555_dept.xml -> (hid, dept, doctor)"""
    base = os.path.basename(path)
    m = re.match(r'^(SH-\d+)_(.*?)_(.*?)_(\d{8}T\d{6})_(\w+)\.xml$', base)
    if not m:
        return None
    return {'hid': m.group(1), 'dept': m.group(2), 'doctor': m.group(3), 'kind': m.group(5)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', action='append', default=[])
    ap.add_argument('--verbose', action='store_true')
    a = ap.parse_args()
    dirs = a.dir or [os.path.join(ROOT, 'slots', 'out', 'raw'),
                     os.path.join(ROOT, 'slots', 'out', 'diag')]
    files = load_corpus(dirs)
    if not files:
        print('语料为空: %s' % dirs)
        return 1
    print('语料: %s  共 %d 份' % (dirs, len(files)))

    sixm = lambda s: bool(six_fields.match_six_page(s))
    conf = {}
    lv_new = {}
    fails = {'B_six_with_active_subtab': [], 'B_L2_no_active_subtab': [],
             'B_collapsed_not_unknown': [],
             'C_L1_no_hospital': [], 'C_six_no_identity': [], 'C_L2_no_dept': [],
             'C_L1_no_tab': [], 'C_doctor_impersonation': [],
             'C_doctor_title_residue': [],
             'D_mismatch': []}
    nB = nBsel = nL1 = nL1ok = nsix = nsixok = nL2 = nL2ok = 0
    nL1tab = nL1tabok = ndoc = ndocname = 0
    howc = {}
    n_coll = 0        # 塌陷帧(整页零高): 语料里应为 0(它是端侧瞬时帧, 不是页面形态)
    dup = {}    # dump 内容指纹 -> [(文件名, 文件名声称的科室, level)]

    for p in files:
        s = io.open(p, encoding='utf-8', errors='replace').read()
        obs = PS.observe(s, six_match=sixm)
        _c = fname_ctx(p) or {}
        dup.setdefault(hashlib.md5(s.encode('utf-8', 'replace')).hexdigest(), []).append(
            (os.path.basename(p), _c.get('dept', ''), obs.level))
        if obs.collapsed:
            n_coll += 1
            if obs.level != 'unknown':
                fails['B_collapsed_not_unknown'].append(p)
        old = F.page_kind(s)
        conf[(old, obs.level)] = conf.get((old, obs.level), 0) + 1
        lv_new[obs.level] = lv_new.get(obs.level, 0) + 1

        has_sub_txt = sum(1 for x in SUBS if x in s) >= 2
        if has_sub_txt:
            nB += 1
            if obs.subtab_active:
                nBsel += 1
            if obs.level in ('six', 'doc') and obs.subtab_active:
                fails['B_six_with_active_subtab'].append(p)
            if obs.level == 'L2' and obs.subtab_active is None and obs.subtabs:
                fails['B_L2_no_active_subtab'].append(p)

        if obs.level == 'L1':
            nL1 += 1
            if obs.hospital:
                nL1ok += 1
            else:
                fails['C_L1_no_hospital'].append(p)
            nL1tab += 1
            if obs.tab:
                nL1tabok += 1
            else:
                fails['C_L1_no_tab'].append(p)
        if obs.level in ('six', 'doc'):
            nsix += 1
            if obs.hospital and obs.dept:
                nsixok += 1
            else:
                fails['C_six_no_identity'].append(p)
        if obs.level in ('six', 'doc'):
            # doctor 的诚实性: 取不到就 None(how 说明为什么), **绝不用页头/科室/医院名冒充人名**
            ndoc += 1
            howc[obs.doctor_how] = howc.get(obs.doctor_how, 0) + 1
            if obs.doctor:
                ndocname += 1
                for other in (obs.dept, obs.hospital):
                    if other and (obs.doctor == other or other in obs.doctor or obs.doctor in other):
                        fails['C_doctor_impersonation'].append(p)
                        break
                if PS.title_residue(obs.doctor):
                    fails['C_doctor_title_residue'].append(p)
        if obs.level == 'L2':
            nL2 += 1
            if obs.dept:
                nL2ok += 1
            else:
                fails['C_L2_no_dept'].append(p)

        ctx = fname_ctx(p)
        if ctx and obs.level in ('L2', 'six', 'doc'):
            ok_id, why = PS.dept_consistent(s, ctx['dept'], obs)
            if not ok_id:
                fails['D_mismatch'].append((p, ctx['dept'], why, obs.level))

    print('\n== A. level 混淆矩阵 (旧 page_kind -> 新 level) ==')
    for (o, n), c in sorted(conf.items(), key=lambda kv: -kv[1]):
        flag = '' if o == n else ('  <- 改判' if n != 'unknown' else '  <- 仍未知')
        print('  %-8s -> %-10s %4d%s' % (o, n, c, flag))
    print('  新 level 分布: %s' % lv_new)

    print('\n== B. 子 tab 硬判据(selected) ==')
    print('  含 >=2 子tab 文本的 dump: %d, 其中 selected 命中: %d' % (nB, nBsel))
    print('  塌陷帧(整页零高, 必须判 unknown): %d' % n_coll)

    print('\n== C. 身份抽取非空率 ==')
    print('  L1 医院名      : %d/%d' % (nL1ok, nL1))
    print('  医生主页 身份行 : %d/%d' % (nsixok, nsix))
    print('  L2 科室名(标题): %d/%d' % (nL2ok, nL2))
    print('  L1 大类选中(tab): %d/%d' % (nL1tabok, nL1tab))
    print('  医生主页 doctor : %d/%d  how=%s' % (ndocname, ndoc, howc))

    print('\n== D. 文件名声称的上下文 vs dump 实测 ==')
    print('  不一致: %d' % len(fails['D_mismatch']))

    # E: 同一屏(内容逐字节相同)被存成多份证据, 且这些文件名声称不同科室 => 采集器"以为换了页, 其实没换"
    grp_multi = [v for v in dup.values() if len(v) > 1]
    grp_conflict = [v for v in grp_multi if len({d for _, d, _ in v if d}) > 1]
    print('\n== E. 同一屏被存成多份证据(dump 内容完全相同) ==')
    print('  重复组: %d 份参与; 其中「文件名声称科室不同」的组: %d (涉及 %d 份)'
          % (sum(len(v) for v in grp_multi), len(grp_conflict), sum(len(v) for v in grp_conflict)))
    if a.verbose:
        for v in sorted(grp_conflict, key=lambda x: -len(x))[:8]:
            print('    x%d  %s' % (len(v), ' | '.join(d for _, d, _ in v)[:150]))

    bad = 0
    print('\n== 不变量 ==')
    for k in ('B_six_with_active_subtab', 'B_L2_no_active_subtab', 'B_collapsed_not_unknown',
              'C_L1_no_hospital', 'C_six_no_identity', 'C_L2_no_dept',
              'C_L1_no_tab', 'C_doctor_impersonation', 'C_doctor_title_residue'):
        n = len(fails[k])
        hard = k in ('B_six_with_active_subtab', 'B_collapsed_not_unknown',
                     'C_doctor_impersonation', 'C_doctor_title_residue')
        print('  %-26s %4d %s' % (k, n, 'HARD' if hard else ''))
        if hard and n:
            bad += 1
        if a.verbose and n:
            for x in fails[k][:20]:
                print('      %s' % (x if isinstance(x, str) else os.path.basename(x)))
    for p, want, got, lv in (fails['D_mismatch'][:20] if a.verbose else []):
        print('  D  %s  文件名 dept=%r  实测 %r (%s)' % (os.path.basename(p), want, got, lv))
    print('\n结果: %s' % ('PASS' if not bad else 'FAIL(%d 个硬不变量被破坏)' % bad))
    return 0 if not bad else 1


if __name__ == '__main__':
    sys.exit(main())
