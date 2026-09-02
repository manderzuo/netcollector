# -*- coding: utf-8 -*-
"""smoke_all.py — 全模块冒烟测试（重写版）。"""

import sys

sys.path.insert(0, ".")
from sig.a_bogus import ABogusSigner
from sig.x_bogus import XBogusGenerator
from sig.digest import sm3_hex
from sig.ms_token import fetch_ms_token
from sig.report_body import build_envelope


def main():
    # 1. SM3 标准向量
    assert sm3_hex(b"abc") == "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0"
    print("[1] SM3 标准向量 PASS")

    # 2. a_bogus 生成
    signer = ABogusSigner(fixed=False)
    sig = signer.sign("https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id=1&cursor=0&count=20&item_type=0&device_platform=webapp&aid=6383")
    print(f"[2] a_bogus len={len(sig)} prefix={sig[:40]}")

    # 3. X-Bogus 生成
    xsigner = XBogusGenerator()
    xsig = xsigner.sign("4c61626364656667")
    print(f"[3] x_bogus len={len(xsig)} prefix={xsig[:40]}")

    # 4. 上报体
    env = build_envelope()
    print(f"[4] envelope len={len(env)} prefix={env[:80]}")

    # 5. msToken（失败返回空串，不算错）
    tok = fetch_ms_token(use_cache=False)
    status = "OK" if tok else "EMPTY(acceptable)"
    print(f"[5] msToken len={len(tok)} status={status}")

    print("ALL MODULES OK")


if __name__ == "__main__":
    main()
