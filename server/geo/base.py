# -*- coding: utf-8 -*-
"""base.py — GeoProvider 抽象 + 统一结果结构 GeoHit。

三个能力（高德/天地图都具备，只是接口形态不同）：
    search(text, city)      POI 关键字检索（命中率高，优先用于"徐汇区宜山路600号"这类地址）
    geocode(text, city)     结构化地理编码（search 未命中时兜底）
    regeo(lng, lat, radius) 逆地理编码（坐标 -> 地址）

统一出参 GeoHit：
    lng/lat  经度/纬度（**已按 config.GEO_OUTPUT_CRS 归一**，默认 GCJ-02，与 App/管线一致）
    crs      出参坐标系
    src_crs  provider 原生坐标系
    name/address/adname/adcode/city/district  可读字段（provider 缺项时留空）
    method   'search' | 'geocode' | 'regeo'（+ '_cached' 后缀表示命中缓存）
    note     人类可读说明（进日志，便于排查"为什么定位到这儿"）
"""


class GeoError(RuntimeError):
    """provider 不可用 / 配额耗尽 / 参数非法。"""

    def __init__(self, msg, provider=None, upstream_status=None):
        RuntimeError.__init__(self, msg)
        self.msg = msg
        self.provider = provider
        self.upstream_status = upstream_status


class GeoHit:
    def __init__(self, lng, lat, crs="GCJ-02", src_crs=None, name="", address="",
                 adname="", adcode="", city="", district="", method="", note="", raw=None,
                 township="", street="", number=""):
        self.lng = float(lng)
        self.lat = float(lat)
        self.crs = crs
        self.src_crs = src_crs or crs
        self.name = name or ""
        self.address = address or ""
        self.adname = adname or ""
        self.adcode = adcode or ""
        self.city = city or ""
        self.district = district or ""
        self.method = method
        self.note = note
        self.raw = raw
        self.township = township or ""
        self.street = street or ""
        self.number = number or ""


    def to_dict(self, with_raw=False):
        d = {
            "lng": round(self.lng, 6), "lat": round(self.lat, 6), "crs": self.crs,
            "src_crs": self.src_crs,
            "name": self.name, "address": self.address,
            "adname": self.adname, "adcode": self.adcode,
            "city": self.city, "district": self.district,
            "township": self.township, "street": self.street, "number": self.number,
            "location": "%.6f,%.6f" % (self.lng, self.lat),   # 与端上 amap 'location' 字段同形
            "method": self.method, "note": self.note,
            "found": True,                                    # 未命中走 502，不会到这里
        }
        if with_raw and self.raw is not None:
            d["raw"] = self.raw
        return d


class GeoProvider:
    name = "?"
    native_crs = "GCJ-02"

    def search(self, text, city=None):
        raise NotImplementedError

    def geocode(self, text, city=None):
        raise NotImplementedError

    def regeo(self, lng, lat, radius=1500):
        raise NotImplementedError
