# -*- coding: utf-8 -*-
"""coords.py — 坐标系换算（公开算法，不耗任何 API 配额）。

  WGS-84 / CGCS2000（GPS、天地图原生）  <->  GCJ-02（高德、腾讯）
说明：CGCS2000 与 WGS-84 在当前毫米级精度下差异 < 1m，业务上按同一坐标系处理。

公式与 mobile/controller-app/app/python/app/locate.py:wgs84_to_gcj02 保持一致
（2026-10-06 修复版：分母必须 *180/pi，否则偏移被放大 180 倍）。
"""
import math

A = 6378245.0
EE = 0.00669342162296594323


def _tlat(x, y):
    r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    r += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return r


def _tlng(x, y):
    r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    r += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return r


def out_of_china(lng, lat):
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def wgs84_to_gcj02(lng, lat):
    if out_of_china(lng, lat):
        return lng, lat
    dlat = _tlat(lng - 105.0, lat - 35.0)
    dlng = _tlng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((A * (1 - EE) / magic / sqrtmagic) * 180.0 / math.pi)
    dlng = (dlng * 180.0) / ((A / sqrtmagic * math.cos(radlat)) * 180.0 / math.pi)
    return lng + dlng, lat + dlat


def gcj02_to_wgs84(lng, lat, iters=6):
    """逆向近似：不动点迭代（误差 < 1e-7 度）。"""
    if out_of_china(lng, lat):
        return lng, lat
    wlng, wlat = lng, lat
    for _ in range(iters):
        gl, ga = wgs84_to_gcj02(wlng, wlat)
        wlng += lng - gl
        wlat += lat - ga
        if abs(lng - gl) < 1e-9 and abs(lat - ga) < 1e-9:
            break
    return wlng, wlat


def convert(lng, lat, src, dst):
    """按坐标系名换算，返回 (lng, lat, dst)。未知坐标系名 -> 原样返回并保留名字。"""
    src = (src or "").upper()
    dst = (dst or "").upper()
    if not src or not dst or src == dst:
        return lng, lat, (dst or src or "GCJ-02")
    if src in ("WGS-84", "WGS84", "CGCS2000") and dst in ("GCJ-02", "GCJ02"):
        lng, lat = wgs84_to_gcj02(lng, lat)
        return lng, lat, "GCJ-02"
    if src in ("GCJ-02", "GCJ02") and dst in ("WGS-84", "WGS84", "CGCS2000"):
        lng, lat = gcj02_to_wgs84(lng, lat)
        return lng, lat, dst
    return lng, lat, dst
