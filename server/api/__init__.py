# -*- coding: utf-8 -*-
"""api 包：对外接口。新增接口 = 新加一个模块 + 在这里 import。

顺序约定：具体路径在前，宽泛路径（如 ^/?$）在后；router 取第一个命中。
"""


def register():
    from api import health      # noqa: F401  /  /healthz  /v1/routes
    from api import infer       # noqa: F401  /v1/infer
    from api import location    # noqa: F401  /v1/location
    from api import apk         # noqa: F401  /v1/app/*
