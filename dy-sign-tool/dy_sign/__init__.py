# -*- coding: utf-8 -*-
"""dy_sign 包：抖音签名服务（a_bogus / msToken）。

独立自包含的纯 Python 签名实现，不依赖外部采集框架。

主要接口：
- sign_a_bogus(url) -> str          生成 a_bogus
- sign_a_bogus_for(api_path, query) 按接口路径 + query 拼 URL 后签名
- get_mstoken(ttwid) -> str         获取/缓存 msToken
- sign_url(url, ttwid) -> dict      一键：a_bogus + msToken
"""
