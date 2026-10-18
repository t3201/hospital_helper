# -*- coding: utf-8 -*-
"""
fetch_slots_parity.py — PC↔APP slots/fetch_slots.py 一致性校验器
================================================================

背景:
  PC  侧  slots/fetch_slots.py                              (走 AdbRunner/subprocess/adb)
  APP 侧  mobile/controller-app/app/python/slots/fetch_slots.py  (走 devbridge)

两份文件**不是**逐字节相同: Adb 类 / Nav 导航 / IME 通道 / 入口 是合法平台差异。
但 **判定/取数/条件解析/杠杆** 等纯业务块本应一致 —— 本工具按「区块名」逐块比对,
只对「必须一致」的块要求归一化后 hash 相同, 其余登记进白名单。

用法:
  python tools/fetch_slots_parity.py                     # 常规, 退出码非 0 = 有未登记漂移
  python tools/fetch_slots_parity.py --diff <name>       # 输出指定区块的 unified diff
  python tools/fetch_slots_parity.py --json              # JSON 报告(供 CI 消费)
  python tools/fetch_slots_parity.py --no-whitelist      # 忽略 diff_ok/单边白名单, 展示全部原始漂移(自检用)
  python tools/fetch_slots_parity.py --stale <third.py>  # 额外把一份(如 build 副本)当 PC 侧, 检 NO_RESUME 类漏接线

白名单文件: tools/fetch_slots_parity.json
  must_same  : 名字列表 — 两边都必须在且归一化后一致
  diff_ok    : {名字: 原因} — 平台差异允许, 不参与 parity 检查
  pc_only    : {名字: 原因} — 只允许 PC 有
  app_only   : {名字: 原因} — 只允许 APP 有

退出码:
  0 = 全部通过(含白名单内条目均符合)
  1 = 有未登记的 (b) 意外漂移 / 未登记的单边块 / must_same 缺失
  2 = 白名单文件缺失或 JSON 解析失败
"""
import ast, hashlib, json, os, sys, difflib

PC  = 'slots/fetch_slots.py'
APP = 'mobile/controller-app/app/python/slots/fetch_slots.py'
WL  = 'tools/fetch_slots_parity.json'

# ---------------- block extraction ----------------

def _strip_docstrings(src):
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            if ast.get_docstring(node, clean=False) is not None and node.body:
                node.body = node.body[1:]
    try:
        return ast.unparse(tree)
    except Exception:
        return src

def extract_blocks(path):
    """Return dict: name -> (lineno, src, norm_hash). Covers top-level def/class, ALL-CAPS consts,
    and import statements (so one-sided imports like `import devbridge` are visible).
    Class methods are keyed 'ClassName.method'."""
    src = open(path, encoding='utf-8').read()
    tree = ast.parse(src)
    out = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            out[n.name] = _block(n, src)
            if isinstance(n, ast.ClassDef):
                for m in n.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out['%s.%s' % (n.name, m.name)] = _block(m, src)
        elif isinstance(n, ast.Assign):
            try:
                names = [t.id for t in n.targets if isinstance(t, ast.Name)]
                if names and all(x.isupper() for x in names):
                    out[names[0]] = _block(n, src)
            except AttributeError:
                pass
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            if isinstance(n, ast.Import):
                key = '__import__:' + ','.join(a.name for a in n.names)
            else:
                key = '__import__:%s' % (n.module or 'from')
            out[key] = _block(n, src)
    return out

def _block(node, src):
    seg = '\n'.join(src.split('\n')[node.lineno - 1:node.end_lineno])
    norm = _strip_docstrings(seg)
    return (node.lineno, seg, hashlib.sha256(norm.encode('utf-8')).hexdigest())

# ---------------- comparison core ----------------

def compare(pc, ap, wl, use_wl=True):
    must = set(wl.get('must_same', []))
    diff_ok = set(wl.get('diff_ok', {})) if use_wl else set()
    pc_only_wl = set(wl.get('pc_only', {})) if use_wl else set()
    app_only_wl = set(wl.get('app_only', {})) if use_wl else set()

    shared = set(pc) & set(ap)
    pc_one = set(pc) - set(ap)
    ap_one = set(ap) - set(pc)

    same, drift, wl_diff = [], [], []
    for name in sorted(shared):
        if pc[name][2] == ap[name][2]:
            same.append(name)
        elif name in diff_ok:
            wl_diff.append(name)
        else:
            drift.append(name)

    pc_unreg = sorted(n for n in pc_one if n not in pc_only_wl)
    ap_unreg = sorted(n for n in ap_one if n not in app_only_wl)
    pc_reg = sorted(n for n in pc_one if n in pc_only_wl)
    ap_reg = sorted(n for n in ap_one if n in app_only_wl)

    must_problems = []
    if use_wl:
        for name in sorted(must):
            if name not in pc or name not in ap:
                must_problems.append('must_same 块单边缺失: %s (PC=%s APP=%s)' % (name, name in pc, name in ap))
            elif name in drift:
                must_problems.append('must_same 块实际漂移: %s' % name)

    hygiene = {
        'diff_ok_but_identical': sorted(n for n in same if n in diff_ok),
        'pc_only_ghost': sorted(n for n in pc_only_wl if n in shared),
        'app_only_ghost': sorted(n for n in app_only_wl if n in shared),
    }

    return {
        'shared_total': len(shared),
        'pc_blocks': len(pc), 'app_blocks': len(ap),
        'same': same, 'drift': drift, 'whitelist_diff': wl_diff,
        'pc_only_registered': pc_reg, 'app_only_registered': ap_reg,
        'pc_only_unregistered': pc_unreg, 'app_only_unregistered': ap_unreg,
        'must_problems': must_problems,
        'whitelist_hygiene': hygiene,
    }

# ---------------- report ----------------

def print_report(report, wl, do_diff, pc, ap, use_wl, pc_path, ap_path):
    print('=' * 78)
    print('fetch_slots parity   PC=%d blocks  APP=%d blocks   (whitelist=%s)'
          % (report['pc_blocks'], report['app_blocks'], 'ON' if use_wl else 'OFF'))
    print('shared=%d  same=%d  drift(unreg)=%d  diff_ok(whitelisted)=%d'
          % (report['shared_total'], len(report['same']), len(report['drift']), len(report['whitelist_diff'])))
    print('pc_only: total=%d (registered %d, UNREGISTERED %d)'
          % (len(report['pc_only_registered']) + len(report['pc_only_unregistered']),
             len(report['pc_only_registered']), len(report['pc_only_unregistered'])))
    print('app_only: total=%d (registered %d, UNREGISTERED %d)'
          % (len(report['app_only_registered']) + len(report['app_only_unregistered']),
             len(report['app_only_registered']), len(report['app_only_unregistered'])))
    print('-' * 78)

    if report['drift']:
        print('UNREGISTERED DRIFT (b) — 本应一致但内容分叉:')
        for n in report['drift']:
            print('  ! %-28s  PC@%-5d APP@%-5d   (python tools/fetch_slots_parity.py --diff %s)'
                  % (n, pc[n][0], ap[n][0], n))
    else:
        print('no unregistered drift.')
    if report['must_problems']:
        print('MUST_SAME VIOLATIONS:')
        for p in report['must_problems']:
            print('  !', p)

    for grp, label in ((report['pc_only_registered'], 'PC-only (a 平台差异):'),
                       (report['app_only_registered'], 'APP-only (a 平台差异):'),
                       (report['pc_only_unregistered'], 'PC-only UNREGISTERED (c):'),
                       (report['app_only_unregistered'], 'APP-only UNREGISTERED (c):')):
        if grp:
            print(label)
            for n in grp:
                reason = (wl.get('pc_only', {}).get(n) or wl.get('app_only', {}).get(n) or '')
                print('  - %-28s %s' % (n, reason))

    if use_wl:
        h = report['whitelist_hygiene']
        if h['diff_ok_but_identical']:
            print('hygiene: diff_ok 登记但实际已一致(可移回 must_same):')
            for n in h['diff_ok_but_identical']:
                print('  *', n)
        for g in (h['pc_only_ghost'], h['app_only_ghost']):
            if g:
                print('hygiene: 单边登记但实际两边都有:')
                for n in g:
                    print('  *', n)
    print('-' * 78)
    failed = bool(report['drift'] or report['pc_only_unregistered']
                  or report['app_only_unregistered'] or report['must_problems'])
    print('VERDICT:', 'FAIL' if failed else 'PASS')
    print('=' * 78)

    if do_diff:
        if do_diff in pc and do_diff in ap:
            d = list(difflib.unified_diff(pc[do_diff][1].split('\n'), ap[do_diff][1].split('\n'),
                                          fromfile='PC:%s' % do_diff, tofile='APP:%s' % do_diff, n=2, lineterm=''))
            print('\n'.join(d[:400]) if d else '(identical)')
        elif do_diff in pc:
            print('block only on PC:\n' + pc[do_diff][1][:2000])
        elif do_diff in ap:
            print('block only on APP:\n' + ap[do_diff][1][:2000])
        else:
            print('--diff: block %r not found on either side' % do_diff)
    return 1 if failed else 0

def main():
    argv = sys.argv[1:]
    do_diff = argv[argv.index('--diff') + 1] if '--diff' in argv else None
    as_json = '--json' in argv
    use_wl = '--no-whitelist' not in argv
    stale = argv[argv.index('--stale') + 1] if '--stale' in argv else None

    pc_path = stale or PC
    try:
        wl = json.load(open(WL, encoding='utf-8'))
    except Exception as e:
        sys.exit('whitelist error: %s' % e)
    for k in ('must_same', 'diff_ok', 'pc_only', 'app_only'):
        wl.setdefault(k, {} if k != 'must_same' else [])

    pc = extract_blocks(pc_path)
    ap = extract_blocks(APP)
    report = compare(pc, ap, wl, use_wl)

    if as_json:
        report['pc_path'] = pc_path
        report['app_path'] = APP
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if stale:
            print('[stale check] PC side = %s (build 副本/历史版本)' % stale)
        rc = print_report(report, wl, do_diff, pc, ap, use_wl, pc_path, APP)
        sys.exit(rc)

if __name__ == '__main__':
    main()
