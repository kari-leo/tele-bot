"""Feishu webhook adapter and HTTP message sender."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from tele_bot.models import (
    AttachmentDescriptor,
    CardAction,
    IncomingMessage,
    InteractiveCard,
    MessageType,
    OutgoingMessage,
)

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
        self._token_lock = threading.Lock()

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
        if event_type == "card.action.trigger":
            return self._parse_card_action(payload.get("event", {}))
        if event_type != "im.message.receive_v1":
            _LOG.debug("Ignoring Feishu event type: %s", event_type)
            return None

        event = payload.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {})

        raw_message_type = str(message.get("message_type", ""))
        if raw_message_type not in {"text", "file", "image"}:
            _LOG.debug("Ignoring unsupported Feishu message: %s", raw_message_type)
            return None

        try:
            content = json.loads(message.get("content", "{}"))
        except json.JSONDecodeError:
            _LOG.warning("Ignoring Feishu message with invalid JSON content")
            return None

        message_type = MessageType(raw_message_type)
        attachments: tuple[AttachmentDescriptor, ...] = ()
        if message_type is MessageType.TEXT:
            text = str(content.get("text", "")).strip()
        else:
            key_name = "file_key" if message_type is MessageType.FILE else "image_key"
            attachment_key = str(content.get(key_name, "")).strip()
            if not attachment_key:
                _LOG.warning("Ignoring Feishu %s message without %s", raw_message_type, key_name)
                return None
            attachment_name = content.get("file_name") or message.get("file_name")
            mime_type = content.get("mime_type") or message.get("mime_type")
            raw_size = content.get("file_size") or message.get("file_size")
            try:
                size_bytes = int(raw_size) if raw_size is not None else None
            except (TypeError, ValueError):
                size_bytes = None
            attachments = (
                AttachmentDescriptor(
                    kind=message_type,
                    key=attachment_key,
                    name=str(attachment_name) if attachment_name else None,
                    mime_type=str(mime_type) if mime_type else None,
                    size_bytes=size_bytes,
                ),
            )
            text = (
                f"[文件] {attachment_name or attachment_key}"
                if message_type is MessageType.FILE
                else "[图片] 暂不支持 OCR"
            )

        if message_type is MessageType.TEXT and message.get("chat_type") == "group":
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
            message_id=str(message.get("message_id")) if message.get("message_id") else None,
            message_type=message_type,
            attachments=attachments,
            quote_target_message_id=(
                str(message.get("parent_id") or message.get("root_id"))
                if message.get("parent_id") or message.get("root_id")
                else None
            ),
        )

    def _parse_card_action(self, event: dict[str, Any]) -> IncomingMessage | None:
        operator = event.get("operator", {}).get("operator_id", {})
        context = event.get("context", {})
        action = event.get("action", {})
        value = action.get("value", {})
        if not isinstance(value, dict):
            return None
        action_id = str(value.get("action_id") or action.get("name") or "").strip()
        token = str(value.get("value") or "").strip()
        chat_id = str(context.get("open_chat_id") or "").strip()
        user_id = str(operator.get("user_id") or operator.get("open_id") or "").strip()
        if not action_id or not token or not chat_id or not user_id:
            return None
        card_action = CardAction(action_id, str(action.get("tag") or action_id), token)
        return IncomingMessage(
            channel=self.name,
            user_id=user_id,
            chat_id=chat_id,
            text=action_id,
            message_id=str(context.get("open_message_id")) if context.get("open_message_id") else None,
            message_type=MessageType.INTERACTIVE,
            card_action=card_action,
        )

    def fetch_cloud_document(self, document_id: str) -> tuple[str, str | None]:
        """Fetch plain raw content for an explicitly selected Feishu docx document."""
        if not document_id or not all(char.isalnum() or char in "_-" for char in document_id):
            raise ValueError("invalid Feishu document identifier")
        access_token = self._get_access_token()
        response = httpx.get(
            f"{self.api_base_url}/open-apis/docx/v1/documents/{document_id}/raw_content",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        self._raise_for_http_error(response, "document fetch")
        result = response.json()
        if result.get("code") != 0:
            raise RuntimeError(self._format_api_error("document fetch", result))
        data = result.get("data", {})
        content = str(data.get("content") or "").strip()
        if not content:
            raise RuntimeError("Feishu document returned no text content")
        revision = data.get("revision_id") or data.get("document_revision_id")
        return content, str(revision) if revision is not None else None

    def is_allowed(self, message: IncomingMessage) -> bool:
        if not self.allowed_user_ids:
            return True
        return message.user_id in self.allowed_user_ids

    def send_text(self, message: OutgoingMessage) -> dict[str, Any]:
        if message.card is not None:
            return self.send_card(message.chat_id, message.card)
        if message.reply_to_message_id:
            return self.reply_text(message.reply_to_message_id, message.text)
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

    def reply_text(self, message_id: str, text: str) -> dict[str, Any]:
        """Reply to one message so Feishu renders the original-message quote."""
        normalized_id = str(message_id).strip()
        if not normalized_id:
            raise ValueError("message_id is required for a reply")
        return self._post_message_api(
            f"/open-apis/im/v1/messages/{normalized_id}/reply",
            {"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
            "message reply",
        )

    def send_card(self, chat_id: str, card: InteractiveCard) -> dict[str, Any]:
        """Send a card built only from the restricted domain contract."""
        normalized_chat_id = str(chat_id).strip()
        if not normalized_chat_id:
            raise ValueError("chat_id is required")
        card_json = self.build_card(card)
        return self._post_message_api(
            "/open-apis/im/v1/messages",
            {
                "receive_id": normalized_chat_id,
                "msg_type": "interactive",
                "content": json.dumps(card_json, ensure_ascii=False),
            },
            "card send",
            params={"receive_id_type": "chat_id"},
        )

    @staticmethod
    def build_card(card: InteractiveCard) -> dict[str, Any]:
        """Map the safe card contract to Feishu JSON without accepting raw elements."""
        if not card.title.strip() or not card.body.strip():
            raise ValueError("card title and body are required")
        if len(card.title) > 80 or len(card.body) > 4000:
            raise ValueError("card title or body exceeds the supported length")
        if len(card.actions) > 3:
            raise ValueError("a card supports at most 3 actions")
        templates = {
            None: "blue",
            "pending": "orange",
            "success": "green",
            "failed": "red",
            "cancelled": "grey",
        }
        if card.status not in templates:
            raise ValueError(f"unsupported card status: {card.status}")
        elements: list[dict[str, Any]] = [
            {"tag": "div", "text": {"tag": "lark_md", "content": card.body}}
        ]
        if card.actions:
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": action.label},
                            "type": "primary" if index == 0 else "default",
                            "value": {
                                "action_id": action.action_id,
                                "value": action.value,
                            },
                        }
                        for index, action in enumerate(card.actions)
                    ],
                }
            )
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": templates[card.status],
                "title": {"tag": "plain_text", "content": card.title},
            },
            "elements": elements,
        }

    def download_attachment(
        self,
        message_id: str,
        attachment: AttachmentDescriptor,
        *,
        max_bytes: int = 30 * 1024 * 1024,
    ) -> bytes:
        """Download an attachment after validating its server-issued identifiers."""
        if attachment.kind not in {MessageType.FILE, MessageType.IMAGE}:
            raise ValueError("only file and image attachments can be downloaded")
        normalized_id = str(message_id).strip()
        normalized_key = attachment.key.strip()
        if not normalized_id or not normalized_key or "/" in normalized_key or "\\" in normalized_key:
            raise ValueError("invalid Feishu attachment identifier")
        access_token = self._get_access_token()
        response = httpx.get(
            f"{self.api_base_url}/open-apis/im/v1/messages/{normalized_id}/resources/{normalized_key}",
            params={"type": attachment.kind.value},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        self._raise_for_http_error(response, "attachment download")
        if len(response.content) > max_bytes:
            raise ValueError(f"Feishu attachment exceeds {max_bytes} bytes")
        return response.content

    def _post_message_api(
        self,
        path: str,
        payload: dict[str, Any],
        operation: str,
        *,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        access_token = self._get_access_token()
        response = httpx.post(
            f"{self.api_base_url}{path}",
            params=params,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=payload,
            timeout=10,
        )
        self._raise_for_http_error(response, operation)
        result = response.json()
        if result.get("code") != 0:
            raise RuntimeError(self._format_api_error(operation, result))
        return result

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
        with self._token_lock:
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
            self._token_expires_at = time.time() + max(0, result.get("expire", 7200) - 300)
            return self._access_token
