import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tele_bot.config.telegram import TelegramSettings
from tele_bot.config.telegram.settings import _load_local_env


class TelegramSettingsTests(unittest.TestCase):
    def test_reads_values_from_env(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_SECRET_TOKEN": "secret",
            "TELEGRAM_ALLOWED_USER_IDS": "1, 2,3",
            "TELEGRAM_API_BASE_URL": "https://example.com",
            "TELEGRAM_PROXY_URL": "http://127.0.0.1:7890",
            "TELEGRAM_POLLING_TIMEOUT": "15",
            "TELEGRAM_POLLING_INTERVAL_SECONDS": "4",
        }

        with patch.dict(os.environ, env, clear=True):
            settings = TelegramSettings.from_env()

        self.assertEqual(settings.bot_token, "token")
        self.assertEqual(settings.secret_token, "secret")
        self.assertEqual(settings.allowed_user_ids, {"1", "2", "3"})
        self.assertEqual(settings.api_base_url, "https://example.com")
        self.assertEqual(settings.proxy_url, "http://127.0.0.1:7890")
        self.assertEqual(settings.polling_timeout, 15)
        self.assertEqual(settings.polling_interval_seconds, 4)

    def test_reads_values_from_local_env_file(self) -> None:
        local_env_path = Path("/home/johnny/tele_bot/tele_bot/config/telegram/local.env")
        original = local_env_path.read_text(encoding="utf-8") if local_env_path.exists() else None

        try:
            local_env_path.write_text(
                "TELEGRAM_BOT_TOKEN=file-token\nTELEGRAM_ALLOWED_USER_IDS=9,10\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                settings = TelegramSettings.from_env()

            self.assertEqual(settings.bot_token, "file-token")
            self.assertEqual(settings.allowed_user_ids, {"9", "10"})
            self.assertIsNone(settings.proxy_url)
        finally:
            if original is None:
                local_env_path.unlink(missing_ok=True)
            else:
                local_env_path.write_text(original, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
