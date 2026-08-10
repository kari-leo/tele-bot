"""Feishu webhook adapter and HTTP message sender."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from tele_bot.models import IncomingMessage, OutgoingMessage

_LOG = logging.getLogger(__name__)


class FeishuWebhookAdapter:
    """Feishu event-subscription adapter for developer-server webhooks."""

    name = "feishu"

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        verification_token: str = "",
        encrypt_key: str = "",
        allowed_user_ids: list[str] | None = None,
        api_base_url: str = "https://open.feishu.cn",
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.verification_token = verification_token
        self.encrypt_key = encrypt_key
        self.allowed_user_ids = set(allowed_user_ids or [])
        self.api_base_url = api_base_url.rstrip("/")
        self._access_token: str | None = None
        self._token_expires_at: float = 0

    def decode_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return the clear Feishu event payload.

        Feishu sends plain JSON unless Encrypt Key is enabled. Encrypted events
        arrive as {"encrypt": "..."} and are decrypted here when pycryptodome is
        installed.
        """
        encrypted = payload.get("encrypt")
        if not encrypted:
            return payload
        if not self.encrypt_key:
            raise ValueError("FEISHU_ENCRYPT_KEY is required for encrypted events")
        clear_text = self._decrypt_event(str(encrypted))
        try:
            decoded = json.loads(clear_text)
        except json.JSONDecodeError as exc:
            raise ValueError("Feishu encrypted event decrypted to invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("Feishu encrypted event decrypted to a non-object payload")
        return decoded

    def _decrypt_event(self, encrypted: str) -> str:
        try:
            from Crypto.Cipher import AES
        except ImportError as exc:
            raise RuntimeError(
                "Encrypted Feishu events require pycryptodome. "
                "Install dependencies from requirements.txt or disable Encrypt Key."
            ) from exc

        key = hashlib.sha256(self.encrypt_key.encode("utf-8")).digest()
        raw = base64.b64decode(encrypted)
        cipher = AES.new(key, AES.MODE_CBC, key[:16])
        decrypted = cipher.decrypt(raw)
        pad_len = decrypted[-1]
        if pad_len < 1 or pad_len > AES.block_size:
            raise ValueError("Invalid Feishu encrypted event padding")
        return decrypted[:-pad_len].decode("utf-8")

    def challenge_response(self, payload: dict[str, Any]) -> dict[str, str] | None:
        if payload.get("type") != "url_verification":
            return None
        challenge = payload.get("challenge")
        if not isinstance(challenge, str):
            raise ValueError("Feishu url_verification payload missing challenge")
        return {"challenge": challenge}

    def event_id(self, payload: dict[str, Any]) -> str | None:
        header = payload.get("header")
        if isinstance(header, dict):
            event_id = header.get("event_id")
            if event_id:
                return str(event_id)
        uuid = payload.get("uuid")
        return str(uuid) if uuid else None

    def is_valid_token(self, payload: dict[str, Any]) -> bool:
        if not self.verification_token:
            return True
        token = payload.get("token")
        header = payload.get("header")
        if token is None and isinstance(header, dict):
            token = header.get("token")
        return token == self.verification_token

    def parse_incoming(self, payload: dict[str, Any]) -> IncomingMessage | None:
        """Parse a clear Feishu im.message.receive_v1 event."""
        header = payload.get("header", {})
        event_type = header.get("event_type")
        if event_type != "im.message.receive_v1":
            _LOG.debug("Ignoring Feishu event type: %s", event_type)
            return None

        event = payload.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {})

        if message.get("message_type") != "text":
            _LOG.debug("Ignoring non-text Feishu message: %s", message.get("message_type"))
            return None

        try:
            content = json.loads(message.get("content", "{}"))
        except json.JSONDecodeError:
            _LOG.warning("Ignoring Feishu message with invalid JSON content")
            return None

        text = str(content.get("text", "")).strip()
        if message.get("chat_type") == "group":
            for mention in message.get("mentions", []):
                mention_key = mention.get("key", "")
                if mention_key and text.startswith(mention_key):
                    text = text[len(mention_key) :].strip()
                    break

        if not text:
            return None

        sender_id = sender.get("sender_id", {})
        user_id = sender_id.get("user_id") or sender_id.get("open_id", "unknown")
        chat_id = message.get("chat_id")
        if not chat_id:
            _LOG.warning("Ignoring Feishu message without message.chat_id")
            return None

        return IncomingMessage(
            channel=self.name,
            user_id=str(user_id),
            chat_id=str(chat_id),
            text=text,
        )

    def is_allowed(self, message: IncomingMessage) -> bool:
        if not self.allowed_user_ids:
            return True
        return message.user_id in self.allowed_user_ids

    def send_text(self, message: OutgoingMessage) -> dict[str, Any]:
        access_token = self._get_access_token()
        url = f"{self.api_base_url}/open-apis/im/v1/messages"
        params = {"receive_id_type": "chat_id"}
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        payload = {
            "receive_id": message.chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": message.text}, ensure_ascii=False),
        }

        try:
            response = httpx.post(
                url, params=params, headers=headers, json=payload, timeout=10
            )
            response.raise_for_status()
            result = response.json()
            if result.get("code") != 0:
                _LOG.error("Feishu send error: %s", result)
            return result
        except Exception as exc:
            _LOG.error("Failed to send Feishu message: %s", exc)
            return {"code": -1, "msg": str(exc)}

    def edit_message(self, message_id: str, text: str) -> dict[str, Any]:
        """Update an existing text message in place."""
        access_token = self._get_access_token()
        url = f"{self.api_base_url}/open-apis/im/v1/messages/{message_id}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        payload = {
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        response = httpx.put(url, headers=headers, json=payload, timeout=10)
        self._raise_for_http_error(response, "message edit")
        result = response.json()
        if result.get("code") != 0:
            raise RuntimeError(self._format_api_error("message edit", result))
        return result

    def delete_message(self, message_id: str) -> dict[str, Any]:
        """Delete an existing message."""
        access_token = self._get_access_token()
        url = f"{self.api_base_url}/open-apis/im/v1/messages/{message_id}"
        headers = {"Authorization": f"Bearer {access_token}"}
        response = httpx.delete(url, headers=headers, timeout=10)
        self._raise_for_http_error(response, "message delete")
        result = response.json()
        if result.get("code") != 0:
            raise RuntimeError(self._format_api_error("message delete", result))
        return result

    def upload_file(self, file_path: str | Path) -> dict[str, Any]:
        """Upload a local file and return Feishu's response containing file_key."""
        path = Path(file_path)
        if not path.is_file():
            raise ValueError(f"not a file: {path}")
        if path.stat().st_size > 30 * 1024 * 1024:
            raise ValueError("Feishu file upload limit is 30 MB")
        if len(path.name) > 64:
            raise ValueError("Feishu file name must be at most 64 characters")

        access_token = self._get_access_token()
        url = f"{self.api_base_url}/open-apis/im/v1/files"
        headers = {"Authorization": f"Bearer {access_token}"}
        with path.open("rb") as file_handle:
            response = httpx.post(
                url,
                headers=headers,
                data={"file_type": "stream", "file_name": path.name},
                files={"file": (path.name, file_handle, "application/octet-stream")},
                timeout=30,
            )
        self._raise_for_http_error(response, "file upload")
        result = response.json()
        if result.get("code") != 0:
            raise RuntimeError(self._format_api_error("file upload", result))
        return result

    def send_file(self, chat_id: str, file_path: str | Path) -> dict[str, Any]:
        """Upload a local file and send it to a Feishu chat."""
        normalized_chat_id = str(chat_id).strip()
        if not normalized_chat_id or normalized_chat_id == "default":
            raise ValueError("a valid Feishu chat_id is required")

        upload_result = self.upload_file(file_path)
        file_key = upload_result.get("data", {}).get("file_key")
        if not file_key:
            raise RuntimeError(f"Feishu file upload response missing file_key: {upload_result}")

        access_token = self._get_access_token()
        url = f"{self.api_base_url}/open-apis/im/v1/messages"
        payload = {
            "receive_id": normalized_chat_id,
            "msg_type": "file",
            "content": json.dumps({"file_key": file_key}),
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        response = httpx.post(
            url,
            params={"receive_id_type": "chat_id"},
            headers=headers,
            json=payload,
            timeout=10,
        )
        self._raise_for_http_error(response, "file send")
        result = response.json()
        if result.get("code") != 0:
            raise RuntimeError(self._format_api_error("file send", result))
        return result

    @staticmethod
    def _raise_for_http_error(response: httpx.Response, operation: str) -> None:
        if response.is_success:
            return
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        raise RuntimeError(
            f"Feishu {operation} HTTP {response.status_code}: {detail}"
        )

    @staticmethod
    def _format_api_error(operation: str, result: dict[str, Any]) -> str:
        return (
            f"Feishu {operation} failed: code={result.get('code')}, "
            f"msg={result.get('msg', 'unknown error')}"
        )

    def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token

        url = f"{self.api_base_url}/open-apis/auth/v3/tenant_access_token/internal"
        payload = {"app_id": self.app_id, "app_secret": self.app_secret}

        response = httpx.post(url, json=payload, timeout=10)
        response.raise_for_status()
        result = response.json()
        if result.get("code") != 0:
            raise RuntimeError(f"Failed to get Feishu access token: {result}")

        self._access_token = result["tenant_access_token"]
        self._token_expires_at = time.time() + result.get("expire", 7200) - 300
        return self._access_token
