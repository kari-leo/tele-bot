"""History permissions, card workflows and non-idempotent resume boundaries."""

from collections import deque
from dataclasses import replace
from unittest.mock import Mock, patch
import json
import sys
import time

import pytest

from tele_bot.channels.feishu import FeishuWebhookAdapter
from tele_bot.config.feishu import FeishuSettings
from tele_bot.models import IncomingMessage, MessageType
from tele_bot.router.codex_history_service import CodexHistoryService
from tele_bot.tools.codex_history import AppServerConnection, CodexHistoryClient, CodexHistoryError
from tele_bot.tools.codex_runner import CodexRunner
from tele_bot.tools.workspace_policy import WorkspacePolicy


def message(text="/codex history", **kwargs):
    return IncomingMessage("feishu", "user", "private", text,
                           chat_type=kwargs.pop("chat_type", "p2p"),
                           sender_ids=("user", "ou_owner"), **kwargs)


def callback(action, **kwargs):
    return message(action.action_id, chat_type=None, message_type=MessageType.INTERACTIVE,
                   card_action=action, **kwargs)


class History:
    def __init__(self):
        self.rows = [{"id": f"thread-{i}", "name": f"任务 {i}", "cwd": "D:/project",
                      "updatedAt": 1792900000} for i in range(10)]
        self.list_calls, self.read_calls, self.resume_calls = [], [], []
        self.text = "这是历史答复"

    def list_threads(self, **kwargs):
        self.list_calls.append(kwargs)
        return {"data": self.rows, "nextCursor": "next-page"}

    def read_thread(self, thread_id):
        self.read_calls.append(thread_id)
        return next(row for row in self.rows if row["id"] == thread_id)

    def read_turns(self, thread_id, cursor=None):
        return {"data": [{"items": [{"type": "userMessage", "content": [{"type": "text", "text": "原任务"}]},
                                   {"type": "reasoning", "text": "不要展示的推理"},
                                   {"type": "commandExecution", "aggregatedOutput": "不要展示的工具输出"},
                                   {"type": "agentMessage", "text": self.text}]}], "nextCursor": None}

    def resume(self, thread_id, prompt, **kwargs):
        self.resume_calls.append((thread_id, prompt))
        return "执行完成"


@pytest.fixture
def history():
    return History()


@pytest.fixture
def service(history):
    return CodexHistoryService(history, ("ou_owner",))


@pytest.mark.parametrize("change", [
    {"chat_type": "group"}, {"chat_type": None}, {"channel": "api"},
    {"user_id": "intruder", "sender_ids": ("ou_other",)},
])
def test_reject_before_access(service, history, change):
    result = service.handle(replace(message(), **change))
    assert result is not None
    assert not history.list_calls


def test_missing_admin_and_untrusted_ingress_fail_closed(history):
    for admins, trusted in [((), True), (("ou_owner",), False)]:
        result = CodexHistoryService(history, admins).handle(message(), trusted=trusted)
        assert result is not None
    assert not history.list_calls


def test_private_list_buttons_pagination_and_number(service, history):
    response = service.handle(message("查看 Codex 最近会话"))
    assert len(response.card.actions) == 12
    payload = FeishuWebhookAdapter.build_card(response.card)
    assert len(payload["elements"]) == 5
    assert all(len(element["actions"]) <= 3 for element in payload["elements"][1:])
    next_page = next(a for a in response.card.actions if a.label == "下一页")
    service.handle(callback(next_page))
    assert history.list_calls[-1]["cursor"] == "next-page"
    detail = service.handle(message("/codex history 2"))
    assert "任务 1" in detail.card.body
    assert "原任务" in detail.card.body
    assert "不要展示" not in detail.card.body
    assert not history.resume_calls


def test_forwarded_forged_and_expired_buttons_do_not_read(service, history):
    action = service.handle(message()).card.actions[0]
    for changed in [replace(callback(action), chat_id="forwarded"),
                    replace(callback(action), user_id="other", sender_ids=()),
                    replace(callback(action), chat_type="group"),
                    callback(replace(action, action_id="codex_history_continue"))]:
        service.handle(changed)
    service.clock = lambda: time.monotonic() + 1900
    service.handle(callback(action))
    assert not history.read_calls
    assert not history.resume_calls


def test_history_button_is_bound_to_the_admin_who_opened_it(service, history):
    service.admin_user_ids = frozenset(("ou_owner", "ou_other_admin"))
    action = service.handle(message()).card.actions[0]
    service.handle(replace(callback(action), user_id="ou_other_admin", sender_ids=()))
    assert not history.read_calls
    assert not history.resume_calls


def test_browse_continue_then_one_task_and_cancel(service, history):
    first = service.handle(message()).card.actions[0]
    detail = service.handle(callback(first))
    continue_button = next(a for a in detail.card.actions if a.label == "继续此会话")
    response = service.handle(callback(continue_button))
    assert "发送接下来" in response.text
    assert not history.resume_calls
    service.handle(message("修改 README 并说明变更"))
    assert history.resume_calls == [("thread-0", "修改 README 并说明变更")]
    assert service.handle(message("一般聊天")) is None
    service.handle(callback(continue_button))
    service.handle(message("取消"))
    assert len(history.resume_calls) == 1
    assert service.handle(message("一般聊天")) is None


def test_explicit_resume_uses_selected_number_and_ignores_other_commands(service, history):
    service.handle(message())
    service.handle(message("/codex resume 3 完成这个改动"))
    assert history.resume_calls == [("thread-2", "完成这个改动")]
    service.handle(message("/codex resume"))
    assert service.handle(message("/codex inspect 查看当前项目")) is None
    service.handle(message("取消"))
    assert len(history.resume_calls) == 1


def test_search_archive_and_list_expiry(service, history):
    service.handle(message("/codex history --archived 机器人"))
    assert history.list_calls[-1] == {"cursor": None, "search": "机器人", "archived": True}
    detail = service.handle(message("/codex history 1"))
    assert all(action.label != "继续此会话" for action in detail.card.actions)
    assert "只能查看" in service.handle(message("/codex resume 1 新任务")).text
    assert not history.resume_calls
    service.clock = lambda: time.monotonic() + 1900
    result = service.handle(message("/codex resume 1 新任务"))
    assert "序号不在" in result.text
    assert not history.resume_calls


def test_long_transcript_paginates_without_losing_text(service, history):
    history.text = "甲" * 7000 + "末尾标记"
    service.handle(message())
    reply = service.handle(message("/codex history 1"))
    pages = [reply.card.body]
    while any(a.label == "继续阅读" for a in reply.card.actions):
        reply = service.handle(callback(next(a for a in reply.card.actions if a.label == "继续阅读")))
        assert len(reply.card.body) <= 4000
        pages.append(reply.card.body)
    assert sum(page.count("甲") for page in pages) == 7000
    assert "末尾标记" in pages[-1]


def test_resume_error_is_not_retried_or_logged_as_raw_exception(service, history):
    service.handle(message())
    history.resume = Mock(side_effect=CodexHistoryError("执行状态未知，请先查看历史。"))
    reply = service.handle(message("/codex resume 1 做任务"))
    assert "执行状态未知" in reply.text
    history.resume.assert_called_once()
    assert service.handle(message("普通问题")) is None


def test_config_admin_comes_from_feishu_config():
    values = {"FEISHU_APP_ID": "app", "FEISHU_APP_SECRET": "secret", "FEISHU_ADMIN_USER_IDS": "ou_owner"}
    with patch.dict("os.environ", {}, clear=True), patch.object(FeishuSettings, "_load_env_file", return_value=values):
        assert FeishuSettings.from_env().admin_user_ids == ("ou_owner",)


def test_adapter_preserves_verified_chat_type_and_aliases():
    adapter = FeishuWebhookAdapter("app", "secret")
    parsed = adapter.parse_incoming({"header": {"event_type": "im.message.receive_v1"}, "event": {
        "sender": {"sender_id": {"user_id": "user", "open_id": "ou_owner"}},
        "message": {"chat_type": "p2p", "chat_id": "chat", "message_type": "text",
                    "content": json.dumps({"text": "/codex history"})}}})
    assert parsed.chat_type == "p2p"
    assert parsed.sender_ids == ("user", "ou_owner")


def test_service_routes_history_before_agent_and_disables_public_api(service):
    from tele_bot.service import MessageService
    core = Mock()
    adapter = FeishuWebhookAdapter("app", "secret", verification_token="verify")
    messages = MessageService(core, feishu_adapter=adapter, codex_history_service=service,
                              attachment_store=Mock(), admin_user_ids=("ou_owner",))
    response = messages.handle(message())
    assert "任务 0" in response.text
    blocked = messages.handle(message(), allow_codex_history=False)
    assert "仅配置的管理员" in blocked.text
    adapter.verification_token = ""
    assert "任务 0" in messages.handle(message()).text
    messages.handle(message("/codex resume 1 继续修改"))
    assert service.client.resume_calls == [("thread-0", "继续修改")]
    core.handle_message.assert_not_called()


@pytest.fixture
def runner(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = WorkspacePolicy((tmp_path,), workspace, tmp_path / "quarantine")
    return CodexRunner(policy=policy, default_workspace=workspace, timeout_seconds=10)


class Server:
    def __init__(self, runner):
        self.events = deque()
        self.calls = []
        self.thread = {"id": "thread", "cwd": str(runner.default_workspace), "model": "saved-model"}
        self.failure = False
        self.waited = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def rpc(self, method, params, **kwargs):
        self.calls.append((method, params))
        return {"thread": self.thread, "turn": {"id": "new-turn"}, "data": []}

    def wait_turn(self, thread_id, turn_id, **kwargs):
        self.waited = (thread_id, turn_id)
        if self.failure:
            raise CodexHistoryError("timeout")
        return "done"


def test_resume_preserves_id_model_and_overrides_inherited_permissions(runner):
    server = Server(runner)
    client = CodexHistoryClient(runner, lambda _: server)
    assert client.resume("thread", "继续修改") == "done"
    assert server.waited == ("thread", "new-turn")
    resume = dict(server.calls)["thread/resume"]
    turn = dict(server.calls)["turn/start"]
    assert resume["threadId"] == turn["threadId"] == "thread"
    assert resume["model"] == turn["model"] == "saved-model"
    assert resume["sandbox"] == "workspace-write"
    assert turn["approvalPolicy"] == "never"
    assert turn["approvalsReviewer"] == "user"
    assert turn["sandboxPolicy"] == {"type": "workspaceWrite", "writableRoots": [str(runner.default_workspace)],
                                      "networkAccess": False, "excludeSlashTmp": True, "excludeTmpdirEnvVar": True}


@pytest.mark.parametrize("bad_cwd", ["", "missing", "outside"])
def test_disallowed_workspaces_never_resume(runner, tmp_path, bad_cwd):
    server = Server(runner)
    server.thread["cwd"] = str(tmp_path.parent) if bad_cwd == "outside" else bad_cwd
    client = CodexHistoryClient(runner, lambda _: server)
    with pytest.raises(CodexHistoryError):
        client.resume("thread", "task")
    assert [m for m, _ in server.calls] == ["thread/read"]


def test_timeout_interrupts_only_the_started_turn_and_does_not_retry(runner):
    server = Server(runner)
    server.failure = True
    client = CodexHistoryClient(runner, lambda _: server)
    with pytest.raises(CodexHistoryError, match="避免重复执行"):
        client.resume("thread", "task")
    assert [m for m, _ in server.calls].count("turn/start") == 1
    assert dict(server.calls)["turn/interrupt"] == {"threadId": "thread", "turnId": "new-turn"}
    assert client.execution_lock.acquire(blocking=False)
    client.execution_lock.release()


def test_busy_threads_and_concurrent_execution_do_not_start_a_turn(runner):
    server = Server(runner)
    server.thread["status"] = {"type": "active"}
    client = CodexHistoryClient(runner, lambda _: server)
    with pytest.raises(CodexHistoryError, match="正在执行"):
        client.resume("thread", "task")
    assert [m for m, _ in server.calls] == ["thread/read"]
    client.execution_lock.acquire()
    with pytest.raises(CodexHistoryError, match="等待完成"):
        client.resume("thread", "task")
    client.execution_lock.release()


def test_resume_budget_cannot_exceed_twenty_minutes(runner):
    server = Server(runner)
    server.wait_turn = Mock(return_value="done")
    client = CodexHistoryClient(replace(runner, timeout_seconds=99999), lambda _: server)
    before = time.monotonic()
    client.resume("thread", "task")
    deadline = server.wait_turn.call_args.kwargs["deadline"]
    assert 1199 <= deadline - before <= 1201


def test_list_includes_exec_source_and_filters_before_pagination(runner):
    server = Server(runner)
    client = CodexHistoryClient(runner, lambda _: server)
    client.list_threads(cursor="next", search="中文", archived=True)
    params = server.calls[0][1]
    assert "exec" in params["sourceKinds"]
    assert params["cursor"] == "next"
    assert params["archived"] is True
    assert params["searchTerm"] == "中文"


def test_rpc_rejects_destructive_methods_and_redacts_errors(runner):
    server = AppServerConnection(runner)
    with pytest.raises(CodexHistoryError):
        server.rpc("thread/delete", {"threadId": "thread"})
    server._send = Mock()
    server.messages.put({"id": 1, "error": {"code": -32603, "message": "secret credential content"}})
    with pytest.raises(CodexHistoryError) as error:
        server.rpc("thread/list", {})
    assert "secret" not in str(error.value)


def test_rpc_rejects_server_approvals_and_correlates_final_turn(runner):
    server = AppServerConnection(runner)
    server._send = Mock()
    server.messages.put({"id": "approval", "method": "item/commandExecution/requestApproval", "params": {}})
    server.messages.put({"id": 1, "result": {"turn": {"id": "new"}}})
    server.rpc("turn/start", {})
    assert server._send.call_args[0][0]["error"]["code"] == -32601
    server.events.extend([
        {"method": "turn/completed", "params": {"threadId": "other", "turn": {"id": "new", "status": "completed"}}},
        {"method": "item/completed", "params": {"threadId": "thread", "turnId": "old", "item": {"text": "stale"}}},
        {"method": "item/completed", "params": {"threadId": "thread", "turnId": "new", "item": {"text": "新答复", "phase": "final_answer"}}},
        {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "new", "status": "completed"}}},
    ])
    assert server.wait_turn("thread", "new", deadline=time.monotonic()+1) == "新答复"


def test_stdio_protocol_unicode_and_early_notifications(runner, tmp_path):
    # Real pipes against a tiny local protocol peer: no account or model calls.
    script = tmp_path / "with spaces" / "peer.py"
    script.parent.mkdir()
    script.write_text('''import json, sys
for line in sys.stdin.buffer:
    request = json.loads(line)
    if "id" not in request:
        continue
    result = {"ok": "中文"} if request["method"] == "thread/list" else {}
    if request["method"] == "thread/list":
        print(json.dumps({"method": "turn/completed", "params": {"threadId": "old"}}), flush=True)
    print(json.dumps({"id": request["id"], "result": result}), flush=True)
''', encoding="utf-8")
    configured = replace(runner, command=f'"{sys.executable}" -u "{script}"')
    with AppServerConnection(configured) as server:
        assert server.rpc("thread/list", {}) == {"ok": "中文"}
        assert server.events[0]["method"] == "turn/completed"
    assert server.process is None
