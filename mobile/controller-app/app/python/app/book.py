# -*- coding: utf-8 -*-
"""模拟挂号：--dry-run 默认开；只打印将执行的步骤序列。
不真发、不提交、不支付（450 家 capabilities.book=false, RANKING_SPEC §4.5 禁止事项）。
--execute 在原型中一律拒绝（安全边界, 不是可选开关）。"""


def plan_steps(rec, info, patient=None):
    date = (info or {}).get('earliest_date') or '待定（最早号源未核实, 到院/到页后再选）'
    dept = (info or {}).get('dept_name') or rec.get('dept') or '目标科室'
    tab = rec.get('dept_tab') or (info or {}).get('dept_tab')
    raw = rec.get('dept_raw') or (info or {}).get('dept_raw')
    if tab:
        step4 = ('4. L1 选科室页找到「%s」tab, 点「%s」行进入科室页'
                '（注意: 同 tab 下可能有多个含相似字样的科室, 以该行为准; L2 页自带日期条）' % (tab, raw or dept))
    else:
        step4 = '4. 科室页找到「%s」tab 并进入（L2 页自带 14 天日期条）' % dept
    if patient and patient.get('name_masked'):
        # 表单选了随申办「就诊人」→ 步骤里点名该就诊人（脱敏名/年龄/性别/医保卡），省一次找人的操作
        step7 = ('7. 选择就诊人「%s」（%s · %s · 医保卡=%s）+ 短信验证码（用户手动完成, 脚本不触碰凭据）' % (
            patient['name_masked'],
            ('%s岁' % patient['age']) if patient.get('age') is not None else '年龄未知',
            {'male': '男', 'female': '女'}.get(patient.get('gender'), '性别未定'),
            '有' if patient.get('insurance_card') else '无'))
    else:
        step7 = '7. 填写实名信息 + 短信验证码（用户手动完成, 脚本不触碰凭据）'
    return [
        '1. 打开「随申办」App（包名 mobi.w3studio.apps.android.shsmy.phone）',
        '2. 首页 →「预约挂号」→「全部医院 (444)」',
        '3. 检索并选择医院: %s（%s, 主院区）' % (rec['name'], rec['district']),
        step4,
        '5. 选择日期: %s（最早可约; 日期条若显示无号则换日期/医生）' % date,
        '6. 选择医生与时段 →「去预约」（医生列表来自 SlotResult.doctors, is_target 标记目标医生）',
        step7,
        '8. 确认预约 → 支付（用户手动完成）',
    ]


def run(rec, info, execute=False, patient=None):
    if execute:
        return {
            'dry_run': False, 'executed': False,
            'refused': ('原型安全边界: 450 家医院 capabilities.book 均为 false, 本脚本不发起任何'
                        '真实挂号/提交/支付请求（RANKING_SPEC §4.5 禁止事项）。'),
        }
    steps = plan_steps(rec, info, patient=patient)
    return {
        'dry_run': True, 'executed': False, 'steps': steps,
        'note': 'dry-run（默认开）: 仅打印将执行的步骤序列; 不真发、不提交、不支付。',
    }
