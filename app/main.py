# -*- coding: utf-8 -*-
"""挂号推荐运行期原型 — 入口（PC 优先, 目标手机端）

用法:
  python -m app.main --demo red_flag      # 一条命令跑完 demo（胸痛/红旗）
  python -m app.main --demo chronic       # 高血压复诊（--loc-text geocode stub）
  python -m app.main --demo peds          # 儿童生长发育
  python -m app.main --loc "121.4737,31.2304" --symptom "胸痛"   # 交互式（自由文本 ≤3 轮）
  python -m app.main --loc-text "徐汇区宜山路600号" --symptom "高血压复诊"   # 唯一 LLM 后端: Agent Service 直出权重向量+目标科室
  python -m app.main --loc-text "徐汇区宜山路600号" --symptom "高血压复诊" --llm-model Qwen-local2  # 换 model_id
  python -m app.main --check              # 只跑回归断言（对 out/*/result.json 逐例 A/B/C/D, 失败非零退出）
  python -m app.main --loc "121.4737,31.2304" --symptom "胸痛" --non-interactive  # 管道/CI 安全
  python -m app.main --loc-text "徐家汇" --symptom "牙疼" --spec-file spec.json --non-interactive
      spec.json（Web 面板产出, 简化表单）: {"symptom_text": "...", "patient": {...} | null,
      "patient_selected": true/false, "fee_expect": "auto|zero|ten|hundred|thousand|tenk|any",
      "metro_priority": false} —— 时效/层次/费用上限不再由表单给, 交给 LLM 从描述解析。
纯标准库、零第三方依赖；无 profile 文件时自动降级（stub 并标注 slot_source）。
非 TTY（管道/CI）下不会调用 input()：有 --symptom 即用默认值, 否则给可操作提示（不裸 traceback）。
2026-10-04: 推理层唯一 LLM 后端 = Agent Service（--llm rules/ollama 已废除/并入）:
  LLM 直出 特征权重向量+目标科室（含归一化科室词表+医院目录基础数据上下文）;
  Agent Service 不可用 → 受控退出（不静默回退规则）。
"""
import argparse
import datetime as dt
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

from . import book, candidates, dialogue, finalize_llm, infer, locate
from . import llm_client
from . import slots as slots_mod
from . import recommend

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEMO_ALIASES = {
    'chest_pain': 'red_flag', 'red_flag': 'red_flag', 'chest': 'red_flag',
    'hypertension': 'chronic', 'chronic': 'chronic', 'chronic_followup': 'chronic',
    'peds': 'peds', 'peds_growth': 'peds', 'growth': 'peds',
}


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description='挂号推荐运行期原型（纯标准库）')
    ap.add_argument('--demo', help='demo 用例: red_flag/chronic/peds（别名 chest_pain/hypertension/peds_growth）')
    ap.add_argument('--symptom', help='症状自由文本（交互模式, 省去第1轮问答）')
    ap.add_argument('--spec-file', default=None,
                    help='结构化病例 JSON（web/表单入口, 与交互解析同一 schema）: '
                         '{symptom_text, patient, patient_selected, fee_expect'
                         '(auto|zero|ten|hundred|thousand|tenk|any), metro_priority}; '
                         '旧字段 age/gender/urgency_days/visit_type/price_cap_cny/'
                         'level_expectation/proxy 仍兼容直传')
    ap.add_argument('--loc', help='"lng,lat"（GCJ-02）; 代挂号主场景: 人为指定位置')
    ap.add_argument('--loc-text', help='地址文本（优先级: 高德 place/text → 高德 geocode/geo → 内置 stub 小表兜底）')
    ap.add_argument('--loc-adb', action='store_true', help='从 ADB dumpsys location 取位置（需 adb）')
    ap.add_argument('--no-network', action='store_true',
                    help='强制离线: --loc-text 不发高德请求, 只查内置 stub 小表（位置为内置样例坐标, 非真实定位）')
    ap.add_argument('--llm-model', default='default',
                    help='LLM model_id（唯一后端 = Agent Service; 默认 default, 服务端按 label 解析）')
    ap.add_argument('--agentsvc-base', default=None,
                    help='Agent Service base URL（默认按 RUNTIME_SERVER_URL/AGENTS_URL 环境变量, '
                         '否则 http://127.0.0.1:7988）')
    ap.add_argument('--device', help='ADB 设备序列号（--live 需要）')
    ap.add_argument('--live', action='store_true',
                    help='允许对缺失/TTL 过期的 (院,科) 批量触发实时抓取（需 --device; v2 --condition 统一接口, 一次 adb 会话覆盖全部条件组; 采集深度见 --live-slot-type）')
    ap.add_argument('--live-slot-type', default=None, choices=[None, 'expert', 'normal', 'specialty'],
                    help='--live 采集深度: 缺省=全部子 tab(专家/普通/专病, 最全但最慢, 约 5-15 分钟/科室); '
                         'expert=仅专家门诊(快速档, 约 1/3 耗时)')
    ap.add_argument('--live-timeout', type=int, default=None,
                    help='live 批量实抓超时上限(秒, 默认 3600=60分钟): 到点 v2 优雅停止(不跑后续组), 已完成组增量落盘保留')
    ap.add_argument('--probe-n', type=int, default=None,
                    help='Phase A 快速探测(probe)覆盖的候选院数(默认 12; L2 单页信号, 不逐医生)')
    ap.add_argument('--deep-n', type=int, default=None,
                    help='Phase B 深核(deep)院数=初排前 N 名(默认 5; 逐医生六字段, 终裁素材)')
    ap.add_argument('--radius', type=float, default=None, help='覆盖场景距离半径(km)')
    ap.add_argument('--top', type=int, default=20, help='人工甄别列表行数')
    ap.add_argument('--no-book', action='store_true', help='跳过模拟挂号步骤')
    ap.add_argument('--out', default=os.path.join(APP_DIR, 'out'))
    ap.add_argument('--non-interactive', action='store_true',
                    help='禁用交互追问: 必须配 --symptom, 其余轮次取默认值（非 TTY 时会自动进入该模式）')
    ap.add_argument('--check', action='store_true',
                    help='只跑回归断言（对 out/*/result.json 逐例校验 A/B/C/D, 任一失败非零退出）, 不跑流水线')
    ap.add_argument('--trace', action='store_true', help='出错时打印完整 traceback（默认只给可操作提示）')
    return ap.parse_args(argv)


def run_checks(args):
    """对已落盘的 out/<case>/result.json 逐例跑硬断言（B4 回归）。"""
    import glob
    paths = sorted(glob.glob(os.path.join(args.out, '*', 'result.json')))
    if not paths:
        raise SystemExit('未找到 %s/*/result.json; 先跑一个 case（如 --demo red_flag）再 --check' % args.out)
    all_ok = True
    for p in paths:
        case = os.path.basename(os.path.dirname(p))
        with open(p, encoding='utf-8') as fh:
            data = json.load(fh)
        checks = recommend.validate_result(data)
        ok = all(c['ok'] for c in checks)
        all_ok = all_ok and ok
        print('[check] %s: %s' % (case, 'PASS' if ok else 'FAIL'))
        for c in checks:
            line = '   %s %s' % ('PASS' if c['ok'] else 'FAIL', c['name'])
            if c['detail'] and not c['ok']:
                line += ' | ' + c['detail']
            print(line)
    print('[check] 汇总: %s（%d 个 case）' % ('全部通过' if all_ok else '存在失败', len(paths)))
    return 0 if all_ok else 1


def _demo_location(case):
    if case.get('loc'):
        d = locate.parse_loc(case['loc'])
        if case.get('loc_note'):
            d['note'] = case['loc_note']
        return d
    if case.get('loc_text'):
        return locate.StubGeocoder().resolve(case['loc_text'])
    raise SystemExit('demo 用例缺少 loc/loc_text')


def _best_info(infos):
    """医院代表号源科室: 已核实有号 > 已核实无号 > 未核实（事实优先, 2026-10-06）。
    9dc8 教训: 旧规则下未核实(level_default, ed=p50=5.0 估计) 与 已核实 no_slot(ed=None→99)
    同分, 未核实者凭 ed 胜出, 导致已核实无号事实（华山 耳鼻喉科 no_slot）被未核实的兄弟科室
    （声带疾病专病门诊, live error）掩盖。已核实事实（即使为负）必须优先于未核实估计——
    铁律"未核实不决定生死"约束的是未核实一侧, 不是已核实一侧。"""
    def rank(i):
        st = i.get('status')
        verified = bool(i.get('verified'))
        if verified and st == 'ok':
            score = 3
        elif verified:
            score = 2
        else:
            score = 1
        ed = i.get('earliest_days')
        return (score, -(ed if isinstance(ed, (int, float)) else 99), i.get('dept_id') or '')
    return sorted(infos, key=rank, reverse=True)[0]


def run_pipeline(args, case_id=None, out_subdir=None):
    """case_id: demo 用例 id (剧本重放); out_subdir: 自定义落盘子目录 (web job 等). 二者互斢。"""
    out_dir = os.path.join(args.out, case_id or out_subdir or 'interactive')
    os.makedirs(out_dir, exist_ok=True)

    def log(msg):
        print(msg)

    meta = {'generated_at': dt.datetime.now().isoformat(timespec='seconds'),
            'mode': ('demo:%s' % case_id) if case_id else ('web' if str(out_subdir or '').startswith('web_') else 'interactive'),
            'llm': 'agentsvc(model_id=%s)' % args.llm_model,
            'app': '挂号推荐运行期原型 v0.2（PC 原型, 目标手机端）'}

    # 1) 位置
    if case_id:
        demo = dialogue.load_demo(case_id)
        loc = _demo_location(demo)
    else:
        try:
            loc = locate.locate(args)
        except locate.GeocodeError as e:
            raise SystemExit('【1 位置】解析失败: %s' % e)
    log('【1 位置】%s, %s（%s）— %s' % (loc['lng'], loc['lat'], loc['crs'], loc.get('note', '')))
    if loc.get('is_stub') or str(loc.get('method', '')).startswith('stub'):
        log('   ⚠ 位置为内置样例坐标，非真实定位（method=%s；无 AMAP_KEY / --no-network / demo 固定点）' % loc.get('method'))

    # 2) 病情采集（demo 脚本重放 / 交互 ≤3 轮）
    if case_id:
        spec = dialogue.parse_case(demo['answers'])
        log('【2 病情】(demo 脚本答案重放, 与交互同一解析路径)')
    elif getattr(args, 'spec_file', None):
        with open(args.spec_file, encoding='utf-8') as fh:
            spec = dialogue.spec_from_dict(json.load(fh))
        log('【2 病情】(--spec-file 结构化字段, 与交互解析同一 schema)')
    else:
        spec = dialogue.run_interactive(prefill=args.symptom, non_interactive=args.non_interactive)
    log('   症状: %s' % spec['symptom_text'])
    log('   患者=%s(age=%s, 性别=%s) 代挂=%s 就诊=%s 等待≤%d天 费用上限=%s 水平预期=%s' % (
        spec['patient']['age_band'], spec['patient']['age'],
        {'male': '男', 'female': '女'}.get(spec['patient']['gender'], '未提供'),
        spec['patient']['proxy'],
        spec['visit_type'], spec['urgency_days'], spec['prefs']['price_cap_cny'],
        spec['prefs']['level_expectation']))
    _pt = spec['patient']
    if _pt.get('name_masked'):      # 表单选了随申办就诊人 -> 显示来源与性别推断依据（可追溯）
        log('   就诊人=%s · 来源=%s · 医保卡=%s · 性别依据=%s' % (
            _pt['name_masked'], _pt.get('patient_source') or '-',
            '有' if _pt.get('insurance_card') else '无', _pt.get('gender_source') or '-'))
    _fee = spec['prefs'].get('fee_expect')
    if _fee:                        # 预期费用尺度条（web 表单）; 非 auto 时已覆盖 price_cap_cny
        _cap = spec['prefs'].get('fee_expect_cny')
        log('   预期费用档位=%s（%s → %s）' % (
            _fee, spec['prefs'].get('fee_expect_label') or _fee,
            ('硬上限 %s 元' % _cap) if _cap is not None else '不设硬上限, 由 LLM 从描述判断'))
    for n in spec['notes']:
        log('   · %s' % n)

    # 3) 权重推理（唯一 LLM 后端 = Agent Service: LLM 直出 特征权重向量 + 目标科室, 含基础数据上下文）
    stage1 = candidates.Stage1()
    try:
        inf = infer.infer_llm(spec, stage1, loc_note=loc.get('note'),
                              model=args.llm_model, base=args.agentsvc_base)
    except (llm_client.AgentsvcError, RuntimeError) as e:
        raise SystemExit('【3 权重推理】LLM 不可用或输出未通过校验: %s\n'
                         '   rules 回退已废除: 请确认 Agent Service 在运行（默认 http://127.0.0.1:7988, '
                         '或设 RUNTIME_SERVER_URL / --agentsvc-base）, 或症状描述更具体, 然后重跑。' % e)
    m = inf['llm_meta']
    log('   [LLM] agentsvc model_id=%s @ %s 耗时=%sms 首字节=%sms 结构重试=%s 思维链=%s字(仅长度) '
        'prompt=%s字符(含基础数据上下文) session_id=%s(仅记录)'
        % (m['model_id'], m['base'], m.get('elapsed_ms'), m.get('first_byte_ms'), m.get('attempts'),
           m.get('thinking_chars'), m.get('prompt_chars'), m.get('session_id')))
    log('   权重向量(绝对, 行和=1.0): %s' % json.dumps(inf['weights'], ensure_ascii=False))
    _tier_zh = {1: '首选', 2: '相关', 3: '弱相关'}
    _tiers = inf.get('llm_dept_tiers') or {}
    log('   目标科室(LLM 直选, 归一化词表校验, how=%s): %s' % (
        inf['dept_how'],
        ', '.join('%s[%s]' % (d, _tier_zh.get(_tiers.get(d), '首选')) for d in inf['llm_depts']) or '(无)'))
    cxt = inf['context']
    log('   基础数据上下文: %d 科室(归一化 canonical, 覆盖≥%d 家医院) / %d 家医院(等级/区县) — 只读' % (
        cxt['n_depts'], cxt['dept_hc_min'], cxt['n_hospitals']))
    log('【3 权重推理】scenario=%s（%s）来源=%s' % (inf['scenario'], inf.get('scenario_zh', ''), inf['llm_mode']))
    for r in inf['reasoning']:
        log('   · %s' % r)

    # 2026-10-06: 年龄未提供且 LLM scenario=peds → 患者层按 child 处理(不再默认 adult):
    # 主诉文本由 LLM 通读(如"孩子咽喉不适…倾向儿童专属医院"), 结构化表单 age 空值
    # 不应覆盖 LLM 的人群判断(否则儿科科室/儿童医院被患者层过滤误删)。
    if spec['patient']['age'] is None and inf['scenario'] == 'peds' and spec['patient']['age_band'] != 'child':
        log('   ⚠ 患者层修正: 年龄未提供(原默认 %s) 但 LLM scenario=peds(儿科) → 按 child 处理' % spec['patient']['age_band'])
        spec['patient']['age_band'] = 'child'
        spec['patient']['is_child'] = True

    # 4) 候选集（stage-1）
    symptom = inf['triage_term'] or spec['symptom_text']
    res = stage1.run_llm(symptom, [(d, (inf.get('llm_dept_tiers') or {}).get(d, 1)) for d in inf['llm_depts']],
                         inf['scenario'], [loc['lng'], loc['lat']],
                         radius_km=args.radius, weights=inf['weights'],
                         patient_band=spec['patient']['age_band'], dept_how=inf['dept_how'],
                         patient_gender=spec['patient'].get('gender'))
    if res is None:
        log('   ⚠ LLM 直选科室 + 症状词表解析均无目标科室: 请用更具体的症状描述重试。')
        raise SystemExit(2)
    if res.get('patient_filter'):
        log('   患者层科室过滤: %s' % res['patient_filter']['note'])
    _fam = [t for t in res['triage']['targets'] if t.get('family_of')]
    if _fam:
        log('   同族 canonical 扩展(只读, 补偿碎片化失召回): ' +
            '; '.join('%s[族员 of %s, tier%d]' % (t['canonical'], t['family_of'], t['priority']) for t in _fam[:6]))
    if res['triage']['term'] is None or not res['candidates']:
        log('   ⚠ triage 未命中任何目标科室（主词="%s"）: 请换更具体的症状描述（如"胸痛""高血压"）后重试。'
            % res['triage']['term'])
        raise SystemExit(2)
    queries = stage1.slot_queries(res, inf, [loc['lng'], loc['lat']])
    hf = res['hard_filter']
    log('【4 候选集】主词="%s"(%s) 目标科室=%d K=%s 硬过滤: 召回=%d 通过=%d 砍=%d' % (
        res['triage']['term'], res['triage']['how'], len(res['triage']['targets']),
        res['K'], hf['recall'], hf['passed'], hf['dropped']))
    log('   应用权重: %s' % json.dumps(res['applied_weights'], ensure_ascii=False))
    for c in res['candidates'][:10]:
        log('   %2d %s %-16s %-8s %-8s static=%s' % (
            c['rank'], c['hospital_id'], c['name'][:16], c['level'], c['district'], c['score']))
    if res.get('patient_filter') and res['patient_filter'].get('dropped_hospitals'):
        log('   医院级剔除: ' + '; '.join('%s[%s]' % (x['name'], x['reason']) for x in res['patient_filter']['dropped_hospitals'][:4]))

    # 5) 号源（两阶段 live, 2026-10-06 架构: Phase A probe 快筛初排 -> Phase B deep 前 N 深核 -> Phase C 终裁 LLM）
    # 用户指令: 有号是独立高权重特征; 粗排(有无号源/类型/日期)要在一小时内覆盖几十家 => probe 不做逐医生下钻;
    # 深核只给初排前 N 名(医生/价格/时间段明细); 最终取舍走第 2 次 LLM 推理。
    resolver = slots_mod.SlotResolver(allow_live=args.live, device=args.device, log=log,
                                       slot_type=args.live_slot_type,
                                       live_timeout_s=args.live_timeout)
    _total_budget = int(args.live_timeout or 3600)
    _probe_n = max(1, getattr(args, 'probe_n', None) or 12)
    _deep_n = max(1, getattr(args, 'deep_n', None) or 5)
    _probe_budget = max(300, int(_total_budget * 0.55)) if args.live else 0

    def _prep_primary(cands_n):
        """top N 候选院, 每院只取主科室(tier 最高) = 推荐行的挂号入口(9dc8 教训: 多科室扩展吹爆批次)。"""
        _out = []
        for c in res['candidates'][:cands_n]:
            _hid = c['hospital_id']
            _tds = stage1.hospital_target_depts(_hid, res)
            if _tds:
                _t = _tds[0]
                _out.append((_hid, _t['dept_id'], _t['canonical']))
        return _out

    def _resolve_all():
        """全候选集 resolve(纯缓存/字典查找, 零设备开销; 不设 12 家预算——no_slot 事实必须全量保留, 14:24 教训)。"""
        _sbh, _nok, _nns = {}, 0, 0
        for c in res['candidates']:
            hid = c['hospital_id']
            rec = stage1.norms['hospitals'][hid]
            tds = stage1.hospital_target_depts(hid, res)
            infos = []
            for t in tds:
                info = resolver.resolve(hid, t['dept_id'], t['canonical'], rec['level'], rec['type'],
                                        l1_cat=stage1.dept_l1(t['dept_id']))
                info['dept_name'] = info.get('dept_name') or t['canonical']
                infos.append(info)
            if not infos:
                infos = [resolver.resolve(hid, 'UNKNOWN', '目标科室', rec['level'], rec['type'])]
            best = _best_info(infos)
            _sbh[hid] = {'best': best, 'all': infos}
            if best.get('verified') and best.get('status') == 'ok':
                _nok += 1
            elif best.get('verified') and best.get('status') == 'no_slot':
                _nns += 1
        log('【5 号源】处理 %d 家: 已核实有号 %d / 已核实无号 %d / 未核实 %d（来源标注见报告 §5/§7）'
            % (len(_sbh), _nok, _nns, len(_sbh) - _nok - _nns))
        return _sbh

    if resolver.allow_live:
        log('【5 号源·Phase A】快速探测 probe: 候选前 %d 院主科室, L2 单页(按钮/日期条/空态, 不逐医生点), 预算 %d 分钟'
            % (_probe_n, _probe_budget // 60))
        resolver.prepare_probe(_prep_primary(_probe_n), budget_s=_probe_budget)
    slot_by_hid = _resolve_all()
    final = recommend.finalize(res['candidates'], slot_by_hid, inf, top_n=args.top)
    if resolver.allow_live:
        # Phase B: deep 深核 —— 初排前 _deep_n 名(两级排序: 已核实有号在前)逐医生六字段, 终裁素材
        _deep_prep = []
        for _row in final['top_list'][:_deep_n]:
            _hid = _row['hospital_id']
            _tds = stage1.hospital_target_depts(_hid, res)
            if _tds:
                _t = _tds[0]
                _deep_prep.append((_hid, _t['dept_id'], _t['canonical']))
        _deep_budget = max(300, _total_budget - _probe_budget)
        log('【5 号源·Phase B】深核 deep: 初排前 %d 名(%s), 逐医生六字段, 预算 %d 分钟'
            % (len(_deep_prep), ', '.join(_row['hospital_id'] for _row in final['top_list'][:_deep_n]),
               _deep_budget // 60))
        resolver.prepare_live(_deep_prep, budget_s=_deep_budget)
        slot_by_hid = _resolve_all()
        final = recommend.finalize(res['candidates'], slot_by_hid, inf, top_n=args.top)
        # Phase C: 终裁 LLM（全流程第 2 次推理）—— 明细=deep 已核实有号行, 摘要=其余 probe 已核实行
        _hav_c = stage1.sh.haversine_km
        _lng0_c, _lat0_c = float(loc['lng']), float(loc['lat'])
        _detailed, _probe_sum = [], []
        for _row in final['top_list']:
            _info = (slot_by_hid.get(_row['hospital_id']) or {}).get('best') or {}
            _src = _info.get('slot_source')
            if _src == 'live' and _info.get('verified') and _info.get('status') == 'ok':
                _rec3 = (stage1.norms.get('hospitals') or {}).get(_row['hospital_id']) or {}
                _rt3 = resolver._dept_route(_row['hospital_id'], _info.get('dept_id')) if _info.get('dept_id') else None
                _detailed.append({
                    'hospital_id': _row['hospital_id'], 'name': _row['name'],
                    'level': _row.get('level'), 'district': _row.get('district'),
                    'distance_km': (round(_hav_c(_lng0_c, _lat0_c, float(_rec3['lng']), float(_rec3['lat'])), 1)
                                    if _rec3.get('lng') else None),
                    'dept': _info.get('dept_name'), 'dept_tab': _rt3[0] if _rt3 else None,
                    'slot_types': sorted(set(s.get('sub_tab') for s in (_info.get('slot_samples') or [])
                                             if s.get('sub_tab'))) or (_info.get('bookable_subtabs') or []),
                    'earliest_date': _info.get('earliest_date'), 'earliest_days': _info.get('earliest_days'),
                    'fee_tier': _info.get('fee_tier'), 'fee_tiers': _info.get('fee_tiers'),
                    'doctors': _info.get('doctors') or [], 'slot_samples': _info.get('slot_samples') or [],
                })
            elif _src == 'live_probe' and _info.get('verified'):
                _probe_sum.append({'hospital_id': _row['hospital_id'], 'name': _row['name'],
                                   'status': _info.get('status'), 'earliest_date': _info.get('earliest_date'),
                                   'slot_types': _info.get('bookable_subtabs') or []})
        _llm_final = finalize_llm.run_final(spec, inf, _detailed, _probe_sum,
                                            model=args.llm_model, base=args.agentsvc_base,
                                            log=log, loc_text=(loc.get('note') or ''))
        if _llm_final:
            final = recommend.apply_llm_picks(final, slot_by_hid, inf, _llm_final)
            log('【6 终裁LLM】已应用: ' + '; '.join('#%d %s%s' % (p['rank'], p['hospital_id'],
                                                                  ('[' + p['doctor'] + ']') if p['doctor'] else '')
                                                   for p in _llm_final['picks']))
        else:
            log('【6 终裁LLM】未应用(无明细/调用失败/校验不过) → 推荐=确定性 top3(两级排序)')
    final['live_coverage'] = {'probe': getattr(resolver, 'probe_coverage', None),
                              'deep': getattr(resolver, 'live_coverage', None)}
    # 后处理: 挂号入口(tab/raw 行名) + 直线距离(km), 供 web 表格 科室/距离 列与 显式排除 原因使用
    _hav = stage1.sh.haversine_km
    _lng0, _lat0 = float(loc['lng']), float(loc['lat'])
    for _r in list(final['top_list']) + list(final['excluded']) + list(final['recommendations']):
        _hid = _r['hospital_id']
        _info = slot_by_hid.get(_hid, {}).get('best') or {}
        _did = _info.get('dept_id')
        if _did and _did != 'UNKNOWN':
            _rt = resolver._dept_route(_hid, _did)
            if _rt:
                _r['dept_tab'], _r['dept_raw'] = _rt
        _rec = (stage1.norms.get('hospitals') or {}).get(_hid) or {}
        if _rec.get('lng') and _rec.get('lat'):
            _r['lng'], _r['lat'] = float(_rec['lng']), float(_rec['lat'])
            _r['distance_km'] = round(_hav(_lng0, _lat0, _rec['lng'], _rec['lat']), 1)
    for _r in final['excluded']:
        # 显式排除: “实查…”的原因必须说明查的是哪个 tab/科室行(用户要求)
        if _r.get('dept_tab') and _r.get('reason') and _r['reason'].startswith('实查'):
            _rs = '「%s」tab → 「%s」行' % (_r['dept_tab'], _r.get('dept_raw') or '')
            _r['reason'] = '实查' + _rs + _r['reason'][len('实查'):]
    log('【6 推荐】终排=静态分×号源效用; 已核实 %d/%d%s' % (
        final['n_verified'], final['n_total'],
        '（全部未核实 → §4.5 回落静态排序, 显式标注“号源未核实”）' if final['fallback_all_unverified'] else ''))
    for i, r in enumerate(final['recommendations'], 1):
        log('   %d) %s [%s/%s] 终排=%s（静态 %s × 号源 %s）' % (
            i, r['name'], r['level'], r['district'], r['final_score'], r['static_score'], r['slot_utility']))
        log('      why: %s' % r['why'])
        if r.get('dept_tab'):
            log('      挂号入口: 「%s」tab → 点「%s」行（院页面 raw 名, 同 tab 可能有相似名科室）' % (
                r['dept_tab'], r.get('dept_raw') or ''))

    # 7) 模拟挂号（dry-run 默认开）
    booking = None
    if not args.no_book and final['recommendations']:
        top = final['recommendations'][0]
        booking = book.run(top, slot_by_hid.get(top['hospital_id'], {}).get('best'),
                           patient=spec['patient'])
        log('【7 模拟挂号】dry-run（默认开）: 不真发、不提交、不支付')
        for s in booking.get('steps', []):
            log('   ' + s)
        if booking.get('refused'):
            log('   ' + booking['refused'])

    # 候选行补坐标(GCJ-02), 供 web 距离列点击弹高德地图
    for _c in res['candidates']:
        _rec2 = (stage1.norms.get('hospitals') or {}).get(_c.get('hospital_id')) or {}
        if _rec2.get('lng') and _rec2.get('lat'):
            _c['lng'], _c['lat'] = float(_rec2['lng']), float(_rec2['lat'])

    # 8) 回归断言（B4 硬断言, 每次运行必跑; 任一失败 → 非零退出）
    checks = recommend.validate_result(
        {'final': final, 'candidates_static': res['candidates'], 'slot_queries': queries})
    for c in checks:
        log('【8 断言】%s %s%s' % ('PASS' if c['ok'] else 'FAIL', c['name'],
                                   (' | ' + c['detail']) if (c['detail'] and not c['ok']) else ''))

    # 9) 落盘
    result = {
        'meta': meta, 'loc': loc, 'spec': spec, 'inference': inf,
        'stage1': {k: res[k] for k in ('symptom', 'scenario', 'triage', 'applied_weights',
                                       'hard_filter', 'K', 'K_rule', 'patient_filter')
                   if k in res},
        'slot_queries': queries,
        'slots': {h: v['best'] for h, v in slot_by_hid.items()},
        'final': final,
        'booking': booking,
        'candidates_static': res['candidates'],
        'checks': checks,
    }
    meta['started_at'] = meta.pop('generated_at')
    meta['generated_at'] = dt.datetime.now().isoformat(timespec='seconds')
    rp = os.path.join(out_dir, 'result.json')
    with open(rp, 'w', encoding='utf-8') as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1, default=str)
    report = recommend.render_report(meta, loc, spec, inf, res, slot_by_hid, final, booking,
                                     queries, checks=checks)
    mp = os.path.join(out_dir, 'report.md')
    with open(mp, 'w', encoding='utf-8') as fh:
        fh.write(report)
    log('【9 落盘】%s / %s' % (os.path.relpath(rp), os.path.relpath(mp)))
    if not all(c['ok'] for c in checks):
        log('【回归断言失败】见上方 FAIL 行; 完整结果已落盘 %s' % os.path.relpath(rp))
        return 2
    return 0


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.check:
            return run_checks(args)
        case_id = DEMO_ALIASES.get(args.demo) if args.demo else None
        if args.demo and not case_id:
            raise SystemExit('未知 --demo: %s（可选: %s）' % (args.demo, ', '.join(sorted(set(DEMO_ALIASES)))))
        if not case_id and not (args.loc or args.loc_text or args.loc_adb):
            raise SystemExit('未指定位置: 用 --loc "lng,lat" / --loc-text "地址" / --loc-adb, 或 --demo <case>')
        return run_pipeline(args, case_id=case_id)
    except SystemExit as e:
        # 受控退出（参数错误/断言失败等）: 保留一行可操作信息, 不裸 traceback
        code = e.code
        if not isinstance(code, int):
            print(str(code) if code is not None else '退出')
            raise SystemExit(1)
        raise
    except KeyboardInterrupt:
        print('\n[中断] 用户取消（Ctrl-C）。未发起任何真实挂号; 重新运行同一条命令即可继续。')
        return 130
    except EOFError:
        print('\n[错误] stdin 提前关闭（EOF）——非交互环境请补 --symptom "症状"（或加 --non-interactive）; '
              'demo 用 --demo red_flag/chronic/peds。')
        return 1
    except Exception as e:
        if getattr(args, 'trace', False):
            raise
        print('\n[错误] 未预期异常 %s: %s' % (type(e).__name__, str(e)[:300]))
        print('[提示] 可操作选项: ① 换 --demo red_flag/chronic/peds 验证环境; ② 加 --no-network 排除高德网络问题; '
              '③ 需要完整堆栈时加 --trace。')
        return 3


if __name__ == '__main__':
    sys.exit(main())
