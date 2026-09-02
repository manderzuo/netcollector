# -*- coding: utf-8 -*-
"""test_cooldown.py — 冷却模块验证。"""

import sys
import time

sys.path.insert(0, ".")
from cooldown import CooldownPolicy
from api_client import DouyinApiClient, ApiError


def test_jitter():
    """测试随机间隔在范围内。"""
    policy = CooldownPolicy()
    for _ in range(5):
        w = policy.page_interval
        assert w[0] <= w[1], "区间非法"
    print("[1] 冷却配置合法")


def test_batch_cooldown_trigger():
    """测试批次冷却触发逻辑（不实际等待）。"""
    policy = CooldownPolicy(batch_size=10, batch_cooldown=(0.01, 0.02))
    # 第 10 个应触发
    start = time.time()
    triggered = policy.maybe_batch_cooldown(10)
    assert triggered, "第10个应触发批次冷却"
    assert time.time() - start < 1, "测试冷却不应等太久"
    # 第 5 个不应触发
    triggered2 = policy.maybe_batch_cooldown(5)
    assert not triggered2, "第5个不应触发"
    print("[2] 批次冷却触发逻辑正确")


def test_retry_backoff():
    """测试风控退避：模拟 403 重试。"""
    policy = CooldownPolicy(backoff_base=0.01, max_retries=2)

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise ApiError(403, "HTTP 403", "captcha required")
        return "ok"

    result = policy.retry_with_backoff(flaky)
    assert result == "ok", "重试后应成功"
    assert calls["n"] == 2, "应调用 2 次"
    print("[3] 风控退避重试正确")


def test_client_backoff():
    """测试客户端级退避：模拟 403。"""
    import unittest.mock as mock
    client = DouyinApiClient(cookie_str="x", backoff=True, max_retries=2, backoff_base=0.01)
    calls = {"n": 0}

    def fake_get(url):
        calls["n"] += 1
        if calls["n"] < 2:
            raise ApiError(403, "HTTP 403", "verify")
        return '{"ok": true}'

    client._http_get = fake_get
    body = client.request_raw("https://x.com/api", {"a": "1"})
    assert body == '{"ok": true}', "重试后应成功"
    assert calls["n"] == 2
    print("[4] 客户端风控退避正确")


if __name__ == "__main__":
    test_jitter()
    test_batch_cooldown_trigger()
    test_retry_backoff()
    test_client_backoff()
    print("\n[PASS] 冷却模块全部验证通过!")
