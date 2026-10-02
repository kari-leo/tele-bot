import os
import unittest
from unittest.mock import patch

from tele_bot.config.feishu import FeishuSettings
from tele_bot.config.llm.settings import AliBailianSettings
from tele_bot.config.runtime import RuntimeSettings
from tele_bot.models import IncomingMessage, MessageType, OutgoingMessage


class MessageContractTests(unittest.TestCase):
    def test_legacy_positional_message_contract_remains_compatible(self) -> None:
        incoming = IncomingMessage("feishu", "user", "chat", "hello")
        outgoing = OutgoingMessage("feishu", "chat", "world", True)

        self.assertEqual(incoming.message_type, MessageType.TEXT)
        self.assertEqual(incoming.attachments, ())
        self.assertIsNone(incoming.message_id)
        self.assertTrue(outgoing.already_sent)
        self.assertIsNone(outgoing.card)


class KnowledgeConfigurationTests(unittest.TestCase):
    def test_embedding_defaults_are_safe_and_independent(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch(
            "tele_bot.config.llm.settings._load_local_env", return_value={}
        ):
            settings = AliBailianSettings.from_env()

        self.assertEqual(settings.embedding_model, "text-embedding-v4")
        self.assertEqual(settings.embedding_dimensions, 1024)
        self.assertIsNone(settings.embedding_api_key)
        with self.assertRaisesRegex(RuntimeError, "EMBEDDING_API_KEY"):
            settings.require_embedding_credentials()

    def test_invalid_embedding_dimension_fails_during_load(self) -> None:
        with patch.dict(
            os.environ, {"ALIBAILIAN_EMBEDDING_DIMENSIONS": "0"}, clear=True
        ), patch("tele_bot.config.llm.settings._load_local_env", return_value={}):
            with self.assertRaisesRegex(ValueError, "DIMENSIONS"):
                AliBailianSettings.from_env()

    def test_runtime_knowledge_defaults_and_validation(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = RuntimeSettings.from_env()
        self.assertEqual(settings.kb_sqlite_path, "data/knowledge.sqlite")
        self.assertEqual(settings.kb_max_chunks_per_space, 10_000)

        with patch.dict(os.environ, {"KB_MAX_CHUNKS_PER_SPACE": "-1"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "KB_MAX_CHUNKS_PER_SPACE"):
                RuntimeSettings.from_env()

    def test_feishu_settings_loads_kb_creators_from_feishu_config(self) -> None:
        file_values = {
            "FEISHU_APP_ID": "cli_test",
            "FEISHU_APP_SECRET": "secret",
            "KB_CREATOR_USER_IDS": " user_1,ou_2,, user_1 ",
        }
        with patch.dict(os.environ, {}, clear=True), patch.object(
            FeishuSettings, "_load_env_file", return_value=file_values
        ):
            settings = FeishuSettings.from_env()

        self.assertEqual(
            settings.kb_creator_user_ids, ("user_1", "ou_2", "user_1")
        )


if __name__ == "__main__":
    unittest.main()
