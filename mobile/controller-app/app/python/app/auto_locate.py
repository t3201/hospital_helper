# -*- coding: utf-8 -*-
"""自动定位（端上）— 对齐 web /api/device-loc 的语义。

web 原链路：adb 读手机最后位置(dumpsys location, WGS-84, fused>gps>network)
  -> wgs84_to_gcj02 -> 高德 regeo_gcj02 逆地理成地址 -> 提交 loc_text。

端上差异：没有 adb，改用 LocationManager.getLastKnownLocation（WGS-84）；
WGS-84->GCJ-02 用 amap.wgs84_to_gcj02（公开换算法，非 API 调用，不耗配额）；
逆地理用 amap.regeo_gcj02（仅当有 AMAP_KEY 才发；无 key 时只回坐标、不报错）。

产出（对齐 web body）：
  {ok, lng, lat, crs:'GCJ-02', address, city, district,
   provider, age_s, amap_snapped, error}
调用方（MainActivity）：address 非空 -> 填 loctext（地址文本，pipeline 再用高德 geocode）；
                        address 空   -> 填 loc 经纬度（"lng,lat"，pipeline parse_loc 直读）。

安全：不打印/不缓存 key；amap 内部已脱敏；本模块只读坐标与地址，无隐私补全。
"""
import json
import os
import time

import devbridge

try:
    from . import amap
    from hhpaths import out as _out
    CACHE_FILE = _out('cache', 'device_loc.json')
except Exception:  # 桌面/无 hhpaths
    import amap  # type: ignore
    CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache', 'device_loc.json')

# 与 web DEVICE_LOC_TTL_S 对齐：30 分钟。
CACHE_TTL_S = 1800


def _read_last():
    """读最后已知位置 -> (lat, lng, provider, age_ms) | None。"""
    return devbridge.last_known_location()


def _autolocate(fresh=False):
    """对齐 web._device_loc：读位置 -> GCJ-02 -> (有 key 时) 逆地理。
    返回 (dict, err)。失败但有旧缓存 -> 回旧缓存并附 error 说明。"""
    cached = None
    try:
        with open(CACHE_FILE, encoding='utf-8') as fh:
            cached = json.load(fh)
    except Exception:
        cached = None
    if cached and not fresh and (time.time() - float(cached.get('ts', 0))) < CACHE_TTL_S:
        cached['from_cache'] = True
        return cached, None

    lat, lng, prov, age_ms = None, None, '', 0
    loc = _read_last()
    if loc is None:
        msg = '未取到最后已知位置（未授予位置权限 / 无 GPS 历史 / LocationManager 无记录）'
        if cached:
            cached = dict(cached)
            cached['from_cache'] = True
            cached['error'] = '实时读失败(%s) -> 返回旧缓存' % msg
            return cached, None
        return None, msg
    lat, lng, prov, age_ms = loc

    # WGS-84 -> GCJ-02（公开换算法，非网络调用）。
    try:
        g_lng, g_lat = amap.wgs84_to_gcj02(lng, lat)
    except Exception:
        g_lng, g_lat = lng, lat
    g_lng, g_lat, address, city, district, snapped = float(g_lng), float(g_lat), '', '', '', False
    key = None
    try:
        key = amap.resolve_key()
    except Exception:
        key = None
    if key:
        try:
            r, _cached = amap.regeo_gcj02(key, g_lng, g_lat, radius=1500)
            if r and r.get('found'):
                address = r.get('address') or ''
                city = r.get('city') or ''
                district = r.get('district') or ''
                fl = (r.get('location') or '').split(',')
                if len(fl) == 2:
                    n_lng, n_lat = float(fl[0]), float(fl[1])
                    snapped = abs(n_lng - g_lng) > 1e-5 or abs(n_lat - g_lat) > 1e-5
                    g_lng, g_lat = n_lng, n_lat
        except amap.AmapError:
            pass  # 逆地理失败(配额/网络) -> 只回坐标

    d = {
        'ts': time.time(),
        'ts_iso': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'lng': round(g_lng, 6), 'lat': round(g_lat, 6), 'crs': 'GCJ-02',
        'address': address, 'city': city, 'district': district,
        'provider': prov, 'age_s': round(age_ms / 1000.0, 1),
        'amap_snapped': snapped, 'has_amap_key': bool(key),
        'from_cache': False,
    }
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        tmp = CACHE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass
    return d, None


def autolocate_json(fresh=True):
    """App 入口（对齐 web「📍自动定位」按钮）：点按钮 = 实时读 fresh=True。
    返回单 JSON 字符串（避免 Chaquopy 元组跨语言摩擦）。
    {ok, lng, lat, crs, address, city, district, provider, age_s, amap_snapped, error}"""
    d, err = None, None
    try:
        d, err = _autolocate(fresh=fresh)
    except Exception as e:  # noqa
        d, err = None, '%s: %s' % (type(e).__name__, e)
    if d is not None:
        out = {
            'ok': True, 'lng': d.get('lng'), 'lat': d.get('lat'), 'crs': d.get('crs', 'GCJ-02'),
            'address': d.get('address', ''), 'city': d.get('city', ''), 'district': d.get('district', ''),
            'provider': d.get('provider', ''), 'age_s': d.get('age_s'),
            'amap_snapped': bool(d.get('amap_snapped')), 'has_amap_key': bool(d.get('has_amap_key')),
            'from_cache': bool(d.get('from_cache')), 'error': d.get('error'),
        }
    else:
        out = {'ok': False, 'lng': None, 'lat': None, 'crs': 'GCJ-02', 'address': '',
               'city': '', 'district': '', 'provider': '', 'age_s': None,
               'amap_snapped': False, 'has_amap_key': False, 'from_cache': False,
               'error': err or 'unknown'}
    return json.dumps(out, ensure_ascii=False)


def load_loc_cache_json():
    """App 启动时回填：只读缓存（不读位置），无缓存 -> ok=False。"""
    try:
        with open(CACHE_FILE, encoding='utf-8') as fh:
            d = json.load(fh)
    except Exception:
        return json.dumps({'ok': False, 'address': '', 'lng': None, 'lat': None,
                           'from_cache': True}, ensure_ascii=False)
    out = {'ok': True, 'lng': d.get('lng'), 'lat': d.get('lat'), 'crs': d.get('crs', 'GCJ-02'),
           'address': d.get('address', ''), 'city': d.get('city', ''), 'district': d.get('district', ''),
           'provider': d.get('provider', ''), 'age_s': d.get('age_s'),
           'from_cache': True, 'ts_iso': d.get('ts_iso')}
    return json.dumps(out, ensure_ascii=False)
