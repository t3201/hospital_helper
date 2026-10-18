# -*- coding: utf-8 -*-
"""挂号推荐运行期原型（纯标准库、PC 优先、目标手机端）。

流水线: locate → dialogue → infer → candidates(stage-1) → slots(缓存优先) → recommend → book(dry-run)
复用(禁止重写): ranking/scripts/score_hospitals.py (stage-1)、slots/fetch_slots.py (live 抓取, 需设备)
权威契约: ranking/RANKING_SPEC.md
"""
__version__ = '0.1.0'
