# -*- coding: utf-8 -*-
"""geo 门面：provider 选择 + 坐标系归一 + 检索回落链。

对外只用一个函数：
    resolve(text=..., city=..., lng=..., lat=..., radius=..., crs=...) -> GeoHit
    失败抛 GeoError。

规则：
  * 给了 lng/lat            -> 逆地理（regeo）
  * 只给 text               -> search（POI）→ 未命中回落 geocode
  * 出参坐标系统一为 config.GEO_OUTPUT_CRS（默认 GCJ-02）；provider 原生坐标系
    若不同（如天地图 CGCS2000）自动换算，并在 GeoHit.src_crs 里如实标注。
"""
from . import coords
from .amap import AmapProvider
from .base import GeoError, GeoHit, GeoProvider  # noqa: F401  (对外导出)
from .tianditu import TiandituProvider

_PROVIDERS = {"amap": AmapProvider, "tianditu": TiandituProvider}
_cache = {}


def provider(name=None):
    """按名字取 provider 实例（进程内单例，缓存复用）。"""
    import config

    name = (name or config.GEO_PROVIDER or "amap").strip().lower()
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise GeoError("未知的地理位置 provider: %r（可选：%s）"
                       % (name, "/".join(sorted(_PROVIDERS))))
    if name not in _cache:
        _cache[name] = cls()
    return _cache[name]


def available_providers():
    return sorted(_PROVIDERS)


def _normalize(hit, out_crs):
    if hit is None:
        return None
    out_crs = (out_crs or "").strip().upper() or None
    if not out_crs or out_crs == (hit.src_crs or "").upper():
        hit.crs = hit.src_crs or "GCJ-02"
        return hit
    lng, lat, crs = coords.convert(hit.lng, hit.lat, hit.src_crs, out_crs)
    hit.lng, hit.lat, hit.crs = lng, lat, crs
    hit.note += "（%s→%s 已换算）" % (hit.src_crs, crs)
    return hit


def resolve(text=None, lng=None, lat=None, city=None, radius=1500, crs=None, prov=None, mode="auto"):
    """统一定位入口。返回 GeoHit；未命中/上游失败抛 GeoError。

    mode: auto（默认，按入参自动选）| search | geocode | regeo
    """
    p = provider(prov)
    out_crs = crs or None
    mode = (mode or "auto").strip().lower()

    # ---- 逆地理
    if mode == "regeo" or (mode == "auto" and lng not in (None, "") and lat not in (None, "")):
        if lng in (None, "") or lat in (None, ""):
            raise GeoError("逆地理需要 lng+lat", provider=p.name)
        hit = p.regeo(float(lng), float(lat), radius=radius)
        if hit is None:
            raise GeoError("%s 逆地理未命中: %.6f,%.6f" % (p.name, float(lng), float(lat)),
                           provider=p.name)
        return _normalize(hit, out_crs)

    # ---- 文本定位：search → geocode（mode 指定时只走那一条）
    t = (text or "").strip()
    if not t:
        raise GeoError("定位入参不足：需要 text（地址/POI）或 lng+lat（坐标）", provider=p.name)

    steps = ("search", "geocode") if mode == "auto" else (mode,)
    errs = []
    for fn_name in steps:
        fn = getattr(p, fn_name, None)
        if fn is None:
            errs.append("%s: provider 不支持" % fn_name)
            continue
        try:
            hit = fn(t, city=city)
        except GeoError as e:
            errs.append("%s: %s" % (fn_name, e.msg))
            continue
        if hit is not None:
            return _normalize(hit, out_crs)
    if errs:
        raise GeoError("%s 定位失败 -> %s" % (p.name, " | ".join(errs)), provider=p.name)
    raise GeoError("%s 未命中「%s」（%s 均无结果）" % (p.name, t, "/".join(steps)), provider=p.name)
