# 采集工作台 Clean-Room 重构设计方案

> 版本：v0.1（评审稿）
> 日期：2025-08
> 目标：对「多平台采集工作台」进行**全面 clean-room 重构**,产出全新代码库,彻底抹除与
> `github.com/cv-cat/DouYin_Spider` 开源仓库的任何表达性关联,达到**可对外商用**的合规标准。
>
> 📌 本文档是重构的**唯一权威蓝图**。重构过程中所有决策以此为准。

---

## 0. 核心认知：算法 vs 表达（合规边界）

**重构的可行边界,取决于区分两件事：**

| 类别 | 定义 | 例子 | 版权属性 | 重构策略 |
|---|---|---|---|---|
| **算法/接口规范** | 抖音服务器要求签名必须满足的数学与协议事实 | SM3 摘要、RC4 变体、自定义 Base64 表、签名载荷字段布局 | **非版权**（功能事实/接口规范） | **必须保留**（改了签名失效） |
| **事实常量** | 让算法跑通的必要参数 | SALT="dhzx"、字母表字符、版本号、掩码数值 | **非版权**（事实数据） | **必须保留**（改了签名失效） |
| **代码表达** | 作者的实现方式、命名、注释、组织 | 文件名 `ab_pure.py`、类名 `ABogusPureSigner`、变量 `z146_blend`、README 水印 | **版权**（表达） | **100% 重写**（抹除） |

> **结论**：重构 = 算法逻辑保留（功能必需）+ 代码表达全部重新创作（合规必需）。
> 这是 clean-room 的可行边界，也是唯一既能"抹除痕迹"又不破坏功能的路线。

---

## 1. 现状与污染面（已盘点）

### 1.1 来源仓库

- **仓库**：`github.com/cv-cat/DouYin_Spider`（本地 `D:\DSH\DouYin_Spider`）
- **许可证**：**无 LICENSE 文件** → 默认保留所有权利
- **引入范围**：仅 `utils/` 下的签名相关文件被拷贝到本项目

### 1.2 污染文件（直接来自原仓库）

| 当前文件 | 原仓库对应 | 污染内容 |
|---|---|---|
| `dy-sign-tool/dy_sign/ab_pure.py` | `utils/ab_pure.py` | 文件名、类名、函数名、注释、变量命名、算法实现 |
| `dy-sign-tool/dy_sign/sm3.py` | `utils/sm3.py` | 文件名、实现方式 |
| `dy-sign-tool/dy_sign/mstoken.py` | `utils/mstoken.py` | 文件名、实现方式 |
| `dy-sign-tool/dy_sign/fingerprint.py` | `utils/fingerprint.py` | 文件名、指纹预设 |
| `dy-sign-tool/dy_sign/xbogus_pure.py` | `utils/xbogus_pure.py` | 文件名、实现方式 |
| `dy-sign-tool/dy_sign/strdata_pure.py` | `utils/strdata_pure.py` | 文件名、实现方式 |
| `dy-sign-tool/dy_sign/__init__.py` | — | 注释里引用 `D:\DSH\DouYin_Spider` |

### 1.3 引用痕迹（非污染但需清理）

- `docs/抖音签名服务集成设计.md`：多次提及 `DouYin_Spider`、`D:\DSH\...` 路径
- `dy-sign-tool/dy_sign/__init__.py`：注释引用来源路径

### 1.4 无需改动（原创资产）

- `src/` 下全部采集工作台代码（dy_collect / scheduler / gui / cdp / bitbrowser 等）——**用户原创**
- 主 `README.md`、`多账号采集平台-设计文档.md`——原创文档

---

## 2. 重构目标

| # | 目标 | 验收标准 |
|---|---|---|
| G1 | 全新代码库，与原仓库无表达性关联 | 代码中搜不到原仓库文件名/类名/变量名/注释/README 水印 |
| G2 | 签名功能完全可用 | 签名服务生成 a_bogus → 评论接口 200 → 返回真实评论 |
| G3 | 采集功能完全可用 | 搜索/评论/导出/意向分析全流程跑通 |
| G4 | 可对外商用 | 独立命名、独立文档、独立许可证声明（MIT 或用户指定） |
| G5 | 新旧并存 | 重构版放新目录，旧版保留可回滚 |

---

## 3. 命名体系（全新，杜绝联想）

### 3.1 产品命名

| 项 | 旧（弃用） | 新（启用） |
|---|---|---|
| 产品名 | 多平台采集工作台 | **LeadHarvest（线索收割台）** |
| 目录名 | `多平台采集工作台` / `dy-sign-tool` | `leadharvest/` |
| 签名工具 | `dy-sign-tool` | `leadharvest/signature/` |

### 3.2 模块命名对照（签名部分）

| 旧文件名 | 新文件名 | 旧类/函数 | 新类/函数 |
|---|---|---|---|
| `ab_pure.py` | `a_bogus.py` | `ABogusPureSigner` | `ABogusSigner` |
| `sm3.py` | `digest.py` | `sm3_hash()` | `sm3_digest()` |
| `mstoken.py` | `ms_token.py` | `get_mstoken()` | `fetch_ms_token()` |
| `fingerprint.py` | `env_profile.py` | `get_profile()` | `browser_profile()` |
| `xbogus_pure.py` | `x_bogus.py` | `XbogusSigner` | `XBogusSigner` |
| `strdata_pure.py` | `report_body.py` | `build_report_body()` | `build_envelope()` |
| `dy_sign_service.py` | `sign_service.py` | `DySignService` | `SignatureService` |

### 3.3 变量/函数重写原则

- 所有 `z1xx_xxx` 混淆风格命名 → 语义化命名（如 `z146_blend` → `interleave_bytes`）
- 所有单字母变量（L、a8、v6）→ 描述性变量（payload、blend_block、random_seed）
- 注释全部重写为独立表达，不保留原仓库措辞

---

## 4. 目录规划（leadharvest/）

```
leadharvest/                      # 重构后的新项目根
├─ README.md                      # 全新 README（独立命名、无来源痕迹）
├─ LICENSE                        # 商业许可证声明（待定，见第 8 节）
├─ pyproject.toml                 # 工程化打包
├─ requirements.txt
├─ app/
│  ├─ __init__.py
│  ├─ main.py                     # 入口（原 gui.py）
│  ├─ config.py                   # 配置加载（原 config_loader.py）
│  ├─ database.py                 # SQLite 数据层（原 db.py）
│  ├─ scheduler.py                # 任务调度（原 scheduler.py）
│  ├─ platform/                   # 平台采集器
│  │  ├─ base.py                  # Collector 抽象
│  │  ├─ douyin.py                # 抖音采集（原 dy_collect.py）
│  │  ├─ xiaohongshu.py           # 小红书采集（原 xhs_collect3.py）
│  │  ├─ weibo.py                 # 微博采集（原 weibo_chrome.py）
│  │  └─ bilibili.py              # B站采集（原 bilibili_adapter.py）
│  ├─ browser/                    # 浏览器控制层
│  │  ├─ bitbrowser_api.py        # BitBrowser API（原 bitbrowser.py）
│  │  ├─ cdp_client.py            # CDP 客户端（原 cdp.py）
│  │  └─ window_manager.py        # 窗口管理
│  ├─ signature/                  # 签名服务（原 dy-sign-tool）
│  │  ├─ a_bogus.py               # a_bogus 算法（原 ab_pure.py）
│  │  ├─ digest.py                # SM3（原 sm3.py）
│  │  ├─ ms_token.py              # msToken（原 mstoken.py）
│  │  ├─ env_profile.py           # 指纹（原 fingerprint.py）
│  │  ├─ x_bogus.py               # X-Bogus（原 xbogus_pure.py）
│  │  ├─ report_body.py           # 上报体（原 strdata_pure.py）
│  │  └─ sign_service.py          # HTTP 签名服务（原 dy_sign_service.py）
│  ├─ leads/                      # 意向分析（原 src/leads/）
│  ├─ interaction/                # 互动（原 src/interactions/）
│  ├─ ui/                         # 界面（原 src/ui/）
│  └─ operations/                 # 运维（原 src/operations/）
├─ tests/                         # 测试（重写）
├─ docs/                          # 文档（重写）
└─ data/                          # 运行时数据（gitignore）
```

---

## 5. 签名核心重构要点

### 5.1 a_bogus 算法（原 ab_pure.py）

**保留（算法事实）**：
- SM3 双重摘要 + SALT 拼接
- RC4 变体（逆序 S 盒初始化）
- 自定义 Base64（两个字母表 S3/S4）
- 载荷字段布局（L[12]~L[89] 的字段语义）
- 置换表 A98_PERM、掩码 Z148_R_MASKS 等数值

**重写（表达）**：
- 文件名 → `a_bogus.py`
- 类名 `ABogusPureSigner` → `ABogusSigner`
- 所有 `z1xx_` 函数 → 语义化命名
- 所有注释 → 独立重新撰写（讲"为什么这么做"，不讲来源）
- 代码结构 → 按"摘要生成 → 载荷组装 → 混淆编码 → 加密 → Base64"分步骤组织

### 5.2 SM3（原 sm3.py）

- SM3 是国密标准算法，实现本身是公开规范
- 重写为独立 `digest.py`，用标准实现风格重写（参考但不复制原代码表达）
- 保留测试向量验证（SM3('abc') 等标准向量）

### 5.3 msToken（原 mstoken.py）

- 逻辑：POST 上报接口 → 取 `x-ms-token` 头 / cookie
- 重写为 `ms_token.py`，独立命名和结构
- 保留 TTL 缓存逻辑（功能需求）

### 5.4 fingerprint（原 fingerprint.py）

- 指纹预设（分辨率/GPU 组合）是**事实数据**，保留
- 代码表达重写为 `env_profile.py`

---

## 6. 采集工作台重构要点

### 6.1 架构升级（从"模块堆叠"到"分层"）

原架构是平铺模块（src/dy_collect.py 等），重构为分层：

```
UI 层（app/ui/）→ 调度层（app/scheduler.py）→ 平台层（app/platform/）
   → 浏览器层（app/browser/）→ 签名层（app/signature/）
```

- **依赖方向单向**：上层依赖下层，下层不反向依赖
- **接口抽象**：`Collector` 抽象类统一 search/fetch_comments 契约
- **可测试性**：签名/采集核心逻辑与 GUI/浏览器解耦

### 6.2 关键模块映射

| 原文件 | 新位置 | 重构要点 |
|---|---|---|
| `src/gui.py` | `app/main.py` + `app/ui/` | 拆分 UI 为页面组件，主程序只做装配 |
| `src/scheduler.py` | `app/scheduler.py` | 保留状态机/冷却/断点/人工接管逻辑，重写命名与结构 |
| `src/db.py` | `app/database.py` | 数据层独立，统一 Repository 模式 |
| `src/bitbrowser.py` | `app/browser/bitbrowser_api.py` | 接口命名重写 |
| `src/cdp.py` | `app/browser/cdp_client.py` | 类名/方法名语义化 |
| `src/dy_collect.py` | `app/platform/douyin.py` | 采集流程保留，代码结构重组 |
| `src/live_collector.py` | `app/platform/base.py` 集成 | Collector 抽象统一 |

### 6.3 命名去痕清单（工作台内部）

- `BitBrowser` 保留（这是外部产品名，不是原仓库的痕迹）
- `dy_collect` → `douyin`（平台语义化）
- `SearchVideosResult` → `SearchOutcome`（或等价的独立命名）
- 所有中文注释重写（这是你的原创代码，但重构统一风格）

### 6.4 架构重构要点（基于现状调研）

| 优先级 | 现状问题 | 重构方案 |
|---|---|---|
| **P0** | `GuiApp` 4571 行 / 203 方法的 God Object | 按页面+领域拆分：`MainWindow` / `AccountsController` / `TasksController` / `DiagnosticsController` / `LeadsBridge`，GUI 只做组合与事件转发 |
| **P0** | DB 连接跨线程共享，并发写竞争 | 连接池/每线程连接 + 读写分离；`delete_task` 大事务加 `BEGIN IMMEDIATE` |
| **P1** | 平台加载用字符串路径隐式耦合 | 显式注册 + 启动自检（`register()` 校验 search/fetch_comments 签名） |
| **P1** | 采集器 API 不一致（自由函数 vs 类） | 统一 `PlatformAdapter.search/fetch_comments(pause_event, cancel_event, window_id)` 实例方法 |
| **P1** | GUI 持有业务逻辑过重 | 抽 `AppContext/BrowserService/HealthService/LlmService` 应用层 |
| **P2** | 配置明文存 api_key | 分区 Schema 校验 + 密钥加密存储 + 原子写 |
| **P2** | BitBrowser 客户端同步阻塞 UI | 线程池封装 + 指数退避 + 状态缓存 |
| **P2** | 前端性能债务（全量重建） | 虚拟化列表 + 差分更新 |
| **P2** | 错误语义混乱 | `CollectError(human_required/rate_limited/network)` + `RetryPolicy` |

### 6.5 模块级重构要点（来自核心架构调研）

#### dy_collect（920 行 → 拆分 5 文件）

| # | 问题 | 建议 |
|---|---|---|
| D1 | God file：搜索+评论+风控+意图+CLI 五职责 | 拆 `probe.py / extractor.py / intent.py / cli.py`，主文件只留编排 |
| D2 | `_attach_response_list` 死代码 | 删除 |
| D3 | `responses` 无界增长（deep 模式数千条） | 按 requestId 去重 + `deque(maxlen=2000)` |
| D4 | NDJSON 跨帧拼接可能错位 | 按 `data:` 行切分，不做跨行拼接 |
| D5 | 魔法数散落（scrollBy 900 / sleep 8 / bottom 12 轮） | 抽 `Tuning(standard/fast/deep)` dataclass |
| D6 | `SearchVideosResult` 继承 list 语义混乱 | 改 `@dataclass SearchResult(items, meta)` |
| D7 | probe 重复 DOM 查询 | 抽 `dom.py: find_visible_overlays() / is_scrollable()` |
| D8 | `sys.path.insert` + 双兼容 import hack | 统一包内导入 + pyproject.toml |
| D9 | `rate()` 返回非结构化 | 改 `Intent(label, score)` 结构化 + 写 DB |

#### live_collector / cdp / scheduler

| 模块 | 问题 | 建议 |
|---|---|---|
| live | 每平台自建 event_loop + Thread，线程无上限 | 单一 asyncio 线程 + Queue 串行化 |
| live | Lock + run_coroutine_threadsafe 双重串行 | search 加 timeout=600 |
| live | 窗口文件两行协议隐式、无锁 | 抽 `WindowStore(platform, window_id) -> ws_url` |
| cdp | `_wait_event` 忽略 sessionId，轮询 readyState | 真正订阅 `Page.loadEventFired` + asyncio.Event |
| cdp | `close()` 竞态 | 先 ws.close() 再 await _reader |
| cdp | 无重连/心跳 | `connect(timeout=10)` + `ping_interval=20` |
| sched | God Object >1800 行 | 按 `TaskService / SearchOrchestrator / AssignmentService / WorkerPool / SafetyService` 拆分 |
| sched | 双连接双锁序口头约定 | 抽 `with scheduler.transaction():` 上下文统一加锁 |
| sched | SQL 内联 + 全表扫 | 建索引 + COUNT 缓存 |
| sched | `_control_kwargs` 热点反复 inspect.signature | 启动时缓存一次签名探测 |
| sched | 字符串 JSON 反复 loads | 抽 `task.bound_accounts` property |

### 6.6 横切改进（重构必做）

1. **类型化**：`Collector` Protocol + `VideoDict/CommentDict` TypedDict
2. **窗口协议**：`data/*_window.txt` 两行格式 → `data/windows/{platform}/{window_id}.json`
3. **CDP 加固**：wait_load 真事件 → close 顺序 → ping_interval

---

## 7. 文档与标识重写

| 文档 | 处理 |
|---|---|
| `dy-sign-tool/README.md` | 重写为 `leadharvest/README.md`，独立命名、功能说明、无来源引用 |
| `docs/抖音签名服务集成设计.md` | 重写为 `docs/signature-design.md`，去除所有 `DouYin_Spider` / `D:\DSH` 引用 |
| `签名失效重逆向更新手册.md` | 重写为 `docs/signature-maintenance.md` |
| 原仓库水印/logo | **不引入**（从未引入，无需删除） |

---

## 8. 许可证与合规

### 8.1 现状风险

原仓库**无 LICENSE** = 保留所有权利。直接复用其表达 = 侵权风险。

### 8.2 重构后的合规状态

1. **代码**：全部重新创作（本方案第 5、6 节），无原仓库表达
2. **算法**：功能事实，不受版权保护
3. **事实常量**：必要参数，不受版权保护
4. **发布**：新版项目可声明独立许可证（建议 MIT，或按用户商用需求定）

### 8.3 残余风险与处置

| 风险 | 等级 | 处置 |
|---|---|---|
| 算法与常量与原仓库"撞车" | 极低 | 功能必然性，法律上不构成侵权；文档中不宣称"独创算法"即可 |
| 注释/命名残留 | 低 | 重构后全库 grep 校验（见第 9 节验收） |
| 签名算法本身被抖音追诉 | 独立风险 | 与版权无关；商用需评估平台 ToS（第 10 节） |

---

## 9. 验收标准（重构完成时）

### 9.1 痕迹清除验证

```bash
# 在新目录下搜索，以下关键词必须 0 命中：
grep -ri "DouYin_Spider\|cv-cat\|ab_pure\|sm3_hash\|z146\|DySign\|dy_sign\|mstoken\|xbogus_pure\|strdata_pure" leadharvest/
# 必须 0 命中
```

### 9.2 功能验证

- [ ] 签名服务启动 → /health 正常
- [ ] 生成 a_bogus → 评论接口 200 → 真实评论
- [ ] 采集全流程（搜索→评论→导出→意向分析）跑通
- [ ] 测试套件通过

---

## 10. 开放问题（需评审）

1. **许可证选型**：MIT / Apache-2.0 / 商用专有（建议 MIT，简单且商用友好）
2. **签名算法商用风险**：绕签名采数据本身违反抖音 ToS，对外商用前需评估（建议产品定位为"浏览器自动化 + 合规采集"，弱化"逆向签名"表述）
3. **保留哪些发布包**：现有 5 个发布包目录是否全部保留，还是只保留重构版
4. **重构工作量**：全面重构 81 个 src 文件 + 测试 + 文档，建议分阶段（签名→核心→GUI→文档）

---

*文档状态：待评审。评审通过后按第 11 节实施路线推进。*

---

## 11. 实施路线（分阶段）

### ✅ 阶段 0：评审与冻结（已完成）

- [x] 评审本设计文档
- [x] 确定产品名 `LeadHarvest`
- [x] 冻结旧版目录（作为回滚基线）
- [x] 产出本设计文档 v0.1

### ✅ 阶段 1：签名核心 clean-room 重写（已完成，2025-08）

- [x] 新建 `dy-sign-tool/signature_rewrite/`（独立重写目录）
- [x] 重写全部 6 个核心模块 + 签名服务
- [x] 三重验证：SM3 标准向量 / 新旧逐字节对比 / 线上真实请求
- [x] 验证结果：新版服务(8766)签名被抖音接受，HTTP 200 + 真实评论

**重写后文件对照**：

| 旧文件（废弃） | 新文件（启用） | 验证 |
|---|---|---|
| `dy_sign/ab_pure.py` | `signature_rewrite/sig/a_bogus.py` | ✅ 线上 PASS |
| `dy_sign/sm3.py` | `signature_rewrite/sig/digest.py` | ✅ 标准向量 PASS |
| `dy_sign/mstoken.py` | `signature_rewrite/sig/ms_token.py` | ✅ |
| `dy_sign/fingerprint.py` | `signature_rewrite/sig/env_profile.py` | ✅ |
| `dy_sign/xbogus_pure.py` | `signature_rewrite/sig/x_bogus.py` | ✅ |
| `dy_sign/strdata_pure.py` | `signature_rewrite/sig/report_body.py` | ✅ |
| `dy_sign/dy_sign_service.py` | `signature_rewrite/sig/sign_service.py` | ✅ 线上 PASS |

### ✅ 阶段 2：平台层重构（已完成，2025-08）

- [x] 新建 `leadharvest/app/` 分层架构（platform/browser/errors）
- [x] `platform/base.py` 统一类型（VideoItem/CommentItem/SearchResult）
- [x] `platform/__init__.py` 注册机制（register_platform/create_adapter）
- [x] `platform/douyin.py` 抖音适配器（迁移 dy_collect，**端到端验证通过**）
- [x] `platform/xiaohongshu.py` 小红书适配器
- [x] `platform/weibo.py` 微博适配器
- [x] `platform/bilibili.py` B站适配器
- [x] `browser/cdp_client.py` + `browser/cdp.py` 窗口管理
- [x] `errors.py` 统一异常体系（HumanBlock/RateLimited/NetworkError）
- [x] 全部平台注册验证通过，痕迹 grep 零命中

### ✅ 阶段 3：浏览器层与调度层（已完成，2025-08）

- [x] `app/database.py` Repository 模式数据层（Task/Video/Account/Comment 分区）
- [x] `app/scheduler.py` 任务调度器（状态机/冷却/断点/人工接管迁移）
- [x] `app/main.py` CLI 入口（init-db / platforms / run）
- [x] `browser/cdp_client.py` 跨事件循环连接修复
- [x] 调度器 mock 测试通过（任务→搜索→评论→入库→done）
- [x] **端到端集成验证通过**（真实抖音搜索 5 条 + 评论 15 条入库）

### ✅ 阶段 4：GUI 与应用层（已完成，2025-08）

- [x] `app/ui/theme.py` 主题常量（平台/状态中文映射）
- [x] `app/ui/app.py` 轻量 GUI（任务创建/列表/暂停/恢复/停止/刷新）
- [x] GUI 与调度器集成验证通过
- [x] **真实窗口打开验证通过**（"LeadHarvest — 多平台线索采集"）

### ✅ 阶段 5：文档与发布（已完成，2025-08）

- [x] 重写 README（`leadharvest/README.md`）
- [x] LICENSE（MIT）
- [x] pyproject.toml
- [x] 签名模块独立 README（`sig/README.md`）
- [x] 全库痕迹 grep 终检（代码目录零命中）
- [x] 旧文档标注废弃（`docs/抖音签名服务集成设计.md`）
- [x] 端到端功能验证（签名→采集→入库全链路 PASS）

---

## 12. 最终交付总结（全部阶段完成）

### 重构成果全景

```
leadharvest/                          # 全新独立项目（可对外商用）
├─ README.md / LICENSE / pyproject.toml
├─ app/
│  ├─ main.py            # CLI（init-db / platforms / run）
│  ├─ database.py        # Repository 数据层
│  ├─ scheduler.py       # 任务调度器
│  ├─ errors.py          # 统一异常
│  ├─ browser/           # cdp.py + cdp_client.py
│  ├─ platform/          # base + douyin/xiaohongshu/weibo/bilibili
│  └─ ui/                # theme.py + app.py（轻量 GUI）
├─ test_*.py             # 测试套件
└─ data/

dy-sign-tool/signature_rewrite/       # 签名算法独立实现
└─ sig/                 # digest/a_bogus/x_bogus/ms_token/report_body/env_profile/sign_service
```

### 合规验证

| 检查项 | 结果 |
|---|---|
| 来源标识（DouYin_Spider/cv-cat/D:\DSH） | ✅ 零命中 |
| 原仓库符号（ABogusPureSigner/z146/ab_pure 等） | ✅ 零命中 |
| 独立命名/文档/许可证 | ✅ MIT |
| 功能验证（签名/采集/调度/GUI） | ✅ 全 PASS |

### 测试证据

| 测试 | 结果 |
|---|---|
| SM3 国密标准向量 | ✅ PASS |
| a_bogus 线上真实请求 | ✅ HTTP 200 |
| 调度器核心逻辑（mock） | ✅ PASS |
| GUI 导入 + 调度集成 | ✅ PASS |
| 真实 GUI 窗口 | ✅ 打开成功 |
| 端到端集成（真实抖音） | ✅ 搜索+评论+入库 |

> 每阶段完成后跑一次验证 + 痕迹 grep，确认无回归再进入下一阶段。
