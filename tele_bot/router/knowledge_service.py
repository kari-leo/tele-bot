"""Explicit `/kb` command surface and user/chat-bound confirmation gate."""

from __future__ import annotations

import time
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from secrets import token_urlsafe
from threading import Lock

from tele_bot.channels.feishu import FeishuWebhookAdapter
from tele_bot.knowledge.ingestion import DocumentParser, ParsedDocument
from tele_bot.knowledge.tables import TableReadOptions
from tele_bot.knowledge.models import DocumentSource, MemberRole
from tele_bot.knowledge.search import KnowledgeIndexer, KnowledgeSearch
from tele_bot.knowledge.sources import KnowledgeSourceResolver
from tele_bot.knowledge.cloud_sources import CloudSourceConnector, canonical, parse_cloud_url, normalize_selection
from tele_bot.knowledge.feishu_docs import CloudAPIError
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
    project_space_id: str | None = None


@dataclass(frozen=True)
class LocalDocumentSnapshot:
    path: str
    parsed: ParsedDocument


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
        docs_connector: CloudSourceConnector | None = None,
    ) -> None:
        self.store, self.parser, self.indexer, self.search = store, parser, indexer, search
        self.policy, self.feishu_adapter = policy, feishu_adapter
        self.source_resolver = KnowledgeSourceResolver(policy)
        self.confirmation_ttl_seconds = confirmation_ttl_seconds
        self.creator_user_ids = frozenset(creator_user_ids)
        self.docs_connector = docs_connector
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
        except (ValueError, PermissionError, RuntimeError, KeyError, OSError) as exc:
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
            lines = ["知识库命中（检索片段，不代表整表完整结果，不能直接用于全表计数/求和）："]
            for hit in hits:
                excerpt = hit.text.replace("\n", " ")[:240]
                section = " / ".join(hit.section_path) or "无标题章节"
                lines.append(f"- {hit.rank}. {hit.title}（{section}，片段 {hit.ordinal + 1}，{hit.score:.3f}）：{excerpt}\n"
                             f"  来源：{hit.source_url or hit.source_ref} {hit.source_locator}；版本：{hit.document_version_id}；索引时间：{hit.indexed_at}")
                if hit.source_status:
                    lines.append(f"  最近成功读取：{hit.source_read_at}；源状态：{hit.source_status}；源 revision：{hit.source_revision}；缺失/失败：{hit.source_error or '无'}")
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
            hint, _, configuration = argument.partition(" --table-options ")
            options = self._table_options(configuration)
            selection = self.source_resolver.resolve(hint, bound_directory=space.allowed_directory)
            snapshots, descriptions, total = [], [], 0
            for path in selection.files:
                parsed = self.parser.parse_file(str(path), policy=self.policy,
                                                bound_directory=space.allowed_directory, table_options=options)
                total += len(parsed.text)
                self.parser.table_limits.check(total, self.parser.table_limits.max_output_chars, "批次预览输出字符数")
                snapshots.append(LocalDocumentSnapshot(str(path), parsed))
                descriptions.append(self._describe_document(parsed))
            file_list = "\n".join(descriptions)
            summary = (
                f"项目空间：{space.name}（{space.id}）\n从目录批量导入 {len(selection.files)} 个文档：\n"
                f"{selection.directory}\n{file_list}"
                f"\n读取配置：{json.dumps(configuration and json.loads(configuration) or {}, ensure_ascii=False)}"
                "\n确认后将把以上内容作为当前知识库的副本，供空间成员读取。"
            )
            return self._preview(
                message,
                "import_files",
                tuple(snapshots),
                summary,
            )
        if verb == "import-doc":
            self.store.require_member(message.chat_id, message.user_id, admin=True)
            url, _, configuration = argument.partition(" --options ")
            return self._prepare_cloud(message,url,json.loads(configuration) if configuration else {})
        if verb == "inspect-doc":
            self.store.require_member(message.chat_id,message.user_id,admin=True)
            if self.docs_connector is None:
                raise RuntimeError("飞书 Docs 连接器未配置")
            try:
                data=self.docs_connector.inspect(argument)
            except CloudAPIError as exc:
                parsed=parse_cloud_url(argument)
                for source in self.store.list_cloud_sources(message.chat_id,message.user_id):
                    stored=json.loads(source["resource_json"])
                    if source["entry_url"]==argument or (stored["kind"]==parsed.kind and stored["token"]==parsed.token):
                        self.store.cloud_failure(message.chat_id,message.user_id,source["source_ref"],exc.kind,str(exc))
                raise
            return self._out(message,json.dumps(data,ensure_ascii=False,indent=2))
        if verb == "sources":
            sources=self.store.list_cloud_sources(message.chat_id,message.user_id)
            return self._out(message,"\n".join(
                f"- {s['source_ref']} [{s['status']}] {s['actual_url']}\n"
                f"  范围：{s['selection_json']}；最近成功读取：{s['last_success_at'] or '无'}；"
                f"revision：{s['remote_revision']}；原因：{s['error'] or '无'}" for s in sources) or "当前空间没有已确认的云来源。")
        if verb == "refresh":
            sources=self.store.list_cloud_sources(message.chat_id,message.user_id)
            matches=[s for s in sources if s["source_ref"]==argument or s["source_ref"].startswith(argument)] if argument else []
            if len(matches)!=1:
                raise ValueError("请用 /kb sources 查看并指定唯一完整来源 ID：/kb refresh <cloud:...>")
            source=matches[0]
            return self._prepare_cloud(message,source["entry_url"],json.loads(source["selection_json"]),expected_ref=source["source_ref"])
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
        space = self.store.require_member(message.chat_id, message.user_id, admin=True)
        if not message.message_id or len(message.attachments) != 1:
            raise ValueError("飞书附件缺少可下载标识")
        attachment = message.attachments[0]
        if self.feishu_adapter is None:
            raise RuntimeError("飞书附件下载未配置")
        raw = self.feishu_adapter.download_attachment(message.message_id, attachment,
                                                       max_bytes=self.parser.max_bytes)
        parsed = self.parser.parse_bytes(attachment.name or "upload.bin", raw, attachment.mime_type)
        return self._preview(
            message, "import_attachment", (attachment.key, parsed),
            f"项目空间：{space.name}（{space.id}）\n导入飞书附件：{attachment.name or attachment.key}\n"
            + self._describe_document(parsed) + "\n导入内容成为当前空间成员可读取的副本。"
        )

    def _prepare_cloud(self,message,url,selection,expected_ref=None):
        space=self.store.require_member(message.chat_id,message.user_id,admin=True)
        if self.docs_connector is None:
            raise RuntimeError("飞书 Docs 连接器未配置")
        # Validate the URL before any remote request. Resolve Wiki to the real token.
        original=parse_cloud_url(url)
        generations={s["source_ref"]:s["generation"] for s in self.store.list_cloud_sources(message.chat_id,message.user_id)}
        source_ref=expected_ref
        try:
            resource=self.docs_connector.resolve(url)
            selected=normalize_selection(resource,selection)
            source_ref=self.docs_connector.source_ref(resource,selected)
            if expected_ref and expected_ref!=source_ref:
                raise ValueError("来源实际资源/范围已改变，请用 import-doc 重新审阅新来源")
            snapshot=self.docs_connector.load(resource,selected)
        except CloudAPIError as exc:
            known=self.store.list_cloud_sources(message.chat_id,message.user_id)
            candidates=[source_ref] if source_ref else [s["source_ref"] for s in known if s["entry_url"]==original.entry_url]
            if exc.kind=="revoked" and source_ref:
                candidates.extend(s["source_ref"] for s in known if
                    (json.loads(s["resource_json"])["kind"],json.loads(s["resource_json"])["token"])==(resource.kind,resource.token))
            for ref in set(candidates):
                self.store.cloud_failure(message.chat_id,message.user_id,ref,exc.kind,str(exc))
            raise
        for ref in json.loads(snapshot.references_json):
            if ref.get("failure_kind"):
                self.store.cloud_failure(message.chat_id,message.user_id,ref["source_ref"],ref["failure_kind"],ref["reason"])
        descriptions=[self._describe_document(s.parsed)+f"\n实际来源：{self.docs_connector.actual_url(s.resource,json.loads(s.selection_json))}"
                      +f"\n快照读取时间：{s.read_at}；远端 revision：{s.revision or 'API 未提供'}"
                      for s in (snapshot,*snapshot.children)]
        summary=(f"项目空间：{space.name}（{space.id}）\n云来源：{url}\n读取范围：{snapshot.selection_json}\n"
                 +"\n".join(descriptions)+f"\n嵌入引用：{snapshot.references_json}\n"
                 "导入内容成为空间成员可读的副本，不继承源文档/视图的行列 ACL；未选择的引用不会自动读取。")
        return self._preview(message,"import_cloud",(snapshot,canonical(generations)),summary)

    @staticmethod
    def _table_options(configuration: str) -> TableReadOptions:
        if not configuration:
            return TableReadOptions()
        try:
            values = json.loads(configuration)
            if not isinstance(values, dict) or set(values) - {"header_rows", "delimiter", "encoding", "sheets", "cell_range"}:
                raise ValueError("unknown table option")
            if "header_rows" in values and (type(values["header_rows"]) is not int or values["header_rows"] < 0):
                raise ValueError("header_rows must be a nonnegative integer")
            if "sheets" in values:
                if not isinstance(values["sheets"], list) or any(not isinstance(s, str) for s in values["sheets"]):
                    raise ValueError("sheets must be a list of names")
                values["sheets"] = tuple(values["sheets"])
            for key in ("delimiter", "encoding", "cell_range"):
                if key in values and not isinstance(values[key], str):
                    raise ValueError(f"{key} must be text")
            return TableReadOptions(**values)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"无效 --table-options JSON：{exc}") from exc

    @staticmethod
    def _describe_document(parsed: ParsedDocument) -> str:
        lines = [f"- {parsed.name}：正文/规范化文本 {len(parsed.text)} 字符，表格 {len(parsed.tables)} 个"]
        for table in parsed.tables:
            lines.append(f"  - {table.title}；{table.locator()}；数据记录 {len(table.rows)}；表头源行 {table.header_rows}；"
                         + "字段：" + "；".join(c.name for c in table.columns))
        lines.extend(f"  缺失内容：{item}" for item in parsed.missing_content)
        lines.extend(f"  提示：{warning}" for warning in parsed.warnings)
        return "\n".join(lines)

    def _preview(
        self, message: IncomingMessage, operation: str, arguments: tuple[object, ...], summary: str
    ) -> OutgoingMessage:
        if len(summary.encode("utf-8")) > 24000:
            raise ValueError("预览范围/字段清单过长，请减少文件、工作表或列后重试；未生成确认操作")
        token = token_urlsafe(18)
        pending = PendingKnowledgeOperation(
            token, message.chat_id, message.user_id, operation, arguments, summary,
            time.time() + self.confirmation_ttl_seconds,
            self.store.get_space(message.chat_id, message.user_id).id if operation in {
                "import_files", "import_attachment", "import_doc", "import_cloud"
            } else None,
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
        if pending.project_space_id is not None:
            space = self.store.require_member(message.chat_id, message.user_id, admin=True)
            if space.id != pending.project_space_id:
                raise PermissionError("会话绑定空间已变化，请在目标空间重新生成导入预览")
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
            missing: list[str] = []
            for snapshot in args:
                raw_path = snapshot.path
                try:
                    # Revalidate current path/binding and byte identity; index exactly
                    # the parsed scope the administrator reviewed, never a new selection.
                    path = self.policy.resolve_existing(raw_path)
                    bound = self.policy.resolve_existing(space.allowed_directory or "")
                    try:
                        path.relative_to(bound)
                    except ValueError as exc:
                        raise PermissionError("导入文件已不在当前绑定目录内") from exc
                    with path.open("rb") as stream:
                        raw = stream.read(self.parser.max_bytes + 1)
                    if hashlib.sha256(raw).hexdigest() != snapshot.parsed.input_sha256:
                        raise ValueError("文件在预览后已变化，请重新导入并确认")
                    parsed = snapshot.parsed
                    missing.extend(parsed.missing_content)
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
            status = "完整成功" if not failures and not missing else ("部分成功" if indexed + skipped else "失败")
            result = f"批量导入{status}：已索引 {indexed}，未变化 {skipped}，失败 {len(failures)}。"
            if failures:
                result += "\n" + "\n".join(f"- {failure}" for failure in failures[:10])
            if missing:
                result += "\n缺失内容（可检索公式表达式，无法提供缓存结果）：\n" + "\n".join(missing)
            return self._out(message, result)
        if operation == "import_attachment":
            return self._run_import(message, DocumentSource.FEISHU_UPLOAD, str(args[0]), args[1])
        if operation == "import_cloud":
            root=args[0]
            generations=json.loads(args[1])
            indexed=unchanged=0
            failures=[]
            missing=list(root.parsed.missing_content)
            units={s.source_ref:s for s in (*root.children,root)}
            for snapshot in units.values():
                sources=self.store.list_cloud_sources(message.chat_id,message.user_id)
                previous=next((s for s in sources if s["source_ref"]==snapshot.source_ref),None)
                expected_generation=generations.get(snapshot.source_ref,0)
                if (previous["generation"] if previous else 0)!=expected_generation:
                    failures.append(f"{snapshot.parsed.name}：来源在预览后已有新状态，请重新预览")
                    continue
                actual_url=self.docs_connector.actual_url(snapshot.resource,json.loads(snapshot.selection_json))
                state="partial" if snapshot.parsed.missing_content else "indexed"
                error="; ".join(snapshot.parsed.missing_content) or None
                if snapshot is root and failures:
                    state="partial"
                    error="; ".join(failures+[error] if error else failures)
                cloud_state=dict(source_ref=snapshot.source_ref,
                    resource_json=canonical(asdict(snapshot.resource)),selection_json=snapshot.selection_json,
                    entry_url=snapshot.resource.entry_url,actual_url=actual_url,revision=snapshot.revision,
                    read_at=snapshot.read_at,status=state,error=error,
                    manifest_json=snapshot.references_json,expected_generation=expected_generation)
                try:
                    pending_index=self.indexer.enqueue(chat_id=message.chat_id,actor_user_id=message.user_id,
                        source=DocumentSource.FEISHU_DOCUMENT,source_ref=snapshot.source_ref,source_url=actual_url,
                        parsed=snapshot.parsed,defer_publish=True,cloud_state=cloud_state)
                    if not pending_index.changed:
                        if cloud_state["status"]!="partial":
                            cloud_state["status"]="unchanged"
                        self.store.save_cloud_source(message.chat_id,message.user_id,document_id=pending_index.document.id,**cloud_state)
                        unchanged+=1
                        continue
                    job=self.indexer.run(message.chat_id,message.user_id,pending_index)
                    if job is None or job.status.value!="succeeded":
                        cloud_state["status"]=job.status.value if job else "failed"
                        cloud_state["error"]=job.error if job else "索引作业未启动"
                        self.store.save_cloud_source(message.chat_id,message.user_id,document_id=pending_index.document.id,**cloud_state)
                        raise RuntimeError(cloud_state["error"])
                    indexed+=1
                except (PermissionError,RuntimeError,ValueError) as exc:
                    failures.append(f"{snapshot.parsed.name}：{exc}")
            status="失败" if failures and not indexed+unchanged else "部分成功" if failures or missing else "完整成功"
            unsupported=sum(any(word in item for word in ("未支持","暂不支持","未提取块类型")) for item in missing)
            result=f"云来源导入{status}：已索引 {indexed}，未变化 {unchanged}，未支持 {unsupported}，失败 {len(failures)}，缺失内容 {len(missing)}。"
            return self._out(message,result+("\n"+"\n".join(failures+missing) if failures or missing else ""))
        raise RuntimeError(f"unsupported pending operation: {operation}")

    def _run_import(self, message, source, source_ref, parsed, source_url=None):
        pending = self.indexer.enqueue(
            chat_id=message.chat_id, actor_user_id=message.user_id, source=source,
            source_ref=source_ref, parsed=parsed, source_url=source_url,
        )
        if not pending.changed:
            return self._out(message, "文档内容未变化，已跳过 Embedding。"
                             + ("\n部分内容仍缺失：\n" + "\n".join(parsed.missing_content) if parsed.missing_content else ""))
        job = self.indexer.run(message.chat_id, message.user_id, pending)
        if job is None or job.status.value != "succeeded":
            raise RuntimeError(job.error if job else "索引作业未启动")
        return self._out(message, f"文档已索引：{parsed.name}"
                         + ("\n部分成功，缺失内容：\n" + "\n".join(parsed.missing_content) if parsed.missing_content else ""))

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
            "表格可附加 --table-options JSON（header_rows、sheets、cell_range、delimiter、encoding）；"
            "/kb import-doc <docx/wiki/sheets/base 链接> [--options JSON]；/kb inspect-doc <链接>；"
            "/kb sources；/kb refresh <来源ID>；/kb search <问题>；/kb list；"
            "/kb add-member <user_id> [member|admin]；/kb delete <文档ID>；/kb budget"
        )
