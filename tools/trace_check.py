#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""证据链自动复核(④ 的"可被自动复核"这一半)。

把设备上 out/trace/<ts>/ 里的每一帧 XML 用【现在的观测层】重新读一遍, 和当时记录的
expected / ok 对账:

  * 声明 ok=True 但重新观测不支持  => 矛盾(HARD): 当时其实没到, 后面所有结论都不可信
  * 声明 ok=False 但重新观测支持  => 可疑: 当时其实到了, 却被当成失败(白白重试/熔断)
  * 观测层升级后重读结果和当时记录不一致 => drift(提示, 不判死)

并支持 --ask 直接回答业务问题, 例如:
  --ask subtab=普通门诊   => 证据链里到底有没有一帧"真的选中了普通门诊"

用法:
  python tools/trace_check.py D:/tmp/traces/20261016T001122_cond [--ask k=v ...] [--quiet]
退出码: 0 = 证据与声明一致; 1 = 有矛盾/可疑(至少一条对不上)
"""
import argparse
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from slots import page_state as PS          # noqa: E402
from slots import nav_machine as NM         # noqa: E402
from slots import six_fields as SF          # noqa: E402


def observe(xml_text):
    """唯一观测入口(和端上 obs_of 完全一致, 不自己另写一套判据)。"""
    return PS.observe(xml_text, six_match=lambda x: bool(SF.match_six_page(x)))


def load_events(trace_dir):
    fp = os.path.join(trace_dir, 'events.jsonl')
    if not os.path.exists(fp):
        raise SystemExit('找不到 events.jsonl: %s' % fp)
    out = []
    for line in io.open(fp, encoding='utf-8'):
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def recheck(ev, trace_dir):
    """返回 (verdict, obs, diffs, note)。
    verdict ∈ ok / unverified(页面承载不了该字段, 无法核实) / contradiction(证据反驳声明)
             / suspect(声明失败但证据支持) / nodump。
    """
    dump = ev.get('dump')
    if not dump:
        return 'nodump', None, [], '该事件没存 XML'
    fp = os.path.join(trace_dir, dump.replace('/', os.sep))
    if not os.path.exists(fp):
        return 'nodump', None, [], '存档缺帧: %s' % dump
    txt = io.open(fp, encoding='utf-8', errors='replace').read()
    obs = observe(txt)
    exp = dict(ev.get('expected') or {})
    if isinstance(exp.get('level'), list):
        exp['level'] = tuple(exp['level'])
    pruned = NM.prune_for_level(exp, obs.level)      # 该级别承载不了的字段不参与判定
    ok, diffs = PS.matches(pruned, obs, strict=True)  # 严格判据: 名字必须真在页面身份位上
    claimed = bool(ev.get('ok'))
    # 当时记录的 observed brief 里的 level, 和现在重读的 level 比对(观测层漂移)
    old_level = ''
    m = re.search(r"level=([A-Za-z0-9_]+)", ev.get('observed') or '')
    if m:
        old_level = m.group(1)
    note = ''
    if old_level and old_level != obs.level:
        note = 'drift: 当时记 %s, 现在重读 %s' % (old_level, obs.level)
    # 软字段(页面根本没显示这个信号)只算"无法核实", 不算"证据反驳" —— 与 act 的口径一致
    hard = [d for d in (diffs or []) if not str(d).startswith('\u2248')]
    soft = [d for d in (diffs or []) if str(d).startswith('\u2248')]
    if soft and not note:
        note = '无法核实: %s' % '; '.join(str(x)[:60] for x in soft)
    if claimed and hard:
        return 'contradiction', obs, hard, note
    if (not claimed) and ok:
        return 'suspect', obs, diffs, note
    if claimed and soft and not hard:
        return 'unverified', obs, soft, note
    return 'ok', obs, diffs, note


FIELD_ALIAS = {'subtab': 'subtab_active', 'hospital': 'hospital', 'level': 'level',
               'dept': 'dept', 'tab': 'tab', 'doctor': 'doctor', 'day': 'day_selected'}


def ask(trace_dir, events, key, val):
    """证据链里到底有没有一帧支持 key=val(严格名字判定)。"""
    attr = FIELD_ALIAS.get(key, key)
    hits = []
    for ev in events:
        dump = ev.get('dump')
        if not dump:
            continue
        fp = os.path.join(trace_dir, dump.replace('/', os.sep))
        if not os.path.exists(fp):
            continue
        obs = observe(io.open(fp, encoding='utf-8', errors='replace').read())
        got = getattr(obs, attr, None)
        if got and (val == got or val in str(got)):
            hits.append((ev.get('n'), dump, str(got)))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('trace_dir')
    ap.add_argument('--ask', action='append', default=[],
                    help='k=v, 例如 subtab=普通门诊; 可重复')
    ap.add_argument('--quiet', action='store_true', help='只打印矛盾/可疑')
    args = ap.parse_args()

    events = load_events(args.trace_dir)
    tally = {'ok': 0, 'contradiction': 0, 'suspect': 0, 'nodump': 0, 'unverified': 0}
    hard = []
    print('证据链复核: %s (%d 个事件)' % (args.trace_dir, len(events)))
    for ev in events:
        verdict, obs, diffs, note = recheck(ev, args.trace_dir)
        tally[verdict] = tally.get(verdict, 0) + 1
        mark = {'ok': '  一致', 'contradiction': '!! 矛盾', 'unverified': ' ~ 未核实',
                'suspect': ' ? 可疑', 'nodump': '  无帧'}[verdict]
        if verdict != 'ok' or not args.quiet:
            print('  %s #%03d %-16s 声明ok=%-5s 重读: level=%-7s dept=%-8s subtab=%-8s tab=%-6s doctor=%s%s'
                  % (mark, ev.get('n') or 0, (ev.get('state') or '')[:16], ev.get('ok'),
                     getattr(obs, 'level', '?'),
                     (getattr(obs, 'dept', '') or '-')[:8],
                     (getattr(obs, 'subtab_active', '') or '-')[:8],
                     (getattr(obs, 'tab', '') or '-')[:6],
                     (getattr(obs, 'doctor', '') or '-'),
                     ' | ' + note if note else ''))
            if diffs:
                print('        diffs: %s' % json.dumps(diffs, ensure_ascii=False)[:200])
        if verdict in ('contradiction', 'suspect'):
            hard.append((verdict, ev.get('n'), ev.get('state'), ev.get('action'), diffs))

    for q in args.ask:
        if '=' not in q:
            continue
        k, v = q.split('=', 1)
        hits = ask(args.trace_dir, events, k, v)
        if hits:
            print('  提问 %s=%s => 有证据: %d 帧支持, 最早 #%s %s'
                  % (k, v, len(hits), hits[0][0], hits[0][1]))
        else:
            print('  提问 %s=%s => 没有任何一帧支持(证据链不支持这个说法)' % (k, v))

    print('小结: 一致=%d 矛盾=%d 可疑=%d 未核实=%d 无帧=%d'
          % (tally['ok'], tally['contradiction'], tally['suspect'],
             tally['unverified'], tally['nodump']))
    if hard:
        for verdict, n, state, action, diffs in hard:
            print('  %s: #%s %s(%s) diffs=%s'
                  % (verdict, n, state, action, json.dumps(diffs, ensure_ascii=False)[:160]))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
