# -*- coding: utf-8 -*-
"""amap.py — 高德 Web 服务 API provider（当前默认实现）。

检索顺序与 mobile 侧 app/amap.py 完全一致，保证归集后定位结果不变：
    v3/place/text（POI 检索，适合"徐汇区宜山路600号"这类非结构化地址）
    -> 未命中回落 v3/geocode/geo（结构化地理编码）
逆地理：v3/geocode/regeo。
坐标系：高德原生 GCJ-02，与出参默认坐标系一致（零换算）。

安全：key 只在 config 里出现；日志/异常消息一律经 audit._mask 脱敏；
      缓存文件只存结果字段，不含 key / URL。
"""
import hashlib
import json
import os
import re
import time
import urllib.parse

import config
from core import audit
from core.http_util import UpstreamError, upstream

from .base import GeoError, GeoHit, GeoProvider

API_PLACE = "https://restapi.amap.com/v3/place/text"
API_GEO = "https://restapi.amap.com/v3/geocode/geo"
API_REGEO = "https://restapi.amap.com/v3/geocode/regeo"
CITY_DEFAULT = "上海"

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cache", "geo")


def _norm(text):
    return re.sub(r"\s+", "", text or "")


def _cache_path(tag, text):
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]
    return os.path.join(CACHE_DIR, "%s_%s.json" % (tag, h))


def _cache_read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        if time.time() - float(d.get("_ts", 0)) > config.GEO_CACHE_TTL_S:
            return None
        return d
    except Exception:
        return None


def _cache_write(path, d):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception as e:  # 缓存失败不影响主流程
        audit.warn("geo cache write failed: %s" % e)


def _split_loc(loc_str):
    parts = (loc_str or "").split(",")
    if len(parts) < 2:
        return None, None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None, None


class AmapProvider(GeoProvider):
    name = "amap"
    native_crs = "GCJ-02"

    def __init__(self, key=None):
        self.key = key if key is not None else config.AMAP_KEY

    # ---------------------------------------------------------------- 内部
    def _get(self, endpoint, url_base, params):
        if not self.key:
            raise GeoError("服务端未配置高德 key（请在 server/.env 里填 HH_AMAP_KEY）",
                           provider=self.name)
        q = dict(params)
        q["key"] = self.key
        q["output"] = "json"
        url = url_base + "?" + urllib.parse.urlencode(q)
        try:
            _status, _hdrs, text, _elapsed = upstream(url, method="GET", timeout=config.GEO_TIMEOUT_S)
            data = json.loads(text)
        except UpstreamError as e:
            raise GeoError("高德 %s 调用失败: %s" % (endpoint, e.msg), provider=self.name,
                           upstream_status=e.upstream_status)
        except Exception as e:  # noqa
            raise GeoError("高德 %s 调用失败: %s: %s" % (endpoint, type(e).__name__, e), provider=self.name)
        if str(data.get("status")) != "1":
            raise GeoError("高德 %s 返回非 OK: infocode=%s info=%s"
                           % (endpoint, data.get("infocode"), data.get("info")), provider=self.name)
        return data

    # ---------------------------------------------------------------- 能力
    def search(self, text, city=None):
        """v3/place/text。-> GeoHit | None（未命中返回 None，不抛异常）。"""
        t = _norm(text)
        if not t:
            return None
        path = _cache_path("place", t)
        hit = _cache_read(path)
        if hit is not None:
            return self._from_cache(hit, "search")
        data = self._get("v3/place/text", API_PLACE,
                         {"keywords": t, "city": city or CITY_DEFAULT, "citylimit": "true",
                          "offset": 5, "page": 1})
        pois = data.get("pois") or []
        rec = {"found": False, "q": t, "_ts": time.time()}
        if pois:
            p = pois[0]
            lng, lat = _split_loc(p.get("location"))
            if lng is not None:
                rec = {"found": True, "q": t, "_ts": time.time(),
                       "lng": lng, "lat": lat,
                       "name": p.get("name") or "", "address": p.get("address") or "",
                       "adname": p.get("adname") or "", "adcode": p.get("adcode") or ""}
        _cache_write(path, rec)
        return self._from_cache(rec, "search", cached=False)

    def geocode(self, text, city=None):
        """v3/geocode/geo。-> GeoHit | None。"""
        t = _norm(text)
        if not t:
            return None
        path = _cache_path("geo", t)
        hit = _cache_read(path)
        if hit is not None:
            return self._from_cache(hit, "geocode")
        data = self._get("v3/geocode/geo", API_GEO,
                         {"address": t, "city": city or CITY_DEFAULT, "citylimit": "true"})
        gs = data.get("geocodes") or []
        rec = {"found": False, "q": t, "_ts": time.time()}
        if gs:
            g = gs[0]
            lng, lat = _split_loc(g.get("location"))
            if lng is not None:
                rec = {"found": True, "q": t, "_ts": time.time(),
                       "lng": lng, "lat": lat,
                       "name": "", "address": g.get("formatted_address") or "",
                       "adname": g.get("district") or "", "adcode": g.get("adcode") or ""}
        _cache_write(path, rec)
        return self._from_cache(rec, "geocode", cached=False)

    def regeo(self, lng, lat, radius=1500):
        """v3/geocode/regeo。-> GeoHit | None。"""
        lng, lat = round(float(lng), 6), round(float(lat), 6)
        text = "%.6f,%.6f" % (lng, lat)
        path = _cache_path("regeo", text)
        hit = _cache_read(path)
        if hit is not None:
            return self._from_cache(hit, "regeo")
        data = self._get("v3/geocode/regeo", API_REGEO,
                         {"location": text, "radius": int(radius), "extensions": "base", "batch": "false"})
        rg = data.get("regeocode") or {}
        ac = rg.get("addressComponent") or {}
        addr = rg.get("formatted_address") or ""
        if isinstance(addr, (list, dict)):
            addr = ""
        rec = {"found": bool(addr), "q": text, "_ts": time.time(),
               "lng": lng, "lat": lat, "address": addr,
               "city": ac.get("city") or ac.get("province") or "",
               "district": ac.get("district") or "",
               "township": ac.get("township") or "",
               "adcode": ac.get("adcode") or "",
               "street": (ac.get("streetNumber") or {}).get("street") or "",
               "number": (ac.get("streetNumber") or {}).get("number") or ""}
        n_lng, n_lat = _split_loc(rg.get("location"))
        if n_lng is not None:
            rec["lng"], rec["lat"] = n_lng, n_lat      # 高德吸附后的精确 GCJ-02
            rec["snapped"] = True
        _cache_write(path, rec)
        return self._from_cache(rec, "regeo", cached=False)

    # ---------------------------------------------------------------- 组装
    def _from_cache(self, rec, method, cached=None):
        if cached is None:
            cached = True
        if not rec.get("found"):
            return None
        m = method + ("_cached" if cached else "")
        where = (rec.get("adname") or "") + (rec.get("district") or "")
        note = "高德 %s 命中「%s」%s%s" % (
            method, rec.get("name") or rec.get("address") or "?", where,
            "（缓存命中）" if cached else "")
        return GeoHit(rec["lng"], rec["lat"], crs=self.native_crs, src_crs=self.native_crs,
                      name=rec.get("name", ""), address=rec.get("address", ""),
                      adname=rec.get("adname", ""), adcode=rec.get("adcode", ""),
                      city=rec.get("city", ""), district=rec.get("district", ""),
                      township=rec.get("township", ""), street=rec.get("street", ""),
                      number=rec.get("number", ""),
                      method=m, note=note)
