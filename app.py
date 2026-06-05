from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, status

from tele_bot.agents import ControlledAgentExecutor
from tele_bot.agent import AgentCore
from tele_bot.channels.telegram import TelegramAdapter
from tele_bot.config import AliBailianSettings, TelegramSettings
from tele_bot.llm import AliBailianChatClient
from tele_bot.models import IncomingMessage
from tele_bot.router import JsonFileConversationStateStore, Router, RouterConfig
from tele_bot.service import MessageService

settings = TelegramSettings.from_env()
llm_settings = AliBailianSettings.from_env()
telegram_adapter = TelegramAdapter(
    bot_token=settings.bot_token,
    allowed_user_ids=settings.allowed_user_ids,
    api_base_url=settings.api_base_url,
    proxy_url=settings.proxy_url,
)
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
executor = ControlledAgentExecutor(
    llm_client=llm_client,
    router=router,
    state_store=state_store,
)
message_service = MessageService(agent_core=AgentCore(executor=executor))

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
