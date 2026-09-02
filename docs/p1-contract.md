# P1 骨架 — 技术手册与接口契约（v1，2026-08-18）

> 本文件是本阶段全部子代理的唯一权威契约。三份交付物（bitbrowser.py / db.py / scheduler.py）
> 必须严格遵循本文件的接口签名与数据形状。设计文档：`D:\DSH\cdp\多账号采集平台-设计文档.md`（1~7 章骨架）。
> 项目根：`D:\DSH\platform`（src 目录放 Python 模块，docs 放文档）。

## 0. 环境事实（实测确认）

- Python 3.11.9 可用（`C:\Users\StarLink\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe`），标准库优先，**不要引入未验证的第三方依赖**（无 requests/flask 保证；可用 urllib）。
- BitBrowser Global 7.1.5 已安装，本地 API 服务运行在 `http://127.0.0.1:54345`，**无需鉴权**（enableAuth=false）。
- 所有接口 **POST + JSON body**；返回 `{"success": true, "data": ...}` 或 `{"success": false, "msg": "..."}`。
- 实测例：`browser/open` 返回 `data.ws = "ws://127.0.0.1:58930/devtools/browser/<uuid>"` + `data.http = "127.0.0.1:58930"`。
- ⚠️ **中文编码**：发 JSON body 必须用 UTF-8 字节（PowerShell 发中文会乱码，Python 用 `json.dumps(..., ensure_ascii=False).encode("utf-8")`）。
- ⚠️ 窗口关闭后**等 5 秒**再删/重开（官方要求进程彻底退出）。
- Windows + 标准库：SQLite3、threading、urllib 均可。GUI 本阶段不做（后续）。
- 工作目录：模块放在 `D:\DSH\platform\src\` 下。每个文件必须可 `python -c "import ..."` 直接导入成功。

## 1. bitbrowser.py — BitBrowser 客户端封装

文件：`D:\DSH\platform\src\bitbrowser.py`
类：`class BitBrowserClient`，构造 `BitBrowserClient(base_url="http://127.0.0.1:54345", timeout=20)`。

方法（全部返回 Python dict；请求失败抛 `BitBrowserError(异常)`，包裹 API 返回的 msg）：

| 方法 | 调用的官方接口 | 请求体示例 | 返回（data 部分） |
|---|---|---|---|
| `health()` | `POST /health` | `{}` | `{"success": true, "data": "the server is running good."}` 原样返回 data |
| `list_browsers(page=0, page_size=100, **filters)` | `POST /browser/list` | `{"page":0,"pageSize":100}` | 完整 data dict：`{"page","pageSize","totalNum","list":[...]}` |
| `detail(browser_id)` | `POST /browser/detail` | `{"id": ...}` | data: 窗口对象 |
| `open_browser(browser_id, args=None, queue=True, ignore_default_urls=False, new_page_url=None)` | `POST /browser/open` | `{"id", "args": [], "queue": true}` | data 含 `ws`、`http`、`coreVersion`、`pid` |
| `close_browser(browser_id)` | `POST /browser/close` | `{"id": ...}` | data |
| `close_by_seqs(seqs)` / `close_all()` | `/browser/close/byseqs` / `/browser/close/all` | `{"seqs":[...]}` / `{}` | data |
| `pids(ids)`, `pids_all()` | `/browser/pids` / `/browser/pids/all` | `{"ids":[...]}` / `{}` | data: {browserId: pid} |
| `ports()` | `POST /browser/ports` | `{}` | data: {browserId: "port"} |
| `create_window(name, group_id=None, proxy=None, fingerprint=None, **kw)` | `POST /browser/update` | 见下 | data: 完整窗口对象（含新建的 id） |
| `update_partial(ids, **fields)` | `POST /browser/update/partial` | `{"ids":[...], ...fields}` | data |
| `cookies_get(browser_id)`, `cookies_set(browser_id, cookies)`, `cookies_clear(browser_id, save_synced=True)` | `/browser/cookies/get` / `set` / `clear` | 见官方文档 | data |
| `check_agent(host, port, proxy_type='socks5', username='', password='', ip_check_service='ip123in')` | `POST /checkagent` | 见官方文档 | data |

`create_window` 请求体最小形状（proxy=None 时用 noproxy 直连）：
```json
{ "name": "...", "proxyMethod": 2, "proxyType": "noproxy", "browserFingerPrint": {} }
```
（`browserFingerPrint` 传空对象 = 随机指纹，必传。）`proxy` 传 dict 时：`{"proxyMethod":2,"proxyType":"socks5|http|https|ssh","host","port","proxyUserName","proxyPassword"}`。

模块头部写模块 docstring + 一句"接口来自 D:\DSH\cdp\doc-browser-api.txt"。

## 2. db.py — SQLite 数据层

文件：`D:\DSH\platform\src\db.py`

- `def init_db(db_path: str) -> sqlite3.Connection` — 建库建表（幂等，`CREATE TABLE IF NOT EXISTS`），返回连接（`row_factory=sqlite3.Row`，`PRAGMA journal_mode=WAL`）。
- 默认库路径：`D:\DSH\platform\data\platform.db`（函数允许显式传入）。
- 表结构（按设计文档 3.2 节，字段可增不可删核心字段）：

```sql
-- 任务
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  keyword TEXT NOT NULL,              -- 关键词
  platform TEXT NOT NULL DEFAULT 'douyin',
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|phase_a_search|phase_b_comments|done|aborted|failed
  batch_size INTEGER NOT NULL DEFAULT 10,  -- 每账号冷却前采集的 URL 数
  cooldown_seconds INTEGER NOT NULL DEFAULT 75,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at TEXT
);
-- 视频 URL（阶段 A 产物）
CREATE TABLE IF NOT EXISTS videos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  platform TEXT NOT NULL DEFAULT 'douyin',
  vid TEXT NOT NULL UNIQUE,           -- 平台视频/笔记 ID
  url TEXT NOT NULL,
  title TEXT,
  author TEXT,
  extra TEXT,                          -- JSON 扩展
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|assigned|collecting|done|failed
  assigned_account TEXT,               -- 分配给哪个账号（阶段 B）
  collected_at TEXT,
  FOREIGN KEY (task_id) REFERENCES tasks(id)
);
-- 账号
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,                  -- 账号/窗口名
  bb_window_id TEXT,                   -- BitBrowser 窗口 ID
  platform TEXT NOT NULL DEFAULT 'douyin',
  status TEXT NOT NULL DEFAULT 'idle', -- idle|working|cooldown|waiting_human|frozen|dead
  processed_count INTEGER NOT NULL DEFAULT 0,
  batch_count INTEGER NOT NULL DEFAULT 0,  -- 当前批次已采数（10 触发冷却）
  cd_until TEXT,                       -- 冷却截止时间 (ISO)
  wait_reason TEXT,                    -- 冻结原因（验证码/登录态）
  wait_since TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
-- 评论
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
-- 人工接管审计（P7）
CREATE TABLE IF NOT EXISTS human_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER,
  action TEXT NOT NULL,                -- captcha|login_expired|slider|user_continued
  detail TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
```

- 提供的便捷函数（命名自定但语义要等价）：
  - 任务：`create_task(conn, keyword, platform, batch_size, cooldown_seconds) -> int`、`update_task_status(conn, task_id, status)`、`get_task(conn, task_id)`
  - 视频：`insert_video(conn, task_id, vid, url, title=None, author=None, extra=None) -> int`（冲突时返回已有 id）、`mark_videos_assigned(conn, task_id, account, vids)`、`get_videos_by_status(conn, task_id, status) -> list`
  - 账号：`upsert_account(conn, name, bb_window_id, platform) -> int`、`update_account_status(...)`、`begin_cooldown(conn, account_id, seconds)`（设置 cd_until）、`reset_batch(conn, account_id)`（batch_count 归零；同时若 cd_until 过期则清空）
  - 评论：`insert_comment(conn, video_id, user_id, nickname, content, comment_time, extra=None, intent_score=0, intent_label=None, reply_suggestion=None)`
- `db.py` 不得 import bitbrowser 或 scheduler（保持单向依赖：scheduler→bitbrowser, db）。

## 3. scheduler.py — 任务调度器

文件：`D:\DSH\platform\src\scheduler.py`

```python
class Scheduler:
    def __init__(self, db_path: str, bb: "BitBrowserClient" | None = None):
        # bb 为 None 时用 BitBrowserClient()（或允许 mock 注入）
    def create_task(self, keyword, platform='douyin', batch_size=10, cooldown_seconds=60) -> int
    def add_account(self, name, bb_window_id, platform='douyin') -> int
    def start(self, task_id: int):
        """阶段A: 搜索(占位) -> 阶段B: URL 均分给账号并行采评论。
        真实搜索/评论采集用 `collector` 回调占位（本阶段假数据可跑通）"""
    def status_report(self) -> dict
```

关键行为（必须实现的逻辑）：
1. **状态机**：task `pending -> phase_a_search -> phase_b_comments -> done`；account `idle -> working -> cooldown -> idle`、`waiting_human`（遇验证码等不可自动恢复的状态，冻结该账号，**其余账号不中断**）。自带 `pause()`/`resume()`（task 级暂停后从断点恢复 = 已采集的 videos 不重采）。
2. **URL 均分**：阶段 A 产出的 videos 按账号数均分（余数从第一个账号顺延）。每账号按 `batch_size`（默认 10）分组，**每组采完进入 cooldown_seconds（60~90 建议 75）**。
3. **断点续跑**：`start()` 在任何状态可重入——以 DB 为准（videos.status / accounts.cd_until / task.status），不重复分配已完成/采集中的视频。
4. **冷却逻辑**：`account.status='cooldown'` + `cd_until` 存 DB；重入时检查 `cd_until` 与当前时间，过期则恢复。
5. **并发**：多账号并行（`threading.Thread` 每账号一个 worker；或统一线程池）；**不依赖第三方库**。
6. **P7 模拟**：`mark_human_waiting(account_id, reason)` 冻结该账号（不 refresh/不 close）；`resolve_human(account_id)` 恢复并继续。scheduler 只负责状态编排——冻结期间**绝不对该窗口执行任何 browser 操作**。
7. **占位采集回调**：`scheduler` 通过注入的 `collector` 对象采集评论，接口：
   ```python
   class Collector:  # 由上层注入（本阶段由 sched 内置的 FakeCollector 演示）
       def search(self, keyword, platform) -> list[dict]      # -> [{"vid":..., "url":..., "title":...}]
       def fetch_comments(self, vid, account) -> list[dict]   # -> [{"user_id":..., "nickname":..., "content":..., "comment_time":...}]
   ```
8. `status_report()` 返回汇总 dict：各任务状态、各账号状态/进度/冷却剩余、视频与评论计数——供 GUI 直接渲染。
9. scheduler.py 顶部注释写明依赖关系：`db -> scheduler -> bitbrowser`；collector 注入点。

>>> 演示入口 <<<
在 `D:\DSH\platform\src\__main__.py`（或同目录 `demo.py`）提供 `python src/demo.py` 可跑的假数据全流程：
建任务（比如关键词"智能快递柜"）→ 注入 FakeCollector（搜索出 25 个假视频、每视频 2~5 条假评论）→ 3 个假账号并行跑完阶段 A+B（每 10 个冷却用钩子打印"账号 X 冷却 75s（模拟）"而不真等）→ 打印 status_report 与每个账号的收集计数、task 转 done。
**demo 的冷却必须可跳过**（`cooldown_seconds=0` 或 `--no-cooldown` 参数）以便立即跑通。

## 4. 验收标准（子代理交付前自查）

1. `python -c "import sys; sys.path.insert(0,'D:/DSH/platform'); import src.bitbrowser, src.db, src.scheduler"` 无报错。
2. `python src/demo.py --no-cooldown`（从 `D:\DSH\platform` 运行）在 30 秒内跑完并打印完整的 status_report、task=done、各账号计数正确（25 视频 3 账号 → 分配 9/8/8；每账号每批 10；评论总数 = sum(fake)）。
3. db.py 幂等：连续两次 init 同库不报错。
4. bitbrowser.py 的真实接口方法已按上表挂好（本阶段可不真调外部，但方法存在且对 54345 的调用路径正确；用 `BitBrowserClient().health()` 实测一次能通）。

## 5. 交付内容清单

- `D:\DSH\platform\src\bitbrowser.py`
- `D:\DSH\platform\src\db.py`
- `D:\DSH\platform\src\scheduler.py`
- `D:\DSH\platform\src\demo.py`
- （可选）`D:\DSH\platform\requirements.txt`（应为空/仅注释，因为只用标准库）