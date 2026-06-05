import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tele_bot.llm.alibailian import AliBailianChatClient


class AliBailianChatClientTests(unittest.TestCase):
    def test_generate_reply_uses_responses_api_and_writes_report(self) -> None:
        class FakeFunctionCall:
            type = "function_call"
            name = "opencli_search"
            call_id = "call_1"
            arguments = '{"query":"OpenAI Responses API"}'

        class FakeResponse:
            def __init__(self, response_id, output=None, output_text=""):
                self.id = response_id
                self.output = output or []
                self.output_text = output_text

        class FakeResponses:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    return FakeResponse("resp_1", output=[FakeFunctionCall()])
                return FakeResponse(
                    "resp_2",
                    output_text="# 摘要\n这是研究摘要。\n\n# 关键信息\n- 条目一\n\n# 来源\n- https://example.com",
                )

        class FakeClient:
            def __init__(self):
                self.responses = FakeResponses()

        fake_client = FakeClient()

        with TemporaryDirectory() as temp_dir:
            client = AliBailianChatClient(
                api_key="token",
                base_url="https://example.com/v1",
                model="qwen-plus",
                report_dir=Path(temp_dir),
            )

            with patch("tele_bot.llm.alibailian.OpenAI", return_value=fake_client):
                with patch("tele_bot.llm.alibailian.OpenCLISearchTool.execute") as execute_mock:
                    execute_mock.return_value = {
                        "query": "OpenAI Responses API",
                        "engine": "duckduckgo",
                        "result_count": 1,
                        "results": [
                            {
                                "rank": 1,
                                "title": "Using tools | OpenAI API",
                                "url": "https://developers.openai.com/api/docs/guides/tools",
                                "snippet": "tool calling docs",
                            }
                        ],
                    }
                    reply = client.generate_response(
                        prompt="查一下 Responses API",
                        system_prompt="你是 markdown 助手",
                        model="qwen-plus",
                        allow_tools=True,
                        search_required=True,
                        allow_markdown=True,
                        save_markdown=True,
                        conversation_turns=None,
                    )

            self.assertIn("这是研究摘要。", reply.reply_text)
            self.assertIn(str(Path(temp_dir)), reply.report_path or "")
            report_files = list(Path(temp_dir).glob("*.md"))
            self.assertEqual(len(report_files), 1)
            self.assertIn("# 摘要", report_files[0].read_text(encoding="utf-8"))
            self.assertEqual(fake_client.responses.calls[0]["model"], "qwen-plus")
            self.assertEqual(fake_client.responses.calls[0]["tools"][0]["name"], "opencli_search")
            self.assertEqual(fake_client.responses.calls[1]["previous_response_id"], "resp_1")
            self.assertTrue(reply.used_tool)

    def test_falls_back_when_model_not_found(self) -> None:
        client = AliBailianChatClient(
            api_key="token",
            base_url="https://example.com/v1",
            model="qwen3-plus",
        )

        class FakeResponse:
            id = "resp"
            output = []
            output_text = "# 摘要\nfallback reply\n\n# 关键信息\n- ok\n\n# 来源\n- none"

        class FakeResponses:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs["model"])
                if kwargs["model"] == "qwen3-plus":
                    raise Exception("model not found")
                return FakeResponse()

        class FakeClient:
            def __init__(self):
                self.responses = FakeResponses()

        fake_client = FakeClient()

        with patch("tele_bot.llm.alibailian.OpenAI", return_value=fake_client):
            reply = client.generate_response(
                prompt="hello",
                system_prompt="你是聊天助手",
                model="qwen3-plus",
                allow_tools=False,
                search_required=False,
                allow_markdown=False,
                save_markdown=False,
                conversation_turns=None,
            )

        self.assertIn("fallback reply", reply.reply_text)
        self.assertEqual(fake_client.responses.calls, ["qwen3-plus", "qwen-plus"])

    def test_search_required_fails_closed_when_model_skips_tool(self) -> None:
        client = AliBailianChatClient(
            api_key="token",
            base_url="https://example.com/v1",
            model="qwen-plus",
        )

        class FakeResponse:
            id = "resp"
            output = []
            output_text = "我猜最新文档已经更新了。"

        class FakeResponses:
            def create(self, **kwargs):
                return FakeResponse()

        class FakeClient:
            def __init__(self):
                self.responses = FakeResponses()

        with patch("tele_bot.llm.alibailian.OpenAI", return_value=FakeClient()):
            with self.assertRaises(RuntimeError):
                client.generate_response(
                    prompt="帮我搜索一下最新 release",
                    system_prompt="你是聊天助手",
                    model="qwen-plus",
                    allow_tools=True,
                    search_required=True,
                    allow_markdown=False,
                    save_markdown=False,
                    conversation_turns=None,
                )

    def test_reads_model_settings_from_local_env(self) -> None:
        from tele_bot.config.llm import AliBailianSettings

        local_env_path = Path("/home/johnny/tele_bot/tele_bot/config/llm/local.env")
        original = local_env_path.read_text(encoding="utf-8") if local_env_path.exists() else None

        try:
            local_env_path.write_text(
                "ALIBAILIAN_API_KEY=file-key\nALIBAILIAN_MODEL=qwen3-plus\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                settings = AliBailianSettings.from_env()

            self.assertEqual(settings.api_key, "file-key")
            self.assertEqual(settings.model, "qwen3-plus")
            self.assertEqual(settings.reasoning_model, "qwen3-max")
        finally:
            if original is None:
                local_env_path.unlink(missing_ok=True)
            else:
                local_env_path.write_text(original, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()