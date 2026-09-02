# -*- coding: utf-8 -*-
"""env_profile.py — 浏览器环境指纹档案。

提供签名算法所需的浏览器环境快照：
- User-Agent 与 Sec-CH-UA 头
- 视口/屏幕几何数据（签名载荷中编码的分辨率信息）
- 硬件并发数、设备内存、WebGL 供应商

设计说明：
- 进程级单例缓存：同一进程内所有签名共用同一指纹，保证一致性。
- 几何/GPU 从预设集合中随机选取，模拟真实浏览器多样性。
- 这些数值是签名算法的输入事实，属于功能必需参数。
"""

import random as _rnd

# 常见桌面视口几何预设：(innerWidth, innerHeight, outerWidth, outerHeight,
#                          availWidth, availHeight, screenWidth, screenHeight)
_GEOMETRY_PRESETS = (
    (1920, 937, 1920, 1040, 1920, 1040, 1920, 1080),
    (1366, 637, 1366, 728, 1366, 728, 1366, 768),
    (1536, 737, 1536, 824, 1536, 824, 1536, 864),
    (1440, 773, 1440, 860, 1440, 860, 1440, 900),
    (1280, 593, 1280, 680, 1280, 680, 1280, 720),
)

# 常见 GPU 组合：(vendor, renderer)
_GPU_PRESETS = (
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 (0x00002786) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002503) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) UHD Graphics 770 (0x00004680) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon RX 6600 (0x000073FF) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
)

_CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

_profile_cache = None


def browser_profile() -> dict:
    """获取进程级浏览器环境档案（惰性单例）。"""
    global _profile_cache
    if _profile_cache is None:
        geometry = _rnd.choice(_GEOMETRY_PRESETS)
        gpu = _rnd.choice(_GPU_PRESETS)
        _profile_cache = {
            "ua": _CHROME_UA,
            "sec_ch_ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
            "sec_ch_ua_platform": '"Windows"',
            "browser_name": "Chrome",
            "browser_version": "150.0.0.0",
            "engine_name": "Blink",
            "engine_version": "150.0.0.0",
            "os_name": "Windows",
            "os_version": "10",
            "platform": "Win32",
            "cpu_core_num": "12",
            "device_memory": "8",
            "geometry": geometry,
            "webgl_vendor": gpu[0],
            "webgl_renderer": gpu[1],
            "screen_width": str(geometry[6]),
            "screen_height": str(geometry[7]),
        }
    return _profile_cache
