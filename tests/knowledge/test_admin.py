"""Administrators manage all spaces while queries keep their selected scope."""

from unittest.mock import patch

import pytest

from tele_bot.config.feishu import FeishuSettings
from tele_bot.config.mcp import KnowledgeMCPSettings
from tele_bot.persistence.kb_store import KnowledgeStore


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "kb.sqlite"
    legacy = KnowledgeStore(path)
    alpha = legacy.create_space("chat-a", "Alpha", "old-owner")
    beta = legacy.create_space("chat-b", "Beta", "another-owner")
    legacy.add_member("chat-a", "old-owner", "legacy-member")
    legacy.close()
    admin_store = KnowledgeStore(path, admin_user_ids=("admin",))
    yield admin_store, alpha, beta
    admin_store.close()


def test_admin_can_manage_existing_spaces_without_membership_migration(store):
    kb, alpha, beta = store
    assert {s.id for s in kb.list_accessible_spaces("admin")} == {alpha.id, beta.id}
    assert kb.require_member("chat-a", "admin", admin=True).id == alpha.id
    assert kb.require_space_member(beta.id, "admin", admin=True).id == beta.id
    # Selecting a different space remains explicit and never searches all spaces.
    assert kb.bind_space("private", "admin", "Beta").id == beta.id
    assert kb.require_member("private", "admin").id == beta.id
    with pytest.raises(PermissionError):
        kb.require_member("unbound", "admin")


def test_old_members_and_owners_do_not_gain_admin_access(store):
    kb, alpha, _ = store
    for user in ("old-owner", "legacy-member", "stranger"):
        for operation in (lambda: kb.list_accessible_spaces(user),
                          lambda: kb.require_member("chat-a", user),
                          lambda: kb.require_space_member(alpha.id, user),
                          lambda: kb.bind_space("private", user, "Alpha"),
                          lambda: kb.create_space("new", "New", user)):
            with pytest.raises(PermissionError):
                operation()


def test_empty_admin_configuration_grants_nobody():
    kb = KnowledgeStore(":memory:", admin_user_ids=())
    try:
        with pytest.raises(PermissionError):
            kb.create_space("chat", "Space", "admin")
    finally:
        kb.close()


def test_single_admin_config_replaces_legacy_per_feature_grants():
    values = {"FEISHU_APP_ID": "app", "FEISHU_APP_SECRET": "secret",
              "FEISHU_ADMIN_USER_IDS": " admin,admin,other ",
              "KB_CREATOR_USER_IDS": "legacy", "FEISHU_CODEX_OWNER_ID": "legacy"}
    with patch.dict("os.environ", {}, clear=True), patch.object(FeishuSettings, "_load_env_file", return_value=values):
        assert FeishuSettings.from_env().admin_user_ids == ("admin", "other")
        values["FEISHU_ADMIN_USER_IDS"] = ""
        assert FeishuSettings.from_env().admin_user_ids == ()
        values["FEISHU_ADMIN_USER_IDS"] = "*"
        with pytest.raises(ValueError, match="explicit"):
            FeishuSettings.from_env()


def test_mcp_defaults_to_admin_and_rejects_a_non_admin_identity():
    values = {"FEISHU_ADMIN_USER_IDS": "admin,other"}
    with patch.dict("os.environ", {}, clear=True), patch("tele_bot.config.mcp._load_feishu_local_env", return_value=values):
        assert KnowledgeMCPSettings.from_env().user_id == "admin"
        values["KB_MCP_USER_ID"] = "outsider"
        with pytest.raises(ValueError, match="must be one of"):
            KnowledgeMCPSettings.from_env()
