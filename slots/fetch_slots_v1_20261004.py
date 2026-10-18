# -*- coding: utf-8 -*-
"""
fetch_slots.py — 随申办·预约挂号 号源态势层抓取器（stage-2, 两层分离之"态势层"）

设计（依据 stage-2 探路结论 A-D，见 slots/PROBE_NOTES.md）:
  A. 科室页(L2) 自带 14 天日期条，文本含 "有号/无号" → 一个 (tab,dept) 单元只需
     1 次点击 + 1 次 dump，不进医生主页。
  B. 余量无数字，只有 去预约/已约满 二值 → 态势层用 bookable_doctor_count 代理，
     明细层(医生主页)才统计 可约时段数 作为 SlotResult.remaining_quota 口径。
  C. L1 页一次 dump 即含全部 tab 名（屏外 bounds 高度为 0 但 text 完整）→
     tab 枚举零额外成本，并与 manifest 计划做对账。
  D. 号源实时变动 → 每条快照带 captured_at（设备时钟），ttl_hint_min=90。

两层:
  态势层(本脚本默认): (hospital, tab, dept) -> days_14[{date,weekday,has_slot}]
     + earliest_bookable + tab_kind + bookable_doctor_count。不进医生主页。
  明细层(--detail): 对指定科室点 1-2 个"去预约"医生进医生主页, 统计可约时段数/
     价格/时段, 记录单次成本。留给运行期按需触发。

接口:
  python slots/fetch_slots.py --hospital SH-010 --slug jitongnan [--device 476cf991]
      [--resume] [--include-all] [--limit N] [--dry-run]
      [--detail "科室raw名[;科室raw名]"] [--only-detail]
      [--shard i/n]   # 多设备: 每台设备跑一个分片, 单元级取模切分(见 COST_MODEL)

接口 (S8 dept 模式, 2026-10-03):
  python slots/fetch_slots.py --dept "第十人民医院,眼科,眼科门诊" [--dept "SH-084,眼科,眼科门诊"] ...
      [--dept-file depts.json] [--max-consec-errors 3] [--deadline 09:00] [--dry-run]
  三段 {医院,tab,dept}: 医院同时支持中文名与 SH-xxx 式 id; tab/dept 支持 raw 名; 可传多套, 逐套隔离。
  CJK 免损坏通道: --dept-file <UTF-8 JSON> (命令行中文受 cmd 代码页影响; 收到 U+FFFD 乱码 => 显式报错, 不许静默跑)。
  L2 后: 枚举全部子 tab(专家/普通/专病...)逐个采 + 每子 tab 枚举全部医生卡(有号/无号都记);
  对有号医生**点卡身**进医生主页(不点「去预约」按钮), 点「显示全部排班/展开全部排班」展开(两串兼容,
  记录展开前后时段行数证明生效), 六字段逐字段 provenance 落盘到独立文件
  slots/out/dept_runs/<hid>_<tab>_<dept>_<ts>.jsonl + _summary.json (不写 six_fields.jsonl / raw/ 存量文件)。
  S13 (S11 H3 修复, 2026-10-03): ① 卡集枚举=滚动到底直到末尾连续 2 次 dump 稳定(懒加载 20→21, 漏卡修复);
  ② 每卡 tap 前 _ensure_card_visible: 零高裁剪/中心越界 => 小步 swipe + 重 dump 取新坐标再 tap
  (绝不对 (548,2347) 类屏外中心发 tap); ③ 展开按钮 toggle 三串(显示/展开全部排班 <-> 收起排班),
  收起态不点、不判折叠; ④ 记录新增 挂号费用_原文(XML 原串逐字) + 顶层 sub_tab + extract/extract_fee/
  extract_type(text_off=**字节偏移**, grep -bo 口径, 与安卓小能手 s14-unified-v1 同 schema) 供
  compare_truth.py 零别名零容错对账。
  退出码: 0=正常 2=配置/导航失败 3=blocked 4=单元级连续 error 熔断。
断点续跑: 粒度 = (hospital, tab, dept)。每抓完一个单元立即落盘
  slots/out/<file>.json + slots/out/state.json + slots/out/fetch.log。
  --resume 跳过 status in {ok, no_slot} 的单元；error 单元重抓。
失败隔离: 单元失败不中断(tab 级重试/跳过)；单院遇 实名/验证码/风控 弹窗 →
  记录 blocked 并跳过该医院(不绕过)。零模型调用、纯 ADB + UI XML 文本匹配。
只读: 仅 点击科室/医生/日期条/Back, 绝不点 去预约/提交/支付。
"""
import argparse
import base64
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

import six_fields  # 六字段记录 schema/解析/指纹 (同目录, 纯标准库, 零模型)

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../workspace
MANIFEST_GLOB = os.path.join(WS, 'hospital_intel', 'manifests', '*.json')
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')
STATE_FILE = os.path.join(OUT_DIR, 'state.json')
LOG_FILE = os.path.join(OUT_DIR, 'fetch.log')

PKG = 'mobi.w3studio.apps.android.shsmy.phone'
ADBXML = '/sdcard/_fsdump.xml'
PRUNE_KINDS = {'other', 'screening', 'ancillary', 'selfname'}
TTL_HINT_MIN = 90

# ---------------- tab_kind 判定规则（对 5589 个 tab 名离线验证过） ----------------
# 顺序: campus 优先, 其次 site, 其余 category。
CAMPUS_KW = ['院区', '门诊部', '分院', '本部', '总院', '主院区', '新院区', '老院区',
             '南院', '北院', '东院', '西院', '门诊楼', '分医院', '院本部']
SITE_KW = ['村', '卫生室', '延伸', '分中心', '服务站', '服务点', '站点', '门诊点',
           '责任区', '分站', '小区']
TAB_KIND_RULES_DOC = (
    'campus: 名含 院区/门诊部/分院/本部/总院/主院区/新院区/老院区/南院/北院/东院/西院/门诊楼/分医院/院本部; '
    'site: 名含 村/卫生室/延伸/分中心/服务站/服务点/站点/门诊点/责任区/分站/小区; '
    'category: 其余(专病/专科/专家/返聘/夜门诊/特需/互联网/(门诊)/(医技)/科室组名, 兜底桶)')

def tab_kind(name):
    n = (name or '').strip()
    for kw in CAMPUS_KW:
        if kw in n:
            return 'campus'
    for kw in SITE_KW:
        if kw in n:
            return 'site'
    return 'category'

# ---------------- 页面特征文本 ----------------
T444 = '全部医院 (444)'
T444b = '全部医院(444)'
YJ = '预约挂号'
YY = '应用服务'
AUTH_T = '需要如下个人信息'
NODATA1 = '未搜到相关信息'
NODATA2 = '暂无相关内容'
LOADING = '加载中'
BLOCKED_KW = ['实名认证', '验证码', '滑动验证', '人脸识别', '身份核验', '风控', '安全验证']

BAR_RE = re.compile(r'^(周[一二三四五六日天])\s+(\d{2}-\d{2})\s+(有号|无号)$')
SLOT_ROW_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})\s*周[一二三四五六日天]\s*(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})$')
# S3 坑6: 费用字符两套 — 医生主页 U+FFE5(￥) / 确认页 U+00A5(¥) => 正则兼容(与 six_fields 同口径)
FEE_RE = six_fields.FEE_RE
# S3 坑2: 姓名节点可带「 收藏」后缀(医生主页标题)/无职称医生(仅姓名) => 统一用 six_fields 正则
DOC_TITLE_RE = six_fields.DOC_TITLE_RE
DOC_NAME_ONLY_RE = six_fields.DOC_NAME_ONLY_RE
SUB_TABS = {'专家门诊', '普通门诊', '专病门诊', '特需门诊', '互联网', '夜间门诊', '凌晨门诊'}
# S8 (2026-10-03): 医生主页排班展开按钮两串文本兼容 —— 安卓小能手实测页面文本为「显示全部排班」,
# 旧代码只写死「展开全部排班」, 该按钮从未被点过(每人只拿到可见 3 行)。两串都要认, 并记展开前后行数。
# S13: 展开按钮是 **toggle**: 「显示全部排班」 <-> 「收起排班」(ADB 真值 expand_evidence:
# btn_before='显示全部排班' btn_after='收起排班'; 14:11 dump 实测二次进入已呈收起态, 旧代码
# 对收起串零认识 => 二次进入失配)。三串都进 EXPAND_TEXTS(时段块终结符/按钮存在性检测),
# 但**只有 SHOW 串可点**(收起串点了会折叠回 3 行); schedule_collapsed 只认 SHOW 串
# (收起串在场 = 已展开 = 未折叠)。
EXPAND_SHOW_TEXTS = ('显示全部排班', '展开全部排班')
EXPAND_COLLAPSE_TEXTS = ('收起排班',)
EXPAND_TEXTS = EXPAND_SHOW_TEXTS + EXPAND_COLLAPSE_TEXTS
UNIT_ERROR_BREAK = 3   # S8: 单元级连续 error 熔断阈值(连续 error 单元 >= 阈值 => 中止运行, 退出码 4)
DEPT_RUNS_DIR = os.path.join(OUT_DIR, 'dept_runs')

# ---------------- 日志 ----------------
_LOGH = None
def log(msg):
    global _LOGH
    line = '%s %s' % (dt.datetime.now().strftime('%H:%M:%S'), msg)
    print(line, flush=True)
    if _LOGH is None:
        _LOGH = open(LOG_FILE, 'a', encoding='utf-8')
    _LOGH.write(line + '\n')
    _LOGH.flush()

# ---------------- ADB 封装 ----------------
class Adb:
    def __init__(self, dev):
        self.dev = dev
        self._n = 0

    def run(self, *a, timeout=90, check=True):
        cmd = ['adb', '-s', self.dev] + list(a)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError('adb %r rc=%d err=%s' % (a, r.returncode, (r.stderr or r.stdout)[:200]))
        return r.stdout or ''

    def dump(self, tries=4, wait=7):
        """uiautomator dump -> 本地 XML 文本。WebView 页偶发 dump 失败, 重试。"""
        last = None
        for i in range(tries):
            try:
                self.run('shell', 'uiautomator', 'dump', ADBXML, timeout=40)
                local = os.path.join(OUT_DIR, '_last_dump.xml')
                self.run('pull', ADBXML, local, timeout=40)
                with open(local, encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                last = e
                log('  dump-retry %d/%d after %ds (%s)' % (i + 1, tries, wait, str(e)[:80]))
                time.sleep(wait)
        raise RuntimeError('dump failed: %s' % last)

    def tap(self, x, y):
        self.run('shell', 'input', 'tap', str(int(x)), str(int(y)))

    def back(self):
        self.run('shell', 'input', 'keyevent', '4')

    def keyevent(self, k):
        self.run('shell', 'input', 'keyevent', str(k))

    def swipe(self, x1, y1, x2, y2, dur=350):
        self.run('shell', 'input', 'swipe', str(x1), str(y1), str(x2), str(y2), str(dur))

    def input_cjk(self, text):
        b64 = base64.b64encode(text.encode('utf-8')).decode()
        self.run('shell', 'am', 'broadcast', '-a', 'ADB_CLEAR_TEXT')
        time.sleep(0.4)
        self.run('shell', 'am', 'broadcast', '-a', 'ADB_INPUT_B64', '--es', 'msg', b64)

    def device_date(self):
        out = self.run('shell', "date '+%Y-%m-%d %H:%M:%S'").strip()
        return dt.datetime.strptime(out, '%Y-%m-%d %H:%M:%S')

# ---------------- XML 节点解析 ----------------
def parse_nodes(s):
    """-> [(text, cls, x1,y1,x2,y2)] 按 (y,x) 排序, 过滤 icon-font 文本。"""
    rows = []
    for n in re.findall(r'<node [^>]*?/?>', s):
        tm = re.search(r'text="([^"]*)"', n)
        bm = re.search(r'bounds="(\[\d+,\d+\]\[\d+,\d+\])"', n)
        cm = re.search(r'class="([^"]+)"', n)
        if not tm or not bm:
            continue
        t = tm.group(1)
        if not t.strip():
            continue
        if all(0xE000 <= ord(c) <= 0xF8FF for c in t):
            continue
        x1, y1, x2, y2 = map(int, re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bm.group(1)).groups())
        cls = cm.group(1).rsplit('.', 1)[-1] if cm else ''
        rows.append((t, cls, x1, y1, x2, y2))
    rows.sort(key=lambda r: (r[3], r[2]))
    return rows

def find_node(rows, text, cls=None, x2max=None, x1min=None, y1min=None, y1max=None, exact=True):
    for (t, c, x1, y1, x2, y2) in rows:
        if exact:
            if t != text:
                continue
        else:
            if text not in t:
                continue
        if cls and c != cls:
            continue
        if x2max is not None and x2 > x2max:
            continue
        if x1min is not None and x1 < x1min:
            continue
        if y1min is not None and y1 < y1min:
            continue
        if y1max is not None and y1 > y1max:
            continue
        return (t, c, x1, y1, x2, y2)
    return None

def node_visible(node):
    if not node:
        return False
    return (node[5] - node[3]) > 20 and (node[4] - node[2]) > 20

# ---------------- 页面分类 ----------------
def texts_of(s):
    return [t for (t, c, *_ ) in parse_nodes(s)]

def page_kind(s):
    if any(k in s for k in BLOCKED_KW):
        return 'blocked'
    if AUTH_T in s:
        return 'auth'
    if T444 in s or T444b in s:
        return '444'
    # 六字段齐全页(指纹规则 six_fields.SIX_PAGE_RULES; 未登记 = 永不命中 = 不猜, 报 error)
    if six_fields.match_six_page(s):
        return 'six'
    if '医生主页' in s:
        return 'doc'
    if '不限 日期' in s and ('有号' in s or '无号' in s):
        return 'L2'
    # L2 无日期条变体(社区中心): 无 14 天条, 只有 子tab(TabWidget)+医生卡/空态
    if ('专家门诊' in s or '普通门诊' in s) and ('去预约' in s or '已约满' in s or '暂无可预约' in s) and '医生主页' not in s:
        return 'L2b'
    if '选科室' in s:
        return 'L1'
    if YJ in s and '历史搜索' in s:
        return 'search'
    if 'et_search' in s:
        return 'native_search'
    return 'other'

# ---------------- 导航 ----------------
class Nav:
    def __init__(self, adb, hosp_name):
        self.adb = adb
        self.hosp = hosp_name

    def agree_auth(self, s):
        log('  auth page -> agree')
        self.adb.tap(540, 1610)
        time.sleep(3.5)

    def ensure_444(self, max_rounds=6):
        for r in range(max_rounds):
            s = self.adb.dump()
            k = page_kind(s)
            if k == 'blocked':
                return 'blocked', s
            if k == 'auth':
                self.agree_auth(s)
                continue
            if k == '444':
                return 'ok', s
            if k in ('L1', 'L2', 'L2b', 'doc', 'six'):  # S3: 医生主页指纹命中也要 back 出
                self.adb.back(); time.sleep(2.0); continue
            if k in ('search', 'native_search', 'other'):
                # 回 444: 优先 back 到 444; 失败则走全局通道重入
                self.adb.back(); time.sleep(2.0)
                s2 = self.adb.dump()
                if page_kind(s2) == '444':
                    return 'ok', s2
                return self._relaunch_channel()
        return 'fail', s

    def _relaunch_channel(self):
        log('  relaunch via global channel (HOME + force-stop wechat + monkey)')
        self.adb.keyevent(3); time.sleep(2)
        self.adb.run('shell', 'am', 'force-stop', 'com.tencent.mm'); time.sleep(1)
        self.adb.run('shell', 'monkey', '-p', PKG, '-c', 'android.intent.category.LAUNCHER', '1')
        time.sleep(9)
        s = self.adb.dump()
        if 'et_search' not in s:  # home -> 首页搜索框
            self.adb.tap(473, 786); time.sleep(3)
            s = self.adb.dump()
        if 'et_search' not in s:
            return 'fail', s
        self.adb.tap(462, 198); time.sleep(1.2)
        self.adb.input_cjk(YJ); time.sleep(1.2)
        self.adb.keyevent(66); time.sleep(1.0)
        for _ in range(12):
            s = self.adb.dump()
            if YY in s:
                break
            time.sleep(1)
        if YY not in s:
            return 'fail', s
        self.adb.tap(540, 484); time.sleep(3.5)
        for _w in range(8):
            s = self.adb.dump()
            if (AUTH_T in s) or (T444 in s) or (T444b in s) or ('TabWidget' in s):
                break
            time.sleep(2)
        for k in range(2):
            if AUTH_T in s:
                self.agree_auth(s)
                s = self.adb.dump()
            else:
                break
        if page_kind(s) == 'L1':
            self.adb.tap(79, 184); time.sleep(3.5)
            s = self.adb.dump()
        return ('ok' if page_kind(s) == '444' else 'fail'), s

    def enter(self):
        """进入本院 L1(选科室)页。返回 (ok|blocked|fail, s)"""
        st, s = self.ensure_444()
        if st != 'ok':
            return st, s
        if page_kind(s) != '444':
            s = self.adb.dump()
        # 444 页内搜索: 找搜索框(EditText/带 hint), 找不到用探路坐标
        rows = parse_nodes(s)
        box = None
        for (t, c, x1, y1, x2, y2) in rows:
            if c == 'EditText' and node_visible((t, c, x1, y1, x2, y2)):
                box = (x1, y1, x2, y2); break
        if box is None:
            for (t, c, x1, y1, x2, y2) in rows:
                if ('输入' in t or '医院' in t) and c in ('TextView', 'EditText') and y1 < 500 and node_visible((t, c, x1, y1, x2, y2)):
                    box = (x1, y1, x2, y2); break
        cx, cy = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2) if box else (681, 349)
        # S3 坑8(2026-10-03 设备实测): 444 页搜索框不在 dump(WebView) -> tap 进独立频道搜索页
        # (指纹 预约挂号+历史搜索); 搜索页点 EditText(聚焦后入 dump)聚焦, 单发输入(双发=文本翻倍),
        # 校验不翻倍/不丢失, 再点「搜索」Button 提交。
        self.adb.tap(cx, cy); time.sleep(1.5)
        s = self.adb.dump()
        if '历史搜索' not in s and 'et_search' not in s:
            self.adb.tap(cx, cy); time.sleep(1.5)
            s = self.adb.dump()
        in1 = None
        for (t, c, x1, y1, x2, y2) in parse_nodes(s):
            if c == 'EditText' and node_visible((t, c, x1, y1, x2, y2)):
                in1 = (x1, y1, x2, y2)
                break
        ix, iy = ((in1[0] + in1[2]) // 2, (in1[1] + in1[3]) // 2) if in1 else (480, 318)

        def _ed_text():
            """聚焦后 EditText 节点入 dump; 返回其文本(未聚焦/未渲染=None)。
            用节点文本校验输入, 不用全页 'hosp in s'(历史搜索 chips 会假阳性)。"""
            for (t, c, x1, y1, x2, y2) in parse_nodes(s):
                if c == 'EditText':
                    return t
            return None

        self.adb.tap(ix, iy); time.sleep(1.0)
        self.adb.input_cjk(self.hosp); time.sleep(1.8)   # 单发(坑8: 双发 ADB_INPUT_B64 会文本翻倍)
        s = self.adb.dump()
        et = _ed_text()
        if et != self.hosp:
            log('  search input verify fail (EditText=%r) -> refocus + single re-input' % (et,))
            self.adb.tap(ix, iy); time.sleep(1.0)
            self.adb.input_cjk(self.hosp); time.sleep(2.0)   # input_cjk 自带 ADB_CLEAR_TEXT
            s = self.adb.dump()
            et = _ed_text()
        if et != self.hosp:
            return 'fail', s

        # 搜索按钮
        btn = None
        for (t, c, x1, y1, x2, y2) in parse_nodes(s):
            if t == '搜索' and x1 > 850 and y1 < 500:
                btn = (x1, y1, x2, y2); break
        bx, by = ((btn[0] + btn[2]) // 2, (btn[1] + btn[3]) // 2) if btn else (1010, 316)
        self.adb.tap(bx, by)
        exact = None
        for _i in range(14):
            s = self.adb.dump()
            n = find_node(parse_nodes(s), self.hosp, cls='TextView', x1min=380, y1min=500)
            if n and node_visible(n):
                exact = n; break
            if NODATA1 in s or NODATA2 in s:
                break
            if LOADING in s:
                time.sleep(7)
            else:
                time.sleep(1.5)
        if not exact:
            return 'fail', s
        self.adb.tap((exact[2] + exact[4]) // 2, (exact[3] + exact[5]) // 2)
        time.sleep(5)
        s = self.adb.dump()
        if page_kind(s) == 'L1' and self.hosp in s:
            return 'ok', s
        return 'fail', s

    def verify_L1(self, s=None):
        s = s or self.adb.dump()
        return (page_kind(s) == 'L1' and self.hosp in s)

    def recover_L1(self, tries=4):
        for _ in range(tries):
            if self.verify_L1():
                return True
            self.adb.back(); time.sleep(2.0)
        st, s = self.enter()
        return st == 'ok'

    def select_tab(self, tab_name, plan_order, first_dept=None):
        """切到左列指定 tab。返回 (ok|notfound, s)
        点击后**轮询等右栏出现 first_dept 行**(WebView 切 tab 渲染竞态,
        实测 2.2s 固定等待不够, 有时 >30s 或点击未注册 → 补点)。"""
        s = self.adb.dump()
        for i in range(12):
            rows = parse_nodes(s)
            node = find_node(rows, tab_name, cls='View', x2max=395)
            if node and node_visible(node):
                # 底部/顶部被裁切的节点: 中心点可能在屏外(>2392) → 点击不注册。
                # 先小幅滚动左列把节点完整带入可视区, 再点(实测 中医科 tab y=2327 被裁切)。
                for _n in range(2):
                    if node[5] > 2330:
                        self.adb.swipe(150, 1700, 150, max(900, 1700 - (node[5] - 2330) - 30), 300)
                        time.sleep(1.3)
                        s = self.adb.dump()
                        node = find_node(parse_nodes(s), tab_name, cls='View', x2max=395)
                        if not (node and node_visible(node)):
                            break
                    elif node[3] < 270:
                        self.adb.swipe(150, 1500, 150, min(2200, 1500 + (270 - node[3]) + 30), 300)
                        time.sleep(1.3)
                        s = self.adb.dump()
                        node = find_node(parse_nodes(s), tab_name, cls='View', x2max=395)
                        if not (node and node_visible(node)):
                            break
                    else:
                        break
                if not (node and node_visible(node)):
                    continue
                ty = max(node[3] + 12, min(node[5] - 12, 2320))
                self.adb.tap((node[2] + node[4]) // 2, ty)
                time.sleep(1.5)
                for _w in range(6):
                    s = self.adb.dump()
                    if not self.verify_L1(s):
                        break
                    if first_dept is None:
                        return 'ok', s
                    row = self.find_dept_row(s, first_dept)
                    if row:
                        return 'ok', s
                    time.sleep(1.5)
                if first_dept is not None:
                    return 'notfound', s
                return 'ok', s
            # 判断滚动方向: 比较目标 tab 与当前左列首个可见 tab 在计划中的次序
            vis = [t for (t, c, x1, y1, x2, y2) in rows
                   if c == 'View' and x2 <= 395 and node_visible((t, c, x1, y1, x2, y2))]
            first = vis[0] if vis else None
            direction = 1  # 默认上滑(看后面的 tab)
            if first and plan_order:
                try:
                    fi = plan_order.index(first)
                    ti = plan_order.index(tab_name)
                    direction = 1 if ti > fi else -1
                except ValueError:
                    pass
            if direction > 0:
                self.adb.swipe(150, 2000, 150, 900, 350)
            else:
                self.adb.swipe(150, 900, 150, 2000, 350)
            time.sleep(1.6)
            s = self.adb.dump()
        return 'notfound', s

    def find_dept_row(self, s, dept_name):
        rows = parse_nodes(s)
        best = None
        for (t, c, x1, y1, x2, y2) in rows:
            if c != 'TextView' or x1 < 385 or x2 > 1081:
                continue
            if y1 < 640 or y1 > 2360:
                continue
            if t == dept_name or (len(dept_name) >= 4 and dept_name in t and len(t) <= len(dept_name) + 6):
                if node_visible((t, c, x1, y1, x2, y2)):
                    best = (t, c, x1, y1, x2, y2)
                    break
                if best is None:
                    best = (t, c, x1, y1, x2, y2)
        return best

    def open_dept(self, tab_name, dept_name, plan_order, first_dept=None):
        """L1 -> 点科室行 -> L2。返回 (ok|blocked|fail, s, tab_ok)"""
        tab_ok, s = self.select_tab(tab_name, plan_order, first_dept)
        if tab_ok != 'ok':
            return 'fail', s, tab_ok
        for i in range(7):
            row = self.find_dept_row(s, dept_name)
            if row and node_visible(row):
                self.adb.tap((row[2] + row[4]) // 2, (row[3] + row[5]) // 2)
                # 轮询等待 L2 渲染(代替固定 3.2s); 夜间 WebView 偶发 >10s
                time.sleep(1.0)
                s = ''
                for _w in range(5):
                    s = self.adb.dump()
                    if page_kind(s) in ('L2', 'L2b', 'doc', 'six', 'blocked', 'auth'):  # S3: six=医生主页指纹命中
                        break
                    # 科室页已现(有 预约挂号 头 + 无 选科室/医生主页)但子tab/按钮尚未渲染
                    if YJ in s and '选科室' not in s and '医生主页' not in s:
                        break
                    time.sleep(2.5)
                k = page_kind(s)
                if k not in ('L2', 'L2b', 'doc', 'six', 'blocked', 'auth') and \
                        YJ in s and '选科室' not in s and '医生主页' not in s and dept_name in s:
                    k = 'L2b'   # 科室页(标题已现)但子tab/按钮未渲染完 → 按无条变体降级解析
                if k == 'blocked':
                    return 'blocked', s, 'ok'
                if k == 'auth':
                    self.agree_auth(s); time.sleep(1.0)
                    s = self.adb.dump()
                    k = page_kind(s)
                if k in ('L2', 'L2b', 'doc', 'six'):
                    return 'ok', s, 'ok'
                return 'fail', s, 'ok'
            # 右栏滚动找行
            self.adb.swipe(700, 2000, 700, 1000, 350)
            time.sleep(1.4)
            s = self.adb.dump()
        return 'fail', s, 'ok'

# ---------------- 解析: 态势层 ----------------
def parse_bar(s, today):
    """14 天日期条 -> [date_iso, weekday, has_slot] + anomaly 说明。
    顺序 = XML 文档序(van-tabs DOM 序 = 日期序)。**不能**用 bounds 排序:
    横滑条屏外项 bounds=[0,0][0,0](探路 C 同款现象), bounds 序会乱。"""
    items = []
    for n in re.findall(r'<node [^>]*?/?>', s):
        tm = re.search(r'text="([^"]*)"', n)
        if not tm:
            continue
        m = BAR_RE.match(tm.group(1))
        if m:
            items.append((m.group(1), m.group(2), m.group(3) == '有号'))
    if not items:
        return None, 'no_bar'
    # 年推断: 取 >= today+1 的最近一次出现
    out = []
    for (wd, mmdd, has) in items:
        mo, dd = int(mmdd.split('-')[0]), int(mmdd.split('-')[1])
        try:
            d = dt.date(today.year, mo, dd)
        except ValueError:
            d = dt.date(today.year, mo, 1)
        if d < today:
            d = d.replace(year=today.year + 1)
        out.append({'date': d.isoformat(), 'weekday': wd, 'has_slot': has})
    # S3 坑4: bar 长度可变(新华 20 天 / 十院眼科 29 天, 非固定 14) => 按实际长度验「自 today+1 连续」
    exp = [(today + dt.timedelta(days=i)).isoformat() for i in range(1, len(out) + 1)]
    got = [x['date'] for x in out]
    anomaly = None
    if got != exp:
        if got[:1] == exp[:1] and len(got) >= 5:
            anomaly = 'bar_dates_shifted(n=%d)' % len(got)
        else:
            anomaly = 'bar_unexpected(%s..%s)' % (got[0], got[-1])
    return out, anomaly

def parse_l2(s, dept_name, today, adb):
    """L2 科室页 -> SlotResult(态势层)。"""
    bar, anomaly = parse_bar(s, today)
    # S8: 去重保序(parse_nodes 按 (y,x) 排, 子 tab 同一横排 => x 序=视觉序)
    sub_tabs = []
    for (t, c, *_) in parse_nodes(s):
        if t in SUB_TABS and t not in sub_tabs:
            sub_tabs.append(t)
    bookable = full = 0
    for (t, c, x1, y1, x2, y2) in parse_nodes(s):
        if t == '去预约':
            bookable += 1
        elif t == '已约满':
            full += 1
    docs = []
    for (t, c, *_ ) in parse_nodes(s):
        m = DOC_TITLE_RE.match(t)
        if m:
            docs.append({'name': m.group(1), 'title': m.group(2)})
    empty = ('暂无可预约号源' in s) or ('暂无可预约' in s)
    earliest = None
    if bar:
        for d in bar:
            if d['has_slot']:
                earliest = d['date']; break
    # S3 坑1(新华眼科实测): L2 部门 bar「有号」≠ 医生个人有号(bar 疑陈旧/部门级: 连续 5 天有号
    # 而医生主页 20 天全无号+暂无可预约) => bar 只作 hint(bar_unreliable), 禁止用 bar 单独判
    # status=ok; 号源事实只取医生主页时段行(明细层)。bar 与页内事实冲突 => bar_conflict 显式记录。
    bar_has = bool(bar) and any(d['has_slot'] for d in bar)
    bar_conflict = None
    if bar and bar_has and empty:
        bar_conflict = 'bar_has_slot_but_empty_state(bar %d 天有号, 页内却暂无可预约)' % len(bar)
    if bar and (not bar_has) and bookable:
        bar_conflict = 'bar_all_no_slot_but_bookable_btns(n=%d)' % bookable
    if bar is None:
        # L2b 无日期条变体(社区中心): 医生卡二值 => 未验证 hint(号源结构差异)
        status = 'bar_hint' if bookable > 0 else 'no_slot'
    elif bookable == 0 and not bar_has:
        status = 'no_slot'
    else:
        status = 'bar_hint'   # bar 有号/有可点医生卡 => 未验证, 明细层(医生主页时段行)为准
    res = {
        'status': status,
        'page_variant': ('l2_bar' if bar is not None else 'l2_nobar'),
        'days_14': bar,
        'bar_days': (len(bar) if bar else None),
        'bar_hint': ('bar_unreliable' if (bar and (bar_has or status == 'bar_hint')) else None),
        'bar_conflict': bar_conflict,
        'earliest_bookable': earliest,
        'earliest_bookable_source': ('bar_hint' if bar_has else None),
        'earliest_slot': ({'date': earliest, 'time_range': None} if earliest else None),
        'remaining_quota': None,          # 态势层无数字余量(探路 B); 明细层才填可约时段数
        'fee_tier': None,
        'bookable_doctor_count': bookable,
        'full_doctor_count': full,
        'doctors': docs[:12],
        'sub_tabs': sub_tabs,
        'empty_state_text': empty,
        'bar_anomaly': anomaly,
        'title_match': (dept_name in s),
        'ttl_hint_min': TTL_HINT_MIN,
        'error_reason': None if status != 'error' else 'parse_fail',
    }
    return res

def parse_doc_home(s, today):
    """医生主页(明细层) -> 时段/价格/可约时段数。
    S8: 时段块按 **XML 文档序**(DOM 序=日期序)解析, 不再用 (y,x) bounds 排序:
    「显示全部排班」展开后, 屏外行 bounds 被压平到 y=2347(裁剪零高), bounds 排序会把
    费用/类型/按钮 挂错行(稳定排序下 x 序 行<类型<按钮 交错, 费用全丢)。文档序稳定正确。
    块终止于: 展开按钮(显示/展开全部排班) / 推荐医生 / 预约须知 / 医生名卡节点
    (推荐医生区也有「去预约」按钮, 必须与最后一条时段行隔离)。"""
    bar, anomaly = parse_bar(s, today)
    slots = []
    cur = None

    def _node_loc(nm, n):
        """S13 (真值交换格式): 文档序节点 -> {'text_off': **字节偏移**, 'node_bounds': ...}。"""
        tm2 = re.search(r'text="([^"]*)"', n)
        if tm2 is None:
            return None
        bm2 = re.search(r'bounds="(\[\d+,\d+\]\[\d+,\d+\])"', n)
        char_off = nm.start() + tm2.start(1)
        return {'text_off': len(s[:char_off].encode('utf-8')),
                'node_bounds': bm2.group(1) if bm2 else None}

    for nm in re.finditer(r'<node [^>]*?/?>', s):
        n = nm.group(0)
        tm = re.search(r'text="([^"]*)"', n)
        if not tm:
            continue
        t = tm.group(1)
        if not t.strip():
            continue
        if all(0xE000 <= ord(c) <= 0xF8FF for c in t):
            continue
        m = SLOT_ROW_RE.match(t)
        if m:
            if cur is not None:
                slots.append(cur)
            cur = {'date': m.group(1), 'time_range': m.group(2) + '-' + m.group(3),
                   'row_text': m.group(0),    # 六字段: 就诊时间用页内原文(含星期)
                   'row_loc': _node_loc(nm, n)}   # S13: 就诊时间源文本定位(字节偏移)
            continue
        if cur is None:
            continue
        if t in EXPAND_TEXTS or t in ('推荐医生', '预约须知') \
                or DOC_TITLE_RE.match(t) or DOC_NAME_ONLY_RE.match(t):
            # 时段块结束(展开按钮含收起态 / 推荐医生区 / 医生名卡)
            slots.append(cur)
            cur = None
            continue
        mf = FEE_RE.match(t)
        if mf:
            cur['fee_cny'] = int(mf.group(1))
            cur['fee_raw'] = t            # S13: 费用节点 XML 原串逐字(实测全角 ￥519)
            cur['fee_loc'] = _node_loc(nm, n)
            continue
        # S3: 行级门诊类型(S1 实测「专家门诊」在时段行块内); 只取 >=4 字词避免误抓 简介/科室名 的「门诊」
        if 'outpatient_type' not in cur:
            for tok in six_fields.OUTPATIENT_TYPES:
                if len(tok) >= 4 and tok in t:
                    cur['outpatient_type'] = tok
                    # S13: 类型独立节点(实测每行一个「专家门诊」节点)才存定位; 子串命中不猜
                    cur['type_loc'] = _node_loc(nm, n) if t == tok else None
                    break
        if t in ('去预约', '已约满'):
            cur['bookable'] = (t == '去预约')
            continue
    if cur is not None:
        slots.append(cur)
    bookable_slots = sum(1 for x in slots if x.get('bookable'))
    fees = [x.get('fee_cny') for x in slots if x.get('fee_cny') is not None]
    fee_tier = None
    if fees:
        f = min(fees)
        fee_tier = '15' if f <= 15 else ('50' if f <= 50 else '100+')
    dates = sorted(set(x['date'] for x in slots))
    # S13: 只认 SHOW 串 = 折叠态; 「收起排班」在场 = 已展开(收起按钮) = 未折叠
    collapsed = any(x in s for x in EXPAND_SHOW_TEXTS)
    return {
        'bar': bar, 'bar_anomaly': anomaly,
        'slot_rows_shown': len(slots),
        'bookable_time_slots': bookable_slots,   # = remaining_quota 口径(探路 B)
        'full_time_slots': len(slots) - bookable_slots,
        'fees_cny': sorted(set(fees)),
        'fee_tier': fee_tier,
        'dates_shown': dates,
        'schedule_collapsed': collapsed,   # True = 还有"展开全部排班", 计数为可见下限
        'slots': slots[:40],
    }

def fee_tier_from(cny):
    if cny is None:
        return None
    return '15' if cny <= 15 else ('50' if cny <= 50 else '100+')


# S8: 实测 L2 专长文本首字为「擅」(U+64C5), 旧过滤 startswith('专长'[U+4E13]) 从未命中
# (可见卡靠 y 序侥幸配对正确; 裁剪卡同 y 时专长文本会漏进姓名池) => 两字形兼容 + 长度守卫。
SPEC_PREFIXES = ('\u4e13\u957f', '\u64c5\u957f')   # 专长 / 擅長(实测首字 U+64C5, 长=U+957F)


def _is_name_node(t):
    """L2 卡身姓名节点判定: 短 CJK(姓名/姓名+职称, <=12 字); 排除 专长/擅長 与按钮词。"""
    if not t or not re.search(r'[\u4e00-\u9fff]', t):
        return False
    # S19(V2): 原生顶栏/导航文本永不做卡身姓名 — 列表滚动后顶部按钮(y≈257)与顶栏标题
    # (y≈150-218)会落进 160px 配对窗被误配成假卡(10:17 跑 doc#9 '预约挂号' 根因)
    if t in ('去预约', '已约满', '预约挂号', '选科室', '我的预约', '就诊人', '我的收藏'):
        return False
    if t.startswith(SPEC_PREFIXES):
        return False
    # S9: 排除含全角/半角冒号的文本 — 实测专病门诊子 tab 卡内描述行「简介：无」会因 y 邻近
    # 被误配成卡身姓名(首跑出现 2 条 姓名='简介：无'); 姓名/诊所名不含冒号
    if '\uff1a' in t or ':' in t:
        return False
    return len(t) <= 12


def _find_doc_cards(rows2):
    """L2 医生卡枚举: 每个「去预约/已约满」按钮(x1>800)配对同卡身姓名节点
    (240<=x1<860, 含 CJK, 非 专长/按钮词, y 落在按钮纵向范围内)。
    S3: 点卡身=个人版(六字段齐全); **不点**「去预约」按钮(=科室组版, 缺就诊医生)。
    S8: 屏外裁剪卡(bounds 压平到 y=2347 零高, 十院眼科 L2 实测 5 张)窗口法会把
    5 个裁剪按钮全部配到第一个姓名 => 裁剪按钮改按 **XML 文档序**(DOM 卡序:
    姓名→专长→按钮)与裁剪姓名一一对应。
    返回 [{'name_node': (t,c,x1,y1,x2,y2), 'btn': (...)}] 按 y 序(可见卡在前, 裁剪卡在后)。"""
    btns = [n for n in rows2 if n[0] in ('去预约', '已约满') and n[2] > 800]
    cards = []
    clipped_btns = []
    for b in btns:
        if b[5] - b[3] <= 0:   # 零高 = 屏外裁剪
            clipped_btns.append(b)
            continue
        name = None
        for (t, c, x1, y1, x2, y2) in rows2:
            # S9: 右界 860->1081: 实测无号卡姓名行跨整行 [244,*][1035,*], 旧过滤会整卡漏掉;
            # 加 x1<=500 排除右侧元素('展开' x1=939 / 去预约按钮 x1=849), 姓名实测恒 x1=244
            if x1 < 240 or x1 > 500 or x2 > 1081:
                continue
            if not _is_name_node(t):
                continue
            # S9: 上界 -60 -> -160: 专病门诊卡布局 诊所名/简介行/按钮 三行 — 实测首跑「简介：无」
            # 落在旧窗口内(距按钮 y1 <=60px), 而诊所名在简介行上方 ~76px => 诊所名距按钮 y1
            # 最远 ~136px; 160 罩住本卡诊所名, 且够不到上一卡(卡间距 ~242px, 上卡名 ~-378px)
            if y1 < b[3] - 160 or y1 > b[5]:
                continue
            name = (t, c, x1, y1, x2, y2)
            break
        if name:
            cards.append({'name_node': name, 'btn': b})
    if clipped_btns:
        cnames = []
        for (t, c, x1, y1, x2, y2) in rows2:
            # S9: 与上同: 裁剪姓名同样放宽(跨整行 [244,*][1035,*] 的裁剪姓名不能漏)
            if x1 < 240 or x1 > 500 or x2 > 1081:
                continue
            if not _is_name_node(t):
                continue
            if y2 - y1 <= 0 and y1 >= 2200:   # 姓名同样被裁剪(零高, 屏幕底)
                cnames.append((t, c, x1, y1, x2, y2))
        for i, b in enumerate(clipped_btns):
            if i < len(cnames):
                cards.append({'name_node': cnames[i], 'btn': b})
    cards.sort(key=lambda c: (0, c['name_node'][3])
               if (c['name_node'][5] - c['name_node'][3]) > 0 else (1, c['name_node'][3]))
    return cards


def _expected_doc_name(t):
    """L2 卡身节点文本 -> 期望姓名: '徐挺 主任医师'->'徐挺'; '徐健江'->'徐健江'。"""
    m = DOC_TITLE_RE.match(t)
    if m:
        return m.group(1)
    m = DOC_NAME_ONLY_RE.match(t)
    if m:
        return m.group(1)
    parts = t.split()
    return parts[0] if parts else t


def _doc_name_only(t):
    """医生名归一(仅姓名, 与 _expected_doc_name 同口径): '徐峰主任医师'->'徐峰'; '徐健江'->'徐健江'。"""
    return _expected_doc_name(t or '')


# ================= S13: L2 卡体滚动纪律 (S11 H3 修复, 2026-10-03) =================
# 实测几何(十院眼科, 屏幕 1080x2392): 内容区 LinearLayout [0,0][1080,2347]; 屏外卡体被压平
# 为零高裁剪节点(底部 y=2347 / 顶部 y=241); 首屏卡中心 ∈ [1081,2192]; 日期条 y≈702-811、
# 子 tab 行 y≈871-1000 — 卡体中心落进这些区域时 tap 会命中日期 chip/子 tab 而非卡。
L2_LIST_X = 540        # 列表区 swipe/tap 的 x(卡体 x 244..852 中线)
L2_TAP_Y_MIN = 1050    # 卡体中心 y 下限(必须低于子 tab 行 871-1000)
L2_TAP_Y_MAX = 2200    # 卡体中心 y 上限(必须高于内容区底 2347, 保证卡完整可见)


def _card_in_view(nm):
    """S13: 卡体姓名节点是否可直接 tap(真实高 + 中心 y 在安全区 [L2_TAP_Y_MIN, L2_TAP_Y_MAX])。
    零高裁剪节点(高<=20)与中心越界节点一律判不可直接 tap — 直接 tap 其中心
    (如首屏裁剪卡 (548,2347))落在屏幕底边无卡区域 => 不跳转(S11 C2 实测)。"""
    if not node_visible(nm):
        return False
    cy = (nm[3] + nm[5]) // 2
    return L2_TAP_Y_MIN <= cy <= L2_TAP_Y_MAX


def _ensure_card_visible(adb, name, max_steps=8, settle=1.6):
    """S13 (H3 核心修复): tap 前把 L2 上指定姓名的卡带入安全可视区, 返回其**最新**姓名节点;
    小步 swipe + 重 dump + 重定位(DOM 同名节点); 带不进 => None(调用方记 card_tap_no_navigate,
    不猜, 绝不对零高裁剪节点发 tap)。
    方向判定: 底部零高裁剪(y≈2347) => 内容上移(swipe up); 顶部零高裁剪(y≈241) => 内容下移
    (swipe down); 可见但中心越界 => 向中心区小步微调; 未找到 => 默认内容下移(枚举后停在列表
    底部, 目标通常在上方), 连续 2 次 dump 卡集不变(到顶)仍未找到 => 翻向内容上移(目标在下方
    或懒加载未渲染, 向下滚触发加载)。等价 ADB 真值 §7.2 第 1 步『滚动+重新 dump, 直到目标
    姓名节点 y2>y1』。回归断言(S11 C2/C3): 首屏 `input tap 548 2347` 必须仍留在 L2(底边无害
    无效坐标); 上滑使卡 y2>y1 后点姓名中心必须进主页。"""
    direction = None
    stable = 0
    prev_key = None
    for _step in range(max_steps + 1):
        s = adb.dump()
        if page_kind(s) not in ('L2', 'L2b'):
            return None   # 离开 L2: 交给调用方处理
        rows = parse_nodes(s)
        vis = clip_top = clip_bot = None
        names_now = []
        for (t, c, x1, y1, x2, y2) in rows:
            if t:
                names_now.append(t)
            if t != name:
                continue
            if node_visible((t, c, x1, y1, x2, y2)):
                vis = (t, c, x1, y1, x2, y2)
            elif y2 - y1 <= 0:
                if y1 < 600:
                    clip_top = (t, c, x1, y1, x2, y2)
                else:
                    clip_bot = (t, c, x1, y1, x2, y2)
        if vis is not None:
            if _card_in_view(vis):
                return vis
            cy = (vis[3] + vis[5]) // 2
            direction = 'down' if cy < L2_TAP_Y_MIN else 'up'   # 偏上 => 内容下移; 偏下 => 内容上移
            step = 400                                          # 可见但越界: 小步微调
        else:
            if clip_bot is not None:
                direction = 'up'
            elif clip_top is not None:
                direction = 'down'
            elif direction is None:
                direction = 'down'
            step = 800                                          # 不可见: 大步
        if direction == 'up':
            adb.swipe(L2_LIST_X, 1900, L2_LIST_X, max(300, 1900 - step), 350)
        else:
            adb.swipe(L2_LIST_X, 1100, L2_LIST_X, min(2300, 1100 + step), 350)
        time.sleep(settle)
        key = tuple(names_now)
        stable = stable + 1 if key == prev_key else 0
        prev_key = key
        if stable >= 2 and vis is None:
            # 已到列表一端(连续 2 次 dump 卡集不变)但目标仍未出现 => 翻向继续
            direction = 'up' if direction == 'down' else 'down'
            stable = 0
    return None


def _scroll_l2_to_top(adb, s, max_steps=14, settle=1.2):
    """S19(V2): 把 L2 列表滚回顶部 => (s, ok)。
    采完一个子 tab 后列表停在底部(懒加载) => 子 tab 行被裁剪为零高(实测 y=241),
    首屏子 tab 坐标失效: 直接 tap 会命中医生卡离开 L2
    (2026-10-04 10:17 跑 普通门诊/专病门诊 nav_fail 根因)。
    成功条件: find_subtab_nodes 非空(子 tab 行可见且非零高); 已在顶部则立即返回。"""
    for _i in range(max_steps + 1):
        if find_subtab_nodes(parse_nodes(s)):
            return s, True
        adb.swipe(L2_LIST_X, 1100, L2_LIST_X, 2000, 350)   # 内容下移 = 向上滚
        time.sleep(settle)
        s = adb.dump()
        if page_kind(s) not in ('L2', 'L2b'):
            return s, False
    return s, bool(find_subtab_nodes(parse_nodes(s)))


def _enumerate_l2_cards(adb, s0, max_swipes=12, settle=1.6):
    """S13 (H3 姊妹修复): 滚动到底枚举 L2 全部医生卡 — 首屏单次 dump 会漏卡:
    ① 屏外卡是零高裁剪节点(首屏只见 5/10), ② 列表懒加载(实测滚一次 20→21)。
    每 swipe 小步 + 重 dump; 卡集按姓名文本合并去重(偏好真实高几何, 顺序=首见序=DOM 序);
    停止条件 = **末尾(卡名集+末 3 卡 y 几何)连续 2 次 dump 稳定**(ADB 真值 §7.3 纪律
    『滚动到底(尾部两次 dump 稳定)』)或 max_swipes 封顶(记 WARN)。
    返回 (s_last, merged_cards)。"""
    merged = {}
    order = []

    def _merge(dump_s):
        for c in _find_doc_cards(parse_nodes(dump_s)):
            nm = c['name_node'][0]
            vis = (c['name_node'][5] - c['name_node'][3]) > 0
            old = merged.get(nm)
            if old is None:
                merged[nm] = c
                order.append(nm)
            elif vis and (old['name_node'][5] - old['name_node'][3]) <= 0:
                merged[nm] = c   # 偏好真实高(可 tap)几何

    def _tail_key(dump_s):
        cards = _find_doc_cards(parse_nodes(dump_s))
        names = tuple(c['name_node'][0] for c in cards)
        tail = tuple((c['name_node'][3], c['name_node'][5]) for c in cards[-3:])
        return (names, tail)

    s = s0
    if page_kind(s) not in ('L2', 'L2b'):
        return s, []
    _merge(s)
    prev = _tail_key(s)
    stable = 0
    for _i in range(max_swipes):
        adb.swipe(L2_LIST_X, 1900, L2_LIST_X, 1100, 350)   # 内容上移: 看下方
        time.sleep(settle)
        s = adb.dump()
        if page_kind(s) not in ('L2', 'L2b'):
            break
        _merge(s)
        cur = _tail_key(s)
        if cur == prev:
            stable += 1
            if stable >= 2:
                break   # 末尾连续 2 次 dump 稳定 => 到底, 枚举完成
        else:
            stable = 0
        prev = cur
    else:
        log('  WARN: L2 卡枚举 max_swipes=%d 仍未末尾稳定(列表超长/仍在懒加载), 按已枚举继续' % max_swipes)
    return s, [merged[n] for n in order]


# ================= S8: --dept 模式(子 tab 枚举 + 排班完整性, 2026-10-03) =================
# 用户验收口径: 脚本结果 == ADB 独立真值, 逐条一致。上次(十院眼科)缺陷:
#   1) L2 只采了默认子 tab(专家门诊), 漏 普通门诊/专病门诊 => 现在枚举子 tab 逐个采;
#   2) 展开按钮实测文本「显示全部排班」, 旧代码写死「展开全部排班」从未点中 => 两串兼容 + 记前后行数;
#   3) CJK 参数被 cmd 代码页损坏(眼科|眼 -> \ufffd\u06ff...) => --dept-file 免损坏通道 + 乱码显式报错。


def safe_fname(x):
    """文件名安全组件(与 six_fields.save_raw_evidence 同口径)。"""
    return re.sub(r'[^\w\u4e00-\u9fff-]', '_', str(x))[:24]


def find_subtab_nodes(rows):
    """L2 子 tab 节点(SUB_TABS 文本)带几何, 按 x 左->右, 按名去重。
    实测(十院眼科 L2): class=View 横排(y=871: x=112..399 / 396..683 / 680..967; 另有 y=468 变体),
    宽 ~287, 高 >20。带宽过滤 300<y1<1100 避免误抓医生卡区/日期条同名文本。"""
    out = []
    for (t, c, x1, y1, x2, y2) in rows:
        if t not in SUB_TABS:
            continue
        if not (300 < y1 < 1100 and (x2 - x1) > 80 and (y2 - y1) > 20):
            continue
        if any(o[0] == t for o in out):
            continue
        out.append((t, c, x1, y1, x2, y2))
    out.sort(key=lambda r: r[2])
    return out


def find_expand_node(rows):
    """医生主页排班展开按钮(**可点**的只有折叠态): 「显示全部排班」(实测真文本)/
    「展开全部排班」(旧变体)。S13: 收起态「收起排班」**不得**返回 — 点它会折叠回 3 行;
    收起态的存在性由 find_expand_state 检测(二次进入已展开, 无需点)。"""
    for (t, c, x1, y1, x2, y2) in rows:
        if t in EXPAND_SHOW_TEXTS and node_visible((t, c, x1, y1, x2, y2)):
            return (t, c, x1, y1, x2, y2)
    return None


def find_expand_state(rows):
    """S13: 排班 toggle 按钮任意态的可见节点(显示/展开全部排班 | 收起排班), 供 exp 记录标注;
    无按钮(页面无折叠功能/全量已渲染) => None。"""
    for (t, c, x1, y1, x2, y2) in rows:
        if t in EXPAND_TEXTS and node_visible((t, c, x1, y1, x2, y2)):
            return (t, c, x1, y1, x2, y2)
    return None


def parse_dept_spec(s, label):
    """'医院,tab,dept'(兼容全角逗号) -> (h, t, d)。
    段数不对/空字段/U+FFFD 乱码 => ValueError(不许静默按空串/乱码跑)。"""
    parts = re.split(r'[,，]', s or '')
    if len(parts) != 3:
        raise ValueError('%s: 应为三段"医院,tab,dept", 实得 %d 段: %r'
                         % (label, len(parts), (s or '')[:60]))
    h, t, d = (p.strip() for p in parts)
    for name, v in (('医院', h), ('tab', t), ('dept', d)):
        if not v:
            raise ValueError('%s: %s 段为空(疑似 CJK 参数被 cmd 代码页损坏成空; 改用 --dept-file 免损坏通道)'
                             % (label, name))
        if '\ufffd' in v or any(ord(c) < 0x20 for c in v):
            raise ValueError('%s: %s 段含 U+FFFD/控制字符 = CJK 参数被 cmd 代码页损坏'
                             '(先 chcp 65001 & set PYTHONUTF8=1, 或改用 --dept-file <UTF-8 json>): %r'
                             % (label, name, v[:40]))
    return h, t, d


def load_dept_file(path):
    """--dept-file: UTF-8 JSON, CJK 免损坏通道。两种形态:
      {"depts": [{"hospital","tab","dept"}, ...], "device"?: "...", "deadline"?: "09:00"}
      ["医院,tab,dept", ...] 或 [["医院","tab","dept"], ...]
    返回 (specs, device_or_None, deadline_or_None)。"""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        raise SystemExit('dept-file 读失败(%s): %s (要求 UTF-8 JSON)' % (path, str(e)[:100]))
    specs, device, deadline = [], None, None
    items = data
    if isinstance(data, dict):
        device = data.get('device')
        deadline = data.get('deadline')
        items = data.get('depts')
        if items is None:
            items = data.get('specs')
    if not isinstance(items, list) or not items:
        raise SystemExit('dept-file %s: 无 depts 列表(期望 {"depts":[...]} 或列表)' % path)
    for i, it in enumerate(items):
        if isinstance(it, dict):
            h, t, d = it.get('hospital'), it.get('tab'), it.get('dept')
            raw = '%s,%s,%s' % (h or '', t or '', d or '')
        elif isinstance(it, (list, tuple)):
            if len(it) != 3:
                raise SystemExit('dept-file %s: 第 %d 项应为 3 元素 [医院,tab,dept]: %r'
                                 % (path, i, str(it)[:60]))
            h, t, d = it
            raw = '%s,%s,%s' % (h or '', t or '', d or '')
        elif isinstance(it, str):
            raw = it
        else:
            raise SystemExit('dept-file %s: 第 %d 项类型不支持: %r' % (path, i, str(it)[:60]))
        try:
            specs.append(parse_dept_spec(raw, '%s#%d' % (os.path.basename(path), i)))
        except ValueError as e:
            raise SystemExit('dept-file 项错误: %s' % e)
    return specs, device, deadline


def _bump(d, k):
    d[k] = d.get(k, 0) + 1


def _cards_key(rows):
    """L2 医生卡集合指纹(姓名文本多重集), 用于子 tab 切换后判断重渲染完成。"""
    return tuple(sorted(c['name_node'][0] for c in _find_doc_cards(rows)))


def _tap_subtab_and_wait(nav, adb, node, prev_key):
    """点 L2 子 tab 并轮询等右栏重渲染 => (status, s)。
    status: ok=出现新卡集或空态 / no_cards=等待渲染后仍无医生卡且无空态文本(列表未渲染完/空态缺文本) /
            unchanged=仍是上一子 tab 卡集且无空态(点击未生效, 不可信, 不采) / left=页面离开 L2。
    WebView 切 tab 实测可 >10s => 轮询 8 次。
    S9: 点节点中心(S7 实测中心 (539,935) 有效; 旧公式钉在 y2-12 贴近下划线指示器);
    切 tab 后列表 DOM 重建+懒加载重触发, 前几次 dump 卡集为空 () — 必须与"点击未生效"
    (卡集==prev) 区分, 不能提前判 ok; no_cards/unchanged => 补点 1 次(同一操作最多 2 次)再轮询。"""
    cx = (node[2] + node[4]) // 2
    cy = (node[3] + node[5]) // 2
    log('  subtab tap %r center=(%d,%d)' % (node[0], cx, cy))
    adb.tap(cx, cy)
    time.sleep(1.5)

    def _poll(n):
        s = ''
        for _w in range(n):
            s = adb.dump()
            k = page_kind(s)
            if k not in ('L2', 'L2b'):
                return 'left', s
            if '暂无可预约' in s:
                return 'ok', s
            nk = _cards_key(parse_nodes(s))
            if nk and nk != prev_key:
                return 'ok', s
            time.sleep(1.5)
        nk = _cards_key(parse_nodes(s))
        if nk == prev_key:
            return 'unchanged', s
        return 'no_cards', s

    st, s = _poll(8)
    if st in ('no_cards', 'unchanged'):
        log('  subtab tap retry#1 %r (%s after 8 polls)' % (node[0], st))
        adb.tap(cx, cy)
        time.sleep(2.0)
        st, s = _poll(6)
    return st, s


def fetch_one_dept_unit(nav, adb, m, hosp, hid, tab, dept, today, plan_order, ts_base,
                        jsonl_fh, six_seen):
    """采一个 (tab,dept) 单元(S8): 复用 Nav.open_dept 到 L2 => 枚举全部子 tab 逐个切 =>
    每子 tab 枚举全部医生卡(有号/无号都记) => 对有号医生**点卡身**进医生主页 =>
    「显示/展开全部排班」展开(记前后行数) => 六字段逐条写 jsonl_fh(dept_runs 独立文件)。
    six_seen = 全局 (dept,sub,doctor,row) 去重集。返回 (unit_summary_dict, blocked_bool)。
    error 原因分类: nav_fail(导航失败) / no_doctor_cards(无医生卡) / unknown_page_variant(页面变体未知)
    / blocked(风控) / exception。"""
    t0 = time.monotonic()
    base = '%s_%s_%s_%s' % (hid, safe_fname(tab), safe_fname(dept), ts_base)
    unit = {
        'hospital_id': hid, 'hospital': hosp, 'tab': tab, 'dept': dept,
        'status': 'error', 'error_reason': None, 'interrupted': None,
        'sub_tabs': [], 'per_sub_tab': {}, 'six_records_total': 0, 'variants': {},
        'cost_s': None,
        'jsonl_file': os.path.relpath(os.path.join(DEPT_RUNS_DIR, base + '.jsonl'), WS).replace('\\', '/'),
        'summary_file': os.path.relpath(os.path.join(DEPT_RUNS_DIR, base + '_summary.json'), WS).replace('\\', '/'),
        'captured_at': dt.datetime.now().isoformat(timespec='seconds'),
    }
    first_d = None
    for t in m['features']['bookable_departments']['value']['l1']:
        if t['name'] == tab:
            first_d = ([d.get('raw') for d in t.get('depts', [])] or [None])[0]
            break

    ost, s2, tab_ok = nav.open_dept(tab, dept, plan_order, first_d)
    if ost == 'blocked':
        unit['error_reason'] = 'blocked'
        unit['cost_s'] = round(time.monotonic() - t0, 1)
        return unit, True
    if ost != 'ok':
        unit['error_reason'] = 'nav_fail(open_dept=%s,tab_ok=%s): 未到达 L2' % (ost, tab_ok)
        unit['cost_s'] = round(time.monotonic() - t0, 1)
        return unit, False

    rows = parse_nodes(s2)
    subs = find_subtab_nodes(rows)
    if not subs:
        # S9: 子 tab 行可能尚未渲染(WebView 渲染滞后; 滚动态下子 tab 文本还会被裁剪成
        # y=241 零高 => find_subtab_nodes 返回空) => 短等+重 dump 最多 3 轮, 别急着判"无子 tab"
        for _r in range(3):
            time.sleep(2.0)
            s2 = adb.dump()
            if page_kind(s2) not in ('L2', 'L2b'):
                break
            rows = parse_nodes(s2)
            subs = find_subtab_nodes(rows)
            if subs:
                break
        else:
            log('  WARN: subtab row not rendered after 3 waits -> treat as no sub tab')
    sub_names = [x[0] for x in subs]
    if not sub_names:
        sub_names = ['(单子列,无子tab)']
        subs = [None]
    unit['sub_tabs'] = sub_names
    ctx = {'hospital_name': hosp, 'dept_name': dept}
    prev_key = _cards_key(rows)
    n_six = 0
    any_sub_info = False

    for i, subname in enumerate(sub_names):
        t1 = time.monotonic()
        ss = {'status': 'ok', 'error_reason': None, 'doctor_count': 0,
              'bookable_count': 0, 'full_count': 0, 'doctors': [], 'bookable_doctors': [],
              'full_doctors': [], 'empty_state': ('暂无可预约' in s2),
              'six_records': 0, 'expands': [], 'variants': {}, 'cost_s': None}
        if i > 0:
            # S19(V2): 采集后列表已在底部 => 子 tab 行裁剪、首屏坐标失效;
            # 必须先滚回 L2 顶部 + 重 dump 取新子 tab 坐标再点。
            s2, ok_top = _scroll_l2_to_top(adb, s2)
            subs_now = find_subtab_nodes(parse_nodes(s2))
            node_i = subs[i]
            if subs_now:
                for cand in subs_now:
                    if cand[0] == subs[i][0]:
                        node_i = cand
                        break
            else:
                log('  WARN: 滚顶后子 tab 行仍不可见, 用首屏坐标(可能失效)')
            st, s2 = _tap_subtab_and_wait(nav, adb, node_i, prev_key)
            if st == 'left':
                ss['status'] = 'error'
                ss['error_reason'] = 'nav_fail(sub_tab_tap=%s: 页面离开 L2, kind=%s)' % (subname, page_kind(s2))
                ss['cost_s'] = round(time.monotonic() - t1, 1)
                unit['per_sub_tab'][subname] = ss
                # 页面离开 L2: 后续子 tab 的点击坐标失效 => 标记 skipped, 恢复 L1, 结束本单元
                for j in range(i + 1, len(sub_names)):
                    unit['per_sub_tab'][sub_names[j]] = {'status': 'error',
                        'error_reason': 'nav_fail(skipped: 前一子 tab 点击后页面离开 L2, 未点切)',
                        'doctor_count': 0, 'bookable_count': 0, 'full_count': 0, 'doctors': [],
                        'bookable_doctors': [], 'full_doctors': [], 'empty_state': False,
                        'six_records': 0, 'expands': [], 'variants': {}, 'cost_s': 0.0}
                unit['interrupted'] = 'left_L2_at_sub_tab=%s' % subname
                if not nav.recover_L1():
                    st2, _ = nav.enter()
                    if st2 != 'ok':
                        unit['interrupted'] = 'l1_lost(恢复 L1 失败)'
                break
            if st == 'no_cards':
                # S9: 切子 tab 后列表懒加载未渲染完(或空态缺文本): 已等待+补点 1 次仍无卡 => 记 error 不猜
                ss['status'] = 'error'
                ss['error_reason'] = ('no_doctor_cards(sub_tab_tap=%s: 切换后等待渲染并补点 1 次, 仍无医生卡且无'
                                      '"暂无可预约"空态文本; 疑列表未渲染完或空态缺文本)') % subname
                ss['cost_s'] = round(time.monotonic() - t1, 1)
                unit['per_sub_tab'][subname] = ss
                prev_key = ()
                continue
            if st == 'unchanged':
                # 诚实处理: 卡集未变且无空态 => 无法区分"切 tab 未生效"与"空子 tab 无空态文本" => 不采(不猜)
                # S9: 已先补点 1 次(同一操作最多 2 次)仍不变才走到这里
                ss['status'] = 'error'
                ss['error_reason'] = ('unknown_page_variant(sub_tab_tap=%s: 点 2 次后卡集仍与上一子 tab 相同且无空态文本, '
                                      '无法确认切换生效, 不采)') % subname
                ss['cost_s'] = round(time.monotonic() - t1, 1)
                unit['per_sub_tab'][subname] = ss
                prev_key = ()
                continue
        # S13 (S11 H3): 首屏单次 dump 漏卡(零高裁剪 + 懒加载, 实测 20→21) => 滚动到底,
        # 末尾连续 2 次 dump 稳定才停(ADB 真值 §7.3 纪律); 枚举后 s2 停在列表尾部,
        # 逐卡 tap 前再由 _ensure_card_visible 滚动+重 dump 定位
        s2, cards = _enumerate_l2_cards(adb, s2)
        rows = parse_nodes(s2)
        ss['empty_state'] = ('暂无可预约' in s2)
        prev_key = _cards_key(rows)
        ss['doctor_count'] = len(cards)
        ss['doctors'] = [_expected_doc_name(c['name_node'][0]) for c in cards]
        bcards = [c for c in cards if c['btn'][0] == '去预约']
        ss['bookable_count'] = len(bcards)
        ss['bookable_doctors'] = [_expected_doc_name(c['name_node'][0]) for c in bcards]
        ss['full_doctors'] = [_expected_doc_name(c['name_node'][0]) for c in cards if c['btn'][0] == '已约满']
        if cards or ss['empty_state']:
            any_sub_info = True
        if not cards and not ss['empty_state']:
            ss['status'] = 'error'
            ss['error_reason'] = 'no_doctor_cards(无医生卡且无"暂无可预约"空态文本; 页面变体未知)'
            ss['cost_s'] = round(time.monotonic() - t1, 1)
            unit['per_sub_tab'][subname] = ss
            continue

        for di, card in enumerate(bcards):
            # S13 (S11 H3 核心修复): tap 前先把卡带入安全可视区 + 重 dump 取新坐标;
            # 绝不对零高裁剪节点直接 tap(其中心 (548,2347) 落屏幕底边, 不命中任何卡
            # => 旧代码记 card_tap_no_navigate 的根因; S11 C2/C3 回归断言见 _ensure_card_visible)
            nm = _ensure_card_visible(nav.adb, card['name_node'][0])
            if nm is None:
                _bump(ss['variants'], 'card_tap_no_navigate')
                log('  doc#%d %s: <=8 步滚动仍未带入可视区 => card_tap_no_navigate(不猜, 不 tap 底边)'
                    % (di, card['name_node'][0]))
                continue
            nav.adb.tap((nm[2] + nm[4]) // 2, (nm[3] + nm[5]) // 2)   # 点卡身(个人版), 不点「去预约」按钮
            time.sleep(3.0)
            s3 = nav.adb.dump()
            k3 = page_kind(s3)
            if k3 == 'blocked':
                nav.adb.back(); time.sleep(1.5)
                nav.adb.back(); time.sleep(1.0)
                unit['cost_s'] = round(time.monotonic() - t0, 1)
                return unit, True
            if k3 in ('L2', 'L2b'):
                # S9: 卡身点击未跳转 => 新鲜 dump 重定位该卡姓名节点补点 1 次(同一操作最多 2 次)。
                # 成因: 上一医生主页 back 回 L2 后渲染位置漂移, 子 tab 切换时的缓存坐标已偏
                # (S7 实测 fling 惯性); 补点仍不跳转才记 variant, 绝不点「去预约」兜底(组版风险)
                s3 = nav.adb.dump()
                nm2 = None
                for (t2, c2, x1b, y1b, x2b, y2b) in parse_nodes(s3):
                    if t2 == nm[0] and node_visible((t2, c2, x1b, y1b, x2b, y2b)):
                        nm2 = (t2, c2, x1b, y1b, x2b, y2b)
                        break
                if nm2 is not None:
                    nav.adb.tap((nm2[2] + nm2[4]) // 2, (nm2[3] + nm2[5]) // 2)
                    time.sleep(3.0)
                    s3 = nav.adb.dump()
                    k3 = page_kind(s3)
                    if k3 == 'blocked':
                        nav.adb.back(); time.sleep(1.5)
                        nav.adb.back(); time.sleep(1.0)
                        unit['cost_s'] = round(time.monotonic() - t0, 1)
                        return unit, True
            if k3 in ('L2', 'L2b'):
                _bump(ss['variants'], 'card_tap_no_navigate')
                continue   # 补点后仍未跳转, 不点「去预约」兜底(组版风险)
            if k3 not in ('doc', 'six'):
                _bump(ss['variants'], 'unknown_page_kind:%s' % k3)
                nav.adb.back(); time.sleep(1.5)
                continue
            if k3 == 'doc':
                # S19(V2): 指纹未命中 = WebView 渲染未全("医生主页"标题先现, "不限 日期"/
                # "医生简介"/时段行后现; 2026-10-04 10:46 跑实测专病 14/16 诊所页 miss,
                # 同模板页渲染完成后全命中) => 轮询 8x2s 重 dump(同 _tap_subtab_and_wait 纪律),
                # 仍 miss => 弃(不猜)
                for _w6 in range(8):
                    time.sleep(2.0)
                    s3 = nav.adb.dump()
                    k3 = page_kind(s3)
                    if k3 in ('six', 'blocked'):
                        break
                if k3 == 'blocked':
                    nav.adb.back(); time.sleep(1.5)
                    nav.adb.back(); time.sleep(1.0)
                    unit['cost_s'] = round(time.monotonic() - t0, 1)
                    return unit, True
                if k3 != 'six':
                    # S19(V3): 诊所页 no-bar 变体(2026-10-04 实测 眼科-青光眼 主页 = 医生简介+
                    # 预约须知+推荐医生, 无日期栏/无时段行/无"暂无可预约"; ~1min 内多次 dump 稳定)
                    # => 记 no_slot_page_no_bar 0 行(诚实, 非规则 miss); 其余指纹 miss 仍记 six_rule_miss
                    if '\u63a8\u8350\u533b\u751f' in s3:
                        _bump(ss['variants'], 'no_slot_page_no_bar')
                        log('  doc#%d %s: no-bar 变体(推荐医生在、无日期栏/无时段) => 0 行' % (di, nm[0]))
                    else:
                        _bump(ss['variants'], 'six_rule_miss')
                    nav.adb.back(); time.sleep(1.5)
                    continue
            pd = six_fields.find_page_doctor(s3)
            if pd is None:
                _bump(ss['variants'], 'group_missing_doctor')
                nav.adb.back(); time.sleep(1.8)
                continue
            if _doc_name_only(pd[0]) != _expected_doc_name(nm[0]):
                _bump(ss['variants'], 'doctor_mismatch:%s' % pd[1][:20])
                nav.adb.back(); time.sleep(1.8)
                continue
            dh = parse_doc_home(s3, today)
            rows_before = dh['slot_rows_shown']
            # S11 §4.2 (S13 附带修复): 渲染时序歧义 — 首 dump 0 行且无任何展开按钮(含收起态)
            # 视为未渲染完: 等 2.5s 重 dump 一次再定"无排班"(143634 轮 3 例该状态,
            # 16:15 ADB 侧同页 10 行; 首 dump 缺行+缺按钮不得直接记 no_slot_rows_on_home)
            if rows_before == 0 and find_expand_state(parse_nodes(s3)) is None:
                time.sleep(2.5)
                s3b = nav.adb.dump()
                if page_kind(s3b) == 'six':
                    s3 = s3b
                    dh = parse_doc_home(s3, today)
                    rows_before = dh['slot_rows_shown']
            exp = {'doctor': _expected_doc_name(nm[0]), 'button': None,
                   'rows_before': rows_before, 'rows_after': None, 'verified': None}
            ex = find_expand_node(parse_nodes(s3))   # S13: 只认 SHOW 串(折叠态); 收起态点了会折回
            if ex:
                exp['button'] = ex[0]
                nav.adb.tap((ex[2] + ex[4]) // 2, (ex[3] + ex[5]) // 2)
                time.sleep(2.5)
                s4 = nav.adb.dump()
                if page_kind(s4) == 'six':
                    s3 = s4
                    dh = parse_doc_home(s3, today)
                    exp['rows_after'] = dh['slot_rows_shown']
                    # verified: 行数增加, 或展开按钮消失(全部排班已展开)
                    exp['verified'] = (exp['rows_after'] is not None and exp['rows_after'] > rows_before) \
                        or find_expand_node(parse_nodes(s3)) is None
                else:
                    exp['verified'] = False
            else:
                # S13: 按钮是 toggle(显示全部排班 <-> 收起排班): 二次进入可能已呈收起态 =
                # 已展开 => 不点(点了会折回), 行数为全量, 如实记录按钮态
                est = find_expand_state(parse_nodes(s3))
                exp['button'] = est[0] if est else None
                exp['rows_after'] = rows_before
                exp['verified'] = True   # 无"显示/展开"按钮 = 已展开(收起态) / 无折叠
            ss['expands'].append(exp)
            if rows_before == 0:
                _bump(ss['variants'], 'no_slot_rows_on_home')
            dev_ts = nav.adb.device_date().isoformat(timespec='seconds')
            raw_rel = six_fields.save_raw_evidence(os.path.join(OUT_DIR, '_last_dump.xml'), OUT_DIR,
                                                   hid, dept, pd[0], dev_ts + '_dept')
            # S13: 真值交换格式 extract.file 用 workspace 相对路径(与安卓小能手 'tmp/...' 同基)
            # S19(V2): raw_rel 是相对 slots/ 的 'out/raw/...', 直接 join 'slots/out' 会出 out/out/raw;
            # 统一用 basename 重组为 workspace 相对路径。
            raw_ws = os.path.join(os.path.relpath(OUT_DIR, WS), six_fields.RAW_DIRNAME,
                                  os.path.basename(raw_rel)).replace('\\', '/')
            for sl in dh['slots']:
                if not sl.get('bookable'):
                    continue
                key = (dept, subname, pd[0], sl['row_text'])
                if key in six_seen:
                    continue
                rec = six_fields.build_six_record(s3, 'six', ctx, row_text=sl['row_text'],
                                                  fee=sl.get('fee_cny'), fee_raw=sl.get('fee_raw'),
                                                  doctor=pd[0],
                                                  outpatient_type=sl.get('outpatient_type'),
                                                  captured_at=dev_ts, raw_evidence=raw_ws,
                                                  hospital_id=hid,
                                                  unit={'tab': tab, 'dept': dept, 'sub_tab': subname},
                                                  doctor_idx=di,
                                                  row_loc=sl.get('row_loc'), fee_loc=sl.get('fee_loc'),
                                                  type_loc=sl.get('type_loc'), extract_file=raw_ws)
                if rec.get('status') != 'ok':
                    _bump(ss['variants'], 'six_record_error:%s' % str(rec.get('error_reason'))[:40])
                    continue
                six_seen.add(key)
                jsonl_fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
                jsonl_fh.flush()
                ss['six_records'] += 1
                n_six += 1
            # 回 L2: 校验; 丢了 => 本单元中断(已采记录保留), 恢复 L1 供下一单元
            nav.adb.back(); time.sleep(1.5)
            kback = None
            for _b in range(3):
                kback = page_kind(nav.adb.dump())
                if kback in ('L2', 'L2b'):
                    break
                nav.adb.back(); time.sleep(1.2)
            if kback not in ('L2', 'L2b'):
                _bump(ss['variants'], 'l2_lost_after_doc')
                ss['cost_s'] = round(time.monotonic() - t1, 1)
                unit['per_sub_tab'][subname] = ss
                unit['interrupted'] = 'l2_lost_after_doc(后续子 tab 未采, 已采记录有效)'
                if not nav.recover_L1():
                    st2, _ = nav.enter()
                    if st2 != 'ok':
                        unit['interrupted'] = 'l1_lost(恢复 L1 失败)'
                break
        ss['cost_s'] = round(time.monotonic() - t1, 1)
        unit['per_sub_tab'][subname] = ss
        if unit['interrupted']:
            break
    if any_sub_info:
        unit['status'] = 'ok'
    else:
        unit['error_reason'] = unit['error_reason'] or \
            'no_doctor_cards(全部子 tab: 无医生卡且无空态文本; 页面变体未知)'
    agg = {}
    for v in unit['per_sub_tab'].values():
        for k, n in (v.get('variants') or {}).items():
            agg[k] = agg.get(k, 0) + n
    unit['variants'] = agg
    unit['six_records_total'] = n_six
    unit['cost_s'] = round(time.monotonic() - t0, 1)
    return unit, False


def run_dept_mode(adb, resolved, a, today):
    """S8 --dept 模式入口。resolved = [{'spec':(医院,tab,dept), 'm':manifest, 'hid', 'hosp'}, ...]
    (manifest 已解析; 同医院相邻分组, 每家只走一次 444->搜索->L1)。
    隔离: 单医院 enter 失败/单元 error 不影响其他套; 单元级连续 error 熔断(>= a.max_consec_errors)
    => stop_reason=unit_circuit_break, 退出码 4。blocked => 退出码 3。
    返回 (rc, run_summary)。"""
    os.makedirs(DEPT_RUNS_DIR, exist_ok=True)
    ts_base = dt.datetime.now().strftime('%Y%m%dT%H%M%S')
    run_sum = {'ts': ts_base, 'device': a.device, 'mode': 'dept', 'units': [],
               'stop_reason': None, 'circuit_break': None, 'six_records_total': 0,
               'note': 'dept 模式: 结果只写 out/dept_runs/, 不写 six_fields.jsonl / 不动 raw/ 存量文件'}
    six_seen = set()
    consec_err = 0
    groups = []
    for r in resolved:
        if groups and groups[-1]['hid'] == r['hid']:
            groups[-1]['specs'].append(r)
        else:
            groups.append({'hid': r['hid'], 'm': r['m'], 'hosp': r['hosp'],
                           'plan_order': [t['name'] for t in r['m']['features']['bookable_departments']['value']['l1']],
                           'specs': [r]})
    deadline = None
    if a.deadline:
        if re.match(r'^\d{1,2}:\d{2}$', a.deadline.strip()):
            _now = dt.datetime.now()
            _hh, _mm = a.deadline.strip().split(':')
            deadline = _now.replace(hour=int(_hh), minute=int(_mm), second=0, microsecond=0)
        else:
            deadline = dt.datetime.fromisoformat(a.deadline.strip())
        log('dept-mode deadline=%s (PC clock)' % deadline.isoformat(timespec='minutes'))
    for g in groups:
        nav = Nav(adb, g['hosp'])
        st, s = nav.enter()
        if st == 'blocked':
            run_sum['stop_reason'] = 'blocked at %s entry' % g['hid']
            for r in g['specs']:
                run_sum['units'].append({'hospital_id': g['hid'], 'tab': r['spec'][1], 'dept': r['spec'][2],
                                         'status': 'error', 'error_reason': 'blocked', 'per_sub_tab': {}})
            break
        if st != 'ok':
            log('dept-mode ENTER FAIL for %s -> skip hospital (隔离, 不影响其他套)' % g['hid'])
            for r in g['specs']:
                run_sum['units'].append({'hospital_id': g['hid'], 'tab': r['spec'][1], 'dept': r['spec'][2],
                                         'status': 'error', 'error_reason': 'nav_fail(enter=%s)' % st,
                                         'per_sub_tab': {}})
            consec_err += 1
            if consec_err >= a.max_consec_errors:
                run_sum['stop_reason'] = 'unit_circuit_break'
                run_sum['circuit_break'] = 'consec_error_units=%d >= %d' % (consec_err, a.max_consec_errors)
                break
            continue
        for r in g['specs']:
            if deadline and dt.datetime.now() >= deadline:
                run_sum['stop_reason'] = 'deadline'
                break
            tab, dept = r['spec'][1], r['spec'][2]
            log('=== dept unit %s %s|%s ===' % (g['hid'], tab, dept))
            base = '%s_%s_%s_%s' % (g['hid'], safe_fname(tab), safe_fname(dept), ts_base)
            jsonl_p = os.path.join(DEPT_RUNS_DIR, base + '.jsonl')
            summ_p = os.path.join(DEPT_RUNS_DIR, base + '_summary.json')
            blocked = False
            unit = {'hospital_id': g['hid'], 'tab': tab, 'dept': dept, 'status': 'error',
                    'error_reason': 'exception:not_started', 'per_sub_tab': {}, 'sub_tabs': [],
                    'six_records_total': 0, 'cost_s': None,
                    'jsonl_file': os.path.relpath(jsonl_p, WS).replace('\\', '/'),
                    'summary_file': os.path.relpath(summ_p, WS).replace('\\', '/')}
            try:
                with open(jsonl_p, 'w', encoding='utf-8') as jf:
                    unit, blocked = fetch_one_dept_unit(nav, adb, g['m'], g['hosp'], g['hid'],
                                                        tab, dept, today, g['plan_order'],
                                                        ts_base, jf, six_seen)
            except Exception as e:
                unit['status'] = 'error'
                unit['error_reason'] = 'exception:%s' % str(e)[:120]
            tmp = summ_p + '.tmp'
            json.dump(unit, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
            os.replace(tmp, summ_p)
            if blocked:
                unit['status'] = 'error'
                run_sum['stop_reason'] = 'blocked at %s %s|%s' % (g['hid'], tab, dept)
                run_sum['units'].append(unit)
                log('DEPT MODE BLOCKED at %s|%s' % (tab, dept))
                break
            run_sum['units'].append(unit)
            run_sum['six_records_total'] += unit.get('six_records_total', 0)
            if unit.get('status') == 'error':
                consec_err += 1
                log('  unit ERROR (%s) consec=%d' % (unit.get('error_reason'), consec_err))
            else:
                consec_err = 0
                log('  unit ok: sub_tabs=%s six=%d cost=%.1fs'
                    % (unit.get('sub_tabs'), unit.get('six_records_total', 0), unit.get('cost_s') or 0))
            if consec_err >= a.max_consec_errors:
                run_sum['stop_reason'] = 'unit_circuit_break'
                run_sum['circuit_break'] = 'consec_error_units=%d >= %d' % (consec_err, a.max_consec_errors)
                log('UNIT CIRCUIT BREAK: 连续 %d 个 error 单元(>=%d) -> 中止运行(退出码 4)'
                    % (consec_err, a.max_consec_errors))
                break
            if not nav.verify_L1():
                if not nav.recover_L1():
                    st2, _ = nav.enter()
                    if st2 != 'ok':
                        run_sum['stop_reason'] = 'l1_lost_after_unit'
                        break
        if run_sum['stop_reason']:
            break
    rs_p = os.path.join(DEPT_RUNS_DIR, 'run_%s.json' % ts_base)
    tmp = rs_p + '.tmp'
    json.dump(run_sum, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    os.replace(tmp, rs_p)
    run_sum['run_summary_file'] = os.path.relpath(rs_p, WS).replace('\\', '/')
    log('=== DEPT MODE DONE: units=%d six_records=%d stop=%s break=%s ==='
        % (len(run_sum['units']), run_sum['six_records_total'],
           run_sum['stop_reason'], run_sum['circuit_break']))
    rc = 0
    if run_sum.get('circuit_break'):
        rc = 4
    elif (run_sum.get('stop_reason') or '').startswith('blocked'):
        rc = 3
    return rc, run_sum


# ---------------- 主流程 ----------------
def load_manifest(hospital_arg):
    for p in sorted(glob.glob(MANIFEST_GLOB)):
        if os.path.basename(p).startswith('_'):
            continue
        m = json.load(open(p, encoding='utf-8'))
        if m['id'] == hospital_arg or m['institution']['name'] == hospital_arg:
            return m
    # S8: 查无此院 = 手机端也搜不到该院; 显式报错(不许静默跑), 并给出 CJK 损坏排查提示
    raise SystemExit('hospital not found: %r — 该字符串无法定位 manifest(手机端搜索也会失败)。'
                     '若参数本意是中文院名: 疑似 CJK 参数被 cmd 代码页损坏(chcp 65001 + PYTHONUTF8=1, '
                     '或改用 --dept-file <UTF-8 JSON> 免损坏通道); 若用 id: 核对 SH-xxx 写法' % hospital_arg)

def build_units(m, include_all=False):
    bd = (m['features'].get('bookable_departments') or {}).get('value') or {}
    units = []
    for t in bd.get('l1', []):
        for d in t.get('depts', []):
            units.append({
                'tab': t['name'], 'tab_index': t.get('tab_index'),
                'dept': d.get('raw'), 'dept_id': d.get('dept_id'),
                'canonical': d.get('canonical'), 'kind': d.get('kind'),
                'visit_type': d.get('visit_type'),
            })
    kept = [u for u in units if u['kind'] not in PRUNE_KINDS or include_all]
    pruned = [u for u in units if u not in kept]
    return units, kept, pruned

def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE, encoding='utf-8'))
    return {}

def save_state(state):
    tmp = STATE_FILE + '.tmp'
    json.dump(state, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)

def recount(hs):
    """从 results 的**终态**幂等重算 done/status_counts（每次 persist 前调用）。
    修复: 旧的累计计数器在 error->ok 重抓后残留幽灵 error 计数, 会让
    --resume / 下游 summary 消费者误判（已修好的单元被算作"已完成但失败"）。
    cost_ms_total 是真实累计耗时, 不重算。"""
    sc = {}
    for v in hs.get('results', {}).values():
        st = (v or {}).get('status')
        if st:
            sc[st] = sc.get(st, 0) + 1
    hs['done'] = sum(sc.values())
    hs['status_counts'] = sc
    return sc

def build_summary(hs, results):
    """summary 一律从最终单元状态(results)重算, 永不取累计计数器。
    自检断言: sum(status_counts) == 实际单元数; 无残留 error
    (若有, 列出哪些单元、为什么放弃)。"""
    sc = {}
    for v in results.values():
        st = (v or {}).get('status')
        if st:
            sc[st] = sc.get(st, 0) + 1
    n = len(results)
    assert sum(sc.values()) == n, \
        'selfcheck failed: sum(status_counts)=%d != units=%d' % (sum(sc.values()), n)
    err_units = []
    for k, v in results.items():
        if (v or {}).get('status') == 'error':
            tab, dept = k.split('\x01', 1)
            err_units.append({'tab': tab, 'dept': dept, 'reason': v.get('error_reason')})
    assert sc.get('error', 0) == len(err_units)
    if not err_units:
        assert 'error' not in sc, 'selfcheck failed: status_counts has error but no error unit'
    return {
        'units_total': hs.get('units_total'),
        'units_pruned': hs.get('units_pruned'),
        'units_fetched': n,
        'status_counts': {k: sc[k] for k in ('ok', 'no_slot', 'bar_hint', 'error') if k in sc},
        'error_units': err_units,
        'selfcheck': {
            'sum_status_counts_equals_units': True,
            'no_residual_error': not err_units,
            'all_planned_units_settled': (hs.get('units_total') or 0) - (hs.get('units_pruned') or 0) == n,
        },
        'blocked': hs.get('blocked'),
        'live_tab_recon': hs.get('live_tab_recon'),
        'cost_ms_total': hs.get('cost_ms_total'),
    }

def _with_alias(r):
    """SlotResult 契约对齐: fetched_at(契约名) = captured_at(约束 D 名)。"""
    if not r:
        return r
    r = dict(r)
    if 'captured_at' in r and 'fetched_at' not in r:
        r['fetched_at'] = r['captured_at']
    return r


def write_hospital_json(hid, slug, m, tab_meta, results, state, device):
    fname = '%s-%s.json' % (hid, slug)
    out = {
        'hospital_id': hid,
        'hospital': m['institution']['name'],
        'device': device,
        'source': 'suishenban-yuyue-guahao(L1/L2 UI dump, 零模型调用)',
        'contract': 'RANKING_SPEC.md §4.4 SlotResult(字段对齐) + 态势层扩展字段',
        'tab_kind_rules': TAB_KIND_RULES_DOC,
        'tabs': [
            {'name': tn, 'tab_kind': tk,
             'depts': {dn: _with_alias(results.get(tn + '\x01' + dn)) for dn in dns
                       if results.get(tn + '\x01' + dn) is not None}}
            for tn, tk, dns in tab_meta
        ],
        # summary 从最终单元状态重算(含自检断言), 永不取累计计数器
        'summary': build_summary(state.get(hid, {}), results),
        'detail_samples': state.get(hid, {}).get('detail_samples'),
        'six_field_records': state.get(hid, {}).get('six_field_records'),  # 六字段明细(2026-10-03 口径)
        'updated_at': dt.datetime.now().isoformat(timespec='seconds'),
    }
    path = os.path.join(OUT_DIR, fname)
    tmp = path + '.tmp'
    json.dump(out, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hospital', default='', help='医院模式: SH-xxx 或 医院全称(与 --dept 二选一)')
    ap.add_argument('--slug', default='', help='医院模式: 输出文件名后缀, 如 jitongnan')
    ap.add_argument('--device', default=None, help='adb 序列号(默认 476cf991; --dept-file 可带 device 字段)')
    ap.add_argument('--resume', action='store_true')
    ap.add_argument('--include-all', action='store_true', help='不剪枝(默认剪掉 other/screening/ancillary/selfname)')
    ap.add_argument('--limit', type=int, default=0, help='调试: 最多抓 N 个单元')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--detail', default='', help='明细层: 分号分隔的科室 raw 名(1-2 个), 抓医生主页')
    ap.add_argument('--only-detail', action='store_true')
    ap.add_argument('--shard', default='', help='i/n 多设备分片')
    ap.add_argument('--max-units', type=int, default=0, help='S3: 态势层每院单元上限(0=不限)')
    ap.add_argument('--dept-priority', default='', help='S3: 科室优先 regex(如 眼科|眼), 匹配单元先抓')
    ap.add_argument('--deadline', default='', help='PC 时钟硬停时刻(09:00 或 ISO); 到点不再开新单元, 收尾 flush 后退出 0')
    ap.add_argument('--spotcheck', default='',
                    help='抽查: "科室raw|医生节点文本[|时段行原文]"; 独立重走+独立重解析, stdout 末行输出记录 JSON 后退出')
    ap.add_argument('--dept', action='append', default=[], metavar='"医院,tab,dept"',
                    help='S8 dept 模式(可重复): 三段, 医院支持中文名/SH-xxx id, tab/dept 支持 raw 名; 可传多套, 逐套隔离')
    ap.add_argument('--dept-file', default='',
                    help='S8: UTF-8 JSON dept 列表(--dept 的免 CJK 损坏通道; 支持 device/deadline 字段)')
    ap.add_argument('--max-consec-errors', type=int, default=UNIT_ERROR_BREAK,
                    help='S8: 单元级连续 error 熔断阈值(默认 3; 连续 error 单元 >= 阈值 => 中止, 退出码 4)')
    a = ap.parse_args()

    if a.max_consec_errors < 1:
        a.max_consec_errors = 1
    # device 优先级: CLI --device > --dept-file 的 device 字段 > 默认 476cf991 (在各分支内解析)

    # ---------------- 零模型守卫: 采集路径不得 import app/llm_client.py, 不得访问 AGENTS_URL ----------------
    for _ev in ('AGENTS_URL', 'RUNTIME_SERVER_URL'):
        os.environ.pop(_ev, None)
    _zm_bad = [mm for mm in sys.modules if mm == 'app.llm_client' or mm.startswith('app.')]
    zero_model = not _zm_bad
    print('zero_model=%s' % zero_model, flush=True)
    assert zero_model, 'zero-model guard violated (app.* 进入采集路径): %s' % _zm_bad

    # ---------------- S8: CJK argv 显式守卫(不许静默按乱码跑) ----------------
    # 实测: cmd 代码页会把 CJK 参数损坏成 U+FFFD/阿拉伯文等乱码传入子进程 => 收到 U+FFFD 即报错并提示免损坏通道。
    _str_args = {'hospital': a.hospital, 'slug': a.slug, 'dept-priority': a.dept_priority,
                 'detail': a.detail, 'spotcheck': a.spotcheck, 'deadline': a.deadline,
                 'dept-file': a.dept_file}
    _str_args.update({'dept[%d]' % i: d for i, d in enumerate(a.dept)})
    _bad = {k: v for k, v in _str_args.items() if v and '\ufffd' in v}
    if _bad:
        raise SystemExit('argv 含 U+FFFD 乱码(疑似 cmd 代码页损坏 CJK 参数): %r '
                         '=> 先 chcp 65001 & set PYTHONUTF8=1(或 python -X utf8), CJK 参数改用 --dept-file <UTF-8 json> 免损坏通道'
                         % {k: v[:40] for k, v in _bad.items()})

    # ---------------- S8: dept 模式分支(与医院模式互斥; 复用 Nav 入口/导航, 不另写导航) ----------------
    dept_mode = bool(a.dept or a.dept_file)
    if dept_mode and a.hospital:
        raise SystemExit('--dept/--dept-file 与 --hospital 不兼容(两种模式二选一)')
    if dept_mode:
        os.makedirs(OUT_DIR, exist_ok=True)   # log() 落 fetch.log 需要 OUT_DIR 存在
        specs = []
        for i, d in enumerate(a.dept):
            try:
                specs.append(parse_dept_spec(d, 'cli --dept#%d' % i))
            except ValueError as e:
                raise SystemExit(str(e))
        f_dev = None
        if a.dept_file:
            f_specs, f_dev, f_dl = load_dept_file(a.dept_file)
            specs.extend(f_specs)
            if not a.deadline and f_dl:
                a.deadline = f_dl
        if a.device is None:   # CLI 未给 => 文件 device => 默认
            a.device = f_dev or '476cf991'
        if not specs:
            raise SystemExit('dept 模式: 无有效 spec(用 --dept "医院,tab,dept" 或 --dept-file <json>)')
        # 解析+去重(设备接触前完成配置校验; 乱码/查无此院 => 显式报错, 不许静默跑)
        resolved, seen = [], set()
        for (h, t, d) in specs:
            m = load_manifest(h)   # 查无 => SystemExit(含 CJK 损坏提示)
            k = (m['id'], t, d)
            if k in seen:
                continue
            seen.add(k)
            resolved.append({'spec': (h, t, d), 'm': m, 'hid': m['id'],
                             'hosp': m['institution']['name']})
        # tab/dept 对照 manifest 预检(仅警告不拦: live 页可能与 manifest 不一致, 后续 nav_fail 会显式报)
        for r in resolved:
            h, t, d = r['spec']
            l1 = r['m']['features']['bookable_departments']['value']['l1']
            tinfo = next((x for x in l1 if x['name'] == t), None)
            if tinfo is None:
                log('WARN: tab %r 不在 %s manifest (live 页可能有; 若无 => nav_fail 显式报)' % (t, r['hid']))
            else:
                dinfo = [x.get('raw') for x in tinfo.get('depts', [])]
                if d not in dinfo:
                    log('WARN: dept %r 不在 manifest tab %r 精确列表(%d 个); 将按 live 页模糊匹配' % (d, t, len(dinfo)))
        if a.dry_run:
            print('dept-mode dry-run: %d 套 spec ->' % len(resolved))
            for r in resolved:
                print('  %s | %s | %s | %s' % (r['hid'], r['spec'][1], r['spec'][2], r['hosp']))
            print('  device=%s deadline=%s max_consec_errors=%d out=%s'
                  % (a.device, a.deadline or '-', a.max_consec_errors,
                     os.path.relpath(DEPT_RUNS_DIR, WS)))
            print('  (dry-run: 不碰设备, 不写文件)')
            return
        adb = Adb(a.device)
        r = adb.run('get-state')
        assert 'device' in r, 'device %s not online: %r' % (a.device, r)
        adb.keyevent(224); time.sleep(1.0)
        adb.run('shell', 'wm', 'dismiss-keyguard')
        adb.run('shell', 'ime', 'set', 'com.android.adbkeyboard/.AdbIME')
        adb.run('shell', 'settings', 'put', 'system', 'screen_off_timeout', '3600000')
        adb.run('shell', 'svc', 'power', 'stayon', 'true')
        today = adb.device_date().date()
        rc, run_sum = run_dept_mode(adb, resolved, a, today)
        adb.run('shell', 'svc', 'power', 'stayon', 'false')
        print(json.dumps(run_sum, ensure_ascii=False, indent=1), flush=True)
        sys.exit(rc)
    if not a.hospital:
        raise SystemExit('需要 --hospital(医院模式) 或 --dept/--dept-file(dept 模式)')
    if not a.slug:
        raise SystemExit('医院模式需要 --slug')
    if a.device is None:
        a.device = '476cf991'

    os.makedirs(OUT_DIR, exist_ok=True)
    deadline = None
    if a.deadline:
        if re.match(r'^\d{1,2}:\d{2}$', a.deadline.strip()):
            _now = dt.datetime.now()
            _hh, _mm = a.deadline.strip().split(':')
            deadline = _now.replace(hour=int(_hh), minute=int(_mm), second=0, microsecond=0)
        else:
            deadline = dt.datetime.fromisoformat(a.deadline.strip())
        log('deadline=%s (PC clock)' % deadline.isoformat(timespec='minutes'))
    m = load_manifest(a.hospital)
    hid = m['id']
    hosp = m['institution']['name']
    units, kept, pruned = build_units(m, a.include_all)
    plan_order = [t['name'] for t in m['features']['bookable_departments']['value']['l1']]
    tab_meta = [(t['name'], tab_kind(t['name']), [d.get('raw') for d in t['depts']])
                for t in m['features']['bookable_departments']['value']['l1']]
    log('=== %s (%s) tabs=%d units_total=%d pruned=%d kept=%d include_all=%s ==='
        % (hosp, hid, len(plan_order), len(units), len(pruned), len(kept), a.include_all))
    if a.dry_run:
        for u in kept[:20]:
            print('  ', u['tab'], '|', u['dept'], '|', u['kind'])
        print('  ... (%d kept)' % len(kept))
        return

    shard = None
    if a.shard:
        i, n = a.shard.split('/')
        shard = (int(i), int(n))
        kept = [u for j, u in enumerate(kept) if j % shard[1] == shard[0]]
        log('shard %d/%d -> %d units' % (shard[0], shard[1], len(kept)))

    if a.dept_priority:
        _pat = re.compile(a.dept_priority)
        _pri = [u for u in kept if u.get('dept') and _pat.search(u['dept'])]
        kept = _pri + [u for u in kept if u not in _pri]
        log('dept-priority %r: %d units first (of %d kept)' % (a.dept_priority, len(_pri), len(kept)))
    if a.max_units and len(kept) > a.max_units:
        log('max-units %d: truncate kept %d -> %d' % (a.max_units, len(kept), a.max_units))
        kept = kept[:a.max_units]

    state = load_state()
    hs = state.setdefault(hid, {'units_total': len(units), 'units_pruned': len(pruned),
                                'done': 0, 'cost_ms_total': 0, 'status_counts': {},
                                'results': {}, 'blocked': None})
    results = hs.setdefault('results', {})
    done_set = {(k.split('\x01')[0], k.split('\x01')[1])
                for k, v in results.items() if v and v.get('status') in ('ok', 'no_slot', 'bar_hint')}
    if a.resume:
        log('resume: %d units already done' % len(done_set))

    adb = Adb(a.device)
    r = adb.run('get-state')
    assert 'device' in r, 'device %s not online: %r' % (a.device, r)
    adb.keyevent(224); time.sleep(1.0)
    adb.run('shell', 'wm', 'dismiss-keyguard')
    adb.run('shell', 'ime', 'set', 'com.android.adbkeyboard/.AdbIME')
    adb.run('shell', 'settings', 'put', 'system', 'screen_off_timeout', '3600000')
    adb.run('shell', 'svc', 'power', 'stayon', 'true')
    today = adb.device_date().date()

    nav = Nav(adb, hosp)
    st, s = nav.enter()
    if st == 'blocked':
        hs['blocked'] = 'auth/realname at entry'
        save_state(state)
        write_hospital_json(hid, a.slug, m, tab_meta, results, state, a.device)
        log('HOSPITAL BLOCKED at entry: %s' % hs['blocked'])
        sys.exit(3)
    if st != 'ok':
        log('ENTER FAIL for %s' % hosp)
        sys.exit(2)

    # ---------------- 抽查模式: 独立重走 1 个已采单元, 独立重解析, 只读不点"确定" ----------------
    if a.spotcheck:
        _sp = a.spotcheck.split('|')
        sp_dept = _sp[0].strip()
        sp_doc = _sp[1].strip() if len(_sp) > 1 else ''
        sp_row = _sp[2].strip() if len(_sp) > 2 else ''
        emit = lambda o: print(json.dumps(o, ensure_ascii=False), flush=True)
        dtab = next((t['name'] for t in m['features']['bookable_departments']['value']['l1']
                     if any(x.get('raw') == sp_dept for x in t['depts'])), None)
        if dtab is None:
            emit({'verdict': 'unable', 'reason': 'dept_not_in_manifest', 'dept': sp_dept})
            sys.exit(0)
        first_d = next((x[2][0] for x in tab_meta if x[0] == dtab and x[2]), None)
        ost, s2, _t = nav.open_dept(dtab, sp_dept, plan_order, first_d)
        if ost != 'ok':
            emit({'verdict': 'unable', 'reason': 'open_dept_fail(%s)' % ost})
            sys.exit(0)
        # S3: 用医生卡枚举(卡身姓名节点)定位; 找不到 => unable, **不点「去预约」按钮**(组版风险)
        node = None
        _cards = _find_doc_cards(parse_nodes(s2))
        for _card in _cards:
            if sp_doc and _card['name_node'][0] == sp_doc and node_visible(_card['name_node']):
                node = _card['name_node']
                break
        if node is None and sp_doc:
            _nm = sp_doc.split()[0]
            for _card in _cards:
                _t = _card['name_node'][0]
                if _t.startswith(_nm) and (DOC_TITLE_RE.match(_t) or DOC_NAME_ONLY_RE.match(_t)):
                    if node_visible(_card['name_node']):
                        break
        if node is None:
            adb.back(); time.sleep(1.2)
            emit({'verdict': 'unable', 'reason': 'doctor_node_not_found', 'doctor': sp_doc})
            sys.exit(0)
        # S13: 同 dept 模式 — tap 前先把卡带入安全可视区(裁剪零高卡直接 tap 不跳转); 带不进才回退原节点
        node = _ensure_card_visible(nav.adb, node[0]) or node
        if not _card_in_view(node):
            log('  spotcheck WARN: card %r 未完整入可视区(中心 y 越界/裁剪), 仍按现坐标 tap' % node[0])
        nav.adb.tap((node[2] + node[4]) // 2, (node[3] + node[5]) // 2)   # 只点卡身
        time.sleep(3.0)
        s3 = nav.adb.dump()
        k3 = page_kind(s3)
        if k3 not in ('doc', 'six'):
            # S3: 卡身点击未进入医生主页 => unable; **不点「去预约」按钮**(组版=缺就诊医生, 无法比对)
            if k3 not in ('L2', 'L2b'):
                nav.adb.back(); time.sleep(1.5)
            emit({'verdict': 'unable', 'reason': 'unexpected_page(%s)_no_group_fallback' % k3})
            sys.exit(0)
        if k3 == 'doc':
            # 指纹规则未命中(未渲染完等) => 等 2s 重 dump 一次, 仍 miss => unable(不猜)
            time.sleep(2.0)
            s3 = nav.adb.dump()
            k3 = page_kind(s3)
            if k3 == 'blocked':
                nav.adb.back(); time.sleep(1.5); nav.adb.back(); time.sleep(1.0)
                emit({'verdict': 'unable', 'reason': 'realname/captcha page AVOIDED (no bypass)'})
                sys.exit(0)
            if k3 != 'six':
                nav.adb.back(); time.sleep(1.5)
                emit({'verdict': 'unable', 'reason': 'six_rule_miss(%s)_no_guess' % k3})
                sys.exit(0)
        elif k3 == 'blocked':
            nav.adb.back(); time.sleep(1.5); nav.adb.back(); time.sleep(1.0)
            emit({'verdict': 'unable', 'reason': 'realname/captcha page AVOIDED (no bypass)'})
            sys.exit(0)
        # 独立重解析(新鲜 dump, 不读回任何缓存)
        dev_ts = nav.adb.device_date().isoformat(timespec='seconds')
        raw_rel = six_fields.save_raw_evidence(os.path.join(OUT_DIR, '_last_dump.xml'), OUT_DIR,
                                               hid, sp_dept, node[0], dev_ts + '_spot')
        raw_ws = os.path.join(os.path.relpath(OUT_DIR, WS), six_fields.RAW_DIRNAME,
                          os.path.basename(raw_rel)).replace('\\', '/')   # S13/S19(V2)
        rule = six_fields.match_six_page(s3)
        if rule == 'six_doc_home':
            dh = parse_doc_home(s3, today)
            slot = None
            if sp_row:
                slot = next((x for x in dh['slots'] if x.get('row_text') == sp_row), None)
            row_found = (slot is not None) if sp_row else True
            pd = six_fields.find_page_doctor(s3)
            rec = six_fields.build_six_record(s3, 'six', {'hospital_name': hosp, 'dept_name': sp_dept},
                                              row_text=sp_row or ((slot or {}).get('row_text')),
                                              fee=((slot or {}).get('fee_cny') if slot else None),
                                              fee_raw=((slot or {}).get('fee_raw') if slot else None),
                                              doctor=(pd or (None, ''))[0],
                                              outpatient_type=((slot or {}).get('outpatient_type') if slot else None),
                                              captured_at=dev_ts, raw_evidence=raw_ws,
                                              hospital_id=hid, unit={'tab': dtab, 'dept': sp_dept},
                                              row_loc=((slot or {}).get('row_loc')),
                                              fee_loc=((slot or {}).get('fee_loc')),
                                              type_loc=((slot or {}).get('type_loc')),
                                              extract_file=raw_ws)
        else:  # six_confirm 兜底
            rec = six_fields.build_six_record(s3, 'six', {'hospital_name': hosp, 'dept_name': sp_dept},
                                              row_text=sp_row or None, fee=None, doctor=None,
                                              captured_at=dev_ts, raw_evidence=raw_ws,
                                              hospital_id=hid, unit={'tab': dtab, 'dept': sp_dept},
                                              extract_file=raw_ws)
            row_found = True
        rec['spotcheck_row_found'] = row_found
        nav.adb.back(); time.sleep(1.5)
        emit({'verdict': 'ok_parsed', 'record': rec, 'page_kind': k3})
        sys.exit(0)
        sys.exit(0)

    # C: 一次 dump 枚举全部 live tab 并和计划对账
    rows = parse_nodes(s)
    live_tabs = [t for (t, c, x1, y1, x2, y2) in rows if c == 'View' and x2 <= 395 and t.strip()]
    live_set, plan_set = set(live_tabs), set(plan_order)
    recon = {'live_count': len(live_tabs), 'plan_count': len(plan_set),
             'live_only': sorted(live_set - plan_set), 'plan_only': sorted(plan_set - live_set)}
    hs['live_tab_recon'] = recon
    log('live tabs=%d plan=%d live_only=%s plan_only=%s'
        % (recon['live_count'], recon['plan_count'], recon['live_only'][:5], recon['plan_only'][:5]))

    def persist():
        recount(hs)  # 每次落盘前从终态幂等重算, 防止累计计数陈旧
        save_state(state)
        write_hospital_json(hid, a.slug, m, tab_meta, results, state, a.device)

    fetched = 0
    stop_reason = None
    consec_err = 0   # S8: 单元级连续 error 计数(熔断用)
    # S3: 按 kept 顺序(眼科优先重排+每院上限)迭代单元, 不再按 tab 分组(眼科科室跨 tab, 分组会漏);
    # open_dept 每次切 tab(select_tab 处理滚动/点击), 连续同 tab 单元无额外成本。
    _tab_first = {t['name']: ([d.get('raw') for d in t['depts']] or [None])[0]
                    for t in m['features']['bookable_departments']['value']['l1']}
    for u in kept:
        if a.only_detail:
            break
        if deadline and dt.datetime.now() >= deadline:
            stop_reason = 'deadline'
            log('DEADLINE reached -> stop (no new unit), flush')
            break
        tab = u['tab']
        k = tab + '\x01' + u['dept']
        if (tab, u['dept']) in done_set:
            continue
        if tab in recon['plan_only']:
            results[k] = {'status': 'error', 'error_reason': 'tab_not_found_live',
                          'captured_at': dt.datetime.now().isoformat(timespec='seconds'),
                          'days_14': None, 'earliest_bookable': None,
                          'tab': tab, 'dept': u['dept'], 'ttl_hint_min': TTL_HINT_MIN}
            log('  [%d/%d] %s|%s -> error(tab_not_found_live)' % (hs['done'], len(kept), tab, u['dept']))
            persist()
            consec_err += 1
            if consec_err >= a.max_consec_errors:
                stop_reason = 'unit_circuit_break'
                log('UNIT CIRCUIT BREAK (plan_only): %d consecutive error units -> stop hospital'
                    % consec_err)
                break
            continue
        t0 = time.monotonic()
        navigated = False
        try:
            first_d = _tab_first.get(tab)
            ost, s2, tab_ok = nav.open_dept(tab, u['dept'], plan_order, first_d)
            cost = time.monotonic() - t0
            if ost == 'blocked':
                hs['blocked'] = 'realname/captcha at (%s,%s)' % (tab, u['dept'])
                hs['results'][k] = {'status': 'error', 'error_reason': 'blocked',
                                    'captured_at': dt.datetime.now().isoformat(timespec='seconds')}
                log('BLOCKED at %s|%s -> skip hospital' % (tab, u['dept']))
                persist()
                write_hospital_json(hid, a.slug, m, tab_meta, results, state, a.device)
                sys.exit(3)
            if ost != 'ok':
                res = {'status': 'error', 'error_reason': 'nav_fail(%s)' % ost,
                       'captured_at': dt.datetime.now().isoformat(timespec='seconds'),
                       'days_14': None, 'earliest_bookable': None,
                       'ttl_hint_min': TTL_HINT_MIN}
            else:
                res = parse_l2(s2, u['dept'], today, adb)
                navigated = True
                if not res['title_match'] and res['status'] == 'ok':
                    res['title_match'] = False
            res.update({'tab': tab, 'tab_kind': tab_kind(tab), 'dept': u['dept'],
                        'dept_id': u['dept_id'], 'canonical': u['canonical'],
                        'kind': u['kind'], 'visit_type': u['visit_type'],
                        'hospital_id': hid,
                        'captured_at': dt.datetime.now().isoformat(timespec='seconds'),
                        'cost_ms': int(cost * 1000)})
            results[k] = res
            hs['cost_ms_total'] = hs.get('cost_ms_total', 0) + int(cost * 1000)
            recount(hs)
            log('  [%d/%d] %s|%s -> %s cost=%.1fs earliest=%s bdocs=%s'
                % (hs['done'], len(kept), tab, u['dept'], res['status'], cost,
                   res.get('earliest_bookable'), res.get('bookable_doctor_count')))
            persist()
            fetched += 1
            if a.limit and fetched >= a.limit:
                log('limit reached (%d)' % a.limit)
                break
        except Exception as e:
            res = {'status': 'error', 'error_reason': 'exception:%s' % str(e)[:120],
                   'tab': tab, 'dept': u['dept'], 'captured_at': dt.datetime.now().isoformat(timespec='seconds'),
                   'days_14': None, 'earliest_bookable': None, 'ttl_hint_min': TTL_HINT_MIN}
            results[k] = res
            recount(hs)
            log('  [%d/%d] %s|%s ERROR %s' % (hs['done'], len(kept), tab, u['dept'], str(e)[:120]))
            persist()
        # 回到 L1: 成功进过 L2 就先显式 back 一次, 再校验(避免 recover 盲目连 back)
        # S8: 单元级连续 error 熔断(家级熔断已按要求拆除; 连续 N 个 error 单元 => 停止本医院)
        if (results.get(k) or {}).get('status') == 'error':
            consec_err += 1
        else:
            consec_err = 0
        if consec_err >= a.max_consec_errors:
            stop_reason = 'unit_circuit_break'
            log('UNIT CIRCUIT BREAK: %d consecutive error units (>=%d) -> stop hospital'
                % (consec_err, a.max_consec_errors))
            break
        if navigated:
            adb.back(); time.sleep(1.2)
        if not nav.verify_L1():
            if not nav.recover_L1():
                log('  recover_L1 failed; re-enter hospital')
                st2, _ = nav.enter()
                if st2 != 'ok':
                    log('re-enter fail; stop hospital')
                    break
        if a.limit and fetched >= a.limit:
            break


    # ---------------- 明细层样例(1-2 个有号科室, 证明"需要时能拿到"+记录成本) ----------------
    if deadline and dt.datetime.now() >= deadline:
        stop_reason = 'deadline'
        log('DEADLINE reached -> skip detail section, flush')
    detail_list = [] if stop_reason == 'deadline' else \
        [x.strip() for x in a.detail.split(';') if x.strip()][:4]
    if detail_list:
        samples = {}
        _six_seen = set()
        for r in (hs.get('six_field_records') or []):
            _six_seen.add((r.get('unit', {}).get('dept'), r.get('就诊医生'), r.get('就诊时间')))
        for dn in detail_list:
            dtab = next((t['name'] for t in m['features']['bookable_departments']['value']['l1']
                         if any(x.get('raw') == dn for x in t['depts'])), None)
            if dtab is None:
                log('detail: dept not in plan: %s' % dn)
                continue
            if not nav.verify_L1():
                nav.recover_L1()
            first_d = next((x[2][0] for x in tab_meta if x[0] == dtab and x[2]), None)
            t0 = time.monotonic()
            ost, s2, _ = nav.open_dept(dtab, dn, plan_order, first_d)
            if ost != 'ok':
                log('detail: open fail %s (%s)' % (dn, ost))
                continue
            rows2 = parse_nodes(s2)
            # S3: 医生卡=卡身姓名节点; **点卡身**(个人版, 六字段齐全); **不点**「去预约」按钮(科室组版, 缺就诊医生)
            cards = _find_doc_cards(rows2)
            if not cards:
                log('detail: no doctor card on L2 for %s' % dn)
                continue
            samples[dn] = {'tab': dtab, 'l2_open_cost_s': round(time.monotonic() - t0, 1),
                           'bookable_doctor_cards': sum(1 for c in cards if c['btn'][0] == '去预约'),
                           'docs': [c['name_node'][0] for c in cards[:8]],
                           'six_records_n': 0, 'variants': [], 'doctor_homes': []}
            for di, card in enumerate(cards[:2]):
                nm = card['name_node']
                expected = _expected_doc_name(nm[0])
                t1 = time.monotonic()
                # S13: 同 dept 模式 — 先滚动把卡带入安全可视区再 tap(首 2 张通常在首屏, 多 1 次 dump)
                nm = _ensure_card_visible(nav.adb, nm[0]) or nm
                nav.adb.tap((nm[2] + nm[4]) // 2, (nm[3] + nm[5]) // 2)   # 卡身
                time.sleep(3.0)
                s3 = nav.adb.dump()
                k3 = page_kind(s3)
                if k3 == 'blocked':
                    log('  detail: realname/captcha page AVOIDED (back out)')
                    nav.adb.back(); time.sleep(1.5)
                    nav.adb.back(); time.sleep(1.0)
                    if not nav.verify_L1():
                        nav.recover_L1()
                    break
                if k3 in ('L2', 'L2b'):
                    # 卡身点击未跳转(仍停留在科室页) => **不点**「去预约」(组版风险), 记录后继续
                    log('  detail %s doc#%d (%s): card-body tap did not navigate (stayed %s); '
                        'NOT tapping 去预约 button (group-variant risk, S1 pitfall)' % (dn, di, nm[0], k3))
                    samples[dn]['variants'].append({'doctor_idx': di, 'tapped': nm[0],
                                                    'variant': 'card_tap_no_navigate', 'page': k3})
                    continue
                if k3 not in ('doc', 'six'):
                    log('detail: unexpected page after doctor tap: %s' % k3)
                    nav.adb.back(); time.sleep(1.5)
                    continue
                if k3 == 'doc':
                    # S19(V2): 同 dept 模式 — 渲染时序 miss 改 8x2s 轮询
                    for _w6 in range(8):
                        time.sleep(2.0)
                        s3 = nav.adb.dump()
                        k3 = page_kind(s3)
                        if k3 in ('six', 'blocked'):
                            break
                    if k3 == 'blocked':
                        log('  detail: realname/captcha page AVOIDED (back out)')
                        nav.adb.back(); time.sleep(1.5)
                        nav.adb.back(); time.sleep(1.0)
                        if not nav.verify_L1():
                            nav.recover_L1()
                        break
                    if k3 != 'six':
                        log('detail %s doc#%d: six-page rule MISS (%s) -> no records (no-guess)' % (dn, di, k3))
                        samples[dn]['variants'].append({'doctor_idx': di, 'tapped': nm[0],
                                                        'variant': 'six_rule_miss', 'page': k3})
                        nav.adb.back(); time.sleep(1.5)
                        continue
                # 展开全部排班拿全量时段(否则计数只是可见下限)
                ex = find_expand_node(parse_nodes(s3))   # S8: 「显示全部排班」/「展开全部排班」两串兼容
                if ex and (ex[5] - ex[3]) > 10:
                    nav.adb.tap((ex[2] + ex[4]) // 2, (ex[3] + ex[5]) // 2)
                    time.sleep(2.5)
                    s3 = nav.adb.dump()
                    if page_kind(s3) != 'six':
                        log('detail %s doc#%d: six-page rule MISS after expand -> no records (no-guess)' % (dn, di))
                        nav.adb.back(); time.sleep(1.5)
                        continue
                cost2 = time.monotonic() - t1
                dh = parse_doc_home(s3, today)
                dh['doctor_idx'] = di
                dh['cost_s'] = round(cost2, 1)
                dh['doctor'] = nm[0]
                samples[dn]['doctor_homes'].append(dh)
                pd = six_fields.find_page_doctor(s3)
                if pd is None:
                    samples[dn]['variants'].append({'doctor_idx': di, 'tapped': nm[0],
                                                    'variant': 'group_missing_doctor',
                                                    'note': '医生主页组版(页头=科室名): 缺「就诊医生」, 不产出六字段记录'})
                    log('  detail %s doc#%d (%s): variant=group_missing_doctor -> no six records' % (dn, di, nm[0]))
                    nav.adb.back(); time.sleep(1.8)
                    continue
                page_name, page_raw = pd
                if page_name != expected:
                    samples[dn]['variants'].append({'doctor_idx': di, 'tapped': nm[0],
                                                    'variant': 'doctor_mismatch', 'page_doctor': page_raw})
                    log('  detail %s doc#%d: doctor mismatch tapped=%r page=%r -> no records (no-guess)' % (dn, di, nm[0], page_raw))
                    nav.adb.back(); time.sleep(1.8)
                    continue
                # 六字段记录: 每个可约时段行一条(号源事实=医生主页时段行, 坑1)
                dev_ts = nav.adb.device_date().isoformat(timespec='seconds')
                raw_rel = six_fields.save_raw_evidence(os.path.join(OUT_DIR, '_last_dump.xml'), OUT_DIR,
                                                       hid, dn, page_name, dev_ts)
                raw_ws = os.path.join(os.path.relpath(OUT_DIR, WS), six_fields.RAW_DIRNAME,
                                  os.path.basename(raw_rel)).replace('\\', '/')   # S13/S19(V2)
                ctx = {'hospital_name': hosp, 'dept_name': dn}
                unit = {'tab': dtab, 'dept': dn}
                n_new = 0
                for sl in dh['slots']:
                    if not sl.get('bookable'):
                        continue
                    key = (dn, page_name, sl['row_text'])
                    if key in _six_seen:
                        continue
                    rec = six_fields.build_six_record(s3, 'six', ctx, row_text=sl['row_text'],
                                                      fee=sl.get('fee_cny'), fee_raw=sl.get('fee_raw'),
                                                      doctor=page_name,
                                                      outpatient_type=sl.get('outpatient_type'),
                                                      captured_at=dev_ts, raw_evidence=raw_ws,
                                                      hospital_id=hid, unit=unit, doctor_idx=di,
                                                      row_loc=sl.get('row_loc'), fee_loc=sl.get('fee_loc'),
                                                      type_loc=sl.get('type_loc'), extract_file=raw_ws)
                    if rec.get('status') != 'ok':
                        log('  detail: six record ERROR: %s' % rec.get('error_reason'))
                        continue
                    six_fields.append_ledger(OUT_DIR, rec)
                    hs.setdefault('six_field_records', []).append(rec)
                    _six_seen.add(key)
                    n_new += 1
                samples[dn]['six_records_n'] = n_new
                log('  detail %s doc#%d (%s): rows=%d bookable_slots=%d fees=%s six_new=%d variant=personal cost=%.1fs'
                    % (dn, di, page_name, dh['slot_rows_shown'], dh['bookable_time_slots'],
                       dh['fees_cny'], n_new, cost2))
                nav.adb.back(); time.sleep(1.8)
        if samples:
            hs['detail_samples'] = samples
            persist()
            log('detail samples saved: %s' % {k: v['six_records_n'] for k, v in samples.items()})

    if stop_reason:
        hs['stop_reason'] = stop_reason
    persist()
    sc = hs.get('status_counts', {})
    avg = (hs.get('cost_ms_total', 0) / hs['done']) / 1000 if hs.get('done') else 0
    log('=== DONE %s: fetched_this_run=%d done=%d/%d status=%s avg_cost=%.2fs stop=%s ==='
        % (hid, fetched, hs['done'], len(kept), sc, avg, stop_reason or '-'))
    adb.run('shell', 'svc', 'power', 'stayon', 'false')
    sys.exit(0 if not hs.get('blocked') else 3)

if __name__ == '__main__':
    main()
