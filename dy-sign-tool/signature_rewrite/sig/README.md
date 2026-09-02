# sig — 平台签名算法模块

独立实现的平台签名算法库,用于构造目标平台接口请求所需的签名参数。

## 模块清单

| 模块 | 功能 |
|---|---|
| `digest.py` | 国密 SM3 摘要（GB/T 32905-2016） |
| `a_bogus.py` | a_bogus 签名生成 |
| `x_bogus.py` | X-Bogus 签名生成（备用通道） |
| `ms_token.py` | msToken 动态获取（带缓存） |
| `report_body.py` | 安全 SDK 上报体构造 |
| `env_profile.py` | 浏览器环境指纹档案 |
| `sign_service.py` | HTTP 签名服务 |

## 快速使用

```python
from sig.a_bogus import ABogusSigner

signer = ABogusSigner()
signature = signer.sign("https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id=123&cursor=0&count=20")
# → 返回 a_bogus 签名字符串
```

## HTTP 服务

```bash
# 启动签名服务（默认 127.0.0.1:8766）
python -m sig.sign_service --port 8766

# 健康检查
curl "http://127.0.0.1:8766/health"

# 生成签名
curl "http://127.0.0.1:8766/sign?url=https%3A%2F%2F..."
```

## 验证

```bash
python smoke_all.py
# → SM3 标准向量 PASS / a_bogus 生成 OK / X-Bogus 生成 OK
```

## 说明

- 纯 Python 实现，仅 `ms_token.py` 依赖 `requests`
- SM3 实现已通过国密标准测试向量验证
- a_bogus 已通过线上接口真实请求验证（HTTP 200）
- 算法参数会随平台升级变化，失效时需重新提取参数更新
