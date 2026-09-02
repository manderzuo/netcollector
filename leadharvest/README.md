# LeadHarvest — 多平台线索采集工作台

> 版本：v0.1.0（重构版）
> 定位：面向销售/运营团队的公开数据采集与分析工具，支持抖音、小红书、微博、B站四大平台。

**LeadHarvest** 是一个基于真实浏览器（BitBrowser）驱动的多平台线索采集桌面工具：
通过 CDP 控制真实浏览器访问目标平台，采集公开的作品与评论数据，再按意向规则自动打分，
生成可用的线索客户清单。

---

## ✨ 核心能力

| 能力 | 说明 |
|---|---|
| 多平台采集 | 抖音 / 小红书 / 微博 / B站 统一适配器架构 |
| 真实浏览器 | BitBrowser + CDP，签名由浏览器自动生成，稳定低风控 |
| 两阶段任务 | 阶段 A 搜索作品 → 阶段 B 分账号采集评论 |
| 意向分析 | 评论按关键词规则打高/中/低意向分 |
| 冷却与风控 | 每账号批次冷却、验证码人工接管、断点续跑 |
| 统一导出 | 作品 / 评论 / 意向客户 / 用户聚合 CSV |

---

## 🚀 快速开始

### 环境要求

- Windows 10+ / Python 3.11
- BitBrowser（比特浏览器）本地服务（默认 `http://127.0.0.1:54345`）
- 已登录目标平台的 BitBrowser 窗口（登录态存在 profile 里）

### 安装

```bash
pip install websockets
```

### 运行

```bash
# 1. 初始化数据库
python -m app.main init-db

# 2. 查看已注册平台
python -m app.main platforms

# 3. 运行采集任务
python -m app.main run --platform douyin --keyword "快递柜" --target 20
```

### 平台适配器架构

```python
from app.platform import create_adapter

# 统一创建任意平台适配器
adapter = create_adapter("douyin")       # douyin / xhs / weibo / bilibili

# 统一接口：搜索 + 评论
result = adapter.search("快递柜", target_count=20)
comments = adapter.fetch_comments(result.items[0].vid, result.items[0].url)
```

---

## 🏗 项目结构

```
leadharvest/
├─ app/
│  ├─ main.py           # CLI 入口
│  ├─ database.py       # SQLite 数据层（Repository 模式）
│  ├─ scheduler.py      # 任务调度器（状态机/冷却/人工接管）
│  ├─ errors.py         # 统一异常体系
│  ├─ browser/          # 浏览器控制层
│  │  ├─ cdp.py         # Chrome DevTools Protocol 客户端
│  │  └─ cdp_client.py  # 窗口会话管理
│  └─ platform/         # 平台采集层
│     ├─ base.py        # 统一类型（VideoItem/CommentItem/SearchResult）
│     ├─ douyin.py      # 抖音
│     ├─ xiaohongshu.py # 小红书
│     ├─ weibo.py       # 微博
│     └─ bilibili.py    # B站
├─ test_*.py            # 测试（调度器/集成/适配器）
└─ data/                # 运行时数据（数据库/窗口缓存）
```

---

## 🧠 设计原则

1. **分层单向依赖**：`ui → scheduler → platform → browser`，下层不反向依赖上层
2. **平台可插拔**：新增平台只需实现 `PlatformAdapter` 并注册
3. **类型化数据**：`VideoItem` / `CommentItem` / `SearchResult` 统一契约
4. **浏览器解耦**：平台逻辑不直接持有浏览器连接，通过 `CdpClient` 注入
5. **断点续跑**：任务/账号状态以数据库为唯一真源，断电可恢复

---

## 🔒 合规与免责

- 本工具仅采集**公开数据**，不进行任何登录态之外的写操作（不发布、不点赞、不私信）
- 请遵守目标平台用户协议与当地法律法规
- 高频采集可能导致账号/IP 被风控，请合理控制频率（冷却参数可配置）
- 本工具用于学习与研究，使用者需自行承担使用责任

---

## 📄 许可证

MIT License — 详见 [LICENSE](LICENSE)。

---

*LeadHarvest — 由独立代码库重构而来，不包含任何第三方开源项目代码。*
