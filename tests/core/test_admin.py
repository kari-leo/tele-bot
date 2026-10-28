"""Only the configured administrator reaches tools through the Feishu entry."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from tele_bot.channels.feishu import FeishuWebhookAdapter
from tele_bot.models import IncomingMessage, OutgoingMessage
from tele_bot.service import MessageService


def message(**changes):
    return replace(IncomingMessage("feishu", "user_id", "private", "hello",
                                   chat_type="p2p", sender_ids=("user_id", "ou_admin")), **changes)


@pytest.fixture
def service():
    core = Mock()
    core.handle_message.side_effect = lambda incoming: OutgoingMessage("feishu", incoming.chat_id, "ok")
    attachments = Mock()
    attachments.find.return_value = ()
    return MessageService(core, FeishuWebhookAdapter("app", "secret", verification_token="verify"),
                          attachment_store=attachments, admin_user_ids=("ou_admin",))


@pytest.mark.parametrize("changes, trusted", [
    ({"user_id": "other", "sender_ids": ()}, True),
    ({"channel": "api"}, True),
    ({}, False),
])
def test_unauthorized_and_raw_api_requests_do_not_reach_tools(service, changes, trusted):
    result = service.handle(message(**changes), allow_admin=trusted)
    assert "仅允许" in result.text
    service.agent_core.handle_message.assert_not_called()
    service.attachment_store.remember.assert_not_called()


def test_admin_alias_is_normalized_for_existing_data_and_callbacks(service):
    assert service.handle(message()).text == "ok"
    forwarded = service.agent_core.handle_message.call_args.args[0]
    assert forwarded.user_id == "ou_admin"
    assert forwarded.sender_ids == ("user_id", "ou_admin")


def test_unconfigured_admin_fails_closed(service):
    service.admin_user_ids = ()
    assert "仅允许" in service.handle(message()).text
    service.agent_core.handle_message.assert_not_called()


def test_admin_does_not_require_a_verification_token(service):
    service.feishu_adapter.verification_token = ""
    assert service.handle(message()).text == "ok"
    assert service.agent_core.handle_message.call_args.args[0].user_id == "ou_admin"


def test_ingress_allowlist_accepts_verified_open_id_alias():
    adapter = FeishuWebhookAdapter("app", "secret", allowed_user_ids=["ou_admin"])
    assert adapter.is_allowed(message())
    assert not adapter.is_allowed(message(user_id="other", sender_ids=()))
