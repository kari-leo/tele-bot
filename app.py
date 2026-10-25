"""tele_bot FastAPI and polling entry points."""

from __future__ import annotations

import os
import sqlite3
import sys
import logging
from collections import deque
from dataclasses import asdict
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException

from tele_bot.agent import AgentCore
from tele_bot.agents import ReactAgentExecutor
from tele_bot.channels.feishu import FeishuWebhookAdapter
from tele_bot.channels.feishu_batching import FeishuMessageBatcher
from tele_bot.persistence.attachment_store import AttachmentStore
from tele_bot.config import AliBailianSettings, RuntimeSettings
from tele_bot.config.paths import configure_runtime_root
from tele_bot.config.feishu import FeishuSettings
from tele_bot.llm.react_client import build_chat_openai, build_context_manager
from tele_bot.llm.embeddings import AliBailianEmbeddingClient
from tele_bot.knowledge.ingestion import DocumentParser, TextChunker
from tele_bot.knowledge.config import IndexingSettings
from tele_bot.knowledge.cloud_sources import CloudSourceConnector
from tele_bot.knowledge.feishu_docs import FeishuDocsClient
from tele_bot.knowledge.search import KnowledgeIndexer, KnowledgeSearch
from tele_bot.models import IncomingMessage
from tele_bot.persistence import KnowledgeStore, build_sqlite_saver
from tele_bot.service import MessageService
from tele_bot.scheduled.service import ScheduleParser, ScheduleService
from tele_bot.scheduled.store import ScheduleStore
from tele_bot.scheduled.worker import ScheduleWorker
from tele_bot.scheduled.delivery import send_scheduled_message
from tele_bot.skills import SkillLoader
from tele_bot.tools.lc_adapters import build_core_tools
from tele_bot.tools.blog_publish import BlogPublishTool
from tele_bot.tools.codex_runner import CodexRunner
from tele_bot.router.codex_service import CodexCommandService
from tele_bot.router.knowledge_service import KnowledgeCommandService
from tele_bot.tools.workspace_policy import WorkspacePolicy
from tele_bot.workflows.react_graph import build_react_graph
from tele_bot.bitable.bootstrap import build_bitable_service

_LOG = logging.getLogger(__name__)
_FEISHU_EVENT_CACHE_LIMIT = 1000
_feishu_event_ids: deque[str] = deque(maxlen=_FEISHU_EVENT_CACHE_LIMIT)
_feishu_event_id_set: set[str] = set()
_feishu_event_lock = Lock()

configure_runtime_root(__file__)


def _fatal(component: str, reason: str) -> None:
    print(f"[FATAL] {component}: {reason}", file=sys.stderr)
    sys.exit(1)


def _build_executor(
    llm_settings: AliBailianSettings,
    sqlite_path: str,
    feishu_adapter: FeishuWebhookAdapter | None = None,
    codex_service: CodexCommandService | None = None,
    schedule_service: ScheduleService | None = None,
    knowledge_search: KnowledgeSearch | None = None,
    bitable_service=None,
) -> ReactAgentExecutor:
    llm = build_chat_openai(
        api_key=llm_settings.api_key,
        base_url=llm_settings.base_url,
        model=llm_settings.model,
        temperature=0,
        timeout=llm_settings.timeout_seconds,
    )
    posts_dir = Path(
        os.environ.get(
            "WINDBORNE_POSTS_DIR",
            str(
                Path(__file__).resolve().parent.parent
                / "windborne-blog"
                / "src"
                / "content"
                / "posts"
            ),
        )
    )
    include_blog = posts_dir.is_dir()
    if not include_blog:
        print(
            f"[INFO] blog_publish disabled - posts_dir not found: {posts_dir}",
            file=sys.stderr,
        )
    skill_loader = SkillLoader()
    tools = build_core_tools(
        include_adviser=True,
        include_blog_publish=include_blog,
        blog_publish_tool=BlogPublishTool(posts_dir) if include_blog else None,
        include_domain_hotspot=True,
        feishu_adapter=feishu_adapter,
        codex_service=codex_service,
        schedule_service=schedule_service,
        knowledge_search=knowledge_search,
        skill_loader=skill_loader,
        bitable_service=bitable_service,
    )
    skill_loader.available_tools = {tool.name for tool in tools}
    system_prompt = skill_loader.build_system_prompt()
    saver = build_sqlite_saver(sqlite_path)
    graph = build_react_graph(
        llm.bind_tools(tools, max_tokens=4096),
        tools,
        checkpointer=saver,
        system_prompt=system_prompt,
        context_manager=build_context_manager(llm_settings, tools),
        skill_loader=skill_loader,
    )
    return ReactAgentExecutor(graph=graph, model_name=llm_settings.model,
                              knowledge_store=knowledge_search.store if knowledge_search is not None else None,
                              bitable_service=bitable_service)


try:
    runtime_settings = RuntimeSettings.from_env()
except RuntimeError as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(1)

llm_settings = AliBailianSettings.from_env()
try:
    feishu_settings = FeishuSettings.from_env()
except ValueError as exc:
    feishu_settings = None
    print(f"[INFO] Feishu webhook disabled: {exc}", file=sys.stderr)

feishu_adapter = (
    FeishuWebhookAdapter(
        app_id=feishu_settings.app_id,
        app_secret=feishu_settings.app_secret,
        verification_token=feishu_settings.verification_token,
        encrypt_key=feishu_settings.encrypt_key,
        allowed_user_ids=feishu_settings.allowed_user_ids,
        api_base_url=feishu_settings.api_base_url,
    )
    if feishu_settings is not None
    else None
)

try:
    schedule_store = ScheduleStore(os.environ.get("TELE_BOT_SCHEDULE_DB", "data/scheduled.sqlite"))
    schedule_parser = ScheduleParser(build_chat_openai(
        api_key=llm_settings.api_key,
        base_url=llm_settings.base_url,
        model=llm_settings.model,
        temperature=0,
        timeout=llm_settings.timeout_seconds,
    ))
    schedule_service = ScheduleService(schedule_store, schedule_parser)
    schedule_worker = ScheduleWorker(schedule_store,
                                    (lambda outgoing: send_scheduled_message(feishu_adapter, outgoing))
                                    if feishu_adapter else None)
except (sqlite3.DatabaseError, OSError, ValueError) as exc:
    _fatal("scheduled-tasks", str(exc))

try:
    knowledge_store = KnowledgeStore(
        runtime_settings.kb_sqlite_path,
        max_chunks_per_space=runtime_settings.kb_max_chunks_per_space,
    )
    embedding_client = AliBailianEmbeddingClient(llm_settings)
    indexing_settings = IndexingSettings.from_env()
    knowledge_indexer = KnowledgeIndexer(
        knowledge_store, embedding_client, llm_settings,
        chunker=TextChunker(max_chars=indexing_settings.max_chars, overlap_chars=indexing_settings.overlap_chars),
        embedding_context_prefix=indexing_settings.context_prefix,
    )
    knowledge_search = KnowledgeSearch(knowledge_store, embedding_client, llm_settings)
    knowledge_parser = DocumentParser(max_bytes=runtime_settings.kb_max_import_bytes,
                                      table_chunk_chars=indexing_settings.max_chars)
    docs_connector = CloudSourceConnector(FeishuDocsClient(feishu_settings.app_id,feishu_settings.app_secret,
        api_base_url=feishu_settings.api_base_url,max_bytes=runtime_settings.kb_max_import_bytes),knowledge_parser) if feishu_settings else None
    knowledge_service = KnowledgeCommandService(
        store=knowledge_store,
        parser=knowledge_parser,
        docs_connector=docs_connector,
        indexer=knowledge_indexer,
        search=knowledge_search,
        policy=WorkspacePolicy.from_env(),
        feishu_adapter=feishu_adapter,
        creator_user_ids=(
            feishu_settings.kb_creator_user_ids if feishu_settings is not None else ()
        ),
    )
except (sqlite3.DatabaseError, OSError, ValueError) as exc:
    _fatal("knowledge-base", str(exc))

bitable_service = build_bitable_service()

try:
    codex_service = CodexCommandService(CodexRunner.from_env())
    executor = _build_executor(
        llm_settings, runtime_settings.sqlite_checkpoint_path,
        feishu_adapter=feishu_adapter, codex_service=codex_service,
        schedule_service=schedule_service if feishu_adapter is not None else None,
        knowledge_search=knowledge_search,
        bitable_service=bitable_service,
    )
except (sqlite3.DatabaseError, OSError, ValueError) as exc:
    _fatal("agent", str(exc))

print(
    f"tele_bot starting "
    f"sqlite={runtime_settings.sqlite_checkpoint_path}",
    f" kb_sqlite={runtime_settings.kb_sqlite_path}",
    file=sys.stderr,
 )

message_service = MessageService(
    agent_core=AgentCore(executor=executor),
    feishu_adapter=feishu_adapter,
    streaming_enabled=feishu_adapter is not None,
    codex_service=codex_service,
    knowledge_service=knowledge_service,
    bitable_service=bitable_service,
    attachment_store=AttachmentStore(Path(runtime_settings.sqlite_checkpoint_path).with_name("attachments.sqlite")),
 )

app = FastAPI(title="tele_bot", version="0.1.0")


@app.on_event("startup")
def start_schedule_worker() -> None:
    schedule_worker.start()


@app.on_event("shutdown")
def stop_schedule_worker() -> None:
    schedule_worker.stop()
    if not feishu_message_batcher.wait_idle(30):
        _LOG.warning("Feishu turns are still running during shutdown")


_feishu_webhook_path = (
    feishu_settings.webhook_path if feishu_settings is not None else "/feishu/webhook"
)


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
    response = message_service.handle(message, allow_schedule=False, allow_knowledge=False, allow_bitable=False)
    return asdict(response)


def _remember_feishu_event_id(event_id: str | None) -> bool:
    if not event_id:
        return True
    with _feishu_event_lock:
        if event_id in _feishu_event_id_set:
            return False
        if len(_feishu_event_ids) == _feishu_event_ids.maxlen:
            oldest = _feishu_event_ids.popleft()
            _feishu_event_id_set.discard(oldest)
        _feishu_event_ids.append(event_id)
        _feishu_event_id_set.add(event_id)
        return True


def _handle_feishu_message(message: IncomingMessage) -> None:
    if feishu_adapter is None:
        return
    try:
        outgoing = message_service.handle(message)
        if outgoing is not None and not outgoing.already_sent:
            if (message.card_action is not None and message.card_action.action_id in {"bitable_confirm", "bitable_cancel"}
                    and message.message_id and outgoing.card is not None
                    and outgoing.card.metadata and outgoing.card.metadata.get("status")):
                try:
                    feishu_adapter.edit_card(message.message_id, outgoing.card)
                    return
                except Exception:
                    _LOG.warning("Could not refresh Bitable confirmation card; sending result separately", exc_info=True)
            feishu_adapter.send_text(outgoing)
            for file_path in outgoing.file_paths:
                feishu_adapter.send_file(outgoing.chat_id, file_path)
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("Failed to handle Feishu message: %s", exc)


feishu_message_batcher = FeishuMessageBatcher(_handle_feishu_message)


@app.post(_feishu_webhook_path)
def handle_feishu_webhook(
    payload: dict,
) -> dict[str, object]:
    if feishu_adapter is None:
        raise HTTPException(status_code=503, detail="Feishu is not configured")

    _LOG.info("Feishu webhook received: keys=%s", sorted(payload.keys()))
    try:
        clear_payload = feishu_adapter.decode_payload(payload)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("Invalid Feishu webhook payload: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid Feishu payload") from exc

    if not feishu_adapter.is_valid_token(clear_payload):
        raise HTTPException(status_code=403, detail="Invalid Feishu token")

    challenge = feishu_adapter.challenge_response(clear_payload)
    if challenge is not None:
        _LOG.info("Feishu webhook url_verification accepted")
        return challenge

    incoming = feishu_adapter.parse_incoming(clear_payload)
    if incoming is None:
        _LOG.info(
            "Feishu webhook ignored: event_type=%s",
            clear_payload.get("header", {}).get("event_type"),
        )
        return {"code": 0, "msg": "ignored"}
    if not feishu_adapter.is_allowed(incoming):
        _LOG.warning("Feishu webhook blocked by whitelist: user=%s", incoming.user_id)
        return {"code": 0, "msg": "ignored"}

    if not _remember_feishu_event_id(feishu_adapter.event_id(clear_payload)):
        _LOG.info("Feishu webhook duplicate ignored: event_id=%s", feishu_adapter.event_id(clear_payload))
        if incoming.card_action is not None:
            return feishu_adapter.card_callback_ack(clear_payload, duplicate=True)
        return {"code": 0, "msg": "duplicate"}

    _LOG.info(
        "Feishu webhook accepted: user=%s chat=%s text=%s",
        incoming.user_id,
        incoming.chat_id,
        incoming.text,
    )
    # Persist resource identifiers before acknowledging the webhook. Nearby
    # uploads/instructions become one turn; card actions bypass the grace period.
    message_service.attachment_store.remember(incoming)
    feishu_message_batcher.submit(incoming)
    if incoming.card_action is not None:
        return feishu_adapter.card_callback_ack(clear_payload)
    return {"code": 0, "msg": "success"}
