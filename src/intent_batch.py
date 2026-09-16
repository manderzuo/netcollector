# -*- coding: utf-8 -*-
"""采集过程中的批量意向分析与本地兜底。

评论先由采集线程落库并进入线索中心；本模块只负责在新评论达到 500 条时
异步调用一次 LLM，并把结果幂等回写到 comments 和 leads。未配置 LLM 时，
直接使用项目内置的 IntentScorer，不阻塞等待远程服务。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime

try:
    from . import db  # type: ignore
    from .config_loader import AppConfig  # type: ignore
    from .leads.intent import IntentScorer  # type: ignore
    from .leads.repository import LeadRepository  # type: ignore
    from .leads.service import LeadService  # type: ignore
    from .llm_api import LLMApiClient, is_llm_eligible_comment  # type: ignore
except ImportError:
    import db  # type: ignore
    from config_loader import AppConfig  # type: ignore
    from leads.intent import IntentScorer  # type: ignore
    from leads.repository import LeadRepository  # type: ignore
    from leads.service import LeadService  # type: ignore
    from llm_api import LLMApiClient, is_llm_eligible_comment  # type: ignore


BATCH_SIZE = 500
_LABELS = {
    "高": "high", "中": "medium", "低": "low",
    "high": "high", "medium": "medium", "low": "low",
}
_SCORES = {"high": 5, "medium": 3, "low": 1}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class _ProgressReporter:
    """把流式响应进度压缩成可读的低频进度日志。

    LLM 服务可能每收到一个 token 就回调一次。尤其是在模型输出思考内容
    时，完成数量仍然是 0；如果每次都写日志，会把 GUI 主线程和磁盘写入
    队列瞬间打满。文件日志仍保留批次开始、有效进度和完成状态。
    """

    def __init__(self, callback, *, min_step: int = 100,
                 min_interval: float = 1.0, clock=None):
        self._callback = callback
        self._min_step = max(1, int(min_step))
        self._min_interval = max(0.0, float(min_interval))
        self._clock = clock or time.monotonic
        self._last_done = -1
        self._last_at = 0.0

    def __call__(self, done, total, phase):
        try:
            done = max(0, int(done or 0))
        except (TypeError, ValueError):
            done = 0
        try:
            total = max(0, int(total or 0))
        except (TypeError, ValueError):
            total = 0
        now = self._clock()

        if self._last_done < 0:
            should_report = True
        elif done <= 0:
            # 开始阶段可能有数千个 token，但只记录一次 0/总数。
            should_report = False
        elif total > 0 and done >= total:
            should_report = done != self._last_done
        else:
            should_report = (
                done > self._last_done
                and (
                    done - self._last_done >= self._min_step
                    or now - self._last_at >= self._min_interval
                )
            )

        if not should_report:
            return False
        self._last_done = done
        self._last_at = now
        self._callback(done, total, phase)
        return True


class IntentBatchProcessor:
    """按新评论累计数触发批量意向分析。"""

    def __init__(self, db_path: str, *, log_callback=None, batch_size: int = BATCH_SIZE):
        self.db_path = db_path
        self.batch_size = max(1, int(batch_size))
        self.log_callback = log_callback
        self._lock = threading.RLock()
        self._pending: set[int] = set()
        self._inflight: set[int] = set()
        self._batch_threads: set[threading.Thread] = set()
        self._closed = False
        self._flush_timer = None
        self._tail_flush_seconds = 5.0
        # 批量 LLM 请求按队列串行执行，避免采集线程一口气启动多个网络请求、
        # 数据库事务和进度日志流，导致整台 GUI 被拖住。后续仍可通过构造参数
        # 扩展并发数，但默认必须保持单批次运行。
        self.max_concurrent_batches = 1
        self._batch_slots = threading.BoundedSemaphore(self.max_concurrent_batches)
        self._llm_config_cache = None
        self._llm_config_cache_until = 0.0
        self._filtered_since_log = 0
        self._last_filtered_log_at = 0.0

    def close(self, timeout: float = 8.0) -> None:
        """停止接收新评论，并把不足一整批的尾部评论也提交处理。"""
        with self._lock:
            self._closed = True
            timer = self._flush_timer
            self._flush_timer = None
        if timer is not None:
            timer.cancel()

        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            # close 后仍允许这一轮强制提交尾批；普通新数据不会再进入队列。
            self._schedule_ready_batches(force=True)
            with self._lock:
                pending = bool(self._pending or self._inflight)
                threads = [
                    thread for thread in self._batch_threads
                    if isinstance(thread, threading.Thread) and thread.is_alive()
                ]
            if not pending or not threads or time.monotonic() >= deadline:
                break
            remaining = max(0.01, deadline - time.monotonic())
            threads[0].join(timeout=min(0.2, remaining))

    def on_comment(self, comment_id: int | None, *, task_id: int | None = None,
                   comment: dict | None = None) -> None:
        """旧单条入口兼容保留，但实际逻辑统一走批量入口。"""
        if comment_id is None:
            return
        self.on_comments([(comment_id, comment)], task_id=task_id)

    def on_comments(self, items: list[tuple[int, dict | None]], *,
                    task_id: int | None = None) -> None:
        """批量接收已落库评论，避免采集结果逐条初始化意图处理。

        采集路径统一使用本方法；只读取一次 LLM 配置，并把本地规则评论
        合并为一次数据库处理。``on_comment`` 仅作为旧调用方的兼容入口。
        """
        normalized = []
        for comment_id, comment in items or []:
            if comment_id is None:
                continue
            normalized.append((int(comment_id), comment))
        if not normalized:
            return

        filtered_ids = []
        eligible_ids = []
        for comment_id, comment in normalized:
            if comment is not None and not is_llm_eligible_comment(comment):
                filtered_ids.append(comment_id)
            else:
                eligible_ids.append(comment_id)

        if filtered_ids:
            with self._lock:
                self._filtered_since_log += len(filtered_ids)
                now = time.monotonic()
                should_log = now - self._last_filtered_log_at >= 30.0
                if should_log:
                    filtered_count = self._filtered_since_log
                    self._filtered_since_log = 0
                    self._last_filtered_log_at = now
            if should_log:
                self._log(
                    f"[intent] 评论意向分析：已过滤 {filtered_count} 条非文本评论，"
                    "统一使用本地规则，不逐条发送 LLM"
                )
            self._apply_local(filtered_ids, source="filtered_non_text")

        if not eligible_ids or not self._llm_configured():
            if eligible_ids:
                self._apply_local(eligible_ids, source="local")
            return

        with self._lock:
            if self._closed:
                return
            self._pending.update(eligible_ids)
        self._schedule_ready_batches(task_id=task_id)
        self._arm_tail_flush()

    def _llm_configured(self) -> bool:
        now = time.monotonic()
        with self._lock:
            if now < self._llm_config_cache_until:
                return bool(self._llm_config_cache)
        try:
            config = AppConfig().llm_api() or {}
        except Exception:
            configured = False
        else:
            configured = bool(
            config.get("enabled")
            and str(config.get("base_url") or "").strip()
            and str(config.get("model") or "").strip()
            and str(config.get("api_key") or "").strip()
            )
        with self._lock:
            # 配置允许热切换，但不应为每一条评论重复读取配置文件。
            self._llm_config_cache = configured
            self._llm_config_cache_until = now + 1.0
        return configured

    def _arm_tail_flush(self) -> None:
        """为不足 batch_size 的尾部评论安排一次延迟提交。"""
        with self._lock:
            if self._closed or not self._pending or self._flush_timer is not None:
                return
            timer = threading.Timer(self._tail_flush_seconds, self._flush_pending_tail)
            timer.daemon = True
            self._flush_timer = timer
        timer.start()

    def _flush_pending_tail(self) -> None:
        with self._lock:
            self._flush_timer = None
            if self._closed:
                return
        self._schedule_ready_batches(force=True)
        self._arm_tail_flush()

    def _schedule_ready_batches(self, *, task_id: int | None = None,
                                force: bool = False) -> None:
        while True:
            with self._lock:
                if self._closed and not force:
                    return
                available = sorted(self._pending - self._inflight)
                if not available or (len(available) < self.batch_size and not force):
                    return
                # 没有空闲槽位时保留在 pending，当前批次完成后会再次调度。
                if not self._batch_slots.acquire(blocking=False):
                    return
                comment_ids = available[:self.batch_size]
                self._inflight.update(comment_ids)
            thread = None
            try:
                thread = threading.Thread(
                    target=self._run_batch,
                    args=(comment_ids, task_id, True),
                    name=f"intent-batch-{comment_ids[0]}",
                    daemon=True,
                )
                with self._lock:
                    self._batch_threads.add(thread)
                thread.start()
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._inflight.difference_update(comment_ids)
                    if thread is not None:
                        self._batch_threads.discard(thread)
                self._batch_slots.release()
                self._log(f"[intent] 批量任务启动失败：{type(exc).__name__}: {exc}")
                return
            self._log(
                f"[intent] 已触发批量意向分析：{len(comment_ids)} 条，"
                f"起始评论#{comment_ids[0]}（队列串行）"
            )

    def _run_batch(self, comment_ids: list[int], task_id: int | None,
                   slot_acquired: bool = False) -> None:
        conn = None
        try:
            conn = db.init_db(self.db_path, check_same_thread=False)
            rows = self._load_rows(conn, comment_ids)
            if len(rows) != len(comment_ids):
                self._log(
                    f"[intent] 批量评论读取不完整：需要 {len(comment_ids)} 条，"
                    f"实际 {len(rows)} 条，缺失部分转本地规则"
                )
                self._apply_local(comment_ids, source="local_fallback")
                return

            # 兼容旧版本已经进入队列的无效评论，并防止本次批量再次出现
            # “API 实际分析 497 条、回写却按 500 条校验”的数量错位。
            row_by_id = {int(row["id"]): row for row in rows}
            eligible_ids = []
            eligible_rows = []
            filtered_ids = []
            for comment_id in comment_ids:
                row = row_by_id.get(int(comment_id))
                if row is not None and is_llm_eligible_comment(row):
                    eligible_ids.append(int(comment_id))
                    eligible_rows.append(row)
                else:
                    filtered_ids.append(int(comment_id))
            if filtered_ids:
                self._log(
                    f"[intent] 已过滤 {len(filtered_ids)} 条非文本评论，"
                    "不发送 LLM（空评论/图片/表情/纯数字）"
                )
                self._apply_local(filtered_ids, source="filtered_non_text")
            if not eligible_ids:
                return

            config = AppConfig().llm_api() or {}
            samples = []
            for row in eligible_rows:
                item = dict(row)
                item["platform_label"] = {
                    "douyin": "抖音", "xhs": "小红书",
                    "weibo": "微博", "bilibili": "B站",
                }.get(item.get("platform"), item.get("platform") or "未知平台")
                samples.append(item)

            def log_progress(done, total, phase):
                self._log(f"[intent] 批量分析进度：{done}/{total}，{phase}")

            progress = _ProgressReporter(log_progress)

            result = LLMApiClient.analyze_comments_batch(
                config, samples, batch_size=self.batch_size,
                progress_callback=progress,
            )
            if result.get("healthy"):
                mapped = self._map_llm_results(result.get("items") or [], eligible_ids)
                if mapped is not None:
                    self._save_results(conn, eligible_rows, mapped, source="llm", task_id=task_id)
                    self._log(
                        f"[intent] LLM 批量意向完成：{len(mapped)} 条，"
                        f"返回 {result.get('returned_count', len(mapped))} 条"
                    )
                    return
            self._log(
                f"[intent] LLM 批量分析失败，转本地规则："
                f"{result.get('detail') or '未返回有效结果'}"
            )
            self._apply_local(comment_ids, source="local_fallback")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[intent] 批量分析异常，转本地规则：{type(exc).__name__}: {exc}")
            self._apply_local(comment_ids, source="local_fallback")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            with self._lock:
                self._inflight.difference_update(comment_ids)
                self._pending.difference_update(comment_ids)
                self._batch_threads.discard(threading.current_thread())
            if slot_acquired:
                self._batch_slots.release()
            # 只有当前批次释放槽位后，才尝试启动下一批，避免并发堆积。
            self._schedule_ready_batches(task_id=task_id)
            self._arm_tail_flush()

    @staticmethod
    def _load_rows(conn, comment_ids: list[int]) -> list[dict]:
        placeholders = ",".join("?" for _ in comment_ids)
        rows = conn.execute(
            f"""SELECT c.id, c.video_id, c.platform, c.nickname, c.content,
                         c.comment_time, c.extra, v.task_id, v.url AS video_url
                    FROM comments c
                    LEFT JOIN videos v ON v.id = c.video_id
                   WHERE c.id IN ({placeholders})
                   ORDER BY c.id""",
            comment_ids,
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _map_llm_results(items: list[dict], comment_ids: list[int]):
        if len(items) != len(comment_ids):
            return None
        mapped = {}
        for item in items:
            try:
                index = int(item.get("编号", item.get("id")))
            except (TypeError, ValueError, AttributeError):
                return None
            label = _LABELS.get(str(item.get("意向", item.get("intent", ""))).strip())
            if index < 1 or index > len(comment_ids) or not label or index in mapped:
                return None
            mapped[comment_ids[index - 1]] = {
                "level": label,
                "score": _SCORES[label],
                "reasons": ["LLM 批量意向分析"],
                "rule_version": "llm-batch-v1",
            }
        if set(mapped) != set(comment_ids):
            return None
        return mapped

    def _apply_local(self, comment_ids: list[int], *, source: str) -> None:
        conn = None
        try:
            conn = db.init_db(self.db_path, check_same_thread=False)
            rows = self._load_rows(conn, comment_ids)
            scorer = IntentScorer()
            mapped = {}
            for row in rows:
                result = scorer.score(row.get("content"))
                level = str(result.level or "low")
                if level not in _SCORES:
                    level = "low"
                mapped[int(row["id"])] = {
                    "level": level,
                    "score": _SCORES[level],
                    "reasons": list(result.reasons or []) or ["本地通用规则解析"],
                    "rule_version": result.rule_version or "intent-rules-v1",
                }
            self._save_results(conn, rows, mapped, source=source)
            if rows:
                self._log(f"[intent] 本地规则意向解析完成：{len(rows)} 条（来源={source}）")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[intent] 本地意向解析失败：{type(exc).__name__}: {exc}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    @staticmethod
    def _save_results(conn, rows: list[dict], mapped: dict, *, source: str,
                      task_id: int | None = None) -> None:
        lead_service = LeadService(LeadRepository(conn))
        row_by_id = {int(row["id"]): row for row in rows}
        lead_items = []
        for comment_id, result in mapped.items():
            row = row_by_id.get(int(comment_id))
            if row is None:
                continue
            extra = row.get("extra")
            if isinstance(extra, str):
                try:
                    extra = json.loads(extra)
                except (TypeError, ValueError):
                    extra = {}
            if not isinstance(extra, dict):
                extra = {}
            extra.update({
                "intent_source": source,
                "intent_analyzed_at": _now_iso(),
            })
            conn.execute(
                "UPDATE comments SET intent_score = ?, intent_label = ?, extra = ? WHERE id = ?",
                (result["score"], result["level"], json.dumps(extra, ensure_ascii=False), int(comment_id)),
            )
            lead_items.append((
                int(comment_id),
                {
                    "task_id": task_id if task_id is not None else row.get("task_id"),
                    "video_id": row.get("video_id"),
                    "intent_override": result,
                },
            ))
        if lead_items:
            lead_service.ingest_comments(lead_items)
        else:
            conn.commit()

    def _log(self, message: str) -> None:
        if callable(self.log_callback):
            try:
                self.log_callback(message)
                return
            except Exception:
                pass
        print(message)
