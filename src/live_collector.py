#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_collector.py — 真实 BitBrowser 采集器，实现 Collector 接口供调度器/GUI 使用。

把 dy_collect（抖音）与 xhs_collect3（小红书）封装成 scheduler.Collector：
  · search(keyword, platform)         -> [{"vid","url","title","author"}, ...]
  · fetch_comments(vid, account)      -> [{"user_id","nickname","content","comment_time","region",...}]

窗口连接：平台 → data/<platform>_window.txt（第一行窗口id，最后一行 ws）。
每个平台一个后台 asyncio 事件循环 + 共享 CdpSession，凭锁把多 worker 的同步调用
串行调度到该循环（单窗口天然串行；多账号并行由 scheduler 的多窗口驱动）。

预留：HumanBlock 时抛 HumanInterventionRequired（P7 人工接管）。
"""

import asyncio
import json
import os
import random
import threading
import time


def _is_connection_closed_error(exc: BaseException) -> bool:
    """判断是否为浏览器/CDP 连接已经失效，而不是平台页面业务错误。

    BitBrowser 关闭窗口后，websockets 可能抛出 ``ConnectionClosedError``，
    也可能只留下 ``no close frame received or sent`` 文本。两种情况都需要
    重新获取窗口的 CDP 地址；不能把它当作评论采集失败直接落库。
    """
    try:
        from websockets.exceptions import ConnectionClosed
    except Exception:  # pragma: no cover - websockets 是运行时依赖
        ConnectionClosed = ()
    if ConnectionClosed and isinstance(exc, ConnectionClosed):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = (
        "connectionclosed",
        "connection closed",
        "no close frame",
        "websocket is closed",
        "websocket connection is closed",
        "not connected",
        "broken pipe",
        "connection reset",
    )
    return any(marker in text for marker in markers)

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
DATA = os.path.join(PROJECT_ROOT, "data")

try:  # 支持 import src.live_collector 与 GUI 直接运行两种方式
    from . import dy_collect  # type: ignore
    from . import xhs_collect3  # type: ignore
    from . import kuaishou_collect  # type: ignore
    from .scheduler import Collector, HumanInterventionRequired  # type: ignore
except ImportError:
    import dy_collect  # noqa: E402
    import xhs_collect3  # noqa: E402
    import kuaishou_collect  # noqa: E402
    from scheduler import Collector, HumanInterventionRequired  # noqa: E402

PLATFORM_ALIAS = {"douyin": "douyin", "dy": "douyin",
                  "xhs": "xhs", "xiaohongshu": "xhs",
                  "kuaishou": "kuaishou", "ks": "kuaishou", "快手": "kuaishou"}


class CollectorTimeoutError(TimeoutError):
    """带采集阶段上下文的超时，避免日志只剩一个空的 ``TimeoutError``。"""

    def __init__(self, operation: str, timeout=None, cause=None):
        self.operation = str(operation or "采集操作")
        self.timeout = timeout
        # LiveCollector 在同一个作品上重建连接并重试一次后置位；scheduler
        # 看到这个标记就暂停等待人工继续，避免再次无限重复超长等待。
        self.connection_retry_attempted = False
        if timeout is None:
            message = f"{self.operation} 内部超时（CDP/页面操作超时）"
        else:
            message = f"{self.operation} 超时（等待 {float(timeout):g} 秒）"
        if cause and str(cause):
            message += f"：{cause}"
        super().__init__(message)

# ---------------------------------------------------------------------------
# 小红书节奏下限（代码内强制，不依赖任务参数）
#
# 实测小红书是各平台里最容易触发风控的：任务参数 batch_size=10 /
# cooldown_seconds=60 相当于「连续采 10 篇只歇 60 秒」，而平台限流页一旦出现，
# 旧实现会把它当成需要人工验证而冻结账号。这里按「约 1 篇/分钟」的下限强制降频，
# 并对限流做退避重试，只有连续多次仍被限流才升级为人工接管。
# ---------------------------------------------------------------------------
XHS_MIN_NOTE_INTERVAL = (55.0, 75.0)      # 两篇笔记之间的随机最小间隔
XHS_BREAK_EVERY_NOTES = 10                # 每采 N 篇进入一次长休
XHS_LONG_BREAK = (60.0, 120.0)            # 长休随机时长（秒）
XHS_RATE_LIMIT_RETRIES = 3                # 单次采集的限流重试次数
XHS_RATE_LIMIT_BACKOFF = (60.0, 90.0)     # 首次退避时长，之后逐次翻倍
XHS_RATE_LIMIT_BACKOFF_MAX = 600.0        # 单次退避上限
XHS_RATE_LIMIT_JITTER = 15.0              # 退避叠加的随机抖动上限

# 等待期间被取消/停止的哨兵返回值。
_CANCELLED = object()


class _PlatformCtx:
    """一个平台 = 一个后台事件循环 + 一个 CdpSession + 一把锁。"""

    def __init__(self, platform, ws_url=None):
        self.platform = PLATFORM_ALIAS[platform]
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.lock = threading.Lock()   # 串行化对本平台的访问
        self.session = None
        self.sid = None
        self.connect_error = None
        self.cancel_event = None
        # 小红书节奏下限：不依赖任务里的 batch_size/cooldown_seconds，
        # 保证任务参数再激进时也不会快于人工浏览节奏。
        # _PlatformCtx.lock 保证同一窗口串行，计数不会被并发 worker 重复计算。
        self._xhs_notes_since_break = 0
        self._xhs_last_note_at = 0.0
        self._xhs_note_interval = random.uniform(*XHS_MIN_NOTE_INTERVAL)
        self.ws_url = ws_url or self._load_ws()
        self.thread.start()
        # 等连接建立
        self._ready = threading.Event()
        asyncio.run_coroutine_threadsafe(self._connect(), self.loop)
        self._ready.wait(timeout=15)
        if self.session is None:
            detail = f"：{self.connect_error}" if self.connect_error else ""
            raise RuntimeError(f"{self.platform} 窗口连接失败（ws={self.ws_url}）{detail}")

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _load_ws(self):
        # 平台 -> 窗口文件：douyin用的dy_window.txt，xhs用xhs_window.txt
        fname = {"douyin": "dy_window.txt", "xhs": "xhs_window.txt",
                 "kuaishou": "kuaishou_window.txt"}[self.platform]
        path = os.path.join(DATA, fname)
        if not os.path.exists(path):
            raise RuntimeError(f"缺少窗口文件: {path}（请先打开 {self.platform} 的 BitBrowser 窗口并写入 ws）")
        lines = [l.strip() for l in open(path, encoding="utf-8") if l.strip()]
        return lines[-1]

    async def _connect(self):
        try:
            from cdp import CdpSession
            self.session = CdpSession(self.ws_url)
            await self.session.connect()
            self.sid = await self.session.attach_page()
        except Exception as e:
            self.session = None
            self.connect_error = f"{type(e).__name__}: {e}"
        finally:
            self._ready.set()

    async def _navigate_home(self):
        """打开 BitBrowser 后强制离开默认导航页，进入对应平台主页。"""
        home = {
            "douyin": "https://www.douyin.com/",
            "xhs": "https://www.xiaohongshu.com/explore",
            "kuaishou": "https://www.kuaishou.com/new-reco",
        }[self.platform]
        await self.session.navigate(home, self.sid, wait_load=False)
        await asyncio.sleep(1.5)

    def navigate_home(self):
        if self.session is None or self.sid is None:
            return
        fut = asyncio.run_coroutine_threadsafe(self._navigate_home(), self.loop)
        fut.result(timeout=30)

    def close(self):
        """关闭当前 CDP 会话和专用事件循环。

        断线重连时必须销毁旧上下文，否则旧 reader/loop 仍会持有已失效的
        websocket，后续调用会不断复用同一个坏连接。
        """
        try:
            if self.session is not None:
                fut = asyncio.run_coroutine_threadsafe(self.session.close(), self.loop)
                fut.result(timeout=5)
        except Exception:
            pass
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:
            pass
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=5)

    # ---------- 异步核心 ----------
    async def _wait_with_events(self, seconds, pause_event, cancel_event):
        """可被暂停/取消唤醒的等待；返回 False 表示已被取消或停止。

        暂停期间不消耗等待时长，恢复后继续把剩余时间走完。
        """
        remaining = max(0.0, float(seconds))
        while remaining > 0:
            if cancel_event is not None and cancel_event.is_set():
                return False
            if pause_event is not None and not pause_event.is_set():
                await asyncio.sleep(0.2)
                continue
            step = min(0.5, remaining)
            await asyncio.sleep(step)
            remaining -= step
        return True

    async def _xhs_retry(self, factory, pause_event, cancel_event, label):
        """执行小红书采集调用；限流时退避重试，耗尽后才升级为人工接管。

        限流（"请求太频繁 / 操作过于频繁"）是暂时性的平台反压信号，不是需要
        人工介入的状态。旧实现把限流混进 HumanBlock 直接冻结账号，导致一次
        限流就整轮停摆；这里改为先退避重试，连续多次仍被限流才交人工处理。

        返回 ``_CANCELLED`` 表示等待期间被取消或停止。
        """
        backoff = random.uniform(*XHS_RATE_LIMIT_BACKOFF)
        for attempt in range(XHS_RATE_LIMIT_RETRIES):
            try:
                return await factory()
            except xhs_collect3.RateLimited as exc:
                if attempt >= XHS_RATE_LIMIT_RETRIES - 1:
                    raise HumanInterventionRequired(
                        "rate_limited",
                        f"小红书{label}连续 {XHS_RATE_LIMIT_RETRIES} 次触发限流未恢复：{exc}",
                    )
                wait = min(backoff * (2 ** attempt), XHS_RATE_LIMIT_BACKOFF_MAX)
                wait += random.uniform(0.0, XHS_RATE_LIMIT_JITTER)
                print(
                    f"[LiveCollector] xhs {label}触发限流，退避 {wait:.0f}s 后重试 "
                    f"({attempt + 1}/{XHS_RATE_LIMIT_RETRIES})：{exc}",
                    flush=True,
                )
                if not await self._wait_with_events(wait, pause_event, cancel_event):
                    return _CANCELLED

    async def _search(self, keyword, mode="standard", target_count=None,
                      pause_event=None, cancel_event=None,
                      search_sort="default"):
        pause_event = pause_event or self.pause_event
        cancel_event = cancel_event or self.cancel_event
        if self.platform == "douyin":
            vids = await dy_collect.search_videos(
                self.session, self.sid, keyword, mode=mode,
                target_count=target_count,
                pause_event=pause_event, cancel_event=cancel_event,
                search_sort=search_sort,
            )
            out = [{
                "vid": v["vid"], "url": v["url"],
                "title": v["desc"][:80], "author": v["nickname"],
                "kind": v["kind"], "create_time": v["create_time"],
            } for v in vids]
            # 转换字段时保留阶段 A 的终止元数据，否则调度器会把一次
            # 只返回半批结果的普通 list 误认为搜索已经完成。
            return dy_collect.SearchVideosResult(
                out,
                search_complete=getattr(vids, "search_complete", True),
                reached_target=getattr(vids, "reached_target", False),
                no_more_results=getattr(vids, "no_more_results", False),
                rounds=getattr(vids, "rounds", 0),
                termination_reason=getattr(vids, "termination_reason", ""),
            )
        elif self.platform == "xhs":
            feeds = await self._xhs_retry(
                lambda: xhs_collect3.load_feeds(
                    self.session, self.sid, keyword, target_count=target_count,
                    pause_event=pause_event, cancel_event=cancel_event,
                    search_sort=search_sort,
                ),
                pause_event, cancel_event, "搜索")
            if feeds is _CANCELLED:
                return xhs_collect3.SearchVideosResult([], search_complete=False)
            out = []
            for f in feeds:
                nid = str(f.get("id") or "")
                token = f.get("xsec_token", "")
                # url 带上 xsec_token，供收藏阶段正常打开笔记页
                if token:
                    import urllib.parse
                    url = f"https://www.xiaohongshu.com/explore/{nid}?xsec_token={urllib.parse.quote(token)}&xsec_source=pc_search"
                else:
                    url = f"https://www.xiaohongshu.com/explore/{nid}"
                out.append({
                    "vid": nid,
                    "url": url,
                    "title": f.get("title", ""), "author": f.get("author", ""),
                    "kind": "note",
                    "_xsec_token": token or "",
                })
            limit = target_count or 100
            return xhs_collect3.SearchVideosResult(
                out[:int(limit)],
                search_complete=getattr(feeds, "search_complete", True),
                reached_target=getattr(feeds, "reached_target", False),
                no_more_results=getattr(feeds, "no_more_results", False),
                rounds=getattr(feeds, "rounds", 0),
                termination_reason=getattr(feeds, "termination_reason", ""),
            )
        else:  # kuaishou
            feeds = await kuaishou_collect.search_videos(
                self.session, self.sid, keyword, mode=mode,
                target_count=target_count, pause_event=pause_event,
                cancel_event=cancel_event, search_sort=search_sort,
            )
            return feeds

    async def _fetch(self, vid, url, xsec_token="", pause_event=None, cancel_event=None):
        pause_event = pause_event or self.pause_event
        cancel_event = cancel_event or self.cancel_event
        while pause_event is not None and not pause_event.is_set():
            if cancel_event is not None and cancel_event.is_set():
                return []
            await asyncio.sleep(.2)
        if cancel_event is not None and cancel_event.is_set():
            return []
        if self.platform == "douyin":
            comments = await dy_collect.fetch_comments(
                self.session, self.sid, url, quiet=3, max_work=300,
                pause_event=pause_event, cancel_event=cancel_event)
            return [{
                "user_id": c.get("user_id", ""), "nickname": c.get("nickname", ""),
                "content": c.get("text", ""), "comment_time": c.get("create_time_str", ""),
                "region": c.get("region", ""), "homepage": c.get("homepage", ""),
                "cid": c.get("cid", ""),
            } for c in comments]
        elif self.platform == "xhs":
            # 从 url 解析 xsec_token（search 阶段已拼入）
            import urllib.parse as _up
            q = _up.parse_qs(_up.urlparse(url).query)
            token = (q.get("xsec_token") or [""])[0]
            note = {"id": vid, "xsec_token": token or xsec_token or ""}

            # 节奏下限①：两篇笔记之间保持随机最小间隔（约 1 篇/分钟）。
            # 任务参数 batch_size/cooldown_seconds 由调度器控制批次冷却，
            # 这里补的是采集器自身的安全下限，任务配置再激进也不会被突破。
            if self._xhs_last_note_at:
                elapsed = asyncio.get_running_loop().time() - self._xhs_last_note_at
                if elapsed < self._xhs_note_interval:
                    wait = self._xhs_note_interval - elapsed
                    print(f"[LiveCollector] xhs 节奏下限：等待 {wait:.0f}s 后再打开下一篇",
                          flush=True)
                    if not await self._wait_with_events(wait, pause_event, cancel_event):
                        return []
            self._xhs_note_interval = random.uniform(*XHS_MIN_NOTE_INTERVAL)

            # 节奏下限②：每采满 N 篇进入一次长休，稀释整体请求密度。
            if self._xhs_notes_since_break >= XHS_BREAK_EVERY_NOTES:
                wait = random.uniform(*XHS_LONG_BREAK)
                print(f"[LiveCollector] xhs 已连续采集 {self._xhs_notes_since_break} 篇，"
                      f"长休 {wait:.0f}s…", flush=True)
                if not await self._wait_with_events(wait, pause_event, cancel_event):
                    return []
                self._xhs_notes_since_break = 0

            # 记录“请求开始时间”用于防止失败后立即重试，但计数只在详情页
            # 成功返回后增加；失败/取消不能消耗正常采集的长休额度。
            self._xhs_last_note_at = asyncio.get_running_loop().time()

            r = await self._xhs_retry(
                lambda: xhs_collect3.fetch_note(
                    self.session, self.sid, note,
                    pause_event=pause_event, cancel_event=cancel_event),
                pause_event, cancel_event, "笔记详情")
            if r is _CANCELLED:
                return []
            self._xhs_notes_since_break += 1
            out = []
            for c in r.get("comments", []):
                out.append({
                    "user_id": c.get("user_id", ""), "nickname": c.get("user", ""),
                    "content": c.get("text", ""), "comment_time": c.get("time", ""),
                    "region": c.get("region", ""), "homepage": c.get("homepage", ""),
                    "cid": c.get("cid", ""),
                })
            return out
        else:  # kuaishou
            comments = await kuaishou_collect.fetch_comments(
                self.session, self.sid, url, quiet=4, max_work=300,
                pause_event=pause_event, cancel_event=cancel_event,
            )
            return [{
                "user_id": c.get("user_id", ""),
                "nickname": c.get("nickname", ""),
                "content": c.get("text", ""),
                "comment_time": c.get("create_time_str", ""),
                "region": c.get("region", ""),
                "homepage": c.get("homepage", ""),
                "cid": c.get("cid", ""),
                "parent_id": c.get("parent_id", ""),
                "extra": c.get("extra", {}),
            } for c in comments]

    # ---------- 同步入口（worker 线程调用）----------
    @staticmethod
    def _wrap_human(fn):
        """把平台采集器的阻塞类异常转成 scheduler.HumanInterventionRequired（P7）。

        ``RateLimited`` 也会走这里：它应当由 ``_xhs_retry`` 在退避重试中消化，
        只有连着多次都限流才会以 "rate_limited" 的理由升级为人工接管。若它
        意外逃到这一层，必须保持相同语义，不能被当成普通采集失败计一次失败。
        """
        try:
            return fn()
        except HumanInterventionRequired:
            raise
        except Exception as e:
            name = type(e).__name__
            if name in ("HumanBlock", "HumanInterventionRequired", "RateLimited"):
                reason = getattr(e, "reason", None) or str(e) or "unknown"
                raise HumanInterventionRequired(reason, str(e))
            raise

    def search(self, keyword, mode="standard", target_count=None,
               pause_event=None, cancel_event=None, search_sort="default"):
        # 搜索必须持续到“达到目标”或页面明确提示没有更多，不能再用
        # 固定的 180 秒总超时截断正常的长时间翻页。
        return self._wrap_human(lambda: self._run_sync(
            self._search, keyword, mode, target_count, pause_event, cancel_event,
            search_sort,
            timeout=None, cancel_event=cancel_event))

    def fetch_comments(self, vid, url="", xsec_token="", platform=None,
                       pause_event=None, cancel_event=None):
        # 单作品评论允许更长的分页窗口；线程级兜底必须略长于采集器
        # max_work，否则慢页面会被外层先取消，最终只得到一个无上下文的
        # TimeoutError。超时后 LiveCollector 会重建 CDP 上下文并重试一次。
        # 小红书额外放宽：激进降频下单篇笔记包含节奏下限等待和长休，
        # 超时会把正常采集取消掉并误判为失败，因此给足余量。
        timeout = {
            "douyin": 960,
            "xhs": 900,
            "kuaishou": 480,
        }.get(self.platform, 480)
        return self._wrap_human(lambda: self._run_sync(
            self._fetch, vid, url, xsec_token, pause_event, cancel_event,
            timeout=timeout, cancel_event=cancel_event))

    def _run_sync(self, coro_fn, *args, timeout=180, cancel_event=None):
        with self.lock:
            previous_cancel_event = None
            if self.session is not None:
                previous_cancel_event = self.session.set_cancel_event(
                    cancel_event if cancel_event is not None else self.cancel_event
                )
            try:
                fut = asyncio.run_coroutine_threadsafe(coro_fn(*args), self.loop)
                operation = getattr(coro_fn, "__name__", "采集操作")
                # timeout=None 表示搜索阶段一直等待采集器返回终止元数据；
                # 单次 CDP 命令仍由 cdp.py 自身的命令级超时保护。
                if timeout is None:
                    try:
                        return fut.result()
                    except CollectorTimeoutError:
                        raise
                    except (TimeoutError, asyncio.TimeoutError) as exc:
                        raise CollectorTimeoutError(
                            operation, cause=exc
                        ) from exc
                try:
                    return fut.result(timeout=timeout)
                except CollectorTimeoutError:
                    raise
                except (TimeoutError, asyncio.TimeoutError) as exc:
                    # concurrent.futures.Future 超时后，底层协程默认仍会在事件
                    # 循环里继续执行。若不取消，它会继续占用同一个平台上下文，
                    # 后续 URL 虽然已被 scheduler 取出，却无法真正打开详情页，
                    # 最终表现为一个 collecting 把整批任务拖住。
                    fut.cancel()
                    try:
                        fut.result(timeout=2)
                    except Exception:
                        pass
                    raise CollectorTimeoutError(
                        operation, timeout=timeout, cause=exc
                    ) from exc
            finally:
                if self.session is not None:
                    self.session.set_cancel_event(previous_cancel_event)


class LiveCollector(Collector):
    """真实采集器：按平台路由到对应 BitBrowser 窗口。"""

    _WINDOW_FILES = {
        "douyin": "dy_window.txt",
        "xhs": "xhs_window.txt",
        "kuaishou": "kuaishou_window.txt",
    }

    def __init__(self, platforms=("douyin", "xhs", "kuaishou"), bb=None):
        self.bb = bb
        self.platforms = tuple(PLATFORM_ALIAS[p] for p in platforms)
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.cancel_event = threading.Event()
        self.ctxs = {}
        self._ctx_lock = threading.RLock()

    @classmethod
    def _window_file(cls, platform):
        return os.path.join(DATA, cls._WINDOW_FILES[PLATFORM_ALIAS[platform]])

    @classmethod
    def _load_cached_ws(cls, platform, window_id):
        """只读取与当前 BitBrowser 窗口 ID 匹配的缓存连接。"""
        path = cls._window_file(platform)
        try:
            with open(path, encoding="utf-8") as stream:
                lines = [line.strip() for line in stream if line.strip()]
        except OSError:
            return None
        if len(lines) < 2 or lines[0] != str(window_id):
            return None
        return lines[-1]

    @classmethod
    def _save_cached_ws(cls, platform, window_id, ws_url):
        """保存最近一次成功连接，供下次启动优先复用。"""
        if not ws_url:
            return
        try:
            os.makedirs(DATA, exist_ok=True)
            with open(cls._window_file(platform), "w", encoding="utf-8") as f:
                f.write(f"{window_id}\n{ws_url}\n")
        except OSError:
            # 缓存失败不影响当前已建立的连接。
            pass

    @staticmethod
    def _build_ctx(platform, ws_url, pause_event, cancel_event):
        ctx = _PlatformCtx(platform, ws_url=ws_url)
        ctx.pause_event = pause_event
        ctx.cancel_event = cancel_event
        if ctx.session is not None:
            ctx.session.set_cancel_event(cancel_event)
        ctx.navigate_home()
        return ctx

    def _ctx(self, platform, window_id=None):
        p = PLATFORM_ALIAS[platform]
        key = (p, str(window_id) if window_id is not None else None)
        if key in self.ctxs:
            return self.ctxs[key]
        if window_id and self.bb is not None:
            # 任务启动前可能已经由用户/GUI打开了窗口。优先复用缓存的
            # CDP 地址，避免再次调用 /browser/open 触发 BitBrowser 的
            # “ID不合法”或重复打开问题。
            cached_ws = self._load_cached_ws(p, window_id)
            cached_error = None
            if cached_ws:
                try:
                    ctx = self._build_ctx(
                        p, cached_ws, self.pause_event, self.cancel_event)
                    self.ctxs[key] = ctx
                    return ctx
                except Exception as exc:  # noqa: BLE001
                    cached_error = f"缓存连接不可用: {type(exc).__name__}: {exc}"

            try:
                opened = self.bb.open_browser(window_id, ignore_default_urls=True)
            except Exception as exc:  # noqa: BLE001
                detail = f"；{cached_error}" if cached_error else ""
                raise RuntimeError(
                    f"平台 {p} 窗口 {window_id} 打开失败: {exc}{detail}"
                ) from exc
            ws_url = opened.get("ws") if isinstance(opened, dict) else None
            if ws_url:
                ctx = self._build_ctx(
                    p, ws_url, self.pause_event, self.cancel_event)
                self._save_cached_ws(p, window_id, ws_url)
                self.ctxs[key] = ctx
                return ctx
            detail = f"；{cached_error}" if cached_error else ""
            raise RuntimeError(
                f"平台 {p} 窗口 {window_id} 未返回 CDP 地址{detail}"
            )
        # 仅在确实没有绑定窗口 ID 时，才回退读取平台窗口文件。
        if not window_id:
            ctx = self._build_ctx(
                p, None, self.pause_event, self.cancel_event)
            self.ctxs[key] = ctx
            return ctx
        raise RuntimeError(f"平台 {p} 窗口 {window_id or '默认'} 未初始化")

    def _refresh_ctx(self, platform, window_id, old_ctx=None, cause=None):
        """通过 BitBrowser 重新取得 CDP 地址并替换失效上下文。

        浏览器窗口 ID 是配置身份，CDP websocket 是一次打开实例的临时连接。
        关闭再打开浏览器后，前者可能不变，但后者一定可能变化，不能继续使用
        ``data/*_window.txt`` 或内存中的旧地址。
        """
        p = PLATFORM_ALIAS[platform]
        key = (p, str(window_id) if window_id is not None else None)
        if not window_id or self.bb is None:
            raise RuntimeError(f"平台 {p} 无法重建浏览器连接：缺少窗口 ID 或 BitBrowser 客户端")

        with self._ctx_lock:
            current = self.ctxs.get(key)
            if old_ctx is not None and current is not None and current is not old_ctx:
                return current
            try:
                opened = self.bb.open_browser(window_id, ignore_default_urls=True)
            except Exception as exc:
                reason = f"；断线原因：{type(cause).__name__}: {cause}" if cause else ""
                raise RuntimeError(
                    f"平台 {p} 窗口 {window_id} 重连失败：{exc}{reason}"
                ) from exc
            ws_url = opened.get("ws") if isinstance(opened, dict) else None
            if not ws_url:
                raise RuntimeError(f"平台 {p} 窗口 {window_id} 重连失败：BitBrowser 未返回 CDP 地址")

            new_ctx = None
            try:
                new_ctx = self._build_ctx(
                    p, ws_url, self.pause_event, self.cancel_event)
                self._save_cached_ws(p, window_id, ws_url)
            except Exception:
                if new_ctx is not None:
                    new_ctx.close()
                raise

            previous = self.ctxs.get(key)
            self.ctxs[key] = new_ctx
            if previous is not None and previous is not new_ctx:
                previous.close()
            return new_ctx

    def search(self, keyword: str, platform: str, mode: str = "standard",
               target_count: int = 100,
               window_id: str = None, pause_event=None, cancel_event=None,
               search_sort: str = "default"):
        ctx = self._ctx(platform, window_id=window_id)
        return ctx.search(keyword, mode, target_count, pause_event, cancel_event, search_sort)

    def fetch_comments(self, vid: str, account: str, url: str = "", platform: str = None,
                       window_id: str = None, pause_event=None, cancel_event=None):
        # 平台由任务显式传入；旧调用才回退到 URL/账号名兼容判断。
        if platform:
            plat = PLATFORM_ALIAS[platform]
        elif "xiaohongshu.com" in str(url).lower():
            plat = "xhs"
        elif "douyin.com" in str(url).lower():
            plat = "douyin"
        elif "kuaishou.com" in str(url).lower() or "gifshow.com" in str(url).lower():
            plat = "kuaishou"
        else:
            plat = "douyin" if str(account).lower().startswith("dy") else "xhs"
        # 优先用 scheduler 传入的真实 url（含 note 类型 / note/ 与 xhs xsec_token）
        url = url or self._default_url(plat, vid)
        ctx = self._ctx(plat, window_id=window_id)
        try:
            return ctx.fetch_comments(vid, url, pause_event=pause_event, cancel_event=cancel_event)
        except CollectorTimeoutError as exc:
            if cancel_event is not None and cancel_event.is_set():
                # 任务停止触发的CDP取消不能进入重连/重试，否则停止按钮会
                # 重新打开浏览器连接，反而延长任务收尾时间。
                raise
            # 超时通常意味着页面事件队列或 CDP 通道已经失去响应；继续复用
            # 原上下文只会把同一把锁和同一个 websocket 带入下一个作品。重建
            # 窗口连接后仅重试当前作品一次，避免一个异常作品无限循环。
            print(
                f"[LiveCollector] {plat} 作品 {vid} 采集超时，"
                f"正在重建 CDP 连接并重试一次：{exc}",
                flush=True,
            )
            fresh = self._refresh_ctx(plat, window_id, old_ctx=ctx, cause=exc)
            try:
                return fresh.fetch_comments(
                    vid, url, pause_event=pause_event, cancel_event=cancel_event
                )
            except CollectorTimeoutError as retry_exc:
                retry_exc.connection_retry_attempted = True
                raise
        except Exception as exc:
            if not _is_connection_closed_error(exc):
                raise
            # 只对明确的 CDP 断线自动重连一次。若重连后仍失败，交给 scheduler
            # 记录真正的连接错误；不会无限重试，也不会误报人工验证。
            print(
                f"[LiveCollector] {plat} 窗口 {window_id} CDP 已断开，"
                "正在重新获取连接并重试当前作品…",
                flush=True,
            )
            fresh = self._refresh_ctx(plat, window_id, old_ctx=ctx, cause=exc)
            return fresh.fetch_comments(
                vid, url, pause_event=pause_event, cancel_event=cancel_event
            )

    def pause(self):
        self.pause_event.clear()

    def resume(self):
        self.pause_event.set()

    def cancel(self):
        self.cancel_event.set()

    def _default_url(self, plat, vid):
        if plat == "douyin":
            return f"https://www.douyin.com/video/{vid}"
        if plat == "kuaishou":
            return f"https://www.kuaishou.com/short-video/{vid}"
        return f"https://www.xiaohongshu.com/explore/{vid}"

    def shutdown(self):
        for ctx in list(self.ctxs.values()):
            try:
                ctx.close()
            except Exception:
                try:
                    ctx.loop.call_soon_threadsafe(ctx.loop.stop)
                except Exception:
                    pass
            if ctx.thread is not threading.current_thread():
                ctx.thread.join(timeout=5)
        self.ctxs.clear()


class HybridCollector(Collector):
    """同时承载 BitBrowser 平台、普通 Chrome 会话和贴吧 API。"""

    def __init__(self, live=None, weibo=None, bilibili=None, bb=None, tieba=None):
        self.live = live
        self.weibo = weibo
        self.bilibili = bilibili
        self.tieba = tieba
        self.bb = bb
        self._browser_collectors = {}

    def _platform_collector(self, platform, window_id=None):
        """按 BitBrowser 窗口动态创建微博/B站 CDP 适配器。

        普通 Chrome 适配器只作为没有 window_id 时的开发备用；正式任务优先使用
        BitBrowser open_browser 返回的 ws。
        """
        if window_id and self.bb is not None and platform in ("weibo", "bilibili"):
            key = (platform, str(window_id))
            if key not in self._browser_collectors:
                # 优先复用已打开窗口记录的 CDP 地址；窗口未关闭时不重复调用
                # open_browser，避免 BitBrowser 返回旧的/空的连接地址。
                fname = "weibo_chrome_window.txt" if platform == "weibo" else "bilibili_chrome_window.txt"
                ws_path = os.path.join(PROJECT_ROOT, "data", fname)
                ws = None
                try:
                    lines = [x.strip() for x in open(ws_path, encoding="utf-8") if x.strip()]
                    if len(lines) >= 2 and lines[0] == str(window_id):
                        ws = lines[-1]
                except OSError:
                    pass
                if not ws:
                    opened = self.bb.open_browser(window_id, ignore_default_urls=True)
                    ws = opened.get("ws") if isinstance(opened, dict) else None
                if not ws:
                    raise RuntimeError(f"平台 {platform} 窗口 {window_id} 未返回 CDP 地址")
                if platform == "weibo":
                    from weibo_chrome import WeiboChromeCollector
                    self._browser_collectors[key] = WeiboChromeCollector(ws)
                else:
                    from bilibili_adapter import BilibiliChromeCollector
                    self._browser_collectors[key] = BilibiliChromeCollector(ws)
            return self._browser_collectors[key]
        if platform == "tieba":
            return self.tieba
        return self.weibo if platform == "weibo" else self.bilibili

    def search(self, keyword, platform, mode="standard", target_count=100, window_id=None,
               pause_event=None, cancel_event=None, search_sort="default"):
        if platform == "tieba":
            collector = self._platform_collector(platform, window_id)
            if collector is None:
                raise RuntimeError("贴吧 API 采集器未配置")
            return collector.search(
                keyword, platform, mode, target_count, window_id,
                search_sort=search_sort, pause_event=pause_event,
                cancel_event=cancel_event,
            )
        if platform == "weibo":
            collector = self._platform_collector(platform, window_id)
            if collector is None:
                raise RuntimeError("微博 Chrome 会话未配置")
            return collector.search(keyword, platform, mode, target_count, window_id,
                                    pause_event=pause_event, cancel_event=cancel_event,
                                    search_sort=search_sort)
        if platform == "bilibili":
            collector = self._platform_collector(platform, window_id)
            if collector is None:
                raise RuntimeError("B站 Chrome 会话未配置")
            return collector.search(keyword, platform, mode, target_count, window_id,
                                    pause_event=pause_event, cancel_event=cancel_event,
                                    search_sort=search_sort)
        if self.live is None:
            raise RuntimeError(f"平台 {platform} 采集器未初始化")
        return self.live.search(keyword, platform, mode, target_count, window_id,
                                pause_event=pause_event, cancel_event=cancel_event,
                                search_sort=search_sort)

    def collect_with_comments(self, keyword, target_count=100, mode="standard", platform="weibo", window_id=None,
                              progress_callback=None, pause_event=None, cancel_event=None,
                              search_sort="default"):
        collector = self._platform_collector(platform, window_id)
        if collector is None:
            raise RuntimeError(
                "贴吧 API 采集器未配置" if platform == "tieba"
                else f"{platform} Chrome 会话未配置"
            )
        # 微博/B站的旧适配器已经固定了 collect_with_comments 签名；贴吧
        # 适配器才需要显式接收 platform/window_id。避免新增平台参数破坏旧平台。
        kwargs = {
            "progress_callback": progress_callback,
            "pause_event": pause_event,
            "cancel_event": cancel_event,
            "search_sort": search_sort,
        }
        if platform == "tieba":
            kwargs.update({"platform": platform, "window_id": window_id})
        return collector.collect_with_comments(keyword, target_count, mode, **kwargs)

    def fetch_comments(self, vid, account, url="", platform=None, window_id=None,
                       pause_event=None, cancel_event=None):
        if platform == "tieba":
            collector = self._platform_collector(platform, window_id)
            if collector is None:
                raise RuntimeError("贴吧 API 采集器未配置")
            return collector.fetch_comments(
                vid, account, url, platform, window_id,
                pause_event=pause_event, cancel_event=cancel_event,
            )
        if platform == "weibo":
            collector = self._platform_collector(platform, window_id)
            if collector is None:
                raise RuntimeError("微博 Chrome 会话未配置")
            return collector.fetch_comments(vid, account, url, platform, window_id,
                                            pause_event=pause_event, cancel_event=cancel_event)
        if platform == "bilibili":
            collector = self._platform_collector(platform, window_id)
            if collector is None:
                raise RuntimeError("B站 Chrome 会话未配置")
            return collector.fetch_comments(vid, account, url, platform, window_id,
                                            pause_event=pause_event, cancel_event=cancel_event)
        if self.live is None:
            raise RuntimeError(f"平台 {platform} 采集器未初始化")
        return self.live.fetch_comments(vid, account, url, platform, window_id,
                                        pause_event=pause_event, cancel_event=cancel_event)

    def pause(self):
        if self.live:
            self.live.pause()
        if self.weibo:
            pass

    def resume(self):
        if self.live:
            self.live.resume()

    def cancel(self):
        if self.live:
            self.live.cancel()

    def shutdown(self):
        if self.live:
            self.live.shutdown()
        for collector in (self.tieba,):
            shutdown = getattr(collector, "shutdown", None)
            if callable(shutdown):
                shutdown()


# ---------------------------------------------------------------------------
# SchedulerAdapter：把真实收藏并入 scheduler 的 URL 路由（scheduler 只把 vid/url 传给
# fetch_comments，这里用 SchedulerAdapter.fetch_comments(vid, account) 通过一个
# vid->url 映射表找到真实 url 后调 LiveCollector.search 拿的 URL）。
# 注意：scheduler._worker 调 fetch_comments(vid, account)，为能拿到 url 我们
# 需要 scheduler 传入 url。见下方说明。
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # 快速冒烟测试真实采集
    import sys
    print("LiveCollector 冒烟测试：连接窗口 + 搜索一个关键词")
    lc = LiveCollector(platforms=("douyin", "xhs", "kuaishou"))
    try:
        vids = lc.search("智能快递柜", "douyin")
        print(f"抖音搜索到 {len(vids)} 个作品，前3个：")
        for v in vids[:3]:
            print(f"  {v['vid'][-8:]} | {v['title'][:20]}")
    finally:
        lc.shutdown()
