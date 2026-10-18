# -*- coding: utf-8 -*-
"""
fetch_slots.py — 随申办·预约挂号 号源统一抓取接口（V2, --condition, 2026-10-04）

V2 重构 (S20):
  * 移除 --hospital(姿态层批跑) 与 --dept(部门模式) 两个 CLI; 老代码备份 =
    slots/fetch_slots_v1_20261004.py (批跑管线 run_batch.py / app live 抓取 app/slots.py /
    restart_s5.py 已指向备份; 批跑口径不变。新接口仅服务「抬号号源」六字段采集。)
  * 新统一参数 --condition(可重复) / --condition-file(CJK 免损坏通道):
      {"hospital": "第十人民医院|SH-084", "tab": ..., "dept": ...,
       "date": "YYYY-MM-DD"|null,
       "slot_type": "expert|normal|specialty"|null,
       "time_range": "HH:MM-HH:MM"|["HH:MM","HH:MM"]|null,
       "doctor": "医生名"|null}
    缺失 key 或 null = 该条件不限制。多个 --condition => 结果合并为一个列表。
  * 语义:
    - date=null => 医生主页「不限 日期」视图一次抓全(不逐日期查);
      date 指定 => 点医生主页日期条格切到该日(屏外 => 先左滑日期条; 超出条范围 => date_not_found 诚实 0 行)。
    - slot_type => 只进对应子 tab(expert=专家门诊 / normal=普通门诊 / specialty=专病门诊);
      不指定 => 照旧逐个子 tab 全走。单列页(无子 tab 行): normal 按默认列表口径(default_list_as_normal),
      expert/specialty 无法确认 => 诚实 0 行(subtab_absent)。
    - doctor => 只点名称匹配的医生卡(部分匹配); 无匹配 => 诚实 0 行(doctor_not_found)。
    - time_range => 时段行后置过滤(时段完全落在区间内, 边界含); 不参与导航(可多条件共享一次采集)。
  * 页面位置状态 (需求 3/4): 每确认到达一个页面即记录 pos={level,hid,hosp,tab,dept,subtab,date,doctor}
    并持久化 slots/out/nav_state.json。每次检索先 dump 判断当前屏实际位置(与记录互证):
    同医院同科室 => 最少回退(留在 L2 / 回 L1 换科室 / 切子 tab); 换医院 => 回 444 列表重搜;
    位置不符/异常 => 整体重置(重新打开随申办 => 444)。
  * 输出: slots/out/condition_runs/<ts>_all.jsonl(合并六字段记录, 带 cond_idxs/conditions 回指)
    + run_<ts>.json(每条件组 summary + nav_reuse 统计)。不写 six_fields.jsonl / dept_runs / state.json。
  * 同 (hospital,tab,dept,date,slot_type,doctor) 但 time_range 不同的条件自动分组: 一次采集,
    写记录时按各条件过滤, 记录 cond_idxs 回指所有满足它的条件。
  * tab=null 不支持(全院姿态遍历 = v1 --hospital 模式); dept=null 且 tab 已给 => 展开 manifest
    该 tab 下全部科室, 逐科室采。

采集纪律(继承 v1, 全部保留): 六字段只从医生主页(点卡身)取; 「显示全部排班/展开全部排班」展开
(收起态不点、不判折叠); 卡集枚举=滚动到底末尾连续 2 次 dump 稳定(懒加载); 每卡 tap 前
_ensure_card_visible(零高裁剪/屏外中心不 tap); 子 tab 切换前滚回 L2 顶部 + 重 dump 取新坐标;
切 tab/渲染等待 8x2s 轮询; 六字段指纹 miss => 不猜, 记 variant。费用字符两套兼容: 医生主页 U+FFE5(￥) / 确认页 U+00A5(¥)。

零模型: 采集路径不 import app/llm_client.py、不访问 AGENTS_URL。纯 ADB + UI XML 文本匹配。
只读: 仅点击 科室/医生/日期条/Back, 绝不点 去预约/提交/支付。
退出码: 0=正常 2=配置/导航失败(全部条件组失败) 3=blocked 4=单元级连续 error 熔断。

接口示例 (CJK 命令行参数需 chcp 65001 + set PYTHONUTF8=1):
  python slots/fetch_slots.py --condition "{\"hospital\":\"第十人民医院\",\"tab\":\"眼科\",\"dept\":\"眼科门诊\"}"
  python slots/fetch_slots.py --condition "{\"hospital\":\"SH-084\",\"tab\":\"眼科\",\"dept\":\"眼科门诊\",\"slot_type\":\"normal\"}"
  python slots/fetch_slots.py --condition-file conds.json
  # conds.json = [{"hospital":"第十人民医院","tab":"眼科","dept":"眼科门诊",
  #                "date":"2026-10-07","doctor":"徐建江","time_range":"13:00-15:00"}, ...]
"""
import argparse
import base64
import datetime as dt
import glob
import json
import os
import re
import sys
import time

# 端上改造：six_fields 为端上包成员；数据根/产物根经 hhpaths（不走 adb/subprocess）。
try:
    from slots import six_fields          # 六字段记录 schema/解析/指纹
except Exception:
    import six_fields

# 观测层(2026-05 状态机改造): 把"我在哪"从**断言**变成**观测**。
# 此前 pos.level/subtab 是"动作返回 ok 就把目标值写进去", 于是 nav_state 与真实页面
# 一旦分叉就再也发现不了(浮窗说在 A 医院、屏幕在 B 医院就是这么来的)。
try:
    from slots import page_state as PS
except Exception:
    try:
        from . import page_state as PS
    except Exception:
        import page_state as PS

# 导航状态机(2026-10-15 第③步): "现在在哪个页 -> 下一步走哪条边" 只由这张转移表 +
# 观测结果决定; 动作是否算完成, 由 act() 轮询到"观测==期望"决定。见 nav_machine.py。
try:
    from slots import nav_machine as NM
except Exception:
    try:
        from . import nav_machine as NM
    except Exception:
        import nav_machine as NM

import devbridge                          # 端上设备原语（AccessibilityService）

try:                                    # ②/⑥: 随申办导航的单一所有者(见 slots/navlock.py)
    from slots import navlock as NAVLOCK
except Exception:                       # 桌面直跑(无包前缀)
    import navlock as NAVLOCK

try:
    from hhpaths import WS, data as _data, out as _out
    MANIFEST_GLOB = _data('hospital_intel', 'manifests', '*.json')
    OUT_DIR = _out('slots', 'out')
except Exception:
    WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../workspace
    MANIFEST_GLOB = os.path.join(WS, 'hospital_intel', 'manifests', '*.json')
    OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')
try:
    os.makedirs(OUT_DIR, exist_ok=True)
except OSError:
    pass
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
# 2026-10-28 真机实测(476cf991/HyperOS, 随申办现版本): 444 页锚点 TextView text='全部医院 (444)'
#   bounds=[45,1048][360,1104]; 括号里的计数=医院总数会随时间变化 => 用正则兜底任意计数。
RE_444 = re.compile('全部医院\\s*\\(\\d+\\)')
# 444(H5) 页搜索框的 hint：空框时该页同样显示「历史搜索」，会被下面的 search 规则误判成搜索页，
# 于是搜索通道把「预约挂号」打进 444 的框里(文本叠加 -> 死循环)。用它做 444 的兜底指纹。
HINT_444 = '搜索医院、医生、疾病'
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
# S8: 单元级连续 error 熔断阈值(连续 error 单元 >= 阈值 => 中止运行, 退出码 4)
# 2026-10-14 调参(MI 5X 0043d38b0504 诊断): 慢机上「读屏空窗」是**可恢复**态(activity 切换瞬间 /
# binder 4s 超时), 过去与真实导航错误共用阈值 3 => 三次空窗就把整轮 probe 打死(rc=4), 号源只剩
# 档位先验/未核实。现在拆成两类分别计数: 真实导航/页面错误阈值 4; 空窗(empty_dump)阈值 6。
# ⑥ 唯一熔断依据的默认阈值(可被 --max-consec-errors / run_conditions(max_consec_errors=)
# 覆盖 -> set_verify_break)。以前还有 RESET_FULL_BUDGET / SEARCH_RESTART_BUDGET /
# EMPTY_DUMP_ERROR_BREAK 三套"够了就停", 现已删除: 恢复动作(reset/重启)本身不设配额,
# 它有没有效由 verify 说话; 连续 VERIFY_BREAK_N 次 verify 失败(含 unknown/empty_dump/
# wrong_page/blocked 各类, 只是分类不同、恢复不同)才停。空窗(empty_dump)也是 verify
# 失败的一种, 不再单独计数。
# 单次 ensure_444 里「最坏重启(launch CLEAR_TOP)」的次数上限。实测 MI5X 23 分钟内 launch() 71 次,
# 每次重启又制造一次空窗 => 自我放大。
# (2026-10-16) 原 SEARCH_RESTART_BUDGET 已删: 重启不设配额, 熔断器是唯一依据
# 2026-10-14 (MI 5X 实测): L2 已到达但子 tab 行没渲染出来 => 旧值 3 轮(6s)判 subtab_absent 太急。
SUBTAB_WAIT_ROUNDS = 5

# ---------------- 屏幕几何（2026-10-14 双机适配）----------------
# 本文件大量 y 常量是按 1080x2392 的 Redmi 标定的；MI 5X(0043d38b0504) 是 1080x1920，同一页面
# 所有 y 等比压缩（实测 raw/SH-084_眼科门诊_L2NOSUB_20261014T140907.xml：子 tab 行 871→192、
# 日期条 702→591、首屏医生卡 1081→1008、屏底裁剪节点 2347→1917）。绝对阈值在短屏上会**整段筛空**：
#   · find_subtab_nodes 的 300<y1<1100 在 1920 屏上筛不到 y=192 的子 tab => 误判 subtab_absent；
#   · _find_doc_cards 的「裁剪名 y1>=2200」在 1920 屏上永不成立 => 屏底卡整批漏计；
#   · _ensure_card_visible 的 clip_top 判据 y1<600 同理失真。
# 统一改成「按 2392 标定的比例 × 当前屏高」，两台机共用同一组比例。
_SCREEN_HW = [None]


def screen_hw():
    """当前屏幕物理尺寸（端上走 devbridge；PC/异常回退 1080x2392）。只取一次并缓存。"""
    if _SCREEN_HW[0] is None:
        w = h = None
        try:
            w, h = devbridge.screen_size()
        except Exception:
            pass
        try:
            _SCREEN_HW[0] = (int(w or 1080), int(h or 2392))
        except Exception:
            _SCREEN_HW[0] = (1080, 2392)
    return _SCREEN_HW[0]


def Y(frac):
    """把「按 2392 高标定」的 y 比例换算成当前屏幕绝对 y（Y(0.439)=1050@2392 / 843@1920）。"""
    return int(round(screen_hw()[1] * frac))


# 屏外裁剪节点的判定高度: 2392 屏把屏外卡 bounds 压成**零高**, 1920 屏(MI 5X)实测压到 **3px**;
# node_visible 用 h>20, 这里用 h<=CLIP_H_MAX 视作裁剪, 两处口径保持一致。
CLIP_H_MAX = 20


def _default_device():
    """默认设备标识: 端上=真实机器标识, 桌面=旧默认(保持 PC 原型行为不变)。"""
    try:
        if devbridge.available():
            return devbridge.device_key()
    except Exception:
        pass
    return (os.environ.get('DEVICE_SERIAL') or '476cf991')
DEPT_RUNS_DIR = os.path.join(OUT_DIR, 'dept_runs')
COND_RUNS_DIR = os.path.join(OUT_DIR, 'condition_runs')
NAV_STATE_FILE = os.path.join(OUT_DIR, 'nav_state.json')
# S20 (2026-10-04): --condition 统一接口 — slot_type 英文 key => 子 tab raw 名
SLOT_TYPE_EN2ZH = {'expert': '专家门诊', 'normal': '普通门诊', 'specialty': '专病门诊'}

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


def _step(text, state='run'):
    """逐步骤提示 → 浮窗(MiniChip) + 详细日志。桌面/无桥时安全降级。

    用于让"AI 代理实时抓取"的每一步(寻找tab / 点击科室 / 进入子tab / 点医生 / 等待渲染)
    在浮窗上可见, 便于手机端直接观察卡在哪一步; 同时落盘到详细日志。
    """
    try:
        devbridge.emit_step_state(text, state)
    except Exception:
        try:
            devbridge.emit_step(text)
        except Exception:
            pass
    try:
        log('» %s' % text)
    except Exception:
        pass

# ---------------- ADB 封装 ----------------
class Adb:
    def __init__(self, dev=None):
        self.dev = dev or _default_device()
        self._n = 0
        w, h = devbridge.screen_size()
        self.screen_w, self.screen_h = (w or 1080, h or 2392)
        self._dump_fast_ok = None

    def get_state(self):
        return 'device' if devbridge.is_ready() else 'offline'

    def run(self, *a, **kw):
        """兼容旧 adb 调用面 -> 端上原语（不 subprocess、不弹控制台）。"""
        a = [str(x) for x in a]
        if not a:
            return ''
        if a[0] == 'get-state':
            return self.get_state()
        if a[0] == 'shell' and len(a) > 1:
            sub = a[1]
            if sub == 'input':
                act = a[2] if len(a) > 2 else ''
                if act == 'tap':
                    devbridge.tap(a[3], a[4])
                elif act == 'swipe':
                    devbridge.swipe(a[3], a[4], a[5], a[6], a[7] if len(a) > 7 else 350)
                elif act == 'keyevent':
                    devbridge.keyevent(a[3])
                return ''
            if sub == 'monkey':
                devbridge.launch()   # force-stop 无端上等价 -> CLEAR_TOP 重启
                return ''
            # am force-stop / settings / svc / wm / ime / pm / dumpsys：no-op（端上由服务承担）
            return ''
        return ''

    def dump(self, tries=3, wait=3):
        """读屏 -> 与 uiautomator 同构 XML（WebView 偶发失败重试）。

        根因修复(2026-10-04): 「窗口为空(随申办不在前台)」= devbridge.dump() 抛的
        DeviceError('读屏为空…') 属于**可恢复态**，不再直接冒泡 —— 返回空/原 XML，
        让上层 ensure_444 有机会走 ensure_foreground()/_relaunch_channel() 把随申办拉前台。
        只有**真正异常**(非空窗口语义)才 raise。
        """
        last = None          # str(空/原 XML) 或 None
        real_err = None      # 非"空窗口"语义的异常
        for i in range(tries):
            try:
                xml = devbridge.dump()
            except Exception as e:
                msg = str(e)
                if '读屏为空' in msg or '未取到随申办窗口' in msg:
                    # 空窗口(未在前台/未授权)：可恢复 => 记为"空"，不 raise
                    log('  dump empty-window try %d/%d (%s)' % (i + 1, tries, msg[:80]))
                    last = last or ''
                    time.sleep(wait)
                    continue
                real_err = e
                log('  dump-retry %d/%d after %ds (real) (%s)' % (i + 1, tries, wait, msg[:80]))
                time.sleep(wait)
                continue
            if xml and xml.find('<hierarchy') >= 0:
                return xml
            # 空/无 hierarchy（随申办未在前台）
            log('  dump empty try %d/%d (no <hierarchy>)' % (i + 1, tries))
            last = xml or (last or '')
            time.sleep(wait)
        if real_err is not None:
            raise RuntimeError('dump failed: %s' % real_err)
        return last if isinstance(last, str) else ''

    def ensure_foreground(self, timeout_s=15):
        """端上：先自动唤起随申办并等其窗口出现，再让上层 dump（旧版假设人已打开）。"""
        xml = ''
        for _ in range(max(1, int(timeout_s * 2))):
            try:
                xml = devbridge.dump() or ''
            except Exception as e:
                # 空窗口/未在前台 => xml 仍视为空，继续唤起
                msg = str(e)
                if '读屏为空' not in msg and '未取到随申办窗口' not in msg:
                    log('  ensure_foreground dump-exc (%s)' % msg[:80])
                    xml = ''
            if '<hierarchy' in (xml or ''):
                return xml          # 已取到窗口（含随申办）→ 交给上层判页
            try:
                devbridge.launch()      # 唤起（CLEAR_TOP 等效重启）
            except Exception as e:
                log('  ensure_foreground launch-exc (%s)' % str(e)[:80])
            time.sleep(1.5)
        return xml

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
        # 端上 ACTION_SET_TEXT，中英文通吃，不依赖 ADBKeyboard 广播
        devbridge.input_text(text)

    def device_date(self):
        return devbridge.device_date()

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

# ---- 原始 XML 锚点助手(2026-10-28 实测): parse_nodes 只收 text 非空节点, 但现版本随申办
# 首页搜索入口(text='' content-desc='搜索框')与搜索页输入框(text='' resource-id='et_search')
# 都是空 text => 必须按 content-desc / resource-id 在原始 XML 里定位。 ----
def _node_bounds(nx):
    bm = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', nx)
    if not bm:
        return None
    return tuple(map(int, bm.groups()))    # (x1,y1,x2,y2)

def find_by_desc(s, desc_sub):
    for nx in re.findall(r'<node [^>]*?/?>', s):
        dm = re.search(r'content-desc="([^"]*)"', nx)
        if dm and desc_sub in dm.group(1):
            b = _node_bounds(nx)
            if b and (b[3] - b[1]) > 20 and (b[2] - b[0]) > 20:
                return b
    return None

def find_by_resource_id(s, rid):
    for nx in re.findall(r'<node [^>]*?/?>', s):
        rm = re.search(r'resource-id="([^"]*)"', nx)
        if rm and rm.group(1).endswith(rid):
            b = _node_bounds(nx)
            if b and (b[3] - b[1]) > 20 and (b[2] - b[0]) > 20:
                return b
    return None

# ---------------- raw-XML 输入框定位（2026-10-09 根因修复）----------------
# 根因: parse_nodes() 只收 text 非空的行, 而搜索框未输入时 **text 为空**, 只有
#   class/resource-id/bounds => parse_nodes 里永远找不到它(日志 EditText=None)。
# 实测(476cf991, 1080x2392): 444 页搜索框 resource-id 形如 van-search-<N>-input
#   (N 会变: 1 / 6 / 66 => 编号不能写死), class=android.widget.EditText,
#   bounds 实测 [129,294][846,342] 与 [132,246][921,294]; 原生搜索页为 :id/et_search。
# => 一律按 raw XML 定位: resource-id 优先, class=EditText 兜底, 取 bounds 中心 tap。
INPUT_RID_KW = ('van-search', 'et_search', '-input', 'searchbox',
                'search-box', 'search-input', 'search_input')
INPUT_CLASS_KW = ('EditText', 'AutoCompleteTextView', 'MultiAutoCompleteTextView')


def _node_info(nx):
    """单个 <node> 标签 -> dict(text/desc/rid/cls + bounds)，取不到的字段留空。"""
    b = _node_bounds(nx)
    if not b:
        return None
    g = lambda pat: (re.search(pat, nx).group(1) if re.search(pat, nx) else '')
    x1, y1, x2, y2 = b
    return {'text': g(r'text="([^"]*)"'), 'desc': g(r'content-desc="([^"]*)"'),
            'rid': g(r'resource-id="([^"]*)"'), 'cls': g(r'class="([^"]*)"'),
            'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2,
            'cx': (x1 + x2) // 2, 'cy': (y1 + y2) // 2, 'w': x2 - x1, 'h': y2 - y1}


def input_candidates(s):
    """raw XML 里所有「像输入框」的可见节点，按优先级排序 -> [(score, node_dict), ...]。

    score: 0=resource-id 含 van-search(H5/Vant 搜索框) 1=rid 以 -input/et_search/searchbox 结尾
           2=rid 含其它 search 关键字 3=class 属 EditText 族 4=content-desc 含「搜索」(图标入口)
    同级里越宽越像搜索框；再按 y 靠上优先(工具栏内的搜索框总在列表之上)。"""
    out = []
    for nx in re.findall(r'<node [^>]*?/?>', s or ''):
        a = _node_info(nx)
        if not a or a['w'] <= 20 or a['h'] <= 20:
            continue                      # 零宽高/被裁掉 => 不可见
        rid, cls, desc = a['rid'], a['cls'], a['desc']
        score = None
        if 'van-search' in rid:
            score = 0
        elif rid.endswith('-input') or rid.endswith('et_search') or rid.endswith('searchbox'):
            score = 1
        elif any(k in rid for k in INPUT_RID_KW):
            score = 2
        elif any(k in cls for k in INPUT_CLASS_KW):
            score = 3
        elif '搜索' in desc:
            score = 4
        if score is None:
            continue
        out.append((score, a))
    out.sort(key=lambda t: (t[0], -t[1]['w'], t[1]['y1']))
    return out


def find_input_node(s, y1max=None, tag=''):
    """按 raw XML 定位输入框(不依赖 text)。返回 node dict 或 None。

    优先级: resource-id 含 van-search -> rid 以 -input/et_search 结尾 -> class=EditText 可见兜底。
    dict 含 rid/cls/text/desc/x1,y1,x2,y2,cx,cy —— 调用方取 (cx,cy) 直接 tap。"""
    for _score, a in input_candidates(s):
        if y1max is not None and a['y1'] > y1max:
            continue
        if tag:
            log('  %s input node(raw-XML): rid=%r cls=%s text=%r bounds=[%d,%d][%d,%d]'
                % (tag, (a['rid'].split('/')[-1] or a['desc'] or '-'),
                   (a['cls'].split('.')[-1] or '-'), (a['text'] or '')[:12],
                   a['x1'], a['y1'], a['x2'], a['y2']))
        return a
    if tag:
        log('  %s input node(raw-XML): NOT FOUND (无 van-search/-input/EditText 可见节点)' % tag)
    return None


def input_node_text(s, node=None):
    """输入框的 text 属性(raw XML)。找不到节点 -> None；找到但未输入 -> ''。"""
    n = node if node is not None else find_input_node(s)
    return (n.get('text') or '').strip() if n else None


# ---------------- 页面分类 ----------------
def texts_of(s):
    return [t for (t, c, *_ ) in parse_nodes(s)]

def page_kind(s):
    if any(k in s for k in BLOCKED_KW):
        return 'blocked'
    if AUTH_T in s:
        return 'auth'
    if T444 in s or T444b in s or RE_444.search(s):
        return '444'
    if HINT_444 in s:
        return '444'          # 框里已有文本/列表被过滤时没有「全部医院(444)」，但 hint 一直在
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
    # 2026-10-28 真机实测(476cf991): 随申办首页指纹 = 搜索 flipper content-desc='搜索框'
    #   ([45,727][1035,845]) + 底部 '扫一扫'; 旧版无此指纹只能落到 'other' 走重启。
    if '搜索框' in s and '扫一扫' in s:
        return 'home'
    return 'other'

# ---------------- 导航 ----------------
# C(2026-10-09 实测 476cf991/HyperOS): 「位置不符」就 HOME+launch 整体重置 => 死循环
# (每轮 2~5 分钟, 最后 "reset 2 次仍到不了 444")。整体重置每次检索最多 1 次, 用满后
# 直接用当前页走搜索通道。run_conditions() 开头清零。
# (2026-10-16) 原 RESET_FULL_BUDGET/_RESET_FULL_USED 已删: 整机 reset 不设配额,
# 由 nav_machine 的 attempts 预算 + 连续 verify 失败熔断共同兜底
_RESET_FULL_USED = [0]   # 仅计数用于日志(不参与停止判断)
# 2026-10-14 诊断(0043d38b0504 / MI 5X): 「最坏重启」本身会制造一次空窗 => 自我放大
# (23 分钟内 launch() 71 次, 其中 11:49 那一分钟 30/30 次拿不到随申办窗口)。
# 因此 _search_channel 的 restart 也有配额, 用满后只在当前页重试, 不再反复 launch。
_SEARCH_RESTART_USED = [0]


def _empty_dump(s):
    """读屏空窗判据: dump 为空 / 只有窗口骨架(0 个 <node>)。

    2026-10-14 诊断(MI 5X): AccDeviceBridge 在 activity 切换瞬间会拿到
    「systemui + 两个 pkg=null,root=null」的快照并返回空 dump; page_kind('')='other'
    于是被当成「未知页面」走整机重启, 一个 <1s 的读屏过渡态被放大成导航失败
    (not_l2(kind=other)) 并触发单元熔断。空窗是**可恢复**的读屏失败, 不是页面状态,
    必须与 kind=other 分开处理、分开熔断计数。
    """
    if not s:
        return True
    s = s.strip()
    if not s:
        return True
    return '<node' not in s


# ---------------- 证据链(2026-05): 让"这一步到底生效没有"事后可复核 ----------------
# 此前只有**事后** dump, 无法证明某次点击真的生效; 现在每次判定都留: 期望 / 实测 / 当时的 dump。
TRACE = [None]


def trace_begin(tag=''):
    try:
        TRACE[0] = PS.Trace(OUT_DIR, run_id=dt.datetime.now().strftime('%Y%m%dT%H%M%S') + (tag or ''))
        log('trace: %s' % TRACE[0].dir)
    except Exception as e:
        TRACE[0] = None
        log('trace: 初始化失败(忽略, 不影响采集) %s' % str(e)[:80])


def obs_of(s_dump):
    # 观测的唯一入口: 六字段判定由本模块注入(page_state 不认识 six_fields, 避免反向依赖)。
    return PS.observe(s_dump, six_match=lambda x: bool(six_fields.match_six_page(x)))


def trace_ev(state, action, expected, obs, ok, dump=None, extra=None):
    t = TRACE[0]
    if not t:
        return
    try:
        t.event(state, action, expected, obs, ok, dump=dump, extra=extra)
    except Exception:
        pass


# ---------------- 状态机唯一动作原语(2026-05 ②) ----------------
# 旧写法是「tap -> time.sleep -> 相信页面已经变」, 三起现场问题(浮窗与屏幕不符 /
# 到了普通门诊页却进不去 / 浮窗提前播报下一家)都出在: 动作与事实之间没有**判定**。
# act() 把三者钉成一个原语:  动作 -> 观测(PageIdentity) -> 判定(期望 vs 实测) -> 证据链。
#   * 判定不通过就继续轮询(容忍 WebView 渲染滞后), 到 timeout 才算失败;
#   * attempts>1 时失败前重做动作(重做时由调用方的 action 自己重新定位节点);
#   * 每次判定(成功/失败)都写 trace: 期望 / 实测 / 当时的 dump。
ACT_TIMEOUT = 12.0
ACT_SETTLE = 1.0


# ---------------- 唯一熔断依据(2026-05 ⑥) ----------------
# 之前同时有 4 套「够了就停」: RESET_FULL_BUDGET(reset 配额) / SEARCH_RESTART_BUDGET
# (重启配额) / ensure_444 的 max_rounds / 连续 error 单元 + 连续空窗单元 双熔断。
# 它们各数各的, 两个方向都出错: 环境只是慢的时候被配额提前挡掉(实测「reset 2 次仍到不了
# 444」之后还在空转), 真坏了的时候又要等好几层各自数满才停。
# 现在只留一条: **连续 N 次 verify 失败**。一次「判定」= 一个动作/一条转移的最终结论
# (不是每次轮询、不是每次 attempt), 任何一次成功立即清零。恢复动作本身不设配额 ——
# reset/back/restart 想用就用, 只要还没熔断; 熔断了就立刻如实收手。
VERIFY_BREAK_N = 3
VERIFY = {'fail': 0, 'tripped': False, 'why': None, 'last': None,
          'n_ok': 0, 'n_fail': 0}


def set_verify_break(n):
    global VERIFY_BREAK_N
    try:
        VERIFY_BREAK_N = max(1, int(n))
    except Exception:
        pass
    return VERIFY_BREAK_N


def verify_reset():
    VERIFY.update({'fail': 0, 'tripped': False, 'why': None})


CHIP_CN = {
    # 状态机边名(nav_machine) -> 浮窗上的人话; 调用方已给中文 what 时原样透传
    'open_dept': '进入科室', 'tap_subtab': '切换门诊类别', 'enter_l1': '进入科室列表',
    'back_l1': '返回上一级', 'back_l2': '返回门诊页', 'search': '搜索医院',
    'reset': '重启会话', 'agree_auth': '同意授权', 'subtab': '切换门诊类别',
    'nudge': '重排页面',
    'nudge': '重排页面',
    'doctor': '进入医生主页', 'tap_doctor': '进入医生主页', 'tab': '切换大类',
    'enter_l2': '进入门诊挂号', 'doctor_landing': '进入医生主页',
}


def _chip_label(what):
    """动作名 -> 浮窗文案(圈5)。未知英文边名不播报, 免得浮窗变成调试日志。"""
    w = str(what or '')
    if not w:
        return ''
    if w in CHIP_CN:
        return CHIP_CN[w]
    return '' if w.isascii() else w


def _chip_ident(o):
    """已验证身份的短摘要(圈5): 只写观测层真正看到的字段。"""
    if o is None:
        return ''
    bits = [x for x in (getattr(o, 'dept', ''), getattr(o, 'subtab_active', ''),
                        getattr(o, 'doctor', '')) if x]
    return ' · '.join(bits[:2]) if bits else (getattr(o, 'level', '') or '')


def verify_judge(ok, what='', why='', obs=None):
    """所有判定的唯一记账点。返回 True 表示已熔断, 调用方必须立刻收手。"""
    if ok:
        VERIFY['fail'] = 0
        VERIFY['n_ok'] += 1
        return False
    VERIFY['fail'] += 1
    VERIFY['n_fail'] += 1
    VERIFY['last'] = {'what': what, 'why': (why or '')[:200],
                      'obs': obs.brief() if obs is not None else '',
                      'streak': VERIFY['fail']}
    if VERIFY['fail'] >= VERIFY_BREAK_N and not VERIFY['tripped']:
        VERIFY['tripped'] = True
        VERIFY['why'] = ('连续 %d 次 verify 失败; 最后一次 %s: %s | 实测 %s'
                         % (VERIFY['fail'], what, (why or '')[:120],
                            VERIFY['last']['obs']))
        log('VERIFY BREAK: %s' % VERIFY['why'])
        _step('熔断 · 连续 %d 次校验失败(最后: %s)'
              % (VERIFY['fail'], (VERIFY['last'].get('what') or '')[:24]), 'fail')
    return VERIFY['tripped']


def act(adb, expect, action=None, state='', what='', timeout=None, settle=None,
        ok_when=None, abort=None, attempts=1, attempt_settle=1.5, chip=True):
    """动作 -> 轮询观测, 直到实测满足 expect 才返回 ok。返回 (ok, obs, s, why)。

    expect   : {'level','hospital','dept','subtab'} 缺项不校验(PS.matches)
    action   : 可调用, 执行副作用(tap/back); attempts>1 时每轮开始前重新调用
    ok_when  : (obs, s) -> (bool, why) 覆盖判定, 用于「卡片集变化」这类相对判据
    abort    : (obs, s) -> str|None 提前退出(如页面已离开 L2 => 'left')
    """
    timeout = ACT_TIMEOUT if timeout is None else float(timeout)
    settle = ACT_SETTLE if settle is None else float(settle)
    attempts = max(1, int(attempts or 1))
    t0 = time.monotonic()
    last_o, last_s, why = None, '', ''
    # 圈5: 浮窗只报「已验证」的事实。动作已发出但屏幕证据未到 => 校验中(琥珀),
    # 判定通过 => 已核实(绿, 附观测到的身份), 判定失败 => 校验失败(红)。
    label = _chip_label(what or state) if chip else ''
    if label:
        _step('校验中 · %s' % label, 'verify')
    for a in range(attempts):
        if action is not None:
            try:
                action()
            except Exception as e:
                log('  act %s: action 异常 %s' % (what or '?', str(e)[:90]))
        deadline = time.monotonic() + timeout
        while True:
            s_dump = adb.dump()
            if s_dump:
                last_s = s_dump
            o = obs_of(s_dump or '')
            last_o = o
            if abort is not None:
                ab = abort(o, s_dump or '')
                if ab:
                    # 页面已经不在目标轨道上(如掉到六字段/登录/风控) => 立刻返回,
                    # 不做第 2、3 次动作: 在错误的页面上重复点击只会越走越深。
                    why = 'abort:%s (%s)' % (ab, o.brief())
                    log('  act %s: 中止(%s)' % (what or '?', ab))
                    trace_ev(state, what, expect, o, False, dump=s_dump,
                             extra={'why': why, 'abort': ab, 'attempt': a + 1,
                                    'cost_ms': int((time.monotonic() - t0) * 1000)})
                    verify_judge(False, what or state, why, o)
                    if label:
                        _step('校验失败 · %s(走错页:%s)' % (label, ab), 'fail')
                    return False, o, last_s, why
            if ok_when is not None:
                ok, why2 = ok_when(o, s_dump or '')
                if why2:
                    why = why2
            else:
                ok, diffs = PS.matches(expect, o)
                why = '; '.join(diffs)
            if ok:
                trace_ev(state, what, expect, o, True, dump=s_dump,
                         extra={'cost_ms': int((time.monotonic() - t0) * 1000),
                                'attempt': a + 1, 'why': why or ''})
                verify_judge(True, what or state, why, o)
                if label:
                    _step('已核实 · %s · %s' % (label, _chip_ident(o)), 'ok')
                return True, o, last_s, why or ''
            if time.monotonic() >= deadline:
                break
            time.sleep(settle)
        if a + 1 < attempts:
            log('  act %s: 第 %d 次动作后仍不符(%s) => 重做动作'
                % (what or '?', a + 1, (why or '')[:120]))
            time.sleep(attempt_settle)
    o = last_o or obs_of('')
    log('  act %s: %d 次动作 / %.1fs 后仍不符: %s (实测 %s)'
        % (what or '?', attempts, timeout * attempts, (why or '')[:150], o.brief()))
    trace_ev(state, what, expect, o, False, dump=last_s,
             extra={'why': why, 'cost_ms': int((time.monotonic() - t0) * 1000)})
    verify_judge(False, what or state, why, o)
    if label:
        _step('校验失败 · %s(%s)' % (label, (why or '超时')[:48]), 'fail')
    return False, o, last_s, why


def _dept_landed_ok(want, got):
    """L2 标题里的科室 vs 我要进的科室 -> (ok, 说明)。

    2026-10-16 真机复盘: 旧判定只看 level, 点错行/落在别家医院的页面上也算"已进入",
    用户看到的就是"浮窗说进了、屏上没进(或进的是别的科室)"。
    规则(与 page_state 的 strict 一致, 但页面没给科室名时不判死):
      * 相等 或 want 是页面名的子串(页面确实显示了目标名) -> ok
      * 页面没给科室名                                  -> ok(留痕: 无法核实)
      * 其余(名字不同 / 页面名只是目标名的一部分=截断外溢) -> 不 ok, 交状态机退层重进
    注: 不给"截断容差"。目标'炎症性肠病专病门诊XX' vs 页面'炎症性肠病专病门诊' 正是
    用户投诉的"浮窗说进了、屏上没进", 放宽一次就会再犯。
    """
    if not got:
        return True, '≈科室名未渲染(无法核实)'
    w, g = str(want).strip(), str(got).strip()
    if w == g or w in g:
        return True, ''
    return False, '科室不符: 要点 %r, 页面是 %r' % (w, g)


def _l2_arrived(o, s, dept_name):
    """open_dept 的落地判定: L2/L2b/医生主页/风控/登录 都算「已离开 L1 列表」;
    标题已现但子tab/按钮未渲染 => 按旧 L2b 变体降级(与旧代码一致, 不放宽)。
    **并且**页面给出的科室必须与目标一致(2026-10-16: 否则点错行也算到达 => 浮窗与屏幕分叉)。"""
    if o.level in ('L2', 'L2b', 'doc', 'six', 'blocked', 'login', 'auth'):
        if o.level in ('L2', 'L2b'):
            ok_d, why_d = _dept_landed_ok(dept_name, getattr(o, 'dept', None))
            if not ok_d:
                return False, why_d
            if why_d:
                log('  L2 落地核对: %s' % why_d)
        return True, o.level
    if YJ in s and '选科室' not in s and '医生主页' not in s and dept_name in s:
        return True, 'L2b(标题已现, 子tab未渲染)'
    if o.level == 'no_window':
        return False, '空窗(读屏失败, 继续等)'
    return False, 'level=%s' % o.level


class Nav:
    def __init__(self, adb, hosp_name):
        self.adb = adb
        self.hosp = hosp_name

    # ---- 观测/判定: 只读、只判定, 怎么改由调用方决定(状态机的"眼睛") ----
    def obs(self, s=None):
        # 当前页的**观测**身份(level/医院/科室/激活子tab/医生), 不写任何断言。
        # six_match 必须由持有 six_fields 的这一侧注入: 否则 page_state 只能把医生主页
        # 判成 'doc'(泛化名), 期望 level='six' 的断言永远不可能成立。
        if s is None:
            s = self.adb.dump()
        return obs_of(s)

    def verify(self, expect, what='', rounds=3, settle=0.8):
        # 期望 vs 实测(最多 rounds 次重读, 容忍 WebView 渲染滞后) -> (ok, obs, dump)。
        # expect 只写关心的字段, 例: {'level':'L2','hospital':'第十人民医院','dept':'骨科门诊',
        # 'subtab':'普通门诊'}。这是**判定**不是重试导航: 判失败时由调用方决定回退到哪一层。
        o = None
        for i in range(max(1, rounds)):
            s = self.adb.dump()
            o = self.obs(s)
            ok, diffs = PS.matches(expect, o)
            if ok:
                return True, o, s
            if i + 1 < max(1, rounds):
                log('  verify %s: 不符 %s => 等 %.1fs 重读(%d/%d)'
                    % (what or '?', '; '.join(diffs), settle, i + 1, rounds))
                time.sleep(settle)
        ok, diffs = PS.matches(expect, o)
        log('  verify %s: 仍不符 %s (实测 %s)' % (what or '?', '; '.join(diffs), o.brief()))
        return False, o, s

    def agree_auth(self, s):
        log('  auth page -> agree')
        # 2026-10-28 真机实测(476cf991/HyperOS, 随申办现版本): 同意按钮 = Button
        #   resource-id='btnSumit' text='同意' bounds=[81,1546][998,1678] (中心 539,1612)。
        #   旧锚 cls='View' 在现版本(Button)命中不到 => 改按文案找(任意类)，找不到再退固定坐标。
        n = find_node(parse_nodes(s), '同意')
        if n and node_visible(n):
            self.adb.tap((n[2] + n[4]) // 2, (n[3] + n[5]) // 2)
        else:
            # 文案拿不到(按钮 text 偶发为空) => 退 raw-XML 按 resource-id 找, 再退固定坐标
            b = find_by_resource_id(s, 'btnSumit')
            if b:
                log('  auth agree: text 未命中 -> raw-XML rid=btnSumit [%d,%d][%d,%d]' % b)
                self.adb.tap((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
            else:
                self.adb.tap(540, 1610)
        time.sleep(3.5)

    # ---- 搜索通道锚点(2026-10-28 476cf991 真机 uiautomator dump 实测, 1080x2392) ----
    # 首页搜索入口: ViewFlipper content-desc='搜索框' [45,730][901,843] (实测点 473,786 进搜索页;
    #               顶部另有一个 desc='搜索框' [146,325][1035,415], 取离验证点近者)
    # 搜索页输入框: EditText resource-id='et_search' [137,153][788,244] (中心 462,198, text 空)
    # 输入后建议行: TextView text='预约挂号' [45,841][1035,983] (中心 540,912)
    # 结果页指纹 : RecyclerView resource-id='lv_search_result' + '应用服务'; 首条结果内层
    #              textView_channelName='预约挂号' [34,458][1035,510] (中心 534,484)
    # 同意/授权页: TextView '需要如下个人信息' + Button '同意'(btnSumit) [81,1546][998,1678]
    # 444 页     : toolbar '预约挂号' + EditText 'van-search-1-input' + '全部医院 (444)'
    def ensure_444(self, max_rounds=6):
        """导航自愈(端上版; 2026-10-28 按 476cf991 真机路径重写)。
        真实路径: 随申办首页 -> 搜索框输入「预约挂号」-> 搜索结果点击进入 -> 同意/授权页点「同意」
                  -> 全部医院列表页(444)。
        分层: (0) ensure_foreground: dump 空则 launch()+轮询(保留);
              (1) page_kind 判页: 已在 444 直接用 / auth 同意 / blocked 诚实返回;
              (2) 深层页(L1/L2/L2b/doc/six): 逐层 back() + 重新 dump 判定;
              (3) search/native_search/home: 走 _search_channel(搜索通道, 输入用 ACTION_SET_TEXT);
              (4) 最坏重启: GLOBAL_ACTION_HOME + launch()(AccDeviceBridge 带 CLEAR_TOP; 端上
                  am force-stop 在 Adb.run() 是 no-op) 后重走搜索通道;
                  最多 max_rounds 轮后诚实返回 'fail'(nav_fail)。
        """
        _fg_armed = True
        restart = False            # 搜索通道是否升级为「最坏重启」
        last_s = ''
        for r in range(max_rounds):
            if VERIFY['tripped']:
                # ⑥: 唯一熔断依据 —— 不再靠 max_rounds 数满才停
                log('  ensure_444: 已熔断(%s) => 如实收手' % VERIFY['why'])
                return 'fail', last_s
            if _fg_armed:
                _fg_armed = False
                # 根因修复(2026-10-04): 端上必须先自己拉前台; 首次 dump 前兜一次 ensure_foreground
                try:
                    if devbridge.is_ready():
                        self.adb.ensure_foreground()
                except Exception as _e:
                    log('  ensure_444 pre-dump ensure_foreground exc (%s)' % str(_e)[:80])
            s = self.adb.dump()
            if s:
                last_s = s
            k = page_kind(s)
            log('  ensure_444 round %d/%d kind=%s' % (r + 1, max_rounds, k))
            if k == 'blocked':
                return 'blocked', s
            if k == 'auth':
                self.agree_auth(s)
                continue
            if k == '444':
                return 'ok', s
            if k in ('L1', 'L2', 'L2b', 'doc', 'six'):  # S3: 医生主页指纹命中也要 back 出
                # 深层页: 逐层 back + 重新 dump 判定(端上 GLOBAL_ACTION_BACK 等效旧 adb back)
                self.adb.back(); time.sleep(2.0)
                s2 = self.adb.dump()
                k2 = page_kind(s2)
                if k2 == '444':
                    return 'ok', s2
                if k2 == k:
                    # back 打不动(还停在同类页) => 升级搜索通道(本轮失败则下轮带重启)
                    log('  ensure_444: back 无效(kind 仍 %s) => 升级搜索通道' % k)
                    st, s3 = self._search_channel(restart=restart)
                    if st == 'ok':
                        return st, s3
                    if st == 'empty_dump':
                        log('  ensure_444: back 无效后搜索通道读到空窗 => 不升级重启')
                        if s3:
                            last_s = s3
                        continue
                    restart = True
                    if s3:
                        last_s = s3
                continue
            if k in ('search', 'native_search', 'home'):
                # 已在搜索页/首页附近: 直接走「搜索『预约挂号』-> 点结果 -> 同意」
                st, s2 = self._search_channel(restart=restart)
                if st == 'ok':
                    return st, s2
                if st == 'empty_dump':
                    log('  ensure_444: 搜索通道读到空窗 => 本轮不升级重启, 下一轮重判页面')
                    if s2:
                        last_s = s2
                    continue
                restart = True
                if s2:
                    last_s = s2
                continue
            # other(未知页/空窗口): 先 back 一次尝试露出 444, 不行再搜索通道(带重启)
            # ---- A11Y 自愈(2026-10-10): kind=other 且窗口空 = 可观测异常信号(甩后台/通知栏抢占/服务掉线)
            if A11Y_SELFHEAL and k == 'other' and not (s or '').strip():
                rstat, rlast = _a11y_recover(self.adb)
                if rstat == 'ok' and rlast:
                    # Self-heal ok = "Suishenban back in foreground (non-empty window)", NOT "at 444".
                    # _a11y_recover docstring: "outer ensure_444 navigates 444 again"
                    # => continue the loop: re-dump, re-judge page_kind, navigate per existing logic to 444.
                    # Do NOT return 'ok' here (would misreport to upper layer: recovery landing is home/H5, not 444).
                    # Bounded: continue consumes one round; if this is the last round the loop exits with 'fail' (correct).
                    continue
                if rstat == 'blocked':
                    return 'blocked', rlast
                return rstat, rlast
            self.adb.back(); time.sleep(2.0)
            s2 = self.adb.dump()
            if page_kind(s2) == '444':
                return 'ok', s2
            st, s3 = self._search_channel(restart=True)
            if st == 'ok':
                return st, s3
            if st == 'empty_dump':
                log('  ensure_444: kind=other 且读屏空窗 => 不按未知页重启, 下一轮重判')
                if s3:
                    last_s = s3
                continue
            restart = True
            if s3:
                last_s = s3
        log('  ensure_444: %d 轮后仍未到 444 => nav_fail' % max_rounds)
        return 'fail', last_s

    def _search_channel(self, restart=False):
        """搜索通道(真实入口路径): [可选最坏重启] -> 首页搜索入口 -> ACTION_SET_TEXT 输入
        「预约挂号」-> 点建议/结果 -> 同意页同意 -> 444。返回 (ok|fail, s)。"""
        if restart:
            # 端上限制: am force-stop 是 no-op => launch()(NEW_TASK|CLEAR_TOP) 等效重启。
            # 2026-10-09 实测(476cf991/HyperOS): 先按 HOME 会让本应用退到后台, 随后
            # ActivityTaskManager "Abort background activity starts from 10342 (result=102)",
            # launch 再也拉不起随申办 => dump 永远 empty-window 死循环。=> 不再按 HOME。
            # 2026-10-14(MI 5X): 重启本身又制造一次空窗 => 空窗自我放大, 加配额。
            # ⑥: 重启不再有配额; 熔断器是唯一依据(空窗自我放大由熔断器兜底)。
            if VERIFY['tripped']:
                log('  search channel: 已熔断(%s) => 不再 launch, 只在当前页重试' % VERIFY['why'])
                restart = False
            else:
                _SEARCH_RESTART_USED[0] += 1
                log('  search channel: worst-case restart (launch CLEAR_TOP, 不按 HOME) '
                    '[第%d次, 无配额; 停止只由熔断器决定 fail=%d/%d]'
                    % (_SEARCH_RESTART_USED[0], VERIFY['fail'], VERIFY_BREAK_N))
                try:
                    devbridge.launch()
                except Exception as e:
                    log('  restart launch exc (%s)' % str(e)[:80])
                time.sleep(6)
        s = self.adb.dump()
        if _empty_dump(s):
            # 空窗 != 未知页: 原地补 dump(Java 侧已补重试, 这里再给两次机会), 不去 launch
            for _r in range(2):
                time.sleep(1.5)
                s2 = self.adb.dump()
                if not _empty_dump(s2):
                    log('  search channel: 空窗补 dump 命中(第 %d 次)' % (_r + 1))
                    s = s2
                    break
        k = page_kind(s)
        if k == '444':
            return 'ok', s
        if k == 'auth':
            self.agree_auth(s)
            s = self.adb.dump()
            return ('ok' if page_kind(s) == '444' else 'fail'), s
        # 1) 进搜索页: **首页优先按 content-desc="搜索框" 定位**(实测 476cf991: 首页入口 =
        #    ViewFlipper content-desc="搜索框" [45,730][901,843] => 点 473,786)。
        #    et_search 只是**搜索页**输入框锚点(首页上不存在) => 只作搜索页/兜底判据,
        #    旧代码在首页上找 et_search 必然 "找不到搜索输入框(et_search)"。
        for _t in range(3):
            if k in ('search', 'native_search') or self._on_search_page(s):
                break
            ex, ey, how = self._home_search_entry(s)
            if ex is None:
                # 红线: 页面上**没有可确认的搜索入口**时不许按旧坐标盲点。
                # 2026-10-16 真机复盘(fetch.log + trace 20261016T103255): 在
                # "普陀区人民医院"的搜索结果页上盲点 (473,786) => 落进该医院卡片行,
                # 用户看到"进了无关医院/科室"。宁可如实 fail, 也不制造未知状态。
                fb = self._home_444_entry(s)
                if fb is not None:
                    log('  search channel: 无搜索入口, 改走首页「预约挂号」入口 (%d,%d) 直达 444'
                        % fb[:2])
                    self.adb.tap(fb[0], fb[1]); time.sleep(3)
                    s2 = self.adb.dump()
                    if not _empty_dump(s2):
                        s, k = s2, page_kind(s2)
                        if k == '444':
                            return 'ok', s
                    continue
                log('  search channel: 当前页(kind=%s)没有可确认的搜索入口 => 不盲点, '
                    '先回首页再找' % k)
                if not self._back_to_home():
                    # 回退途中可能正好落在 444（医院列表）—— 那就是目标页，直接用它，
                    # 不必再强求"首页指纹"。
                    s3 = self.adb.dump()
                    if page_kind(s3) == '444':
                        log('  search channel: 回退途中已到 444 => ok')
                        return 'ok', s3
                    log('  search channel: 回不到可确认的首页 => fail(不盲点)')
                    return 'fail', s
                s = self.adb.dump()
                k = page_kind(s)
                if k == '444':
                    return 'ok', s
                continue
            log('  search channel: tap home entry by %s (%d,%d) try %d/3'
                % (how, ex, ey, _t + 1))
            self.adb.tap(ex, ey); time.sleep(3)
            s2 = self.adb.dump()
            if not s2:
                # dump 空(读屏为空)不能把已知状态擦掉, 否则下一步必然找不到输入框
                log('  search channel: dump 空 => 沿用上帧, 换锚点重试')
            else:
                s = s2
                k = page_kind(s)
        # 2) 点输入框 + 输入「预约挂号」(devbridge.input_text = ACTION_SET_TEXT, 不依赖 ADBKeyboard)
        if _empty_dump(s):
            # 读屏空窗: 不能报成「搜索页输入框未出现」(那是页面判定), 也不该触发整机重启;
            # 空窗同样是 verify 失败(观测不到=无法核实), 计入同一个熔断器, 不再单独计数。
            log('  search channel: 读屏空窗(empty_dump) => 不判 kind=other, 不整机重启')
            return 'empty_dump', s
        pt = self._search_input(s)
        if pt is None:
            log('  search channel: 搜索页输入框未出现(et_search/EditText 均无; '
                '首页已按 content-desc="搜索框" 点过) => fail')
            return 'fail', s
        self.adb.tap(pt[0], pt[1]); time.sleep(1.2)
        self.adb.input_cjk(YJ); time.sleep(1.5)
        s = self.adb.dump()
        # 3) 提交: 实测输入后出现建议行「预约挂号」[45,841][1035,983] => 优先点建议; 否则 ENTER(66)
        if 'lv_search_result' not in s:
            n = find_node(parse_nodes(s), YJ, cls='TextView')
            if n and node_visible(n):
                self.adb.tap((n[2] + n[4]) // 2, (n[3] + n[5]) // 2); time.sleep(3)
            else:
                self.adb.keyevent(66); time.sleep(3)
            s = self.adb.dump()
        # 4) 结果页(实测指纹 lv_search_result): 点首条结果「预约挂号」(实测中心 540,484)
        for _w in range(8):
            if 'lv_search_result' in s or (YY in s and YJ in s):
                break
            time.sleep(1.5)
            s = self.adb.dump()
        n = find_node(parse_nodes(s), YJ, cls='TextView')
        if n and node_visible(n):
            self.adb.tap((n[2] + n[4]) // 2, (n[3] + n[5]) // 2)
        elif 'lv_search_result' in s:
            self.adb.tap(540, 484)   # legacy fixed (Redmi; 476cf991 实测首结果中心 540,484 同值)
        # else: 可能已直达同意/444 页, 不再乱点(红线: 只到列表/号源页)
        time.sleep(3.5)
        # 5) 同意/授权页 -> 同意 -> 444
        for _w in range(12):   # H5 授权页加载可达 ~45s(MI 5X 实测), 轮询放宽
            s = self.adb.dump()
            if (AUTH_T in s) or (T444 in s) or (T444b in s) or RE_444.search(s) or ('选科室' in s):
                break
            time.sleep(2)
        for _ in range(2):
            if AUTH_T in s:
                self.agree_auth(s)
                s = self.adb.dump()
            else:
                break
        if page_kind(s) == 'L1':
            # 兼容个别版本结果页直达医院 L1: 左上返回 [21,146][137,222] => 回 444
            self.adb.tap(79, 184); time.sleep(3.5)
            s = self.adb.dump()
        return ('ok' if page_kind(s) == '444' else 'fail'), s

    @staticmethod
    def _on_search_page(s):
        """是否已在搜索页/结果页(et_search / 历史搜索 / lv_search_result)。"""
        return bool(s) and ('et_search' in s or '历史搜索' in s or 'lv_search_result' in s)

    def _home_444_entry(self, s):
        """首页上「预约挂号」服务入口(实测 476cf991: [45,1058][250,1278] 中心 147,1168)。

        这是**第二条合法进 444 的通道**(实测点它直接到 444 医院列表)。当搜索入口
        在当前页不存在时走这里, 而不是按旧坐标盲点 —— 盲点正是 2026-10-16 实测
        "进了无关的普陀区人民医院" 的起点。返回 (x, y, how) 或 None。"""
        rows = parse_nodes(s)
        for want in ('预约挂号', '预约挂'):
            for n in rows:
                if want not in n[0]:
                    continue
                t, cls, x1, y1, x2, y2 = n
                if not node_visible(n):
                    continue
                w, h = x2 - x1, y2 - y1
                # 只认**入口尺寸**(卡片/磁贴): 宽 100~400、高 100~300, 且在首屏内容区。
                # 搜索结果页里的医院卡片行同样含"预约挂号", 但它是整行(宽 >400)
                # => 用尺寸 + 非 EditText 过滤掉, 避免把"结果行"当入口点。
                if not (100 <= w <= 400 and 100 <= h <= 300 and y1 >= 500):
                    continue
                if cls == 'EditText':
                    continue
                return (x1 + x2) // 2, (y1 + y2) // 2, 'entry_444:%s' % want
        return None

    def _home_search_entry(self, s):
        """首页搜索入口坐标。**优先按 content-desc="搜索框" 定位**(实测 476cf991, 1080x2392,
        dump 复核): 首页有 3 个 content-desc='搜索框' 节点(text 为空, parse_nodes 会过滤 => 按 desc 找):
          ViewFlipper [146,325][1035,415] (顶部搜索条) / ViewFlipper [45,730][901,843]
          (中部 flipper, 实测点其中心 473,786 可进搜索页) / TextView [151,761][385,813](内标签)。
        => 取离实测验证点 (473,786) 最近的候选。返回 (x, y, how)。"""
        cands = []
        for nx in re.findall(r'<node [^>]*?/?>', s):
            dm = re.search(r'content-desc="([^"]*)"', nx)
            if dm and '搜索框' in dm.group(1):
                b = _node_bounds(nx)
                if b and (b[3] - b[1]) > 20 and (b[2] - b[0]) > 20:
                    cands.append(b)
        if cands:
            cands.sort(key=lambda b: abs((b[0] + b[2]) // 2 - 473) + abs((b[1] + b[3]) // 2 - 786))
            b = cands[0]
            return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2, 'desc=搜索框')
        n = find_node(parse_nodes(s), '搜索', cls='TextView')
        if n and node_visible(n):
            return ((n[2] + n[4]) // 2, (n[3] + n[5]) // 2, 'text=搜索')
        # 没有可确认的锚点 => 返回 None(调用方**不许**按坐标盲点)。
        # 旧版在这里返回 (473,786): 那是首页 flipper [45,730][901,843] 的中心, 但在
        # 医院搜索结果页上同一坐标落在医院卡片行里(实测 2026-10-16, 普陀区人民医院
        # 卡片 [45,626][1035,890]) => "重启找回搜索框"变成了"走进另一家医院"。
        return (None, None, 'no-anchor')

    def _back_to_home(self, tries=3):
        """回到**可确认的**随申办首页(首页指纹: content-desc="搜索框" 入口节点)。

        只有确认在首页, "点搜索框"这个动作才有确定语义; 回不去就如实返回 False,
        由上层决定 fail —— 绝不靠旧坐标猜。"""
        for _ in range(tries):
            self.adb.back(); time.sleep(2.5)
            s = self.adb.dump()
            if page_kind(s) == 'home' or self._home_search_entry(s)[2] == 'desc=搜索框':
                return True
        try:
            devbridge.launch()
        except Exception as e:
            log('  back_to_home launch exc (%s)' % str(e)[:80])
        time.sleep(6)
        s = self.adb.dump()
        return page_kind(s) == 'home' or self._home_search_entry(s)[2] == 'desc=搜索框'

    def _search_input(self, s):
        """搜索页输入框中心 —— **raw-XML 定位**(该框 text 为空, parse_nodes 会过滤掉)。

        优先级(find_input_node): resource-id 含 van-search -> rid 以 -input/et_search 结尾
        -> class=EditText 且 bounds 可见。实测(476cf991): 原生页 :id/et_search
        bounds=[137,153][788,244]; H5 版 van-search-<N>-input(N 会变 1/6/66)。"""
        n = find_input_node(s, tag='search-page')
        if n is None:
            return None
        if HINT_444 in (s or ''):
            # 这是 444(H5) 页自己的框，不是搜索页框：打进「预约挂号」会叠加成脏文本
            log('  search-page 输入框实为 444(H5) 框(hint=%s) => 不按搜索页输入框使用' % HINT_444)
            return None
        return (n['cx'], n['cy'])

    def _relaunch_channel(self):
        """兼容旧调用(reset_full): 等价于「最坏重启 + 搜索通道」。"""
        return self._search_channel(restart=True)

    def reset_full(self, tries=1):
        """S20: 整体重置(位置不符/异常路径) = 从打开随申办开始: HOME + force-stop + 重启动 => 444。
        返回 (ok|fail, s)。

        C(2026-10-09 实测 476cf991): resolve_start 判「位置不符」就整体重置 => HOME+launch 死循环
        (每轮 2~5 分钟, 最终 "reset 2 次仍到不了 444")。=> 每次检索**最多 1 次**整体重置,
        用满配额后 reset_full 退化为「直接用当前页走搜索通道」(ensure_444), 不再 HOME+launch。"""
        # ⑥: 不再用「reset 配额」当熔断依据 —— 恢复动作允许重复用, 只看连续 verify 失败。
        # (配额计数保留为统计, 不再决定是否放弃。)
        if VERIFY['tripped']:
            log('  reset_full: 已熔断(%s) => 不再 HOME+launch' % VERIFY['why'])
            return 'fail', ''
        _RESET_FULL_USED[0] += 1
        for _t in range(max(1, int(tries or 1))):
            st, s = self._relaunch_channel()
            if st == 'ok':
                return 'ok', s
            log('  reset_full retry %d' % (_t + 1))
            time.sleep(1.0)
        return 'fail', s

    def enter(self):
        """进入本院 L1(选科室)页。返回 (ok|blocked|fail, s)"""
        st, s = self.ensure_444()
        if st != 'ok':
            return st, s
        if page_kind(s) != '444':
            s = self.adb.dump()
        # 2026-05: 事实成立后才播报(旧版在 ensure_444 之前播, 浮窗比屏幕超前一级)
        _step('医院列表(444)就绪: 搜索 %s' % self.hosp, 'ok')
        # 444 页内搜索框: **raw-XML 定位**(该框 text 为空, 只有 resource-id/class/bounds;
        # parse_nodes 只收 text 非空行 => 旧代码在这里永远 EditText=None)。
        node = find_input_node(s, tag='444')
        if node is None:
            log('  444 input: raw-XML 未找到输入框 -> 探路坐标 (%d,%d)' % self.anchor)
        cx, cy = (node['cx'], node['cy']) if node else self.anchor
        # S3 坑8(2026-10-03 设备实测): 444 页搜索框不在 dump(WebView) -> tap 进独立频道搜索页
        # (指纹 预约挂号+历史搜索); 搜索页点 EditText(聚焦后入 dump)聚焦, 单发输入(双发=文本翻倍),
        # 校验不翻倍/不丢失, 再点「搜索」Button 提交。
        self.adb.tap(cx, cy); time.sleep(1.5)
        s = self.adb.dump()
        if '历史搜索' not in s and 'et_search' not in s:
            self.adb.tap(cx, cy); time.sleep(1.5)
            s = self.adb.dump()
        in1 = find_input_node(s, tag='444-after-tap')   # 聚焦后输入框才入 dump(raw XML 找, 不看 text)
        ix, iy = (in1['cx'], in1['cy']) if in1 else (480, 318)

        def _ed_text():
            """输入框节点(raw XML)的 text 属性；未找到节点=None(未聚焦/未渲染)。
            用节点文本校验输入, 不用全页 'hosp in s'(历史搜索 chips 会假阳性)。"""
            return input_node_text(s)

        self.adb.tap(ix, iy); time.sleep(1.0)
        self.adb.input_cjk(self.hosp); time.sleep(1.8)   # 单发(坑8: 双发 ADB_INPUT_B64 会文本翻倍)
        s = self.adb.dump()
        et = _ed_text()
        if et != self.hosp:
            log('  search input verify fail (EditText=%r) -> refocus + single re-input' % (et,))
            self.adb.tap(ix, iy); time.sleep(1.0)
            self.adb.input_cjk(self.hosp); time.sleep(2.0)
            s = self.adb.dump()
            et = _ed_text()
            if et != self.hosp:
                # 2026-10-07: IME process may have been killed mid-run (MIUI power saving);
                # force re-bind (toggle) and try a third input -- self-heal without a full unit retry
                log('  input verify fail again -> IME re-bind + third input attempt')
                if _ensure_ime_bound(self.adb, quiet=True):
                    self.adb.tap(ix, iy); time.sleep(1.0)
                    self.adb.input_cjk(self.hosp); time.sleep(2.0)
                    s = self.adb.dump()
                    et = _ed_text()
        if et != self.hosp:
            log('  444 输入校验仍失败: 输入框 text=%r 期望=%r -> 本单元 fail(不再盲点)' % (et, self.hosp))
            return 'fail', s
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
            if not (n and node_visible(n)):
                # 2026-10-07: on some devices the H5 result row is class=View (not TextView);
                # class-agnostic fallback (EditText carries the same text but at y1<500 => excluded)
                n = find_node(parse_nodes(s), self.hosp, x1min=380, y1min=500)
            if n and node_visible(n):
                exact = n; break
            if NODATA1 in s or NODATA2 in s:
                break
            if LOADING in s:
                time.sleep(2)   # 2026-10-07: 7->2s(转圈页长等待是“一个页面停留一分钟以上”的主因; 更频繁轮询)
            else:
                time.sleep(1.5)
        if not exact:
            return 'fail', s
        # 点医院卡片 -> 到 L1: **一次点击不生效是常态**(WebView 首屏未就绪/点空)。
        # 旧版只点一次 + 等 10s, 到不了就 ENTER FAIL -> 走 reset -> 恢复通道在结果页上
        # 盲点 -> 走进别的医院(2026-10-16 真机复盘)。现在: 仍在"搜索结果页"就重新定位
        # 卡片再点, 最多 3 次; 每次都留证据。
        for _tap in range(3):
            self.adb.tap((exact[2] + exact[4]) // 2, (exact[3] + exact[5]) // 2)
            s = ''
            arrived = False
            for _w in range(5):   # 2026-10-07: 固定 5s 等待 -> 轮询 L1 就绪(2s 间隔, 至多 10s): 加载完即取, 慢也不误 fail
                time.sleep(2)
                s = self.adb.dump()
                if page_kind(s) == 'L1' and self.hosp in s:
                    arrived = True
                    break
            if arrived:
                if _tap:
                    log('  医院卡片第 %d 次点击后到达 L1 %s' % (_tap + 1, self.hosp))
                trace_ev('L1', 'enter:%s' % self.hosp, {'level': 'L1', 'hospital': self.hosp},
                         obs_of(s), True, dump=s)
                return 'ok', s
            k = page_kind(s)
            # 还停在"输入框里是院名 + 相关医院列表"的搜索结果页 => 刚才那一点没生效
            still_result = (input_node_text(s) == self.hosp) or ('相关医院' in s)
            if k == 'L1' or not still_result:
                break
            n2 = find_node(parse_nodes(s), self.hosp, cls='TextView', x1min=380, y1min=500)
            if not (n2 and node_visible(n2)):
                n2 = find_node(parse_nodes(s), self.hosp, x1min=380, y1min=500)
            if not (n2 and node_visible(n2)):
                break
            log('  点了「%s」卡片但 10s 内没到 L1(kind=%s) => 重新定位卡片再点(第 %d 次)'
                % (self.hosp, k, _tap + 2))
            exact = n2
        trace_ev('L1', 'enter:%s' % self.hosp, {'level': 'L1', 'hospital': self.hosp},
                 obs_of(s), False, dump=s)
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
        """切到左列指定 tab。返回 (ok|notfound, s)。
        点击后**轮询等右栏出现 first_dept 行**(WebView 切 tab 渲染竞态,
        实测 2.2s 固定等待不够, 有时 >30s 或点击未注册 → 补点)。"""
        _step('寻找tab: %s' % tab_name, 'verify')
        s = self.adb.dump()
        for i in range(12):
            rows = parse_nodes(s)
            node = find_node(rows, tab_name, cls='View', x2max=395)
            if node and node_visible(node):
                # 底部/顶部被裁切的节点: 中心点可能在屏外(>2392) → 点击不注册。
                # 先小幅滚动左列把节点完整带入可视区, 再点(实测 中医科 tab y=2327 被裁切)。
                for _n in range(2):
                    if node[5] > Y(0.974):          # 2026-10-14: 2330 是 2392 屏的内容区底
                        _b = Y(0.711)
                        self.adb.swipe(150, _b, 150, max(Y(0.376), _b - (node[5] - Y(0.974)) - 30), 300)
                        time.sleep(1.3)
                        s = self.adb.dump()
                        node = find_node(parse_nodes(s), tab_name, cls='View', x2max=395)
                        if not (node and node_visible(node)):
                            break
                    elif node[3] < Y(0.113):        # 2026-10-14: 270 是 2392 屏的内容区顶
                        _t = Y(0.627)
                        self.adb.swipe(150, _t, 150, min(Y(0.92), _t + (Y(0.113) - node[3]) + 30), 300)
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
                _step('点击tab: %s' % tab_name, 'verify')
                self.adb.tap((node[2] + node[4]) // 2, ty)
                time.sleep(1.5)
                for _w in range(6):
                    s = self.adb.dump()
                    # ①(2026-10-15): 点完 tab 必须**观测到左列该项 selected=true**。语料实测
                    # L1 选中项带 selected="true"; 取不到信号(None)不判死, 但一旦观测到
                    # 「选中的是别的科室大类」, 再点右列就会进错科室 => 如实交回状态机重规划。
                    _ot = obs_of(s)
                    if _ot.tab and _ot.tab != tab_name:
                        log('  tab 未激活: 期望 %r, 页面选中 %r => 不点右列, 交回状态机'
                            % (tab_name, _ot.tab))
                        return 'tab_wrong', s
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
                self.adb.swipe(150, Y(0.837), 150, Y(0.376), 350)   # 2026-10-14: 屏高比例
            else:
                self.adb.swipe(150, Y(0.376), 150, Y(0.837), 350)   # 2026-10-14: 屏高比例
            time.sleep(1.6)
            s = self.adb.dump()
        _step('未找到tab: %s' % tab_name, 'fail')
        log('  select_tab NOTFOUND tab=%r (12 轮滚动后仍无匹配 View 节点; 常见: tab 文本截断/类名非View/x越界/WebView未渲染)' % tab_name)
        return 'notfound', s

    def find_dept_row(self, s, dept_name):
        rows = parse_nodes(s)
        best = None
        for (t, c, x1, y1, x2, y2) in rows:
            # 2026-10-07: H5 dept rows are class=View on some devices (MI 5X/Android 8.1),
            # TextView on the Redmi; accept both (x1>=385 excludes the left-column tab Views).
            if c not in ('TextView', 'View') or x1 < 385 or x2 > 1081:
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
        # 身份闸门(2026-10-16 真机复盘, 用户投诉"进了无关的普陀区人民医院口腔科"):
        # 恢复通道曾在**医院搜索结果页**上按旧坐标 (473,786) 盲点, 落点正好是
        # "普陀区人民医院"卡片行 [45,626][1035,890] => 进了用户没要的医院; 随后
        # open_dept 在**别人的 L1** 上继续找科室并点击, 屏幕与浮窗就此分叉。
        # 规则: 点科室前必须能证明"这是本院的选科室页"(page_kind==L1 且页头有院名)。
        if not self.verify_L1(s):
            s = self.adb.dump()          # WebView 可能正在渲染 => 给一次重读机会
            if not self.verify_L1(s):
                o = obs_of(s)
                log('  open_dept 拒点: 当前不是「%s」的选科室页(kind=%s, 页头=%r) '
                    '=> 不点科室, 交回状态机 reset'
                    % (self.hosp, page_kind(s), (o.hospital or o.title)))
                trace_ev('L1_identity', 'guard:%s' % self.hosp,
                         {'level': 'L1', 'hospital': self.hosp}, o, False, dump=s)
                return 'fail', s, 'wrong_hosp'
        for i in range(7):
            row = self.find_dept_row(s, dept_name)
            if row and node_visible(row):
                _step('点击科室: %s' % dept_name, 'verify')
                self.adb.tap((row[2] + row[4]) // 2, (row[3] + row[5]) // 2)
                _step('等待进入门诊挂号(L2)…', 'verify')
                # ②(2026-05): 点击后**必须观测到页面真的成为 L2** 才算进入
                # (旧版是 5 次 page_kind 轮询 + 「标题已现但子tab未渲染」直接接受)。
                ok_a, o_a, s, why_a = act(
                    self.adb, {'level': ('L2', 'L2b', 'doc', 'six', 'blocked', 'login', 'auth')},
                    state='enter_l2', what='科室:%s' % dept_name,
                    timeout=12.0, settle=1.2,
                    ok_when=lambda o_, s_: _l2_arrived(o_, s_, dept_name))
                if ok_a and (why_a == 'blocked' or o_a.level == 'blocked'):
                    return 'blocked', s, 'ok'
                if ok_a and (why_a in ('login', 'auth') or o_a.level in ('login', 'auth')):
                    self.agree_auth(s); time.sleep(1.0)
                    s = self.adb.dump()
                    k = page_kind(s)
                    return ('ok', s, 'ok') if k in ('L2', 'L2b', 'doc', 'six') else ('fail', s, 'ok')
                if ok_a:
                    # ⑤: 播报的是**实测落地页**(标题里的科室), 不是"我想要的科室" ——
                    # 浮窗与屏幕不符的另一个来源就是把意图当结果播报。
                    # 浮窗上必须能核对"哪家医院的哪个科室": 只报科室名时, 走错医院
                    # 用户看不出来(2026-10-16 投诉: 进了无关的普陀区人民医院口腔科)。
                    _step('已进入门诊挂号: %s · %s' % (self.hosp, o_a.dept or dept_name), 'ok')
                    return 'ok', s, 'ok'
                log('  open_dept FAIL dept=%r: 点击后未落到 L2 (%s; 实测 %s)'
                    % (dept_name, (why_a or '')[:120], o_a.brief()))
                return 'fail', s, 'ok'
            # 右栏滚动找行
            self.adb.swipe(700, Y(0.837), 700, Y(0.418), 350)   # 2026-10-14: 2000 在 1920 屏外(会被钳成短距)
            time.sleep(1.4)
            s = self.adb.dump()
        _step('未找到科室: %s' % dept_name, 'fail')
        log('  open_dept FAIL dept=%r (7 轮滚动后仍未进入 L2; kind=%s)' % (dept_name, page_kind(s)))
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
        # 复用 six_fields.fee_tier_from(口径唯一真源), 避免内联分档边界漂移
        fee_tier = six_fields.fee_tier_from(f)
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
    # 复用 six_fields.fee_tier_from(与 six_fields.fee_tier_from 同步, 口径唯一真源)
    return six_fields.fee_tier_from(cny)


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
    # 2026-10-14 (MI 5X 取证): 屏外裁剪节点在 2392 屏被压成**零高**，在 1920 屏实测是 **3px 高**
    # (raw/SH-084_..._L2NOSUB: 6 个姓名 bounds=[246,1917][*，1920])。旧判据 `<= 0` 在短屏上
    # 认不出裁剪 => 裁剪按钮退回"可见配对"分支，其 y 窗口把同一个姓名节点配给多张卡
    # (doctors 里 '彭清' 重复 6 次)。判据改为高度 <= CLIP_H_MAX，并记录已配对的姓名下标。
    used = set()
    for b in btns:
        if b[5] - b[3] <= CLIP_H_MAX:   # 零高/近零高 = 屏外裁剪
            clipped_btns.append(b)
            continue
        name = None
        _ni = None
        for _i, (t, c, x1, y1, x2, y2) in enumerate(rows2):
            if _i in used:
                continue
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
            _ni = _i
            break
        if name:
            cards.append({'name_node': name, 'btn': b})
            used.add(_ni)
    if clipped_btns:
        cnames = []
        for (t, c, x1, y1, x2, y2) in rows2:
            # S9: 与上同: 裁剪姓名同样放宽(跨整行 [244,*][1035,*] 的裁剪姓名不能漏)
            if x1 < 240 or x1 > 500 or x2 > 1081:
                continue
            if not _is_name_node(t):
                continue
            if y2 - y1 <= CLIP_H_MAX and y1 >= Y(0.90):   # 姓名同样被裁剪(零/近零高, 屏幕底)
                # 2026-10-14: 旧阈值 2200 只在 2392 屏成立; MI 5X(1920 屏)屏底裁剪节点实测 y=1917
                # => 整批屏底卡被漏计(枚举少卡)。改为屏高比例。
                cnames.append((t, c, x1, y1, x2, y2))
        for i, b in enumerate(clipped_btns):
            if i < len(cnames):
                cards.append({'name_node': cnames[i], 'btn': b})
    cards.sort(key=lambda c: (0, c['name_node'][3])
               if (c['name_node'][5] - c['name_node'][3]) > CLIP_H_MAX else (1, c['name_node'][3]))
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


# 2026-10-14 (MI 5X six=0 真因, 现场证据 SH-084_眼科门诊_MISMATCH1_徐建江_*.xml):
# 旧 WebView(Android 8.1)把医生主页页头的「姓名+职称」渲染成 <img>, 例如
#   <node text="42500853400_C035" class="android.widget.Image" bounds="[45,255][204,411]"/>
# 整页 XML 里医生姓名出现 0 次 => find_page_doctor 只能退到页头唯一 CJK 节点
# 「第十人民医院    眼科门诊」, 于是 21/21 全部 doctor_mismatch, six=0(被误记成"该条件无号")。
# 页面本身确实是个人版(标题「医生主页」+ 医生简介/预约须知 + 日期条 + 去预约行), 组版页没有这些指纹。
_IMG_ASSET_ID_RE = re.compile(r'^\d{8,}_[A-Za-z0-9]{2,}$')


def _doc_name_rendered_as_image(s):
    """页头带(200<=y1<550)存在「text 像图片资源号」的 Image 节点 = 姓名被渲染成图片的指纹。"""
    for tag in re.findall(r'<node[^>]*?/?>', s):
        cls = re.search(r'class="([^"]*)"', tag)
        if not cls or 'Image' not in cls.group(1):
            continue
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', tag)
        if not b or not (200 <= int(b.group(2)) < 550):
            continue
        t = re.search(r'text="([^"]*)"', tag)
        if t and _IMG_ASSET_ID_RE.match(t.group(1).strip()):
            return True
    return False


# ================= S13: L2 卡体滚动纪律 (S11 H3 修复, 2026-10-03) =================
# 实测几何(十院眼科, 屏幕 1080x2392): 内容区 LinearLayout [0,0][1080,2347]; 屏外卡体被压平
# 为零高裁剪节点(底部 y=2347 / 顶部 y=241); 首屏卡中心 ∈ [1081,2192]; 日期条 y≈702-811、
# 子 tab 行 y≈871-1000 — 卡体中心落进这些区域时 tap 会命中日期 chip/子 tab 而非卡。
L2_LIST_X = 540        # 列表区 swipe/tap 的 x(卡体 x 244..852 中线)
L2_TAP_Y_MIN = 1050    # 卡体中心 y 下限(必须低于子 tab 行 871-1000)
L2_TAP_Y_MAX = 2200    # 卡体中心 y 上限(必须高于内容区底 2347, 保证卡完整可见)


def _tap_y_bounds():
    """卡体中心 y 安全区(2026-10-14 双机适配): 常量 1050/2200 是按 2392 高 Redmi 标定的，
    MI 5X(1080x1920) 首屏卡中心实测 1008~1900 —— 绝对下限 1050 会把首屏第一张卡挡在安全区外
    (白滚一轮)，绝对上限 2200 又超出屏幕。改为比例 + 屏底钳制:
    2392 屏 => 1050..2192(与旧常量一致); 1920 屏 => 843..1766。"""
    return Y(0.439), min(Y(0.92), screen_hw()[1] - 60)


def _card_in_view(nm):
    """S13: 卡体姓名节点是否可直接 tap(真实高 + 中心 y 在安全区 [L2_TAP_Y_MIN, L2_TAP_Y_MAX])。
    零高裁剪节点(高<=20)与中心越界节点一律判不可直接 tap — 直接 tap 其中心
    (如首屏裁剪卡 (548,2347))落在屏幕底边无卡区域 => 不跳转(S11 C2 实测)。"""
    if not node_visible(nm):
        return False
    cy = (nm[3] + nm[5]) // 2
    lo, hi = _tap_y_bounds()
    return lo <= cy <= hi


def _ensure_card_visible(adb, name, max_steps=8, settle=1.0):   # 2026-10-07: settle 1.6->1.0
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
            elif y2 - y1 <= CLIP_H_MAX:
                if y1 < Y(0.25):          # 2026-10-14: 旧绝对值 600 在 1920 屏上把屏底裁剪也判成顶部
                    clip_top = (t, c, x1, y1, x2, y2)
                else:
                    clip_bot = (t, c, x1, y1, x2, y2)
        # 2026-10-07: safe zone is SUBTAB-AWARE, not a fixed 1050/2200 (tuned for the 2392 Redmi).
        # Lower bound = subtab-row bottom + 10 (the tap point = card center must be below the subtab
        # row; on short screens / single-item lists the card can sit just under the subtabs and a
        # fixed boundary scrolls forever). Upper bound clamped to the real screen bottom.
        subs = find_subtab_nodes(rows)
        sub_bottom = max((r[5] for r in subs), default=None)
        _lo, _hi = _tap_y_bounds()
        y_min = (sub_bottom + 10) if sub_bottom is not None else _lo
        y_max = _hi
        if vis is not None:
            cy = (vis[3] + vis[5]) // 2
            if y_min <= cy <= y_max:
                return vis
            direction = 'down' if cy < y_min else 'up'   # 偏上 => 内容下移; 偏下 => 内容上移
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
            _a = Y(0.795)
            adb.swipe(L2_LIST_X, _a, L2_LIST_X, max(Y(0.125), _a - step), 350)
        else:
            _a = Y(0.46)
            adb.swipe(L2_LIST_X, _a, L2_LIST_X, min(Y(0.96), _a + step), 350)
        time.sleep(settle)
        key = tuple(names_now)
        stable = stable + 1 if key == prev_key else 0
        prev_key = key
        if stable >= 2 and vis is None:
            # 已到列表一端(连续 2 次 dump 卡集不变)但目标仍未出现 => 翻向继续
            direction = 'up' if direction == 'down' else 'down'
            stable = 0
    return None


def _scroll_l2_to_top(adb, s, max_steps=14, settle=0.9):   # 2026-10-07: settle 1.2->0.9
    """S19(V2): 把 L2 列表滚回顶部 => (s, ok)。
    采完一个子 tab 后列表停在底部(懒加载) => 子 tab 行被裁剪为零高(实测 y=241),
    首屏子 tab 坐标失效: 直接 tap 会命中医生卡离开 L2
    (2026-10-04 10:17 跑 普通门诊/专病门诊 nav_fail 根因)。
    成功条件: find_subtab_nodes 非空(子 tab 行可见且非零高); 已在顶部则立即返回。"""
    for _i in range(max_steps + 1):
        if find_subtab_nodes(parse_nodes(s)):
            return s, True
        adb.swipe(L2_LIST_X, Y(0.42), L2_LIST_X, Y(0.96), 350)   # 内容下移 = 向上滚 (2026-10-07 大步; 2026-10-14 改屏高比例, 1920 短屏不再被钳成零距)
        time.sleep(settle)
        s = adb.dump()
        if page_kind(s) not in ('L2', 'L2b'):
            return s, False
    return s, bool(find_subtab_nodes(parse_nodes(s)))


def _enumerate_l2_cards(adb, s0, max_swipes=12, settle=1.0):   # 2026-10-07: settle 1.6->1.0(swipe dur=350ms, 1.0s 足够列表稳定)
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
            vis = (c['name_node'][5] - c['name_node'][3]) > CLIP_H_MAX
            old = merged.get(nm)
            if old is None:
                merged[nm] = c
                order.append(nm)
            elif vis and (old['name_node'][5] - old['name_node'][3]) <= 0:
                merged[nm] = c   # 偏好真实高(可 tap)几何

    def _tail_key(dump_s):
        # 2026-10-07: 仅以卡名序列判稳定 — 旧版含末 3 卡 y 几何, 底部回弹/沉降抖动
        # 使 stable 永不达 2 => 白滚满 12 次(用户观察到“滚到头仍滚十次以上”的根因)
        return tuple(c['name_node'][0] for c in _find_doc_cards(parse_nodes(dump_s)))

    s = s0
    if page_kind(s) not in ('L2', 'L2b'):
        return s, []
    _merge(s)
    prev = _tail_key(s)
    stable = 0
    for _i in range(max_swipes):
        adb.swipe(L2_LIST_X, Y(0.795), L2_LIST_X, Y(0.46), 350)   # 内容上移: 看下方(2026-10-14 屏高比例)
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


def subtab_names(s):
    """这页**有没有**子tab(观测层: rid=van-tabs-* 优先, 与几何无关) -> [名字]。

    与 find_subtab_nodes 的分工: 这里回答"存不存在", find_subtab_nodes 回答"能不能点"
    (要有可点几何)。两者必须分开 —— 混为一谈时, 塌陷帧(整页零高)会把子tab 筛空,
    于是把三 tab 页面误判成 '(单列,无子tab)', 再把默认激活的 专家门诊 列表当成
    普通门诊采走(2026-10-15 20:49 端侧实测, probe 就是这么错的)。
    """
    return list(PS.observe(s).subtabs or [])


def find_subtab_nodes(rows):
    """L2 子 tab 节点(SUB_TABS 文本)带几何, 按 x 左->右, 按名去重。
    实测(十院眼科 L2): class=View 横排(y=871: x=112..399 / 396..683 / 680..967; 另有 y=468 变体),
    宽 ~287, 高 >20。带宽过滤避免误抓医生卡区/日期条同名文本。
    2026-10-14 双机适配: 旧窗口是绝对值 300<y1<1100(按 2392 高 Redmi 标定)，MI 5X(1080x1920)
    同一页面子 tab 行实测 y1=192 —— 绝对窗口把整行筛空 => 误判 subtab_absent。改为比例窗口
    Y(0.08)<y1<Y(0.46): 2392 屏=191..1100(871/468 均命中), 1920 屏=154..883(192 命中,
    首屏医生卡 1008 仍在窗外)。"""
    out = []
    for (t, c, x1, y1, x2, y2) in rows:
        if t not in SUB_TABS:
            continue
        if not (Y(0.08) < y1 < Y(0.46) and (x2 - x1) > 80 and (y2 - y1) > 20):
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


def _bump(d, k):
    d[k] = d.get(k, 0) + 1


def _find_dept_cards(rows):
    """识别'无按钮科室卡'变体(2026-10-09): 部分科室的普通门诊子 tab 渲染的是科室卡
    (defaltDepartment logo + 科室名 + 一行'简介：××普通门诊'), 整页 0 个
    去预约/已约满/暂无可预约 => _find_doc_cards 锚定按钮返回 0 张, 旧 _cards_key 返回空 ()
    => 误判'卡集与上一子 tab 相同'(=点击未生效) => 补点/往返探测白烧 46-113s/单元。
    该变体当下不可约(点卡身实测不跳转, 页面尺寸不变)。
    返回简介行文本列表(非空 => 指纹非空, 调用方据此诚实判 no_slot/dept_card_no_button)。
    判据(紧, 不误伤医生卡): 文本以'简介'开头且含'普通门诊'且为可见正文行(宽>80, 高>0)。"""
    out = []
    for (t, c, x1, y1, x2, y2) in rows:
        if not t:
            continue
        if t.startswith('简介') and '普通门诊' in t and (x2 - x1) > 80 and (y2 - y1) > 0:
            if t not in out:
                out.append(t)
    return out


def _cards_key(rows):
    """L2 医生卡集合指纹(姓名文本多重集), 用于子 tab 切换后判断重渲染完成。"""
    # 无按钮科室卡(简介：××普通门诊)一并纳入 => 指纹非空, 消除旧版"卡集与上一子 tab 相同"误判。
    # 本函数仅用于"内容是否变化"判定, 不用于统计医生数(统计用 _find_doc_cards 直接计数)。
    names = [c['name_node'][0] for c in _find_doc_cards(rows)]
    names += _find_dept_cards(rows)
    return tuple(sorted(names))


def _tap_subtab_and_wait(nav, adb, node, prev_key):
    """点 L2 子 tab 并轮询等右栏重渲染 => (status, s)。
    status: ok=出现新卡集或空态 / no_cards=等待渲染后仍无医生卡且无空态文本(列表未渲染完/空态缺文本) /
            unchanged=仍是上一子 tab 卡集且无空态(点击未生效, 不可信, 不采) / left=页面离开 L2。
    WebView 切 tab 实测可 >10s => 轮询 8 次。
    S9: 点节点中心(S7 实测中心 (539,935) 有效; 旧公式钉在 y2-12 贴近下划线指示器);
    切 tab 后列表 DOM 重建+懒加载重触发, 前几次 dump 卡集为空 () — 必须与"点击未生效"
    (卡集==prev) 区分, 不能提前判 ok; no_cards/unchanged => 补点 1 次(同一操作最多 2 次)再轮询。"""
    target = node[0]
    cx, cy = (node[2] + node[4]) // 2, (node[3] + node[5]) // 2
    log('  subtab tap %r center=(%d,%d)' % (target, cx, cy))
    s_before = adb.dump()
    att = [0]

    def _do_tap():
        # 第 1 次点节点中心; 第 2 次在新 dump 里重新定位(切 tab 后 DOM 重建, 位置会漂);
        # 第 3 次点上边缘(避开下划线指示器) —— 与旧版 retry#1/retry#2 等价。
        nonlocal cx, cy
        att[0] += 1
        if att[0] == 2:
            n2 = find_node(parse_nodes(adb.dump()), target, cls='View',
                           y1min=node[3] - 160, y1max=node[3] + 160)
            if n2 and node_visible(n2):
                cx2, cy2 = (n2[2] + n2[4]) // 2, (n2[3] + n2[5]) // 2
                if (cx2, cy2) != (cx, cy):
                    log('  subtab re-located %r (%d,%d)->(%d,%d)' % (target, cx, cy, cx2, cy2))
                    cx, cy = cx2, cy2
        if att[0] >= 3:
            adb.tap(cx, node[3] + 15)
        else:
            adb.tap(cx, cy)

    def _ok(o, s_dump):
        # 硬判据 selected="true"; 只有它**不可用**(未渲染/旧版页面)时才退旧启发式,
        # 且「上一个子tab 的空态」不能算成功 —— 这正是「到了普通门诊页却进不去」的机制。
        if o.subtab_active:
            if PS._name_ok(target, o.subtab_active):
                return True, 'selected=%s' % o.subtab_active
            return False, '停在 selected=%s' % o.subtab_active
        if '暂无可预约' in s_dump:
            return True, '空态文本(无 selected)'
        nk = _cards_key(parse_nodes(s_dump))
        if nk and nk != prev_key:
            return True, '卡片集变化(无 selected)'
        if nk == prev_key:
            return False, '卡片集未变(无 selected)'
        return False, '无卡片且无空态文本(无 selected)'

    _soft = [0]

    def _abort(o, s_dump):
        # 空窗 = 读屏失败(可恢复); 'unknown' 多半是切 tab 时 SPA 重建的半成品, 也要再等一轮
        # 定性 —— 连续两次都不是 L2 才判"离开"。明确的其他页(六字段/登录/风控/L1/搜索/首页)
        # 一次就走: 在错的页面上重复点击只会越走越深。
        if o.level == 'no_window':
            return None
        if o.level in ('L2', 'L2b'):
            _soft[0] = 0
            return None
        if o.level == 'unknown':
            _soft[0] += 1
            return ('left(unknown x%d)' % _soft[0]) if _soft[0] >= 2 else None
        return 'left(%s)' % o.level

    # ②(2026-05): 点 -> 观测 -> 判定 交给 act; 最多 3 次动作(中心/重定位/上边缘),
    # 每次判定都进证据链(期望=目标子tab, 实测=页面 selected 或降级依据)。
    ok, o, s, why = act(adb, {'level': ('L2', 'L2b'), 'subtab': target},
                        action=_do_tap, state='subtab', what='子tab:%s' % target,
                        timeout=9.0, settle=1.2, ok_when=_ok, abort=_abort,
                        attempts=3, attempt_settle=1.5)
    if ok:
        st = 'ok'
    elif (why or '').startswith('abort:left'):
        st = 'left'
    elif 'selected=' in (why or '') or '卡片集未变' in (why or ''):
        st = 'unchanged'
    else:
        st = 'no_cards'
    # 播报在判定之后: 浮窗说的就是屏幕上真实停下的位置
    _act = o.subtab_active if o else ''
    if st == 'ok':
        _step('已进入子tab: %s' % target, 'ok')
    else:
        _step('校验失败 · 子tab 未生效: %s%s'
              % (target, ('(实测=%s)' % _act) if _act else ''), 'fail')
    return st, s


# ---------------- S20 (2026-10-04): --condition 统一接口 ----------------
COND_KEYS = {'hospital', 'tab', 'dept', 'date', 'slot_type', 'time_range', 'doctor'}


def parse_condition(obj, label, today):
    """S20: 解析/校验一条 condition JSON => 规范化 dict。未知 key/类型错 => ValueError(诚实, 不静默跑)。"""
    if not isinstance(obj, dict):
        raise ValueError('%s: condition 必须是 JSON 对象' % label)
    bad = [k for k in obj if k not in COND_KEYS]
    if bad:
        raise ValueError('%s: 未知 key %s (允许: %s)' % (label, bad, sorted(COND_KEYS)))
    h = obj.get('hospital')
    if not h or not isinstance(h, str) or not h.strip():
        raise ValueError('%s: 必须给 hospital(中文名或 SH-xxx)' % label)

    def _opt(k):
        v = obj.get(k)
        if v is None:
            return None
        if not isinstance(v, str) or not v.strip():
            raise ValueError('%s: %s 须为非空字符串或 null' % (label, k))
        return v.strip()

    tab = _opt('tab')
    dept = _opt('dept')
    doctor = _opt('doctor')
    date_iso = date_md = None
    d = obj.get('date')
    if d is not None:
        if not isinstance(d, str) or not re.match(r'^\d{4}-\d{2}-\d{2}$', d):
            raise ValueError('%s: date 须为 "YYYY-MM-DD" 或 null' % label)
        try:
            dd = dt.date.fromisoformat(d)
        except ValueError:
            raise ValueError('%s: date 非有效日历日期: %s' % (label, d))
        if dd < today:
            raise ValueError('%s: date %s 早于今天 %s(随申办只卖未来日期)' % (label, d, today))
        date_iso, date_md = d, d[5:]
    st = obj.get('slot_type')
    if st is not None and st not in SLOT_TYPE_EN2ZH:
        raise ValueError('%s: slot_type 须为 expert|normal|specialty 之一或 null' % label)
    tr = None
    trv = obj.get('time_range')
    if trv is not None:
        if isinstance(trv, str):
            m2 = re.match(r'^\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s*$', trv)
            if not m2:
                raise ValueError('%s: time_range 字符串须为 "HH:MM-HH:MM"' % label)
            trv = [m2.group(1), m2.group(2)]
        if not (isinstance(trv, (list, tuple)) and len(trv) == 2):
            raise ValueError('%s: time_range 须为 ["HH:MM","HH:MM"] 或 null' % label)

        def _hm(x):
            m3 = re.match(r'^\s*(\d{1,2}):(\d{2})\s*$', str(x))
            if not m3:
                raise ValueError('%s: time_range 时刻格式须为 HH:MM' % label)
            return int(m3.group(1)) * 60 + int(m3.group(2))
        tr = (_hm(trv[0]), _hm(trv[1]))
        if tr[0] > tr[1]:
            raise ValueError('%s: time_range 起时刻晚于止时刻' % label)
    return {'hospital': h.strip(), 'tab': tab, 'dept': dept, 'doctor': doctor,
            'date': date_iso, 'date_md': date_md, 'slot_type': st, 'time_range': tr}


def load_conditions_cli(a, today):
    """S20: 汇总 --condition(可重复) + --condition-file 的条件 => (conds, file_device, file_deadline)。"""
    raws = []
    for i, c in enumerate(a.condition):
        try:
            raws.append(json.loads(c))
        except Exception as e:
            raise SystemExit('--condition#%d 不是合法 JSON: %s' % (i, e))
    fdev = fdl = None
    if a.condition_file:
        try:
            data = json.load(open(a.condition_file, encoding='utf-8'))
        except Exception as e:
            raise SystemExit('--condition-file 读取/解析失败: %s' % e)
        if isinstance(data, dict):
            fdev, fdl = data.get('device'), data.get('deadline')
            items = data.get('conditions', [])
        elif isinstance(data, list):
            items = data
        else:
            raise SystemExit('--condition-file 顶层须为 JSON 列表或 {"conditions": [...]}')
        raws.extend(items)
    if not raws:
        raise SystemExit('无 condition: 用 --condition <json>(可重复) 或 --condition-file <json>')
    conds = []
    for i, obj in enumerate(raws):
        try:
            conds.append({'cond': parse_condition(obj, 'condition#%d' % i, today), 'raw': obj})
        except ValueError as e:
            raise SystemExit(str(e))
    return conds, fdev, fdl


def _pos_now(**kw):
    p = {'level': None, 'hid': None, 'hosp': None, 'tab': None, 'dept': None,
         'subtab': None, 'date': None, 'doctor': None,
         'ts': dt.datetime.now().isoformat(timespec='seconds')}
    p.update(kw)
    return p


def save_nav_state(pos):
    """S20 需求3: 持久化当前页面位置(每确认到达一个页面调用一次)。"""
    if pos is None:
        return
    try:
        tmp = NAV_STATE_FILE + '.tmp'
        json.dump(pos, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
        os.replace(tmp, NAV_STATE_FILE)
    except Exception as e:
        log('  WARN: nav_state 保存失败: %s' % e)


def load_nav_state():
    try:
        return json.load(open(NAV_STATE_FILE, encoding='utf-8'))
    except Exception:
        return None


def slot_in_range(row_text, tr):
    """S20: 时段行是否完全落在 [tr[0], tr[1]](分钟, 边界含)。tr=None => 不限制。
    不可解析的时段 => False(诚实: 带 time_range 条件时无法核实 => 不采)。"""
    if tr is None:
        return True
    m = SLOT_ROW_RE.match(row_text)
    if not m:
        return False
    sh, sm = m.group(2).split(':')
    eh, em = m.group(3).split(':')
    return (int(sh) * 60 + int(sm)) >= tr[0] and (int(eh) * 60 + int(em)) <= tr[1]


def _doc_bar_cell(s, md):
    """S20: 医生主页日期条中日期段 == md('MM-DD') 的格子 => (text, [x1,y1,x2,y2]|None)。
    屏外格 DOM 零 bounds 但 text 完整(L1 tab 同款现象) => 整条日期范围即使屏外也可查。"""
    for nm2 in re.finditer(r'<node [^>]*?/?>', s):
        n = nm2.group(0)
        tm = re.search(r'text="([^"]*)"', n)
        if not tm:
            continue
        m2 = BAR_RE.match(tm.group(1))
        if m2 and m2.group(2) == md:
            bm2 = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', n)
            b = [int(bm2.group(i)) for i in (1, 2, 3, 4)] if bm2 else None
            return tm.group(1), b
    return None


def select_date_doc_home(adb, s, md, max_scroll=10):
    """S20: 医生主页日期条点格切到指定日期(md='MM-DD')。返回 (status, s):
      ok               = 已切(页内时段行全为该日 / 或当日无行=暂无可预约)
      date_not_found   = 日期不在条内(超出条范围, 如 >30 天)
      date_select_fail = 点过但页内行不全为该日(tap 未生效; 已重试 1 次)
    2026-10-04 设备实测(徐建江主页): 日期条 30 格, 首屏可见 6 格; 点「周三 10-07 有号」
    => 页内行立即只留 10-07(折叠 3 行 + 显示全部排班), 「不限 日期」标签不变 => 以「行全为该日」验证。"""
    def _verify(sx):
        for nm2 in re.finditer(r'<node [^>]*?/?>', sx):
            tm = re.search(r'text="([^"]*)"', nm2.group(0))
            if not tm:
                continue
            m2 = SLOT_ROW_RE.match(tm.group(1))
            # S20 \u4fee\u6b63: \u65f6\u6bb5\u884c\u65e5\u671f\u4e3a\u5b8c\u6574 ISO(2026-10-07), \u53ea\u5339 MM-DD \u6bb5
            if m2 and m2.group(1)[5:] != md:
                return False
        return True
    for attempt in range(2):
        if attempt:
            s = adb.dump()
        c = _doc_bar_cell(s, md)
        if c is None:
            return 'date_not_found', s
        text, b = c
        if not b or b == [0, 0, 0, 0]:
            found = None
            for _sc in range(max_scroll):
                adb.swipe(1000, Y(0.44), 140, Y(0.44), 300)   # 2026-10-14: 医生页日期条按屏高定位
                time.sleep(1.3)
                s = adb.dump()
                c = _doc_bar_cell(s, md)
                if c is None:
                    return 'date_not_found', s
                if c[1] and c[1] != [0, 0, 0, 0]:
                    found = c
                    break
            if found is None:
                return 'date_not_found', s
            text, b = found
        cx, cy = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
        log('  date tap %r center=(%d,%d) attempt=%d' % (text, cx, cy, attempt + 1))
        adb.tap(cx, cy)
        time.sleep(2.5)
        ok = False
        for _w in range(8):
            s = adb.dump()
            if page_kind(s) not in ('six', 'doc'):
                break
            if _verify(s):
                ok = True
                break
            time.sleep(2.0)
        if ok:
            return 'ok', s
    return 'date_select_fail', s


def resolve_start(adb, nav, target, stats):
    """S20 需求4: 每次检索开始先判断当前手机屏幕页面位置(实时 dump 为准, 与 nav_state 记录互证)。
    target = {'hid','hosp'}。返回 pos; 位置不符/不可识别 => 整体重置(重新打开随申办 => 444)。"""
    rec = load_nav_state()
    s = adb.dump()
    k = page_kind(s)
    p444 = _pos_now(level='444')
    if k == '444':
        log('  START: 当前=444 列表(全新开始)')
        save_nav_state(p444)
        return p444
    if k == 'auth':
        nav.agree_auth(s)
        s = adb.dump()
        if page_kind(s) == '444':
            save_nav_state(p444)
            return p444
    if k == 'search':
        adb.back(); time.sleep(2.0)
        s = adb.dump()
        if page_kind(s) == '444':
            log('  START: 频道搜索页 -> back => 444')
            save_nav_state(p444)
            return p444
    if k == 'L1':
        if target['hosp'] in s:
            p = _pos_now(level='L1', hid=target['hid'], hosp=target['hosp'])
            save_nav_state(p)
            log('  START: 当前=L1 %s(复用, 免重搜)' % target['hid'])
            return p
        st, s2 = nav.ensure_444()
        if st == 'ok':
            save_nav_state(p444)
            return p444
    if k in ('L2', 'L2b'):
        if rec and rec.get('hid') == target['hid'] and rec.get('dept') and rec['dept'] in s:
            p = dict(rec); p['level'] = 'L2'
            p['ts'] = dt.datetime.now().isoformat(timespec='seconds')
            save_nav_state(p)
            log('  START: 当前=L2 %s|%s(记录互证通过, 复用)' % (target['hid'], rec['dept']))
            return p
        if rec and rec.get('hid') == target['hid']:
            adb.back(); time.sleep(2.0)
            s2 = adb.dump()
            if page_kind(s2) == 'L1' and target['hosp'] in s2:
                p = _pos_now(level='L1', hid=target['hid'], hosp=target['hosp'])
                save_nav_state(p)
                log('  START: L2(部门不符) -> back => L1 %s(复用)' % target['hid'])
                return p
    if k in ('doc', 'six'):
        if rec and rec.get('hid') == target['hid'] and rec.get('dept'):
            adb.back(); time.sleep(2.0)
            s2 = adb.dump()
            if page_kind(s2) in ('L2', 'L2b') and rec['dept'] in s2:
                p = dict(rec)
                p['level'] = 'L2'; p['doctor'] = None; p['date'] = None
                p['ts'] = dt.datetime.now().isoformat(timespec='seconds')
                save_nav_state(p)
                log('  START: 医生主页 -> back => L2 %s|%s(记录互证)' % (target['hid'], rec['dept']))
                return p
    # C(2026-10-09 实测 476cf991): 这里曾「位置不符 => 反复 HOME+launch」=> 死循环(每轮 2~5 分钟)。
    # 现在 reset_full 内部配额最多 1 次, 用满后自动退化为「用当前页走搜索通道」(ensure_444)。
    log('  START: 位置不符/不可识别 (live=%s rec=%s) => 整体重置(最多 1 次), 之后用当前页继续搜索通道'
        % (k, (rec or {}).get('level')))
    stats['reset'] += 1
    for _t in range(2):
        st, s2 = nav.reset_full()
        if st == 'ok':
            save_nav_state(p444)
            return p444
    log('  FATAL: reset 2 次仍到不了 444')
    raise SystemExit(2)


# ---- 到目标 L2 的导航: 状态机版(2026-10-15 第③步) ----
# 旧版(_goto_unit_legacy)按 pos(上次记下的位置)做 if 链: 屏幕与记录一分叉就照旧假设硬点,
# 于是「浮窗说进了、屏上没进」「普通门诊没进去」。新版只看**当前观测**:
#   观测当前页 -> 查 nav_machine 转移表选边 -> 执行 -> act() 轮询到"观测==期望"才算走通
#   -> 没走通就重新观测、重新查表(re-plan), 而不是沿用旧 pos 硬走。
# pos 退化为记录/统计/续跑用, 不再是决策依据。
GOTO_ENGINE = 'machine'   # 逃生口(保留一个版本); 端侧连续干净运行后删除 legacy 分支


def goto_unit(nav, adb, pos, g, stats):
    """把**当前屏幕**搬到 (g['tab'], g['dept']) 的 L2。返回 (ok|blocked|fail, s, pos)。"""
    if GOTO_ENGINE != 'machine':
        return _goto_unit_legacy(nav, adb, pos, g, stats)
    hid, hosp, tab, dept = g['hid'], g['hosp'], g['tab'], g['dept']
    first_d = None
    for t in g['m']['features']['bookable_departments']['value']['l1']:
        if t['name'] == tab:
            first_d = ([d.get('raw') for d in t.get('depts', [])] or [None])[0]
            break

    def _back():
        adb.back()
        time.sleep(2.0)

    def _open_dept():
        return nav.open_dept(tab, dept, g['plan_order'], first_d)

    def _enter_l1():
        return nav.enter()

    def _to_444():
        return nav.ensure_444()

    def _reset():
        st, s2 = nav.reset_full()
        if st != 'ok':
            return st, s2
        return nav.enter()

    def _nudge():
        """塌陷帧(WebView 无障碍树 bounds 全空)时轻扫一下, 逼页面重新渲染。
        只碰内容区中部, 不点任何业务元素(不选日期/不点卡片/不切子tab), 不改业务状态。

        这里**只负责动作**: "重排是否成功"由状态机那一次 act 轮询判定(单一判定、
        单一证据、熔断只记一次), 不在动作里再套一层判定。"""
        adb.swipe(L2_LIST_X, Y(0.62), L2_LIST_X, Y(0.42), 300)
        return 'ok', adb.dump()

    actions = {'back_l2': _back, 'back_l1': _back, 'open_dept': _open_dept,
               'enter_l1': _enter_l1, 'search': _to_444, 'reset': _reset,
               'nudge': _nudge,
               'agree_auth': lambda: nav.agree_auth(adb.dump())}

    def _act(expect=None, action=None, state='', what='', timeout=None,
             ok_when=None, attempts=1, abort=None):
        # 超时/重试由状态机的预算表给(nav_machine.STATES/EDGE_BUDGET), 不在这写死数字
        return act(adb, expect, action=action, state=state, what=what or state,
                   timeout=timeout, ok_when=ok_when, attempts=attempts, abort=abort)

    st, obs, s, why, path = NM.run(adb.dump, obs_of, _act,
                                   {'level': ('L2', 'L2b'), 'dept': dept},
                                   actions, ctx={'dept': dept},
                                   trace=lambda *a: trace_ev(*a), log=log,
                                   judge=verify_judge,
                                   tripped=lambda: VERIFY['tripped'])
    for _n, _okk, _w in path:
        if _n == 'reset':
            stats['reset'] += 1
        elif _n == 'enter_l1':
            stats['enter'] += 1
        elif _n == 'back_l1':
            stats['back_to_l1'] += 1
    if st == 'ok':
        if not path:
            stats['stay_l2'] += 1
        pos = _pos_now(level='L2', hid=hid, hosp=hosp, tab=tab, dept=dept)
        save_nav_state(pos)
        log('  GOTO(machine): 到达 L2 %s|%s  path=%s  %s'
            % (tab, dept, [p[0] for p in path], obs.brief()))
        return 'ok', s, pos
    log('  GOTO(machine): %s (%s) path=%s  实测=%s'
        % (st, why, [p[0] for p in path], obs.brief() if obs else '-'))
    return ('blocked' if st in ('blocked', 'login') else 'fail'), s, pos



def _goto_unit_legacy(nav, adb, pos, g, stats):
    """S20 需求4: 从当前 pos 到 (g['tab'], g['dept']) 的 L2, 最少回退(同医院)。
    返回 (ok|blocked|fail, s, pos)。导航异常 => reset_full + enter 重试一次。"""
    hid, hosp, tab, dept = g['hid'], g['hosp'], g['tab'], g['dept']
    first_d = None
    for t in g['m']['features']['bookable_departments']['value']['l1']:
        if t['name'] == tab:
            first_d = ([d.get('raw') for d in t.get('depts', [])] or [None])[0]
            break
    # case 1: 已在目标 L2(同医院+tab+dept) => 原地停留(重 dump 验证)
    if pos.get('level') in ('L2', 'L2b') and pos.get('hid') == hid and pos.get('tab') == tab and pos.get('dept') == dept:
        s = adb.dump()
        k = page_kind(s)
        if k in ('L2', 'L2b') and dept in s:
            stats['stay_l2'] += 1
            pos['level'] = 'L2'; pos['date'] = None; pos['doctor'] = None
            pos['ts'] = dt.datetime.now().isoformat(timespec='seconds')
            save_nav_state(pos)
            log('  GOTO: 停留 L2 %s|%s(复用)' % (tab, dept))
            return 'ok', s, pos
        if k in ('doc', 'six'):
            adb.back(); time.sleep(2.0)
            s = adb.dump()
            if page_kind(s) in ('L2', 'L2b') and dept in s:
                stats['stay_l2'] += 1
                pos['level'] = 'L2'; pos['date'] = None; pos['doctor'] = None
                save_nav_state(pos)
                log('  GOTO: 医生主页 -> back => L2 %s|%s(复用)' % (tab, dept))
                return 'ok', s, pos
    # case 2: 在医生主页(任意) => back 到 L2
    if pos.get('level') in ('doc', 'six'):
        adb.back(); time.sleep(2.0)
        s = adb.dump()
        if page_kind(s) in ('L2', 'L2b'):
            pos['level'] = 'L2'; pos['date'] = None; pos['doctor'] = None
            save_nav_state(pos)
        else:
            pos['level'] = None
    # case 3: 在 L2 但非目标 tab/dept => back 到 L1
    if pos.get('level') in ('L2', 'L2b'):
        adb.back(); time.sleep(2.0)
        s = adb.dump()
        if page_kind(s) == 'L1' and nav.verify_L1(s):
            stats['back_to_l1'] += 1
            pos['level'] = 'L1'; pos['dept'] = None; pos['subtab'] = None; pos['date'] = None
            save_nav_state(pos)
        else:
            pos['level'] = None
    if pos.get('level') is None:
        # 位置丢失 => 重置 + 重进
        log('  GOTO: 位置丢失 => reset + enter')
        stats['reset'] += 1
        st, s2 = nav.reset_full()
        if st != 'ok':
            return 'fail', s2, pos
        st, s2 = nav.enter()
        if st != 'ok':
            return st, s2, pos
        pos = _pos_now(level='L1', hid=hid, hosp=hosp)
        save_nav_state(pos)
    # 现在应为 L1(同医院) => 开科室
    ost, s, tab_ok = nav.open_dept(tab, dept, g['plan_order'], first_d)
    if ost == 'blocked':
        return 'blocked', s, pos
    if ost != 'ok':
        log('  GOTO: open_dept fail(%s) => reset + enter + 重试一次' % ost)
        st, s2 = nav.reset_full()
        if st == 'ok':
            st, s2 = nav.enter()
            if st == 'ok':
                pos = _pos_now(level='L1', hid=hid, hosp=hosp)
                save_nav_state(pos)
                ost, s, tab_ok = nav.open_dept(tab, dept, g['plan_order'], first_d)
                if ost == 'blocked':
                    return 'blocked', s, pos
    if ost == 'ok':
        pos['level'] = 'L2'; pos['hid'] = hid; pos['hosp'] = hosp
        pos['tab'] = tab; pos['dept'] = dept; pos['subtab'] = None
        pos['date'] = None; pos['doctor'] = None
        pos['ts'] = dt.datetime.now().isoformat(timespec='seconds')
        save_nav_state(pos)
        return 'ok', s, pos
    return 'fail', s, pos


def expand_conditions(conds):
    """S20: dept=null 且 tab 已给 => 展开 manifest 该 tab 下全部科室(每科室一个单元);
    dept 已给 => 1 单元。分组键 (hid,tab,dept,date,slot_type,doctor): 同键条件(仅 time_range 不同)
    共享一次采集。返回 (units, errors); units 保持调用方顺序(管线中=静态分序, 第1名先跑; 2026-10-05 修复: 原按 (hid,tab,dept) 字母排序会覆盖优先级)。"""
    groups = {}
    errors = []
    order = []
    for idx, it in enumerate(conds):
        cond, raw = it['cond'], it['raw']
        m = cond['_m']
        hid, hosp = m['id'], m['institution']['name']
        l1 = m['features']['bookable_departments']['value']['l1']
        tinfo = next((t for t in l1 if t['name'] == cond['tab']), None)
        if tinfo is None:
            errors.append({'idx': idx, 'raw': raw,
                           'error': 'tab %r 不在 %s manifest(live 页可能有; 建议核对 tab 名)' % (cond['tab'], hid)})
            continue
        depts = [d.get('raw') for d in tinfo.get('depts', []) if d.get('raw')]
        if cond['dept'] is not None:
            depts = [cond['dept']]
        if not depts:
            errors.append({'idx': idx, 'raw': raw,
                           'error': 'manifest tab %r 下无科室列表(dept=%s)' % (cond['tab'], cond['dept'])})
            continue
        plan_order = [t['name'] for t in l1]
        for d in depts:
            gk = (hid, cond['tab'], d, cond['date'], cond['slot_type'], cond['doctor'])
            g = groups.get(gk)
            if g is None:
                g = groups[gk] = {'hid': hid, 'hosp': hosp, 'm': m, 'plan_order': plan_order,
                                  'tab': cond['tab'], 'dept': d, 'members': [], 'gk': list(gk)}
                order.append(g)
            g['members'].append((idx, cond, raw))
    # 2026-10-05: 保持调用方顺序(管线中=静态分序: 排名越靠前越先跑)。
    # 原 sorted((hid,tab,dept)) 为最小化换医院而按字母序重排, 导致静态第1名(如 SH-084)
    # 实际排到第 6 位才跑 —— 优先级代价 >> 导航代价, 故改为保持输入序。
    units = order
    return units, errors



def _verify_subtab_active(adb, s, target, prev_key):
    """2026-10-07c (MI 5X): round-trip probe confirming the target subtab is the ACTIVE one.
    Taps a visible SIBLING subtab; if the card set changes, taps are effective and the
    target tab was already active (the earlier target tap was a no-op) => tap back to the
    target and return the fresh dump. If the sibling tap also changes nothing (WebView not
    responding, or a fully-empty department where every tab shows the same empty list),
    returns False and the caller keeps the honest error. Cost ~10s, only on the ambiguous
    no_cards/unchanged paths."""
    try:
        rows = parse_nodes(s)
        # 2026-05: 有 selected 硬判据时不必再做 ~10s 的往返试探 ——
        # selected 就是目标 => 直接采信; selected 是别的 tab => 直接判未生效(旧版会再点两轮、
        # 每轮 4s, 最终仍证明不了"回到了目标", 于是把没进去的子tab 当采到了)。
        act = PS.active_name(s)
        if act:
            if PS._name_ok(target, act):
                log('  _verify_subtab_active: selected=%r 已是目标(硬判据, 免往返)' % act)
                return s
            log('  _verify_subtab_active: selected=%r 非目标 %r => 判未生效' % (act, target))
            return False
        subs = find_subtab_nodes(rows)
        others = [x for x in subs if x[0] != target and node_visible(x)]
        if not others:
            return False
        o = others[0]
        adb.tap((o[2] + o[4]) // 2, (o[3] + o[5]) // 2)
        time.sleep(4.0)
        s3 = adb.dump()
        if page_kind(s3) not in ('L2', 'L2b'):
            return False
        k3 = _cards_key(parse_nodes(s3))
        if k3 == prev_key:
            return False
        back = [x for x in find_subtab_nodes(parse_nodes(s3)) if x[0] == target]
        if not back:
            return False
        adb.tap((back[0][2] + back[0][4]) // 2, (back[0][3] + back[0][5]) // 2)
        time.sleep(4.0)
        s4 = adb.dump()
        if page_kind(s4) not in ('L2', 'L2b'):
            return False
        return s4
    except Exception as e:
        log('  _verify_subtab_active failed (keep conservative error): %s' % str(e)[:80])
        return False


def fetch_one_dept_unit(nav, adb, m, hosp, hid, tab, dept, today, plan_order, ts_base,
                        jsonl_fh, six_seen, cond=None, pos=None, cond_idxs=None, cond_raws=None,
                        member_trs=None, rec_buffer=None, s2_in=None):
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
    if cond:
        unit['condition'] = {k: cond.get(k) for k in ('date', 'slot_type', 'time_range', 'doctor')}
        unit['cond_idxs'] = list(cond_idxs or [])
        unit['conditions'] = list(cond_raws or [])
        unit.pop('jsonl_file', None)
        unit.pop('summary_file', None)
    first_d = None
    for t in m['features']['bookable_departments']['value']['l1']:
        if t['name'] == tab:
            first_d = ([d.get('raw') for d in t.get('depts', [])] or [None])[0]
            break

    if s2_in is not None:
        # S20: run_condition_mode 的 goto_unit 已导航/验证到目标 L2 => 免重走 open_dept
        # (从 L2 重调 open_dept 会在非 L1 页找 tab 心得 notfound => 双重导航 bug, 2026-10-04 实测)
        s2 = s2_in
        log('  L2 已由 goto 到达/验证(免 open_dept 重走)')
    else:
        ost, s2, tab_ok = nav.open_dept(tab, dept, plan_order, first_d)
        if ost == 'blocked':
            unit['error_reason'] = 'blocked'
            unit['cost_s'] = round(time.monotonic() - t0, 1)
            return unit, True
        if ost != 'ok':
            unit['error_reason'] = 'nav_fail(open_dept=%s,tab_ok=%s): 未到达 L2' % (ost, tab_ok)
            unit['cost_s'] = round(time.monotonic() - t0, 1)
            return unit, False

    # 身份校验(与 probe 同纪律: 先重读 2 次防标题滞后, 仍不符才判死)
    _obs = obs_of(s2)
    _ok_id, _why = PS.dept_consistent(s2, dept, _obs)
    for _r in range(2):
        if _ok_id or page_kind(s2) not in ('L2', 'L2b'):
            break
        log('  身份待确认(期望=%s, %s) => 等 1.2s 重读(%d/2)' % (dept, _why, _r + 1))
        time.sleep(1.2)
        s2 = adb.dump()
        _obs = obs_of(s2)
        _ok_id, _why = PS.dept_consistent(s2, dept, _obs)
    unit['observed'] = _obs.brief()
    trace_ev('enter_l2', dept, {'level': 'L2', 'dept': dept, 'hospital': hosp}, _obs, _ok_id, dump=s2)
    if not _ok_id:
        unit['error_reason'] = 'dept_mismatch(期望=%s 实测%s)' % (dept, _why)
        unit['cost_s'] = round(time.monotonic() - t0, 1)
        log('  身份不符: 期望科室 %s, %s => 不采(交上层回退到 L1 重走)' % (dept, _why))
        return unit, False

    rows = parse_nodes(s2)
    subs = find_subtab_nodes(rows)
    if not subs:
        # S9: 子 tab 行可能尚未渲染(WebView 渲染滞后; 滚动态下子 tab 文本还会被裁剪成
        # y=241 零高 => find_subtab_nodes 返回空) => 短等+重 dump 最多 3 轮, 别急着判"无子 tab"
        # 2026-10-14: 慢机器(MI 5X / Android 8.1)上 3 轮 2s 不够(实测 L2 已到达但子 tab 行
        # 还没渲染出来 => 误判 subtab_absent) => 轮数提到 5, 并在失败时把 L2 现场落盘取证。
        for _r in range(SUBTAB_WAIT_ROUNDS):
            time.sleep(2.0)
            s2 = adb.dump()
            if page_kind(s2) not in ('L2', 'L2b'):
                break
            rows = parse_nodes(s2)
            subs = find_subtab_nodes(rows)
            if subs:
                break
        if not subs and page_kind(s2) in ('L2', 'L2b'):
            # S20 (2026-10-04 实测): L2 复用继承滚动位置(列表停在底部) => 子 tab 行被压零高裁剪
            # => 结论"无子 tab"前先滚回 L2 顶部重 dump
            log('  subtab 行可能被压剪(列表在底部) => 滚顶重 dump')
            s2, _ok_top = _scroll_l2_to_top(adb, s2)
            rows = parse_nodes(s2)
            subs = find_subtab_nodes(rows)
        if not subs and subtab_names(s2):
            # 观测层(rid 优先)看得到子tab, 只是几何不可点 => 这是"帧不可信", 不是"没有子tab"。
            # 旧代码在这里直接降级成单列 => 把默认激活 tab 的列表当目标 tab 采走。
            _nm = subtab_names(s2)
            unit['sub_tabs'] = _nm
            unit['error_reason'] = 'subtab_not_tappable(%s 存在但几何不可点)' % _nm
            unit['cost_s'] = round(time.monotonic() - t0, 1)
            log('  子tab %s 存在但始终不可点 => 不采(不降级为单列)' % _nm)
            return unit, False
        if not subs:
            log('  WARN: subtab row not rendered after %d waits + scroll top -> treat as no sub tab'
                % SUBTAB_WAIT_ROUNDS)
            # 取证: 老机器上"真的没有子 tab"和"WebView 还没渲染"从日志上分不出来,
            # 把 L2 现场 XML 落盘(raw/SH-xxx_..._L2NOSUB_*.xml), 事后可判定。
            try:
                _ev = six_fields.save_raw_evidence(
                    s2, OUT_DIR, hid, dept, 'L2NOSUB',
                    dt.datetime.now().strftime('%Y%m%dT%H%M%S'))
                log('  L2 现场已落盘取证: %s (xml_len=%d)' % (_ev, len(s2 or '')))
            except Exception as _e:
                log('  L2 现场落盘取证失败: %s' % str(_e)[:100])
    sub_names = [x[0] for x in subs]
    if not sub_names:
        # 只有"观测层也确认没有子tab"才允许单列口径(上面已拦住"有名字但不可点")
        sub_names = ['(单列,无子tab)']
        subs = [None]
    unit['sub_tabs'] = sub_names
    # S20: slot_type 收窄 — 只采指定子 tab(cond=None 保持旧行为: 全部子 tab 逐个走)
    target_sub = None
    if cond and cond.get('slot_type'):
        zh = SLOT_TYPE_EN2ZH[cond['slot_type']]
        if sub_names and sub_names[0] != '(单列,无子tab)':
            if zh in sub_names:
                target_sub = zh
            else:
                unit['error_reason'] = 'subtab_absent(%s 不在该科室子 tab %s; 诚实 0 行)' % (zh, sub_names)
                unit['cost_s'] = round(time.monotonic() - t0, 1)
                return unit, False
        else:
            if cond['slot_type'] == 'normal':
                target_sub = '(单列,无子tab)'
                _bump(unit['variants'], 'default_list_as_normal')
                log('  slot_type=normal 落在单列页(无子 tab 行) => 默认列表作普通门诊口径')
            else:
                unit['error_reason'] = 'subtab_absent(%s: 页面无子 tab 行(单列), 无法确认子 tab, 诚实 0 行)' % zh
                unit['cost_s'] = round(time.monotonic() - t0, 1)
                return unit, False
    if target_sub:
        _ti = sub_names.index(target_sub) if target_sub in sub_names else 0
        iter_pairs = [(_ti, target_sub)]
    else:
        iter_pairs = [(i, sub_names[i]) for i in range(len(sub_names))]
    ctx = {'hospital_name': hosp, 'dept_name': dept}
    prev_key = _cards_key(rows)
    n_six = 0
    any_sub_info = False

    for i, subname in iter_pairs:
        t1 = time.monotonic()
        ss = {'status': 'ok', 'error_reason': None, 'doctor_count': 0,
              'bookable_count': 0, 'full_count': 0, 'doctors': [], 'bookable_doctors': [],
              'full_doctors': [], 'empty_state': ('暂无可预约' in s2),
              'six_records': 0, 'expands': [], 'variants': {}, 'cost_s': None}
        _reused = False
        if subname != '(单列,无子tab)' and pos is not None and pos.get('subtab') == subname:
            # S20 需求4: 该子 tab 已处于激活态(位置记录) => 免点击; 必须卡集指纹一致
            # (防 H5 重载后激活子 tab 已回默认 => 静默采错子 tab)
            s2 = adb.dump()
            _rows_chk = parse_nodes(s2)
            _ck = _cards_key(_rows_chk)
            _stored_ck = pos.get('cards_key')
            if _stored_ck is not None:
                _stored_ck = tuple(_stored_ck)
            if page_kind(s2) in ('L2', 'L2b') and any(x[0] == subname for x in find_subtab_nodes(_rows_chk)) and (_stored_ck is None or _stored_ck == _ck):
                log('  subtab %r 已激活(位置复用, 免 tap)' % subname)
                prev_key = _ck
                _reused = True
        # S20 tap 判定: 仅当「新 L2 默认首子tab」(无记录)**且页面没有硬判据反证**、
        # 或「复用已由 cards_key 确认」时免 tap; 其余一律点击重确认(防 stale subtab 静默采错)。
        # ②(2026-05): 有了 selected 硬判据后 i==0 也不能免检 —— 页面实测停在别的 tab 上
        # 就必须点到位(这就是「到了普通门诊页却进不去」的机制; 旧版默认 tab 从不核对)。
        _cur = obs_of(s2)
        _hard = bool(_cur.subtab_active)
        _on_it = _hard and PS._name_ok(subname, _cur.subtab_active)
        _skip_tap = _reused or _on_it or (i == 0 and (pos is None or pos.get('subtab') is None)
                                          and not (_hard and not _on_it))
        ss['verified'] = ('reuse' if _reused else 'selected' if _on_it
                          else 'none' if _skip_tap else 'tap')
        if subname != '(单列,无子tab)' and not _skip_tap:
            # S19(V2): 采集后列表已在底部 => 子 tab 行裁剪、首屏坐标失效;
            # S20: 停留 L2 且已知激活子 tab ≠ 目标 => i==0 也要点
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
                for _j2, _sn2 in iter_pairs[i + 1:]:
                    unit['per_sub_tab'][_sn2] = {'status': 'error',
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
                # 2026-10-07c (MI 5X): before recording a blind error, run the sibling-tab
                # round-trip to disambiguate "tap never took effect" from "target tab active
                # but its list is genuinely empty / slow to render" (dept-level 普通门诊 pages).
                _s4 = _verify_subtab_active(adb, s2, subname, prev_key)
                if _s4:
                    s2 = _s4
                    _rows4 = parse_nodes(s2)
                    _c4 = _cards_key(_rows4)
                    if _c4:
                        log('  subtab %r: round-trip re-settle found cards => collecting' % subname)
                        prev_key = _c4
                    elif '\u6682\u65e0\u53ef\u9884\u7ea6' in s2:
                        prev_key = ()
                    else:
                        log('  subtab %r: round-trip confirms target tab active with an EMPTY list '
                            '(no cards, no empty-state text) => verified-empty' % subname)
                        ss['empty_state'] = True
                        ss['empty_verified_via'] = 'roundtrip_empty_list'
                        prev_key = ()
                else:
                    # 2026-10-07c: fully-empty dept variant -- if the date bar is present and
                    # shows ALL days no-slot, the department has no bookable slots at all =>
                    # verified no_slot (bar is dept-level, independent of subtab ambiguity).
                    _b_items, _b_anom = parse_bar(s2, today)
                    if _b_items and all(not it['has_slot'] for it in _b_items):
                        log('  subtab %r: date bar all-no-slot (%d days) => verified no_slot' % (subname, len(_b_items)))
                        ss['status'] = 'no_slot'
                        ss['bar_all_no_slot'] = True
                        ss['cost_s'] = round(time.monotonic() - t1, 1)
                        unit['per_sub_tab'][subname] = ss
                        prev_key = ()
                        continue
                    ss['bar_days'] = len(_b_items) if _b_items else 0
                    ss['bar_has_cnt'] = sum(1 for _it in _b_items if _it['has_slot']) if _b_items else 0
                    try:
                        _dd = os.path.join(OUT_DIR, 'diag')
                        os.makedirs(_dd, exist_ok=True)
                        _ci = (unit.get('cond_idxs') or [0])[0]
                        _fn = '%s_c%d_%s.xml' % (unit.get('hospital_id'), _ci, dt.datetime.now().strftime('%H%M%S'))
                        with open(os.path.join(_dd, _fn), 'w', encoding='utf-8') as _fh:
                            _fh.write(s2)
                        ss['diag_dump'] = _fn
                    except Exception:
                        pass
                    ss['status'] = 'error'
                    ss['error_reason'] = ('no_doctor_cards(sub_tab_tap=%s: \u5207\u6362\u540e\u7b49\u5f85\u6e32\u67d3\u5e76\u8865\u70b9 1 \u6b21, \u4ecd\u65e0\u533b\u751f\u5361\u4e14\u65e0'
                                          '"\u6682\u65e0\u53ef\u9884\u7ea6"\u7a7a\u6001\u6587\u672c; \u7591\u5217\u8868\u672a\u6e32\u67d3\u5b8c\u6216\u7a7a\u6001\u7f3a\u6587\u672c)') % subname
                    ss['cost_s'] = round(time.monotonic() - t1, 1)
                    unit['per_sub_tab'][subname] = ss
                    prev_key = ()
                    continue
            if st == 'unchanged':
                if (pos is not None and pos.get('subtab') == subname
                        and not PS.active_name(s2)):
                    # 2026-05: pos.subtab 是"动作 ok 就写进去"的断言, 不能反过来当证据;
                    # 页面上有 selected 硬判据时必须以 selected 为准。
                    # S20: pos \u8bb0\u5f55\u8be5\u5b50 tab \u5df2\u6fc0\u6d3b => tap \u4e3a no-op, \u5361\u96c6\u4e0e prev \u76f8\u540c
                    # => \u6309\u5df2\u6fc0\u6d3b\u5904\u7406
                    log('  subtab %r unchanged \u4f46 pos.subtab \u8bb0\u5f55\u4e00\u81f4 => \u6309\u5df2\u6fc0\u6d3b\u5904\u7406' % subname)
                    prev_key = _cards_key(parse_nodes(s2))
                else:
                    # 2026-10-07c (MI 5X): 'unchanged' with a non-empty prev is ambiguous between
                    # "tap failed" and "target tab was ALREADY active at L2 landing" (the app
                    # defaults to the content-bearing tab; tapping it is a no-op). Round-trip
                    # probe: if a sibling tab responds (card set changes), taps work => target
                    # was already active => tap back and collect. Otherwise keep the honest error.
                    _s4 = _verify_subtab_active(adb, s2, subname, prev_key)
                    if _s4:
                        s2 = _s4
                        prev_key = _cards_key(parse_nodes(s2))
                        log('  subtab %r unchanged: round-trip proves target tab already active => collecting' % subname)
                    else:
                        # 2026-10-07c: fully-empty dept variant -- if the date bar is present and
                        # shows ALL days no-slot, the department has no bookable slots at all =>
                        # verified no_slot (bar is dept-level, independent of subtab ambiguity).
                        _b_items, _b_anom = parse_bar(s2, today)
                        if _b_items and all(not it['has_slot'] for it in _b_items):
                            log('  subtab %r: date bar all-no-slot (%d days) => verified no_slot' % (subname, len(_b_items)))
                            ss['status'] = 'no_slot'
                            ss['bar_all_no_slot'] = True
                            ss['cost_s'] = round(time.monotonic() - t1, 1)
                            unit['per_sub_tab'][subname] = ss
                            prev_key = ()
                            continue
                        ss['bar_days'] = len(_b_items) if _b_items else 0
                        ss['bar_has_cnt'] = sum(1 for _it in _b_items if _it['has_slot']) if _b_items else 0
                        try:
                            _dd = os.path.join(OUT_DIR, 'diag')
                            os.makedirs(_dd, exist_ok=True)
                            _ci = (unit.get('cond_idxs') or [0])[0]
                            _fn = '%s_c%d_%s.xml' % (unit.get('hospital_id'), _ci, dt.datetime.now().strftime('%H%M%S'))
                            with open(os.path.join(_dd, _fn), 'w', encoding='utf-8') as _fh:
                                _fh.write(s2)
                            ss['diag_dump'] = _fn
                        except Exception:
                            pass
                        ss['status'] = 'error'
                        ss['error_reason'] = ('unknown_page_variant(sub_tab_tap=%s: \u70b9 2 \u6b21\u540e\u5361\u96c6\u4ecd\u4e0e\u4e0a\u4e00\u5b50 tab \u76f8\u540c\u4e14\u65e0\u7a7a\u6001\u6587\u672c, '
                                              '\u65e0\u6cd5\u786e\u8ba4\u5207\u6362\u751f\u6548, \u4e0d\u91c7)') % subname
                        ss['cost_s'] = round(time.monotonic() - t1, 1)
                        unit['per_sub_tab'][subname] = ss
                        prev_key = ()
                        continue
        if subname != '(单列,无子tab)':
            if pos is not None:
                pos['subtab'] = subname
                pos['level'] = 'L2'
                pos['ts'] = dt.datetime.now().isoformat(timespec='seconds')
                save_nav_state(pos)
        # S13 (S11 H3): 首屏单次 dump 漏卡(零高裁剪 + 懒加载, 实测 20→21) => 滚动到底,
        # 末尾连续 2 次 dump 稳定才停(ADB 真值 §7.3 纪律); 枚举后 s2 停在列表尾部,
        # 逐卡 tap 前再由 _ensure_card_visible 滚动+重 dump 定位
        s2, cards = _enumerate_l2_cards(adb, s2)
        if not cards:
            # 2026-10-14 (MI 5X 0043d38b0504 six=0 根因之一): 子 tab 刚点完/页面还在跳转时
            # page_kind 还不是 L2 => _enumerate_l2_cards 直接返回 0 张。老机器 WebView 渲染慢,
            # 等 1.6s 再枚举一次(命中即继续走完整的 bookable/tap 流程)。
            time.sleep(1.6)
            _s2b = adb.dump()
            if page_kind(_s2b) in ('L2', 'L2b'):
                _s2b, _cards2 = _enumerate_l2_cards(adb, _s2b)
                if _cards2:
                    log('  首轮枚举 0 张 => 重枚举命中 %d 张(渲染竞态)' % len(_cards2))
                    s2, cards = _s2b, _cards2
        rows = parse_nodes(s2)
        ss['empty_state'] = ('暂无可预约' in s2)
        prev_key = _cards_key(rows)
        if pos is not None and subname != '(单列,无子tab)':
            pos['cards_key'] = list(prev_key)
            save_nav_state(pos)
        ss['doctor_count'] = len(cards)
        ss['doctors'] = [_expected_doc_name(c['name_node'][0]) for c in cards]
        bcards = [c for c in cards if c['btn'][0] == '去预约']
        ss['bookable_count'] = len(bcards)
        ss['bookable_doctors'] = [_expected_doc_name(c['name_node'][0]) for c in bcards]
        ss['full_doctors'] = [_expected_doc_name(c['name_node'][0]) for c in cards if c['btn'][0] == '已约满']
        # 2026-10-14 (MI 5X 0043d38b0504 取证): 枚举到 10 张卡却 bookable_count=0 且 full_doctors=[]
        # —— 说明按钮文本既不是「去预约」也不是「已约满」(或卡身/按钮配对错位)。这种情况以前
        # 只留一个 0，无法判断是"真的全约满"还是"解析错位"。现在落盘 L2NOSTATE 现场 + 按钮文本直方图。
        if cards and not bcards and not ss['full_doctors']:
            _btn_hist = {}
            for c in cards:
                _btn_hist[c['btn'][0]] = _btn_hist.get(c['btn'][0], 0) + 1
            log('  WARN: cards=%d 但无「去预约」也无「已约满」按钮 => 按钮文本=%s' % (len(cards), _btn_hist))
            try:
                _ev = six_fields.save_raw_evidence(
                    s2, OUT_DIR, unit.get('hospital_id'), '%s_%s' % (dept, subname), 'L2NOSTATE',
                    dt.datetime.now().strftime('%Y%m%dT%H%M%S'))
                log('  L2 无状态按钮取证: %s (cards=%d)' % (_ev, len(cards)))
            except Exception as _e:
                log('  L2 无状态按钮取证失败: %s' % str(_e)[:100])
        # 2026-10-14 (MI 5X 0043d38b0504 取证): 卡身枚举到 10 张但 0 张「去预约」, 且 doctors 里
        # 同一个名字重复 7 次 => 分不清"真的全约满"还是"老机器 WebView 列表还没渲染完"。
        # 只在**重名指纹**出现时落盘 L2 现场(真约满的列表不会大量重名, 不会刷屏)。
        if cards and ss['bookable_count'] == 0 and len(set(ss['doctors'])) < len(cards):
            try:
                _ev = six_fields.save_raw_evidence(
                    s2, OUT_DIR, unit.get('hospital_id'), '%s_%s' % (dept, subname), 'L2DUPNAMES',
                    dt.datetime.now().strftime('%Y%m%dT%H%M%S'))
                log('  L2 重名指纹取证: %s (cards=%d uniq=%d)'
                    % (_ev, len(cards), len(set(ss['doctors']))))
            except Exception as _e:
                log('  L2 重名指纹取证失败: %s' % str(_e)[:100])
        if cards or ss['empty_state']:
            any_sub_info = True
        if not cards and not ss['empty_state']:
            # H5 空态(nodata 图 + 「暂无可预约号源」)比子 tab 切换渲染慢(实测 476cf991 专家门诊:
            # 切 tab 后首次 dump 只有日期条, 1.5s 后才出现空态节点) => 判 error 前再等一次重 dump。
            time.sleep(1.6)
            s2 = adb.dump()
            rows = parse_nodes(s2)
            cards = _find_doc_cards(rows)
            ss['empty_state'] = ('暂无可预约' in s2)
            prev_key = _cards_key(rows)
            ss['doctor_count'] = len(cards)
            ss['doctors'] = [_expected_doc_name(c['name_node'][0]) for c in cards]
            if cards:
                # 2026-10-14 (MI 5X six=0 根因之二): 这里补 dump 找到了卡, 但 bcards/bookable_*
                # 还停留在首轮的空值 => tap_cards=bcards 恒空 => 一张医生主页都没点开 => six=0
                # (被误记成"该条件无号")。补 dump 后必须重新计数。
                bcards = [c for c in cards if c['btn'][0] == '去预约']
                ss['bookable_count'] = len(bcards)
                ss['bookable_doctors'] = [_expected_doc_name(c['name_node'][0]) for c in bcards]
                ss['full_doctors'] = [_expected_doc_name(c['name_node'][0])
                                      for c in cards if c['btn'][0] == '已约满']
                log('  补 dump 后重新计数: cards=%d bookable=%d full=%d'
                    % (len(cards), len(bcards), len(ss['full_doctors'])))
            if cards or ss['empty_state']:
                any_sub_info = True
        if not cards and not ss['empty_state']:
            _dept = _find_dept_cards(rows)
            if _dept:
                # 无按钮科室卡变体(2026-10-09): 该科室普通门诊渲染科室卡(简介：××普通门诊),
                # 整页无 去预约/已约满/暂无可预约, 点卡身实测不跳转 => 当下不可约。
                # 诚实判 no_slot; 识别成功即止, 不再补点/往返探测(旧版该分支前的
                # no_cards/unchanged 分支因空指纹误判"点击未生效", 白烧 46-113s/单元)。
                _bump(ss['variants'], 'dept_card_no_button')
                ss['status'] = 'no_slot'
                ss['dept_card_no_button'] = True
                ss['dept_card_text'] = _dept[0]
                ss['error_reason'] = 'dept_card_no_button(科室卡无去预约/已约满/暂无可预约; 当下不可约)'
                ss['cost_s'] = round(time.monotonic() - t1, 1)
                unit['per_sub_tab'][subname] = ss
                continue
            ss['status'] = 'error'
            ss['error_reason'] = 'no_doctor_cards(无医生卡且无"暂无可预约"空态文本; 页面变体未知)'
            ss['cost_s'] = round(time.monotonic() - t1, 1)
            unit['per_sub_tab'][subname] = ss
            continue

        # S20: doctor 收窄 — 只点名称匹配的卡(部分匹配); 无匹配 => 诚实 0 行(不猜)
        tap_cards = bcards
        if cond and cond.get('doctor'):
            tap_cards = [c for c in bcards
                         if cond['doctor'] in _expected_doc_name(c['name_node'][0])]
            ss['doctor_filter'] = cond['doctor']
            ss['doctor_filter_matched'] = len(tap_cards)
            if not tap_cards:
                ss['status'] = 'error'
                ss['error_reason'] = ('doctor_not_found(%s 不在该子 tab %d 位可预约医生: %s; 诚实 0 行)'
                                      % (cond['doctor'], len(bcards), ', '.join(ss['bookable_doctors'][:10])))
                ss['cost_s'] = round(time.monotonic() - t1, 1)
                unit['per_sub_tab'][subname] = ss
                continue
        _mm_dumps = [0]   # 2026-10-14: doctor_mismatch 证据落盘配额(防爆盘)
        for di, card in enumerate(tap_cards):
            # S13 (S11 H3 核心修复): tap 前先把卡带入安全可视区 + 重 dump 取新坐标;
            # 绝不对零高裁剪节点直接 tap(其中心 (548,2347) 落屏幕底边, 不命中任何卡
            # => 旧代码记 card_tap_no_navigate 的根因; S11 C2/C3 回归断言见 _ensure_card_visible)
            nm = _ensure_card_visible(nav.adb, card['name_node'][0])
            if nm is None:
                _bump(ss['variants'], 'card_tap_no_navigate')
                log('  doc#%d %s: <=8 步滚动仍未带入可视区 => card_tap_no_navigate(不猜, 不 tap 底边)'
                    % (di, card['name_node'][0]))
                continue
            _step('点医生: %s' % card['name_node'][0], 'verify')
            _tx, _ty = (nm[2] + nm[4]) // 2, (nm[3] + nm[5]) // 2
            nav.adb.tap(_tx, _ty)   # 点卡身(个人版), 不点「去预约」按钮
            # 2026-10-14: 打出实际落点/节点框 —— MI 5X 上 21/21 doctor_mismatch, 需要区分
            # 「tap 落到了去预约(=>科室组版)」与「页头解析错」两种成因。
            log('  doc#%d %s: tap=(%d,%d) 姓名节点=[%d,%d][%d,%d]'
                % (di, nm[0], _tx, _ty, nm[2], nm[3], nm[4], nm[5]))
            _step('等待医生主页(六字段)…', 'verify')
            # L3 (2026-10-09): 有界轮询替代固定 3.0s — 命中 doc/six/blocked 立即返回;
            # 最坏 ~3.0s + 1 dump(不慢于旧 time.sleep(3.0)+dump), 成功路径更快。关闭 L3 即回旧固定 sleep。
            if L3_BOUNDED_POLL:
                # ②(2026-05): 期望「离开 L2, 落到医生主页」; 3s 内没离开就诚实返回 L2,
                # 交给下面的「补点 1 次」分支(旧版 _wait_navigate 只看页面变没变)。
                ok3, o3, s3, why3 = act(nav.adb,
                                        {'level': ('six', 'doc', 'blocked', 'login', 'auth'),
                                         'doctor': _expected_doc_name(nm[0])},
                                        state='doctor_landing', what='点医生:%s' % nm[0],
                                        timeout=3.0, settle=0.6)
                k3 = o3.level if ok3 else (o3.level or page_kind(s3))
                if k3 in ('six', 'doc'):
                    # ⑤: 落地判定之后才播报, 且播报**实测科室**(页面身份位), 不把意图当结果
                    _step('已进入医生主页 · %s' % (o3.dept or dept), 'ok')
            else:
                time.sleep(3.0)
                s3 = nav.adb.dump()
                k3 = page_kind(s3)
            if k3 != 'blocked':
                log('  doc#%d %s: 落地页 kind=%s' % (di, nm[0], k3))
            if k3 == 'blocked':
                nav.adb.back(); time.sleep(1.5)
                nav.adb.back(); time.sleep(1.0)
                unit['cost_s'] = round(time.monotonic() - t0, 1)
                return unit, True
            if k3 in ('L2', 'L2b'):
                # S9: 卡身点击未跳转 => 新鲜 dump 重定位该卡姓名节点补点 1 次(同一操作最多 2 次)。
                # 成因: 上一医生主页 back 回 L2 后渲染位置漂移, 子 tab 切换时的缓存坐标已偏
                # (S7 实测 fling 惯性); 补点仍不跳转才记 variant, 绝不点「去预约」兜底(组版风险)
                # L2 (2026-10-09): 首 dump(s3) 已确认仍在 L2, 直接用它重定位姓名节点补点, 省一次重复 dump;
                # 关闭 L2 即回到旧的「先再 dump 一次再重定位」。
                if not L2_DROP_REDUNDANT_DUMP:
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
                # 同上: 页头姓名被图片化时 find_page_doctor 可能直接 None(页头 CJK 候选不唯一)。
                # 个人版指纹齐全 + 页内无该姓名 + 图片化姓名指纹 => 用 L2 卡姓名继续(记 variant)。
                _exp0 = _expected_doc_name(nm[0])
                if (_exp0 not in s3 and '医生主页' in s3
                        and ('医生简介' in s3 or '预约须知' in s3)
                        and _doc_name_rendered_as_image(s3)):
                    _bump(ss['variants'], 'doc_name_from_nav(image_header)')
                    log('  doc#%d %s: 页头无姓名节点(姓名图片化) => 用 L2 卡姓名, 溯源 nav_context'
                        % (di, nm[0]))
                    pd = (_exp0, nm[0])
                else:
                    _bump(ss['variants'], 'group_missing_doctor')
                    nav.adb.back(); time.sleep(1.8)
                    continue
            if _doc_name_only(pd[0]) != _expected_doc_name(nm[0]):
                # 2026-10-14 (MI 5X six=0 真因): 旧 WebView 把页头「姓名+职称」渲染成 <img>,
                # 页内 0 次出现医生姓名 => 页内身份核验不可能成功; 但页面确实是个人版(标题「医生主页」
                # + 医生简介/预约须知 + 日期条 + 去预约行)。故四重前置: 页内无该姓名 + 页头行属于
                # 本医院/本科室 + 个人版指纹齐全 + 页头确有"姓名图片化"指纹, 才用 L2 卡姓名作为
                # 就诊医生(six_fields 以 nav_context 溯源, 不谎称页内核实); 缺一仍按 mismatch 拒绝。
                _exp = _expected_doc_name(nm[0])
                _hdr = pd[1] or ''
                _hdr_ok = (dept and dept in _hdr) or \
                          (unit.get('hospital') and unit['hospital'] in _hdr)
                if (_exp not in s3 and _hdr_ok and '医生主页' in s3
                        and ('医生简介' in s3 or '预约须知' in s3)
                        and _doc_name_rendered_as_image(s3)):
                    _bump(ss['variants'], 'doc_name_from_nav(image_header)')
                    log('  doc#%d %s: 页头姓名是图片(旧 WebView), 页内无姓名可核验 '
                        '(页头=%r) => 用 L2 卡姓名, 溯源 nav_context' % (di, nm[0], pd[1][:24]))
                    pd = (_exp, nm[0])
                else:
                    _bump(ss['variants'], 'doctor_mismatch:%s' % pd[1][:20])
                    log('  doc#%d %s: 页头医生=%r != 期望 %r (tap 落点见上一行)'
                        % (di, nm[0], pd[1][:24], _exp))
                    trace_ev('doctor_home', nm[0], {'level': 'six', 'doctor': _exp},
                             obs_of(s3), False, dump=s3, extra={'page_header': pd[1][:40]})
                    if _mm_dumps[0] < 3:
                        _mm_dumps[0] += 1
                        try:
                            six_fields.save_raw_evidence(
                                s3, OUT_DIR, hid, dept,
                                'MISMATCH%d_%s' % (_mm_dumps[0], nm[0]),
                                nav.adb.device_date().isoformat(timespec='seconds'))
                        except Exception:
                            pass
                    nav.adb.back(); time.sleep(1.8)
                    continue
            # 证据链: 医生主页身份已核对(期望=点进去的那张卡, 实测=页头)
            trace_ev('doctor_home', nm[0], {'level': 'six', 'doctor': _expected_doc_name(nm[0])},
                     obs_of(s3), True, dump=s3, extra={'page_header': (pd[1] or '')[:40]})
            # S20 需求3: 确认到达医生主页 => 记录位置
            if pos is not None:
                pos['level'] = 'doc'
                pos['doctor'] = _expected_doc_name(nm[0])
                pos['date'] = None
                pos['ts'] = dt.datetime.now().isoformat(timespec='seconds')
                save_nav_state(pos)
            # S20: date 收窄 — 点医生主页日期条格切到指定日(null = 不限日期视图, 旧行为)
            if cond and cond.get('date_md'):
                dst, s3 = select_date_doc_home(adb, s3, cond['date_md'])
                if dst != 'ok':
                    _bump(ss['variants'], 'date_%s' % dst)
                    log('  doc#%d %s: date %s 切换失败(%s) => 0 行(诚实)' % (di, nm[0], cond['date'], dst))
                    nav.adb.back(); time.sleep(1.8)
                    continue
                if pos is not None:
                    pos['date'] = cond['date_md']
                    save_nav_state(pos)
            dh = parse_doc_home(s3, today)
            rows_before = dh['slot_rows_shown']
            # S11 §4.2 (S13 附带修复): 渲染时序歧义 — 首 dump 0 行且无任何展开按钮(含收起态)
            # 视为未渲染完: 等 2.5s 重 dump 一次再定"无排班"(143634 轮 3 例该状态,
            # 16:15 ADB 侧同页 10 行; 首 dump 缺行+缺按钮不得直接记 no_slot_rows_on_home)
            if rows_before == 0 and find_expand_state(parse_nodes(s3)) is None and not L1_FAST_NO_SLOT_EXIT:
                # L1 (2026-10-09): 提速杠杆 — 此前已由 _wait_navigate(L3)/8x轮询确认到达 six 页,
                # 且 6 个有号医生都已在「有 expand 按钮」分支被记录, 剩下的 0 行 + 无按钮 = 折叠态 0 行 = 无号,
                # 再等 2.5s + 二次 dump 是纯加时间 => 省去(等价于旧行为, 但少一次 sleep+dump)。
                # 关闭 L1 即回到旧逻辑(等 2.5s + 二次 dump)。
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
            # 2026-10-28 修复：端上 dump 是 in-memory XML (devbridge.dump()), 没有落盘 _last_dump.xml;
            # 直接传 s3 (当前 XML 字符串) 给 save_raw_evidence，让它直接写 raw/ 目录。
            raw_rel = six_fields.save_raw_evidence(s3, OUT_DIR,
                                                    hid, dept, pd[0], dev_ts + '_dept')
            # S13: 真值交换格式 extract.file 用 workspace 相对路径(与安卓小能手 'tmp/...' 同基)
            # S19(V2): raw_rel 是相对 slots/ 的 'out/raw/...', 直接 join 'slots/out' 会出 out/out/raw;
            # 统一用 basename 重组为 workspace 相对路径。
            raw_ws = os.path.join(os.path.relpath(OUT_DIR, WS), six_fields.RAW_DIRNAME,
                                  os.path.basename(raw_rel)).replace('\\', '/')
            # S20: time_range 后置过滤(按条件组成员逐个判) + date 安全过滤(日视图应只含该日行)
            n_tr_drop = 0
            for sl in dh['slots']:
                if not sl.get('bookable'):
                    continue
                if cond and cond.get('date') and sl.get('date') != cond['date']:
                    _bump(ss['variants'], 'row_date_mismatch')
                    continue
                incl = []
                if cond_idxs is not None:
                    for _ci, _ctr in zip(cond_idxs, member_trs):
                        if slot_in_range(sl['row_text'], _ctr):
                            incl.append(_ci)
                        else:
                            n_tr_drop += 1
                    if not incl:
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
                if rec_buffer is not None:
                    # S20: 缓存合并 — 同行(同医生同时段) 跨条件组重叠(不限日期 vs 指定日期)
                    # => cond_idxs 取并集, 末次统一写出(不重复行)
                    if key in rec_buffer:
                        rec_buffer[key][1].update(incl)
                    else:
                        rec_buffer[key] = [rec, set(incl)]
                else:
                    if incl:
                        rec['cond_idxs'] = incl
                        rec['conditions'] = [cond_raws[j] for j in incl]
                    six_seen.add(key)
                    jsonl_fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
                    jsonl_fh.flush()
                ss['six_records'] += 1
                n_six += 1
            if n_tr_drop:
                ss['time_range_filtered_out'] = n_tr_drop
            # 回 L2: 校验; 丢了 => 本单元中断(已采记录保留), 恢复 L1 供下一单元
            nav.adb.back(); time.sleep(1.5)
            kback = None
            for _b in range(3):
                kback = page_kind(nav.adb.dump())
                if kback in ('L2', 'L2b'):
                    break
                nav.adb.back(); time.sleep(1.2)
            if pos is not None and kback in ('L2', 'L2b'):
                pos['level'] = 'L2'
                pos['doctor'] = None
                pos['date'] = None
                pos['ts'] = dt.datetime.now().isoformat(timespec='seconds')
                save_nav_state(pos)
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
    elif any(v.get('bar_all_no_slot') for v in unit['per_sub_tab'].values()):
        # 2026-10-07c: dept-level date bar verified all-no-slot => verified no_slot unit
        unit['status'] = 'no_slot'
    elif any(v.get('dept_card_no_button') for v in unit['per_sub_tab'].values()):
        # 2026-10-09: 无按钮科室卡变体 => 该科室普通门诊当下不可约 => verified no_slot unit
        unit['status'] = 'no_slot'
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


def _checkpoint(run_sum, rec_buffer, jf, partial_p, rs_p, idx2raw):
    """增量落盘(2026-10-05): 每组完成后 flush 合并记录快照 + run 摘要快照,
    超时/硬杀不丢已完成组。正常跑完时终版仍写 <ts>_all.jsonl, partial 文件随后删除。"""
    try:
        if jf is not None:
            jf.flush()
        tmp = partial_p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as pf:
            for _key, (_rec, _idxs) in rec_buffer.items():
                _r2 = dict(_rec)
                _i2 = sorted(_idxs)
                _r2['cond_idxs'] = _i2
                _r2['conditions'] = [idx2raw.get(i, i) for i in _i2]
                pf.write(json.dumps(_r2, ensure_ascii=False) + '\n')
        os.replace(tmp, partial_p)
        tmp2 = rs_p + '.tmp'
        json.dump(run_sum, open(tmp2, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        os.replace(tmp2, rs_p)
    except Exception as e:
        log('  checkpoint 增量落盘失败(忽略, 不影响主流程): %s' % str(e)[:100])


# ================= probe 模式 (2026-10-06): 号源可得性简要探测 =================
# 目的 = 只判「有无号源 / 什么类型(专家·普通·专病) / 什么日期(距今天数)」=> 排名信号,
# **不逐医生点进主页**(那是深核阶段 --mode full 的事; full 每医生 ~10s 是 9dc8 超时根因)。
# L2 科室页一次 dump 即得全部信号:
#   1) 「去预约」按钮(可约医生卡) => 强信号 status=ok; 首屏未见按钮才有限上下滑(有界)找;
#   2) 「暂无可预约号源」空态 => 强信号 status=no_slot;
#   3) 日期条「有号/无号」(部门级聚合, S3 坑1 可陈旧) => 弱信号 status=bar_hint。
# 日期条屏外项也在 XML 中(bounds=[0,0][0,0], 文档序) => 一次 dump 读全, **无需横滑**。
# ================= 提速杠杆开关 (2026-10-09 设备侧基线 SH-084 专家门诊 215s) =================
# 三个杠杆各自独立开关、各自可回退: 真值=启用, 假值=原逻辑。
# 正确性不退化保障: 三个杠杆都只改「等多久 / 是否再多 dump 一次」,
# 不改「从哪个页面取 six 字段」— six_records 仍从 six 页的时段行取,
# 有号/无号判定仍以 six 页 rows_before 为准, 且仅在已确认到达 six 页后才生效。
#   L1_FAST_NO_SLOT_EXIT : six 页 rows_before==0 且无展开按钮 → 省去 2.5s+二次 dump 再校验, 直接 back
#   L2_DROP_REDUNDANT_DUMP : 补点重试路径若首 dump 已落 doc/six/blocked 定态 → 省去补点前的重复 dump
#   L3_BOUNDED_POLL      : tap 卡身后用有界轮询(6x1s, 指纹稳定即止) 替代 3.0s 固定 sleep
L1_FAST_NO_SLOT_EXIT = True
L2_DROP_REDUNDANT_DUMP = True
L3_BOUNDED_POLL = True

# ================= A11Y 自愈/体检开关 (2026-10-10 nav_fail 根因轮, 与 PC 同一套语义) =================
# 背景: `pm clear`/重装 / HyperOS 甩后台(GAME_GOTO_BACKGROUND) / 通知栏抢焦点
#       => 读屏无障碍服务 unbound 或随申办窗口为空 => devbridge.dump() 空 => page_kind='other'
#       => ensure_444 6 轮全败 => nav_fail(goto_unit=fail) 含糊报错, 且白烧时间。
# 判据可观测(窗口空/kind=other), 有界重试(<=3), 失败给精确 reason; 平台差异分别处理:
#   * 服务 Bound 判定  -> devbridge.is_ready()(PyBridge/SsAccessibilityService)
#   * 拉随申办回前台    -> devbridge.launch()(NEW_TASK|CLEAR_TOP; 端上 am force-stop 是 no-op)
#   * 通知栏/后台抢占   -> 直接 launch() 重拉即可(端上**不按 HOME**: 见 _search_channel 注释,
#                          HOME 会触发 HyperOS "Abort background activity starts")
# 回退: A11Y_PREFLIGHT=False / A11Y_SELFHEAL=False (或 hh_levers.json {"A11Y":0} 同时关闭)。
A11Y_PREFLIGHT = True
A11Y_SELFHEAL = True
# NO_RESUME (2026-10-10 B 轮收尾): 强制重跑开关 —— 置 1 时忽略 condition_runs 里
# 已 verified ok/no_slot 的 done 标记, 全部单元真跑(用于 A/B 提速/正向自证复现)。
# 与 L1/L2/L3/A11Y 同一风格: 默认值 + apply_lever_overrides 读 hh_levers.json 的 "NO_RESUME"
# 键覆盖(同名环境变量 NO_RESUME=1 亦可); 置 0/不写 => 保持断点续跑(默认行为)。
NO_RESUME = False
A11Y_SERVICE = 'com.hospitalhelper.controller/.SsAccessibilityService'
A11Y_RECOVER_MAX = 3       # 自愈有界重试上限(被甩后台/通知栏抢占)
A11Y_RECOVER_GAP_S = 2.0   # 每次自愈尝试后的等待上限
_A11Y_RECOVERY = {'attempts': 0, 'recovered': None, 'reason': None}  # 留痕(供 unit['nav_recovery'])


def _a11y_mark(attempts, recovered, reason):
    _A11Y_RECOVERY['attempts'] = attempts
    _A11Y_RECOVERY['recovered'] = recovered
    _A11Y_RECOVERY['reason'] = reason


def a11y_bound(adb):
    """读屏无障碍服务是否 Bound(端上走 devbridge.is_ready; 桌面/异常 => True 不误伤)。"""
    try:
        import devbridge as _db
        return bool(_db.is_ready())
    except Exception:
        return True


def _a11y_recover(adb):
    """自愈: 把随申办重新拉回**前台**。返回 (status, s)。
    status = 'ok' 恢复成功(随申办已在前景, 外层 ensure_444 再走 444 导航);
             'a11y_lost_midrun' 服务中途掉线(立即中止);
             'a11y_bound_but_recover_failed' 仍 Bound 但有界重试后随申办仍无窗口。
    判据可观测: devbridge 只读随申办窗口, 拉回后 dump 非空 = 随申办已在前台(不空转到 444)。
    端上: devbridge.launch()=CLEAR_TOP 重拉, 不按 HOME(HyperOS 会 Abort 后台启动, 见 _search_channel 注释)。"""
    if not a11y_bound(adb):
        log('  A11Y self-heal: 读屏服务已掉线(unbound) => 立即中止本单元 (a11y_lost_midrun)')
        _a11y_mark(_A11Y_RECOVERY['attempts'] + 1, False, 'a11y_lost_midrun')
        return 'a11y_lost_midrun', ''
    last = ''
    for i in range(1, A11Y_RECOVER_MAX + 1):
        log('  A11Y self-heal %d/%d: 拉随申办回前台(devbridge.launch CLEAR_TOP, 不按 HOME)' % (i, A11Y_RECOVER_MAX))
        try:
            import devbridge as _db
            _db.launch()
            time.sleep(6)
            last = adb.dump() or ''
        except Exception as e:
            log('  A11Y self-heal %d: 异常 %s' % (i, str(e)[:80]))
        win_alive = bool((last or '').strip())
        log('  A11Y self-heal %d: window_alive=%s kind=%s' % (i, win_alive, page_kind(last)))
        if win_alive:
            _a11y_mark(i, True, 'recovered')
            log('  A11Y self-heal: 第 %d 次尝试恢复成功 (nav_recovery={attempts:%d, recovered:true})' % (i, i))
            return 'ok', last
        k = page_kind(last)
        if k == 'blocked':
            _a11y_mark(i, False, 'blocked')
            return 'blocked', last
        time.sleep(A11Y_RECOVER_GAP_S)
    _a11y_mark(A11Y_RECOVER_MAX, False, 'a11y_bound_but_recover_failed')
    log('  A11Y self-heal %d 次后随申办仍无窗口 => a11y_bound_but_recover_failed' % A11Y_RECOVER_MAX)
    return 'a11y_bound_but_recover_failed', last


def _a11y_reason(status):
    """把自愈/体检状态映射为精确 error_reason(不再笼统 goto_unit=fail)。"""
    if status == 'a11y_lost_midrun':
        return 'a11y_lost_midrun(导航中读屏服务掉线; 需重新 enable 无障碍后重跑)'
    if status == 'a11y_bound_but_recover_failed':
        return 'a11y_bound_but_recover_failed(读屏服务在, 但有界 %d 次重拉随申办仍无窗口; 检查 HyperOS 后台限制/通知栏抢占)' % A11Y_RECOVER_MAX
    return status


def apply_lever_overrides():
    """杠杆运行时开关(2026-10-09 提速轮): 依次尝试读
    $HH_LEVERS / ~/.hh_levers.json / <DATA_DATA>/data/com.hospitalhelper.controller/files/hh_levers.json
    里的 {"L1":0,"L2":1,"L3":0} 覆盖上面三个默认值(写 globals)。
    作用: (a) 满足「三杠杆各自独立可回退」; (b) A/B 测量时逐杠杆开关, 无需改代码/重打包。
    只改这三个布尔, 不动任何判定/取数逻辑。文件不存在 => 用默认值(全开)。"""
    import json as _json, os as _os
    cands = []
    _env = _os.environ.get('HH_LEVERS')
    if _env:
        cands.append(_env)
    cands.append('/data/user/0/com.hospitalhelper.controller/files/hh_levers.json')
    cands.append(_os.path.join(_os.path.expanduser('~'), '.hh_levers.json'))
    _dd = _os.environ.get('DATA_DATA') or _os.environ.get('ANDROID_DATA')
    if _dd:
        cands.append(_os.path.join(_dd, 'com.hospitalhelper.controller/files/hh_levers.json'))
    for p in cands:
        try:
            with open(p, 'r', encoding='utf-8') as _f:
                d = _json.load(_f)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        g = globals()
        if d.get('L1') is not None:
            g['L1_FAST_NO_SLOT_EXIT'] = bool(d['L1'])
        if d.get('L2') is not None:
            g['L2_DROP_REDUNDANT_DUMP'] = bool(d['L2'])
        if d.get('L3') is not None:
            g['L3_BOUNDED_POLL'] = bool(d['L3'])
        if d.get('A11Y') is not None:
            g['A11Y_PREFLIGHT'] = bool(d['A11Y'])
            g['A11Y_SELFHEAL'] = bool(d['A11Y'])
        if d.get('NO_RESUME') is not None:
            g['NO_RESUME'] = bool(d['NO_RESUME'])
        # 环境变量同名兜底(优先级最高, 便于 `NO_RESUME=1 python ...` 单次使用)
        _nr = _os.environ.get('NO_RESUME')
        if _nr is not None:
            g['NO_RESUME'] = _nr not in ('0', 'false', 'False', 'no', '')
        try:
            log('[levers] override from %s: L1=%s L2=%s L3=%s A11Y=%s/%s NO_RESUME=%s'
                % (p, g['L1_FAST_NO_SLOT_EXIT'], g['L2_DROP_REDUNDANT_DUMP'], g['L3_BOUNDED_POLL'],
                   g['A11Y_PREFLIGHT'], g['A11Y_SELFHEAL'], g['NO_RESUME']))
        except Exception:
            pass
        return
    # 无杠杆文件: 仅环境变量仍生效
    try:
        _nr = _os.environ.get('NO_RESUME')
        if _nr is not None:
            globals()['NO_RESUME'] = _nr not in ('0', 'false', 'False', 'no', '')
            log('[levers] env NO_RESUME=%s (no levers file)' % globals()['NO_RESUME'])
    except Exception:
        pass


apply_lever_overrides()


def _wait_navigate(adb, initial_s='', timeout=6.0, interval=1.0):
    """L3: 有界轮询替代固定 sleep —— 点卡身后等页面进入目标态(doc/six/blocked)。
    语义等价 3.0s 固定等待但更快且更稳(慢机器 3.0s 可能不够): 每 interval 重新判定,
    命中 doc/six/blocked 立即返回该 XML; 超时则回退到一次普通 dump(= 原固定 sleep 的落点)。
    只用于「tap 卡身后等导航」, 不用于子 tab 切换等已有独立轮询的路径。
    时间上界: 最坏 ~timeout + 1 次 dump(与旧 time.sleep(3.0)+dump 同量级, 不慢于旧逻辑);
    命中 doc/six/blocked 立即返回该 dump(常见成功路径更快)。
    initial_s 非空时先判定(省一次 dump): 已在定态立即返回。"""
    s = initial_s
    if s:
        k = page_kind(s)
        if k in ('doc', 'six', 'blocked'):
            return s
    deadline = time.monotonic() + timeout
    while True:
        if s:
            k = page_kind(s)
            if k in ('doc', 'six', 'blocked'):
                return s
        if time.monotonic() >= deadline:
            return s or adb.dump()
        time.sleep(interval)
        s = adb.dump()


PROBE_MAX_SWIPES = 4
PROBE_SETTLE = 1.0   # 2026-10-07: 1.6->1.0


def probe_scan_subtab(adb, s, max_swipes=PROBE_MAX_SWIPES, settle=PROBE_SETTLE):
    """子 tab 医生列表快扫: 统计 去预约/已约满/医生卡; 未见「去预约」才有限下滑(有界, 找到即停)。
    返回 (s_last, bookable, full, doc_count, empty_state, scrolls, exhausted)。"""
    def _stat(sx):
        rows = parse_nodes(sx)
        bk = 0
        fl = 0
        for (t, c, *_x) in rows:
            if t == '去预约':
                bk += 1
            elif t == '已约满':
                fl += 1
        cards = _find_doc_cards(rows)
        return bk, fl, len(cards), ('暂无可预约' in sx), tuple(c['name_node'][0] for c in cards)

    bk, fl, nc, empty, key = _stat(s)
    scrolls = 0
    exhausted = False
    if bk == 0 and not empty:
        prev_key = None
        stable = 0
        for _i in range(max_swipes):
            adb.swipe(L2_LIST_X, Y(0.795), L2_LIST_X, Y(0.46), 350)   # 内容上移: 看下方(2026-10-14 屏高比例)
            time.sleep(settle)
            s = adb.dump()
            if page_kind(s) not in ('L2', 'L2b'):
                break
            scrolls += 1
            bk, fl, nc, empty, key = _stat(s)
            if bk > 0 or empty:
                break
            stable = stable + 1 if key == prev_key else 0
            prev_key = key
            if stable >= 2:
                exhausted = True   # 列表到底(连续 2 次卡集不变) => 无按钮可找
                break
    return s, bk, fl, nc, empty, scrolls, exhausted


def probe_one_dept_unit(nav, adb, g, today, s_in):
    """probe 采一个 (tab,dept) 单元: L2 页一次 dump(日期条+子 tab) + 逐子 tab 快扫(不点医生)。
    返回 unit dict(一行写 <ts>_all.jsonl; 供 app/slots.py _parse_probe_runs 消费)。
    status: ok=有可约按钮 / no_slot=空态或日期条全无号 / bar_hint=日期条有号但未见按钮(弱信号, 深核阶段定夺)。"""
    t0 = time.monotonic()
    hid, hosp = g['hid'], g['hosp']
    tab, dept = g['tab'], g['dept']
    cond0 = g['members'][0][1]
    unit = {
        'mode': 'probe', 'hospital_id': hid, 'hospital': hosp, 'tab': tab, 'dept': dept,
        'condition': {k: cond0.get(k) for k in ('date', 'slot_type', 'time_range', 'doctor')},
        'cond_idxs': [mm[0] for mm in g['members']],
        'conditions': [mm[2] for mm in g['members']],
        'status': 'error', 'error_reason': None, 'interrupted': None,
        'bar': None, 'bar_days': None, 'bar_anomaly': None,
        'earliest_bookable': None, 'earliest_bookable_source': None,
        'sub_tabs': [], 'per_sub_tab': {}, 'bookable_subtabs': [],
        'bookable_doctor_count': 0, 'variants': {},
        'cost_s': None, 'captured_at': dt.datetime.now().isoformat(timespec='seconds'),
    }
    s2 = s_in
    if _empty_dump(s2):
        # 读屏空窗(可恢复) != 页面异常: 单独 error_reason(分类不同、恢复不同), 但计入同一个熔断器,
        # 不再伪装成 not_l2(kind=other) 去触发 3 连熔断(MI 5X 2026-10-14 诊断)。
        unit['error_reason'] = 'empty_dump(读屏空窗, 非页面异常)'
        unit['cost_s'] = round(time.monotonic() - t0, 1)
        return unit
    if page_kind(s2) not in ('L2', 'L2b'):
        unit['error_reason'] = 'not_l2(kind=%s)' % page_kind(s2)
        unit['cost_s'] = round(time.monotonic() - t0, 1)
        return unit
    # 身份校验(2026-05 状态机改造): 页面上的科室必须与本轮目标一致。
    # 语料对账(tools/page_state_check.py, 1322 份 dump): 可核对身份的 959 份(L2+医生主页)里
    # **102 份(10.6%)**文件名声称的科室在页面"身份位"(标题/身份行)上不存在; 另有 256 份 dump
    # **内容逐字节相同**却被存成不同科室的证据(最大一组 125 份其实是同一个登录页;
    # SH-001_产科_... 与 SH-001_产科咨询门诊_... 两份完全一致, 标题与科室卡片都是"炎症性肠病专病门诊")。
    # 即: 采集器把同一屏反复存成不同科室的证据, 旧版对此完全无感
    # => 历史库里"某院某科室无号"的结论, 可能来自另一个科室(甚至登录页)的屏幕。
    # 判不符前先重读 2 次: SPA 路由标题可能滞后 1~2s, 不能拿一次读屏就当终审。
    _obs = obs_of(s2)
    _ok_id, _why = PS.dept_consistent(s2, dept, _obs)
    for _r in range(2):
        if _ok_id or page_kind(s2) not in ('L2', 'L2b'):
            break
        log('  身份待确认(期望=%s, %s) => 等 1.2s 重读(%d/2)' % (dept, _why, _r + 1))
        time.sleep(1.2)
        s2 = adb.dump()
        _obs = obs_of(s2)
        _ok_id, _why = PS.dept_consistent(s2, dept, _obs)
    unit['observed'] = _obs.brief()
    trace_ev('enter_l2', dept, {'level': 'L2', 'dept': dept, 'hospital': hosp}, _obs, _ok_id, dump=s2)
    if not _ok_id:
        unit['error_reason'] = 'dept_mismatch(期望=%s 实测%s)' % (dept, _why)
        unit['cost_s'] = round(time.monotonic() - t0, 1)
        log('  身份不符: 期望科室 %s, %s => 不采(交上层回退到 L1 重走)' % (dept, _why))
        return unit
    # 子 tab 渲染等待(与 full 同纪律: 最多 3 轮 x 2s)
    # ②(2026-10-15): 「有没有子tab」由观测层回答, 「能不能点」由几何回答; 两者不一致
    # 就是"帧不可信"(塌陷/未渲染) => 重读, 读不出来就报错。**绝不**在这种帧上判
    # '(单列,无子tab)' —— 那等于把默认 tab 的列表当成目标 tab 采走。
    subs = find_subtab_nodes(parse_nodes(s2))
    _sub_names = subtab_names(s2)
    for _r in range(SUBTAB_WAIT_ROUNDS):
        if subs or page_kind(s2) not in ('L2', 'L2b'):
            break
        if _sub_names:
            log('  子tab 存在但不可点(%s, 帧塌陷/未渲染) => 等 2s 重读(%d/%d)'
                % (_sub_names, _r + 1, SUBTAB_WAIT_ROUNDS))
        time.sleep(2.0)
        s2 = adb.dump()
        subs = find_subtab_nodes(parse_nodes(s2))
        _sub_names = subtab_names(s2)
    if (not subs) and _sub_names:
        unit['sub_tabs'] = _sub_names
        unit['error_reason'] = 'subtab_not_tappable(%s 存在但几何不可点)' % _sub_names
        unit['cost_s'] = round(time.monotonic() - t0, 1)
        log('  FATAL-ish: 子tab %s 存在但始终不可点 => 不采(旧代码会误判单列并读错列表)' % _sub_names)
        return unit
    bar, bar_anom = parse_bar(s2, today)
    unit['bar'] = bar
    unit['bar_days'] = len(bar) if bar else None
    unit['bar_anomaly'] = bar_anom
    _bar_has0 = bool(bar) and any(d['has_slot'] for d in bar)
    if bar and not _bar_has0:
        # 2026-10-07: 日期条每日 chip 均明确有号/无号(unknown=0), 全无号 => 不需子 tab 重复验证,
        # 直接 no_slot(每组省 ~30-60s; 深核阶段仍会对 top 候选六字段复核)。
        unit['status'] = 'no_slot'
        unit['earliest_bookable_source'] = 'bar_all_no_slot'
        unit['cost_s'] = round(time.monotonic() - t0, 1)
        log('  probe: %s|%s 日期条全无号(%d天) => 跳过子 tab 扫描' % (hosp, dept, len(bar)))
        return unit
    sub_names = [x[0] for x in subs]
    if not sub_names:
        sub_names = ['(单列,无子tab)']
    unit['sub_tabs'] = sub_names
    target_sub = None
    if cond0 and cond0.get('slot_type'):
        zh = SLOT_TYPE_EN2ZH.get(cond0['slot_type'])
        if zh in sub_names:
            target_sub = zh
        elif sub_names == ['(单列,无子tab)'] and cond0['slot_type'] == 'normal':
            target_sub = '(单列,无子tab)'
        else:
            unit['error_reason'] = 'subtab_absent(%s 不在 %s)' % (zh, sub_names)
            unit['cost_s'] = round(time.monotonic() - t0, 1)
            return unit
    if target_sub:
        iter_names = [target_sub]
    else:
        iter_names = sub_names
    prev_key = _cards_key(parse_nodes(s2))
    for i, subname in enumerate(iter_names):
        single = (subname == '(单列,无子tab)')
        _cur = obs_of(s2)
        # ②(2026-05): 采之前先确认"页面确实停在这个子tab"。旧代码 i==0 从不核对,
        # 默认 tab 从没被验证过 => "某科室无号"的结论可能来自另一个 tab 的屏幕。
        # 有硬判据(selected)时: 停着就别点, 停错就必须点到位; 没有硬判据时保持旧行为
        # (i==0 直接扫), 但把 verified 记进证据链, 便于事后区分"实测"与"未证"。
        _hard = bool(_cur.subtab_active)
        _on_it = _hard and PS._name_ok(subname, _cur.subtab_active)
        _how = 'selected' if _on_it else ('none' if not _hard else 'tap')
        if not single and (i > 0 or (_hard and not _on_it)):
            # 切子 tab: 滚顶 + 重取坐标 + tap + 轮询重渲染(复用 full 模式组件)
            s2, ok_top = _scroll_l2_to_top(adb, s2, max_steps=8)
            if page_kind(s2) not in ('L2', 'L2b'):
                unit['interrupted'] = 'left_L2_at_sub_tab=%s' % subname
                break
            node_i = None
            for cand in find_subtab_nodes(parse_nodes(s2)):
                if cand[0] == subname:
                    node_i = cand
                    break
            if node_i is None:
                _bump(unit['variants'], 'subtab_node_missing')
                unit['per_sub_tab'][subname] = {'status': 'error', 'error_reason': 'subtab_node_missing',
                                                'bookable_count': 0, 'full_count': 0, 'doctor_count': 0,
                                                'empty_state': False, 'scrolls': 0, 'verified': _how}
                continue
            st, s2 = _tap_subtab_and_wait(nav, adb, node_i, prev_key)
            _how = 'tap'
            if st == 'left':
                unit['interrupted'] = 'left_L2_at_sub_tab=%s' % subname
                break
            if st == 'no_cards':
                unit['per_sub_tab'][subname] = {'status': 'error', 'error_reason': 'no_doctor_cards(等待渲染未出卡)',
                                                'bookable_count': 0, 'full_count': 0, 'doctor_count': 0,
                                                'empty_state': False, 'scrolls': 0, 'verified': _how}
                prev_key = ()
                continue
            if st == 'unchanged':
                unit['per_sub_tab'][subname] = {'status': 'error', 'error_reason': 'subtab_unchanged(tap 未生效, 不采)',
                                                'bookable_count': 0, 'full_count': 0, 'doctor_count': 0,
                                                'empty_state': False, 'scrolls': 0, 'verified': _how}
                prev_key = ()
                continue
        elif not single:
            trace_ev('subtab', subname, {'level': 'L2', 'subtab': subname}, _cur, True, dump=s2,
                     extra={'how': 'already_selected'})
        s2, bk, fl, nc, empty, scrolls, exhausted = probe_scan_subtab(adb, s2)
        prev_key = _cards_key(parse_nodes(s2))
        unit['per_sub_tab'][subname] = {
            'status': 'ok', 'error_reason': None,
            'bookable_count': bk, 'full_count': fl, 'doctor_count': nc,
            'empty_state': empty, 'scrolls': scrolls, 'list_exhausted': exhausted,
            'verified': _how,
        }
    if unit.get('interrupted'):
        _bump(unit['variants'], 'interrupted')
        if not nav.recover_L1():
            st2, _ = nav.enter()
            if st2 != 'ok':
                unit['interrupted'] = 'l1_lost(%s)' % unit['interrupted']
        unit['cost_s'] = round(time.monotonic() - t0, 1)
        return unit
    scanned = [v for v in unit['per_sub_tab'].values() if v.get('status') == 'ok']
    bookable_subs = [k for k, v in unit['per_sub_tab'].items() if v.get('bookable_count', 0) > 0]
    unit['bookable_subtabs'] = bookable_subs
    unit['bookable_doctor_count'] = sum(v.get('bookable_count', 0) for v in unit['per_sub_tab'].values())
    bar_has = bool(bar) and any(d['has_slot'] for d in bar)
    earliest = None
    if bar:
        for d in bar:
            if d['has_slot']:
                earliest = d['date']
                break
    if not scanned:
        unit['error_reason'] = unit['error_reason'] or 'no_subtab_scanned'
        unit['cost_s'] = round(time.monotonic() - t0, 1)
        return unit
    if bookable_subs:
        unit['status'] = 'ok'
        unit['earliest_bookable'] = earliest
        unit['earliest_bookable_source'] = ('card_btn+bar' if bar_has else 'card_btn')
    elif all(v.get('empty_state') for v in scanned):
        unit['status'] = 'no_slot'
        unit['earliest_bookable_source'] = 'empty_state'
    elif bar_has:
        unit['status'] = 'bar_hint'
        unit['earliest_bookable'] = earliest
        unit['earliest_bookable_source'] = 'bar'
    elif bar is not None and not bar_has:
        unit['status'] = 'no_slot'
        unit['earliest_bookable_source'] = 'bar_all_no_slot'
    else:
        unit['status'] = 'error'
        unit['error_reason'] = unit['error_reason'] or 'no_bar_no_button(无日期条/无去预约/无空态; 页面变体未知)'
    unit['cost_s'] = round(time.monotonic() - t0, 1)
    return unit


def _worst_subtab_reason(unit):
    """④: unit 级 error_reason 为空时, 从 per_sub_tab 明细里挑第一条非空原因兜底。"""
    for _nm, ss in (unit.get('per_sub_tab') or {}).items():
        if isinstance(ss, dict) and ss.get('error_reason'):
            return '%s: %s' % (_nm, ss['error_reason'])
    return 'unspecified(状态 error 但无原因; 见 trace 存档)'


def run_condition_mode(adb, units, gerrors, a, today):
    """S20 --condition 统一入口。units = expand_conditions 输出(已按 (hid,tab,dept,date,slot_type,doctor)
    分组, members = 仅 time_range 不同的条件列表)。
    位置复用(需求4): resolve_start(dump 为准, 记录互证) => goto_unit(最少回退); 异常 => reset_full(重开随申办)。
    隔离: 结果只写 out/condition_runs/; 不写 six_fields.jsonl / dept_runs / state.json。
    返回 (rc, run_summary)。"""
    os.makedirs(COND_RUNS_DIR, exist_ok=True)
    ts_base = dt.datetime.now().strftime('%Y%m%dT%H%M%S')
    trace_begin('_cond')
    merged_p = os.path.join(COND_RUNS_DIR, ts_base + '_all.jsonl')
    run_sum = {'ts': ts_base, 'device': a.device, 'mode': ('probe' if getattr(a, 'mode', 'full') == 'probe' else 'condition'),
               'units': [], 'condition_errors': gerrors, 'stop_reason': None, 'circuit_break': None,
               'groups_planned': len(units),
               'six_records_total': 0,
               'nav_reuse': {'reset': 0, 'enter': 0, 'stay_l2': 0, 'back_to_l1': 0},
               'note': 'condition 模式: 结果只写 out/condition_runs/, 不动 six_fields.jsonl / dept_runs / state.json'}
    six_seen = set()
    rec_buffer = {}
    idx2raw = {}
    for u in units:
        for mm in u['members']:
            idx2raw[mm[0]] = mm[2]
    done_set = set()
    if NO_RESUME:
        log('NO_RESUME=1 => 忽略断点续跑 done 标记, 本次全部单元真跑(A/B / 正向自证)')
    else:
        try:
            for _rp in glob.glob(os.path.join(COND_RUNS_DIR, 'run_*.json')):
                try:
                    with open(_rp, encoding='utf-8') as _fh:
                        _rd = json.load(_fh)
                except Exception:
                    continue
                if _rd.get('device') != a.device or _rd.get('mode') != 'condition':
                    continue
                for _u in _rd.get('units', []):
                    if _u.get('status') in ('ok', 'no_slot'):
                        done_set.add((_u.get('hospital_id'), _u.get('tab'), _u.get('dept'),
                                      (_u.get('condition') or {}).get('slot_type')))
        except Exception:
            done_set = set()
    run_sum['resume_done_pre'] = len(done_set)
    log('resume: %d units already verified ok/no_slot on %s => skip on re-run' % (len(done_set), a.device))
    verify_reset()   # ⑥: 本轮唯一的熔断计数从 0 开始
    deadline = None
    if a.deadline:
        if re.match(r'^\d{1,2}:\d{2}$', a.deadline.strip()):
            _now = dt.datetime.now()
            _hh, _mm = a.deadline.strip().split(':')
            deadline = _now.replace(hour=int(_hh), minute=int(_mm), second=0, microsecond=0)
        else:
            deadline = dt.datetime.fromisoformat(a.deadline.strip())
        log('condition-mode deadline=%s (PC clock)' % deadline.isoformat(timespec='minutes'))
    nav = None
    pos = None
    # 根因修复(2026-10-04): 端上建立 Adb 后、进入条件组循环前，先自动唤起随申办到前台
    # （旧版假设"随申办已由人打开"，未在前台时 dump 抛错 => 根本走不到 _relaunch_channel）。
    try:
        if devbridge.is_ready():
            adb.ensure_foreground(timeout_s=15)
            log('run_condition_mode: ensure_foreground done (随申办已拉前台/窗口已出现)')
    except Exception as _e:
        log('run_condition_mode: ensure_foreground exc (%s)' % str(_e)[:80])
    partial_p = os.path.join(COND_RUNS_DIR, ts_base + '_partial.jsonl')
    rs_p = os.path.join(COND_RUNS_DIR, 'run_%s.json' % ts_base)
    with open(merged_p, 'w', encoding='utf-8') as jf:
        for gi, g in enumerate(units):
            if deadline and dt.datetime.now() >= deadline:
                run_sum['stop_reason'] = 'deadline'
                log('deadline 到点(%s) -> 停止后续组; 已完成组已增量落盘(_partial.jsonl + run 摘要)'
                    % deadline.isoformat(timespec='seconds'))
                break
            cond0 = g['members'][0][1]
            if (g['hid'], g['tab'], g['dept'], cond0.get('slot_type')) in done_set:
                run_sum['skipped_done'] = run_sum.get('skipped_done', 0) + 1
                continue
            # 2026-05: 这里**不**播报。旧版在导航前播下一组目标, 于是"屏幕还停在上一家医院,
            # 浮窗已经在说下一家医院/科室"(用户投诉③)。播报移到 goto_unit 判定 ok 之后。
            unit = {'hospital_id': g['hid'], 'hospital': g['hosp'], 'tab': g['tab'], 'dept': g['dept'],
                    'condition': {k: cond0.get(k) for k in ('date', 'slot_type', 'time_range', 'doctor')},
                    'cond_idxs': [mm[0] for mm in g['members']],
                    'conditions': [mm[2] for mm in g['members']],
                    'status': 'error', 'error_reason': 'not_started', 'nav': None,
                    'per_sub_tab': {}, 'sub_tabs': [], 'six_records_total': 0, 'variants': {}, 'cost_s': None}
            t_unit = time.monotonic()
            # --- 换医院: 回 444 后 (re)enter ---
            if nav is None:
                nav = Nav(adb, g['hosp'])
                pos = resolve_start(adb, nav, g, run_sum['nav_reuse'])
            elif nav.hosp != g['hosp']:
                st, _s = nav.ensure_444()
                if st == 'blocked':
                    unit['error_reason'] = 'blocked'
                    run_sum['stop_reason'] = 'blocked at 444 (switch to %s)' % g['hid']
                    run_sum['units'].append(unit)
                    break
                if st in ('a11y_lost_midrun', 'a11y_bound_but_recover_failed'):
                    unit['error_reason'] = _a11y_reason(st)
                    unit['nav_recovery'] = dict(_A11Y_RECOVERY)
                    unit['cost_s'] = round(time.monotonic() - t_unit, 1)
                    run_sum['stop_reason'] = st
                    run_sum['units'].append(unit)
                    break
                if st != 'ok':
                    run_sum['nav_reuse']['reset'] += 1
                    st, _s = nav.reset_full()
                    if st != 'ok':
                        unit['error_reason'] = 'nav_fail(cannot_reach_444)'
                        run_sum['stop_reason'] = 'cannot_reach_444'
                        run_sum['units'].append(unit)
                        break
                nav = Nav(adb, g['hosp'])
                pos = _pos_now(level='444')
                save_nav_state(pos)
            if pos.get('level') in ('L1', 'L2', 'L2b') and pos.get('hid') == g['hid']:
                unit['nav'] = ('l1_reuse' if pos.get('level') == 'L1' else 'l2_reuse')
            else:
                st, s = nav.enter()
                if st == 'blocked':
                    unit['error_reason'] = 'blocked'
                    run_sum['stop_reason'] = 'blocked at %s entry' % g['hid']
                    run_sum['units'].append(unit)
                    break
                if st != 'ok':
                    log('  ENTER FAIL for %s => reset + retry once' % g['hid'])
                    run_sum['nav_reuse']['reset'] += 1
                    st, s = nav.reset_full()
                    if st == 'ok':
                        st, s = nav.enter()
                if st != 'ok':
                    unit['error_reason'] = 'nav_fail(enter=%s, reset+retry 仍失败)' % st
                    unit['nav'] = 'reset'
                    unit['cost_s'] = round(time.monotonic() - t_unit, 1)
                    run_sum['units'].append(unit)
                    if VERIFY['tripped']:
                        run_sum['stop_reason'] = 'verify_circuit_break'
                        run_sum['circuit_break'] = VERIFY['why']
                        break
                    continue
                pos = _pos_now(level='L1', hid=g['hid'], hosp=g['hosp'])
                save_nav_state(pos)
                run_sum['nav_reuse']['enter'] += 1
                unit['nav'] = 'enter'
            # --- 到目标 (tab,dept) L2(最少回退) ---
            status, s, pos = goto_unit(nav, adb, pos, g, run_sum['nav_reuse'])
            if status == 'ok':
                # 到达目标层(且身份校验通过)之后才播报 —— 浮窗与屏幕一致
                _step('%s · %s / %s (%d/%d)' % (g['hosp'], g['tab'], g['dept'], gi + 1, len(units)), 'ok')
            if status == 'blocked':
                unit['error_reason'] = 'blocked'
                run_sum['stop_reason'] = 'blocked at %s %s|%s' % (g['hid'], g['tab'], g['dept'])
                run_sum['units'].append(unit)
                break
            if status != 'ok':
                unit['error_reason'] = 'nav_fail(goto_unit=%s)' % status
                unit['cost_s'] = round(time.monotonic() - t_unit, 1)
                run_sum['units'].append(unit)
                if VERIFY['tripped']:
                    run_sum['stop_reason'] = 'verify_circuit_break'
                    run_sum['circuit_break'] = VERIFY['why']
                    break
                continue
            blocked = False
            if getattr(a, 'mode', 'full') == 'probe':
                try:
                    u2 = probe_one_dept_unit(nav, adb, g, today, s)
                    unit.update(u2)
                    jf.write(json.dumps(u2, ensure_ascii=False) + '\n')
                    jf.flush()
                except Exception as e:
                    unit['error_reason'] = 'exception:%s' % str(e)[:120]
            else:
                cond = g['members'][0][1]
                try:
                    u2, blocked = fetch_one_dept_unit(nav, adb, g['m'], g['hosp'], g['hid'], g['tab'], g['dept'],
                                                      today, g['plan_order'], ts_base, jf, six_seen,
                                                      cond=cond, pos=pos,
                                                      cond_idxs=[mm[0] for mm in g['members']],
                                                      cond_raws=[mm[2] for mm in g['members']],
                                                      member_trs=[mm[1]['time_range'] for mm in g['members']],
                                                      rec_buffer=rec_buffer, s2_in=s)
                    unit.update(u2)
                except Exception as e:
                    unit['error_reason'] = 'exception:%s' % str(e)[:120]
            if blocked:
                unit['status'] = 'error'
                run_sum['stop_reason'] = 'blocked at %s %s|%s' % (g['hid'], g['tab'], g['dept'])
                unit['cost_s'] = round(time.monotonic() - t_unit, 1)
                run_sum['units'].append(unit)
                break
            unit['cost_s'] = round(time.monotonic() - t_unit, 1)
            if unit.get('interrupted') and pos is not None:
                # 中断后 pos 可能失真 => 保守化: l1 恢复成功记 L1, l1_lost 记 None(下一组走 reset)
                if str(unit['interrupted']).startswith('l1_lost'):
                    pos['level'] = None
                else:
                    pos['level'] = 'L1'
                    pos['dept'] = None; pos['subtab'] = None; pos['date'] = None
                save_nav_state(pos)
            run_sum['units'].append(unit)
            _checkpoint(run_sum, rec_buffer, jf, partial_p, rs_p, idx2raw)
            # ⑥: 单元层不再有「连续 error」/「连续空窗」两套计数器 —— 读屏空窗只是
            # verify 失败的一种分类, 计数与停机都由唯一熔断器(VERIFY)负责。
            if unit.get('status') == 'error':
                # ④: 失败必须带可复核原因 —— error_reason 为空时从子tab明细兜底, 不再打 ERROR(None)
                _er = unit.get('error_reason') or _worst_subtab_reason(unit)
                unit['error_reason'] = _er
                log('  group ERROR(%s) verify_streak=%d/%d'
                    % (str(_er)[:60], VERIFY['fail'], VERIFY_BREAK_N))
            else:
                # 一组「已验证到达」就是成功: 清零连续失败计数
                verify_judge(True, 'unit:%s|%s' % (g['tab'], g['dept']), '', None)
                if getattr(a, 'mode', 'full') == 'probe':
                    log('  probe: %s|%s status=%s earliest=%s btns=%d cost=%.1fs'
                        % (g['tab'], g['dept'], unit.get('status'), unit.get('earliest_bookable'),
                           unit.get('bookable_doctor_count', 0), unit['cost_s']))
                else:
                    log('  group ok: %s|%s six=%d cost=%.1fs'
                        % (g['tab'], g['dept'], unit.get('six_records_total', 0), unit['cost_s']))
            if VERIFY['tripped']:
                run_sum['stop_reason'] = 'verify_circuit_break'
                run_sum['circuit_break'] = VERIFY['why']
                log('VERIFY CIRCUIT BREAK: %s -> 中止(退出码 4)' % VERIFY['why'])
                break
        # S20: 末次统一写出合并记录(cond_idxs 并集已在采集期累积)
        for _key, (_rec, _idxs) in rec_buffer.items():
            _idxs = sorted(_idxs)
            _rec['cond_idxs'] = _idxs
            _rec['conditions'] = [idx2raw[i] for i in _idxs]
            jf.write(json.dumps(_rec, ensure_ascii=False) + '\n')
        jf.flush()
        run_sum['six_records_total'] = len(rec_buffer)
    tmp = rs_p + '.tmp'
    json.dump(run_sum, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    os.replace(tmp, rs_p)
    try:
        os.remove(partial_p)
    except OSError:
        pass
    run_sum['run_summary_file'] = os.path.relpath(rs_p, WS).replace('\\', '/')
    run_sum['merged_jsonl_file'] = os.path.relpath(merged_p, WS).replace('\\', '/')
    log('=== CONDITION MODE DONE: groups=%d six_records=%d stop=%s ==='
        % (len(run_sum['units']), run_sum['six_records_total'], run_sum['stop_reason']))
    rc = 0
    if run_sum.get('circuit_break'):
        rc = 4
    elif (run_sum.get('stop_reason') or '').startswith('blocked'):
        rc = 3
    elif run_sum['units'] and all(u.get('status') == 'error' for u in run_sum['units']):
        rc = 2
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
                     '或改用 --condition-file <UTF-8 JSON> 免损坏通道); 若用 id: 核对 SH-xxx 写法' % hospital_arg)

def _ime_bound(adb):
    """端上无需 ADBKeyboard/IME 绑定：CJK 输入走 ACTION_SET_TEXT。"""
    return True


def _ensure_ime_bound(adb, quiet=False):
    return True


def _ensure_ime(adb):
    log('端上 CJK 输入走 AccessibilityService ACTION_SET_TEXT（无需 ADBKeyboard）')
    return True


def run_conditions(conditions, device=None, deadline=None, mode='full', limit=0,
                   max_consec_errors=None, on_log=None):
    """②/⑥: 导航所有权门。别的线程正在驱动随申办 -> 立刻如实返回, 绝不并发抢屏。

    2026-10-15 23:24 实机事故: 一个未结束的 full run 与新的 probe run 并发,
    深检/另一 run 把 L2 BACK 回 L1, 于是「点子tab 普通门诊」落在 L1 选科室。
    状态机再严谨, 两个导航所有者也会互相踩 —— 所以这里加唯一互斥。
    """
    if not NAVLOCK.acquire('run_conditions', log=on_log):
        h = NAVLOCK.holder() or {}
        if on_log:
            on_log('[fetch_slots] run_conditions 未启动: 导航被 %s 占用 %.0fs'
                   % (h.get('owner'), h.get('held_s') or 0))
        return 5, {'ok': False, 'rc': 5, 'units': [], 'six_records_total': 0,
                   'stop_reason': 'nav_busy:%s' % h.get('owner'),
                   'error': '随申办导航正被 %s 占用(%.0fs), 不排队不抢占'
                            % (h.get('owner'), h.get('held_s') or 0),
                   'nav_busy': True, 'holder': h}
    try:
        return _run_conditions_impl(conditions, device=device, deadline=deadline,
                                    mode=mode, limit=limit,
                                    max_consec_errors=max_consec_errors, on_log=on_log)
    finally:
        NAVLOCK.release('run_conditions')


def _run_conditions_impl(conditions, device=None, deadline=None, mode='full', limit=0,
                   max_consec_errors=None, on_log=None):
    """端上 in-process 号源抓取入口（替代旧 main() 的子进程调用）。

    与 main() 同语义：条件解析 -> manifest 预检 -> expand_conditions -> run_condition_mode；
    结果写 OUT_DIR/condition_runs/（run_<ts>.json + <ts>_all.jsonl）。返回 (rc, run_sum)。
    """
    class _Args(object):
        pass

    a = _Args()
    a.condition = []
    a.condition_file = ''
    a.device = device or _default_device()
    a.deadline = deadline or ''
    a.dry_run = False
    a.limit = int(limit or 0)
    # ⑥: 这个参数现在只是「连续 verify 失败阈值」的别名(不再有独立的单元熔断)
    a.max_consec_errors = set_verify_break(max_consec_errors or VERIFY_BREAK_N)
    a.mode = mode or 'full'

    os.makedirs(OUT_DIR, exist_ok=True)
    _RESET_FULL_USED[0] = 0      # 仅日志计数
    _SEARCH_RESTART_USED[0] = 0  # 仅日志计数(停止只由熔断器决定)
    pc_today = dt.datetime.now().date()
    conds = []
    for i, obj in enumerate(conditions):
        conds.append({'cond': parse_condition(obj, 'condition#%d' % i, pc_today), 'raw': obj})

    # manifest 解析 + 预检（设备接触前完成配置校验）
    seen_m = {}
    for i, it in enumerate(conds):
        cond, _raw = it['cond'], it['raw']
        if cond['tab'] is None:
            raise ValueError('condition#%d: tab 缺失 — condition 模式不支持全院遍历' % i)
        harg = cond['hospital']
        m = seen_m.get(harg)
        if m is None:
            m = load_manifest(harg)
            seen_m[harg] = m
        l1 = m['features']['bookable_departments']['value']['l1']
        tinfo = next((x for x in l1 if x['name'] == cond['tab']), None)
        if tinfo is None:
            log('WARN condition#%d: tab %r 不在 %s manifest' % (i, cond['tab'], m['id']))
        cond['_m'] = m

    units, gerrors = expand_conditions(conds)
    for e in gerrors:
        log('ERROR condition#%d: %s' % (e['idx'], e['error']))
    if not units:
        raise ValueError('无可抓条件组(全部条件验证/展开失败)')
    if a.limit:
        units = units[:a.limit]
        log('--limit %d: 截断到 %d 组' % (a.limit, len(units)))

    adb = Adb(a.device)
    if adb.get_state() != 'device':
        if A11Y_PREFLIGHT:
            # A11Y 前置体检(2026-10-10): 读屏无障碍服务未 Bound => 立即秒级失败(fail fast),
            # 不进入导航循环空转 6 轮。回退: A11Y_PREFLIGHT=False / hh_levers.json {"A11Y":0}。
            log('A11Y preflight FAIL: 读屏无障碍服务未 Bound (devbridge.is_ready()=False)')
            log('  修复: settings put secure enabled_accessibility_services %s  或用户在系统设置里重新开启读屏'
                % A11Y_SERVICE)
            raise RuntimeError('a11y_not_bound(读屏服务未绑定; 需 adb settings 重新启用或用户手动开启)')
        raise RuntimeError('读屏服务未就绪（无障碍未 Bound）')
    _ensure_ime(adb)
    devbridge.wake()
    devbridge.keep_screen_on(True)
    # 根因修复(2026-10-04): 端上建立 Adb 后、进入条件组循环前，先自动唤起随申办到前台
    try:
        if devbridge.is_ready():
            adb.ensure_foreground(timeout_s=15)
            log('run_conditions: ensure_foreground done (随申办已拉前台/窗口已出现)')
    except Exception as _e:
        log('run_conditions: ensure_foreground exc (%s)' % str(_e)[:80])
    log('[fetch_slots] run_conditions: mode=%s groups=%d device=%s deadline=%s'
        % (a.mode, len(units), a.device, a.deadline or '-'))
    today = adb.device_date().date()
    rc, run_sum = run_condition_mode(adb, units, gerrors, a, today)
    devbridge.keep_screen_on(False)
    return rc, run_sum


def main():
    ap = argparse.ArgumentParser(description='\u968f\u7533\u529e \u53f7\u6e90\u7edf\u4e00\u6293\u53d6\u63a5\u53e3 (--condition)')
    ap.add_argument('--condition', action='append', default=[], metavar='JSON',
                    help='\u6761\u4ef6(\u53ef\u91cd\u590d): {"hospital":..., "tab":..., "dept":..., "date":"YYYY-MM-DD"|null, '
                         '"slot_type":"expert|normal|specialty"|null, "time_range":"HH:MM-HH:MM"|null, "doctor":...|null}; '
                         '\u7f3a\u5931 key / null = \u8be5\u6761\u4ef6\u4e0d\u9650\u5236')
    ap.add_argument('--condition-file', default='',
                    help='UTF-8 JSON \u6761\u4ef6\u6587\u4ef6(CJK \u514d\u635f\u574f\u901a\u9053): \u6761\u4ef6\u5bf9\u8c61\u5217\u8868, '
                         '\u6216 {"device":..,"deadline":..,"conditions":[...]}')
    ap.add_argument('--device', default=None, help='adb \u5e8f\u5217\u53f7(\u9ed8\u8ba4=\u672c\u673a\u6807\u8bc6 devbridge.device_key(); --condition-file \u53ef\u5e26 device \u5b57\u6bb5)')
    ap.add_argument('--deadline', default='', help='PC \u65f6\u949f\u786c\u505c\u65f6\u523b(09:00 \u6216 ISO)')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limit', type=int, default=0, help='\u8c03\u8bd5: \u6700\u591a\u6293\u524d N \u4e2a\u6761\u4ef6\u7ec4')
    ap.add_argument('--max-consec-errors', type=int, default=VERIFY_BREAK_N,
                    help='单元级连续 error 熔断阈值(默认 4; 读屏空窗 empty_dump 单独计数, 阈值 6)')
    ap.add_argument('--mode', choices=['full', 'probe'], default='full',
                    help='full=六字段明细采集(默认, 含逐医生主页下钻); probe=号源可得性简要探测(L2 单页: 去预约按钮/日期条/空态, 不逐医生点击; 批量初排用)')
    a = ap.parse_args()
    a.max_consec_errors = set_verify_break(a.max_consec_errors)

    # ---------------- \u96f6\u6a21\u578b\u5b88\u536b: \u91c7\u96c6\u8def\u5f84\u4e0d\u5f97 import app/llm_client.py, \u4e0d\u5f97\u8bbf\u95ee AGENTS_URL ----------------
    for _ev in ('AGENTS_URL', 'RUNTIME_SERVER_URL'):
        os.environ.pop(_ev, None)
    _zm_bad = [mm for mm in sys.modules if mm == 'app.llm_client' or mm.startswith('app.')]
    zero_model = not _zm_bad
    print('zero_model=%s' % zero_model, flush=True)
    assert zero_model, 'zero-model guard violated (app.* \u8fdb\u5165\u91c7\u96c6\u8def\u5f84): %s' % _zm_bad

    # ---------------- CJK argv \u663e\u5f0f\u5b88\u536b ----------------
    _bad = [c for c in a.condition if '\ufffd' in c]
    if _bad:
        raise SystemExit('argv \u542b U+FFFD \u4e71\u7801(\u7591\u4f3c cmd \u4ee3\u7801\u9875\u635f\u574f CJK \u53c2\u6570): %r => '
                         '\u5148 chcp 65001 & set PYTHONUTF8=1, CJK \u6761\u4ef6\u6539\u7528 --condition-file <UTF-8 json> \u514d\u635f\u574f\u901a\u9053'
                         % [b[:40] for b in _bad])

    os.makedirs(OUT_DIR, exist_ok=True)
    pc_today = dt.datetime.now().date()
    conds, f_dev, f_dl = load_conditions_cli(a, pc_today)
    if a.device is None:
        a.device = f_dev or _default_device()
    if not a.deadline and f_dl:
        a.deadline = f_dl

    # ---------------- manifest \u89e3\u6790 + \u9884\u68c0(\u8bbe\u5907\u63a5\u89e6\u524d\u5b8c\u6210\u914d\u7f6e\u6821\u9a8c) ----------------
    seen_m = {}
    for i, it in enumerate(conds):
        cond, raw = it['cond'], it['raw']
        if cond['tab'] is None:
            raise SystemExit('condition#%d: tab \u7f3a\u5931 \u2014 condition \u6a21\u5f0f\u4e0d\u652f\u6301\u5168\u9662\u904d\u5386'
                             '(\u5957\u8def\u7528 v1 \u5907\u4efd\u811a\u672c slots/fetch_slots_v1_20261004.py --hospital)' % i)
        harg = cond['hospital']
        if harg in seen_m:
            m = seen_m[harg]
        else:
            m = load_manifest(harg)
            seen_m[harg] = m
        l1 = m['features']['bookable_departments']['value']['l1']
        tinfo = next((x for x in l1 if x['name'] == cond['tab']), None)
        if tinfo is None:
            log('WARN condition#%d: tab %r \u4e0d\u5728 %s manifest (live \u9875\u53ef\u80fd\u6709; \u82e5\u65e0 => \u663e\u5f0f\u62a5)'
                % (i, cond['tab'], m['id']))
        elif cond['dept'] is None:
            log('condition#%d: dept=null => \u5c55\u5f00 manifest tab %r \u4e0b\u5168\u90e8\u79d1\u5ba4(%d)'
                % (i, cond['tab'], len(tinfo.get('depts', []))))
        elif cond['dept'] not in [x.get('raw') for x in tinfo.get('depts', [])]:
            log('WARN condition#%d: dept %r \u4e0d\u5728 manifest tab %r \u7cbe\u786e\u5217\u8868; \u5c06\u6309 live \u9875\u6a21\u7cca\u5339\u914d'
                % (i, cond['dept'], cond['tab']))
        cond['_m'] = m

    units, gerrors = expand_conditions(conds)
    for e in gerrors:
        log('ERROR condition#%d: %s' % (e['idx'], e['error']))
    if not units:
        raise SystemExit('\u65e0\u53ef\u6293\u6761\u4ef6\u7ec4(\u5168\u90e8\u6761\u4ef6\u9a8c\u8bc1/\u5c55\u5f00\u5931\u8d25)')
    if a.limit:
        units = units[:a.limit]
        log('--limit %d: \u622a\u65ad\u5230 %d \u7ec4' % (a.limit, len(units)))

    if a.dry_run:
        print('%s-mode dry-run: %d conditions -> %d groups' % (getattr(a, 'mode', 'full'), len(conds), len(units)))
        for u in units:
            c0 = u['members'][0][1]
            print('  %s | %s | %s | date=%s slot_type=%s doctor=%s time_range=%s | cond_idxs=%s'
                  % (u['hid'], u['tab'], u['dept'], c0.get('date'), c0.get('slot_type'),
                     c0.get('doctor'), c0.get('time_range'), [mm[0] for mm in u['members']]))
        if gerrors:
            print('  errors: %s' % gerrors)
        print('  device=%s deadline=%s out=%s' % (a.device, a.deadline or '-', os.path.relpath(COND_RUNS_DIR, WS)))
        print('  (dry-run: \u4e0d\u78b0\u8bbe\u5907, \u4e0d\u5199\u6587\u4ef6)')
        return

    adb = Adb(a.device)
    assert adb.get_state() == 'device', '读屏服务未就绪（无障碍未 Bound）'
    _ensure_ime(adb)
    devbridge.wake()
    devbridge.keep_screen_on(True)
    today = adb.device_date().date()
    rc, run_sum = run_condition_mode(adb, units, gerrors, a, today)
    devbridge.keep_screen_on(False)
    print(json.dumps(run_sum, ensure_ascii=False, indent=1), flush=True)
    sys.exit(rc)


if __name__ == '__main__':
    main()
