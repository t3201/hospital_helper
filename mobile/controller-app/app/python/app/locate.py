# -*- coding: utf-8 -*-
"""定位（端上改造）：优先级 ①--loc "lng,lat"(人为指定, 代挂号主场景)
②--loc-text: amap_place(高德 v3/place/text) → amap_geo(高德 v3/geocode/geo)
→ stub(内置样例小表兜底)。
输出统一 GCJ-02 {lng,lat}。纯标准库；WGS-84→GCJ-02 为公开换算公式；
高德 key 解析/脱敏/缓存在 app/amap.py。

端上变化（P3）：
  * **已移除** --loc-adb / dumpsys location（端上不再有 adb；位置改由表单手填坐标
    或地址文本经高德解析，或 direct WebView 定位由 Java 传入）。
  * fixtures 路径经 hhpaths 指向 assets 解压目录（app/fixtures/geocode_stub.json）。
"""
import json
import math
import os
import re

from . import amap

# 端上改造：fixtures 从 assets 解压目录读；桌面退化本机 app/fixtures。
try:
    from hhpaths import data as _data
    GEOCODE_STUB = _data('app', 'fixtures', 'geocode_stub.json')
except Exception:
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


def locate(args):
    """端上定位入口（已移除 --loc-adb）。"""
    if getattr(args, 'loc', None):
        return parse_loc(args.loc)
    if getattr(args, 'loc_text', None):
        return _resolve_text(args.loc_text, no_network=getattr(args, 'no_network', False))
    raise GeocodeError('未指定位置：请用 --loc "lng,lat" / --loc-text "地址"')
