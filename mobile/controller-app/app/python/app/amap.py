# -*- coding: utf-8 -*-
"""地理编码客户端（纯标准库 urllib，零第三方依赖）。

2026-10-14 接口归集：端上默认**不再持有高德 key**，一律走自建服务端
`https://hh.ei.ac.cn:51120/v1/location`（服务端内部转调高德，以后可换天地图，端上零改动）。
桌面/自测若显式配了真 key（PyBridge amap_key / 环境变量 AMAP_KEY）则仍直连高德，便于离线对照。

key 解析:  PyBridge amap_key → os.environ['AMAP_KEY'] → hospital_intel/.env → 都没有=服务端代理('server')。
检索顺序:  v3/place/text（POI 检索，适合小区/地标/医院名这类"非结构化地址"）
           → 未命中再 v3/geocode/geo（结构化地址 geocoding）。
坐标系:    高德返回 GCJ-02，与 manifest 同系，零转换。

安全（仓库硬规则）:
- key 不硬编码、不打印、不记日志；任何错误/日志消息若含 key 一律脱敏为 ***；
- 绝不打印请求 URL；
- 缓存文件只存结果结构（lng/lat/address/adname/adcode 等），不含 key / URL 编码 / key= 片段，
  可被 hospital_intel/scripts/scan_cache_leak.py 校验。
"""
import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

# 端上改造：.env / 缓存放 filesDir（读数据和可写产物分离；桌面退化本机）。
try:
    from hhpaths import data as _data, out as _out, config as _config
    ENV_FILE = _data('hospital_intel', '.env')
    CACHE_DIR = _out('cache', 'amap')
except Exception:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    WS_DIR = os.path.dirname(APP_DIR)
    ENV_FILE = os.path.join(WS_DIR, 'hospital_intel', '.env')
    CACHE_DIR = os.path.join(APP_DIR, 'cache', 'amap')

    def _config(_k, _d=None):
        return None
CITY = '上海'
TIMEOUT = 15
CACHE_TTL_DAYS = 30  # 地理编码结果稳定；命中与未命中都缓存，避免重复烧配额

API_PLACE = 'https://restapi.amap.com/v3/place/text'
API_GEO = 'https://restapi.amap.com/v3/geocode/geo'
API_REGEO = 'https://restapi.amap.com/v3/geocode/regeo'


class AmapError(RuntimeError):
    pass


def load_env_file(path):
    """迷你 key=value 解析（与 hospital_intel/scripts/enrich_transit_444.py 同型，只读参考不依赖）。"""
    d = {}
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            s = line.strip()
            if not s or s.startswith('#') or '=' not in s:
                continue
            k, v = s.split('=', 1)
            d[k.strip()] = v.strip().strip('"').strip("'")
    return d


def resolve_key():
    """返回真实高德 key（仅桌面/自测显式配置时）或哨兵 'server'（端上默认=走服务端代理）。
    环境变量 HH_LOCATION_DISABLE=1 → 返回 None（彻底禁用地理增强）。"""
    if os.environ.get('HH_LOCATION_DISABLE') == '1':
        return None
    for v in (_config('amap_key'), os.environ.get('AMAP_KEY'),
              load_env_file(ENV_FILE).get('AMAP_KEY')):
        v = (v or '').strip()
        if v and v != SERVER_SENTINEL:
            return v
    return SERVER_SENTINEL



# ---------- 归集服务端模式（端上默认，2026-10-14 接口归集） ----------
# 端上不再持有高德 key：地理能力统一走自建服务端 https://hh.ei.ac.cn:51120/v1/location，
# 服务端内部转调高德（以后可换天地图，端上零改动）。
SERVER_DEFAULT = 'https://hh.ei.ac.cn:51120'
SERVER_LOCATION_PATH = '/v1/location'
SERVER_SENTINEL = 'server'   # resolve_key() 的哨兵值：表示"走服务端代理，无本地 key"


def resolve_server_base():
    """PyBridge 配置 hh_server → 环境变量 HH_SERVER_URL → 写死的 PUBLIC_BASE。"""
    try:
        v = _config('hh_server')
    except Exception:
        v = None
    v = (v or os.environ.get('HH_SERVER_URL') or SERVER_DEFAULT).strip()
    return v.rstrip('/')


def _server_get(params):
    """GET {base}/v1/location?…；只打端点短名，异常不外泄 URL 之外的敏感信息。"""
    url = resolve_server_base() + SERVER_LOCATION_PATH + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0',
                                               'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode('utf-8'))
    except AmapError:
        raise
    except urllib.error.HTTPError as e:      # 4xx/5xx: 把服务端 error 文案带出来（含"未命中"）
        try:
            body = json.loads(e.read().decode('utf-8', 'replace'))
            msg = body.get('error') or body.get('note') or str(body)[:160]
        except Exception:
            msg = 'HTTP %s' % e.code
        raise AmapError('服务端 /v1/location 失败: %s' % msg)
    except Exception as e:
        raise AmapError('服务端 /v1/location 调用失败: %s' % (str(e)[:160]))
    if not data.get('ok', True):
        raise AmapError('服务端 /v1/location 返回错误: %s' % (data.get('error') or data))
    return data


def _server_search(text, city=CITY):
    """服务端正地理编码（内部 place/text → geocode/geo 兜底，与端上原顺序一致）。
    出参为**扁平单条命中**：{ok,provider,mode,method,lng,lat,crs,name,address,adname,
    adcode,city,district,location,note}；未命中服务端返回 502 → 抛 AmapError（调用方回退 stub）。"""
    data = _server_get({'text': text, 'city': city})
    lng, lat = data.get('lng'), data.get('lat')
    return _base_result(bool(data.get('found', lng is not None)) and lng is not None,
                        q=text, crs=(data.get('crs') or 'GCJ-02'), lng=lng, lat=lat,
                        address=data.get('address') or '', adname=data.get('adname') or '',
                        adcode=data.get('adcode') or '', location=data.get('location') or '',
                        name=data.get('name') or '', city=data.get('city') or '',
                        district=data.get('district') or '',
                        township=data.get('township') or '', street=data.get('street') or '',
                        number=data.get('number') or '',
                        method=data.get('method') or '', note=data.get('note') or '',
                        provider=data.get('provider') or '')


def _server_regeo(lng, lat, radius=1000):
    """服务端逆地理编码（内部高德 regeo；crs 由服务端给出，端上原样使用）。"""
    text = '%.6f,%.6f' % (lng, lat)
    data = _server_get({'lng': '%.6f' % lng, 'lat': '%.6f' % lat, 'radius': int(radius)})
    return _base_result(bool(data.get('found', data.get('lng') is not None)),
                        lng=data.get('lng'), lat=data.get('lat'),
                        address=data.get('address') or '',
                        city=data.get('city') or '', district=data.get('district') or '',
                        township=data.get('township') or '', street=data.get('street') or '',
                        number=data.get('number') or '', adcode=data.get('adcode') or '',
                        location=data.get('location') or '', name=data.get('name') or '',
                        method=data.get('method') or '', note=data.get('note') or '',
                        provider=data.get('provider') or '', crs=(data.get('crs') or 'GCJ-02'),
                        q=text)


def _mask(msg, key):
    """防御性脱敏：任何要外泄的字符串若含 key 明文，替换为 ***。"""
    if key:
        msg = msg.replace(key, '***')
        msg = msg.replace(urllib.parse.quote(key, safe=''), '***')
    return msg


def _get(endpoint, url_base, params, key):
    """发起 GET；只打印端点短名，绝不打印 URL；异常消息脱敏后抛出 AmapError。"""
    q = dict(params)
    q['key'] = key
    q['output'] = 'json'
    url = url_base + '?' + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode('utf-8'))
    except AmapError:
        raise
    except Exception as e:
        raise AmapError(_mask('高德 %s 调用失败: %s' % (endpoint, e), key))
    if str(data.get('status')) != '1':
        raise AmapError('高德 %s 返回非 OK: infocode=%s info=%s' % (endpoint, data.get('infocode'), data.get('info')))
    return data


def _split_loc(loc_str):
    parts = (loc_str or '').split(',')
    if len(parts) < 2:
        raise ValueError('location 格式异常: %r' % (loc_str,))
    return float(parts[0]), float(parts[1])


def _cache_path(tag, text):
    h = hashlib.sha1(text.encode('utf-8')).hexdigest()[:20]
    return os.path.join(CACHE_DIR, '%s_%s.json' % (tag, h))


def _cache_read(path, ttl_days):
    try:
        with open(path, encoding='utf-8') as fh:
            d = json.load(fh)
        if time.time() - float(d.get('_ts', 0)) > ttl_days * 86400:
            return None
        return d
    except Exception:
        return None


def _cache_write(path, d):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _norm(text):
    return re.sub(r'\s+', '', text or '')


def _base_result(found, **kw):
    r = {'found': found, 'crs': 'GCJ-02'}
    r.update(kw)
    return r


def search_place(key, text, city=CITY):
    """v3/place/text 优先通道。返回 (result|None, from_cache)；result 含
    address/adname/adcode/location(lng,lat)/name。"""
    t = _norm(text)
    if not key or not t:
        return None, False
    path = _cache_path('place', t)
    hit = _cache_read(path, CACHE_TTL_DAYS)
    if hit is not None:
        hit.pop('_ts', None)
        return hit, True
    if key == SERVER_SENTINEL:
        result = _server_search(t, city)
        result['_ts'] = time.time()
        _cache_write(path, result)
        return result, False
    data = _get('v3/place/text', API_PLACE,
                {'keywords': t, 'city': city, 'citylimit': 'true', 'offset': 5, 'page': 1}, key)
    pois = data.get('pois') or []
    result = _base_result(False)
    if pois:
        p = pois[0]
        try:
            lng, lat = _split_loc(p.get('location'))
        except (ValueError, TypeError):
            lng = lat = None
        if lng is not None:
            result = _base_result(True, lng=lng, lat=lat,
                                  address=p.get('address') or '', adname=p.get('adname') or '',
                                  adcode=p.get('adcode') or '', location=p.get('location') or '',
                                  name=p.get('name') or '')
    result['q'] = t
    result['_ts'] = time.time()
    _cache_write(path, result)
    return result, False


def geocode_geo(key, text, city=CITY):
    """v3/geocode/geo 回落通道。返回 (result|None, from_cache)。"""
    t = _norm(text)
    if not key or not t:
        return None, False
    path = _cache_path('geo', t)
    hit = _cache_read(path, CACHE_TTL_DAYS)
    if hit is not None:
        hit.pop('_ts', None)
        return hit, True
    if key == SERVER_SENTINEL:
        result = _server_search(t, city)
        result['_ts'] = time.time()
        _cache_write(path, result)
        return result, False
    data = _get('v3/geocode/geo', API_GEO,
                {'address': t, 'city': city, 'citylimit': 'true'}, key)
    gs = data.get('geocodes') or []
    result = _base_result(False)
    if gs:
        g = gs[0]
        try:
            lng, lat = _split_loc(g.get('location'))
        except (ValueError, TypeError):
            lng = lat = None
        if lng is not None:
            result = _base_result(True, lng=lng, lat=lat,
                                  address=g.get('formatted_address') or '',
                                  adname=g.get('district') or '', adcode=g.get('adcode') or '',
                                  location=g.get('location') or '')
    result['q'] = t
    result['_ts'] = time.time()
    _cache_write(path, result)
    return result, False


# ---------- 坐标转换 + 逆地理 (web 自动定位用) ----------
_A_PI = math.pi * 3000.0 / 180.0
_A_ECC = 0.00669342162296594323


def _out_of_china(lng, lat):
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x, y):
    r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    return r


def _transform_lng(x, y):
    r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    return r


def wgs84_to_gcj02(lng, lat):
    """浏览器 Geolocation 返回 WGS-84; 高德/manifest 为 GCJ-02, 中国境内偏移 ~100-600m。
    注: 本实现为近似偏移(公开多项式的常见流传变体, 未逐点标定)——web /api/regeo 端点
    会用高德 regeo 返回的匹配坐标(location 字段, 精确 GCJ-02)做最终校正, 不依赖本函数精度。
    境外坐标原样返回（不偏移）。"""
    if _out_of_china(lng, lat):
        return lng, lat
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _A_ECC * magic * magic
    sm = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((6378245.0 * (1 - _A_ECC)) / (sm * sm) * 180.0 / math.pi)
    dlng = (dlng * 180.0) / (6378245.0 / sm * math.cos(radlat) * 180.0 / math.pi)
    return lng + dlng, lat + dlat


def regeo_gcj02(key, lng, lat, radius=1000):
    """逆地理（GCJ-02 坐标 -> 地址描述）。返回 (result|None, from_cache)。
    result: {found, address(formatted), city, district, township, street, lng, lat, crs}。
    key 绝不外泄: 只在本进程内调用 _get, 缓存文件不含 key/URL。"""
    if not key:
        return None, False
    lng = round(float(lng), 6)
    lat = round(float(lat), 6)
    text = '%.6f,%.6f' % (lng, lat)
    path = _cache_path('regeo', text)
    hit = _cache_read(path, CACHE_TTL_DAYS)
    if hit is not None:
        hit.pop('_ts', None)
        return hit, True
    if key == SERVER_SENTINEL:
        result = _server_regeo(lng, lat, radius)
        result['_ts'] = time.time()
        _cache_write(path, result)
        return result, False
    data = _get('v3/geocode/regeo', API_REGEO,
                {'location': text, 'radius': radius, 'extensions': 'base', 'batch': 'false'}, key)
    rg = data.get('regeocode') or {}
    ac = rg.get('addressComponent') or {}
    loc_gcj = rg.get('location') or ''
    result = _base_result(bool(rg.get('formatted_address')),
                          lng=lng, lat=lat,
                          address=rg.get('formatted_address') or '',
                          city=ac.get('city') or ac.get('province') or '',
                          district=ac.get('district') or '',
                          township=ac.get('township') or '',
                          street=ac.get('streetNumber', {}).get('street') or '',
                          number=ac.get('streetNumber', {}).get('number') or '',
                          location=loc_gcj)
    if not result['found']:
        result['address'] = ''
    result['q'] = text
    result['_ts'] = time.time()
    _cache_write(path, result)
    return result, False
