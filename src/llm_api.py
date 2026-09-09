# -*- coding: utf-8 -*-
"""智能回复/评论分析 API 的只读连通性检测。

当前检测约定 OpenAI 兼容 API：在配置的 API 地址后请求 ``/models``。
检测只验证网络、鉴权和服务响应，不调用模型生成内容，也不会把 API Key
写入返回值或日志。
"""

from __future__ import annotations

import json
import re
import socket
import time
import unicodedata
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit


_IMAGE_PLACEHOLDER_RE = re.compile(
    r"^(?:[\[【(（]\s*)?(?:图片|图像|照片|相片|动图|表情包|image|img|photo|picture|sticker|gif)"
    r"(?:\s*[\]】)）])?$",
    flags=re.IGNORECASE,
)
_BRACKET_TOKEN_RE = re.compile(r"^[\[【(（][^\]】)）]{1,24}[\]】)）](?:\s*[\[【(（][^\]】)）]{1,24}[\]】)）])*$")
_PURE_NUMBER_RE = re.compile(r"^[\d０-９\s.,，、:+\-_/\\]+$")
_IMAGE_TYPES = {"image", "img", "photo", "picture", "sticker", "emoji", "gif", "图片", "图像", "照片", "表情包"}


def _comment_extra(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _is_emoji_only(text: str) -> bool:
    """判断是否只有 emoji/肤色修饰符/连接符，没有可供分析的文字。"""
    meaningful = False
    for char in str(text or ""):
        if char.isspace() or char in ("\ufe0f", "\u200d"):
            continue
        category = unicodedata.category(char)
        if category in {"So", "Sk", "Mn", "Me", "Cf"}:
            meaningful = True
            continue
        return False
    return meaningful


def is_llm_eligible_comment(item: dict) -> bool:
    """判断评论是否值得发送给 LLM 做意向分析。

    空内容、图片/表情占位、纯表情和纯数字不进入远程请求；包含实际文字的
    评论即使带有表情或图片标记，仍然保留给模型分析。
    """
    if not isinstance(item, dict):
        return False
    text = str(item.get("content") or "").strip()
    if not text:
        return False
    if _PURE_NUMBER_RE.fullmatch(text):
        return False
    if _IMAGE_PLACEHOLDER_RE.fullmatch(text) or _BRACKET_TOKEN_RE.fullmatch(text):
        return False
    if _is_emoji_only(text):
        return False

    extra = _comment_extra(item.get("extra"))
    content_type = str(
        extra.get("content_type") or extra.get("comment_type") or extra.get("type") or ""
    ).strip().lower()
    explicit_image_only = any(
        bool(extra.get(key)) for key in ("is_image", "image_only", "sticker_only", "emoji_only")
    )
    if (content_type in _IMAGE_TYPES or explicit_image_only) and not re.search(r"[\u3400-\u9fffA-Za-z]", text):
        return False
    return True


class LLMApiClient:
    """提供智能 API 的只读连通性检测。"""

    @staticmethod
    def is_configured(config: dict, *, require_key: bool = True) -> bool:
        """判断发布/分析功能是否具备完整的连接配置。

        连通性检测允许无密钥的本地兼容服务，但发布工作区的“智能 API
        已启用”状态必须和实际调用条件一致；否则界面会显示已启用，调用
        时却只能无鉴权请求并静默回退本地模板。
        """
        config = config if isinstance(config, dict) else {}
        base_url = str(config.get("base_url") or "").strip()
        model = str(config.get("model") or "").strip()
        api_key = str(config.get("api_key") or "").strip()
        return bool(base_url and model and (api_key or not require_key))

    @staticmethod
    def _thinking_payload(config: dict) -> dict:
        """按服务商/模型选择关闭或分离思维链的兼容参数。"""
        config = config if isinstance(config, dict) else {}
        provider = str(config.get("provider") or "").lower()
        model = str(config.get("model") or "").lower()
        if "deepseek" in provider or "deepseek" in model:
            if "v4" in model:
                return {"thinking": {"type": "disabled"}}
        if "minimax" in provider or model.startswith("minimax-"):
            return {"reasoning_split": True}
        return {"enable_thinking": False}

    @staticmethod
    def _models_url(base_url: str) -> str:
        value = str(base_url or "").strip().rstrip("/")
        parsed = urlsplit(value)
        path = parsed.path.rstrip("/")
        for suffix in ("/chat/completions", "/completions"):
            if path.lower().endswith(suffix):
                path = path[:-len(suffix)].rstrip("/")
                break
        if not path.lower().endswith("/models"):
            path = f"{path}/models" if path else "/models"
        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))

    @staticmethod
    def _chat_url(base_url: str) -> str:
        value = str(base_url or "").strip().rstrip("/")
        parsed = urlsplit(value)
        path = parsed.path.rstrip("/")
        if path.lower().endswith("/chat/completions"):
            return value
        if path.lower().endswith("/models"):
            path = path[:-len("/models")].rstrip("/")
        path = f"{path}/chat/completions" if path else "/chat/completions"
        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))

    @staticmethod
    def _error_result(status: str, detail: str, started: float, **extra) -> dict:
        result = {
            "healthy": False,
            "status": status,
            "detail": detail,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }
        result.update(extra)
        return result

    @classmethod
    def check_connection(cls, config: dict) -> dict:
        """检测配置的 OpenAI 兼容 API，返回可直接展示给用户的结果。"""
        started = time.perf_counter()
        config = config if isinstance(config, dict) else {}
        base_url = str(config.get("base_url") or "").strip()
        api_key = str(config.get("api_key") or "")
        model = str(config.get("model") or "").strip()
        timeout = max(1.0, min(60.0, float(config.get("timeout") or 15)))
        if not base_url:
            return cls._error_result("invalid", "请先填写 API 地址", started)
        parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return cls._error_result("invalid", "API 地址格式不正确，应以 http:// 或 https:// 开头", started)

        endpoint = cls._models_url(base_url)
        headers = {"Accept": "application/json", "User-Agent": "DouyinXhsClient/API-Check"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(endpoint, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(512 * 1024).decode("utf-8", errors="replace")
                code = int(getattr(response, "status", response.getcode()) or 0)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                detail = "API 地址可访问，但鉴权失败，请检查 API Key"
                status = "auth_failed"
            elif exc.code in (404, 405):
                detail = "API 地址可访问，但未找到兼容的 /models 接口，请检查 API 地址"
                status = "endpoint_missing"
            else:
                detail = f"API 服务返回 HTTP {exc.code}"
                status = "http_error"
            return cls._error_result(status, detail, started, endpoint=endpoint)
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                return cls._error_result(
                    "timeout", f"API 响应超时（{timeout:g} 秒）", started,
                    endpoint=endpoint, timeout_seconds=timeout,
                )
            return cls._error_result(
                "unreachable", f"无法连接 API 服务：{type(exc).__name__}", started,
                endpoint=endpoint,
            )
        except (socket.timeout, TimeoutError) as exc:
            return cls._error_result(
                "timeout", f"API 响应超时（{timeout:g} 秒）", started,
                endpoint=endpoint, timeout_seconds=timeout,
            )
        except OSError as exc:
            return cls._error_result(
                "unreachable", f"无法连接 API 服务：{type(exc).__name__}", started,
                endpoint=endpoint,
            )

        if code < 200 or code >= 300:
            return cls._error_result("http_error", f"API 服务返回 HTTP {code}", started, endpoint=endpoint)
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return cls._error_result(
                "invalid_response", "API 地址已响应，但返回内容不是 JSON", started,
                endpoint=endpoint,
            )

        available_models = []
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            available_models = [
                str(item.get("id")) for item in payload["data"]
                if isinstance(item, dict) and item.get("id")
            ]
        model_note = ""
        if model and available_models and model not in available_models:
            model_note = "；当前模型未出现在服务返回的模型列表中"
        if model_note:
            return {
                "healthy": False,
                "status": "model_missing",
                "detail": f"API 服务可访问，但模型不可用：{model}{model_note}",
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "endpoint": endpoint,
                "model_count": len(available_models),
                "model_available": False,
            }
        return {
            "healthy": True,
            "status": "ok",
            "detail": f"API 服务连通，兼容模型接口响应正常{model_note}",
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "endpoint": endpoint,
            "model_count": len(available_models),
            "model_available": (model in available_models) if model and available_models else None,
        }

    @classmethod
    def _chat(cls, config: dict, messages: list[dict], *, max_tokens: int = 800,
              extra_payload: dict | None = None, stream: bool = False,
              progress_callback=None, progress_total: int = 0) -> dict:
        """执行一次受控的文本请求，返回脱敏结果。"""
        started = time.perf_counter()
        config = config if isinstance(config, dict) else {}
        base_url = str(config.get("base_url") or "").strip()
        api_key = str(config.get("api_key") or "")
        model = str(config.get("model") or "").strip()
        timeout = max(1.0, min(300.0, float(config.get("timeout") or 60)))
        if not base_url or not model:
            return cls._error_result("invalid", "请填写 API 地址和模型名称", started)
        parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return cls._error_result("invalid", "API 地址格式不正确", started)
        endpoint = cls._chat_url(base_url)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "DouyinXhsClient/API-Test",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload_data = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max(100, min(20000, int(max_tokens))),
            "stream": bool(stream),
        }
        selected_extra = extra_payload if isinstance(extra_payload, dict) else cls._thinking_payload(config)
        payload_data.update(selected_extra)
        payload = json.dumps(payload_data, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(endpoint, data=payload, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                code = int(getattr(response, "status", response.getcode()) or 0)
                if stream and hasattr(response, "__iter__"):
                    text, reasoning = cls._read_stream(
                        response,
                        progress_callback=progress_callback,
                        progress_total=progress_total,
                    )
                    response_data = None
                else:
                    raw = response.read(1024 * 1024).decode("utf-8", errors="replace")
                    response_data = json.loads(raw) if raw.strip() else {}
                    text = cls._extract_text(response_data)
                    reasoning = cls._extract_reasoning(response_data)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                detail = "API 鉴权失败，请检查 API Key"
                status = "auth_failed"
            elif exc.code in (404, 405):
                detail = "未找到兼容的聊天接口，请检查 API 地址"
                status = "endpoint_missing"
            else:
                detail = f"API 服务返回 HTTP {exc.code}"
                status = "http_error"
            return cls._error_result(status, detail, started, endpoint=endpoint)
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                return cls._error_result(
                    "timeout", f"API 响应超时（{timeout:g} 秒）", started,
                    endpoint=endpoint, timeout_seconds=timeout,
                )
            return cls._error_result(
                "unreachable", f"无法连接 API 服务：{type(exc).__name__}", started,
                endpoint=endpoint,
            )
        except (socket.timeout, TimeoutError) as exc:
            return cls._error_result(
                "timeout", f"API 响应超时（{timeout:g} 秒）", started,
                endpoint=endpoint, timeout_seconds=timeout,
            )
        except OSError as exc:
            return cls._error_result(
                "unreachable", f"无法连接 API 服务：{type(exc).__name__}", started,
                endpoint=endpoint,
            )
        if code < 200 or code >= 300:
            return cls._error_result("http_error", f"API 服务返回 HTTP {code}", started, endpoint=endpoint)
        if not text:
            return cls._error_result("invalid_response", "API 返回中没有找到模型文本内容", started, endpoint=endpoint)
        return {
            "healthy": True,
            "status": "ok",
            "detail": "API 已返回模型结果",
            "text": text,
            "raw_text": text,
            "reasoning_text": reasoning,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "endpoint": endpoint,
        }

    @staticmethod
    def _extract_text(payload) -> str:
        if not isinstance(payload, dict):
            return ""
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = message.get("content", first.get("text", ""))
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            return "".join(parts).strip()
        return ""

    @staticmethod
    def _extract_reasoning(payload) -> str:
        if not isinstance(payload, dict):
            return ""
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else first
        value = message.get("reasoning_content") or message.get("reasoning") or ""
        return str(value).strip() if value else ""

    @staticmethod
    def _read_stream(response, *, progress_callback=None, progress_total: int = 0) -> tuple[str, str]:
        """读取 OpenAI 兼容 SSE 流，分别拼接最终文本和思维文本。"""
        text_parts = []
        reasoning_parts = []
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
            line = line.strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = payload.get("choices") if isinstance(payload, dict) else None
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else choice.get("message", {})
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(content, str):
                text_parts.append(content)
            if isinstance(reasoning, str):
                reasoning_parts.append(reasoning)
            if callable(progress_callback):
                # 模型通常会分段返回 JSON；用已收到的“编号”数量推进，
                # 不用计时器伪造进度，避免在模型卡住时显示虚假完成度。
                try:
                    received = len(re.findall(r'"(?:编号|id)"\s*:', "".join(text_parts)))
                    total = max(0, int(progress_total or 0))
                    progress_callback(min(received, total) if total else received,
                                      total, "正在接收分析结果")
                except Exception:
                    pass
        return "".join(text_parts).strip(), "".join(reasoning_parts).strip()

    @staticmethod
    def _split_thinking(text: str) -> tuple[str, str]:
        """分离模型思维链和最终文本，避免思维链进入正式回复框。"""
        raw = str(text or "").strip()
        if not raw:
            return "", ""
        thinking_parts = []

        def closed(match):
            thinking_parts.append(match.group(1).strip())
            return ""

        cleaned = re.sub(
            r"<\s*think\s*>(.*?)<\s*/\s*think\s*>",
            closed,
            raw,
            flags=re.IGNORECASE | re.DOTALL,
        )
        open_match = re.search(r"<\s*think\s*>", cleaned, flags=re.IGNORECASE)
        if open_match:
            thinking_parts.append(cleaned[open_match.end():].strip())
            cleaned = cleaned[:open_match.start()]
        cleaned = re.sub(r"<\|(?:begin|end)_of_thought\|>", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"<\|(?:thinking|think)\|>.*?<\|end\|>", "", cleaned,
                         flags=re.IGNORECASE | re.DOTALL)
        final_text = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        thinking = "\n\n".join(part for part in thinking_parts if part).strip()
        return thinking, final_text

    @classmethod
    def analyze_comment(cls, config: dict, sample: dict) -> dict:
        sample = sample if isinstance(sample, dict) else {}
        nickname = str(sample.get("nickname") or "未知用户")
        platform = str(sample.get("platform_label") or sample.get("platform") or "未知平台")
        content = str(sample.get("content") or "").strip()
        if not content:
            return {"healthy": False, "status": "invalid", "detail": "测试评论内容为空"}
        messages = [
            {"role": "system", "content": (
                "你是评论线索分析助手。请用中文分析评论，输出：意向等级（高/中/低）、"
                "用户需求、判断依据、建议跟进方向。不要编造评论中没有的信息。"
            )},
            {"role": "user", "content": f"平台：{platform}\n昵称：{nickname}\n评论原文：{content}"},
        ]
        result = cls._chat(config, messages)
        if result.get("healthy") and result.get("text"):
            thinking, final_text = cls._split_thinking(result["text"])
            result["thinking"] = thinking
            result["final_text"] = final_text
        return result

    @classmethod
    def analyze_comments_batch(cls, config: dict, comments: list[dict], batch_size: int = 500,
                               progress_callback=None) -> dict:
        """一次提交一批评论进行意向分析；单批最多 500 条。"""
        rows = [item for item in (comments or []) if is_llm_eligible_comment(item)]
        if not rows:
            return {"healthy": False, "status": "invalid", "detail": "没有可分析的评论"}
        if len(rows) > int(batch_size):
            return {"healthy": False, "status": "invalid", "detail": f"单批最多分析 {int(batch_size)} 条评论"}
        payload_rows = []
        for index, item in enumerate(rows, start=1):
            payload_rows.append({
                "编号": index,
                "平台": item.get("platform_label") or item.get("platform") or "未知平台",
                "昵称": item.get("nickname") or "未知用户",
                "评论": str(item.get("content") or "").strip(),
            })
        if callable(progress_callback):
            try:
                progress_callback(0, len(rows), "正在提交批量分析请求")
            except Exception:
                pass
        messages = [
            {"role": "system", "content": (
                "你是批量评论意向分析助手。请逐条分析输入的评论，必须覆盖每个编号，"
                "只输出 JSON 数组，不要输出 Markdown、思维链或其它说明。为减少输出，"
                "每项只能包含两个字段：{\"编号\":1,\"意向\":\"高/中/低\"}。"
                "JSON 尽量紧凑，不要添加解释、空数组外的其它内容，也不要遗漏任何编号。"
                "无法判断时意向填低，不能遗漏或合并评论。"
            )},
            {"role": "user", "content": json.dumps(payload_rows, ensure_ascii=False)},
        ]
        # 批量返回需要更长的服务端处理时间；不沿用单条测试的 60 秒上限。
        batch_config = dict(config or {})
        try:
            batch_config["timeout"] = max(300.0, float(batch_config.get("timeout") or 0))
        except (TypeError, ValueError):
            batch_config["timeout"] = 300.0
        # 500 条结果即使只保留两个字段，也可能超过 8000 tokens；
        # 预留足够输出空间，避免模型输出到一半被服务端截断。
        batch_max_tokens = min(20000, max(12000, len(rows) * 28))
        result = cls._chat(
            batch_config,
            messages,
            max_tokens=batch_max_tokens,
            # 对支持推理开关的 OpenAI 兼容服务关闭思维链，避免 500 条任务耗尽输出额度。
            extra_payload=cls._thinking_payload(batch_config),
            stream=True,
            progress_callback=progress_callback,
            progress_total=len(rows),
        )
        # 少数兼容服务不接受 enable_thinking 字段；400 时兼容回退一次，
        # 后续仍通过原始文本兜底提取 JSON。
        if result.get("status") == "http_error" and "HTTP 400" in str(result.get("detail") or ""):
            result = cls._chat(
                batch_config, messages, max_tokens=batch_max_tokens,
                extra_payload={}, stream=True,
                progress_callback=progress_callback,
                progress_total=len(rows),
            )
        if not result.get("healthy"):
            result["requested_count"] = len(rows)
            if result.get("status") == "timeout":
                result["detail"] = f"批量分析等待模型响应超时（{len(rows)} 条，300 秒）"
            return result
        thinking, final_text = cls._split_thinking(result.get("text") or "")
        parsed = cls._parse_batch_json(final_text)
        if parsed is None:
            # 模型可能把 JSON 放在未闭合的思维链后面，直接从原始文本再提取一次。
            parsed = cls._parse_batch_json(result.get("text") or "")
        raw_text = result.get("text") or ""
        parsed_count = len(parsed or [])
        validated = cls._validate_batch_items(parsed, len(rows))
        if validated is None:
            id_count = len(re.findall(r'"(?:编号|id)"\s*:', raw_text, flags=re.IGNORECASE))
            parsed_count = max(parsed_count, id_count)
            return {
                "healthy": False,
                "status": "invalid_response",
                "detail": (
                    f"API 已响应，但批量 JSON 不完整或缺少编号："
                    f"期望 {len(rows)} 条，当前最多识别到 {parsed_count} 条；"
                    "可能被模型输出上限截断"
                ),
                "raw_text": raw_text,
                "raw_preview": raw_text[:4000],
                "parsed_count": parsed_count,
                "thinking": thinking,
                "text": final_text,
                "elapsed_ms": result.get("elapsed_ms"),
            }
        parsed = validated
        if callable(progress_callback):
            try:
                progress_callback(len(parsed), len(rows), "批量分析完成")
            except Exception:
                pass
        return {
            "healthy": True,
            "status": "ok",
            "detail": f"已完成 {len(rows)} 条评论的批量意向分析",
            "items": parsed,
            "requested_count": len(rows),
            "returned_count": len(parsed),
            "raw_text": result.get("text") or "",
            "thinking": thinking,
            "text": final_text,
            "elapsed_ms": result.get("elapsed_ms"),
        }

    @staticmethod
    def _parse_batch_json(text: str):
        candidate = str(text or "").strip()
        candidate = candidate.lstrip("\ufeff")
        candidate = re.sub(r"```(?:json)?", "", candidate, flags=re.IGNORECASE).replace("```", "").strip()
        data = None
        for possible in (candidate,):
            try:
                data = json.loads(possible)
                break
            except json.JSONDecodeError:
                pass
        if data is None:
            start, end = candidate.find("["), candidate.rfind("]")
            if start >= 0 and end > start:
                try:
                    data = json.loads(candidate[start:end + 1])
                except json.JSONDecodeError:
                    data = None
        if data is None:
            # 兼容模型按 JSONL 逐行返回对象的情况。
            lines = [line.strip().rstrip(",") for line in candidate.splitlines() if line.strip()]
            objects = []
            for line in lines:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    objects = []
                    break
                if isinstance(item, dict):
                    objects.append(item)
            if objects:
                data = objects
        if data is None:
            return None
        if isinstance(data, dict):
            data = data.get("results") or data.get("items") or data.get("data")
        if not isinstance(data, list):
            return None
        return [item for item in data if isinstance(item, dict)]

    @staticmethod
    def _validate_batch_items(items, expected_count: int):
        """校验批量结果覆盖全部编号，拒绝半截 JSON 或重复/缺失编号。"""
        if not isinstance(items, list) or len(items) != int(expected_count):
            return None
        by_id = {}
        for item in items:
            if not isinstance(item, dict):
                return None
            raw_id = item.get("编号", item.get("id"))
            try:
                item_id = int(raw_id)
            except (TypeError, ValueError):
                return None
            intent = str(item.get("意向", item.get("intent", ""))).strip()
            if item_id < 1 or item_id > int(expected_count) or intent not in {"高", "中", "低"}:
                return None
            if item_id in by_id:
                return None
            by_id[item_id] = item
        if set(by_id) != set(range(1, int(expected_count) + 1)):
            return None
        return [by_id[item_id] for item_id in range(1, int(expected_count) + 1)]

    @classmethod
    def generate_reply(cls, config: dict, sample: dict) -> dict:
        sample = sample if isinstance(sample, dict) else {}
        nickname = str(sample.get("nickname") or "未知用户")
        platform = str(sample.get("platform_label") or sample.get("platform") or "未知平台")
        content = str(sample.get("content") or "").strip()
        if not content:
            return {"healthy": False, "status": "invalid", "detail": "测试评论内容为空"}
        messages = [
            {"role": "system", "content": (
                "你是评论回复助手。请根据评论原文生成一条完整、自然、礼貌的中文回复。"
                "只输出最终回复内容，不要解释过程，不要简述，不要添加引号。"
            )},
            {"role": "user", "content": f"平台：{platform}\n昵称：{nickname}\n评论原文：{content}"},
        ]
        result = cls._chat(config, messages)
        if result.get("healthy") and result.get("text"):
            raw_text = result["text"]
            thinking, final_text = cls._split_thinking(raw_text)
            # text 是正式回复的唯一来源；raw_text/thinking 只用于测试展示和排障。
            result["raw_text"] = raw_text
            result["thinking"] = thinking
            result["text"] = final_text
            result["final_text"] = final_text
        return result

    @staticmethod
    def _parse_content_json(text: str):
        """从模型文本中提取内容生成对象，兼容代码块和前后说明文字。"""
        candidate = str(text or "").strip().lstrip("\ufeff")
        if not candidate:
            return None
        candidate = re.sub(r"```(?:json)?", "", candidate, flags=re.IGNORECASE)
        candidate = candidate.replace("```", "").strip()
        decoder = json.JSONDecoder()
        for start in range(len(candidate)):
            if candidate[start] not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        return item
        return None

    @staticmethod
    def _content_list(value) -> list[str]:
        if isinstance(value, list):
            values = value
        else:
            values = re.split(r"[\r\n,，、]+", str(value or ""))
        return [str(item).strip() for item in values if str(item).strip()]

    @classmethod
    def _normalize_content_payload(cls, payload: dict) -> dict | None:
        """把中英文混合字段统一为发布工作区使用的结构。"""
        if not isinstance(payload, dict):
            return None

        def value(*keys):
            for key in keys:
                current = payload.get(key)
                if current is not None and str(current).strip():
                    return current
            return ""

        title = str(value("标题", "title", "选题标题", "短标题") or "").strip()
        body = str(value("正文", "body", "内容", "文案", "正文内容") or "").strip()
        if not title or not body:
            return None

        topics = cls._content_list(value("话题", "topics", "标签", "hashtags"))
        outline = cls._content_list(value("大纲", "outline", "结构", "content_outline"))
        score_value = value("评分", "score", "scores", "fusion_scores")
        score = dict(score_value) if isinstance(score_value, dict) else {}
        for key in ("内容价值", "实用性", "传播潜力", "风险等级", "受众", "开头钩子", "行动引导"):
            current = payload.get(key)
            if current is not None and str(current).strip():
                score[key] = current
        return {
            "title": title,
            "body": body,
            "topics": topics,
            "outline": " · ".join(outline),
            "score": score,
        }

    @classmethod
    def generate_content(cls, config: dict, keyword: str, platform: str = "",
                         source_ref: str = "") -> dict:
        """按选题生成可直接进入发布草稿的结构化内容。"""
        keyword = str(keyword or "").strip()
        if not keyword:
            return {"healthy": False, "status": "invalid", "detail": "生成关键词不能为空"}
        platform_labels = {
            "douyin": "抖音", "xhs": "小红书", "bilibili": "B站", "weibo": "微博", "kuaishou": "快手", "tieba": "百度贴吧",
        }
        platform = str(platform or "").strip().lower()
        platform_label = platform_labels.get(platform, "五个平台") if platform else "五个平台"
        source_ref = str(source_ref or "").strip()
        source_line = f"\n参考来源：{source_ref}" if source_ref else ""
        messages = [
            {"role": "system", "content": (
                "你是内容选题与文案生成助手，参考‘选题生成系统’的结构化工作流。"
                "请围绕用户给出的选题，生成真实、具体、可直接发布的中文内容。"
                "正文必须是完整文案，不要只给摘要；不要编造实时新闻、数据或个人经历。"
                "只返回一个 JSON 对象，禁止 Markdown、解释文字和思维链。字段必须为："
                "标题、正文、话题、大纲、评分。评分可包含内容价值、实用性、传播潜力、风险等级、"
                "受众、开头钩子、行动引导。话题和大纲使用数组，正文可以分段。"
            )},
            {"role": "user", "content": (
                f"选题关键词：{keyword}\n目标平台：{platform_label}{source_line}\n"
                "请输出一条有明确切入角度、开头钩子、主体信息和行动引导的完整内容。"
                "平台为多个平台时，使用通用中文表达，便于后续分别调整。"
            )},
        ]
        result = cls._chat(
            config, messages, max_tokens=2600,
            extra_payload=cls._thinking_payload(config),
        )
        # 少数兼容服务不接受思维链控制字段，跟批量意向分析保持一致，400 时重试一次。
        if result.get("status") == "http_error" and "HTTP 400" in str(result.get("detail") or ""):
            result = cls._chat(config, messages, max_tokens=2600, extra_payload={})
        if not result.get("healthy"):
            return result

        raw_text = str(result.get("text") or "")
        thinking, final_text = cls._split_thinking(raw_text)
        payload = cls._parse_content_json(final_text) or cls._parse_content_json(raw_text)
        normalized = cls._normalize_content_payload(payload or {})
        if normalized is None:
            return {
                "healthy": False,
                "status": "invalid_response",
                "detail": "API 已响应，但未返回可解析的结构化内容",
                "raw_preview": raw_text[:3000],
                "thinking": thinking,
                "text": final_text,
                "elapsed_ms": result.get("elapsed_ms"),
            }
        return {
            "healthy": True,
            "status": "ok",
            "detail": "智能 API 内容生成完成",
            "raw_text": raw_text,
            "thinking": thinking,
            "text": final_text,
            "final_text": final_text,
            "source_ref": source_ref,
            **normalized,
            "elapsed_ms": result.get("elapsed_ms"),
        }
