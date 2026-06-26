"""
tele_bot FastAPI entry point.

Phase 6 wiring (AC v1.0):
- EXECUTOR=react (default): ReactAgentExecutor + SkillLoader + sqlite checkpointer
  + adviser/blog_publish/domain_hotspot tools enabled
- EXECUTOR=legacy: original ControlledAgentExecutor (rollback path)
- TELEGRAM_STREAMING=1 (default, react only): StreamingProgressReporter publishes
  a placeholder and edits it as the ReAct loop advances
- SQLITE_CHECKPOINT_PATH (default data/conversations.sqlite): persistence target

Startup contract:
- ENV parse failure → stderr `[FATAL] executor: ...` + exit 1
- SQLite open / setup failure → stderr `[FATAL] sqlite: ...` + exit 1
- Normal start → stderr `tele_bot starting executor=<x> streaming=<on|off> sqlite=<path>`
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, status

from tele_bot.agent import AgentCore
from tele_bot.agents import ControlledAgentExecutor, ReactAgentExecutor
from tele_bot.channels.telegram import TelegramAdapter
from tele_bot.config import AliBailianSettings, RuntimeSettings, TelegramSettings
from tele_bot.llm import AliBailianChatClient
from tele_bot.llm.react_client import build_chat_openai
from tele_bot.models import IncomingMessage
from tele_bot.persistence import build_sqlite_saver
from tele_bot.router import JsonFileConversationStateStore, Router, RouterConfig
from tele_bot.service import MessageService
from tele_bot.skills import SkillLoader
from tele_bot.tools.lc_adapters import build_core_tools
from tele_bot.workflows.react_graph import build_react_graph


def _fatal(component: str, reason: str) -> None:
    print(f"[FATAL] {component}: {reason}", file=sys.stderr)
    sys.exit(1)


def _build_react_executor(
    llm_settings: AliBailianSettings, sqlite_path: str
) -> ReactAgentExecutor:
    llm = build_chat_openai(
        api_key=llm_settings.api_key,
        base_url=llm_settings.base_url,
        model=llm_settings.model,
        temperature=0,
    )
    tools = build_core_tools(
        include_adviser=True,
        include_blog_publish=True,
        include_domain_hotspot=True,
    )
    system_prompt = SkillLoader().build_system_prompt()
    saver = build_sqlite_saver(sqlite_path)
    graph = build_react_graph(
        llm.bind_tools(tools),
        tools,
        checkpointer=saver,
        system_prompt=system_prompt,
    )
    return ReactAgentExecutor(graph=graph, model_name=llm_settings.model)


def _build_legacy_executor(
    llm_settings: AliBailianSettings,
) -> ControlledAgentExecutor:
    llm_client = AliBailianChatClient(
        api_key=llm_settings.api_key,
        base_url=llm_settings.base_url,
        model=llm_settings.model,
        timeout_seconds=llm_settings.timeout_seconds,
    )
    state_store = JsonFileConversationStateStore(
        directory=Path(__file__).resolve().parent / ".runtime" / "conversation_state"
    )
    router = Router(
        config=RouterConfig(
            default_model=llm_settings.model,
            reasoning_model=llm_settings.reasoning_model,
        )
    )
    return ControlledAgentExecutor(
        llm_client=llm_client,
        router=router,
        state_store=state_store,
    )


# --- Bootstrap (runs at import time) ---

try:
    runtime_settings = RuntimeSettings.from_env()
except RuntimeError as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(1)

settings = TelegramSettings.from_env()
llm_settings = AliBailianSettings.from_env()
telegram_adapter = TelegramAdapter(
    bot_token=settings.bot_token,
    allowed_user_ids=settings.allowed_user_ids,
    api_base_url=settings.api_base_url,
    proxy_url=settings.proxy_url,
)

if runtime_settings.executor == "react":
    try:
        executor = _build_react_executor(
            llm_settings, runtime_settings.sqlite_checkpoint_path
        )
    except sqlite3.DatabaseError as exc:
        _fatal(
            "sqlite",
            f"corrupt: {runtime_settings.sqlite_checkpoint_path}: {exc}",
        )
    except OSError as exc:
        _fatal(
            "sqlite",
            f"cannot mkdir {Path(runtime_settings.sqlite_checkpoint_path).parent}: {exc}",
        )
    streaming_enabled = runtime_settings.telegram_streaming
    _sqlite_label = runtime_settings.sqlite_checkpoint_path
else:
    executor = _build_legacy_executor(llm_settings)
    streaming_enabled = False
    _sqlite_label = "n/a"

print(
    f"tele_bot starting executor={runtime_settings.executor} "
    f"streaming={'on' if streaming_enabled else 'off'} "
    f"sqlite={_sqlite_label}",
    file=sys.stderr,
)

message_service = MessageService(
    agent_core=AgentCore(executor=executor),
    telegram_adapter=telegram_adapter,
    streaming_enabled=streaming_enabled,
)

app = FastAPI(title="tele_bot", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/messages")
def handle_message(payload: dict) -> dict:
    message = IncomingMessage(
        channel=str(payload["channel"]),
        user_id=str(payload["user_id"]),
        chat_id=str(payload["chat_id"]),
        text=str(payload["text"]),
    )
    response = message_service.handle(message)
    return asdict(response)


@app.post("/telegram/webhook")
def telegram_webhook(
    payload: dict,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    if settings.secret_token:
        if x_telegram_bot_api_secret_token != settings.secret_token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="invalid telegram secret token",
            )

    incoming = telegram_adapter.parse_incoming(payload)
    if incoming is None:
        return {"status": "ignored"}

    if not telegram_adapter.is_allowed(incoming):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="telegram user not allowed",
        )

    outgoing = message_service.handle(incoming)
    if settings.bot_token:
        delivery = telegram_adapter.send_text(outgoing)
    else:
        delivery = telegram_adapter.build_send_payload(outgoing)

    return {
        "status": "ok",
        "message": asdict(outgoing),
        "delivery": delivery,
    }


def run_telegram_polling() -> None:
    if not settings.bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required to run polling mode")

    telegram_adapter.polling_forever(
        message_handler=message_service.handle,
        polling_timeout=settings.polling_timeout,
        polling_interval_seconds=settings.polling_interval_seconds,
    )


if __name__ == "__main__":
    run_telegram_polling()
