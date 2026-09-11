# 多平台采集工作台 V2.2.2

一个通过 **比特浏览器（BitBrowser）** 管理多账号、采集抖音/小红书视频与评论、做意向分析的桌面工具（Python + tkinter，零第三方依赖）。

## 快速开始

```bash
# 1. 启动 V2.2.2 GUI（真实采集模式，连 BitBrowser）
cd D:\gpt\douyinxiaohongshu
python src\ui2\default_app.py

# 2. 若只演示 V2.2.2 界面流程（不连 BitBrowser）
python src\ui2\default_app.py --demo

# 3. 旧版 Tk GUI（回退排查入口）
python src\gui.py

# 4. V2.2.2 独立后台服务（仅后台调试；不要与 GUI 同时管理同一数据库）
python src\backend_app.py
```

运行环境：Windows + Python 3.11。GUI/数据库使用标准库；CDP 浏览器控制需要安装 `websockets`：

```bash
pip install websockets
```

> 运行前确认：BitBrowser 本地 API 服务已开启（默认 http://127.0.0.1:54345），并已有至少一个账号 profile 窗口。

首次注册的员工申请会提交到统一账号服务 `https://www.gemstory.cn`，管理员在个人中心的用户审批中处理；员工本机的账号绑定、浏览器登录态、采集数据和 LLM 配置不会因为注册或升级被覆盖。若统一服务暂时不可用，已有本地账号仍可按本地模式登录。

## 使用流程（三步）

见《交接文档.md》第 3 章「使用方法」。

## 目录结构

| 路径 | 说明 |
|---|---|
| `src\ui2\default_app.py` | V2.2.2 默认桌面入口（QML 界面 + 本地后台服务） |
| `src\ui2\qml\main.qml` | V2.2.2 QML 工作台界面 |
| `src\gui.py` | 旧版 Tk 回退入口（账号管理/任务管理/数据导出） |
| `src\scheduler.py` | 任务调度器（状态机/均分/冷却/断点/人工接管） |
| `src\backend_app.py` | V2.2.2 独立后台服务启动入口（本机回环接口） |
| `src\db.py` | SQLite 数据层 |
| `src\bitbrowser.py` | BitBrowser 本地 API 客户端封装 |
| `src\cdp.py` | Chrome DevTools Protocol 客户端 |
| `src\live_collector.py` | 真实采集器（抖音/小红书），实现 Collector 接口 |
| `src\dy_collect.py` | 抖音采集算法（搜索 + 评论 + 半年时间过滤 + 地区） |
| `src\xhs_collect3.py` | 小红书采集算法（搜索 + 评论 + 地区 + 评论者主页） |
| `src\account_reader.py` | 从已登录页面读取账号昵称/ID |
| `src\export_report.py` | 统一导出 4 类 CSV（作品/评论/意向/用户聚合） |
| `src\gen_csv.py` | 意向客户 CSV（8 列：信息提取时间/地区/用户名/主页/发布时间/内容/评级/分析） |
| `src\intent_rules.json` | 意向规则（高/中/低 + 招聘） |
| `src\restore_bindings.py` | 批量恢复账号绑定（扫描窗口登录态） |
| `docs\p1-contract.md` | P1 骨架接口契约 |
| `多账号采集平台-设计文档.md` | 完整设计文档 |
| `交接文档.md` | 完整交接说明（建议先读） |

## 关键配置

- 账号绑定信息存于 `data\platform_gui.db`（SQLite，预设 `platform_gui.db`）
- 窗口 ws 文件：`data\dy_window.txt`、`data\xhs_window.txt`（运行时写入）
- 意向规则：`src\intent_rules.json`（可编辑）

## 常见问题

- **账号未绑定显示**：账号管理页需「读取并绑定」后才会显示昵称并可用于采集
- **窗口打开是多界面**：每个窗口默认单界面（代码里已用 `ignore_default_urls` 避免叠加多 URL）
- **登录失效**：检测到登录弹窗/验证码，GUI 会提示人工介入（P7），不会自动操作
