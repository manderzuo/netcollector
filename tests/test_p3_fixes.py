# -*- coding: utf-8 -*-
"""P3 修复验证测试：配置加载 / 模板持久化 / 日志截断 / 差量刷新。"""
import os
import json
import importlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config_loader import AppConfig, DEFAULT_CONFIG, default_config_dir, templates_path
from interactions.templates import TemplateEngine
from llm_api import LLMApiClient


class TestConfigLoader(unittest.TestCase):
    def test_default_config(self):
        cfg = AppConfig(config_dir=tempfile.mkdtemp())
        self.assertEqual(cfg.get("lead_rules", "henan_confidence_threshold"), 65)
        self.assertEqual(cfg.get("interaction", "real_sender_enabled"), False)
        self.assertEqual(cfg.get("bitbrowser", "base_url"), "http://127.0.0.1:54345")
        self.assertEqual(cfg.get("llm_api", "enabled"), False)

    def test_update_connection_and_api_sections_roundtrip(self):
        d = tempfile.mkdtemp()
        cfg = AppConfig(config_dir=d)
        cfg.update_section("bitbrowser", {
            "base_url": "http://127.0.0.1:60000",
            "timeout": 12,
        })
        cfg.update_section("llm_api", {
            "enabled": True,
            "provider": "自定义服务",
            "base_url": "https://api.example.test/v1",
            "api_key": "secret-value",
            "model": "model-a",
        })
        loaded = AppConfig(config_dir=d)
        self.assertEqual(loaded.bitbrowser()["timeout"], 12)
        self.assertEqual(loaded.llm_api()["provider"], "自定义服务")
        self.assertEqual(loaded.llm_api()["api_key"], "secret-value")

    def test_load_merge(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "app_config.json"), "w", encoding="utf-8") as f:
            f.write('{"lead_rules": {"hot_days": 7}}')
        cfg = AppConfig(config_dir=d)
        # 覆盖生效
        self.assertEqual(cfg.get("lead_rules", "hot_days"), 7)
        # 未覆盖的默认保留
        self.assertEqual(cfg.get("lead_rules", "cooling_days"), 30)

    def test_save_roundtrip(self):
        d = tempfile.mkdtemp()
        cfg = AppConfig(config_dir=d)
        cfg.save()
        self.assertTrue(os.path.exists(os.path.join(d, "app_config.json")))
        cfg2 = AppConfig(config_dir=d)
        self.assertEqual(cfg2.get("lead_rules", "henan_confidence_threshold"), 65)


class TestLLMApiCheck(unittest.TestCase):
    def test_models_url_normalizes_chat_endpoint(self):
        self.assertEqual(
            LLMApiClient._models_url("https://api.example.test/v1/chat/completions"),
            "https://api.example.test/v1/models",
        )

    def test_connection_success_does_not_expose_key(self):
        class Response:
            status = 200

            def read(self, _size):
                return b'{"data":[{"id":"model-a"}]}'

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return self.status

        with patch("llm_api.urllib.request.urlopen", return_value=Response()) as opened:
            result = LLMApiClient.check_connection({
                "base_url": "https://api.example.test/v1",
                "api_key": "secret-value",
                "model": "model-a",
            })
        self.assertTrue(result["healthy"])
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("secret-value", str(result))
        self.assertEqual(opened.call_args.args[0].full_url,
                         "https://api.example.test/v1/models")

    def test_auth_failure_is_explicit(self):
        error = __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
            "https://api.example.test/v1/models", 401, "Unauthorized", {}, None
        )
        with patch("llm_api.urllib.request.urlopen", side_effect=error):
            result = LLMApiClient.check_connection({
                "base_url": "https://api.example.test/v1",
                "api_key": "bad",
            })
        self.assertFalse(result["healthy"])
        self.assertEqual(result["status"], "auth_failed")
        self.assertNotIn("bad", str(result))

    def test_connection_rejects_model_missing_from_provider_list(self):
        class Response:
            status = 200

            def read(self, _size):
                return b'{"data":[{"id":"other-model"}]}'

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return self.status

        with patch("llm_api.urllib.request.urlopen", return_value=Response()):
            result = LLMApiClient.check_connection({
                "base_url": "https://api.example.test/v1",
                "model": "missing-model",
            })
        self.assertFalse(result["healthy"])
        self.assertEqual(result["status"], "model_missing")

    def test_deepseek_v4_uses_official_thinking_disabled_parameter(self):
        self.assertEqual(
            LLMApiClient._thinking_payload({
                "provider": "deepseek", "model": "deepseek/deepseek-v4-flash",
            }),
            {"thinking": {"type": "disabled"}},
        )

    def test_analyze_comment_returns_model_text_without_key(self):
        class Response:
            status = 200

            def read(self, _size):
                return '{"choices":[{"message":{"content":"意向等级：高\\n建议及时跟进"}}]}'.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return self.status

        with patch("llm_api.urllib.request.urlopen", return_value=Response()) as opened:
            result = LLMApiClient.analyze_comment({
                "base_url": "https://api.example.test/v1",
                "api_key": "secret-value",
                "model": "model-a",
            }, {"platform_label": "抖音", "nickname": "测试用户", "content": "多少钱？"})
        self.assertTrue(result["healthy"])
        self.assertIn("意向等级", result["text"])
        self.assertNotIn("secret-value", str(result))
        self.assertEqual(opened.call_args.args[0].full_url,
                         "https://api.example.test/v1/chat/completions")

    def test_generate_reply_requires_comment_content(self):
        result = LLMApiClient.generate_reply({"base_url": "https://api.example.test/v1",
                                              "model": "model-a"}, {})
        self.assertFalse(result["healthy"])
        self.assertEqual(result["status"], "invalid")

    def test_generate_content_parses_structured_json_and_removes_thinking(self):
        class Response:
            status = 200

            def read(self, _size):
                return ('{"choices":[{"message":{"content":"<think>整理结构</think>\\n'
                        '{\\"标题\\":\\"智能标题\\",\\"正文\\":\\"完整正文\\",'
                        '\\"话题\\":[\\"#测试\\"],\\"大纲\\":[\\"开头\\",\\"主体\\"],'
                        '\\"评分\\":{\\"内容价值\\":90}}"}}]}').encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return self.status

        with patch("llm_api.urllib.request.urlopen", return_value=Response()):
            result = LLMApiClient.generate_content({
                "base_url": "https://api.example.test/v1", "model": "model-a",
            }, "测试选题", "douyin")
        self.assertTrue(result["healthy"])
        self.assertEqual(result["title"], "智能标题")
        self.assertEqual(result["body"], "完整正文")
        self.assertEqual(result["topics"], ["#测试"])
        self.assertEqual(result["outline"], "开头 · 主体")
        self.assertEqual(result["score"]["内容价值"], 90)
        self.assertIn("整理结构", result["thinking"])
        self.assertNotIn("<think>", result["final_text"])

    def test_publishing_browser_resolves_cdp_from_top_level_script_import(self):
        browser_module = importlib.import_module("publishing.browser")
        self.assertIs(browser_module._cdp_session_class(), importlib.import_module("cdp").CdpSession)

    def test_generate_reply_removes_thinking_from_formal_text(self):
        class Response:
            status = 200

            def read(self, _size):
                return ('{"choices":[{"message":{"content":"<think>先判断用户需求</think>\\n'
                        '您好，欢迎咨询，我们可以为您详细介绍。"}}]}').encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return self.status

        with patch("llm_api.urllib.request.urlopen", return_value=Response()):
            result = LLMApiClient.generate_reply({
                "base_url": "https://api.example.test/v1", "model": "model-a",
            }, {"nickname": "测试用户", "content": "多少钱？"})
        self.assertTrue(result["healthy"])
        self.assertEqual(result["text"], "您好，欢迎咨询，我们可以为您详细介绍。")
        self.assertIn("先判断用户需求", result["thinking"])
        self.assertIn("<think>", result["raw_text"])

    def test_unclosed_thinking_is_not_put_into_reply(self):
        thinking, final_text = LLMApiClient._split_thinking(
            "<think>未闭合的思维链\n您好，这是正式回复"
        )
        self.assertIn("未闭合的思维链", thinking)
        self.assertEqual(final_text, "")

    def test_batch_analysis_parses_json_items(self):
        class Response:
            status = 200

            def read(self, _size):
                return ('{"choices":[{"message":{"content":"[{\\"编号\\":1,'
                        '\\"意向\\":\\"高\\",\\"需求\\":\\"价格\\",'
                        '\\"依据\\":\\"询价\\"}]"}}]}').encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return self.status

        with patch("llm_api.urllib.request.urlopen", return_value=Response()):
            result = LLMApiClient.analyze_comments_batch({
                "base_url": "https://api.example.test/v1", "model": "model-a",
            }, [{"platform_label": "抖音", "nickname": "用户A", "content": "多少钱？"}])
        self.assertTrue(result["healthy"])
        self.assertEqual(result["requested_count"], 1)
        self.assertEqual(result["items"][0]["意向"], "高")

    def test_batch_analysis_rejects_more_than_500(self):
        result = LLMApiClient.analyze_comments_batch({}, [{"content": "x"}] * 501)
        self.assertFalse(result["healthy"])
        self.assertIn("500", result["detail"])

    def test_batch_analysis_uses_longer_timeout(self):
        class Response:
            status = 200

            def read(self, _size):
                return ('{"choices":[{"message":{"content":"[{\\"编号\\":1,'
                        '\\"意向\\":\\"低\\"}]"}}]}').encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return self.status

        with patch("llm_api.urllib.request.urlopen", return_value=Response()) as opened:
            result = LLMApiClient.analyze_comments_batch(
                {"base_url": "https://api.example.test/v1", "model": "model-a"},
                [{"content": "多少钱？"}],
            )
        self.assertTrue(result["healthy"])
        self.assertEqual(opened.call_args.kwargs["timeout"], 300.0)
        payload = json.loads(opened.call_args.args[0].data.decode("utf-8"))
        self.assertFalse(payload["enable_thinking"])

    def test_batch_parser_accepts_jsonl_and_code_fence(self):
        parsed = LLMApiClient._parse_batch_json(
            "```json\n{\"编号\":1,\"意向\":\"高\"}\n"
            "{\"编号\":2,\"意向\":\"低\"}\n```"
        )
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[1]["意向"], "低")

    def test_batch_validator_rejects_missing_or_duplicate_numbers(self):
        valid = [
            {"编号": 1, "意向": "高"},
            {"编号": 2, "意向": "低"},
        ]
        self.assertEqual(LLMApiClient._validate_batch_items(valid, 2), valid)
        self.assertIsNone(LLMApiClient._validate_batch_items(valid[:1], 2))
        self.assertIsNone(LLMApiClient._validate_batch_items([
            {"编号": 1, "意向": "高"},
            {"编号": 1, "意向": "低"},
        ], 2))


class TestTemplatePersistence(unittest.TestCase):
    def test_save_and_load(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "templates.json")
        eng = TemplateEngine()
        eng._templates["custom"] = "您好{{nickname}}，联系我们。"
        eng.save(path)
        eng2 = TemplateEngine.load(path)
        self.assertIn("custom", eng2.list_templates())
        self.assertEqual(eng2.get("custom"), "您好{{nickname}}，联系我们。")

    def test_load_missing_returns_default(self):
        eng = TemplateEngine.load(os.path.join(tempfile.mkdtemp(), "nope.json"))
        self.assertIn("greeting", eng.list_templates())

    def test_default_template_uses_chinese_variables(self):
        eng = TemplateEngine()
        self.assertIn("{{昵称}}", eng.get("greeting"))
        self.assertIn("{{省份}}", eng.get("greeting"))
        rendered = eng.render(
            "greeting",
            {"nickname": "小王", "province": "河南省", "product": "方案",
             "contact_hint": "欢迎咨询"},
        )
        self.assertIn("小王", rendered)
        self.assertIn("河南省", rendered)

    def test_validate_forbidden(self):
        eng = TemplateEngine()
        risks = eng.validate("x", "{{cookie}}")
        self.assertTrue(any("cookie" in r for r in risks))

    def test_custom_variable_persists_and_renders(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "templates.json")
        eng = TemplateEngine()
        eng.set_custom_variable("门店名称", "郑州体验店")
        eng._templates["store"] = "您好{{昵称}}，欢迎到{{门店名称}}咨询。"
        self.assertEqual(
            eng.render("store", {"nickname": "小王"}),
            "您好小王，欢迎到郑州体验店咨询。",
        )
        eng.save(path)
        loaded = TemplateEngine.load(path)
        self.assertEqual(loaded.list_custom_variables(), {"门店名称": "郑州体验店"})
        self.assertEqual(
            loaded.render("store", {"nickname": "小李"}),
            "您好小李，欢迎到郑州体验店咨询。",
        )

    def test_custom_variable_cannot_override_system_or_forbidden_fields(self):
        eng = TemplateEngine()
        with self.assertRaisesRegex(ValueError, "系统变量重复"):
            eng.set_custom_variable("昵称", "固定昵称")
        with self.assertRaisesRegex(ValueError, "禁用字段"):
            eng.set_custom_variable("token", "secret")

    def test_load_legacy_template_file(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "templates.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"legacy": "您好{{昵称}}"}')
        eng = TemplateEngine.load(path)
        self.assertEqual(eng.get("legacy"), "您好{{昵称}}")
        self.assertEqual(eng.list_custom_variables(), {})


class TestLogSanitize(unittest.TestCase):
    def test_sanitize_truncates_and_masks(self):
        from gui import GuiApp

        # 静态方法直接测
        s = GuiApp._sanitize_log("cookie=abc123 " + "x" * 500)
        self.assertLessEqual(len(s), 201)  # 200 + 省略号
        self.assertNotIn("abc123", s)
        self.assertIn("***", s)


if __name__ == "__main__":
    unittest.main()
