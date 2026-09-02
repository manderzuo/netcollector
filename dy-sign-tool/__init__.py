# -*- coding: utf-8 -*-
"""dy-sign-tool — 抖音 a_bogus 签名工具（独立自包含）。

本工具完全独立于「多平台采集工作台」，可单独拷贝到任何机器运行。
依赖：Python 3.7+，requests（仅 msToken 用）；签名本体零第三方依赖。

目录结构：
  dy-sign-tool/
  ├─ dy_sign/               # 签名核心包（纯算法，零依赖）
  │  ├─ ab_pure.py          # a_bogus 纯 Python 实现
  │  ├─ sm3.py              # SM3 国密哈希
  │  ├─ mstoken.py          # msToken 获取（requests）
  │  ├─ fingerprint.py      # 浏览器指纹档案
  │  ├─ xbogus_pure.py      # X-Bogus 老版签名（备用）
  │  ├─ strdata_pure.py     # strData 上报体生成
  │  └─ dy_sign_service.py  # HTTP 签名服务
  ├─ poc_dy_sign.py         # POC 验证脚本
  ├─ get_real_vids.py       # 辅助：从抖音页提取真实视频 ID
  ├─ inspect_dy_page.py     # 辅助：检查页面状态
  └─ README.md              # 使用说明

用法：
  1. 启动签名服务：  python -m dy_sign.dy_sign_service --port 8765
  2. 健康检查：      curl http://127.0.0.1:8765/health
  3. 生成签名：      curl "http://127.0.0.1:8765/sign?url=https%3A%2F%2F..."
  4. POC 验证：      python poc_dy_sign.py --sign http://127.0.0.1:8765
"""
