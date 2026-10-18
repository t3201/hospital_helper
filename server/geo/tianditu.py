# -*- coding: utf-8 -*-
"""tianditu.py — 天地图 provider（预留实现）。

用途：以后把内部实现从高德换成天地图时，只改 config.GEO_PROVIDER="tianditu"
（或环境变量 HH_GEO_PROVIDER=tianditu）+ 配 HH_TIANDITU_KEY，调用方零改动。

接口对应关系：
    search   -> DataServer?T=search      （关键字 POI 检索）
    geocode  -> geocoder?type=geocode    （地址 -> 坐标）
    regeo    -> geocoder?type=geocode 反向（坐标 -> 地址，ver=1）
坐标系：天地图原生 **CGCS2000**（与 WGS-84 差异 < 1m）。本 provider 如实上报 src_crs，
由 geo/__init__.resolve() 统一换算到 config.GEO_OUTPUT_CRS（默认 GCJ-02），
因此上层（高德坐标系的评分/距离计算）语义不变。
"""
import json
import re
import urllib.parse

import config
from core.http_util import UpstreamError, upstream

from .base import GeoError, GeoHit, GeoProvider

API_GEOCODER = "http://api.tianditu.gov.cn/geocoder"
API_DATASERVER = "http://api.tianditu.gov.cn/DataServer"


def _norm(text):
    return re.sub(r"\s+", "", text or "")


class TiandituProvider(GeoProvider):
    name = "tianditu"
    native_crs = "CGCS2000"

    def __init__(self, key=None):
        self.key = key if key is not None else config.TIANDITU_KEY

    def _require_key(self):
        if not self.key:
            raise GeoError("服务端未配置天地图 key（请在 server/.env 里填 HH_TIANDITU_KEY，"
                           "并把 HH_GEO_PROVIDER 设为 tianditu）", provider=self.name)

    def _call(self, url, params):
        self._require_key()
        q = dict(params)
        q["tk"] = self.key
        full = url + "?" + urllib.parse.urlencode(q)
        try:
            _status, _hdrs, text, _elapsed = upstream(full, method="GET", timeout=config.GEO_TIMEOUT_S)
            data = json.loads(text)
        except UpstreamError as e:
            raise GeoError("天地图调用失败: %s" % e.msg, provider=self.name,
                           upstream_status=e.upstream_status)
        except Exception as e:  # noqa
            raise GeoError("天地图调用失败: %s: %s" % (type(e).__name__, e), provider=self.name)
        # 天地图错误形态多样：{"msg":"...","code":"..."} / {"error":..} / 空结果
        msg = str(data.get("msg") or data.get("message") or "")
        if data.get("code") not in (None, "", 0, "0", "success") and not data.get("location") \
                and not data.get("searchList"):
            raise GeoError("天地图返回异常: code=%s msg=%s" % (data.get("code"), msg), provider=self.name)
        return data

    # ---------------------------------------------------------------- 能力
    def geocode(self, text, city=None):
        t = _norm(text)
        if not t:
            return None
        ds = {"keyWord": t, "level": 16}
        data = self._call(API_GEOCODER, {"ds": json.dumps(ds, ensure_ascii=False), "type": "geocode"})
        loc = data.get("location") or {}
        lng, lat = loc.get("lon"), loc.get("lat")
        if lng in (None, "") or lat in (None, ""):
            return None
        return GeoHit(float(lng), float(lat), crs=self.native_crs, src_crs=self.native_crs,
                      address=data.get("formatted_address") or t,
                      method="geocode", note="天地图 geocode 命中「%s」" % (data.get("formatted_address") or t))

    def regeo(self, lng, lat, radius=1500):
        ds = {"lon": round(float(lng), 6), "lat": round(float(lat), 6), "ver": 1}
        data = self._call(API_GEOCODER, {"postStr": json.dumps(ds, ensure_ascii=False), "type": "geocode"})
        loc = data.get("location") or {}
        addr = data.get("formatted_address") or ""
        if isinstance(addr, (list, dict)):
            addr = ""
        if not addr and not loc:
            return None
        # 逆地理时天地图回显的坐标即入参坐标（CGCS2000）
        out_lng = float(loc.get("lon") or lng)
        out_lat = float(loc.get("lat") or lat)
        return GeoHit(out_lng, out_lat, crs=self.native_crs, src_crs=self.native_crs,
                      address=addr, method="regeo", note="天地图 regeo 命中「%s」" % addr)

    def search(self, text, city=None):
        t = _norm(text)
        if not t:
            return None
        ds = {"searchWord": t, "needPoi": "1", "needExpand": "A", "queryType": "1",
              "startRecordNum": "0", "recordCountPerPage": "5"}
        if city:
            ds["city"] = city
        data = self._call(API_DATASERVER, {"T": "search", "ds": json.dumps(ds, ensure_ascii=False)})
        lst = data.get("searchList") or data.get("pois") or []
        if not lst:
            return None
        p = lst[0]
        # 天地图 POI 坐标字段形态多样：lon/lat 或 screenPoint "x,y"
        lng, lat = p.get("lon"), p.get("lat")
        if lng in (None, "") or lat in (None, ""):
            sp = str(p.get("screenPoint") or p.get("mapPosition") or "")
            parts = sp.split(",")
            if len(parts) == 2:
                lng, lat = parts
        if lng in (None, "") or lat in (None, ""):
            return None
        addr = p.get("addr") or p.get("name") or ""
        return GeoHit(float(lng), float(lat), crs=self.native_crs, src_crs=self.native_crs,
                      name=p.get("name") or "", address=addr,
                      adname=p.get("city") or "", district=p.get("district") or "",
                      method="search", note="天地图 search 命中「%s」" % (p.get("name") or addr))
