# -*- coding: utf-8 -*-
"""就诊人采集 —— 随申办 APP · 预约挂号 · 「就诊人」页（纯标准库 adb + uiautomator dump）。

用途：web 实测端「获取就诊人」按钮 -> 本模块导航到 就诊人 列表页 -> 解析卡片 ->
     写 app/cache/patients.json -> 前端下拉框（第一项=不选择项=为名单外的人挂号）。

安全底线（与 fetch_slots 同一纪律）：
  * 只读：只点 预约挂号 / 就诊人 / 返回 / 同意（授权页）；
    **绝不点** 新增就诊人 / 绑定就诊卡 / 去预约 / 提交 / 支付。
  * 不还原隐私：页面本身已脱敏（姓名 *华 / 手机 136****4396 / 证件号 **************6232 /
    医保卡 ******5170）。本模块只保留页面上可见的脱敏串，不做任何补全/猜测。
  * 失败即报错（不静默给空列表）：blocked（风控/实名）/ 导航失败 / 解析不到卡片
    都抛 PatientError，由调用方（web 端）如实回传错误。

字段口径（2026-10-08 476cf991 实测）：
  * 卡片可见文本：`*华`（脱敏姓名，可见部分=人名最后一个字）、`48岁`、`已创建16天`、
    `手机号 136****4396`、`证件类型 居民身份证`、`证件号 **************6232`、
    `绑定就诊卡`、（有则）`医保卡 ******5170`。
  * 性别：**页面上没有性别文本**（性别位是图标字形 0xe6a6，dump 里不可读；两卡同字形=同性别）。
    故按 居民身份证 脱敏号（实测 14 星 + 末 4 位明文）的第 17 位（=可见 4 位的**倒数第 2 位**）
    奇=男 / 偶=女 推断，并在 gender_source 里如实标注；证件类型非身份证、或号段
    不是「14 星 + 4 位」=> gender=None（不猜）。
  * 是否用医保卡：卡片内出现「医保卡」行（带脱敏卡号）=> insurance_card=True，
    否则 False（该就诊人未绑医保卡）。

CLI：
  python -m app.patients --dev 476cf991            # 采集并打印 JSON
  python -m app.patients --dev 476cf991 --fresh    # 忽略缓存 TTL（默认仍重新采集）
  python -m app.patients --cache                   # 只读缓存，不碰 adb
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time

import devbridge

try:
    from slots import navlock as NAVLOCK
except Exception:                       # 桌面直跑
    import navlock as NAVLOCK

try:
    from hhpaths import out as _out
    CACHE_FILE = _out('cache', 'patients.json')   # 端上缓存写 filesDir/out
except Exception:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    CACHE_FILE = os.path.join(APP_DIR, 'cache', 'patients.json')
CACHE_TTL_S = 1800          # 30 分钟; 过期/点按钮 -> 端上实读
PKG = 'mobi.w3studio.apps.android.shsmy.phone'
ADBXML = '/sdcard/_hh_patients.xml'   # 旧 adb 路径，端上已不再使用（保留兼容占位）

YJ = '预约挂号'
YY = '应用服务'
AUTH_T = '需要如下个人信息'
T444 = '全部医院 (444)'
T444b = '全部医院(444)'
PATIENTS_ENTRY = '就诊人'
NEW_PATIENT = '新增就诊人'
BLOCKED_KW = ['实名认证', '验证码', '滑动验证', '人脸识别', '身份核验', '风控', '安全验证']

NAME_RE = re.compile(r'^\*{1,4}[\u4e00-\u9fa5]{1,3}$')      # *华 / **嘉
AGE_RE = re.compile(r'^(\d{1,3})\s*岁$')
CREATED_RE = re.compile(r'^已创建(\d+)\s*天')
COUNT_RE = re.compile(r'共\s*(\d+)\s*位')
ID_MASK_RE = re.compile(r'^\*{10,16}(\d{3,4})$')            # **************6232
PHONE_MASK_RE = re.compile(r'^1\d{2}\*{3,4}\d{4}$')
CARD_MASK_RE = re.compile(r'^\*{2,}\d{3,4}$')               # ******5170


class PatientError(RuntimeError):
    """导航/风控/解析失败（调用方须如实报错，不得当作“没有就诊人”）。"""


def _log(log, msg):
    (log or (lambda m: print(m, flush=True)))(msg)


def _default_device():
    """默认设备标识: 端上=真实机器标识(PyBridge.deviceKey), 桌面=旧默认(保持 PC 原型行为)。

    2026-10-14 修复: 原先硬编码 '476cf991'，MI 5X(0043d38b0504) 上采集/日志都写成另一台机器。
    """
    try:
        if devbridge.available():
            return devbridge.device_key()
    except Exception:
        pass
    return (os.environ.get('DEVICE_SERIAL') or '476cf991')


# ---------------------------------------------------------------- 设备桥（端上 devbridge）
class Adb(object):
    """与旧 Adb 同语义，但底层换成端上 devbridge（AccessibilityService，不走 adb）。"""

    def __init__(self, dev=None):
        self.dev = dev or _default_device()
        w, h = devbridge.screen_size()
        self.screen_w, self.screen_h = (w or 1080, h or 2392)

    def run(self, *a, **kw):
        """兼容旧代码里少量 adb 调用（get-state / shell am|monkey 等）→ 端上等价原语。"""
        a = [str(x) for x in a]
        if not a:
            return ''
        if a[0] == 'get-state':
            return 'device' if devbridge.is_ready() else 'offline'
        if a[0] == 'shell' and len(a) > 1 and a[1] == 'input':
            act = a[2] if len(a) > 2 else ''
            if act == 'tap':
                devbridge.tap(a[3], a[4])
            elif act == 'swipe':
                devbridge.swipe(a[3], a[4], a[5], a[6], a[7] if len(a) > 7 else 350)
            elif act == 'keyevent':
                devbridge.keyevent(a[3])
            return ''
        # am force-stop / monkey / settings / svc / wm / ime …：端上由 launch()/服务承担，no-op。
        return ''

    def dump(self, tries=3, wait=2):
        """读屏 -> 与 uiautomator 同构 XML（WebView 页偶发失败重试）。"""
        last = None
        for i in range(tries):
            try:
                xml = devbridge.dump()
                i0 = xml.find('<hierarchy')
                if i0 >= 0:
                    return xml[i0:]
                last = xml[:120]
            except Exception as e:
                last = e
            _log(None, '  dump-retry %d/%d (%s)' % (i + 1, tries, str(last)[:80]))
            time.sleep(wait)
        raise PatientError('读屏失败: %s' % str(last)[:120])

    def tap(self, x, y):
        devbridge.tap(int(x), int(y))

    def back(self):
        devbridge.back()

    def keyevent(self, k):
        devbridge.keyevent(k)

    def swipe(self, x1, y1, x2, y2, dur=350):
        H = self.screen_h
        y1 = min(max(int(y1), 60), H - 60)
        y2 = min(max(int(y2), 60), H - 60)
        devbridge.swipe(int(x1), y1, int(x2), y2, dur)

    def input_cjk(self, text):
        # 端上 ACTION_SET_TEXT，中英文通吃，不依赖 ADBKeyboard。
        devbridge.input_text(text)

    def launch(self):
        """端上把随申办拉到前台（devbridge.launch = NEW_TASK|CLEAR_TOP 等效重启，不按 HOME）。"""
        try:
            return bool(devbridge.launch())
        except Exception:
            return False

    def ensure_foreground(self, timeout_s=15):
        """端上：先自动唤起随申办并等其窗口出现，再让上层 dump。

        与 slots/fetch_slots.py::Adb.ensure_foreground 同一语义 —— 老版 patients.py 假设
        「随申办已由人打开」，但 App 内点按钮采集时控制器 App 在前台，随申办在其后面，
        AccDeviceBridge 按包名过滤可见窗口取不到随申办 => 读屏为空。必须先拉前台。
        """
        xml = ''
        for _ in range(max(1, int(timeout_s * 2))):
            try:
                xml = devbridge.dump() or ''
            except Exception:
                xml = ''
            if '<hierarchy' in (xml or ''):
                return xml
            self.launch()
            time.sleep(1.5)
        return xml


# ---------------------------------------------------------------- 节点解析
def nodes(xml):
    """-> [(text, cls, x1,y1,x2,y2)] 按 (y,x) 排序；过滤空文本与 icon-font 私用区字形。"""
    rows = []
    for n in re.findall(r'<node\b[^>]*?/?>', xml):
        tm = re.search(r'text="([^"]*)"', n)
        bm = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', n)
        cm = re.search(r'class="([^"]+)"', n)
        if not tm or not bm:
            continue
        t = tm.group(1).strip()
        if not t or all(0xE000 <= ord(c) <= 0xF8FF for c in t):
            continue
        x1, y1, x2, y2 = (int(v) for v in bm.groups())
        rows.append((t, (cm.group(1).split('.')[-1] if cm else ''), x1, y1, x2, y2))
    rows.sort(key=lambda r: (r[3], r[2]))
    return rows


def page_kind(xml):
    if any(k in xml for k in BLOCKED_KW):
        return 'blocked'
    if NEW_PATIENT in xml or ('还可添加' in xml and PATIENTS_ENTRY in xml):
        return 'patients'
    if AUTH_T in xml:
        return 'auth'
    if T444 in xml or T444b in xml:
        return '444'
    return 'other'


def _row_value(rows, label, ytol=40, below_px=140):
    """标签取值：先找右侧同行（手机号/证件类型/证件号），找不到再找正下方（医保卡卡号在标签下方）。"""
    lab = None
    for r in rows:
        if r[0] == label:
            lab = r
            break
    if lab is None:
        return None
    _, _, lx1, ly1, lx2, ly2 = lab
    right = [r for r in rows
             if r[0] != label and r[2] > lx2 - 5 and not (r[4] < lx1)
             and r[3] < ly2 + ytol and r[5] > ly1 - ytol]
    if right:
        right.sort(key=lambda r: r[2])          # 取最靠近标签的右侧值
        return right[0][0]
    below = [r for r in rows
             if r[0] != label and ly2 <= r[3] <= ly2 + below_px and r[2] < lx2 and r[4] > lx1]
    if below:
        below.sort(key=lambda r: r[3])          # 正下方最近的一行
        return below[0][0]
    return None


def _gender_from_masked_id(cert_type, id_masked):
    """居民身份证脱敏号（实测 14 星 + 末 4 位明文）第 17 位奇偶 -> 男/女；不可判 -> (None, 原因)。

    18 位证号第 17 位是性别位（奇=男 偶=女）。脱敏只留末 4 位（第 15~18 位），
    故第 17 位 = 可见 4 位的**倒数第 2 位**（实测 2026-10-08：`**************6232` -> 第17位=3）。
    """
    if not id_masked:
        return None, '无证件号'
    if '身份证' not in (cert_type or ''):
        return None, '证件类型=%s（非居民身份证，不按号段推断）' % (cert_type or '未知')
    m = ID_MASK_RE.match(id_masked)
    if not m or len(m.group(1)) != 4:
        return None, '证件号脱敏形态非「14星+4位」，不推断'
    digit = int(m.group(1)[-2])             # 第 17 位 = 可见 4 位倒数第 2 位
    return ('male' if digit % 2 else 'female'), '证件号第17位=%d(%s)' % (
        digit, '奇=男' if digit % 2 else '偶=女')


def _pua_nodes(xml):
    """图标字形节点（私用区）：[(glyph, x1,y1,x2,y2)]。实测 0xe6a6=年龄右侧性别图标,
    0xe6a8=手机号行右侧操作图标（不是性别）。"""
    out = []
    for n in re.findall(r'<node\b[^>]*?/?>', xml):
        tm = re.search(r'text="([^"]*)"', n)
        bm = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', n)
        if not tm or not bm:
            continue
        t = tm.group(1)
        if not t or not all(0xE000 <= ord(c) <= 0xF8FF for c in t):
            continue
        x1, y1, x2, y2 = (int(v) for v in bm.groups())
        out.append((t, x1, y1, x2, y2))
    return out


def parse_patients(xml):
    """就诊人页 XML -> (patients[], header_total, notes[])。按脱敏姓名节点切卡片，逐卡取同行值。"""
    rows = nodes(xml)
    icons = _pua_nodes(xml)
    notes = []
    total = None
    for t, _c, _x1, _y1, _x2, _y2 in rows:
        m = COUNT_RE.search(t)
        if m:
            total = int(m.group(1))
            break
    anchors = [r for r in rows if NAME_RE.match(r[0])]
    if not anchors:
        return [], total, ['页面上没有脱敏姓名卡片（可能未登录/未添加就诊人/页面未渲染）']
    anchors.sort(key=lambda r: r[3])
    out = []
    for i, a in enumerate(anchors):
        name, _cls, _x1, y1, _x2, y2 = a
        y_lo = y1 - 20
        y_hi = anchors[i + 1][3] - 20 if i + 1 < len(anchors) else 10 ** 9
        seg = [r for r in rows if y_lo <= r[3] < y_hi]
        age, age_box = None, None
        for t, _c, ax1, ay1, ax2, ay2 in seg:
            m = AGE_RE.match(t)
            if m:
                age, age_box = int(m.group(1)), (ax1, ay1, ax2, ay2)
                break
        # 性别图标：年龄右侧最近的私用区字形（实测 0xe6a6；两卡同字形=同性别，仅作交叉校验用）
        gicon = None
        if age_box:
            near = [g for g in icons if g[1] > age_box[2] - 10 and abs(g[2] - age_box[1]) < 60]
            if near:
                near.sort(key=lambda g: g[1])
                gicon = 'U+%04X' % ord(near[0][0])
        created = None
        for t, _c, _a, _b, _e, _f in seg:
            m = CREATED_RE.match(t)
            if m:
                created = int(m.group(1))
                break
        phone = _row_value(seg, '手机号')
        cert_type = _row_value(seg, '证件类型')
        id_masked = _row_value(seg, '证件号')
        ins_no = _row_value(seg, '医保卡')
        has_ins = any(r[0] == '医保卡' for r in seg)
        gender, gnote = _gender_from_masked_id(cert_type, id_masked)
        out.append({
            'name_masked': name,
            'name_last': name.replace('*', '')[-1:] or None,   # 可见的最后一个字（脱敏后可见部分）
            'age': age,
            'gender': gender,
            'gender_source': gnote,
            'gender_icon': gicon,
            'insurance_card': bool(has_ins),
            'insurance_card_no': ins_no if has_ins else None,
            'phone_masked': phone,
            'cert_type': cert_type,
            'id_no_masked': id_masked,
            'created_days': created,
            'y': y1,
        })
    # 交叉校验：页面唯一可见的性别线索是图标字形（不可读）；证件号位是权威推断，两者不自洽时如实提示
    pairs = [(p.get('gender_icon'), p.get('gender')) for p in out if p.get('gender_icon')]
    by_icon = {}
    for ic, g in pairs:
        by_icon.setdefault(ic, set()).add(g or '?')
    genders = {g for _ic, g in pairs if g}
    if len(by_icon) == 1 and len(genders) > 1:
        notes.append('性别图标字形全同但证件号推断出不同性别（图标不可读, 以证件号第17位为准）')
    if len(by_icon) > 1 and len(genders) == 1:
        notes.append('证件号推断性别全同但性别图标有 %d 种字形(%s)（建议人工核对性别）'
                     % (len(by_icon), ','.join(sorted(by_icon))))
    if total is not None and total != len(out):
        notes.append('页头声明共 %d 位, 本次解析到 %d 位（未滚到底/渲染不全, 已如实标注）' % (total, len(out)))
    return out, total, notes


# ---------------------------------------------------------------- 导航
def _agree_auth(adb, xml, log=None):
    for r in nodes(xml):
        if r[0] == '同意':
            adb.tap((r[2] + r[4]) // 2, (r[3] + r[5]) // 2)
            _log(log, '  授权页 -> 点「同意」')
            time.sleep(3.5)
            return True
    return False


def _relaunch_to_444(adb, log=None, tries=2):
    """HOME + force-stop + monkey -> 首页搜索「预约挂号」-> 应用服务 -> 444 页。"""
    for t in range(tries):
        _log(log, '  重启随申办走全局通道 (HOME + force-stop + monkey)%s' % ('' if t == 0 else ' [重试%d]' % (t + 1)))
        adb.keyevent(3)
        time.sleep(2)
        adb.run('shell', 'am', 'force-stop', PKG, check=False)
        time.sleep(1.5)
        adb.run('shell', 'monkey', '-p', PKG, '-c', 'android.intent.category.LAUNCHER', '1', check=False)
        time.sleep(9)
        xml = adb.dump()
        k = page_kind(xml)
        if k == 'blocked':
            raise PatientError('随申办出现风控/实名认证页（blocked）—— 需人工在手机上完成验证后重试')
        if k == '444':
            return xml
        rows = nodes(xml)
        entry = None
        for r in rows:
            if r[1] == 'EditText':
                entry = r
                break
        if entry is None:
            for r in rows:
                if r[0] == '搜索':
                    entry = r
                    break
        if entry is not None:
            adb.tap((entry[2] + entry[4]) // 2, (entry[3] + entry[5]) // 2)
        else:
            adb.tap(473, 786)          # legacy fixed（Redmi 1080x2392）
        time.sleep(3)
        xml = adb.dump()
        in1 = None
        for r in nodes(xml):
            if r[1] == 'EditText':
                in1 = r
                break
        if in1 is None and 'et_search' not in xml:
            _log(log, '  搜索框未出现 -> 重试')
            continue
        ix, iy = ((in1[2] + in1[4]) // 2, (in1[3] + in1[5]) // 2) if in1 else (462, 198)
        adb.tap(ix, iy)
        time.sleep(1.2)
        adb.input_cjk(YJ)
        time.sleep(1.2)
        adb.keyevent(66)
        for _w in range(12):
            xml = adb.dump()
            if YY in xml:
                break
            time.sleep(1)
        if YY not in xml:
            _log(log, '  搜索结果页(应用服务)未出现 -> 重试')
            continue
        hit = None
        for r in nodes(xml):
            if r[0] == YJ and r[1] == 'TextView':
                hit = r
                break
        if hit is not None:
            adb.tap((hit[2] + hit[4]) // 2, (hit[3] + hit[5]) // 2)
        else:
            adb.tap(540, 484)
        time.sleep(3.5)
        for _w in range(12):
            xml = adb.dump()
            if (AUTH_T in xml) or (T444 in xml) or (T444b in xml):
                break
            time.sleep(2)
        for _k in range(2):
            if AUTH_T in xml:
                _agree_auth(adb, xml, log)
                xml = adb.dump()
            else:
                break
        if page_kind(xml) == '444':
            return xml
    raise PatientError('无法进入 预约挂号「全部医院(444)」页（导航失败）—— 请手动打开随申办·预约挂号后重试')


def ensure_patients_page(adb, log=None, max_rounds=8):
    """把手机带到 就诊人 列表页。返回该页 XML。"""
    for _r in range(max_rounds):
        xml = adb.dump()
        k = page_kind(xml)
        if k == 'patients':
            return xml
        if k == 'blocked':
            raise PatientError('随申办出现风控/实名认证页（blocked）—— 需人工在手机上完成验证后重试')
        if k == 'auth':
            if not _agree_auth(adb, xml, log):
                time.sleep(3)
            continue
        if k == '444':
            hit = None
            for r in nodes(xml):
                if r[0] == PATIENTS_ENTRY and r[1] in ('TextView', 'View', 'ImageView'):
                    hit = r
                    break
            if hit is None:
                raise PatientError('444 页上找不到「就诊人」入口（页面结构变化）')
            _log(log, '  444 页 -> 点「就诊人」(%d,%d)' % ((hit[2] + hit[4]) // 2, (hit[3] + hit[5]) // 2))
            adb.tap((hit[2] + hit[4]) // 2, (hit[3] + hit[5]) // 2)
            for _w in range(10):
                time.sleep(1.5)
                xml2 = adb.dump()
                if page_kind(xml2) == 'patients':
                    return xml2
            raise PatientError('点「就诊人」后未进入就诊人页（页面未渲染/结构变化）')
        # other：先 back 逐级退, 退不动就走全局通道
        adb.back()
        time.sleep(2)
        xml2 = adb.dump()
        if page_kind(xml2) in ('patients', '444'):
            continue
        return ensure_patients_page_after_relaunch(adb, log)
    raise PatientError('导航到就诊人页失败（轮次用尽）')


def ensure_patients_page_after_relaunch(adb, log=None):
    xml = _relaunch_to_444(adb, log)
    return ensure_patients_page(adb, log)


def _goto_444_then_patients(adb, log=None):
    """端上导航：用已验证的 fetch_slots.Nav.ensure_444 到 444 页，再点「就诊人」进列表页。

    复用 slots/fetch_slots.Nav（端上 ensure_foreground + 搜索通道 + A11Y 自愈合均已验证），
    避免本文件 _relaunch_to_444 依赖端上 no-op 的 monkey/force-stop。返回就诊人页 XML。
    """
    from slots import fetch_slots as _fs
    st, s444 = _fs.Nav(adb, '').ensure_444(max_rounds=6)
    if st != 'ok':
        raise PatientError('导航到 全部医院(444) 页失败（端上 nav=%s）' % st)
    _log(log, '[patients] 已到 444 页，点「就诊人」…')
    # 从 444 XML 里找「就诊人」入口并点击，再等就诊人页渲染（同 ensure_patients_page 的 444 分支逻辑）
    hit = None
    for r in nodes(s444):
        if r[0] == PATIENTS_ENTRY and r[1] in ('TextView', 'View', 'ImageView'):
            hit = r
            break
    if hit is None:
        raise PatientError('444 页上找不到「就诊人」入口（页面结构变化）')
    adb.tap((hit[2] + hit[4]) // 2, (hit[3] + hit[5]) // 2)
    for _w in range(12):
        time.sleep(1.5)
        xml2 = adb.dump()
        if page_kind(xml2) == 'patients':
            return xml2
    raise PatientError('点「就诊人」后未进入就诊人页（页面未渲染/结构变化）')


def collect(dev=None, log=None, back_after=True, max_scrolls=4):
    """实采就诊人列表 -> dict（含 patients/captured_at/device/notes）。失败抛 PatientError。"""
    t0 = time.time()
    adb = Adb(dev)
    # 端上关键：App 内点按钮时控制器 App 在前台、随申办在其后 => 读屏按包名过滤取不到随申办窗口。
    # 走已验证的端上导航：复用 slots/fetch_slots.Nav.ensure_444（内部 ensure_foreground + 搜索通道 + 自愈合），
    # 到 444 后再点「就诊人」进列表页。不再用本文件自带的 _relaunch_to_444（其 monkey/force-stop 在端上是 no-op）。
    _log(log, '[patients] dev=%s 导航到 444 页…' % dev)
    xml = _goto_444_then_patients(adb, log)
    seen, order = {}, []
    total, notes_all = None, []
    stale = 0
    for i in range(max_scrolls + 1):
        pts, total, notes = parse_patients(xml)
        for p in pts:
            key = (p['name_masked'], p['age'], p['phone_masked'], p['id_no_masked'])
            if key not in seen:
                seen[key] = p
                order.append(key)
        if notes:
            notes_all = notes
        if total is not None and len(order) >= total:
            break
        if i >= max_scrolls:
            break
        before = len(order)
        adb.swipe(adb.screen_w // 2, int(adb.screen_h * 0.75), adb.screen_w // 2, int(adb.screen_h * 0.30))
        time.sleep(1.5)
        xml = adb.dump()
        if page_kind(xml) != 'patients':
            notes_all.append('滚动后离开就诊人页, 停止滚动')
            break
        if len(order) == before:
            stale += 1
            if stale >= 2:
                break
        else:
            stale = 0
    patients = [seen[k] for k in order]
    for idx, p in enumerate(patients):
        p['id'] = 'p%d' % (idx + 1)
        p['label'] = '%s · %s · %s%s' % (
            p['name_masked'],
            ('%d岁' % p['age']) if p['age'] is not None else '年龄未知',
            {'male': '男', 'female': '女'}.get(p['gender'], '性别未定'),
            ' · 医保卡' if p['insurance_card'] else ' · 无医保卡',
        )
    if back_after:
        adb.back()          # 退回 444 页, 不把手机停在就诊人页
        time.sleep(1.2)
    if not patients:
        raise PatientError('就诊人页解析到 0 位就诊人：%s' % ('; '.join(notes_all) or '页面无卡片'))
    d = {
        'captured_at': dt.datetime.now().isoformat(timespec='seconds'),
        'ts': time.time(),
        'device': dev,
        'total_declared': total,
        'patients': patients,
        'notes': notes_all,
        'elapsed_s': round(time.time() - t0, 1),
        'source': 'ssb_app/预约挂号/就诊人',
    }
    return d


def load_cache():
    try:
        with open(CACHE_FILE, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return None


def save_cache(d):
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        tmp = CACHE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


def get_patients(dev=None, fresh=False, log=None, back_after=True):
    """web 端入口：缓存优先（30 分钟 TTL），fresh/过期 -> adb 实采。
    返回 (dict, err)；实采失败但有旧缓存 -> 返回旧缓存并附 error 说明。"""
    dev = (dev or _default_device()).strip()
    cached = load_cache()
    if cached and not fresh and (time.time() - float(cached.get('ts', 0))) < CACHE_TTL_S:
        cached = dict(cached)
        cached['from_cache'] = True
        return cached, None
    try:
        d = collect(dev=dev, log=log, back_after=back_after)
    except PatientError as e:
        if cached:
            cached = dict(cached)
            cached['from_cache'] = True
            cached['error'] = '实采失败(%s) → 返回旧缓存' % str(e)[:150]
            return cached, None
        return None, str(e)
    except Exception as e:  # noqa: BLE001
        if cached:
            cached = dict(cached)
            cached['from_cache'] = True
            cached['error'] = '实采异常(%s: %s) → 返回旧缓存' % (type(e).__name__, str(e)[:120])
            return cached, None
        return None, '%s: %s' % (type(e).__name__, str(e)[:200])
    d['from_cache'] = False
    save_cache(d)
    return d, None


def fetch_patients_json(dev=None):
    """端上 App 入口（对齐 web「获取就诊人」按钮）：实采(fresh=True)就诊人 -> 单 JSON 字符串。

    用单 JSON 字符串返回避免 Chaquopy 元组 (dict, err) 的跨语言摩擦。
    返回 {"ok":bool,"patients":[...],"from_cache":bool,"elapsed_s":float|null,"error":str|null}。
    实采成功 -> 写缓存并返回；实采失败但有旧缓存 -> 回旧缓存(from_cache=True)；
    实采失败且无缓存 -> ok=False。
    """
    d = None
    err = None
    # ②/⑥: 采集要导航到 444; 别的任务在驱动随申办时如实 busy, 不并发抢屏
    if not NAVLOCK.acquire('patients_fetch'):
        h = NAVLOCK.holder() or {}
        return json.dumps({'ok': False, 'patients': [], 'from_cache': False,
                           'busy': True, 'elapsed_s': 0.0,
                           'error': '随申办导航正被 %s 占用(%.0fs), 稍后再采'
                                    % (h.get('owner'), h.get('held_s') or 0)},
                          ensure_ascii=False)
    try:
        d, err = get_patients(dev=dev, fresh=True,
                              log=lambda m: print(m, flush=True), back_after=True)
    except Exception as e:  # noqa: BLE001
        d, err = None, '%s: %s' % (type(e).__name__, e)
    finally:
        # 锁必须成对释放。之前这里没有 finally: 读屏失败/异常时锁留在 patients_fetch 上,
        # 之后每次自检/抓取都被 nav_busy 拒绝(实测僵尸持有 3257s, rc=5, units=0)。
        NAVLOCK.release('patients_fetch')
    if d is not None:
        out = {'ok': True, 'patients': d.get('patients') or [],
               'from_cache': bool(d.get('from_cache')),
               'elapsed_s': d.get('elapsed_s'), 'error': d.get('error')}
    else:
        out = {'ok': False, 'patients': [], 'from_cache': False,
               'elapsed_s': None, 'error': err or 'unknown'}
    return json.dumps(out, ensure_ascii=False)


def load_patients_json():
    """读缓存（不碰 adb）-> 同 fetch_patients_json 的 shape，供 App 启动时回填 Spinner。
    无缓存 -> ok=False（调用方静默保留默认「不选择」即可）。"""
    d = load_cache()
    if not d:
        return json.dumps({'ok': False, 'patients': [], 'from_cache': True}, ensure_ascii=False)
    out = {'ok': True, 'patients': d.get('patients') or [], 'from_cache': True,
           'captured_at': d.get('captured_at'), 'elapsed_s': None, 'error': None}
    # 这里原来有一句"无主的 release" —— 本函数从未 acquire, 释放别人的锁会把正在跑的
    # 抓取任务的导航权抢走。已删除(锁的 acquire/release 只在 fetch_patients_json 内成对)。
    return json.dumps(out, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser(description='随申办 就诊人 列表采集（只读）')
    ap.add_argument('--dev', default=None,
                    help='设备标识(默认=本机标识 devbridge.device_key(); 桌面回退 DEVICE_SERIAL)')
    ap.add_argument('--cache', action='store_true', help='只读缓存文件, 不碰 adb')
    ap.add_argument('--no-back', action='store_true', help='采完不退回 444 页')
    a = ap.parse_args()
    if a.cache:
        d = load_cache()
        if not d:
            print('无缓存（app/cache/patients.json 不存在）')
            return 1
        d = dict(d)
        d['from_cache'] = True
    else:
        d, err = get_patients(dev=a.dev, fresh=True, log=lambda m: print(m, flush=True),
                              back_after=not a.no_back)
        if err:
            print('[patients] 失败: %s' % err)
            return 2
    print(json.dumps(d, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
