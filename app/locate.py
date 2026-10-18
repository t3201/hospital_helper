# -*- coding: utf-8 -*-
"""定位：优先级 ①--loc "lng,lat"(人为指定, 代挂号主场景)
②--loc-text: amap_place(高德 v3/place/text) → amap_geo(高德 v3/geocode/geo) → stub(内置样例小表兜底)
③--loc-adb(ADB dumpsys location 接口)。输出统一 GCJ-02 {lng,lat}。
纯标准库；WGS-84→GCJ-02 为公开换算公式；高德 key 解析/脱敏/缓存见 app/amap.py。
--no-network 或无 key 时 --loc-text 只走 stub（位置为内置样例坐标, 非真实定位, 显式标注）。
"""
import json
import math
import os
import re
import shutil
import subprocess

from . import amap

APP_DIR = os.path.dirname(os.path.abspath(__file__))
GEOCODE_STUB = os.path.join(APP_DIR, 'fixtures', 'geocode_stub.json')


def wgs84_to_gcj02(lng, lat):
    a = 6378245.0
    ee = 0.00669342162296594323

    def tlat(x, y):
        r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
        r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        r += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
        r += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
        return r

    def tlng(x, y):
        r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
        r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        r += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
        r += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
        return r

    dlat = tlat(lng - 105.0, lat - 35.0)
    dlng = tlng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    # 2026-10-06 修复: 两处分母均缺 *180.0 -> 偏移被放大 180 倍 (实测上海偏差 ~4.5km,
    # 导致 --loc-adb / web 设备定位读到完全错误的点位)。标准式分母 = M(或N) * 180/pi。
    dlat = (dlat * 180.0) / ((a * (1 - ee) / magic / sqrtmagic) * 180.0 / math.pi)
    dlng = (dlng * 180.0) / ((a / sqrtmagic * math.cos(radlat)) * 180.0 / math.pi)
    return lng + dlng, lat + dlat


class GeocodeError(RuntimeError):
    pass


class StubGeocoder:
    """可插拔 geocoder：默认读 fixtures/geocode_stub.json 小表；
    接真实服务时替换本类（保持 resolve(text)->dict{lng,lat,crs,method,note} 接口即可）。"""

    def __init__(self, path=GEOCODE_STUB):
        self.table = {}
        if os.path.exists(path):
            data = json.load(open(path, encoding='utf-8'))
            for e in data.get('entries', []):
                self.table[e['text']] = e

    def resolve(self, text):
        t = re.sub(r'\s+', '', text)
        for key, e in self.table.items():
            if key == t or key in t or t in key:
                return {'lng': e['lng'], 'lat': e['lat'], 'crs': 'GCJ-02',
                        'method': 'stub', 'is_stub': True,
                        'note': '位置为内置样例坐标，非真实定位（stub 小表命中「%s」）' % e.get('addr', key)}
        raise GeocodeError('geocode stub 小表未命中: %r（可插拔：把 StubGeocoder 换成真实 geocode 服务）' % text)


def _finish_amap(r, method, cached):
    """高德结果 → resolve() 结构：method 如实标注，缓存命中加 _cached 后缀且 cached=true。"""
    m = method + ('_cached' if cached else '')
    where = (r.get('adname') or '') + (r.get('name') or r.get('address') or '')
    note = '高德 %s 命中「%s」%s%s' % (method, r.get('name') or r.get('address') or '?', where,
                                       '（缓存命中）' if cached else '')
    return {'lng': r['lng'], 'lat': r['lat'], 'crs': 'GCJ-02',
            'method': m, 'cached': cached, 'is_stub': False, 'note': note,
            'address': r.get('address', ''), 'adname': r.get('adname', ''), 'adcode': r.get('adcode', '')}


def _resolve_text(text, no_network=False):
    """--loc-text 优先级: amap_place → amap_geo → stub(兜底, 显式标注内置样例坐标)。"""
    if no_network:
        print('   (离线模式 --no-network: 不发高德请求, --loc-text 只查内置 stub 小表)')
    else:
        key = amap.resolve_key()
        if not key:
            print('   (未检测到 AMAP_KEY: --loc-text 回落内置 stub 小表, 位置为内置样例坐标, 非真实定位)')
        else:
            try:
                r, cached = amap.search_place(key, text)
                if r and r.get('found'):
                    return _finish_amap(r, 'amap_place', cached)
                print('   (高德 place/text 未命中, 回落 geocode/geo)')
            except amap.AmapError as e:
                print('   ⚠ 高德 place/text 不可用: %s（回落 geocode/geo → stub）' % e)
            try:
                r, cached = amap.geocode_geo(key, text)
                if r and r.get('found'):
                    return _finish_amap(r, 'amap_geo', cached)
                print('   (高德 geocode/geo 未命中, 回落 stub)')
            except amap.AmapError as e:
                print('   ⚠ 高德 geocode/geo 不可用: %s（回落 stub 内置样例坐标）' % e)
    try:
        return StubGeocoder().resolve(text)
    except GeocodeError:
        raise GeocodeError('--loc-text 未解析: 高德 place/text 与 geocode/geo 均未命中, 内置 stub 小表也无「%s」'
                           '（离线/无 key 时仅 stub 小表内地址可用: %s）' % (text, sorted(StubGeocoder().table)))


def parse_loc(s):
    m = re.match(r'^\s*([\d.]+)\s*[,，]\s*([\d.]+)\s*$', s)
    if not m:
        raise GeocodeError('--loc 格式应为 "lng,lat": %r' % s)
    return {'lng': float(m.group(1)), 'lat': float(m.group(2)), 'crs': 'GCJ-02',
            'method': 'user_specified',
            'note': '人为指定坐标（代挂号主场景/固定演示点），假定 GCJ-02'}


def _parse_et(et_str):
    """et=+5d10h48m1s67ms -> 秒 (位置产生时刻的 elapsedRealtime)。"""
    if not et_str:
        return None
    m = re.match(r'\+?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?(?:(\d+)ms)?$',
                 et_str.strip())
    if not m or not any(m.groups()):
        return None
    d, h, mi, sec, ms = m.groups()
    total = int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60
    if sec:
        total += float(sec)
    if ms:
        total += int(ms) / 1000.0
    return total


def locate_from_adb(device=None, adb='adb'):
    """从手机取位置: adb shell dumpsys location -> last location(WGS-84) -> 转 GCJ-02。
    兼容两种输出格式(实测 476cf991 / MIUI 为新格式; 旧格式仅在新格式无该 provider 时兜底):
      新: last location=Location[fused 31.254646,121.432931 hAcc=48.0 et=+5d10h48m1s67ms {Bundle[...]}
      旧: Fused: 31.254646, 121.432931
    provider 优先级 fused > gps > network(同类多条取最后一条)。
    注意: 返回的是手机"最后记录"的位置, 可能数小时~数天前(见 age_s), 不一定是当前所在。"""
    if not shutil.which(adb):
        raise GeocodeError('未找到 adb（--loc-adb 需要 Android 设备与 adb）')
    base = [adb] + (['-s', device] if device else [])
    out = subprocess.run(base + ['shell', 'dumpsys', 'location'],
                         capture_output=True, text=True, timeout=30).stdout
    cand = {}
    for m in re.finditer(r'last location=Location\[(\w+)\s+([0-9.]+),\s*([0-9.]+)'
                         r'(?:\s+hAcc=[0-9.]+\s+et=([+\w.dhms]+))?', out):
        prov = m.group(1).lower()
        cand[prov] = (m.group(2), m.group(3), prov, m.group(4))
    for m in re.finditer(r'\b(Network|GPS|Fused)\s*:\s*([0-9.]+),\s*([0-9.]+)', out):
        prov = m.group(1).lower()
        cand.setdefault(prov, (m.group(2), m.group(3), m.group(1), None))
    if not cand:
        raise GeocodeError('dumpsys location 中未解析到 last location 行'
                           '(Location[provider lat,lng] 与 Provider: lat,lng 两种格式均无)')
    prio = {'fused': 0, 'gps': 1, 'network': 2}
    lat_s, lng_s, prov, et_s = min(cand.values(), key=lambda t: prio.get(t[2].lower(), 9))
    lat, lng = float(lat_s), float(lng_s)
    lng, lat = wgs84_to_gcj02(lng, lat)
    age_s = None
    et = _parse_et(et_s)
    if et is not None:
        try:
            up = subprocess.run(base + ['shell', 'cat', '/proc/uptime'],
                                capture_output=True, text=True, timeout=15).stdout
            up_s = float(up.strip().split()[0])
            age_s = max(0.0, up_s - et)  # /proc/uptime 不含挂起时间 -> 偏保守(偏小)
        except Exception:
            age_s = None
    note = 'dumpsys 最后位置 provider=%s, WGS-84→GCJ-02 已换算' % prov
    if age_s is not None and age_s > 900:
        note += ', 该位置记录约 %.1f 小时前(手机最近未更新定位, 可能非当前位置)' % (age_s / 3600.0)
    return {'lng': round(lng, 6), 'lat': round(lat, 6), 'crs': 'GCJ-02',
            'method': 'adb_dumpsys', 'provider': prov, 'age_s': age_s,
            'note': note}



def locate(args):
    if getattr(args, 'loc', None):
        return parse_loc(args.loc)
    if getattr(args, 'loc_text', None):
        return _resolve_text(args.loc_text, no_network=getattr(args, 'no_network', False))
    if getattr(args, 'loc_adb', False):
        return locate_from_adb(getattr(args, 'device', None))
    raise GeocodeError('未指定位置：请用 --loc "lng,lat" / --loc-text "地址" / --loc-adb')
