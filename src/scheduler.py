# -*- coding: utf-8 -*-
"""
scheduler.py — P1 任务调度器（多账号采集平台）

依赖方向（单向，禁止反向）:
        db -> scheduler -> bitbrowser

即 scheduler 依赖 db.py（SQLite 数据层）与 bitbrowser.py（BitBrowser 客户端）；
db.py / bitbrowser.py 不得 import scheduler。

权威契约: docs/p1-contract.md 第 3 节「scheduler.py — 任务调度器」，第 2 节为 db.py
接口，第 1 节为 bitbrowser.py 接口（本阶段只预留对接位，不真实调用 BitBrowser）。

职责（本文件实现）:
  * 任务状态机: pending -> phase_a_search -> phase_b_comments -> done
  * 账号状态机: idle -> working -> cooldown -> idle；waiting_human（P7 冻结）
  * 阶段 A: collector.search() 产出视频 URL（本阶段由 FakeCollector 假数据占位）
  * 阶段 B: 视频按账号均分（余数从第一个账号顺延），每账号一个 worker 线程并行采评论
  * 冷却: 每 batch_size 个视频进入 cooldown_seconds；等待用 threading.Event 实现，
    可被 shutdown()/pause() 立即唤醒（不依赖任何第三方库）
  * 断点续跑: start(task_id) 在任何状态可重入，以 DB 为唯一真源；
    已完成(done)/采集失败(failed)的视频不重复分配，collecting 残留仅由原属账号续采
  * P7: mark_human_waiting() 冻结账号（期间绝不对该账号执行任何浏览器操作），
    resolve_human() 恢复并继续采集；其余账号不受影响（互不阻塞）
  * status_report() 返回可直接 json.dumps 的汇总 dict（GUI 渲染用）

collector 注入点:
    Scheduler(db_path, collector=obj) 或 sched.collector = obj。
    collector 需实现 Collector 抽象（search / fetch_comments）。
    未注入时默认使用本文件内置的 FakeCollector（假数据演示）。

与 db.py 的对接（按契约第 2 节函数名，全部在调用点集中）:
    使用: init_db / create_task / update_task_status / get_task / insert_video /
          mark_videos_assigned / get_videos_by_status / upsert_account /
          update_account_status / begin_cooldown / reset_batch / insert_comment
    注:
      - mark_videos_assigned 的第 3 个参数传账号 name（videos.assigned_account 为
        TEXT 列，与契约表结构一致），vids 传视频 vid 字符串列表（与 db.py 实现
        "WHERE vid IN (...)" 的语义对齐）。
      - 契约便捷函数未覆盖的写入（视频状态流转、processed_count/batch_count 自增、
        wait_reason/wait_since 清理、human_actions 审计、计数查询）由本文件用 SQLite
        原生 SQL 直接操作契约 DDL 声明过的列，不新增表、不增删核心字段。

BitBrowser 对接位（本阶段不启用）:
    真实阶段 B 采集时在 _worker 中通过 self.bb（BitBrowserClient 或注入的 mock）
    调 open_browser / ports 等获取窗口与页面；缺省时 __init__ 惰性构造
    BitBrowserClient()；bitbrowser.py 尚未就绪或不可用则回落为 None，采集继续走
    collector（FakeCollector 数据全假，因此 demo 全程不连 BitBrowser）。
"""

import datetime
import inspect
import json as _json
import os
import sqlite3
import threading
import time
import uuid

DEFAULT_COLLECT_TYPES = [
    "video_info", "author_info", "engagement", "comments", "comment_user", "region", "intent"
]

try:  # 包方式导入：import src.scheduler
    from . import db  # type: ignore
    from .account_reader import looks_like_account_key  # type: ignore
    from .search_sort import sort_label  # type: ignore
except ImportError:  # 脚本方式导入：python src/demo.py（src 在 sys.path）
    import db  # type: ignore
    from account_reader import looks_like_account_key  # type: ignore
    from search_sort import sort_label  # type: ignore


__all__ = [
    "Scheduler", "Collector", "FakeCollector", "HumanInterventionRequired",
    "TaskCancelled", "TaskPaused",
]

# 账号状态中的 working/cooldown 必须带有当前进程租约。没有活动线程、
# 没有存活租约的状态只能是上次异常退出留下的残留，启动和状态轮询时会校正为 idle。
_RUNTIME_ACCOUNT_STATUSES = frozenset({"working", "cooldown"})
_RUNTIME_HEARTBEAT_INTERVAL = 2.0
_RUNTIME_LEASE_TIMEOUT = 15.0

# 账号表允许本调度器直接写入的列（契约 DDL 核心字段，白名单防手滑）
_ACCT_WRITABLE = {
    "status", "processed_count", "batch_count",
    "cd_until", "wait_reason", "wait_since",
}


def _now_iso() -> str:
    """本地时间 ISO 字符串（与 db 的 datetime('now','localtime') 同语义）。"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _iso_to_dt(text) -> datetime.datetime | None:
    """兼容 'YYYY-MM-DD HH:MM:SS' 与带 T 的 ISO 两种存量格式。"""
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(str(text).replace("T", " "))
    except ValueError:
        return None


class HumanInterventionRequired(Exception):
    """采集过程中需要人工接管（验证码 / 登录态过期 / 滑块等）。

    由 collector 抛出；scheduler 捕获后将账号置为 waiting_human 并冻结 —
    绝不自行刷新 / 关闭 / 导航该窗口（P7 硬约束），等待 resolve_human()。
    """

    def __init__(self, reason: str = "captcha", message: str | None = None):
        self.reason = reason
        super().__init__(message or f"需要人工接管：{reason}")


class TaskCancelled(Exception):
    """任务在耗时搜索/预取阶段被用户停止。"""


class TaskPaused(Exception):
    """任务被用户暂停；与停止不同，断点和任务暂停状态都要保留。"""


class Collector:
    """占位采集回调抽象（由上层注入；本阶段用内置 FakeCollector 演示）。

    search(keyword, platform) -> [{"vid","url","title","author"?}, ...]
    fetch_comments(vid, account) -> [{"user_id","nickname","content","comment_time",...}, ...]
      —— vid 为平台视频 ID；account 为账号 name（字符串）。
    """

    def search(self, keyword: str, platform: str, mode: str = "standard",
               target_count: int = 100, window_id: str = None,
               search_sort: str = "default"):
        raise NotImplementedError

    def fetch_comments(self, vid: str, account: str, url: str = "", platform: str = None,
                       window_id: str = None, pause_event=None, cancel_event=None):
        raise NotImplementedError


class FakeCollector(Collector):
    """假数据采集器：N 个假视频、每视频 2~5 条假评论，线程安全、确定性可重放。

    - search() 返回 video_count 个视频（vid: fake_vid_001...，url/title 可点击）。
    - fetch_comments() 每视频 2 + (idx % 4) 条评论（2..5 条），intent 打分齐全；
      同一 (vid, account) 只生成一次，断点续跑重复拉取时重放同一批（幂等）。
    - human_on: 指定首次拉取即抛 HumanInterventionRequired 的 vid 集合（P7 演示）。
    """

    def __init__(self, video_count: int = 25, human_on: set | None = None, seed_vid_zero: int = 1):
        self.video_count = int(video_count)
        self.human_on = set(human_on or ())
        self._lock = threading.Lock()
        self._id_offset = int(seed_vid_zero)  # vid 编号从该值起（fake_vid_001 默认）
        self._videos = self._build_videos()
        self._fetched: dict = {}   # (vid, account) -> comments（确定性重放）
        self._raised: set = set()  # 已触发过人工接管的 vid（只触发一次）

    def _build_videos(self):
        out = []
        for i in range(self._id_offset, self._id_offset + self.video_count):
            vid = f"fake_vid_{i:03d}"
            out.append({
                "vid": vid,
                "url": f"https://www.douyin.com/video/{vid}",
                "title": f"智能快递柜 演示视频 {i}",
                "author": f"创作者_{i}",
            })
        return out

    def search(self, keyword: str, platform: str, mode: str = "standard",
               target_count: int = 100, window_id: str = None,
               search_sort: str = "default"):
        # 假数据：关键词/平台只影响 title 前缀以外的文案，此处保持确定性
        return [dict(v) for v in self._videos]

    def fetch_comments(self, vid: str, account: str, url: str = "", platform: str = None,
                       window_id: str = None, pause_event=None, cancel_event=None):
        with self._lock:
            key = (vid, account)
            if vid in self.human_on and vid not in self._raised:
                self._raised.add(vid)
                raise HumanInterventionRequired("captcha", f"假数据：视频 {vid} 触发验证码，需人工处理")
            if key in self._fetched:
                return [dict(c) for c in self._fetched[key]]  # 幂等重放
            idx = int(vid.rsplit("_", 1)[1]) - self._id_offset
            n = 2 + (idx % 4)  # 2..5
            acc_no = account.rsplit("_", 1)[-1] if account and account.rsplit("_", 1)[-1].isdigit() else "?"
            comments = []
            for j in range(1, n + 1):
                comments.append({
                    "user_id": f"user_{vid}_u{j}",
                    "nickname": f"网友_{acc_no}_{j}",
                    "content": f"[假数据] 智能快递柜相关评论 {idx + 1}-{j}",
                    "comment_time": "2026-08-18 12:00:00",
                    "extra": None,
                    "intent_score": 5 if j == 1 else (3 if j == 2 else 1),
                    "intent_label": "high" if j == 1 else ("medium" if j == 2 else "low"),
                    "reply_suggestion": "可跟进" if j == 1 else None,
                })
            self._fetched[key] = [dict(c) for c in comments]
            return [dict(c) for c in comments]

    def expected_total_comments(self) -> int:
        """与 fetch_comments 完全同源的期望评论总数（供 demo 核对）。"""
        return sum(2 + (i % 4) for i in range(self.video_count))

    def per_account_comment_totals(self):
        """每个账号实际生成（含重放）的总评论数 -> {account: n}。"""
        with self._lock:
            totals: dict = {}
            for (vid, account), comments in self._fetched.items():
                totals[account] = totals.get(account, 0) + len(comments)
            return totals


class Scheduler:
    """P1 任务调度器。契约第 3 节（scheduler.py — 任务调度器）的完整实现。"""

    def __init__(self, db_path: str, bb=None, collector: Collector | None = None,
                 cooldown_handler=None):
        """bb=None 时默认惰性构造 bitbrowser.BitBrowserClient()（本阶段可为 None）。
        collector: Collector 实例，None 时用内置 FakeCollector。
        cooldown_handler: 可选回调 (account_name, seconds) -> bool；
            返回 True 表示「模拟冷却，不真实等待」（demo 用，打印"账号 X 冷却 75s（模拟）"）。
        """
        self.db_path = db_path
        self.conn = db.init_db(
            db_path, check_same_thread=False
        )  # GUI/启动线程/轮询线程共享，访问由 _lock 保护
        self.ctl = sqlite3.connect(db_path, check_same_thread=False)  # 跨线程写（冻结/审计/任务终态）
        self.ctl.row_factory = sqlite3.Row
        self.ctl.execute("PRAGMA busy_timeout=5000")
        self.ctl.execute("PRAGMA journal_mode=WAL")
        self.ctl.execute("PRAGMA foreign_keys=ON")
        self.collector = collector if collector is not None else FakeCollector()
        # 演示采集器必须与 BitBrowser 完全隔离，不能因为 bb=None 又自动构造真实客户端。
        # 浏览器客户端必须由上层显式注入；测试/离线采集器不得因 bb=None
        # 意外连接本机 BitBrowser API。
        self.bb = bb
        self.cooldown_handler = cooldown_handler
        try:
            from .intent_batch import IntentBatchProcessor  # type: ignore
        except ImportError:
            from intent_batch import IntentBatchProcessor  # type: ignore
        self._intent_batch_processor = IntentBatchProcessor(
            db_path, log_callback=self._emit_log
        )
        # GUI 可注入的运行日志回调；未注入时仍保留 stdout 兼容行为。
        self.log_callback = None
        self._log_file_lock = threading.Lock()
        self._log_path = os.path.join(
            os.path.dirname(os.path.abspath(db_path)), "logs", "scheduler.log"
        )
        # 运行租约用于跨进程区分“当前仍在采集”和“上次进程异常退出后的残留”。
        # 同一进程内即使意外创建了两个 Scheduler，也会使用各自租约，避免
        # 一个实例关闭时误清理另一个实例正在使用的账号。
        self._runtime_owner = f"{os.getpid()}:{uuid.uuid4().hex}"
        self._runtime_heartbeat_stop = threading.Event()
        self._runtime_heartbeat_thread = None
        self._runtime_heartbeat_guard = threading.Lock()

        self._lock = threading.RLock()       # 保护以下线程共享状态
        # 共享 this.conn 的串行化锁：self.conn 是 Scheduler 单例共享连接（check_same_thread=False），
        # 多个 start()（不同任务）并发对同一连接写会触发 SystemError，因此所有对 self.conn 的
        # 读/写都必须在这把锁内执行。worker 各自用独立连接（db.init_db），不受此锁约束，
        # 因此采集是真正并行的；这里只串行化"共享连接"上的簿记读写。
        self._conn_lock = threading.RLock()
        self._ctl_lock = threading.RLock()
        self._stop = threading.Event()       # shutdown 全局停止（应用退出）
        # 按任务的暂停事件：task_id -> Event；clear=该任务暂停, set=该任务运行（默认运行）。
        # 每个任务的 worker 只等自己任务的 event，从而实现"按任务编号暂停/继续"，
        # 不再是一个全局暂停事件影响所有任务。
        self._pause_evts: dict = {}          # task_id -> threading.Event
        self._task_stop_evts: dict = {}      # task_id -> Event：按任务停止信号
        self._pause_release_evts: dict = {}  # task_id -> Event：暂停时释放账号并退出旧 worker
        self._global_paused = False          # 无参 pause() = 全局暂停(含未来新任务)
        self._worker_threads: dict = {}      # account_id -> Thread
        self._worker_task: dict = {}         # account_id -> task_id（worker 归属任务）
        self._task_threads: dict = {}        # task_id -> [Thread]
        self._phase_threads: dict = {}       # task_id -> 阶段 A 搜索线程，避免继续时重复启动
        self._phase_account_by_task: dict = {}  # task_id -> 阶段 A 当前占用的账号 id
        self._supervisors: dict = {}         # task_id -> Thread
        self._done_events: dict = {}         # task_id -> Event（wait_for_task 等待）
        self._cd_events: dict = {}           # account_id -> Event（冷却可取消等待）
        self._human_events: dict = {}        # account_id -> Event（人工恢复唤醒）
        self._waiting_accounts: set = set()  # 当前 waiting_human 的账号 id
        self._waiting_tasks: dict = {}       # account_id -> task_id（人工验证归属）
        self._human_reconcile_lock = threading.Lock()
        self._human_reconcile_inflight = False
        # 人工验证完成后，阶段 A 继续搜索优先回到原账号，避免恢复时因
        # 账号列表顺序变化而切换到同任务的其它账号。
        self._resume_account_by_task: dict = {}  # task_id -> account_id
        self._task_status: dict = {}         # 内存态缓存（报表展示用；DB 仍是真源）
        # 用户明确点击“继续”后，允许该任务绕过一次“连续失败”保护，
        # 给浏览器重连逻辑一个恢复机会；不绕过工作时段/限额等其它安全规则。
        self._safety_recovery_tasks: set[int] = set()
        self._collection_run_ids: dict = {}  # task_id -> 当前采集运行编号（最佳努力审计）
        self._monitor_runner = None
        self._waiting_restart_inflight = False
        self._closed = False
        # 进程启动时先修正旧版本/异常退出留下的工作状态。只清理没有
        # 活动线程且租约已经失效的 working/cooldown，不触碰任务、作品、评论。
        self._reconcile_stale_account_states()
        # 任务状态也会跨 GUI 重启持久化；没有线程跟随进程恢复时，
        # 采集中状态必须落成可续跑的暂停，而不能继续显示假运行。
        self._reconcile_stale_task_states()

    def _emit_log(self, message: str) -> None:
        """统一输出后台日志，同时持久化，便于复盘阶段切换和停止原因。"""
        text = str(message)
        print(text)
        try:
            os.makedirs(os.path.dirname(self._log_path), exist_ok=True)
            with self._log_file_lock:
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(f"[{_now_iso()}] [{threading.current_thread().name}] {text}\n")
        except Exception:
            # 日志写入失败不能影响采集任务本身。
            pass
        callback = self.log_callback
        if callable(callback):
            try:
                callback(text)
            except Exception:
                pass

    @staticmethod
    def _build_lead_service(conn):
        """为采集连接构造线索服务；采集器只负责落评论，服务负责入线索。"""
        from leads.repository import LeadRepository
        from leads.service import LeadService

        return LeadService(LeadRepository(conn))

    def _ensure_collection_run(self, task: dict) -> str | None:
        """为一次 start/continue 建立可追溯运行编号；失败不阻断老采集流程。"""
        task_id = int(task["id"])
        current = self._collection_run_ids.get(task_id)
        if current:
            try:
                with self._ctl_lock:
                    row = self.ctl.execute(
                        "SELECT status FROM collection_runs WHERE run_id = ?", (current,)
                    ).fetchone()
                if row and row["status"] == "running":
                    return current
            except Exception:
                pass
        try:
            from operations.collection_runs import CollectionRunStore
            with self._ctl_lock:
                store = CollectionRunStore(self.ctl)
                run_id = store.create(
                    task_id,
                    task.get("platform") or "unknown",
                    execution_mode=task.get("execution_mode") or "manual",
                    metadata={"keyword": task.get("keyword"), "target_count": task.get("target_count")},
                )
            self._collection_run_ids[task_id] = run_id
            return run_id
        except Exception as exc:
            self._emit_log(f"[run] 运行记录初始化失败，继续采集：{type(exc).__name__}: {exc}")
            return None

    def _update_collection_run(self, task_id: int, **kwargs) -> None:
        run_id = self._collection_run_ids.get(int(task_id))
        if not run_id:
            return
        try:
            from operations.collection_runs import CollectionRunStore
            with self._ctl_lock:
                CollectionRunStore(self.ctl).update(run_id, **kwargs)
                if kwargs.get("status") in {"completed", "no_more", "failed", "cancelled"}:
                    # 监控规则的下一次运行时间只在本轮真正结束后推进；
                    # 这样“无更多内容”可被明确记录，同时不会在任务运行中重复触发。
                    task = self._r(self.ctl.execute(
                        "SELECT execution_mode FROM tasks WHERE id = ?", (int(task_id),)
                    ).fetchone())
                    if task.get("execution_mode") == "monitoring":
                        from operations.monitoring import MonitoringRuleStore
                        MonitoringRuleStore(self.ctl).mark_run(
                            int(task_id), run_id, no_more=kwargs.get("status") == "no_more"
                        )
                    self._collection_run_ids.pop(int(task_id), None)
        except Exception as exc:
            self._emit_log(f"[run] 运行记录更新失败：{type(exc).__name__}: {exc}")

    def _record_collection_event(self, task_id: int, stage: str, event_type: str,
                                 message: str = "", payload: dict | None = None) -> None:
        run_id = self._collection_run_ids.get(int(task_id))
        if not run_id:
            return
        try:
            from operations.collection_runs import CollectionRunStore
            with self._ctl_lock:
                CollectionRunStore(self.ctl).event(run_id, stage, event_type, message, payload)
        except Exception as exc:
            self._emit_log(f"[run] 运行事件写入失败：{type(exc).__name__}: {exc}")

    def _ingest_comment_to_lead(self, conn, comment_id, lead_service=None,
                                *, task_id=None, video_id=None):
        """评论落库后同步生成/更新线索，失败不阻断原有采集流程。"""
        if comment_id is None:
            return None
        try:
            service = lead_service or self._build_lead_service(conn)
            context = {}
            if task_id is not None:
                context["task_id"] = task_id
                # 任务归属决定线索归属；任务是员工隔离的根节点。
                try:
                    owner_row = conn.execute(
                        "SELECT owner_user_id FROM tasks WHERE id = ?", (int(task_id),)
                    ).fetchone()
                    if owner_row and owner_row["owner_user_id"] is not None:
                        context["data_owner_user_id"] = int(owner_row["owner_user_id"])
                except Exception:
                    # 兼容极旧库/迁移失败场景，不阻断原有采集。
                    pass
            if video_id is not None:
                context["video_id"] = video_id
            lead_id = service.ingest_comment(comment_id, context=context)
            owner_id = context.get("data_owner_user_id")
            if owner_id:
                try:
                    try:
                        from .data_scope import SyncStore  # type: ignore
                    except ImportError:  # pragma: no cover
                        from data_scope import SyncStore  # type: ignore
                    lead_row = conn.execute(
                        "SELECT * FROM leads WHERE id = ?", (int(lead_id),)
                    ).fetchone()
                    comment_row = conn.execute(
                        "SELECT * FROM comments WHERE id = ?", (int(comment_id),)
                    ).fetchone()
                    if lead_row:
                        SyncStore(conn).enqueue(
                            int(owner_id), "lead", int(lead_id), dict(lead_row)
                        )
                    if comment_row:
                        SyncStore(conn).enqueue(
                            int(owner_id), "comment", int(comment_id), dict(comment_row)
                        )
                except Exception as exc:
                    self._emit_log(
                        f"[sync] 评论 {comment_id} 同步队列写入失败：{type(exc).__name__}: {exc}"
                    )
            return lead_id
        except Exception as exc:  # noqa: BLE001 线索扩展失败不能丢采集结果
            self._emit_log(f"[leads] 评论 {comment_id} 转线索失败：{type(exc).__name__}: {exc}")
            return None

    def _handle_collected_comment(self, conn, comment_id, lead_service=None,
                                  *, task_id=None, video_id=None, comment=None):
        """评论入线索后触发意向批处理；触发失败不影响采集。"""
        self._ingest_comment_to_lead(
            conn, comment_id, lead_service,
            task_id=task_id, video_id=video_id,
        )
        try:
            self._intent_batch_processor.on_comment(
                comment_id, task_id=task_id, comment=comment
            )
        except Exception as exc:  # noqa: BLE001
            self._emit_log(f"[intent] 评论 {comment_id} 批处理触发失败：{type(exc).__name__}: {exc}")

    @staticmethod
    def _record_task_error(conn, task_id: int, detail: str) -> None:
        """记录最近一次视频级异常，但不把整个任务误标记为 failed。"""
        conn.execute(
            "UPDATE tasks SET error_message = ?, updated_at = ? WHERE id = ?",
            (str(detail)[:1000], _now_iso(), task_id),
        )

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _default_bb():
        """bb 缺省时构造 BitBrowserClient()；bitbrowser.py 未就绪则回落 None。"""
        try:
            try:
                from . import bitbrowser  # type: ignore
            except ImportError:
                import bitbrowser  # type: ignore
            return bitbrowser.BitBrowserClient()  # noqa: F821
        except Exception:
            return None  # bitbrowser 尚未交付（并行开发期）；本阶段不真连

    @staticmethod
    def _r(row) -> dict:
        """sqlite3.Row / dict / tuple -> dict（兼容不同数据层实现）。"""
        if row is None:
            return {}
        if isinstance(row, dict):
            return row
        if hasattr(row, "keys"):
            return dict(row)
        return {"id": row[0]} if row else {}

    @staticmethod
    def _is_demo_account(account: dict) -> bool:
        """判断是否为演示账号；真实 BitBrowser 不得尝试打开这类窗口。"""
        name = str(account.get("name") or "")
        window_id = str(account.get("bb_window_id") or "").strip().lower()
        return name in {"演示账号1", "演示账号2", "演示账号3"} or window_id.startswith("demo-")

    def _acct_fields(self, conn, account_id: int, **fields):
        """直接写账号表白名单列（契约便捷函数未覆盖的字段）。"""
        cols = {k: v for k, v in fields.items() if k in _ACCT_WRITABLE}
        if not cols or account_id is None:
            return
        sets = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(f"UPDATE accounts SET {sets} WHERE id = ?", (*cols.values(), account_id))

    def _acct_status(self, conn, account_id: int, status: str):
        """写入账号状态，并同步维护当前调度器的运行租约。"""
        account_id = int(account_id)
        if status in _RUNTIME_ACCOUNT_STATUSES:
            conn.execute(
                "UPDATE accounts SET status = ?, runtime_owner = ?, "
                "runtime_heartbeat = ?, wait_reason = NULL, wait_since = NULL "
                "WHERE id = ?",
                (status, self._runtime_owner, _now_iso(), account_id),
            )
            conn.commit()
            self._ensure_runtime_heartbeat()
            return
        if status == "idle":
            # 只有 worker/搜索阶段真正退出后才会走到这里；清理冷却和租约，
            # 使账号可以马上被其它任务领取。
            conn.execute(
                "UPDATE accounts SET status = 'idle', cd_until = NULL, "
                "wait_reason = NULL, wait_since = NULL, runtime_owner = NULL, "
                "runtime_heartbeat = NULL WHERE id = ?",
                (account_id,),
            )
            conn.commit()
            return
        if status in ("waiting_human", "frozen", "dead"):
            conn.execute(
                "UPDATE accounts SET status = ?, runtime_owner = NULL, "
                "runtime_heartbeat = NULL WHERE id = ?",
                (status, account_id),
            )
            conn.commit()
            return
        db.update_account_status(conn, account_id, status)

    def _ensure_runtime_heartbeat(self) -> None:
        """懒启动本调度器的账号租约心跳线程。"""
        with self._runtime_heartbeat_guard:
            current = self._runtime_heartbeat_thread
            if current is not None and current.is_alive():
                return
            if self._closed:
                return
            self._runtime_heartbeat_stop.clear()
            thread = threading.Thread(
                target=self._runtime_heartbeat_loop,
                name="scheduler-account-heartbeat",
                daemon=True,
            )
            self._runtime_heartbeat_thread = thread
            thread.start()

    def _runtime_heartbeat_loop(self) -> None:
        """周期刷新当前进程持有的账号租约，防止长耗时浏览器操作被误清理。"""
        while not self._runtime_heartbeat_stop.wait(_RUNTIME_HEARTBEAT_INTERVAL):
            try:
                with self._ctl_lock:
                    self.ctl.execute(
                        "UPDATE accounts SET runtime_heartbeat = ? "
                        "WHERE runtime_owner = ? AND status IN ('working','cooldown')",
                        (_now_iso(), self._runtime_owner),
                    )
                    self.ctl.commit()
            except (sqlite3.Error, RuntimeError):
                # 关闭阶段连接可能已经释放；心跳不能影响采集和退出流程。
                pass

    @staticmethod
    def _process_alive(pid: int) -> bool:
        """仅用标准库检查租约进程是否仍存在，兼容 Windows/Python。"""
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except (OSError, ProcessLookupError, SystemError, ValueError, OverflowError):
            # Windows 在强制结束旧 GUI/浏览器后，极少数 Python 运行时会把
            # 无效进程句柄包装成 SystemError（底层仍是 WinError 6）。
            # 这里统一按“租约进程已结束”处理，不能让启动阶段崩溃。
            return False
        return True

    @classmethod
    def _runtime_lease_alive(cls, owner, heartbeat) -> bool:
        """判断数据库里的账号租约是否仍可能属于存活进程。"""
        if not owner or not heartbeat:
            return False
        try:
            pid = int(str(owner).split(":", 1)[0])
        except (TypeError, ValueError):
            return False
        heartbeat_at = _iso_to_dt(heartbeat)
        if heartbeat_at is None:
            return False
        age = (datetime.datetime.now() - heartbeat_at).total_seconds()
        return age <= _RUNTIME_LEASE_TIMEOUT and cls._process_alive(pid)

    def _reconcile_stale_account_states(self) -> dict:
        """清理没有活动线程/有效租约的 working、cooldown 残留状态。

        账号状态是跨 GUI 重启持久化的，但线程和调度器内存态不会持久化。
        因此不能只看数据库里的 status：正常运行的其它进程有新鲜租约时保留，
        上次异常退出或旧版本未写租约时恢复 idle。返回值供启动日志和测试使用。
        """
        cleared = []
        with self._conn_lock, self._lock:
            rows = self.conn.execute(
                "SELECT id, name, status, runtime_owner, runtime_heartbeat "
                "FROM accounts WHERE status IN ('working','cooldown') ORDER BY id"
            ).fetchall()
            for row in rows:
                account_id = int(row["id"])
                if self._thread_alive(account_id):
                    continue
                # 当前调度器拥有的租约没有活动线程时，内存态是更强的真源；
                # 不能因为心跳线程还活着而把已经结束的账号继续显示为 working。
                if (str(row["runtime_owner"] or "") != self._runtime_owner and
                        self._runtime_lease_alive(row["runtime_owner"], row["runtime_heartbeat"])):
                    continue
                cur = self.conn.execute(
                    "UPDATE accounts SET status = 'idle', cd_until = NULL, "
                    "wait_reason = NULL, wait_since = NULL, runtime_owner = NULL, "
                    "runtime_heartbeat = NULL "
                    "WHERE id = ? AND status IN ('working','cooldown')",
                    (account_id,),
                )
                if cur.rowcount:
                    cleared.append({
                        "account_id": account_id,
                        "name": str(row["name"] or ""),
                        "from_status": str(row["status"] or ""),
                    })
            if cleared:
                self.conn.commit()
        for item in cleared:
            self._emit_log(
                f"[scheduler] 启动状态校正：账号#{item['account_id']} "
                f"{item['from_status']} → idle（未发现活动采集线程或有效租约）"
            )
        return {"cleared": cleared}

    def _reconcile_stale_task_states(self) -> dict:
        """将重启后无活动线程的采集中任务恢复为可继续的暂停态。"""
        stale = []
        runtime_statuses = {"phase_a_search", "phase_b_comments", "running"}
        with self._conn_lock, self._lock:
            task_rows = self.conn.execute(
                "SELECT * FROM tasks WHERE status IN "
                "('phase_a_search','phase_b_comments','running') ORDER BY id"
            ).fetchall()
            for raw_task in task_rows:
                task = self._r(raw_task)
                task_id = int(task.get("id") or 0)
                if not task_id:
                    continue
                if (
                    any(thread.is_alive() for thread in self._task_threads.get(task_id, []))
                    or (self._phase_threads.get(task_id) is not None
                        and self._phase_threads[task_id].is_alive())
                    or (self._supervisors.get(task_id) is not None
                        and self._supervisors[task_id].is_alive())
                ):
                    continue

                bound_names = task.get("task_accounts") or "[]"
                if isinstance(bound_names, str):
                    try:
                        bound_names = _json.loads(bound_names)
                    except (TypeError, ValueError):
                        bound_names = []
                if not isinstance(bound_names, list):
                    bound_names = []
                names = {str(item).strip() for item in bound_names if str(item).strip()}
                assigned_rows = self.conn.execute(
                    "SELECT DISTINCT assigned_account FROM videos "
                    "WHERE task_id = ? AND assigned_account IS NOT NULL",
                    (task_id,),
                ).fetchall()
                names.update(
                    str(row["assigned_account"] or "").strip()
                    for row in assigned_rows if str(row["assigned_account"] or "").strip()
                )
                running_run = self.conn.execute(
                    "SELECT run_id, account_id FROM collection_runs WHERE task_id = ? "
                    "AND status = 'running' ORDER BY started_at DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                account_rows = self.conn.execute(
                    "SELECT id, name, status, runtime_owner, runtime_heartbeat, wait_reason "
                    "FROM accounts WHERE platform = ?",
                    (str(task.get("platform") or "douyin"),),
                ).fetchall()
                if names:
                    relevant = [
                        row for row in account_rows
                        if str(row["name"] or "") in names
                    ]
                elif running_run and running_run["account_id"]:
                    relevant = [
                        row for row in account_rows
                        if int(row["id"] or 0) == int(running_run["account_id"])
                    ]
                else:
                    # 无绑定账号且运行记录未记录账号时，无法证明是其它进程
                    # 的新鲜任务租约；按异常退出恢复为暂停更安全。
                    relevant = []
                active_lease = any(
                    str(row["status"] or "") in runtime_statuses
                    and str(row["runtime_owner"] or "") != self._runtime_owner
                    and self._runtime_lease_alive(
                        row["runtime_owner"], row["runtime_heartbeat"]
                    )
                    for row in relevant
                )
                if active_lease:
                    continue

                human_reason = next(
                    (
                        str(row["wait_reason"] or "").strip()
                        for row in relevant
                        if str(row["status"] or "") == "waiting_human"
                        and str(row["wait_reason"] or "").strip()
                    ),
                    "",
                )
                previous_status = str(task.get("status") or "")
                reason = (
                    f"需要人工验证：{human_reason}"
                    if human_reason else
                    f"应用异常退出：上次处于{previous_status}，已暂停，可点击继续"
                )
                db.update_task_status(self.conn, task_id, "paused", reason)
                run = running_run
                if run:
                    self.conn.execute(
                        "UPDATE collection_runs SET status = 'paused', finished_at = ?, "
                        "stop_reason = ? WHERE run_id = ? AND status = 'running'",
                        (_now_iso(), reason, str(run["run_id"])),
                    )
                if previous_status == "phase_a_search":
                    self.conn.execute(
                        "UPDATE task_search_queries SET status = 'incomplete', updated_at = ? "
                        "WHERE task_id = ? AND status = 'in_progress'",
                        (_now_iso(), task_id),
                    )
                stale.append({
                    "task_id": task_id,
                    "from_status": previous_status,
                    "reason": reason,
                })
            if stale:
                self.conn.commit()
        for item in stale:
            self._emit_log(
                f"[scheduler] 启动状态校正：任务#{item['task_id']} "
                f"{item['from_status']} → paused（{item['reason']}）"
            )
        return {"cleared": stale}

    def _account_safety_decision(self, conn, account_id: int, account_name: str,
                                 operation: str = "collect"):
        """在实际采集动作前执行账号安全预检，并返回可解释的判定。"""
        from operations.account_safety import AccountSafetyStore

        now = datetime.datetime.now()
        hour_ago = (now - datetime.timedelta(hours=1)).isoformat(timespec="seconds")
        day_ago = (now - datetime.timedelta(days=1)).isoformat(timespec="seconds")
        hourly = conn.execute(
            "SELECT COUNT(*) AS n FROM videos WHERE assigned_account = ? "
            "AND status = 'done' AND collected_at >= ?",
            (account_name, hour_ago),
        ).fetchone()["n"]
        daily = conn.execute(
            "SELECT COUNT(*) AS n FROM interaction_events WHERE account_id = ? "
            "AND event_type IN ('sent', 'reply_sent') AND created_at >= ?",
            (account_id, day_ago),
        ).fetchone()["n"]
        last_row = conn.execute(
            "SELECT MAX(collected_at) AS latest FROM videos WHERE assigned_account = ? "
            "AND collected_at IS NOT NULL", (account_name,)
        ).fetchone()
        last_action = None
        if last_row and last_row["latest"]:
            try:
                last_action = datetime.datetime.fromisoformat(last_row["latest"])
            except ValueError:
                last_action = None
        failures = self._consecutive_failure_count(conn, account_name)
        return AccountSafetyStore(conn).check(
            account_id, operation, now=now, hourly_collected=int(hourly or 0),
            daily_replied=int(daily or 0), last_action_at=last_action,
            consecutive_failures=int(failures or 0),
        )

    @staticmethod
    def _consecutive_failure_count(conn, account_name: str) -> int:
        """统计账号最近一段连续失败，不把历史失败累计成暂停条件。"""
        rows = conn.execute(
            "SELECT status FROM videos "
            "WHERE assigned_account = ? AND status IN ('done', 'failed') "
            "ORDER BY id DESC",
            (account_name,),
        ).fetchall()
        count = 0
        for row in rows:
            if str(row["status"] if hasattr(row, "keys") else row[0]) != "failed":
                break
            count += 1
        return count

    @staticmethod
    def _is_browser_connection_error(exc: BaseException) -> bool:
        """区分 CDP/浏览器断线与作品或平台页面本身的采集错误。"""
        text = f"{type(exc).__name__}: {exc}".lower()
        return any(marker in text for marker in (
            "connectionclosed", "connection closed", "no close frame",
            "websocket is closed", "not connected", "broken pipe",
            "connection reset",
        ))

    def _consume_safety_recovery(self, task_id: int) -> bool:
        """消费一次人工继续触发的连续失败恢复机会。"""
        with self._lock:
            tid = int(task_id)
            if tid not in self._safety_recovery_tasks:
                return False
            self._safety_recovery_tasks.discard(tid)
            return True

    @staticmethod
    def _release_video_for_retry(conn, video_id: int) -> None:
        """连接断开时保留作品，避免把可恢复的任务计入失败。"""
        conn.execute(
            "UPDATE videos SET status = 'assigned' "
            "WHERE id = ? AND status = 'collecting'", (video_id,)
        )

    def _log_human_action(self, account_id, action: str, detail: str):
        # human_actions 审计（契约 DDL 表），跨线程写走 self.ctl
        try:
            with self._ctl_lock:
                self.ctl.execute(
                    "INSERT INTO human_actions (account_id, action, detail) VALUES (?, ?, ?)",
                    (account_id, action, detail),
                )
                self.ctl.commit()
        except Exception:
            pass  # 审计失败不阻断主流程

    def _all_accounts(self):
        cur = self.conn.execute("SELECT * FROM accounts ORDER BY id")
        return [self._r(r) for r in cur.fetchall()]

    @staticmethod
    def _account_matches_bound_name(account: dict, bound_names) -> bool:
        """兼容旧任务：绑定值可能是内部标识，也可能是旧版平台昵称。"""
        values = {
            str(account.get("name") or "").strip(),
            str(account.get("nickname") or "").strip(),
        }
        return bool(values.intersection({str(item or "").strip() for item in bound_names}))

    def _all_tasks(self):
        cur = self.conn.execute("SELECT * FROM tasks ORDER BY id")
        return [self._r(r) for r in cur.fetchall()]

    def _task_video_count(self, task_id: int) -> int:
        cur = self.conn.execute("SELECT COUNT(*) c FROM videos WHERE task_id = ?", (task_id,))
        r = cur.fetchone()
        return int(r["c"]) if r else 0

    def _task_done_count(self, task_id: int) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) c FROM videos WHERE task_id = ? AND status = 'done'",
            (task_id,),
        )
        r = cur.fetchone()
        return int(r["c"]) if r else 0

    def _task_done_count_ctl(self, task_id: int) -> int:
        cur = self.ctl.execute(
            "SELECT COUNT(*) c FROM videos WHERE task_id = ? AND status = 'done'",
            (task_id,),
        )
        r = cur.fetchone()
        return int(r["c"]) if r else 0

    def _task_video_left(self, task_id: int) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) c FROM videos WHERE task_id = ? AND status NOT IN ('done','failed')",
            (task_id,),
        )
        r = cur.fetchone()
        return int(r["c"]) if r else 0

    def _task_video_left_ctl(self, task_id: int) -> int:
        """供后台线程（supervisor）使用的同义查询 —— 走 check_same_thread=False 的 self.ctl。"""
        cur = self.ctl.execute(
            "SELECT COUNT(*) c FROM videos WHERE task_id = ? AND status NOT IN ('done','failed')",
            (task_id,),
        )
        r = cur.fetchone()
        return int(r["c"]) if r else 0

    def _task_search_exhausted_ctl(self, task_id: int) -> bool:
        row = self.ctl.execute(
            "SELECT search_exhausted FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return bool(row and int(row["search_exhausted"] or 0))

    def _mark_search_exhausted(self, task_id: int, reason: str):
        """持久化“已明确没有更多结果”，重启后也禁止重复搜索。"""
        now = db._now_iso()
        with self._conn_lock:
            self.conn.execute(
                "UPDATE tasks SET search_exhausted = 1, search_stop_reason = ?, "
                "search_stopped_at = ?, updated_at = ? WHERE id = ?",
                (str(reason or "确认没有更多视频"), now, now, task_id),
            )
            self.conn.commit()

    def _ensure_task_search_queries(self, task: dict, target_count: int) -> list[dict]:
        """为任务创建一次性的逐词搜索快照。关键词组后续修改不影响本任务。"""
        task_id = int(task["id"])
        try:
            rows = self.conn.execute(
                "SELECT * FROM task_search_queries WHERE task_id = ? ORDER BY query_order",
                (task_id,),
            ).fetchall()
        except sqlite3.Error:
            return [{"query_order": 1, "query": str(task.get("keyword") or "").strip(),
                     "exclude_terms": "[]", "target_count": int(target_count),
                     "status": "pending"}]
        if rows:
            return [self._r(row) for row in rows]

        entries = []
        group_id = task.get("keyword_group_id")
        if group_id:
            try:
                from operations.keywords import KeywordGroupStore
                entries = KeywordGroupStore(self.conn).expand(int(group_id))
            except Exception as exc:  # noqa: BLE001
                self._emit_log(f"[scheduler] 任务 {task_id} 读取关键词组失败，回退任务关键词：{exc}")
        if not entries:
            keyword = str(task.get("keyword") or "").strip()
            if keyword:
                entries = [{"query": keyword, "exclude_terms": []}]
        now = db._now_iso()
        self.conn.executemany(
            "INSERT OR IGNORE INTO task_search_queries "
            "(task_id, query_order, query, exclude_terms, target_count, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            [(task_id, index, str(item.get("query") or "").strip(),
              _json.dumps(item.get("exclude_terms") or [], ensure_ascii=False),
              int(target_count), now, now)
             for index, item in enumerate(entries, 1) if str(item.get("query") or "").strip()],
        )
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT * FROM task_search_queries WHERE task_id = ? ORDER BY query_order",
            (task_id,),
        ).fetchall()
        return [self._r(row) for row in rows]

    def _task_query_count(self, task_id: int, query: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM videos WHERE task_id = ? AND search_query = ?",
            (int(task_id), str(query)),
        ).fetchone()
        return int(row["c"] or 0) if row else 0

    def _reset_task_search_queries(self, task_id: int, *, only_open: bool = False) -> None:
        """继续采集或监控新一轮开始时，重新打开尚未达标的关键词。"""
        where = "AND status IN ('no_more', 'incomplete')" if only_open else ""
        self.conn.execute(
            f"UPDATE task_search_queries SET status = 'pending', updated_at = ? "
            f"WHERE task_id = ? {where}",
            (db._now_iso(), int(task_id)),
        )
        self.conn.execute(
            "UPDATE tasks SET search_exhausted = 0, search_stop_reason = NULL, "
            "search_stopped_at = NULL, search_phase_complete = 0, updated_at = ? WHERE id = ?",
            (db._now_iso(), int(task_id)),
        )
        self.conn.commit()

    def _recover_keyword_search_phase(self, task: dict, task_id: int,
                                      query_rows: list[dict]) -> bool:
        """恢复关键词组的阶段 A/B 边界，避免评论采集中断后重新搜索。"""
        if not query_rows:
            return False
        task_status = str(task.get("status") or "")
        phase_b_started = task_status in ("phase_b_comments", "running", "done")
        changed = False
        with self._conn_lock:
            if phase_b_started:
                # 任务已经落到了阶段 B，说明搜索阶段在进入评论采集前应已结束。
                # 对意外中断时仍停留 in_progress/pending 的关键词补记终态，
                # 绝不重新打开搜索。
                for row in query_rows:
                    if str(row.get("status") or "") in ("completed", "no_more"):
                        continue
                    query = str(row.get("query") or "")
                    target = max(1, int(row.get("target_count") or task.get("target_count") or 100))
                    count = self._task_query_count(task_id, query)
                    status = "completed" if count >= target else "no_more"
                    self.conn.execute(
                        "UPDATE task_search_queries SET status = ?, updated_at = ? "
                        "WHERE task_id = ? AND query_order = ?",
                        (status, db._now_iso(), task_id, int(row.get("query_order") or 0)),
                    )
                    changed = True
                if changed:
                    query_rows[:] = [self._r(row) for row in self.conn.execute(
                        "SELECT * FROM task_search_queries WHERE task_id = ? ORDER BY query_order",
                        (task_id,),
                    ).fetchall()]
            terminal = bool(query_rows) and all(
                str(row.get("status") or "") in ("completed", "no_more")
                for row in query_rows
            )
            if not terminal:
                return False
            all_reached = all(
                self._task_query_count(task_id, row.get("query")) >=
                int(row.get("target_count") or task.get("target_count") or 1)
                for row in query_rows
            )
            has_no_more = any(str(row.get("status") or "") == "no_more" for row in query_rows)
            self.conn.execute(
                "UPDATE tasks SET search_phase_complete = 1, search_exhausted = ?, "
                "updated_at = ? WHERE id = ?",
                (int(has_no_more and not all_reached), db._now_iso(), task_id),
            )
            self.conn.commit()
        if changed:
            self._emit_log(
                f"[scheduler] 任务 {task_id} 恢复时补记关键词搜索终态，"
                "阶段A已完成，不重新搜索，继续阶段B评论采集"
            )
        return True

    def _run_keyword_group_search(self, task: dict, task_id: int,
                                   query_rows: list[dict], *, is_monitoring: bool) -> bool:
        """按关键词顺序完成阶段 A；所有关键词结束后才允许进入阶段 B。"""
        search_account = None
        try:
            with self._conn_lock, self._lock:
                bound_names = task.get("task_accounts") or "[]"
                if isinstance(bound_names, str):
                    try:
                        bound_names = _json.loads(bound_names)
                    except Exception:
                        bound_names = []
                candidates = [a for a in self._all_accounts()
                              if (a.get("platform") or "douyin") == task["platform"]
                              and (not bound_names or self._account_matches_bound_name(a, bound_names))
                              and (self.bb is None or not self._is_demo_account(a))
                              and a.get("status") not in ("waiting_human", "dead", "cooldown")
                              and not self._thread_alive(a["id"])
                              and not self._is_waiting(a["id"])]
                preferred_id = self._resume_account_by_task.get(int(task_id))
                if preferred_id is not None:
                    candidates.sort(key=lambda a: 0 if int(a.get("id", -1)) == int(preferred_id) else 1)
                search_account = candidates[0] if candidates else None
                if search_account:
                    self._phase_account_by_task[int(task_id)] = int(search_account["id"])
                    self._acct_status(self.conn, int(search_account["id"]), "working")
                    self._update_collection_run(task_id, account_id=int(search_account["id"]))
                if search_account is None:
                    any_account = any(
                        (a.get("platform") or "douyin") == task["platform"]
                        and (not bound_names or self._account_matches_bound_name(a, bound_names))
                        and (self.bb is None or not self._is_demo_account(a))
                        for a in self._all_accounts()
                    )
                    db.update_task_status(self.conn, task_id,
                                          "waiting_account" if any_account else "no_account")
                    self._task_status[task_id] = "waiting_account" if any_account else "no_account"
                    self._phase_threads.pop(task_id, None)
                    self._emit_log(f"[scheduler] 任务 {task_id} 暂无可派出的{task['platform']}账号，等待账号释放后重新搜索")
                    return False

            selected_sort = task.get("search_sort", "default") or "default"
            self._emit_log(
                f"[scheduler] 任务 {task_id} 进入关键词组逐词搜索：共 {len(query_rows)} 个关键词，"
                f"排序={sort_label(task.get('platform', 'douyin'), selected_sort)}"
            )
            import json
            selected_types = task.get("collect_types") or []
            if isinstance(selected_types, str):
                try:
                    selected_types = json.loads(selected_types)
                except Exception:
                    selected_types = DEFAULT_COLLECT_TYPES
            if not selected_types:
                selected_types = DEFAULT_COLLECT_TYPES
            only_with_comments = bool(task.get("only_with_comments"))
            comment_collector = getattr(self.collector, "collect_with_comments", None)
            progress_types = selected_types
            lead_service = None
            if only_with_comments and callable(comment_collector):
                try:
                    lead_service = self._build_lead_service(self.conn)
                except Exception as exc:  # noqa: BLE001
                    self._emit_log(f"[leads] 线索服务初始化失败，继续保留评论采集：{exc}")

            for query_row in query_rows:
                query = str(query_row.get("query") or "").strip()
                if not query:
                    continue
                status = str(query_row.get("status") or "pending")
                query_target = max(1, int(query_row.get("target_count") or task.get("target_count") or 100))
                existing_count = self._task_query_count(task_id, query)
                if status == "completed" or existing_count >= query_target:
                    self.conn.execute(
                        "UPDATE task_search_queries SET status = 'completed', updated_at = ? "
                        "WHERE task_id = ? AND query_order = ?",
                        (db._now_iso(), task_id, int(query_row["query_order"])),
                    )
                    self.conn.commit()
                    continue
                if status == "no_more":
                    continue
                self._raise_for_task_control(task_id)
                remaining_target = max(1, query_target - existing_count)
                # 标记当前关键词正在处理。暂停、人工接管、进程重启或异常退出时，
                # 该状态不会被当作完成；恢复时必须回到这个关键词继续搜索。
                with self._conn_lock:
                    self.conn.execute(
                        "UPDATE task_search_queries SET status = 'in_progress', updated_at = ? "
                        "WHERE task_id = ? AND query_order = ?",
                        (db._now_iso(), task_id, int(query_row["query_order"])),
                    )
                    self.conn.commit()
                self._emit_log(
                    f"[scheduler] 任务 {task_id} 搜索关键词 {int(query_row['query_order'])}/{len(query_rows)}："
                    f"{query}，目标 {query_target}，本次补采 {remaining_target}"
                )
                search_meta = None

                if only_with_comments and callable(comment_collector):
                    def _on_prefetched_item(item):
                        self._raise_for_task_control(task_id)
                        with self._conn_lock:
                            if self._task_query_count(task_id, query) >= query_target:
                                return
                            extra = item.get("extra")
                            if isinstance(extra, dict) and "engagement" not in progress_types:
                                extra = {k: v for k, v in extra.items()
                                         if k not in ("digg", "digg_count", "like_count", "collect_count", "share_count")}
                            video_id = db.insert_video(
                                self.conn, task_id, item["vid"], item.get("url", ""),
                                item.get("title") if "video_info" in progress_types else None,
                                item.get("author") if "author_info" in progress_types else None,
                                extra, platform=task["platform"], search_query=query)
                            for comment in item.get("comments", []) or []:
                                c_extra = comment.get("extra") or {}
                                if not isinstance(c_extra, dict):
                                    c_extra = {}
                                if comment.get("region") not in (None, "") and "region" in progress_types:
                                    c_extra["region"] = comment.get("region")
                                if comment.get("cid") not in (None, ""):
                                    c_extra["platform_comment_id"] = str(comment.get("cid"))
                                if "engagement" in progress_types:
                                    for key in ("digg", "digg_count"):
                                        if comment.get(key) not in (None, ""):
                                            c_extra[key] = comment.get(key)
                                comment_id = db.insert_comment(
                                    self.conn, video_id,
                                    comment.get("user_id") if "comment_user" in progress_types else None,
                                    comment.get("nickname") if "comment_user" in progress_types else None,
                                    comment.get("content") if "comments" in progress_types else None,
                                    comment.get("comment_time"), extra=c_extra,
                                    intent_score=comment.get("intent_score", 0) if "intent" in progress_types else 0,
                                    intent_label=comment.get("intent_label") if "intent" in progress_types else None,
                                    reply_suggestion=comment.get("reply_suggestion") if "intent" in progress_types else None,
                                    platform=task["platform"])
                                self._handle_collected_comment(
                                    self.conn, comment_id, lead_service,
                                    task_id=task_id, video_id=video_id,
                                    comment=comment)
                            self.conn.execute(
                                "UPDATE videos SET status = 'done', collected_at = ? WHERE id = ?",
                                (db._now_iso(), video_id))
                            self.conn.commit()

                    bundle = comment_collector(
                        query, target_count=remaining_target,
                        mode=task.get("collect_mode", "standard"),
                        platform=task.get("platform", "weibo"),
                        window_id=(search_account or {}).get("bb_window_id"),
                        progress_callback=_on_prefetched_item,
                        **self._control_kwargs(comment_collector, task_id, search_sort=selected_sort),
                    ) or {}
                    results = bundle.get("items", []) if isinstance(bundle, dict) else []
                    search_meta = bundle if isinstance(bundle, dict) else None
                else:
                    raw_results = self.collector.search(
                        query, task["platform"], task.get("collect_mode", "standard"), remaining_target,
                        window_id=(search_account or {}).get("bb_window_id"),
                        **self._control_kwargs(self.collector.search, task_id, search_sort=selected_sort),
                    )
                    results = [] if raw_results is None else raw_results

                if self._task_stop_requested(task_id):
                    if self._pause_release_requested(task_id):
                        raise TaskPaused()
                    raise TaskCancelled()

                # 如果采集器在暂停窗口内返回了部分结果，当前关键词不能被视为
                # 已完成；即使返回对象带有默认的 search_complete，也必须回到
                # 当前关键词继续，禁止直接切换下一个关键词。
                paused_during_search = not self._task_pause_evt(task_id).is_set()
                search_complete = bool(getattr(results, "search_complete", True))
                if search_meta is not None:
                    search_complete = bool(search_meta.get("search_complete", search_complete))
                reached_target = bool((search_meta or {}).get("reached_target", getattr(results, "reached_target", False)))
                no_more_results = bool((search_meta or {}).get("no_more_results", getattr(results, "no_more_results", False)))
                termination_detail = ((search_meta or {}).get("termination_reason", "")
                                      or getattr(results, "termination_reason", ""))
                candidate_vids = []
                candidate_seen = set()
                for item in results:
                    vid = str(item.get("vid") or "") if isinstance(item, dict) else ""
                    if vid and vid not in candidate_seen:
                        candidate_seen.add(vid)
                        candidate_vids.append(vid)
                with self._conn_lock:
                    existing_rows = self.conn.execute(
                        "SELECT vid FROM videos WHERE task_id = ? AND platform = ?",
                        (task_id, task["platform"]),
                    ).fetchall()
                    existing_vids = {str(row[0]) for row in existing_rows}
                    new_count = sum(1 for vid in candidate_vids if vid not in existing_vids)
                    duplicate_count = len(candidate_vids) - new_count
                    for item in results:
                        extra = item.get("extra")
                        if isinstance(extra, dict) and "engagement" not in selected_types:
                            extra = {k: v for k, v in extra.items()
                                     if k not in ("digg", "digg_count", "like_count", "collect_count", "share_count")}
                        db.insert_video(self.conn, task_id, item["vid"], item.get("url", ""),
                                        item.get("title") if "video_info" in selected_types else None,
                                        item.get("author") if "author_info" in selected_types else None,
                                        extra, platform=task["platform"], search_query=query)
                    query_count = self._task_query_count(task_id, query)
                    # 旧版/测试采集器只返回普通 list，没有终止元数据；返回的数量已达到本词目标时可安全完成。
                    legacy_reached = (search_meta is None and not hasattr(results, "search_complete")
                                      and bool(candidate_vids) and query_count >= query_target)
                    if legacy_reached:
                        reached_target = True
                    if paused_during_search:
                        query_status = "incomplete"
                    elif reached_target or query_count >= query_target:
                        query_status = "completed"
                    elif no_more_results:
                        query_status = "no_more"
                    elif not search_complete:
                        query_status = "incomplete"
                    else:
                        query_status = "incomplete"
                    now = db._now_iso()
                    self.conn.execute(
                        "UPDATE task_search_queries SET status = ?, discovered_count = ?, new_count = ?, "
                        "duplicate_count = ?, last_run_at = ?, updated_at = ? "
                        "WHERE task_id = ? AND query_order = ?",
                        (query_status, len(candidate_vids), new_count, duplicate_count, now, now,
                         task_id, int(query_row["query_order"])),
                    )
                    self.conn.commit()
                self._emit_log(
                    f"[scheduler] 任务 {task_id} 关键词“{query}”搜索完成：候选 {len(candidate_vids)}，"
                    f"新增 {new_count}，重复 {duplicate_count}，当前 {query_count}/{query_target}，状态={query_status}"
                )
                self._record_collection_event(
                    task_id, "search", "keyword_finished",
                    f"关键词“{query}”搜索完成",
                    {"query": query, "discovered_count": len(candidate_vids), "new_count": new_count,
                     "duplicate_count": duplicate_count, "query_status": query_status},
                )
                if query_status not in ("completed", "no_more"):
                    detail = f"关键词“{query}”搜索未确认达到目标或没有更多内容"
                    if paused_during_search:
                        detail = f"关键词“{query}”搜索期间发生暂停，保留当前关键词，恢复后继续"
                    with self._conn_lock, self._lock:
                        db.update_task_status(self.conn, task_id, "incomplete", detail)
                        self._task_status[task_id] = "incomplete"
                    self._emit_log(f"[scheduler] 任务 {task_id} 暂不进入阶段B：{detail}")
                    return False

            with self._conn_lock:
                final_rows = [self._r(row) for row in self.conn.execute(
                    "SELECT * FROM task_search_queries WHERE task_id = ? ORDER BY query_order", (task_id,)
                ).fetchall()]
                all_terminal = bool(final_rows) and all(r.get("status") in ("completed", "no_more") for r in final_rows)
                all_reached = bool(final_rows) and all(
                    self._task_query_count(task_id, r.get("query")) >= int(r.get("target_count") or 1)
                    for r in final_rows
                )
                if all_terminal:
                    self.conn.execute(
                        "UPDATE tasks SET search_exhausted = ?, search_stop_reason = ?, "
                        "search_stopped_at = ?, search_phase_complete = 1, "
                        "updated_at = ? WHERE id = ?",
                        (int(not all_reached),
                         "所有关键词均已达到目标" if all_reached else "部分关键词已确认没有更多内容",
                         db._now_iso(), db._now_iso(), task_id),
                    )
                    self.conn.commit()
            self._emit_log(
                f"[scheduler] 任务 {task_id} 关键词组阶段A完成：已逐个搜索 {len(query_rows)} 个关键词，"
                "现在统一进入详情采集和账号均分"
            )
            return True
        except TaskPaused:
            self._mark_task_paused(task_id, "用户暂停，已保留当前关键词和作品断点")
            return False
        except TaskCancelled:
            self._update_collection_run(task_id, status="cancelled", stop_reason="用户停止")
            with self._conn_lock, self._lock:
                db.update_task_status(self.conn, task_id, "aborted")
                self._task_status[task_id] = "aborted"
            return False
        except HumanInterventionRequired as h:
            self._update_collection_run(task_id, status="paused", stop_reason=f"需要人工验证：{h.reason}")
            with self._conn_lock, self._lock:
                if search_account:
                    self.mark_human_waiting(search_account["id"], h.reason, task_id=task_id)
                db.update_task_status(self.conn, task_id, "paused", f"需要人工验证：{h.reason}")
                self._task_status[task_id] = "paused"
            self._emit_log(f"[scheduler] 任务 {task_id} 搜索需要人工接管：{h.reason}")
            return False
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}".strip()
            self._update_collection_run(task_id, status="failed", stop_reason=detail)
            with self._conn_lock, self._lock:
                db.update_task_status(self.conn, task_id, "failed", detail)
                self._task_status[task_id] = "failed"
            self._emit_log(f"[scheduler] 任务 {task_id} 关键词组搜索失败：{detail}")
            return False
        finally:
            self._release_phase_account(task_id)

    def _account_todo(self, task_id: int, account: dict) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) c FROM videos WHERE task_id = ? AND assigned_account = ? "
            "AND status IN ('assigned','collecting')",
            (task_id, account["name"]),
        )
        r = cur.fetchone()
        return int(r["c"]) if r else 0

    def _requeue_orphaned_videos(self, task_id: int, task: dict) -> int:
        """把分配给已删除/已解绑账号的残留视频退回 pending。

        软件重启、账号重新绑定或账号被删除后，视频表可能还保留旧的
        assigned_account。若不回收，这些视频既不属于任何现有 worker，
        又会让任务误以为仍有资源，点击继续后只能立即回到等待状态。
        """
        bound_names = task.get("task_accounts") or "[]"
        if isinstance(bound_names, str):
            try:
                bound_names = _json.loads(bound_names)
            except Exception:
                bound_names = []
        accounts = [a for a in self._all_accounts()
                    if (a.get("platform") or "douyin") == task.get("platform")]
        if bound_names:
            allowed = {a.get("name") for a in accounts
                       if self._account_matches_bound_name(a, bound_names)}
        else:
            allowed = {a.get("name") for a in accounts}

        rows = self.conn.execute(
            "SELECT id, assigned_account FROM videos "
            "WHERE task_id = ? AND status IN ('assigned','collecting')",
            (task_id,),
        ).fetchall()
        orphan_ids = [int(r["id"]) for r in rows
                      if r["assigned_account"] not in allowed]
        if not orphan_ids:
            return 0
        placeholders = ",".join("?" for _ in orphan_ids)
        self.conn.execute(
            f"UPDATE videos SET status = 'pending', assigned_account = NULL "
            f"WHERE id IN ({placeholders}) AND status IN ('assigned','collecting')",
            orphan_ids,
        )
        self.conn.commit()
        self._emit_log(f"[scheduler] 任务 {task_id} 回收 {len(orphan_ids)} 条孤立视频，重新进入分配队列")
        return len(orphan_ids)

    def _thread_alive(self, account_id: int) -> bool:
        t = self._worker_threads.get(account_id)
        if t is not None and t.is_alive():
            return True
        # 阶段 A 搜索也会占用账号窗口。旧实现只登记阶段线程，未登记
        # 搜索账号，导致另一个任务可能同时领取同一个浏览器窗口。
        for task_id, owner_id in list(self._phase_account_by_task.items()):
            if int(owner_id) != int(account_id):
                continue
            phase = self._phase_threads.get(task_id)
            if phase is not None and phase.is_alive():
                return True
        return False

    def _release_phase_account(self, task_id: int) -> None:
        """释放阶段 A 搜索占用的账号；人工冻结时保留 waiting_human。"""
        with self._conn_lock, self._lock:
            task_id = int(task_id)
            account_id = self._phase_account_by_task.get(task_id)
            if account_id is None or account_id in self._waiting_accounts:
                if account_id is not None and account_id in self._waiting_accounts:
                    self._phase_account_by_task.pop(task_id, None)
                return
            # 释放前再次确认没有阶段/worker 仍持有该账号，避免并发任务误清理。
            # 当前线程正在执行 finally 时，_thread_alive() 仍会看到自己；
            # 这里需要把“当前阶段线程”排除，否则阶段 A 永远无法释放账号。
            current_phase = self._phase_threads.get(task_id)
            current_is_phase = current_phase is threading.current_thread()
            if self._thread_alive(account_id) and not current_is_phase:
                return
            self._phase_account_by_task.pop(task_id, None)
            self._acct_status(self.conn, account_id, "idle")

    def _pause_release_requested(self, task_id: int) -> bool:
        with self._lock:
            evt = self._pause_release_evts.get(int(task_id))
            return bool(evt is not None and evt.is_set())

    def _task_stop_requested(self, task_id: int) -> bool:
        with self._lock:
            evt = self._task_stop_evts.get(int(task_id))
            return bool(evt is not None and evt.is_set())

    def _raise_for_task_control(self, task_id: int) -> None:
        """在搜索/预取回调中把暂停和停止分成两条可恢复路径。"""
        if self._wait_if_paused(task_id):
            return
        if self._pause_release_requested(task_id):
            raise TaskPaused()
        raise TaskCancelled()

    def _mark_task_paused(self, task_id: int, detail: str = "用户暂停") -> None:
        """统一持久化暂停状态，并把正在处理的关键词退回可恢复状态。"""
        with self._conn_lock, self._lock:
            current = self.conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (int(task_id),)
            ).fetchone()
            if not current or current["status"] in ("done", "aborted", "failed"):
                return
            self.conn.execute(
                "UPDATE task_search_queries SET status = 'incomplete', updated_at = ? "
                "WHERE task_id = ? AND status = 'in_progress'",
                (db._now_iso(), int(task_id)),
            )
            db.update_task_status(self.conn, int(task_id), "paused", detail)
            self._task_status[int(task_id)] = "paused"
        self._update_collection_run(int(task_id), status="paused", stop_reason=detail)

    def _is_waiting(self, account_id: int) -> bool:
        with self._lock:
            return account_id in self._waiting_accounts

    def _cd_left_seconds(self, cd_until) -> int:
        dt = _iso_to_dt(cd_until)
        if dt is None:
            return 0
        return max(0, int((dt - datetime.datetime.now()).total_seconds()))

    def _control_kwargs(self, fn, task_id: int, **extra) -> dict:
        """仅向采集器传入其签名支持的任务控制/扩展参数。"""
        try:
            params = inspect.signature(fn).parameters
            accepts_any = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        except (TypeError, ValueError):
            params, accepts_any = {}, False
        out = {}
        if accepts_any or "pause_event" in params:
            out["pause_event"] = self._task_pause_evt(task_id)
        if accepts_any or "cancel_event" in params:
            out["cancel_event"] = self._task_stop_evts.setdefault(task_id, threading.Event())
        for name, value in extra.items():
            # 扩展参数不能仅因存在 **kwargs 就强行传入：测试/第三方适配器
            # 可能接收后再转发给旧签名，最终仍会因未知参数失败。
            if name in params:
                out[name] = value
        return out

    # ------------------------------------------------------------------ 公共 API
    def create_task(self, keyword: str, platform: str = "douyin",
                    batch_size: int = 10, cooldown_seconds: int = 60,
                    collect_mode: str = "standard", target_count: int = 100,
                    collect_types=None, task_accounts=None,
                    only_with_comments: bool = False, output_dir: str = None,
                    search_sort: str = "default", execution_mode: str = "once",
                    keyword_group_id: int = None,
                    monitor_interval_seconds: int = 3600) -> int:
        with self._conn_lock:
            task_id = db.create_task(self.conn, keyword, platform, batch_size, cooldown_seconds,
                                     collect_mode, target_count, collect_types, task_accounts,
                                     only_with_comments, output_dir, search_sort,
                                     execution_mode, keyword_group_id)
            if str(execution_mode or "once") == "monitoring":
                try:
                    from operations.monitoring import MonitoringRuleStore
                    MonitoringRuleStore(self.conn).upsert(
                        task_id, interval_seconds=monitor_interval_seconds
                    )
                except Exception as exc:
                    # 增量监控是新增能力，初始化失败不能破坏旧版任务创建。
                    self._emit_log(f"[monitor] 任务#{task_id} 监控规则初始化失败：{type(exc).__name__}: {exc}")
            return task_id

    def configure_monitoring(self, task_id: int, interval_seconds: int = 3600,
                             enabled: bool = True, search_sort: str | None = None) -> dict:
        """配置单个任务的增量监控，并可同步修改搜索排序。"""
        task_id = int(task_id)
        interval_seconds = int(interval_seconds)
        if not 60 <= interval_seconds <= 604800:
            raise ValueError("增量监控间隔必须是 60–604800 秒")
        with self._conn_lock:
            task = self._r(self.conn.execute(
                "SELECT id, platform FROM tasks WHERE id = ?", (task_id,)
            ).fetchone())
            if not task:
                raise KeyError(f"任务不存在: {task_id}")
            from operations.monitoring import MonitoringRuleStore
            rule_store = MonitoringRuleStore(self.conn)
            rule_store.upsert(task_id, interval_seconds=interval_seconds, enabled=bool(enabled))
            if not enabled:
                # 禁用后保留规则历史，但让监控轮询器跳过该任务。
                self.conn.execute(
                    "UPDATE tasks SET execution_mode = 'once', next_run_at = NULL WHERE id = ?",
                    (task_id,),
                )
            if search_sort:
                self.conn.execute(
                    "UPDATE tasks SET search_sort = ?, updated_at = ? WHERE id = ?",
                    (str(search_sort), db._now_iso(), task_id),
                )
            self.conn.commit()
            rule = rule_store.get_for_task(task_id) or {}
        runner = self._monitor_runner
        if runner is not None:
            runner.wake()
        return rule

    def start_monitoring(self, poll_seconds: float = 15.0) -> bool:
        """启动定时增量监控后台线程；重复调用不会创建重复线程。"""
        if self._closed:
            raise RuntimeError("调度器已关闭")
        if self._monitor_runner is None:
            from operations.monitor_runner import MonitoringRunner
            self._monitor_runner = MonitoringRunner(self, poll_seconds=poll_seconds)
        return self._monitor_runner.start()

    def run_monitoring_once(self) -> list[int]:
        """立即执行一轮到期监控，便于 GUI 手动刷新和测试。"""
        if self._monitor_runner is None:
            from operations.monitor_runner import MonitoringRunner
            self._monitor_runner = MonitoringRunner(self)
        return self._monitor_runner.run_once()

    def stop_monitoring(self) -> None:
        runner = self._monitor_runner
        if runner is not None:
            runner.stop()

    def delete_task(self, task_id: int) -> bool:
        task_id = int(task_id)
        # 右键删除允许作用于采集中任务；先停止属于该任务的阶段线程、监督线程
        # 和 worker，避免删除后旧线程继续写入已经不存在的 task_id。
        with self._lock:
            has_active_state = bool(
                self._phase_threads.get(task_id)
                or self._supervisors.get(task_id)
                or any(tid == task_id for tid in self._worker_task.values())
                or task_id in self._waiting_tasks.values()
            )
        if has_active_state:
            self.stop_task(task_id)

        with self._conn_lock, self._lock:
            deleted = db.delete_task(self.conn, task_id)
            if deleted:
                # 这些是运行时缓存，不清理会让后续“继续/等待账号恢复”
                # 误把已删除任务当成仍存在的任务。
                self._pause_evts.pop(task_id, None)
                self._task_stop_evts.pop(task_id, None)
                self._pause_release_evts.pop(task_id, None)
                self._task_threads.pop(task_id, None)
                self._phase_threads.pop(task_id, None)
                self._phase_account_by_task.pop(task_id, None)
                self._supervisors.pop(task_id, None)
                self._done_events.pop(task_id, None)
                self._resume_account_by_task.pop(task_id, None)
                self._task_status.pop(task_id, None)
                self._collection_run_ids.pop(task_id, None)
                for account_id, waiting_task_id in list(self._waiting_tasks.items()):
                    if int(waiting_task_id) == task_id:
                        self._waiting_tasks.pop(account_id, None)
                        self._waiting_accounts.discard(account_id)
            return deleted

    def get_task(self, task_id: int) -> dict:
        with self._conn_lock, self._lock:
            return self._r(db.get_task(self.conn, task_id))

    def add_account(self, name: str, bb_window_id=None, platform: str = "douyin",
                    nickname: str = None) -> int:
        with self._conn_lock:
            return db.upsert_account(self.conn, name, bb_window_id, platform, nickname)

    def remove_account(self, name: str = None, account_id: int = None,
                       platform: str = None) -> bool:
        """按 name 解除账号绑定。返回是否删除成功。"""
        with self._conn_lock, self._lock:
            blocker = self._account_removal_blocker_unlocked(account_id) if account_id is not None else None
            if blocker:
                raise RuntimeError(blocker)
            return db.remove_account(self.conn, name, account_id=account_id, platform=platform)

    def account_in_use(self, account_id: int) -> bool:
        with self._lock:
            return self._thread_alive(account_id)

    def account_removal_blocker(self, account_id: int) -> str | None:
        with self._conn_lock, self._lock:
            return self._account_removal_blocker_unlocked(account_id)

    def _account_removal_blocker_unlocked(self, account_id: int) -> str | None:
        if self._thread_alive(account_id):
            return "账号正在参与采集，请先停止对应任务"
        account = self._r(self.conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone())
        if not account:
            return None
        for task in self._all_tasks():
            if task.get("platform") != account.get("platform") or task.get("status") in ("done", "aborted"):
                continue
            try:
                names = _json.loads(task.get("task_accounts") or "[]")
            except Exception:
                names = []
            if account.get("name") in names:
                return f"账号仍绑定任务#{task['id']}，请先完成或删除该任务"
        return None

    def start(self, task_id: int, *, force_search: bool = False) -> int:
        """阶段 A（搜索，占位）-> 阶段 B（URL 均分 + 每账号并行采评论）。

        任何状态可重入（断点续跑）：以 DB 为真源，已完成/失败视频不重分配，
        collecting 残留由原属账号的 worker 续采；重复调用不会起重复 worker。

        ``force_search=True`` 仅用于用户点击“继续采集”：即使上一轮已明确
        没有更多结果，也允许重新搜索一次，发现新视频后补入；普通重启/续跑
        仍遵守 ``search_exhausted`` 终态，避免后台无休止重复搜索。

        线程安全要点（修复 GUI 卡死）：
          * 耗时搜索调 self.collector.search()（真实模式可达数十秒~180s，
            CDP 翻页+网络）绝不能持有 self._lock 或 self._conn_lock —— 否则轮询线程的
            status_report()（在 tkinter 主线程执行）会阻塞等锁，导致 GUI 卡死。
          * 阶段 A 的搜索在锁外执行；共享内存态（_task_status / _waiting_accounts /
            线程簿记）由 self._lock 保护。
          * 共享连接 self.conn 的读/写由 self._conn_lock 串行化（多任务并发 start 时
            同一连接并发写会 SystemError）。worker 用各自独立连接，采集真正并行。
          * 锁次序恒为：self._conn_lock 在外、self._lock 在内（避免死锁）。
        """
        if self._closed:
            raise RuntimeError("调度器已关闭")
        # ---- 阶段 A1：读任务、置阶段（短临界区，很快）----
        with self._conn_lock, self._lock:
            task = self._r(db.get_task(self.conn, task_id))
            if not task:
                raise ValueError(f"task {task_id} 不存在")
            existing_phase = self._phase_threads.get(task_id)
            if existing_phase is not None and existing_phase.is_alive():
                # 阶段 A 正在等待暂停事件；继续操作只需唤醒原线程，
                # 不能再次启动一轮微博/B站搜索。
                return task_id
            self._ensure_collection_run(task)
            target_count = max(1, int(task.get("target_count") or 100))
            is_monitoring = str(task.get("execution_mode") or "once") == "monitoring"
            search_exhausted = bool(int(task.get("search_exhausted") or 0))
            search_phase_complete = bool(int(task.get("search_phase_complete") or 0))
            search_phase_reset = False
            query_rows = self._ensure_task_search_queries(task, target_count)
            is_keyword_group = bool(task.get("keyword_group_id") and query_rows)
            if is_keyword_group:
                # 任务表中的 target_count 是“每个关键词”的目标；阶段 B 使用总目标。
                target_count = sum(
                    max(1, int(row.get("target_count") or task.get("target_count") or 100))
                    for row in query_rows
                )
                if is_monitoring and self._task_video_left(task_id) == 0:
                    # 每轮监控都重新检查所有预制关键词的最新结果。
                    self._reset_task_search_queries(task_id)
                    search_exhausted = False
                    search_phase_complete = False
                    search_phase_reset = True
                elif force_search and search_exhausted:
                    self._reset_task_search_queries(task_id, only_open=True)
                    search_exhausted = False
                    search_phase_complete = False
                    search_phase_reset = True
                # 上面的重置会更新数据库中的关键词状态；恢复时必须重新读取，
                # 不能继续使用重置前的 completed/no_more 快照。
                query_rows = self._ensure_task_search_queries(task, target_count)
                if not search_phase_reset:
                    search_phase_complete = self._recover_keyword_search_phase(
                        task, task_id, query_rows
                    ) or search_phase_complete
            if force_search and search_exhausted:
                # 只清除“搜索已到尽头”的闸门，保留已有视频与去重数据。
                # 新一轮若再次收到无更多提示，阶段 A 会重新写回终态。
                if not is_keyword_group:
                    # 普通任务同样有一条 task_search_queries 记录；否则只清除
                    # tasks.search_exhausted 会让恢复入口继续看到 no_more，跳过搜索。
                    self._reset_task_search_queries(task_id, only_open=True)
                    query_rows = self._ensure_task_search_queries(task, target_count)
                self.conn.execute(
                    "UPDATE tasks SET search_exhausted = 0, search_stop_reason = NULL, "
                    "search_stopped_at = NULL, search_phase_complete = 0, "
                    "error_message = NULL, updated_at = ? "
                    "WHERE id = ?",
                    (db._now_iso(), task_id),
                )
                self.conn.commit()
                task["search_exhausted"] = 0
                search_exhausted = False
                # 上面同时清除了 tasks.search_phase_complete；同步清除
                # 当前 start() 调用里的快照，否则后面的阶段判断仍会把
                # 本次“继续采集”误判成已完成搜索，直接跳过阶段 A。
                search_phase_complete = False
                self._emit_log(f"[scheduler] 任务 {task_id} 用户点击继续采集，重新开放搜索补采")
            # 先回收账号删除/解绑/重启后留下的孤立分配，避免任务表里有资源，
            # 但现有账号没有任何可领取的视频，导致“继续”后立即暂停。
            self._requeue_orphaned_videos(task_id, task)
            # 目标是“内容数量”而非数据库中已有视频行数。普通任务若还有
            # assigned/collecting 内容应直接续跑阶段 B；微博/B站只有在本轮
            # 已无待处理内容且尚未达到目标时，才重新进入阶段 A。
            current_count = self._task_video_count(task_id)
            current_done = self._task_done_count(task_id)
            current_left = self._task_video_left(task_id)
            # 搜索阶段是否完成必须看搜索记录，而不能只看 videos 是否还有待处理项。
            # 搜索中途遇到验证码时，已有视频仍是 pending；若只看 current_left，
            # 恢复会错误跳过剩余关键词，直接进入评论采集。
            search_rows_terminal = bool(query_rows) and all(
                str(row.get("status") or "") in ("completed", "no_more")
                for row in query_rows
            )
            search_phase_complete = bool(
                search_phase_complete or search_exhausted or search_rows_terminal
            )
            if not query_rows:
                # 兼容极旧数据库：没有关键词快照时，用数量/无更多终态兜底。
                search_phase_complete = bool(search_exhausted or current_count >= target_count)
            start_phase_a = not search_phase_complete
            stop_evt = self._task_stop_evts.setdefault(task_id, threading.Event())
            stop_evt.clear()
            if start_phase_a:
                db.update_task_status(self.conn, task_id, "phase_a_search")
                self._task_status[task_id] = "phase_a_search"
                self._phase_threads[task_id] = threading.current_thread()

        # ---- 阶段 A：搜索（锁外执行，避免阻塞其余 status_report / 其它操作）----
        if start_phase_a and is_keyword_group:
            if not self._run_keyword_group_search(task, task_id, query_rows,
                                                  is_monitoring=is_monitoring):
                with self._lock:
                    if self._phase_threads.get(task_id) is threading.current_thread():
                        self._phase_threads.pop(task_id, None)
                return task_id
            with self._lock:
                if self._phase_threads.get(task_id) is threading.current_thread():
                    self._phase_threads.pop(task_id, None)
            start_phase_a = False

        if start_phase_a and not is_keyword_group:
            search_account = None
            try:
                with self._conn_lock, self._lock:
                    bound_names = task.get("task_accounts") or "[]"
                    if isinstance(bound_names, str):
                        try:
                            bound_names = _json.loads(bound_names)
                        except Exception:
                            bound_names = []
                    candidates = [a for a in self._all_accounts()
                                  if (a.get("platform") or "douyin") == task["platform"]
                                  and (not bound_names or self._account_matches_bound_name(a, bound_names))
                                  and (self.bb is None or not self._is_demo_account(a))
                                  and a.get("status") not in ("waiting_human", "dead", "cooldown")
                                  and not self._thread_alive(a["id"])
                                  and not self._is_waiting(a["id"])]
                    preferred_id = self._resume_account_by_task.get(int(task_id))
                    if preferred_id is not None:
                        candidates.sort(key=lambda a: 0 if int(a.get("id", -1)) == int(preferred_id) else 1)
                    search_account = candidates[0] if candidates else None
                    if search_account:
                        # 阶段 A 同样独占一个账号窗口。没有这条登记时，
                        # 其它任务会把同一个账号误判为空闲并同时使用。
                        self._phase_account_by_task[int(task_id)] = int(search_account["id"])
                        self._acct_status(self.conn, int(search_account["id"]), "working")
                        self._update_collection_run(task_id, account_id=int(search_account["id"]))
                    if search_account is None:
                        any_account = any(
                            (a.get("platform") or "douyin") == task["platform"]
                            and (not bound_names or self._account_matches_bound_name(a, bound_names))
                            and (self.bb is None or not self._is_demo_account(a))
                            for a in self._all_accounts()
                        )
                        db.update_task_status(self.conn, task_id,
                                              "waiting_account" if any_account else "no_account")
                        self._task_status[task_id] = "waiting_account" if any_account else "no_account"
                        self._phase_threads.pop(task_id, None)
                        self._emit_log(f"[scheduler] 任务 {task_id} 暂无可派出的{task['platform']}账号，等待账号释放后重新搜索")
                        return task_id
                # 阶段 A 只补采目标差额，不能每次续跑都再次请求完整目标数。
                remaining_target = max(1, target_count - self._task_done_count(task_id))
                selected_sort = task.get("search_sort", "default") or "default"
                self._emit_log(
                    f"[scheduler] 任务 {task_id} 使用搜索排序："
                    f"{sort_label(task.get('platform', 'douyin'), selected_sort)}"
                )
                only_with_comments = bool(task.get("only_with_comments"))
                comment_collector = getattr(self.collector, "collect_with_comments", None)
                search_meta = None
                if only_with_comments and callable(comment_collector):
                    # 微博/B站会在一次搜索中逐个完成“作品+评论”。通过回调即时落库，
                    # GUI 才能在浏览器采集过程中看到实时数量；这些作品直接标记 done，
                    # 阶段 B 不再重复打开页面抓取评论。
                    import json as _progress_json
                    progress_types = task.get("collect_types") or []
                    if isinstance(progress_types, str):
                        try:
                            progress_types = _progress_json.loads(progress_types)
                        except Exception:
                            progress_types = DEFAULT_COLLECT_TYPES
                    if not progress_types:
                        progress_types = DEFAULT_COLLECT_TYPES

                    lead_service = None
                    try:
                        lead_service = self._build_lead_service(self.conn)
                    except Exception as exc:  # noqa: BLE001
                        self._emit_log(f"[leads] 线索服务初始化失败，继续保留评论采集：{exc}")

                    def _on_prefetched_item(item):
                        # 微博/B站阶段 A 也支持按任务暂停；继续时由原采集线程
                        # 从这里恢复，避免重新打开搜索页面。
                        self._raise_for_task_control(task_id)
                        with self._conn_lock:
                            if self._task_done_count(task_id) >= target_count:
                                return
                            extra = item.get("extra")
                            if isinstance(extra, dict) and "engagement" not in progress_types:
                                extra = {k: v for k, v in extra.items()
                                         if k not in ("digg", "digg_count", "like_count", "collect_count", "share_count")}
                            video_id = db.insert_video(
                                self.conn, task_id, item["vid"], item.get("url", ""),
                                item.get("title") if "video_info" in progress_types else None,
                                item.get("author") if "author_info" in progress_types else None,
                                extra, platform=task["platform"],
                                search_query=task.get("keyword"))
                            for comment in item.get("comments", []) or []:
                                c_extra = comment.get("extra") or {}
                                if not isinstance(c_extra, dict):
                                    c_extra = {}
                                if comment.get("region") not in (None, "") and "region" in progress_types:
                                    c_extra["region"] = comment.get("region")
                                if comment.get("cid") not in (None, ""):
                                    c_extra["platform_comment_id"] = str(comment.get("cid"))
                                if "engagement" in progress_types:
                                    for key in ("digg", "digg_count"):
                                        if comment.get(key) not in (None, ""):
                                            c_extra[key] = comment.get(key)
                                comment_id = db.insert_comment(
                                    self.conn, video_id,
                                    comment.get("user_id") if "comment_user" in progress_types else None,
                                    comment.get("nickname") if "comment_user" in progress_types else None,
                                    comment.get("content") if "comments" in progress_types else None,
                                    comment.get("comment_time"), extra=c_extra,
                                    intent_score=comment.get("intent_score", 0) if "intent" in progress_types else 0,
                                    intent_label=comment.get("intent_label") if "intent" in progress_types else None,
                                    reply_suggestion=comment.get("reply_suggestion") if "intent" in progress_types else None,
                                    platform=task["platform"])
                                self._ingest_comment_to_lead(
                                    self.conn, comment_id, lead_service,
                                    task_id=task_id, video_id=video_id)
                            self.conn.execute(
                                "UPDATE videos SET status = 'done', collected_at = ? WHERE id = ?",
                                (db._now_iso(), video_id))
                            self.conn.commit()

                    bundle = comment_collector(
                        task["keyword"],
                        target_count=remaining_target,
                        mode=task.get("collect_mode", "standard"),
                        platform=task.get("platform", "weibo"),
                        window_id=(search_account or {}).get("bb_window_id"),
                        progress_callback=_on_prefetched_item,
                        **self._control_kwargs(
                            comment_collector, task_id,
                            search_sort=task.get("search_sort", "default"),
                        ),
                    ) or {}
                    results = bundle.get("items", []) if isinstance(bundle, dict) else []
                    search_meta = bundle if isinstance(bundle, dict) else None
                else:
                    raw_results = self.collector.search(
                        task["keyword"], task["platform"],
                        task.get("collect_mode", "standard"),
                        remaining_target,
                        window_id=(search_account or {}).get("bb_window_id"),
                        **self._control_kwargs(
                            self.collector.search, task_id,
                            search_sort=task.get("search_sort", "default"),
                        ),
                    )
                    # 保留 SearchVideosResult 的终止元数据；不能用
                    # ``raw_results or []``，否则空结果会丢掉“确认无更多”的状态。
                    results = [] if raw_results is None else raw_results
                if self._task_stop_requested(task_id):
                    if self._pause_release_requested(task_id):
                        raise TaskPaused()
                    raise TaskCancelled()
                search_complete = bool(getattr(results, "search_complete", True))
                if search_meta is not None:
                    search_complete = bool(search_meta.get("search_complete", search_complete))
                reached_target = bool(
                    (search_meta or {}).get("reached_target", getattr(results, "reached_target", False))
                )
                no_more_results = bool(
                    (search_meta or {}).get("no_more_results", getattr(results, "no_more_results", False))
                )
                termination_detail = (
                    (search_meta or {}).get("termination_reason", "")
                    or getattr(results, "termination_reason", "")
                )
                search_reason = (
                    "达到目标数量" if reached_target
                    else termination_detail if no_more_results and termination_detail
                    else "确认没有更多视频" if no_more_results
                    else "搜索阶段未确认终止条件"
                )
                # 搜索结果是候选总数，不能直接当成新增数。与当前任务已入库
                # 的作品 ID 对比后记录新增/历史重复，便于判断补采是否真的拿到新内容。
                candidate_vids = []
                candidate_seen = set()
                for item in results:
                    vid = str(item.get("vid") or "") if isinstance(item, dict) else ""
                    if vid and vid not in candidate_seen:
                        candidate_seen.add(vid)
                        candidate_vids.append(vid)
                with self._conn_lock:
                    existing_rows = self.conn.execute(
                        "SELECT vid FROM videos WHERE task_id = ? AND platform = ?",
                        (task_id, task["platform"]),
                    ).fetchall()
                existing_vids = {str(row[0]) for row in existing_rows}
                new_candidate_count = sum(1 for vid in candidate_vids if vid not in existing_vids)
                duplicate_candidate_count = len(candidate_vids) - new_candidate_count
                self._emit_log(
                    f"[scheduler] 任务 {task_id} 搜索候选去重：候选 {len(candidate_vids)} 条，"
                    f"新增 {new_candidate_count} 条，历史重复 {duplicate_candidate_count} 条"
                )
                self._record_collection_event(
                    task_id, "search", "search_finished", search_reason,
                    {"discovered_count": len(candidate_vids), "new_count": new_candidate_count,
                     "duplicate_count": duplicate_candidate_count},
                )
                self._emit_log(
                    f"[scheduler] 任务 {task_id} 阶段A搜索返回 {len(results)} 条，"
                    f"目标剩余 {remaining_target} 条，轮次={getattr(results, 'rounds', '未知')}，"
                    f"终止原因：{search_reason}"
                )
                if no_more_results:
                    stop_reason = termination_detail or "确认没有更多视频"
                    self._mark_search_exhausted(task_id, stop_reason)
                    self._emit_log(
                        f"[scheduler] 任务 {task_id} 已记录‘没有更多视频’终态（{stop_reason}），"
                        "后续继续/重启不再重新搜索"
                    )
                # 阶段 A 没有达到目标，也没有确认搜索到底时，绝不能把
                # 当前半页结果送入详情页。下次继续时会重新进入阶段 A 补量。
                if not search_complete:
                    detail = "搜索阶段达到安全轮次，但未确认达到目标或没有更多视频"
                    with self._conn_lock, self._lock:
                        db.update_task_status(self.conn, task_id, "incomplete", detail)
                        self._task_status[task_id] = "incomplete"
                    self._emit_log(f"[scheduler] 任务 {task_id} 暂不进入阶段B：{detail}")
                    return task_id
                with self._lock:
                    if self._phase_threads.get(task_id) is threading.current_thread():
                        self._phase_threads.pop(task_id, None)
            except TaskPaused:
                self._mark_task_paused(task_id, "用户暂停，已保留搜索结果和作品断点")
                return task_id
            except TaskCancelled:
                self._update_collection_run(task_id, status="cancelled", stop_reason="用户停止")
                with self._conn_lock, self._lock:
                    db.update_task_status(self.conn, task_id, "aborted")
                    self._task_status[task_id] = "aborted"
                return task_id
            except HumanInterventionRequired as h:
                self._update_collection_run(task_id, status="paused", stop_reason=f"需要人工验证：{h.reason}")
                # 阶段 A 也必须进入 P7，不得让异常卡在 phase_a_search。
                with self._conn_lock, self._lock:
                    # 阶段 A 由实际分配的搜索账号触发验证码时，只冻结该账号，
                    # 不能按平台取第一个账号导致人工处理对象错位。
                    account = search_account
                    if account:
                        self.mark_human_waiting(account["id"], h.reason, task_id=task_id)
                    # 人工验证属于“暂停后继续”，不能落成普通待开始；否则
                    # GUI 会显示“开始”，start() 又会跳过 waiting_human 的原账号。
                    db.update_task_status(self.conn, task_id, "paused",
                                          f"需要人工验证：{h.reason}")
                    self._task_status[task_id] = "paused"
                self._emit_log(f"[scheduler] 任务 {task_id} 搜索需要人工接管：{h.reason}")
                return task_id
            except Exception as e:
                detail = f"{type(e).__name__}: {e}".strip()
                self._update_collection_run(task_id, status="failed", stop_reason=detail)
                with self._conn_lock, self._lock:
                    db.update_task_status(self.conn, task_id, "failed", detail)
                    self._task_status[task_id] = "failed"
                self._emit_log(f"[scheduler] 任务 {task_id} 搜索失败：{detail}")
                return task_id
            finally:
                with self._lock:
                    if self._phase_threads.get(task_id) is threading.current_thread():
                        self._phase_threads.pop(task_id, None)
                self._release_phase_account(task_id)
            # 用户可能在耗时搜索期间点击停止。不得继续落库或覆盖 aborted 终态。
            if self._task_stop_evts.get(task_id, threading.Event()).is_set():
                with self._conn_lock, self._lock:
                    db.update_task_status(self.conn, task_id, "aborted")
                    self._task_status[task_id] = "aborted"
                return task_id
            import json
            selected_types = task.get("collect_types") or []
            if isinstance(selected_types, str):
                try:
                    selected_types = json.loads(selected_types)
                except Exception:
                    selected_types = DEFAULT_COLLECT_TYPES
            if not selected_types:
                selected_types = DEFAULT_COLLECT_TYPES
            # 落库：全程占用共享连接锁（自旋 insert 很快，毫秒级；两任务并发 start 串行化）
            with self._conn_lock:
                for r in results:
                    extra = r.get("extra")
                    if isinstance(extra, dict) and "engagement" not in selected_types:
                        extra = {k: v for k, v in extra.items()
                                 if k not in ("digg", "digg_count", "like_count", "collect_count", "share_count")}
                    db.insert_video(self.conn, task_id, r["vid"], r["url"],
                                    r.get("title") if "video_info" in selected_types else None,
                                    r.get("author") if "author_info" in selected_types else None,
                                    extra,
                                    platform=task["platform"],
                                    search_query=task.get("keyword"))
                # 普通任务也持久化阶段 A 的终止状态。恢复时据此判断是进入
                # 评论采集，还是继续从搜索阶段补采；不能只看 videos 表中是否有待处理项。
                if query_rows:
                    query_count = self._task_query_count(task_id, task.get("keyword", ""))
                    if only_with_comments and query_count == 0:
                        query_count = self._task_video_count(task_id)
                    query_status = (
                        "completed" if reached_target or query_count >= target_count
                        else "no_more" if no_more_results
                        else "incomplete"
                    )
                    self.conn.execute(
                        "UPDATE task_search_queries SET status = ?, discovered_count = ?, "
                        "new_count = ?, duplicate_count = ?, last_run_at = ?, updated_at = ? "
                        "WHERE task_id = ? AND query_order = ?",
                        (query_status, len(candidate_vids),
                         len(candidate_vids), 0, db._now_iso(), db._now_iso(),
                         task_id, int(query_rows[0].get("query_order") or 1)),
                    )
                    self.conn.commit()
                if not results:
                    # 已有部分内容但本轮没有新增时，标记 incomplete，允许
                    # 用户继续；只有从零开始且完全无结果才算失败。
                    current_done = self._task_done_count(task_id)
                    final_status = "incomplete" if no_more_results or current_done > 0 else "failed"
                    error = (
                        "已明确没有更多视频"
                        if no_more_results else
                        "搜索未返回任何内容" if final_status == "failed" else None
                    )
                    db.update_task_status(self.conn, task_id, final_status, error)
                    with self._lock:
                        self._task_status[task_id] = final_status
                    self._update_collection_run(
                        task_id,
                        status="no_more" if no_more_results else "completed",
                        stop_reason=error or "本轮没有新增内容",
                        new_count=0,
                    )
                    self._emit_log(f"[scheduler] 任务 {task_id} 本轮无新增内容，状态={final_status}")
                    return task_id

        # ---- 阶段 B：分配 + 起 worker（对共享连接的操作全程持 conn 锁）----
        with self._conn_lock, self._lock:
            accounts = self._all_accounts()
            # P2 平台绑定：只分配与任务平台匹配的账号（抖音账号只采抖音，小红书只采小红书）
            accounts = [a for a in accounts if (a.get("platform") or "douyin") == task["platform"]]
            # 任务绑定的专属账号：任务创建时选的账号名列表（task_accounts）。
            # 只使用这些账号，避免"一个账号只服务一个任务"的 worker 被多个任务抢占，
            # 从而实现多任务各自用自己账号真正并行。
            bound_names = task.get("task_accounts") or "[]"
            if isinstance(bound_names, str):
                try:
                    bound_names = _json.loads(bound_names)
                except Exception:
                    bound_names = []
            if bound_names:
                accounts = [a for a in accounts
                            if self._account_matches_bound_name(a, bound_names)]
            if not accounts:
                self._emit_log(f"[scheduler] 警告：任务 {task_id} 无可用账号，无法进入阶段 B")
                db.update_task_status(self.conn, task_id, "no_account")
                self._task_status[task_id] = "no_account"
                self._update_collection_run(task_id, status="failed", stop_reason="无可用账号")
                return task_id
            # 重新开始/续跑：清除按任务的停止信号，允许该任务的 worker 运行
            stop_evt = self._task_stop_evts.setdefault(task_id, threading.Event())
            stop_evt.clear()

            # 一个浏览器账号同一时刻只服务一个任务。忙账号不得先分配，
            # 否则当前任务没有 worker 却会永久显示 running。
            available_accounts = [a for a in accounts if not self._thread_alive(a["id"])]
            if not available_accounts:
                # 任务已有 worker 正在采集时，重复唤醒/启动检查不能覆盖为 waiting_account。
                # 否则界面会显示“等待账号”，但后台实际仍在正常采集。
                task_has_workers = any(
                    int(tid) == int(task_id) and self._thread_alive(int(aid))
                    for aid, tid in self._worker_task.items()
                )
                if task_has_workers:
                    db.update_task_status(self.conn, task_id, "phase_b_comments")
                    self._task_status[task_id] = "phase_b_comments"
                    return task_id
                db.update_task_status(self.conn, task_id, "waiting_account")
                self._task_status[task_id] = "waiting_account"
                self._update_collection_run(task_id, status="cancelled", stop_reason="账号正在执行其它任务")
                return task_id

            # ---- 阶段 B：URL 均分（余数从第一个账号顺延：25/3 -> 9/8/8）----
            # 仅当仍有未完成视频时才推进到 phase_b_comments；
            # 对已完成（done）任务重入时保持现状，不覆盖终态。
            videos_left = self._task_video_left(task_id)
            done_count = self._task_done_count(task_id)
            if done_count >= target_count and not is_monitoring:
                db.update_task_status(self.conn, task_id, "done")
                self._task_status[task_id] = "done"
                self._update_collection_run(task_id, status="completed", stop_reason="达到目标数量",
                                            new_count=done_count)
                return task_id
            if videos_left > 0:
                db.update_task_status(self.conn, task_id, "phase_b_comments")
            elif task.get("status") not in ("failed", "aborted"):
                # 当前没有待处理内容，但仍未达到目标：保留可继续状态，
                # 绝不能错误标记为完成。
                db.update_task_status(self.conn, task_id, "incomplete")
                self._task_status[task_id] = "incomplete"
                self._update_collection_run(
                    task_id,
                    status="no_more" if search_exhausted else "completed",
                    stop_reason="已明确没有更多内容" if search_exhausted else "本轮无待处理内容",
                    new_count=0,
                )
                return task_id
            pending = db.get_videos_by_status(self.conn, task_id, "pending")
            self._assign_fair(task_id, available_accounts, pending)

            # ---- 起 worker（每账号一线程）；collecting 残留由原属账号续采 ----
            spawned = []
            for acc in available_accounts:
                if self._account_todo(task_id, acc) > 0 and not self._thread_alive(acc["id"]):
                    th = threading.Thread(
                        target=self._worker,
                        args=(task_id, acc["id"], acc["name"]),
                        name=f"worker-{acc['name']}",
                        daemon=True,
                    )
                    # 先登记归属，再启动线程。旧顺序在短任务中可能出现：
                    # worker 已经结束，但登记代码随后又把一个已结束线程写回表，
                    # 造成账号一直被误判为 working。
                    self._worker_threads[acc["id"]] = th
                    self._worker_task[acc["id"]] = task_id
                    try:
                        th.start()
                    except BaseException:
                        if self._worker_threads.get(acc["id"]) is th:
                            self._worker_threads.pop(acc["id"], None)
                        if self._worker_task.get(acc["id"]) == task_id:
                            self._worker_task.pop(acc["id"], None)
                        self._acct_status(self.conn, acc["id"], "idle")
                        raise
                    spawned.append(th)
            if spawned:
                self._task_threads.setdefault(task_id, []).extend(spawned)
                self._done_events.setdefault(task_id, threading.Event()).clear()
                self._ensure_supervisor(task_id)
                self._task_status[task_id] = "running"
            else:
                task_has_workers = any(
                    int(tid) == int(task_id) and self._thread_alive(int(aid))
                    for aid, tid in self._worker_task.items()
                )
                if task_has_workers:
                    db.update_task_status(self.conn, task_id, "phase_b_comments")
                    self._task_status[task_id] = "phase_b_comments"
                    return task_id
                db.update_task_status(self.conn, task_id, "waiting_account")
                self._task_status[task_id] = "waiting_account"
            # 注意：此处不得强制 set pause_evt —— 若操作者先 pause 再 start，应保持暂停态
            return task_id

    def _assign_fair(self, task_id: int, accounts: list, pending_videos: list):
        """URL 均分：per = n // m，余数从第一个账号顺延，如 25/3 -> [9, 8, 8]。"""
        if not pending_videos or not accounts:
            return
        rows = sorted(pending_videos, key=lambda r: int(self._r(r).get("id", 0)))
        n, m = len(rows), len(accounts)
        per, rem = divmod(n, m)
        sizes = [per + (1 if i < rem else 0) for i in range(m)]
        idx = 0
        for acc, size in zip(accounts, sizes):
            if size <= 0:
                continue
            chunk = [self._r(r)["vid"] for r in rows[idx:idx + size]]  # db.mark_videos_assigned 按 vid 字符串匹配
            idx += size
            db.mark_videos_assigned(self.conn, task_id, acc["name"], chunk)

    def _ensure_supervisor(self, task_id: int):
        """每个 start 周期一个 supervisor：等所有 worker 结束，落任务终态并置 done 事件。"""
        sup = self._supervisors.get(task_id)
        if sup is not None and sup.is_alive():
            return
        th = threading.Thread(target=self._supervisor, args=(task_id,),
                              name=f"sup-{task_id}", daemon=True)
        th.start()
        self._supervisors[task_id] = th

    def _supervisor(self, task_id: int):
        evt = self._done_events.setdefault(task_id, threading.Event())
        try:
            while True:
                with self._lock:
                    alive = [t for t in self._task_threads.get(task_id, []) if t.is_alive()]
                if not alive:
                    pause_release = self._pause_release_requested(task_id)
                    with self._ctl_lock:
                        left = self._task_video_left_ctl(task_id)  # 后台线程：走 self.ctl
                        task_row = self._r(self.ctl.execute(
                            "SELECT status, target_count FROM tasks WHERE id = ?", (task_id,)
                        ).fetchone())
                        task_state = str(task_row.get("status") or "")
                        target = task_row.get("target_count", 100)
                        done_count = self._task_done_count_ctl(task_id)
                        search_exhausted = self._task_search_exhausted_ctl(task_id)
                    if task_state == "aborted":
                        with self._lock:
                            self._task_status[task_id] = "aborted"
                        evt.set()
                        return
                    if task_state == "failed":
                        with self._lock:
                            self._task_status[task_id] = "failed"
                        evt.set()
                        return
                    if pause_release:
                        # 暂停会让旧 worker 退出并释放账号；supervisor 不能
                        # 因为 left==0 又把任务覆盖成 incomplete/done。
                        with self._ctl_lock:
                            task_row = self.ctl.execute(
                                "SELECT status, error_message FROM tasks WHERE id = ?",
                                (task_id,),
                            ).fetchone()
                            if task_row and task_row["status"] not in ("done", "aborted"):
                                detail = str(task_row["error_message"] or "用户暂停")
                                db.update_task_status(self.ctl, task_id, "paused", detail)
                        with self._lock:
                            self._task_status[task_id] = "paused"
                        evt.set()
                        return
                    if left == 0 and done_count >= int(target or 100):
                        with self._lock:
                            with self._ctl_lock:
                                db.update_task_status(self.ctl, task_id, "done")
                            self._task_status[task_id] = "done"
                        self._update_collection_run(task_id, status="completed",
                                                    stop_reason="达到目标数量", new_count=done_count)
                        evt.set()
                        return
                    if left == 0:
                        pause_evt = self._pause_evts.get(task_id)
                        stop_evt = self._task_stop_evts.get(task_id)
                        can_refill = (
                            not search_exhausted and
                            not self._stop.is_set() and
                            not (stop_evt is not None and stop_evt.is_set()) and
                            (pause_evt is None or pause_evt.is_set())
                        )
                        if can_refill:
                            with self._lock:
                                self._task_status[task_id] = "incomplete"
                                # 当前 supervisor 即将退出；先清除登记，
                                # 让下一轮 start() 为新 worker 创建新的 supervisor。
                                if self._supervisors.get(task_id) is threading.current_thread():
                                    self._supervisors.pop(task_id, None)
                            self._emit_log(
                                f"[scheduler] 任务 {task_id} 当前采集池已处理完，"
                                f"已完成 {done_count}/{int(target or 100)}，未收到无更多提示，自动重新进入搜索补采"
                            )
                            threading.Thread(
                                target=self.start,
                                args=(task_id,),
                                name=f"auto-refill-{task_id}",
                                daemon=True,
                            ).start()
                            evt.set()
                            return
                        with self._lock:
                            with self._ctl_lock:
                                db.update_task_status(self.ctl, task_id, "incomplete")
                            self._task_status[task_id] = "incomplete"
                        if search_exhausted:
                            self._update_collection_run(task_id, status="no_more",
                                                        stop_reason="已明确没有更多内容", new_count=done_count)
                        evt.set()
                        return
                    # 有残留（如 worker 被 shutdown 中断）：留给下一次 start() 续跑
                    with self._lock:
                        self._task_status[task_id] = "incomplete"
                    evt.set()
                    return
                time.sleep(0.2)
        except Exception as e:  # pragma: no cover
            detail = f"{type(e).__name__}: {e}".strip()
            # supervisor 自身异常以前只写入内存状态，GUI 下一次从数据库读取时
            # 就只剩下一个无原因的失败/暂停状态。把异常持久化到任务和运行记录，
            # 重启后仍能看到可排查的原因。
            try:
                with self._ctl_lock:
                    db.update_task_status(self.ctl, task_id, "failed", detail)
                self._update_collection_run(task_id, status="failed", stop_reason=detail)
            except Exception as persist_exc:
                self._emit_log(
                    f"[scheduler] 任务 {task_id} 异常原因写入失败："
                    f"{type(persist_exc).__name__}: {persist_exc}"
                )
            with self._lock:
                self._task_status[task_id] = "failed"
            self._emit_log(f"[scheduler] 任务 {task_id} supervisor 异常：{detail}")
            evt.set()

    def _worker(self, task_id: int, account_id: int, account_name: str):
        """每账号一个 worker：循环取自己的 assigned/collecting 视频采评论。

        冷却用 Event 等待（可被 shutdown/pause 唤醒）；waiting_human 期间
        park 在等待队列，绝不执行任何采集 / 浏览器动作（P7）。
        """
        conn = db.init_db(self.db_path)  # 每 worker 独立连接（WAL 并发安全）
        lead_service = None
        try:
            lead_service = self._build_lead_service(conn)
        except Exception as exc:  # noqa: BLE001
            self._emit_log(f"[leads] 线索服务初始化失败，继续保留评论采集：{exc}")
        window_id = None
        try:
            task = self._r(db.get_task(conn, task_id))
            batch_size = int(task.get("batch_size") or 10)
            cd_seconds = int(task.get("cooldown_seconds") or 0)

            # 断点续跑：以上次中断时 DB 的 batch_count 为批次进度真源
            cur = conn.execute("SELECT batch_count FROM accounts WHERE id = ?", (account_id,))
            row = cur.fetchone()
            local_batch = int(row["batch_count"]) if row else 0
            cd_row = conn.execute(
                "SELECT cd_until FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            cooldown_left = self._cd_left_seconds(cd_row["cd_until"] if cd_row else None)
            if cooldown_left > 0:
                self._acct_status(conn, account_id, "cooldown")
                self._emit_log(f"[scheduler] 账号 {account_name} 仍在冷却，剩余 {cooldown_left}s")
                evt = self._cd_events.setdefault(account_id, threading.Event())
                evt.clear()
                evt.wait(timeout=cooldown_left)
                db.reset_batch(conn, account_id)
                if self._stop.is_set() or self._task_stop_requested(task_id):
                    # 暂停/停止期间不再把冷却后的账号写回 working；finally
                    # 会把账号收敛到 idle（人工冻结除外）。
                    conn.commit()
                    return
            acct_row = conn.execute(
                "SELECT bb_window_id FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            window_id = acct_row["bb_window_id"] if acct_row else None

            self._acct_status(conn, account_id, "working")
            while not self._stop.is_set():
                # 按任务停止：本任务的 stop 信号已置位 → 退出该任务 worker（其它任务不受影响）
                stop_evt = self._task_stop_evts.get(task_id)
                if stop_evt is not None and stop_evt.is_set():
                    break
                # P7：人工接管冻结 —— 只有等待，不做任何采集动作
                if self._is_waiting(account_id):
                    self._park_human(account_id)
                    continue
                # 按任务的暂停（断点恢复：DB 状态未变，恢复后原地继续）
                if not self._wait_if_paused(task_id):
                    break
                if self._stop.is_set():
                    break

                safety = self._account_safety_decision(conn, account_id, account_name)
                if not safety.allowed:
                    if safety.reason == "连续失败达到暂停阈值" and self._consume_safety_recovery(task_id):
                        self._emit_log(
                            f"[safety] 任务 {task_id} 用户点击继续，允许一次浏览器连接恢复尝试"
                        )
                    else:
                        self._emit_log(f"[safety] 账号 {account_name} 暂停当前任务：{safety.reason}")
                        break

                vid = self._next_video(conn, task_id, account_id, account_name)
                if vid is None:
                    break  # 该账号无剩余可采视频

                if not self._claim_video(conn, vid["id"]):
                    continue  # 已被并发抢走（防御；正常不会发生）

                try:
                    # 真实对接位（本阶段不启用）：window = self.bb.open_browser(...)
                    import json
                    collect_types = task.get("collect_types") or []
                    if isinstance(collect_types, str):
                        try:
                            collect_types = json.loads(collect_types)
                        except Exception:
                            collect_types = DEFAULT_COLLECT_TYPES
                    if not collect_types:
                        collect_types = DEFAULT_COLLECT_TYPES
                    # 未选择评论内容时，阶段 B 只完成视频处理，不发起评论采集。
                    comments = []
                    if "comments" in collect_types:
                        comments = self.collector.fetch_comments(
                            vid["vid"], account_name, url=vid.get("url", ""),
                            platform=task.get("platform", "douyin"),
                            window_id=window_id,
                            **self._control_kwargs(self.collector.fetch_comments, task_id),
                        ) or []
                        if self._stop.is_set() or self._task_stop_requested(task_id):
                            self._release_video_for_retry(conn, vid["id"])
                            conn.commit()
                            break
                        self._emit_log(
                            f"[scheduler] 任务 {task_id} 账号 {account_name} 作品 {vid['vid']} "
                            f"评论采集完成：{len(comments)} 条"
                        )
                    for c in comments:
                        extra = c.get("extra") or {}
                        if not isinstance(extra, dict):
                            extra = {}
                        # 采集器返回的地区/主页/点赞等字段与评论正文分开落库，
                        # 导出时才能稳定映射到独立列，不会和最新评论时间串列。
                        for key in ("region", "homepage", "digg", "digg_count", "cid"):
                            if c.get(key) not in (None, ""):
                                if key == "region" and "region" not in collect_types:
                                    continue
                                if key in ("digg", "digg_count") and "engagement" not in collect_types:
                                    continue
                                if key == "cid":
                                    extra["platform_comment_id"] = str(c.get(key))
                                    continue
                                extra[key] = c.get(key)
                        user_id = c.get("user_id") if "comment_user" in collect_types else None
                        nickname = c.get("nickname") if "comment_user" in collect_types else None
                        intent_score = c.get("intent_score", 0) if "intent" in collect_types else 0
                        intent_label = c.get("intent_label") if "intent" in collect_types else None
                        reply_suggestion = c.get("reply_suggestion") if "intent" in collect_types else None
                        comment_id = db.insert_comment(
                            conn, vid["id"], user_id, nickname,
                            c.get("content") if "comments" in collect_types else None,
                            c.get("comment_time"), extra=extra,
                            intent_score=intent_score,
                            intent_label=intent_label,
                            reply_suggestion=reply_suggestion,
                            platform=task.get("platform", "douyin"),
                        )
                        self._handle_collected_comment(
                            conn, comment_id, lead_service,
                            task_id=task_id, video_id=vid["id"], comment=c)
                    if self._stop.is_set() or self._task_stop_requested(task_id):
                        # 取消可能发生在评论写入之后、完成标记之前；保留
                        # assigned 状态，恢复时会从同一作品断点继续。
                        self._release_video_for_retry(conn, vid["id"])
                        conn.commit()
                        break
                    self._finish_video(conn, vid["id"])
                    self._bump_account(conn, account_id)  # processed+1, batch+1
                    local_batch += 1
                    conn.commit()
                    if local_batch >= batch_size and self._has_more(conn, task_id, account_id, account_name):
                        self._do_cooldown(
                            conn, account_id, account_name, cd_seconds, task_id=task_id
                        )
                        local_batch = 0
                except HumanInterventionRequired as h:
                    # P7：冻结账号，等待用户处理；该视频保持 collecting（恢复后续采）
                    self._emit_log(f"[scheduler] 账号 {account_name} 需要人工接管（{h.reason}），冻结等待恢复…")
                    self.mark_human_waiting(account_id, h.reason, task_id=task_id)
                except Exception as e:  # noqa: BLE001
                    detail = f"{type(e).__name__}: {e}".strip()
                    if self._stop.is_set() or self._task_stop_requested(task_id):
                        self._release_video_for_retry(conn, vid["id"])
                        conn.commit()
                        break
                    if self._is_browser_connection_error(e):
                        # LiveCollector 会先自动重连并重试一次；仍然失败时暂停任务，
                        # 把当前作品放回 assigned，等待用户重开浏览器后点击继续。
                        self._release_video_for_retry(conn, vid["id"])
                        retry_detail = f"浏览器连接断开，已保留当前作品待重试：{detail}"
                        self._record_task_error(conn, task_id, retry_detail)
                        conn.commit()
                        self.pause(task_id)
                        with self._lock:
                            self._task_status[task_id] = "paused"
                        self._emit_log(
                            f"[scheduler] 账号 {account_name} 浏览器连接断开，"
                            f"任务 {task_id} 已暂停，作品 {vid['vid']} 未计入失败；请重开浏览器后继续"
                        )
                        break
                    self._fail_video(conn, vid["id"])
                    self._record_task_error(conn, task_id, detail)
                    conn.commit()
                    self._emit_log(f"[scheduler] 账号 {account_name} 视频 {vid['vid']} 采集失败：{detail}")
        finally:
            # 退出时置回 idle（waiting_human 除外：状态落盘保持冻结）
            if not self._is_waiting(account_id):
                try:
                    self._acct_status(conn, account_id, "idle")
                except Exception:
                    pass
                # 不自动关闭浏览器：同一账号可能马上被等待队列中的下一个任务复用，
                # 自动关闭会让缓存的 CDP 会话失效并导致后续任务启动失败。
            with self._lock:
                if self._worker_task.get(account_id) == task_id:
                    self._worker_task.pop(account_id, None)
                if self._worker_threads.get(account_id) is threading.current_thread():
                    self._worker_threads.pop(account_id, None)
                current_threads = self._task_threads.get(task_id)
                if current_threads is not None:
                    remaining = [
                        thread for thread in current_threads
                        if thread is not threading.current_thread() and thread.is_alive()
                    ]
                    if remaining:
                        self._task_threads[task_id] = remaining
                    else:
                        self._task_threads.pop(task_id, None)
            try:
                conn.close()
            except Exception:
                pass
            self._restart_waiting_tasks()

    def _restart_waiting_tasks(self):
        """账号释放后自动唤醒等待账号的任务。start 自身负责再次校验账号。"""
        with self._lock:
            if self._closed or self._stop.is_set():
                return
            if self._waiting_restart_inflight:
                return
            self._waiting_restart_inflight = True
        threading.Thread(target=self._restart_waiting_tasks_worker,
                         name="restart-waiting", daemon=True).start()

    def _restart_waiting_tasks_worker(self):
        try:
            # 从当前 worker 的 finally 触发，稍后再查，确保该线程已真正结束。
            time.sleep(.15)
            if self._closed or self._stop.is_set():
                return
            with self._conn_lock:
                rows = self.conn.execute(
                    "SELECT id FROM tasks WHERE status = 'waiting_account' ORDER BY id"
                ).fetchall()
            for row in rows:
                threading.Thread(target=self.start, args=(int(row["id"]),),
                                 name=f"restart-{row['id']}", daemon=True).start()
        except Exception as e:
            self._emit_log(f"[scheduler] 唤醒等待任务失败：{e}")
        finally:
            with self._lock:
                self._waiting_restart_inflight = False

    def _close_account_browser_if_idle(self, conn, account_id, account_name, window_id):
        """账号没有任何待处理作品后关闭其 BitBrowser 窗口，释放内存。

        只有确认该账号没有参与其它任务的 assigned/collecting 作品时才关闭，
        避免多个任务并行时误关正在使用的浏览器。
        """
        if not window_id or self.bb is None:
            return
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM videos "
                "WHERE assigned_account = ? AND status IN ('assigned','collecting')",
                (account_name,),
            ).fetchone()
            if row and int(row["c"] or 0) > 0:
                return
            self.bb.close_browser(window_id)
            self._emit_log(f"[scheduler] 账号 {account_name} 采集完成，已关闭浏览器 {window_id}")
        except Exception as e:
            self._emit_log(f"[scheduler] 关闭账号 {account_name} 浏览器失败：{e}")

    # ------------------------------------------------------------------ worker 内部
    def _next_video(self, conn, task_id: int, account_id: int, account_name: str):
        cur = conn.execute(
            "SELECT id, vid, url FROM videos WHERE task_id = ? AND assigned_account = ? "
            "AND status IN ('assigned','collecting') ORDER BY id LIMIT 1",
            (task_id, account_name),
        )
        row = cur.fetchone()
        return self._r(row) if row else None

    def _claim_video(self, conn, video_id: int) -> bool:
        """条件更新抢占：只把 assigned/collecting 的视频置为 collecting（防重采）。"""
        cur = conn.execute(
            "UPDATE videos SET status = 'collecting' WHERE id = ? AND status IN ('assigned','collecting')",
            (video_id,),
        )
        conn.commit()
        return cur.rowcount > 0

    def _finish_video(self, conn, video_id: int):
        conn.execute(
            "UPDATE videos SET status = 'done', collected_at = datetime('now','localtime') WHERE id = ?",
            (video_id,),
        )

    def _fail_video(self, conn, video_id: int):
        conn.execute("UPDATE videos SET status = 'failed' WHERE id = ?", (video_id,))

    def _bump_account(self, conn, account_id: int):
        conn.execute(
            "UPDATE accounts SET processed_count = processed_count + 1, "
            "batch_count = batch_count + 1 WHERE id = ?",
            (account_id,),
        )

    def _has_more(self, conn, task_id: int, account_id: int, account_name: str) -> bool:
        cur = conn.execute(
            "SELECT COUNT(*) c FROM videos WHERE task_id = ? AND assigned_account = ? "
            "AND status IN ('assigned','collecting')",
            (task_id, account_name),
        )
        r = cur.fetchone()
        return bool(r and r["c"] > 0)

    def _do_cooldown(self, conn, account_id: int, account_name: str, cd_seconds: int,
                     task_id: int | None = None):
        """每组采完进入冷却：DB 落 cd_until；等待可取消；demo 钩子可模拟跳过。"""
        if cd_seconds <= 0:
            db.begin_cooldown(conn, account_id, 0)  # 立即过期，不等待
            db.reset_batch(conn, account_id)
        else:
            db.begin_cooldown(conn, account_id, cd_seconds)   # 记录 cd_until + status=cooldown
            if not (self.cooldown_handler and self.cooldown_handler(account_name, cd_seconds)):
                evt = self._cd_events.setdefault(account_id, threading.Event())
                evt.clear()
                evt.wait(timeout=cd_seconds)  # 可取消：shutdown/pause 置位即醒
            db.reset_batch(conn, account_id)  # batch_count 归零；cd_until 过期则清
        if self._stop.is_set() or (task_id is not None and self._task_stop_requested(task_id)):
            # 任务暂停/停止时，worker finally 负责最终 idle；这里不能把账号
            # 又写回 working，否则状态轮询会看到短暂的假工作态。
            conn.commit()
            return
        # 冷却结束，恢复工作态（即使 cd_until 尚未过期也如实标记，报表更准确）
        self._acct_status(conn, account_id, "working")
        conn.commit()

    def _park_human(self, account_id: int):
        """工作线程放入 waiting 队列：只等 Event，不做任何采集动作。"""
        evt = self._human_events.setdefault(account_id, threading.Event())
        evt.wait(timeout=0.5)  # 周期醒来检查 stop / 是否仍冻结

    def _task_pause_evt(self, task_id: int) -> threading.Event:
        """取某任务的暂停事件（惰性创建；默认 set=运行，除非处于全局暂停态）。"""
        with self._lock:
            evt = self._pause_evts.get(task_id)
            if evt is None:
                evt = threading.Event()
                if self._global_paused:
                    evt.clear()   # 全局暂停时新任务也保持暂停
                else:
                    evt.set()
                self._pause_evts[task_id] = evt
            return evt

    def _wait_if_paused(self, task_id: int) -> bool:
        """按任务编号的暂停等待。返回 False 表示应退出（shutdown）。

        只阻塞【当前任务 task_id】的 worker；其它任务的 worker 不受影响。
        """
        evt = self._task_pause_evt(task_id)
        while not evt.is_set():
            stop_evt = self._task_stop_evts.get(task_id)
            if self._stop.is_set() or (stop_evt is not None and stop_evt.is_set()):
                return False
            evt.wait(0.3)
        stop_evt = self._task_stop_evts.get(task_id)
        return not self._stop.is_set() and not (stop_evt is not None and stop_evt.is_set())

    # ------------------------------------------------------------------ P7 冻结
    def mark_human_waiting(self, account_id: int, reason: str = "captcha", task_id: int | None = None):
        """冻结账号（P7）：置 waiting_human + 审计；工作线程 park，绝不采集/操作浏览器。"""
        with self._lock:
            self._waiting_accounts.add(account_id)
            if task_id is None:
                task_id = self._worker_task.get(account_id)
            if task_id is not None:
                self._waiting_tasks[account_id] = int(task_id)
            self._human_events.setdefault(account_id, threading.Event()).clear()
            with self._ctl_lock:
                db.update_account_status(self.ctl, account_id, "waiting_human",
                                         wait_reason=reason, wait_since=_now_iso())
            self._log_human_action(account_id, reason, f"冻结：等待人工处理（{reason}）")
        # 二维码/验证码只冻结触发它的任务；其它任务继续运行。
        # 只有用户明确点击该任务的“继续采集”时才会 resolve/resume。
        if task_id is not None:
            # 阶段 B 的人工验证也要把本轮运行明确标记为“人工暂停”。
            # 否则任务可能仍保留上一轮搜索的 search_exhausted 标记，
            # resume_task 会误判为用户要求补采搜索，重新打开搜索阶段。
            self._update_collection_run(
                int(task_id), status="paused",
                stop_reason=f"需要人工验证：{reason}",
            )
            self.pause(task_id)

    def resolve_human(self, account_id: int) -> bool:
        """用户处理完成，恢复账号并继续采集（其余账号不受影响）。"""
        with self._lock:
            # 重启后 _waiting_accounts 不会保留，但 DB 中的冻结状态仍然有效；
            # 允许人工复核/用户继续清理这种持久化冻结。
            if account_id not in self._waiting_accounts:
                with self._ctl_lock:
                    row = self.ctl.execute(
                        "SELECT status FROM accounts WHERE id = ?", (account_id,)
                    ).fetchone()
                if not row or row["status"] != "waiting_human":
                    return False
            task_id = self._waiting_tasks.get(account_id)
            if task_id is not None:
                self._resume_account_by_task[int(task_id)] = int(account_id)
            self._waiting_accounts.discard(account_id)
            self._waiting_tasks.pop(account_id, None)
            self._human_events.setdefault(account_id, threading.Event()).set()
            with self._ctl_lock:
                db.update_account_status(self.ctl, account_id, "idle")
            self._log_human_action(account_id, "user_continued", "用户已处理，恢复采集")
            return True

    def reconcile_human_waiting(self) -> dict:
        """复核数据库中残留的人工冻结；只在当前页无拦截时自动解除。"""
        if self.bb is None:
            return {"checked": 0, "cleared": 0, "kept": 0}
        with self._human_reconcile_lock:
            if self._human_reconcile_inflight:
                return {"checked": 0, "cleared": 0, "kept": 0, "skipped": True}
            self._human_reconcile_inflight = True
        checked = cleared = kept = 0
        try:
            with self._ctl_lock:
                rows = self.ctl.execute(
                    "SELECT id, name, platform, bb_window_id, status FROM accounts "
                    "WHERE status = 'waiting_human' ORDER BY id"
                ).fetchall()
            if not rows:
                return {"checked": 0, "cleared": 0, "kept": 0}
            try:
                from .operations.browser_health import BrowserHealthChecker  # type: ignore
            except ImportError:
                from operations.browser_health import BrowserHealthChecker  # type: ignore
            checker = BrowserHealthChecker(self.conn, self.bb)
            for row in rows:
                account = dict(row)
                if self._thread_alive(int(account["id"])):
                    kept += 1
                    continue
                checked += 1
                result = checker.check_current_account(account)
                checks = result.get("checks") or {}
                safe = (
                    result.get("status") not in ("human_required", "login_required", "failed")
                    and bool(checks.get("domain_match"))
                    and not bool(checks.get("captcha"))
                    and not bool(checks.get("login_required"))
                )
                self._emit_log(
                    f"[scheduler] 账号 {account.get('name') or account['id']} "
                    f"人工冻结复核：{result.get('detail') or '无详细结果'}"
                )
                if safe and self.resolve_human(int(account["id"])):
                    cleared += 1
                    self._emit_log(
                        f"[scheduler] 账号 {account.get('name') or account['id']} "
                        "已确认恢复，解除重启后残留人工冻结"
                    )
                else:
                    kept += 1
            # 启动复核只负责清理重启前残留的账号冻结，不能因为账号恢复就自动
            # 唤醒 waiting_account 任务。否则用户尚未点击任何操作时，任务会立刻
            # 进入搜索阶段并滚动浏览器。正常运行期间的人工继续/账号释放仍由
            # resolve_human() 和 worker finally 路径负责唤醒任务。
            return {"checked": checked, "cleared": cleared, "kept": kept}
        finally:
            with self._human_reconcile_lock:
                self._human_reconcile_inflight = False

    def reconcile_human_waiting_async(self) -> None:
        """后台启动一次人工冻结复核，不阻塞 GUI/服务启动。"""
        threading.Thread(
            target=self.reconcile_human_waiting,
            name="reconcile-human-freezes",
            daemon=True,
        ).start()

    def waiting_accounts_for_task(self, task_id: int) -> set[int]:
        """返回指定任务当前处于人工等待的账号 ID（供 GUI 精确恢复）。"""
        with self._lock:
            current = {
                aid for aid, tid in self._waiting_tasks.items()
                if int(tid) == int(task_id) and aid in self._waiting_accounts
            }
        if current:
            return current
        # GUI 重启后内存中的 waiting_tasks 不存在；用任务绑定的账号和
        # 持久化 waiting_human 状态恢复人工验证归属，避免继续时丢原账号。
        try:
            with self._conn_lock:
                task = self._r(self.conn.execute(
                    "SELECT platform, task_accounts FROM tasks WHERE id = ?",
                    (int(task_id),),
                ).fetchone())
                if not task:
                    return set()
                names = task.get("task_accounts") or "[]"
                if isinstance(names, str):
                    try:
                        names = _json.loads(names)
                    except (TypeError, ValueError):
                        names = []
                if not isinstance(names, list):
                    names = []
                bound = {str(name).strip() for name in names if str(name).strip()}
                rows = self.conn.execute(
                    "SELECT id, name FROM accounts WHERE platform = ? "
                    "AND status = 'waiting_human'",
                    (str(task.get("platform") or "douyin"),),
                ).fetchall()
                return {
                    int(row["id"]) for row in rows
                    if not bound or str(row["name"] or "") in bound
                }
        except sqlite3.Error:
            return set()

    # ------------------------------------------------------------------ 任务控制
    def _pause_task_and_release(self, task_id: int, reason: str = "用户暂停") -> None:
        """发出可恢复暂停信号，并让该任务的账号最终回到 idle。

        暂停不再让旧 worker 长时间停在内存里占用账号：先设置任务级停止
        事件，采集器在当前可取消点退出，未完成作品保留为 assigned/collecting
        断点；线程真正退出后由 worker/阶段 finally 清理账号租约。若底层浏览器
        请求暂时不可取消，账号会暂时保留 working，直到线程真实退出，避免把仍
        在操作浏览器的账号错误地释放给另一个任务。
        """
        tid = int(task_id)
        with self._lock:
            pause_evt = self._task_pause_evt(tid)
            pause_evt.clear()
            release_evt = self._pause_release_evts.setdefault(tid, threading.Event())
            release_evt.set()
            stop_evt = self._task_stop_evts.setdefault(tid, threading.Event())
            stop_evt.set()
            worker_items = [
                (int(account_id), thread)
                for account_id, owner_task in self._worker_task.items()
                if int(owner_task) == tid
                for thread in [self._worker_threads.get(account_id)]
                if thread is not None
            ]
            phase = self._phase_threads.get(tid)
            supervisor = self._supervisors.get(tid)

        # 先落任务暂停态，supervisor 即使刚好观察到 worker 已退出，也不能
        # 把用户暂停覆盖成自动补采/完成。
        with self._ctl_lock:
            row = self.ctl.execute(
                "SELECT status FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
            if row and row["status"] not in ("done", "aborted", "failed"):
                db.update_task_status(self.ctl, tid, "paused", str(reason or "用户暂停"))

        account_ids = {account_id for account_id, _thread in worker_items}
        phase_account = None
        with self._lock:
            phase_account = self._phase_account_by_task.get(tid)
        if phase_account is not None:
            account_ids.add(int(phase_account))
        # 取消冷却/人工等待；阶段 A 使用任务 stop_event，不需要调用共享
        # collector.pause()，避免误伤其它任务。
        for account_id in account_ids:
            event = self._cd_events.get(account_id)
            if event is not None:
                event.set()
            event = self._human_events.get(account_id)
            if event is not None:
                event.set()

        self._mark_task_paused(tid, str(reason or "用户暂停"))
        alive_names = []
        for _account_id, thread in worker_items:
            if thread.is_alive():
                alive_names.append(thread.name)
        if phase is not None and phase.is_alive():
            alive_names.append(phase.name)
        if supervisor is not None and supervisor.is_alive():
            alive_names.append(supervisor.name)
        if alive_names:
            self._emit_log(
                f"[scheduler] 任务 {tid} 已暂停，正在等待 {len(alive_names)} 个采集线程退出后释放账号"
            )
        else:
            self._emit_log(f"[scheduler] 任务 {tid} 已暂停，账号已释放")

    def pause(self, task_id=None, reason: str = "用户暂停"):
        """暂停采集。

        - 传 task_id：只暂停该任务（其它任务继续跑，符合"按任务编号暂停"）。
        - 不传 task_id：暂停所有任务（含未来新建任务），供 P7 人工接管全局冻结。
        """
        if task_id is None:
            with self._lock:
                self._global_paused = True
                task_ids = set(self._pause_evts)
                task_ids.update(self._task_threads)
                task_ids.update(self._phase_threads)
                task_ids.update(self._supervisors)
                task_ids.update(int(tid) for tid in self._worker_task.values())
                for evt in list(self._pause_evts.values()):
                    evt.clear()
            for tid in sorted(task_ids):
                self._pause_task_and_release(tid, reason=reason)
            if hasattr(self.collector, "pause"):
                self.collector.pause()
        else:
            self._pause_task_and_release(task_id, reason=reason)

    def resume(self, task_id=None):
        """继续采集。

        - 传 task_id：只继续该任务。
        - 不传 task_id：继续所有任务。
        """
        if task_id is None:
            with self._lock:
                self._global_paused = False
                for evt in list(self._pause_evts.values()):
                    evt.set()
                restart_ids = []
                for tid, release_evt in self._pause_release_evts.items():
                    if release_evt.is_set():
                        release_evt.clear()
                        self._task_stop_evts.setdefault(tid, threading.Event()).clear()
                        restart_ids.append(int(tid))
            if hasattr(self.collector, "resume"):
                self.collector.resume()
            for tid in sorted(set(restart_ids)):
                threading.Thread(
                    target=self.start, args=(tid,), name=f"resume-{tid}", daemon=True
                ).start()
        else:
            with self._lock:
                tid = int(task_id)
                self._safety_recovery_tasks.add(tid)
                release_evt = self._pause_release_evts.get(tid)
                if release_evt is not None and release_evt.is_set():
                    release_evt.clear()
                    self._task_stop_evts.setdefault(tid, threading.Event()).clear()
                self._task_pause_evt(tid).set()

    def stop_task(self, task_id: int):
        """停止【指定任务】的所有 worker（其它任务不受影响）。

        置该任务的停止信号并唤醒其冷却/人工等待，join 对应 worker；
        之后该任务可再次 start 续跑（断点语义）。
        """
        with self._lock:
            task_id = int(task_id)
            # stop 与可恢复暂停必须区分，避免 supervisor 把停止误处理为
            # 可继续的暂停；worker 仍会把当前作品保留为 assigned。
            release_evt = self._pause_release_evts.setdefault(task_id, threading.Event())
            release_evt.clear()
            stop_evt = self._task_stop_evts.setdefault(task_id, threading.Event())
            stop_evt.set()
            # 取消该任务暂停，避免 worker 停在暂停等待
            pause_evt = self._pause_evts.get(task_id)
            if pause_evt is not None:
                pause_evt.set()
            # 找出属于该任务的账号（worker 归属登记）
            stop_accounts = [
                acc_id for acc_id, tid in self._worker_task.items() if tid == task_id
            ]
            stop_threads = [self._worker_threads.get(a) for a in stop_accounts]
            phase_account = self._phase_account_by_task.get(task_id)
            if phase_account is not None:
                stop_accounts.append(int(phase_account))
            task_phase = self._phase_threads.get(task_id)
            task_supervisor = self._supervisors.get(task_id)
        # 先持久化 aborted，supervisor 读到停止信号前不会把任务落成其它终态。
        with self._ctl_lock:
            row = self.ctl.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row and row["status"] not in ("done", "failed"):
                db.update_task_status(self.ctl, task_id, "aborted", "用户停止")
        # 唤醒冷却/人工等待，让 worker 立即退出取视频循环
        for acc_id in set(stop_accounts):
            if acc_id in self._cd_events:
                self._cd_events[acc_id].set()
            if acc_id in self._human_events:
                self._human_events[acc_id].set()
        for t in stop_threads:
            if t is not None:
                t.join(timeout=3)
        phase = task_phase
        if phase is not None and phase is not threading.current_thread():
            phase.join(timeout=3)
        supervisor = task_supervisor
        if supervisor is not None and supervisor is not threading.current_thread():
            supervisor.join(timeout=3)
        with self._conn_lock:
            db.update_task_status(self.conn, task_id, "aborted", "用户停止")
        with self._lock:
            self._task_status[task_id] = "aborted"
        if phase is not threading.current_thread():
            self._release_phase_account(task_id)
        # 不调用共享 collector.cancel()：它会把其它任务一并取消。

    def _any_task_running(self, exclude_task_id=None) -> bool:
        """是否存在仍在运行的 worker（可选排除某任务）。"""
        with self._lock:
            for acc_id, tid in list(self._worker_task.items()):
                if exclude_task_id is not None and tid == exclude_task_id:
                    continue
                th = self._worker_threads.get(acc_id)
                if th is not None and th.is_alive():
                    return True
        return False

    def wait_for_task(self, task_id: int, timeout: float | None = None) -> bool:
        """阻塞直到该任务本轮收敛（done 或 incomplete），返回是否完成。"""
        evt = self._done_events.get(task_id)
        if evt is None:
            return False
        return evt.wait(timeout)

    def shutdown(self, close_connections: bool = False):
        """停止所有 worker（应用退出/全局停止）。唤醒全部等待并等待退出；可再次 start。"""
        self.stop_monitoring()
        self._stop.set()
        self._runtime_heartbeat_stop.set()
        try:
            self._intent_batch_processor.close()
        except Exception:
            pass
        with self._lock:
            self._global_paused = False
            for evt in list(self._pause_evts.values()):
                evt.set()
            for evt in list(self._task_stop_evts.values()):
                evt.set()
        if hasattr(self.collector, "cancel"):
            self.collector.cancel()
        for e in self._cd_events.values():
            e.set()
        for e in self._human_events.values():
            e.set()
        with self._lock:
            threads = list(self._worker_threads.values())
        with self._lock:
            phase_threads = list(self._phase_threads.values())
            supervisor_threads = list(self._supervisors.values())
        current_thread = threading.current_thread()
        for t in threads + phase_threads + supervisor_threads:
            if t is not None and t is not current_thread:
                t.join(timeout=2)
        heartbeat = self._runtime_heartbeat_thread
        if heartbeat is not None and heartbeat is not current_thread:
            heartbeat.join(timeout=2)
        with self._lock:
            phase_accounts = list(self._phase_account_by_task)
            phase_snapshot = {tid: self._phase_threads.get(tid) for tid in phase_accounts}
        for tid in phase_accounts:
            phase = phase_snapshot.get(tid)
            if phase is None or not phase.is_alive():
                self._release_phase_account(tid)
        # 清空按任务的暂停事件与任务登记，避免长期运行堆积
        with self._lock:
            self._pause_evts.clear()
            for account_id, thread in list(self._worker_threads.items()):
                if thread is None or not thread.is_alive():
                    self._worker_threads.pop(account_id, None)
                    self._worker_task.pop(account_id, None)
            for tid, thread in list(self._phase_threads.items()):
                if thread is None or not thread.is_alive():
                    self._phase_threads.pop(tid, None)
            for tid, thread in list(self._supervisors.items()):
                if thread is None or not thread.is_alive():
                    self._supervisors.pop(tid, None)
        if not any(t.is_alive() for t in threads + phase_threads + supervisor_threads):
            self._stop.clear()
        if close_connections:
            self._closed = True
            with self._conn_lock:
                try:
                    self.conn.close()
                except Exception:
                    pass
            with self._ctl_lock:
                try:
                    self.ctl.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------ 报表
    def status_report(self) -> dict:
        """汇总报表（json.dumps 可直接序列化），供 GUI 渲染。"""
        # UI 轮询是最频繁、最可靠的状态刷新入口。先清理旧进程/已结束
        # 线程留下的 working/cooldown，再生成快照，避免界面长期显示假占用。
        self._reconcile_stale_account_states()
        # 与采集回调对共享 self.conn 的写入使用同一把连接锁，确保 GUI
        # 轮询读取到的是完整提交后的快照，而不是写入过程中的半个结果。
        with self._conn_lock, self._lock:
            all_accounts = [dict(item) for item in self._all_accounts()]
            account_display = {}
            for item in all_accounts:
                platform_key = str(item.get("platform") or "douyin").strip().lower()
                raw_name = str(item.get("name") or "").strip()
                resolved_nickname = str(
                    item.get("nickname") or item.get("nick") or item.get("display_name") or ""
                ).strip()
                # 纯数字通常是窗口/平台用户 ID，不应再冒充昵称显示。
                display_name = resolved_nickname or (
                    raw_name
                    if raw_name and not looks_like_account_key(
                        raw_name, window_id=item.get("bb_window_id")
                    )
                    else "未读取昵称"
                )
                if raw_name:
                    account_display[(platform_key, raw_name)] = display_name
                account_display[(platform_key, str(item.get("id") or ""))] = display_name
            tasks_out = {}
            for t in self._all_tasks():
                tid = int(t["id"])
                raw_status = t.get("status")
                phase_thread = self._phase_threads.get(tid)
                phase_active = phase_thread is not None and phase_thread.is_alive()
                worker_active = any(
                    task_id == tid and th is not None and th.is_alive()
                    for account_id, task_id in self._worker_task.items()
                    for th in [self._worker_threads.get(account_id)]
                )
                cur = self.conn.execute(
                    "SELECT status, COUNT(*) c FROM videos WHERE task_id = ? GROUP BY status",
                    (tid,),
                )
                vc = {r["status"]: int(r["c"]) for r in cur.fetchall()}
                cur = self.conn.execute(
                    "SELECT COUNT(*) c FROM comments WHERE video_id IN "
                    "(SELECT id FROM videos WHERE task_id = ?)",
                    (tid,),
                )
                row = cur.fetchone()
                done = self._r(row)
                raw_done_count = int(vc.get("done", 0) or 0)
                valid_video_done = raw_done_count
                valid_comments = int(done.get("c", 0) or 0)
                lead_count = 0
                try:
                    valid_row = self.conn.execute(
                        "SELECT COUNT(*) AS c FROM videos "
                        "WHERE task_id = ? AND status = 'done' "
                        "AND TRIM(COALESCE(title, '')) <> '' "
                        "AND TRIM(COALESCE(url, '')) <> '' "
                        "AND TRIM(COALESCE(vid, '')) <> ''",
                        (tid,),
                    ).fetchone()
                    valid_video_done = int(valid_row["c"] if valid_row else 0)
                    valid_comment_row = self.conn.execute(
                        "SELECT COUNT(*) AS c FROM comments c "
                        "JOIN videos v ON v.id = c.video_id "
                        "WHERE v.task_id = ? AND v.status = 'done' "
                        "AND TRIM(COALESCE(v.title, '')) <> '' "
                        "AND TRIM(COALESCE(v.url, '')) <> '' "
                        "AND TRIM(COALESCE(c.content, '')) <> ''",
                        (tid,),
                    ).fetchone()
                    valid_comments = int(valid_comment_row["c"] if valid_comment_row else 0)
                    lead_row = self.conn.execute(
                        "SELECT COUNT(DISTINCT l.id) AS c FROM leads l "
                        "JOIN lead_evidence e ON e.lead_id = l.id "
                        "LEFT JOIN videos v ON v.id = COALESCE(e.video_id, "
                        "(SELECT c2.video_id FROM comments c2 WHERE c2.id = e.comment_id)) "
                        "WHERE COALESCE(e.task_id, v.task_id) = ?",
                        (tid,),
                    ).fetchone()
                    lead_count = int(lead_row["c"] if lead_row else 0)
                except sqlite3.Error:
                    # 旧库尚未建线索表时，任务报表仍可正常显示基础采集数据。
                    pass
                task_status = raw_status
                pause_evt = self._pause_evts.get(tid)
                if task_status == "aborted":
                    task_status = "stopped"
                elif task_status in ("phase_a_search", "phase_b_comments", "running") and (
                        self._global_paused or
                        (pause_evt is not None and not pause_evt.is_set())):
                    task_status = "paused"
                run_summary = None
                monitoring_rule = None
                keyword_queries = []
                effective_target = int(t.get("target_count") or 100)
                try:
                    query_rows = self.conn.execute(
                        "SELECT query_order, query, target_count, status, discovered_count, "
                        "new_count, duplicate_count FROM task_search_queries "
                        "WHERE task_id = ? ORDER BY query_order", (tid,)
                    ).fetchall()
                    keyword_queries = [dict(row) for row in query_rows]
                    if keyword_queries:
                        effective_target = sum(max(1, int(row.get("target_count") or 100))
                                               for row in keyword_queries)
                    run_row = self.conn.execute(
                        "SELECT run_id, status, started_at, finished_at, stop_reason, "
                        "discovered_count, new_count, duplicate_count, failed_count "
                        "FROM collection_runs WHERE task_id = ? ORDER BY started_at DESC LIMIT 1",
                        (tid,),
                    ).fetchone()
                    if run_row:
                        run_summary = dict(run_row)
                    rule_row = self.conn.execute(
                        "SELECT enabled, interval_seconds, next_run_at, last_run_at, no_more_observed "
                        "FROM monitoring_rules WHERE task_id = ?", (tid,)
                    ).fetchone()
                    if rule_row:
                        monitoring_rule = dict(rule_row)
                except sqlite3.Error:
                    # 旧库迁移失败时保留原状态报表，不让新增字段破坏 GUI。
                    pass
                bound_names = t.get("task_accounts") or "[]"
                if isinstance(bound_names, str):
                    try:
                        bound_names = _json.loads(bound_names)
                    except (TypeError, ValueError):
                        bound_names = []
                if not isinstance(bound_names, list):
                    bound_names = []
                task_platform = str(t.get("platform") or "douyin").strip().lower()
                account_names = []
                for bound in bound_names:
                    value = str(bound or "").strip()
                    if not value:
                        continue
                    account_names.append(account_display.get((task_platform, value), value))
                start_at = str((run_summary or {}).get("started_at") or t.get("created_at") or "")
                end_at = str((run_summary or {}).get("finished_at") or "")
                error_reason = str(
                    t.get("error_message")
                    or (run_summary or {}).get("stop_reason")
                    or t.get("search_stop_reason")
                    or ""
                ).strip()
                tasks_out[tid] = {
                    **t,
                    "status": task_status,
                    "videos_total": sum(vc.values()),
                    "video_pending": vc.get("pending", 0),
                    "video_assigned": vc.get("assigned", 0),
                    "video_collecting": vc.get("collecting", 0),
                    "video_done": vc.get("done", 0),
                    "valid_video_done": valid_video_done,
                    "video_failed": vc.get("failed", 0),
                    "comments": int(done.get("c", 0)),
                    "valid_comments": valid_comments,
                    "lead_count": lead_count,
                    "account_names": account_names,
                    "start_at": start_at,
                    "end_at": end_at,
                    "error_reason": error_reason,
                    "keyword_queries": keyword_queries,
                    "keyword_count": len(keyword_queries),
                    "target_per_keyword": int(t.get("target_count") or 100),
                    "effective_target_count": effective_target,
                    "latest_run": run_summary,
                    "monitoring_rule": monitoring_rule,
                }

            accounts_out = {}
            for a in all_accounts:
                d = dict(a)
                raw_name = str(d.get("name") or "").strip()
                resolved_nickname = str(
                    d.get("nickname") or d.get("nick") or d.get("display_name") or ""
                ).strip()
                d["nickname"] = resolved_nickname or (
                    raw_name
                    if raw_name and not looks_like_account_key(
                        raw_name, window_id=d.get("bb_window_id")
                    )
                    else ""
                )
                d["nickname_resolved"] = bool(resolved_nickname)
                d["cooldown_left_seconds"] = self._cd_left_seconds(a.get("cd_until"))
                accounts_out[f"{a['platform']}:{a['id']}"] = d

            # 总览页的线索/互动指标直接从数据库汇总，避免 QML 为了显示卡片
            # 再发起一次全量查询；查询仍在后台服务线程和连接锁内执行。
            lead_total = 0
            open_interaction_total = 0
            try:
                row = self.conn.execute("SELECT COUNT(*) AS c FROM leads").fetchone()
                lead_total = int(row["c"] if row else 0)
                row = self.conn.execute(
                    "SELECT COUNT(*) AS c FROM interaction_drafts "
                    "WHERE status IN ('draft','pending_review','approved','queued')"
                ).fetchone()
                open_interaction_total = int(row["c"] if row else 0)
            except sqlite3.Error:
                # 老数据库尚未完成线索表迁移时，保留基础总览数据。
                pass

            # “暂无更多视频”表示平台已明确耗尽，是可交付的终止状态；
            # 它虽然没有达到用户设定目标，但不能继续被统计为未完成任务。
            def terminal_task_done(item):
                status = str(item.get("status") or "")
                if status in ("done", "completed", "no_more"):
                    return True
                return status == "incomplete" and bool(item.get("search_exhausted"))

            totals = {
                "tasks": len(tasks_out),
                "tasks_running": sum(1 for t in tasks_out.values()
                                      if t["status"] in ("phase_a_search", "phase_b_comments", "running")),
                "tasks_done": sum(1 for t in tasks_out.values() if terminal_task_done(t)),
                "videos_total": sum(t["videos_total"] for t in tasks_out.values()),
                "videos_done": sum(t["video_done"] for t in tasks_out.values()),
                "valid_videos_done": sum(t["valid_video_done"] for t in tasks_out.values()),
                "comments": sum(t["comments"] for t in tasks_out.values()),
                "valid_comments": sum(t["valid_comments"] for t in tasks_out.values()),
                "leads": lead_total,
                "interactions": open_interaction_total,
                "accounts": len(accounts_out),
                "waiting_human": [a["name"] for a in accounts_out.values() if a["status"] == "waiting_human"],
            }

            return {
                "paused": self._global_paused or any(not e.is_set() for e in self._pause_evts.values()),
                "tasks": tasks_out,
                "accounts": accounts_out,
                "totals": totals,
            }
