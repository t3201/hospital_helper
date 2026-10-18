# -*- coding: utf-8 -*-
"""api/location.py — 地理位置网关 /v1/location。

App 侧不再持有高德 key；只调本接口，内部转调高德（或以后改天地图）。

    POST /v1/location
    {
      "text":  "徐汇区宜山路600号",     # 地址/POI 文本（与 lng+lat 二选一）
      "city":  "上海",                  # 可选，限定城市（提高命中率）
      "lng": 121.47, "lat": 31.23,      # 给了坐标 -> 逆地理编码
      "mode": "auto|search|geocode|regeo",   # 默认 auto（按入参自动选）
      "radius": 1500,                   # 逆地理半径（米）
      "crs":  "GCJ-02"                  # 出参坐标系，默认 config.GEO_OUTPUT_CRS
    }
    GET /v1/location?text=徐家汇&city=上海        # 等价（便于浏览器自测）

出参（统一结构，字段与 mobile 侧 app/locate.py 的 resolve() 对齐）：
    {ok, provider, mode, method, lng, lat, crs, src_crs,
     name, address, adname, adcode, city, district, note, elapsed_ms}
"""
import re

import config
from core import audit
from core.http_util import HttpError
from core.router import route
from geo import GeoError, resolve

# "121.47,31.23" / "121.47 31.23" / "121.47，31.23"
_LOC_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,\s，]\s*(-?\d+(?:\.\d+)?)\s*$")


def _to_float(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise HttpError(400, "坐标必须是数字: %r" % (v,))


def _parse_input(ctx):
    body = {}
    try:
        body = ctx.json(required=False) or {}
    except HttpError:
        body = {}
    text = None
    for k in ("text", "address", "query", "keywords", "q", "loc_text"):
        v = body.get(k)
        if isinstance(v, str) and v.strip():
            text = v.strip()
            break
    if not text:
        text = (ctx.q("text") or ctx.q("address") or ctx.q("q") or "").strip() or None

    lng = _to_float(body.get("lng", body.get("lon")))
    lat = _to_float(body.get("lat"))
    if lng is None:
        lng = _to_float(ctx.q("lng", ctx.q("lon", "")))
    if lat is None:
        lat = _to_float(ctx.q("lat", ""))

    # text 写成 "lng,lat" 的情况（App 自动定位回填 loctext 就是这个形态）
    if lng is None and text:
        m = _LOC_RE.match(text)
        if m:
            lng, lat = float(m.group(1)), float(m.group(2))
    city = None
    for k in ("city", "adname"):
        v = body.get(k)
        if isinstance(v, str) and v.strip():
            city = v.strip()
            break
    if not city:
        city = (ctx.q("city") or "").strip() or None
    mode = (body.get("mode") or ctx.q("mode") or "auto").strip().lower()
    radius = body.get("radius") or ctx.q("radius", "")
    try:
        radius = int(radius) if radius not in (None, "") else 1500
    except (TypeError, ValueError):
        radius = 1500
    crs = (body.get("crs") or ctx.q("crs") or "").strip() or None
    return text, lng, lat, city, mode, radius, crs


def _emit(ctx, hit, mode, provider_name, elapsed_ms):
    out = hit.to_dict()
    out.update({"ok": True, "provider": provider_name, "mode": mode,
                "elapsed_ms": elapsed_ms, "public_base": config.PUBLIC_BASE})
    ctx.send_json(out, 200)
    return out


@route("POST,GET", r"^/v1/location/?$")
def location(ctx):
    """地理位置网关：text 检索 / lng+lat 逆地理，内部转调高德（以后可换天地图）。"""
    import time

    text, lng, lat, city, mode, radius, crs = _parse_input(ctx)
    prov_name = (config.GEO_PROVIDER or "amap").strip().lower()
    key_env = "HH_TIANDITU_KEY" if prov_name == "tianditu" else "HH_AMAP_KEY"
    key_val = config.TIANDITU_KEY if prov_name == "tianditu" else config.AMAP_KEY
    if not key_val:
        # 没配 key 属于服务端配置问题：503 且不可重试（重试也没用），不去打上游
        raise HttpError(503, "服务端未配置地理位置 key（请在 server/.env 里填 %s）" % key_env,
                        provider=prov_name, retryable=False)
    if mode not in ("auto", "search", "geocode", "regeo"):
        raise HttpError(400, "mode 只支持 auto/search/geocode/regeo: %r" % mode)
    if mode == "auto" and not text and lng is None:
        raise HttpError(400, "缺少定位入参：text（地址/POI）或 lng+lat（坐标）")
    t0 = time.time()
    try:
        hit = resolve(text=text, lng=lng, lat=lat, city=city, radius=radius, crs=crs, mode=mode)
    except GeoError as e:
        audit.error("location failed: %s" % e.msg)
        ctx.send_error_json(502, e.msg, provider=e.provider or prov_name, retryable=True)
        return None
    elapsed = int((time.time() - t0) * 1000)
    used = "regeo" if hit.method.startswith("regeo") else hit.method.split("_")[0]
    audit.info("location ok: mode=%s method=%s -> %.6f,%.6f %dms"
               % (used, hit.method, hit.lng, hit.lat, elapsed))
    _emit(ctx, hit, used, prov_name, elapsed)
    return None


@route("GET", r"^/v1/location/providers/?$")
def location_providers(ctx):
    """自描述：当前 provider 与可切换列表（联调/运维用）。"""
    from geo import available_providers, provider

    p = provider()
    ctx.send_json({
        "ok": True,
        "current": config.GEO_PROVIDER,
        "available": available_providers(),
        "native_crs": p.native_crs,
        "output_crs": config.GEO_OUTPUT_CRS,
        "key_configured": bool(config.AMAP_KEY if p.name == "amap" else config.TIANDITU_KEY),
        "cache_ttl_s": config.GEO_CACHE_TTL_S,
    }, 200)
