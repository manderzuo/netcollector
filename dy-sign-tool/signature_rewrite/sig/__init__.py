# -*- coding: utf-8 -*-
"""sig 包 — 抖音签名算法独立实现（clean-room 重写版）。

模块清单：
- digest.py      国密 SM3 摘要
- env_profile.py 浏览器环境指纹
- a_bogus.py     a_bogus 签名
- x_bogus.py     X-Bogus 签名
- ms_token.py    msToken 获取
- report_body.py 上报体构造
"""

from .a_bogus import ABogusSigner
from .x_bogus import XBogusGenerator
from .ms_token import fetch_ms_token

__all__ = ["ABogusSigner", "XBogusGenerator", "fetch_ms_token"]
