# -*- coding: utf-8 -*-
"""router.py — 极简路由注册表。

新增接口只需：
    from core.router import route
    @route("POST", r"^/v1/foo$")
    def foo(ctx): ...
并在 api/__init__.py 的 register() 里 import 对应模块。
"""
import re

ROUTES = []   # [(methods:set, regex, handler, opts)]


def route(methods, pattern, **opts):
    """装饰器：注册 handler 到 (methods, pattern)。

    methods: "GET" | "POST" | "GET,POST"
    opts:    body_limit=..., name=...
    """
    if isinstance(methods, str):
        methods = {m.strip().upper() for m in methods.split(",") if m.strip()}
    else:
        methods = {str(m).upper() for m in methods}
    regex = re.compile(pattern)

    def deco(fn):
        ROUTES.append((methods, regex, fn, opts))
        return fn

    return deco


def resolve(method, path):
    """-> (handler, params, opts) | ('405', allowed, None) | (None, None, None)"""
    allowed = set()
    for methods, regex, fn, opts in ROUTES:
        m = regex.match(path)
        if not m:
            continue
        if method.upper() in methods:
            try:
                params = m.groupdict()
            except Exception:
                params = {"_groups": m.groups()}
            return fn, params, opts
        allowed |= methods
    if allowed:
        return "405", sorted(allowed), None
    return None, None, None


def describe():
    """路由自描述：正则路径 + 处理函数 + docstring 首行（便于 /v1/routes 直接读）。"""
    out = []
    for methods, regex, fn, opts in ROUTES:
        doc = (getattr(fn, "__doc__", "") or "").strip().splitlines()
        out.append({
            "methods": sorted(methods),
            "path": regex.pattern,
            "handler": getattr(fn, "__name__", "?"),
            "desc": doc[0] if doc else "",
            "name": opts.get("name", ""),
        })
    return out
