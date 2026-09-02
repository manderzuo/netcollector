# -*- coding: utf-8 -*-
"""cooldown.py — 风控冷却控制（dy-sign-tool 内独立）。

三层冷却策略：
1. 请求间随机间隔(jitter) —— 模拟真人节奏,消除固定间隔机器特征
2. 批次冷却(batch_cooldown) —— 每 N 个作品后长停,稀释请求密度
3. 风控退避(retry_with_backoff) —— 识别 403/风控响应,递增等待重试
"""

from __future__ import annotations

import random
import time
from typing import Callable

from api_client import ApiError


def jitter(a: float, b: float) -> float:
    """返回 [a, b] 区间随机秒数。"""
    return random.uniform(a, b)


def pause_jitter(a: float, b: float, label: str = ""):
    """随机间隔暂停。"""
    wait = jitter(a, b)
    if label:
        print(f"  [节流] {label} {wait:.1f}s")
    time.sleep(wait)


class CooldownPolicy:
    """冷却策略配置。"""

    def __init__(self,
                 page_interval=(1.0, 5.0),      # 翻页间隔 1-5秒随机
                 comment_interval=(1.5, 3.0),   # 评论翻页间隔(兼容保留)
                 item_interval=(3.0, 6.0),      # 作品间间隔
                 batch_cooldown=(20, 40),        # 随机批次冷却时长 20-40秒
                 backoff_base=30,                # 风控退避基础秒数
                 max_retries=3,                  # 风控最大重试次数
                 extra_jitter=(0.0, 5.0),        # 额外随机冷却 0-5秒
                 batch_size=None,                # 兼容旧参（已废弃，内部随机1-10）
                 page_force_break_after=5,       # 每翻N页强制休息
                 page_force_break=(5, 10)):      # 强制休息时长(秒)
        self.page_interval = page_interval
        self.comment_interval = comment_interval
        self.item_interval = item_interval
        # 新策略：每1-10个随机进入冷却（不用固定10个）
        self._cooldown_threshold = random.randint(1, 10)
        self._cooldown_counter = 0
        self.batch_cooldown = batch_cooldown
        self.backoff_base = backoff_base
        self.max_retries = max_retries
        self.extra_jitter = extra_jitter
        # 翻页强制休息：禁止连续翻页
        self._page_count = 0
        self.page_force_break_after = page_force_break_after
        self.page_force_break = page_force_break

    # ---- 第一层:随机间隔 ----
    def wait_page(self):
        """翻页间隔：1-5秒随机 + 每N页强制休息(禁止连续翻页)。"""
        # 每页之间必须有随机间隔(1-5s)，不允许连续翻页
        pause_jitter(*self.page_interval, "翻页")
        self.wait_extra()
        # 强制休息：每翻 page_force_break_after 页，强制停 5-10s
        self._page_count += 1
        if self._page_count >= self.page_force_break_after:
            wait = random.uniform(*self.page_force_break)
            print(f"\n[翻页休息] 已连续翻 {self._page_count} 页,强制休息 {wait:.0f}s")
            time.sleep(wait)
            self._page_count = 0

    def wait_comment(self):
        """评论翻页(兼容旧调用)：同样 1-5s + 强制休息。"""
        pause_jitter(*self.page_interval, "评论翻页")
        self.wait_extra()
        self._page_count += 1
        if self._page_count >= self.page_force_break_after:
            wait = random.uniform(*self.page_force_break)
            print(f"\n[翻页休息] 已连续翻 {self._page_count} 页,强制休息 {wait:.0f}s")
            time.sleep(wait)
            self._page_count = 0

    def wait_item(self):
        pause_jitter(*self.item_interval, "切换作品")
        self.wait_extra()

    def wait_extra(self):
        """0-5s 额外随机冷却。"""
        pause_jitter(*self.extra_jitter, "额外冷却")

    # ---- 第二层:随机批次冷却 ----
    def maybe_batch_cooldown(self, done_count: int = None) -> bool:
        """每1-10个随机进入冷却，时长20-40秒。"""
        self._cooldown_counter += 1
        if self._cooldown_counter >= self._cooldown_threshold:
            wait = random.uniform(*self.batch_cooldown)
            print(f"\n[随机冷却] 已采 {self._cooldown_counter} 个后触发,暂停 {wait:.0f}s")
            time.sleep(wait)
            # 重置为新的随机阈值
            self._cooldown_counter = 0
            self._cooldown_threshold = random.randint(1, 10)
            return True
        return False

    # ---- 第三层:风控退避重试 ----
    def retry_with_backoff(self, fn: Callable, *args, **kwargs):
        """带退避重试的请求封装。

        检测 403 / 风控特征 → 递增等待(30s/60s/90s) → 重试。
        连续失败 max_retries 次后抛出。
        """
        last_exc = None
        for attempt in range(self.max_retries):
            try:
                return fn(*args, **kwargs)
            except ApiError as exc:
                last_exc = exc
                if not self._is_risk(exc):
                    raise
                wait = self.backoff_base * (attempt + 1)
                print(f"  [风控] 请求被拦(HTTP {exc.status}),"
                      f"等待 {wait}s 重试 ({attempt + 1}/{self.max_retries})")
                time.sleep(wait)
        raise RuntimeError(f"风控重试耗尽: {last_exc}")

    @staticmethod
    def _is_risk(exc: ApiError) -> bool:
        """判断异常是否为风控(403 / 验证码 / 风控文案)。"""
        if exc.status == 403:
            return True
        body = (exc.body or "").lower()
        return any(k in body for k in (
            "captcha", "verify", "滑块", "验证码", "risk",
            "frequently", "too many", "访问频繁", "操作频繁",
        ))
