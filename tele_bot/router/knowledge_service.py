"""Explicit `/kb` command surface and user/chat-bound confirmation gate."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from secrets import token_urlsafe
from threading import Lock

from tele_bot.channels.feishu import FeishuWebhookAdapter
from tele_bot.knowledge.ingestion import DocumentParser, parse_feishu_document_url
from tele_bot.knowledge.models import DocumentSource, MemberRole
from tele_bot.knowledge.search import KnowledgeIndexer, KnowledgeSearch
from tele_bot.knowledge.sources import KnowledgeSourceResolver
from tele_bot.models import CardAction, IncomingMessage, InteractiveCard, MessageType, OutgoingMessage
from tele_bot.persistence.kb_store import KnowledgeStore
from tele_bot.tools.workspace_policy import WorkspacePolicy


@dataclass(frozen=True)
class PendingKnowledgeOperation:
    token: str
    chat_id: str
    user_id: str
    operation: str
    arguments: tuple[object, ...]
    summary: str
    expires_at: float


class KnowledgeCommandService:
    def __init__(
        self,
        *,
        store: KnowledgeStore,
        parser: DocumentParser,
        indexer: KnowledgeIndexer,
        search: KnowledgeSearch,
        policy: WorkspacePolicy,
        feishu_adapter: FeishuWebhookAdapter | None = None,
        confirmation_ttl_seconds: int = 600,
        creator_user_ids: tuple[str, ...] = (),
    ) -> None:
        self.store, self.parser, self.indexer, self.search = store, parser, indexer, search
        self.policy, self.feishu_adapter = policy, feishu_adapter
        self.source_resolver = KnowledgeSourceResolver(policy)
        self.confirmation_ttl_seconds = confirmation_ttl_seconds
        self.creator_user_ids = frozenset(creator_user_ids)
        self._pending: dict[str, PendingKnowledgeOperation] = {}
        self._lock = Lock()

    def handle(self, message: IncomingMessage) -> OutgoingMessage | None:
        try:
            if message.card_action is not None:
                if message.card_action.action_id not in {"kb_confirm", "kb_cancel"}:
                    return None
                return self._handle_action(message, message.card_action)
            normalized = message.text.strip()
            if normalized.lower() in {"确认", "确认执行", "取消", "cancel"}:
                pending = self._single_pending(message.chat_id, message.user_id)
                if pending is None:
                    return None
                return self._complete(message, pending, normalized.lower() in {"取消", "cancel"})
            if message.message_type is MessageType.IMAGE:
                return self._out(message, "图片已收到，但一期不支持 OCR，不会进入知识库。")
            if message.message_type is MessageType.FILE:
                return self._prepare_attachment(message)
            if not normalized.lower().startswith("/kb"):
                return None
            return self._command(message, normalized[3:].strip())
        except (ValueError, PermissionError, RuntimeError, KeyError) as exc:
            return self._out(message, f"知识库操作失败：{exc}")

    def _command(self, message: IncomingMessage, command: str) -> OutgoingMessage:
        verb, _, argument = command.partition(" ")
        verb, argument = verb.lower(), argument.strip()
        if verb in {"help", ""}:
            return self._out(message, self._help())
        if verb == "whoami":
            return self._out(message, f"当前飞书用户标识：{message.user_id}")
        if verb == "create":
            if message.user_id not in self.creator_user_ids and "*" not in self.creator_user_ids:
                raise PermissionError(
                    "当前用户没有创建项目空间的权限；请由管理员在 "
                    "tele_bot/config/feishu/local.env 中配置 KB_CREATOR_USER_IDS"
                )
            space = self.store.create_space(message.chat_id, argument or "项目知识库", message.user_id)
            return self._out(message, f"项目空间已创建：{space.name}。你是管理员。")
        if verb == "spaces":
            spaces = self.store.list_accessible_spaces(message.user_id)
            text = "\n".join(
                f"- {space.id} {space.name}" for space in spaces
            ) or "你当前没有可访问的项目空间。"
            return self._out(message, text)
        if verb in {"use", "bind-space"}:
            spaces = self.store.list_accessible_spaces(message.user_id)
            matches = [
                space for space in spaces
                if space.id == argument or space.id.startswith(argument) or space.name == argument
            ]
            unique = {space.id: space for space in matches}
            if not unique:
                raise PermissionError("找不到该项目空间，或当前用户没有该空间的读取权限")
            if len(unique) > 1:
                raise ValueError("项目空间名称或 ID 前缀不唯一，请使用完整空间 ID")
            space = next(iter(unique.values()))
            return self._preview(
                message,
                "bind_space",
                (space.id,),
                f"将当前会话绑定到项目空间：{space.name}（{space.id}）",
            )
        if verb == "search":
            hits = self.search.search(message.chat_id, message.user_id, argument)
            if not hits:
                return self._out(message, "当前项目空间没有相关资料。")
            lines = ["知识库命中："]
            for hit in hits:
                excerpt = hit.text.replace("\n", " ")[:240]
                lines.append(f"- {hit.title}（{hit.source.value}，片段 {hit.ordinal + 1}，{hit.score:.3f}）：{excerpt}")
            return self._out(message, "\n".join(lines))
        if verb in {"list", "docs"}:
            documents = self.store.list_documents(message.chat_id, message.user_id)
            text = "\n".join(
                f"- {doc.id[:8]} {doc.title} [{doc.status.value}]" for doc in documents
            ) or "当前项目空间还没有文档。"
            return self._out(message, text)
        if verb == "budget":
            space = self.store.get_space(message.chat_id, message.user_id)
            tokens, cost = self.store.daily_embedding_usage(space.id)
            return self._out(message, f"今日 Embedding 用量：{tokens} tokens，估算 ￥{cost:.6f}。")
        if verb == "bind":
            self.store.require_member(message.chat_id, message.user_id, admin=True)
            path = self.policy.resolve_existing(argument)
            if not path.is_dir():
                raise ValueError("绑定目标必须是目录")
            return self._preview(message, "bind", (str(path),), f"绑定允许导入目录：{path}")
        if verb == "import":
            space = self.store.require_member(message.chat_id, message.user_id, admin=True)
            if not space.allowed_directory:
                raise ValueError("请先使用 /kb bind <目录> 绑定允许目录")
            selection = self.source_resolver.resolve(
                argument, bound_directory=space.allowed_directory
            )
            visible_files = [
                str(path.relative_to(selection.directory)) for path in selection.files[:20]
            ]
            remainder = len(selection.files) - len(visible_files)
            file_list = "\n".join(f"- {name}" for name in visible_files)
            if remainder:
                file_list += f"\n- ……另有 {remainder} 个文件"
            summary = (
                f"从目录批量导入 {len(selection.files)} 个文档：\n"
                f"{selection.directory}\n{file_list}"
            )
            return self._preview(
                message,
                "import_files",
                tuple(str(path) for path in selection.files),
                summary,
            )
        if verb == "import-doc":
            self.store.require_member(message.chat_id, message.user_id, admin=True)
            kind, document_id = parse_feishu_document_url(argument)
            if kind != "docx":
                raise ValueError("一期云文档导入仅支持 /docx/ 链接")
            return self._preview(
                message, "import_doc", (argument, document_id), f"导入飞书云文档：{document_id}"
            )
        if verb == "add-member":
            self.store.require_member(message.chat_id, message.user_id, admin=True)
            values = argument.split()
            if not values:
                raise ValueError("用法：/kb add-member <user_id> [member|admin]")
            role = MemberRole(values[1].lower()) if len(values) > 1 else MemberRole.MEMBER
            return self._preview(
                message, "add_member", (values[0], role), f"添加成员 {values[0]}，角色 {role.value}"
            )
        if verb == "delete":
            self.store.require_member(message.chat_id, message.user_id, admin=True)
            if not argument:
                raise ValueError("用法：/kb delete <完整文档 ID>")
            return self._preview(message, "delete", (argument,), f"从检索中删除文档：{argument}")
        if verb == "confirm":
            pending = self._take(argument, message.chat_id, message.user_id)
            return self._execute(message, pending)
        if verb == "cancel":
            pending = self._take(argument, message.chat_id, message.user_id)
            return self._cancel(message, pending)
        raise ValueError("未知命令。发送 /kb help 查看用法")

    def _prepare_attachment(self, message: IncomingMessage) -> OutgoingMessage:
        self.store.require_member(message.chat_id, message.user_id, admin=True)
        if not message.message_id or len(message.attachments) != 1:
            raise ValueError("飞书附件缺少可下载标识")
        attachment = message.attachments[0]
        return self._preview(
            message, "import_attachment", (message.message_id, attachment),
            f"导入飞书附件：{attachment.name or attachment.key}"
        )

    def _preview(
        self, message: IncomingMessage, operation: str, arguments: tuple[object, ...], summary: str
    ) -> OutgoingMessage:
        token = token_urlsafe(18)
        pending = PendingKnowledgeOperation(
            token, message.chat_id, message.user_id, operation, arguments, summary,
            time.time() + self.confirmation_ttl_seconds,
        )
        with self._lock:
            self._purge_expired()
            self._pending[token] = pending
        card = InteractiveCard(
            title="知识库操作确认", body=summary, status="pending",
            actions=(
                CardAction("kb_confirm", "确认", token),
                CardAction("kb_cancel", "取消", token),
            ),
        )
        return OutgoingMessage(
            message.channel, message.chat_id,
            f"{summary}\n确认请回复“确认”，取消请回复“取消”。", False,
            reply_to_message_id=message.message_id, card=card,
        )

    def _handle_action(self, message: IncomingMessage, action: CardAction) -> OutgoingMessage:
        if action.action_id not in {"kb_confirm", "kb_cancel"}:
            raise ValueError("未知卡片动作")
        pending = self._take(action.value, message.chat_id, message.user_id)
        return self._cancel(message, pending) if action.action_id == "kb_cancel" else self._execute(message, pending)

    def _complete(
        self, message: IncomingMessage, pending: PendingKnowledgeOperation, cancelled: bool
    ) -> OutgoingMessage:
        pending = self._take(pending.token, message.chat_id, message.user_id)
        return self._cancel(message, pending) if cancelled else self._execute(message, pending)

    def _execute(self, message: IncomingMessage, pending: PendingKnowledgeOperation) -> OutgoingMessage:
        operation, args = pending.operation, pending.arguments
        if operation == "bind":
            self.store.bind_directory(message.chat_id, message.user_id, str(args[0]))
            return self._out(message, "目录绑定完成。")
        if operation == "bind_space":
            space = self.store.bind_space(message.chat_id, message.user_id, str(args[0]))
            return self._out(message, f"当前会话已绑定项目空间：{space.name}。")
        if operation == "add_member":
            self.store.add_member(message.chat_id, message.user_id, str(args[0]), args[1])
            return self._out(message, "成员变更完成。")
        if operation == "delete":
            self.store.delete_document(message.chat_id, message.user_id, str(args[0]))
            return self._out(message, "文档已从检索中删除。")
        if operation == "import_files":
            space = self.store.require_member(message.chat_id, message.user_id, admin=True)
            indexed = skipped = 0
            failures: list[str] = []
            for raw_path in args:
                try:
                    parsed = self.parser.parse_file(
                        str(raw_path),
                        policy=self.policy,
                        bound_directory=space.allowed_directory or "",
                    )
                    pending_index = self.indexer.enqueue(
                        chat_id=message.chat_id,
                        actor_user_id=message.user_id,
                        source=DocumentSource.BOUND_DIRECTORY,
                        source_ref=str(Path(str(raw_path)).resolve()),
                        parsed=parsed,
                    )
                    if not pending_index.changed:
                        skipped += 1
                        continue
                    job = self.indexer.run(message.chat_id, message.user_id, pending_index)
                    if job is None or job.status.value != "succeeded":
                        raise RuntimeError(job.error if job else "索引作业未启动")
                    indexed += 1
                except (ValueError, PermissionError, RuntimeError, OSError) as exc:
                    failures.append(f"{Path(str(raw_path)).name}: {exc}")
            result = f"批量导入完成：已索引 {indexed}，未变化 {skipped}，失败 {len(failures)}。"
            if failures:
                result += "\n" + "\n".join(f"- {failure}" for failure in failures[:10])
            return self._out(message, result)
        if operation == "import_attachment":
            if self.feishu_adapter is None:
                raise RuntimeError("飞书附件下载未配置")
            raw = self.feishu_adapter.download_attachment(str(args[0]), args[1])
            parsed = self.parser.parse_bytes(args[1].name or "upload.bin", raw, args[1].mime_type)
            return self._run_import(message, DocumentSource.FEISHU_UPLOAD, args[1].key, parsed)
        if operation == "import_doc":
            if self.feishu_adapter is None:
                raise RuntimeError("飞书云文档读取未配置")
            content, _revision = self.feishu_adapter.fetch_cloud_document(str(args[1]))
            parsed = self.parser.parse_bytes(f"{args[1]}.md", content.encode("utf-8"), "text/markdown")
            return self._run_import(
                message, DocumentSource.FEISHU_DOCUMENT, str(args[1]), parsed, source_url=str(args[0])
            )
        raise RuntimeError(f"unsupported pending operation: {operation}")

    def _run_import(self, message, source, source_ref, parsed, source_url=None):
        pending = self.indexer.enqueue(
            chat_id=message.chat_id, actor_user_id=message.user_id, source=source,
            source_ref=source_ref, parsed=parsed, source_url=source_url,
        )
        if not pending.changed:
            return self._out(message, "文档内容未变化，已跳过 Embedding。")
        job = self.indexer.run(message.chat_id, message.user_id, pending)
        if job is None or job.status.value != "succeeded":
            raise RuntimeError(job.error if job else "索引作业未启动")
        return self._out(message, f"文档已索引：{parsed.name}")

    def _take(self, token: str, chat_id: str, user_id: str) -> PendingKnowledgeOperation:
        with self._lock:
            self._purge_expired()
            pending = self._pending.get(token)
            if pending is None or pending.chat_id != chat_id or pending.user_id != user_id:
                raise PermissionError("确认令牌无效、过期或不属于当前用户/会话")
            del self._pending[token]
        return pending

    def _single_pending(self, chat_id: str, user_id: str) -> PendingKnowledgeOperation | None:
        with self._lock:
            self._purge_expired()
            matches = [p for p in self._pending.values() if p.chat_id == chat_id and p.user_id == user_id]
        return matches[0] if len(matches) == 1 else None

    def _purge_expired(self) -> None:
        now = time.time()
        for token in [token for token, pending in self._pending.items() if pending.expires_at <= now]:
            del self._pending[token]

    def _cancel(self, message, pending):
        return self._out(message, f"已取消：{pending.summary}")

    @staticmethod
    def _out(message: IncomingMessage, text: str) -> OutgoingMessage:
        return OutgoingMessage(
            message.channel, message.chat_id, text,
            reply_to_message_id=message.message_id,
        )

    @staticmethod
    def _help() -> str:
        return (
            "/kb whoami；/kb create [名称]；/kb spaces；/kb use <空间ID或名称>；"
            "/kb bind <目录>；/kb import <文件、目录或模糊目录描述>；"
            "/kb import-doc <飞书 docx 链接>；/kb search <问题>；/kb list；"
            "/kb add-member <user_id> [member|admin]；/kb delete <文档ID>；/kb budget"
        )
