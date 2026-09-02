# dy-sign-tool — 抖音 a_bogus 签名工具

> 独立自包含工具，不依赖「多平台采集工作台」。可整体拷贝到任何机器运行。

## 它能做什么

1. **生成 a_bogus 签名**：纯 Python 实现（SM3 + RC4 变体 + 自定义 Base64），无需浏览器、无需补环境。
2. **获取 msToken**：调用 `mssdk.bytedance.com` 上报获取（带 600s 缓存）。
3. **直连抖音接口**：带签名 + 真实 cookie 请求 `/aweme/v1/web/comment/list/` 等接口，验证签名有效性。
4. **POC 验证**：从 BitBrowser 窗口取真实 cookie → 签名 → 请求评论接口 → 判断签名是否被接受。

## 目录结构

```
dy-sign-tool/
├─ dy_sign/                # 签名核心包
│  ├─ ab_pure.py           # a_bogus 纯 Python 实现
│  ├─ sm3.py               # SM3 国密哈希
│  ├─ mstoken.py           # msToken（requests）
│  ├─ fingerprint.py       # 浏览器指纹档案
│  ├─ xbogus_pure.py       # X-Bogus 备用
│  ├─ strdata_pure.py      # strData 上报体
│  └─ dy_sign_service.py   # HTTP 签名服务（/sign /mstoken /sign_full /health）
├─ cdp.py                  # CDP 客户端（取 cookie 用）
├─ bitbrowser.py           # BitBrowser API 客户端（打开窗口用）
├─ poc_dy_sign.py          # POC 验证脚本
├─ update_sign.py          # 签名失效检测 + 常量提取比对（维护工具）
├─ run_probe_real.py       # 用 BitBrowser 真实 cookie 跑失效探测
├─ get_real_vids.py        # 从抖音页提取真实视频 ID
├─ inspect_dy_page.py      # 检查页面状态
└─ README.md
```

## 快速开始

### 1. 启动签名服务

```bash
cd dy-sign-tool
python -m dy_sign.dy_sign_service --port 8765
```

### 2. 健康检查

```bash
curl "http://127.0.0.1:8765/health"
# → {"ok":true,"algorithm":"ab_pure","bdms_version":"1.0.1.19-fix.01",...}
```

### 3. 生成签名

```bash
curl "http://127.0.0.1:8765/sign?url=https%3A%2F%2Fwww.douyin.com%2Faweme%2Fv1%2Fweb%2Fcomment%2Flist%2F%3Faweme_id%3D123%26cursor%3D0%26count%3D20%26item_type%3D0%26device_platform%3Dwebapp%26aid%3D6383"
# → {"a_bogus":"df0bgq6idxW5cdMSuObNSHnlrHnMNkWyizJ/..."}
```

### 4. POC 验证（需要 BitBrowser + 登录态抖音窗口）

```bash
python poc_dy_sign.py --window <BitBrowser窗口ID> --sign http://127.0.0.1:8765
```

流程：打开 BitBrowser 抖音窗口 → CDP 取真实 cookie → 签名 → 请求评论接口 → 输出结果。

```
[OK] 签名有效! 返回 N 条评论, has_more=...
```

## 接口参数参考

**评论列表**（已验证通过）：
```
GET /aweme/v1/web/comment/list/
  aweme_id=<视频ID>
  cursor=0
  count=20
  item_type=0
  device_platform=webapp
  aid=6383
  a_bogus=<签名>
  Cookie: <真实登录cookie>
```

**二级评论**：
```
GET /aweme/v1/web/comment/list/reply/
  item_id=<aweme_id>
  comment_id=<一级评论id>
  cursor / count / a_bogus
```

## 签名维护（失效时用）

```bash
# 1. 检测当前签名是否仍有效（用 BitBrowser 真实 cookie）
python run_probe_real.py <BitBrowser ws地址> <真实视频ID>
# → VERDICT: PASS = 有效；FAIL = 需要更新

# 2. 提取新版 webmssdk.js 的常量并比对
python update_sign.py --extract --js webmssdk_new.js

# 3. 完整流程见《签名失效重逆向更新手册.md》
```

## ⚠️ 重要说明

1. **签名有保质期**：本工具是抖音 a_bogus 算法的「逆向快照」。抖音升级算法后（历史上约数月一次），纯 Python 实现会失效，需重新逆向更新 `ab_pure.py`。
2. **cookie 必需**：评论接口需要真实登录 cookie，否则即使签名正确也会 403/空数据。
3. **风控风险**：纯 API 高频请求有封号/IP 风险。建议控制频率、使用代理、仅采集公开数据自用。
4. **合规**：仅限学习与技术研究，请遵守抖音用户协议与当地法律。

## 已验证（2025-08）

- ✅ a_bogus 生成（192 字符）
- ✅ 评论接口直连 + 真实 cookie → HTTP 200，返回真实评论
- ⚠️ 搜索接口（`general/search/stream`）未验证（风控更严）
- ⚠️ msToken 未验证（评论接口暂不需要）
