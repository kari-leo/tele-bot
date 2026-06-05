from __future__ import annotations

import json
import time
from typing import Any
from urllib import error, request

from tele_bot.models import IncomingMessage, OutgoingMessage


class TelegramAdapter:
    name = "telegram"

    def __init__(
        self,
        bot_token: str | None,
        allowed_user_ids: set[str] | None = None,
        api_base_url: str = "https://api.telegram.org",
        proxy_url: str | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.allowed_user_ids = allowed_user_ids or set()
        self.api_base_url = api_base_url.rstrip("/")
        self.proxy_url = proxy_url

    def parse_incoming(self, payload: dict[str, Any]) -> IncomingMessage | None:
        message = payload.get("message") or payload.get("edited_message")
        if not isinstance(message, dict):
            return None

        text = message.get("text")
        chat = message.get("chat")
        user = message.get("from")
        if not text or not isinstance(chat, dict) or not isinstance(user, dict):
            return None

        return IncomingMessage(
            channel=self.name,
            user_id=str(user.get("id", "")),
            chat_id=str(chat.get("id", "")),
            text=str(text).strip(),
        )

    def is_allowed(self, message: IncomingMessage) -> bool:
        if not self.allowed_user_ids:
            return True

        return message.user_id in self.allowed_user_ids

    def build_send_payload(self, message: OutgoingMessage) -> dict[str, Any]:
        return {
            "chat_id": message.chat_id,
            "text": message.text,
        }

    def build_get_updates_payload(
        self,
        offset: int | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        return payload

    def send_text(self, message: OutgoingMessage) -> dict[str, Any]:
        if not self.bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required to send Telegram messages")

        return self._post("sendMessage", self.build_send_payload(message))

    def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        if not self.bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required to poll Telegram updates")

        return self._post(
            "getUpdates",
            self.build_get_updates_payload(offset=offset, timeout=timeout),
        )

    def polling_forever(
        self,
        message_handler,
        polling_timeout: int = 30,
        polling_interval_seconds: int = 2,
    ) -> None:
        offset: int | None = None

        while True:
            updates = self.get_updates(offset=offset, timeout=polling_timeout)
            results = updates.get("result", [])

            for update in results:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1

                incoming = self.parse_incoming(update)
                if incoming is None or not self.is_allowed(incoming):
                    continue

                outgoing = message_handler(incoming)
                self.send_text(outgoing)

            if not results:
                time.sleep(polling_interval_seconds)

    def _post(self, method: str, payload_data: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.api_base_url}/bot{self.bot_token}/{method}"
        payload = json.dumps(payload_data).encode("utf-8")
        http_request = request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        opener = self._build_opener()
        try:
            with opener.open(http_request) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.URLError as exc:
            if self.proxy_url:
                raise RuntimeError(
                    f"Telegram request failed via proxy {self.proxy_url}: {exc}"
                ) from exc
            raise RuntimeError(
                "Telegram request failed. Set TELEGRAM_PROXY_URL if Telegram requires a proxy in this environment."
            ) from exc

    def _build_opener(self):
        if not self.proxy_url:
            return request.build_opener()

        proxy_handler = request.ProxyHandler(
            {
                "http": self.proxy_url,
                "https": self.proxy_url,
            }
        )
        return request.build_opener(proxy_handler)
