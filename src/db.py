# -*- coding: utf-8 -*-
"""
db.py — 多账号采集平台 P1 骨架的 SQLite 数据层。

本模块独立封装数据库访问：不 import bitbrowser / scheduler，也不依赖任何
第三方库（依赖关系单向：scheduler -> bitbrowser, db）。表结构与函数语义
严格对齐权威契约 D:\\DSH\\platform\\docs\\p1-contract.md 第 2 节。

提供的便捷函数：
- 建库  : init_db（幂等建表，row_factory=sqlite3.Row，WAL 模式）
- 任务  : create_task / update_task_status / get_task
- 视频  : insert_video / mark_videos_assigned / get_videos_by_status
- 账号  : upsert_account / update_account_status / begin_cooldown / reset_batch
- 评论  : insert_comment

约定：
- 时间为本地时间，ISO 8601 字符串（YYYY-MM-DDTHH:MM:SS），与表默认值
  datetime('now','localtime') 保持一致；同格式 ISO 字符串的字典序即时间先后序。
- 写操作在函数内部立即 commit，调用方无需再提交。
- 涉及 UNIQUE 冲突的写入一律用 INSERT OR IGNORE 幂等处理，绝不抛异常。
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta

from comment_time import normalize_xhs_comment_time

# 默认库路径由运行目录决定（init_db 允许显式传入其它路径）。
DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "platform.db",
)

log = logging.getLogger(__name__)

# 表结构（契约第 2 节原文；字段只增不删核心字段）
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  keyword TEXT NOT NULL,              -- 关键词
  platform TEXT NOT NULL DEFAULT 'douyin',
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|phase_a_search|phase_b_comments|done|aborted|failed
  batch_size INTEGER NOT NULL DEFAULT 10,  -- 每账号冷却前采集的 URL 数
  cooldown_seconds INTEGER NOT NULL DEFAULT 75,
  collect_mode TEXT NOT NULL DEFAULT 'standard', -- fast|standard|deep
  search_sort TEXT NOT NULL DEFAULT 'default', -- 平台专属搜索排序键
  target_count INTEGER NOT NULL DEFAULT 100,
  only_with_comments INTEGER NOT NULL DEFAULT 0,
  collect_types TEXT NOT NULL DEFAULT '["video_info","author_info","engagement","comments","comment_user","region","intent"]',
  task_accounts TEXT NOT NULL DEFAULT '[]',  -- 任务绑定的账号名列表(JSON)，按平台隔离
  output_dir TEXT,
  error_message TEXT,
  search_exhausted INTEGER NOT NULL DEFAULT 0, -- 已明确确认搜索没有更多结果
  search_phase_complete INTEGER NOT NULL DEFAULT 0, -- 阶段A已完成，正在/已进入阶段B
  search_stop_reason TEXT,
  search_stopped_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS videos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  platform TEXT NOT NULL DEFAULT 'douyin',
  vid TEXT NOT NULL,                  -- 平台视频/笔记 ID
  url TEXT NOT NULL,
  title TEXT,
  author TEXT,
  extra TEXT,                          -- JSON 扩展
  search_query TEXT,                   -- 本作品来自哪个预制关键词
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|assigned|collecting|done|failed
  assigned_account TEXT,               -- 分配给哪个账号（阶段 B）
  collected_at TEXT,
  FOREIGN KEY (task_id) REFERENCES tasks(id),
  UNIQUE (task_id, platform, vid)
);
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,                  -- 账号/窗口名
  nickname TEXT,                       -- 平台实际昵称（与内部账号/窗口标识分离）
  platform_user_id TEXT,               -- 已确认的平台用户 ID（用于稳定归属校验）
  bb_window_id TEXT,                   -- BitBrowser 窗口 ID
  platform TEXT NOT NULL DEFAULT 'douyin',
  status TEXT NOT NULL DEFAULT 'idle', -- idle|working|cooldown|waiting_human|frozen|dead
  runtime_owner TEXT,                  -- 当前采集进程租约（进程异常退出后用于识别残留状态）
  runtime_heartbeat TEXT,              -- 当前采集进程最近一次心跳
  processed_count INTEGER NOT NULL DEFAULT 0,
  batch_count INTEGER NOT NULL DEFAULT 0,  -- 当前批次已采数（10 触发冷却）
  cd_until TEXT,                       -- 冷却截止时间 (ISO)
  wait_reason TEXT,                    -- 冻结原因（验证码/登录态）
  wait_since TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id INTEGER NOT NULL,
  platform TEXT NOT NULL DEFAULT 'douyin',
  user_id TEXT,                        -- 平台用户 ID
  nickname TEXT,
  content TEXT,
  comment_time TEXT,
  extra TEXT,
  intent_score INTEGER DEFAULT 0,      -- 意向分 5/3/1
  intent_label TEXT,                   -- high|medium|low
  reply_suggestion TEXT,
  UNIQUE (video_id, user_id, content),
  FOREIGN KEY (video_id) REFERENCES videos(id)
);
CREATE TABLE IF NOT EXISTS human_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER,
  action TEXT NOT NULL,                -- captcha|login_expired|slider|user_continued
  detail TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


def _now_iso() -> str:
    """返回本地时间 ISO 8601 字符串（秒级精度）。"""
    return datetime.now().isoformat(timespec="seconds")


def _to_json(value):
    """dict/list 转 JSON 字符串，其余原样返回（已为 str 或 None 时保持不动）。"""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _row_to_dict(row):
    """sqlite3.Row 转 dict；非 Row 对象（如 tuple）也尽量转换。"""
    if row is None:
        return None
    if hasattr(row, "keys"):
        return dict(row)
    return {}


def init_db(db_path: str = None, check_same_thread: bool = True) -> sqlite3.Connection:
    """建库建表（幂等，CREATE TABLE IF NOT EXISTS），返回连接。

    连接属性：row_factory=sqlite3.Row、PRAGMA journal_mode=WAL；
    db_path 缺省为 D:\\DSH\\platform\\data\\platform.db，父目录自动创建。
    """
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    db_path = os.path.abspath(db_path)
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA_SQL)
    task_cols = {r[1] for r in conn.execute("PRAGMA table_info('tasks')").fetchall()}
    account_cols = {r[1] for r in conn.execute("PRAGMA table_info('accounts')").fetchall()}
    if "nickname" not in account_cols:
        # 旧版本把账号标识、窗口名和平台昵称混在 name 中。新增独立字段，
        # 只扩展表结构，不改写旧数据，保证升级不影响任务绑定。
        conn.execute("ALTER TABLE accounts ADD COLUMN nickname TEXT")
    if "platform_user_id" not in account_cols:
        # 账号名称可能是窗口名，不能长期承担平台 UID 的作用。
        # 新字段只增加稳定归属信息，不改动旧账号绑定和任务依赖的 name。
        conn.execute("ALTER TABLE accounts ADD COLUMN platform_user_id TEXT")
    if "runtime_owner" not in account_cols:
        # 账号运行状态必须能区分“当前进程占用”和“上次异常退出残留”。
        # 该列只用于运行时租约，不改变账号标识或任务绑定。
        conn.execute("ALTER TABLE accounts ADD COLUMN runtime_owner TEXT")
    if "runtime_heartbeat" not in account_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN runtime_heartbeat TEXT")
    if "collect_mode" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN collect_mode TEXT NOT NULL DEFAULT 'standard'")
    if "search_sort" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN search_sort TEXT NOT NULL DEFAULT 'default'")
    if "target_count" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN target_count INTEGER NOT NULL DEFAULT 100")
    if "only_with_comments" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN only_with_comments INTEGER NOT NULL DEFAULT 0")
    if "collect_types" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN collect_types TEXT NOT NULL DEFAULT '[\"video_info\",\"author_info\",\"engagement\",\"comments\",\"comment_user\",\"region\",\"intent\"]'")
    if "task_accounts" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN task_accounts TEXT NOT NULL DEFAULT '[]'")
    if "output_dir" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN output_dir TEXT")
    if "error_message" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN error_message TEXT")
    if "search_exhausted" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN search_exhausted INTEGER NOT NULL DEFAULT 0")
    if "search_phase_complete" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN search_phase_complete INTEGER NOT NULL DEFAULT 0")
    if "search_stop_reason" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN search_stop_reason TEXT")
    if "search_stopped_at" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN search_stopped_at TEXT")
    conn.commit()
    _migrate_legacy_video_unique(conn)
    _repair_comments_table(conn)
    _deduplicate_comments(conn)
    _run_lead_migrations(conn)
    # 兼容新增 search_phase_complete 之前已经进入阶段 B 的旧任务：
    # 只要关键词快照全部是终态，就在启动时补记阶段 A 已完成，避免恢复时重搜。
    try:
        conn.execute(
            "UPDATE tasks SET search_phase_complete = 1 "
            "WHERE status IN ('phase_b_comments', 'running', 'incomplete', 'done') "
            "AND EXISTS (SELECT 1 FROM task_search_queries q0 WHERE q0.task_id = tasks.id) "
            "AND NOT EXISTS (SELECT 1 FROM task_search_queries q1 "
            "WHERE q1.task_id = tasks.id AND q1.status NOT IN ('completed', 'no_more'))"
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
    _normalize_existing_xhs_comments(conn)
    return conn


def _run_lead_migrations(conn):
    """幂等应用线索运营与互动中心的新增表迁移（只增不改，见技术方案第 5 章）。"""
    try:
        from migrations import MigrationRunner

        MigrationRunner().run(conn)
    except Exception as exc:
        # 迁移失败不影响老库打开：记录并继续，由上层决定是否中止。
        log.warning("线索/互动数据库迁移失败，已回滚：%s", exc)
        # 不在此抛异常，保证旧库仍可正常使用（Agent A 验收红线）。
        try:
            conn.rollback()
        except Exception:
            pass


def _migrate_legacy_video_unique(conn: sqlite3.Connection) -> None:
    """迁移旧版 videos(vid 全局唯一)为按任务/平台唯一，保留主键。"""
    for idx in conn.execute("PRAGMA index_list('videos')").fetchall():
        # seq, name, unique, origin, partial
        if not idx[2]:
            continue
        cols = [r[2] for r in conn.execute(
            f"PRAGMA index_info('{idx[1]}')"
        ).fetchall()]
        if cols != ["vid"]:
            continue
        # 不再 rename 原表。SQLite 会在 rename 时把 comments 的外键目标同步改成
        # videos_legacy，随后删除旧表会留下永久损坏的外键。这里采用同名替换，
        # comments 始终继续引用 videos。
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript("""
        CREATE TABLE videos_new (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id INTEGER NOT NULL,
          platform TEXT NOT NULL DEFAULT 'douyin',
          vid TEXT NOT NULL,
          url TEXT NOT NULL,
          title TEXT,
          author TEXT,
          extra TEXT,
          status TEXT NOT NULL DEFAULT 'pending',
          assigned_account TEXT,
          collected_at TEXT,
          FOREIGN KEY (task_id) REFERENCES tasks(id),
          UNIQUE (task_id, platform, vid)
        );
        INSERT INTO videos_new
          (id, task_id, platform, vid, url, title, author, extra, status,
           assigned_account, collected_at)
        SELECT id, task_id, platform, vid, url, title, author, extra, status,
               assigned_account, collected_at
        FROM videos;
        DROP TABLE videos;
        ALTER TABLE videos_new RENAME TO videos;
        """)
        conn.execute("PRAGMA foreign_keys=ON")
        break


def _repair_comments_table(conn: sqlite3.Connection) -> None:
    """修复曾被旧版 videos rename 迁移改坏的 comments 外键。"""
    foreign_keys = conn.execute("PRAGMA foreign_key_list('comments')").fetchall()
    if not any(str(row[2]) != "videos" for row in foreign_keys):
        return
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript("""
    CREATE TABLE comments_new (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      video_id INTEGER NOT NULL,
      platform TEXT NOT NULL DEFAULT 'douyin',
      user_id TEXT,
      nickname TEXT,
      content TEXT,
      comment_time TEXT,
      extra TEXT,
      intent_score INTEGER DEFAULT 0,
      intent_label TEXT,
      reply_suggestion TEXT,
      UNIQUE (video_id, user_id, content),
      FOREIGN KEY (video_id) REFERENCES videos(id)
    );
    INSERT OR IGNORE INTO comments_new
      (id, video_id, platform, user_id, nickname, content, comment_time, extra,
       intent_score, intent_label, reply_suggestion)
    SELECT id, video_id, platform, user_id, nickname, content, comment_time, extra,
           intent_score, intent_label, reply_suggestion
    FROM comments;
    DROP TABLE comments;
    ALTER TABLE comments_new RENAME TO comments;
    """)
    conn.execute("PRAGMA foreign_keys=ON")


def _deduplicate_comments(conn: sqlite3.Connection) -> None:
    """让缺少用户 ID/正文的评论也具备稳定幂等性。"""
    conn.execute("""
        DELETE FROM comments
        WHERE id NOT IN (
          SELECT MIN(id) FROM comments
          GROUP BY video_id, platform, COALESCE(user_id, ''), COALESCE(content, '')
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_comments_identity
        ON comments(video_id, platform, COALESCE(user_id, ''), COALESCE(content, ''))
    """)
    conn.commit()


def _normalize_existing_xhs_comments(conn: sqlite3.Connection) -> None:
    """修复历史小红书评论的无年份时间，并把尾部地区写回 extra。"""
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    has_lead_tables = "leads" in tables and "lead_evidence" in tables
    if "comments" not in tables:
        return
    comment_columns = {
        row[1] for row in conn.execute("PRAGMA table_info('comments')").fetchall()
    }
    if "platform" not in comment_columns or "comment_time" not in comment_columns:
        return
    has_extra = "extra" in comment_columns
    select_extra = ", extra" if has_extra else ""
    rows = conn.execute(
        f"SELECT id, comment_time{select_extra} FROM comments "
        "WHERE LOWER(platform) = 'xhs' AND comment_time IS NOT NULL"
    ).fetchall()
    current = datetime.now()
    updates = []
    for row in rows:
        normalized, region = normalize_xhs_comment_time(row["comment_time"])
        # 旧版本已经把小红书的“月-日+地区”错误落成了当前年份的完整日期，
        # 原始是否带年份已无法从库中恢复。当前年份的未来日期不可能是已采集
        # 的评论，因此将其安全回拨到上一年；正常的历史/明确年份不动。
        stored = str(normalized or "")
        try:
            parsed_stored = datetime.strptime(stored, "%Y-%m-%d")
        except ValueError:
            parsed_stored = None
        if (parsed_stored is not None and parsed_stored.year == current.year
                and parsed_stored.date() > current.date()):
            normalized = parsed_stored.replace(year=current.year - 1).strftime("%Y-%m-%d")
        extra = row["extra"] if has_extra else None
        extra_obj = {}
        if extra:
            try:
                parsed = json.loads(extra) if isinstance(extra, str) else extra
                if isinstance(parsed, dict):
                    extra_obj = dict(parsed)
            except (TypeError, ValueError):
                extra_obj = {}
        if region and not extra_obj.get("region"):
            extra_obj["region"] = region
        new_extra = _to_json(extra_obj) if extra_obj else extra
        if normalized != row["comment_time"] or (has_extra and new_extra != extra):
            updates.append((normalized, new_extra, row["id"]))
    if not updates:
        return
    if has_extra:
        conn.executemany(
            "UPDATE comments SET comment_time = ?, extra = ? WHERE id = ?",
            updates,
        )
    else:
        conn.executemany(
            "UPDATE comments SET comment_time = ? WHERE id = ?",
            [(normalized, comment_id) for normalized, _extra, comment_id in updates],
        )
    # 线索和证据都缓存了评论时间，保持互动中心与线索中心显示一致。
    if has_lead_tables:
        for normalized, _extra, comment_id in updates:
            conn.execute(
                "UPDATE leads SET last_interaction_at = ? WHERE source_comment_id = ?",
                (normalized, comment_id),
            )
            conn.execute(
                "UPDATE lead_evidence SET occurred_at = ? WHERE comment_id = ?",
                (normalized, comment_id),
            )
    conn.commit()


# ---------------------------------------------------------------- 任务

def create_task(conn: sqlite3.Connection, keyword: str, platform: str = "douyin",
                batch_size: int = 10, cooldown_seconds: int = 75,
                collect_mode: str = "standard", target_count: int = 100,
                collect_types=None, task_accounts=None,
                only_with_comments: bool = False, output_dir: str = None,
                search_sort: str = "default", execution_mode: str = "once",
                keyword_group_id: int = None) -> int:
    """创建采集任务，返回新任务 id。

    task_accounts：该任务绑定的账号名列表（JSON 存储）；为 None 时存空列表。
    """
    if collect_types is None:
        collect_types = ["video_info", "author_info", "engagement", "comments", "comment_user", "region", "intent"]
    if task_accounts is None:
        task_accounts = []
    cur = conn.execute(
        "INSERT INTO tasks (keyword, platform, batch_size, cooldown_seconds, collect_mode, search_sort, target_count, only_with_comments, collect_types, task_accounts, output_dir, execution_mode, keyword_group_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (keyword, platform, batch_size, cooldown_seconds, collect_mode, search_sort,
         target_count, int(bool(only_with_comments)), _to_json(collect_types),
         _to_json(task_accounts), output_dir, str(execution_mode or "once"), keyword_group_id),
    )
    conn.commit()
    return cur.lastrowid


def update_task_status(conn: sqlite3.Connection, task_id: int, status: str, error_message: str = None) -> None:
    """更新任务状态（pending|phase_a_search|phase_b_comments|done|aborted|failed）。

    同时刷新 updated_at 为当前本地时间。
    """
    conn.execute(
        "UPDATE tasks SET status = ?, error_message = ?, updated_at = ? WHERE id = ?",
        (status, error_message, _now_iso(), task_id),
    )
    conn.commit()


def get_task(conn: sqlite3.Connection, task_id: int) -> dict:
    """按 id 查询任务，返回 dict（不存在时返回 None）。"""
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _row_to_dict(row)


# ---------------------------------------------------------------- 视频

def insert_video(conn: sqlite3.Connection, task_id: int, vid: str, url: str,
                 title: str = None, author: str = None, extra=None,
                 platform: str = "douyin", search_query: str = None) -> int:
    """插入一条视频记录，返回视频 id。

    vid 全局 UNIQUE：冲突时不做任何修改，直接返回已存在行的 id（幂等，不抛异常）。
    extra 传 dict/list 时自动序列化为 JSON 字符串。
    """
    conn.execute(
        "INSERT OR IGNORE INTO videos (task_id, platform, vid, url, title, author, extra, search_query) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, platform, vid, url, title, author, _to_json(extra), search_query),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM videos WHERE task_id = ? AND platform = ? AND vid = ?",
        (task_id, platform, vid),
    ).fetchone()
    return row["id"] if row is not None else None


def mark_videos_assigned(conn: sqlite3.Connection, task_id: int, account: str,
                         vids) -> int:
    """把一批 vid 标记为已分配给某账号（status='assigned', assigned_account=account）。

    返回实际更新的行数；vids 为空时直接返回 0。
    """
    if not vids:
        return 0
    placeholders = ",".join("?" * len(vids))
    cur = conn.execute(
        "UPDATE videos SET status = 'assigned', assigned_account = ? "
        "WHERE task_id = ? AND vid IN (%s)" % placeholders,
        (account, task_id, *vids),
    )
    conn.commit()
    return cur.rowcount


def get_videos_by_status(conn: sqlite3.Connection, task_id: int, status: str) -> list:
    """按 task_id + status 查询视频，返回 dict 列表（按 id 升序）。"""
    rows = conn.execute(
        "SELECT * FROM videos WHERE task_id = ? AND status = ? ORDER BY id",
        (task_id, status),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------- 账号

def upsert_account(conn: sqlite3.Connection, name: str, bb_window_id: str = None,
                   platform: str = "douyin", nickname: str = None) -> int:
    """按平台和窗口/昵称幂等写入，避免跨平台同名账号互相覆盖。"""
    row = None
    if bb_window_id:
        row = conn.execute(
            "SELECT id FROM accounts WHERE platform = ? AND bb_window_id = ? ORDER BY id LIMIT 1",
            (platform, bb_window_id),
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id FROM accounts WHERE platform = ? AND name = ? ORDER BY id LIMIT 1",
            (platform, name),
        ).fetchone()
    if row is not None:
        fields = ["bb_window_id = ?", "platform = ?"]
        values = [bb_window_id, platform]
        if str(nickname or "").strip():
            fields.append("nickname = ?")
            values.append(str(nickname).strip())
        values.append(row["id"])
        conn.execute(
            "UPDATE accounts SET %s WHERE id = ?" % ", ".join(fields),
            tuple(values),
        )
        conn.commit()
        return row["id"]
    cur = conn.execute(
        "INSERT INTO accounts (name, nickname, bb_window_id, platform) VALUES (?, ?, ?, ?)",
        (name, str(nickname or "").strip() or None, bb_window_id, platform),
    )
    conn.commit()
    return cur.lastrowid


def update_account_nickname(conn: sqlite3.Connection, account_id: int,
                            nickname: str) -> None:
    """保存平台实际昵称，不改动任务依赖的内部账号标识 ``name``。"""
    value = str(nickname or "").strip()
    if not value:
        return
    conn.execute(
        "UPDATE accounts SET nickname = ? WHERE id = ?",
        (value, int(account_id)),
    )
    conn.commit()


def update_account_status(conn: sqlite3.Connection, account_id: int, status: str,
                          wait_reason: str = None, wait_since: str = None) -> None:
    """更新账号状态；wait_reason / wait_since 仅在显式传入（非 None）时更新。

    典型用法：update_account_status(conn, aid, 'waiting_human', 'captcha', now)。
    """
    fields, values = ["status = ?"], [status]
    if wait_reason is not None:
        fields.append("wait_reason = ?")
        values.append(wait_reason)
    if wait_since is not None:
        fields.append("wait_since = ?")
        values.append(wait_since)
    # 非运行态不能继续保留旧进程租约，否则下次启动会误判账号仍在工作。
    if status == "idle":
        fields.extend([
            "cd_until = NULL", "wait_reason = NULL", "wait_since = NULL",
            "runtime_owner = NULL", "runtime_heartbeat = NULL",
        ])
    elif status in ("waiting_human", "frozen", "dead"):
        fields.extend(["runtime_owner = NULL", "runtime_heartbeat = NULL"])
    values.append(account_id)
    conn.execute(
        "UPDATE accounts SET %s WHERE id = ?" % ", ".join(fields), tuple(values)
    )
    conn.commit()


def begin_cooldown(conn: sqlite3.Connection, account_id: int, seconds: int) -> None:
    """账号进入冷却：status='cooldown'，cd_until = 当前时间 + seconds（本地 ISO 时间）。

    seconds <= 0 表示立即过期（cd_until 为过去时间），配合 reset_batch 可立即恢复。
    """
    cd_until = (datetime.now() + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE accounts SET status = 'cooldown', cd_until = ? WHERE id = ?",
        (cd_until, account_id),
    )
    conn.commit()


def reset_batch(conn: sqlite3.Connection, account_id: int) -> None:
    """批次计数归零；若 cd_until 已过期（<= 当前时间）则一并清空并为 cooldown
    状态恢复为 idle。未过期的 cd_until 保留（冷却仍在进行中）。"""
    conn.execute(
        "UPDATE accounts SET batch_count = 0 WHERE id = ?", (account_id,)
    )
    conn.execute(
        "UPDATE accounts SET cd_until = NULL, status = 'idle' "
        ", runtime_owner = NULL, runtime_heartbeat = NULL, "
        "wait_reason = NULL, wait_since = NULL "
        "WHERE id = ? AND cd_until IS NOT NULL AND cd_until <= ? AND status = 'cooldown'",
        (account_id, _now_iso()),
    )
    conn.commit()


def remove_account(conn: sqlite3.Connection, name: str = None, account_id: int = None,
                   platform: str = None) -> bool:
    """优先按主键删除账号；兼容按平台+昵称或旧版仅昵称删除。"""
    if account_id is not None:
        cur = conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    elif platform is not None:
        cur = conn.execute("DELETE FROM accounts WHERE platform = ? AND name = ?", (platform, name))
    else:
        cur = conn.execute("DELETE FROM accounts WHERE name = ?", (name,))
    conn.commit()
    return cur.rowcount > 0


def delete_task(conn: sqlite3.Connection, task_id: int) -> bool:
    """删除任务及其任务级数据，并清理所有外键依赖。

    线索是跨任务按用户聚合的，删除任务时保留线索和互动历史；
    只解除被删除评论作为线索来源的引用，并删除该任务产生的证据。
    这样既能真正删除任务，也不会误删同一用户在其它任务中的线索。
    """
    task_id = int(task_id)

    def table_exists(name: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone() is not None

    with conn:
        # 先解除线索表对即将删除评论的引用，避免 comments 外键阻止删除。
        if table_exists("leads"):
            conn.execute(
                "UPDATE leads SET source_comment_id = NULL "
                "WHERE source_comment_id IN ("
                "SELECT c.id FROM comments c "
                "JOIN videos v ON v.id = c.video_id WHERE v.task_id = ?) ",
                (task_id,),
            )

        # 删除任务对应的线索证据。证据可能只记录 task_id，也可能只记录
        # video_id/comment_id，因此三种关联都要覆盖。
        if table_exists("lead_evidence"):
            conn.execute(
                "DELETE FROM lead_evidence WHERE task_id = ? "
                "OR video_id IN (SELECT id FROM videos WHERE task_id = ?) "
                "OR comment_id IN ("
                "SELECT c.id FROM comments c JOIN videos v ON v.id = c.video_id "
                "WHERE v.task_id = ?)",
                (task_id, task_id, task_id),
            )

        # 运行事件依赖运行记录，监控规则又可能引用最后一次运行；
        # 必须按 events -> monitoring_rules -> runs 的顺序清理。
        if table_exists("collection_events"):
            conn.execute(
                "DELETE FROM collection_events WHERE run_id IN "
                "(SELECT run_id FROM collection_runs WHERE task_id = ?)",
                (task_id,),
            )
        if table_exists("monitoring_rules"):
            conn.execute("DELETE FROM monitoring_rules WHERE task_id = ?", (task_id,))
        if table_exists("collection_runs"):
            conn.execute("DELETE FROM collection_runs WHERE task_id = ?", (task_id,))

        # 关键词快照通常有 ON DELETE CASCADE，这里显式清理以兼容旧库。
        if table_exists("task_search_queries"):
            conn.execute("DELETE FROM task_search_queries WHERE task_id = ?", (task_id,))

        conn.execute(
            "DELETE FROM comments WHERE video_id IN "
            "(SELECT id FROM videos WHERE task_id = ?)",
            (task_id,),
        )
        conn.execute("DELETE FROM videos WHERE task_id = ?", (task_id,))
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return cur.rowcount > 0


# ---------------------------------------------------------------- 评论

def insert_comment(conn: sqlite3.Connection, video_id: int, user_id: str,
                   nickname: str = None, content: str = None,
                   comment_time: str = None, extra=None,
                   intent_score: int = 0, intent_label: str = None,
                   reply_suggestion: str = None, platform: str = "douyin") -> int:
    """插入一条评论，返回评论 id。

    UNIQUE(video_id, user_id, content)：重复评论忽略写入，返回已存在行的 id。
    extra 传 dict/list 时自动序列化为 JSON 字符串。
    """
    if str(platform or "").lower() == "xhs":
        raw_comment_time = str(comment_time).strip() if comment_time else ""
        comment_time, region = normalize_xhs_comment_time(comment_time)
        if isinstance(extra, dict):
            extra = dict(extra)
        elif not extra:
            extra = {}
        if isinstance(extra, dict):
            # 保留平台原始展示值，便于审计“明确年份”和“无年份”的差异；
            # comment_time 仍使用标准 YYYY-MM-DD 供排序和界面展示。
            if raw_comment_time and not extra.get("source_comment_time"):
                extra["source_comment_time"] = raw_comment_time
            if region and not extra.get("region"):
                extra["region"] = region
    conn.execute(
        "INSERT OR IGNORE INTO comments "
        "(video_id, platform, user_id, nickname, content, comment_time, extra, "
        " intent_score, intent_label, reply_suggestion) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (video_id, platform, user_id, nickname, content, comment_time, _to_json(extra),
         intent_score, intent_label, reply_suggestion),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM comments WHERE video_id = ? AND platform = ? "
        "AND user_id IS ? AND content IS ?",
        (video_id, platform, user_id, content),
    ).fetchone()
    return row["id"] if row is not None else None
