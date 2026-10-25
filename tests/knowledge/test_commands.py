"""Knowledge command authorization, confirmation, local/attachment imports and cloud refresh."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import Mock

import httpx

from tele_bot.knowledge.config import RetrievalSettings
from tele_bot.knowledge.ingestion import DocumentParser
from tele_bot.knowledge.models import DocumentSource
from tele_bot.models import (
    AttachmentDescriptor,
    CardAction,
    MessageType,
)
from tests.knowledge.cloud_fakes import (
    BASE_SELECTION,
    SHEET_SELECTION,
    FakeCloudAPI,
    connector,
    text_block,
)
from tests.knowledge.support import KnowledgeServiceCase, workbook_bytes


class ProjectSpacePermissionTests(KnowledgeServiceCase):
    def test_confirmation_token_is_bound_to_user_and_chat(self):
        self.service.handle(self.message("admin", "/kb create Team"))
        preview = self.service.handle(self.message("admin", f"/kb bind {self.docs}"))
        token = preview.card.actions[0].value
        denied = self.service.handle(
            self.message(
                "attacker",
                "kb_confirm",
                message_type=MessageType.INTERACTIVE,
                card_action=CardAction("kb_confirm", "确认", token),
            )
        )
        self.assertIn("不属于", denied.text)
        confirmed = self.service.handle(
            self.message(
                "admin",
                "kb_confirm",
                message_type=MessageType.INTERACTIVE,
                card_action=CardAction("kb_confirm", "确认", token),
            )
        )
        self.assertIn("绑定完成", confirmed.text)

    def test_admin_commands_deny_members_before_io(self):
        self.service.handle(self.message("admin", "/kb create Team"))
        self.store.add_member("chat", "admin", "member")
        self.service.docs_connector = Mock()
        client = self.service.indexer.client
        url = "https://team.feishu.cn/docx/doc1"
        for command in (
            f"/kb bind {self.docs}",
            f"/kb import {self.docs}",
            f"/kb import-doc {url}",
            f"/kb inspect-doc {url}",
            "/kb sources",
            "/kb refresh cloud:",
        ):
            with self.subTest(command=command):
                response = self.service.handle(self.message("member", command))
                self.assertIn("管理员", response.text)
                self.assertIsNone(response.card)
                self.assertEqual(self.service.docs_connector.mock_calls, [])
                self.assertEqual(client.calls, [])

    def test_fuzzy_directory_import_previews_then_indexes_all_documents(self):
        (self.docs / "one.md").write_text("alpha", encoding="utf-8")
        (self.docs / "two.txt").write_text("beta", encoding="utf-8")
        (self.docs / "skip.png").write_bytes(b"png")
        self.service.handle(self.message("admin", "/kb create Team"))
        bind = self.service.handle(self.message("admin", f"/kb bind {self.docs}"))
        self.service.handle(
            self.message(
                "admin",
                "kb_confirm",
                message_type=MessageType.INTERACTIVE,
                card_action=bind.card.actions[0],
            )
        )

        preview = self.service.handle(
            self.message("admin", "/kb import 将docs目录里的文档全部导入")
        )
        self.assertIn("批量导入 2 个文档", preview.text)
        completed = self.service.handle(
            self.message(
                "admin",
                "kb_confirm",
                message_type=MessageType.INTERACTIVE,
                card_action=preview.card.actions[0],
            )
        )

        self.assertIn("已索引 2", completed.text)
        self.assertEqual(len(self.store.list_documents("chat", "admin")), 2)

    def test_space_member_can_bind_same_space_in_another_chat(self):
        created = self.service.handle(self.message("admin", "/kb create Shared"))
        self.assertIn("已创建", created.text)
        self.store.add_member("chat", "admin", "member")
        space = self.store.require_member("chat", "admin")

        preview = self.service.handle(
            self.message("member", f"/kb use {space.id}", chat_id="chat-two")
        )
        confirmed = self.service.handle(
            self.message(
                "member",
                "kb_confirm",
                chat_id="chat-two",
                message_type=MessageType.INTERACTIVE,
                card_action=preview.card.actions[0],
            )
        )

        self.assertIn("已绑定项目空间", confirmed.text)
        self.assertEqual(self.store.require_member("chat-two", "member").id, space.id)
        with self.assertRaises(PermissionError):
            self.store.require_member("chat-two", "stranger")

    def test_unlisted_user_cannot_create_space(self):
        response = self.service.handle(self.message("member", "/kb create Forbidden"))
        self.assertIn("没有创建项目空间的权限", response.text)

    def test_whoami_is_available_before_space_binding(self):
        response = self.service.handle(
            self.message("future-creator", "/kb whoami", chat_id="unbound-chat")
        )
        self.assertIn("future-creator", response.text)


class TableImportTests(KnowledgeServiceCase):
    """Reuse the real service/store and offline embedding fixture."""

    def setUp(self):
        super().setUp()
        self.service.handle(self.message("admin", "/kb create Team"))
        bind = self.service.handle(self.message("admin", f"/kb bind {self.docs}"))
        self.confirm(bind)
        self.client = Mock(wraps=self.service.indexer.client)
        self.service.indexer.client = self.client
        self.service.search.retrieval = RetrievalSettings(mode="keyword")

    def test_scan_preview_confirm_search_and_no_embedding_on_reimport(self):
        (self.docs / "data.csv").write_text(
            "项目,风险说明\nAlpha,接口联调延期", encoding="utf-8"
        )
        (self.docs / "data.xlsx").write_bytes(
            workbook_bytes(
                lambda w: [
                    w.active.append(row) for row in (("项目", "数量"), ("Beta", 0))
                ]
            )
        )
        preview = self.service.handle(self.message("admin", f"/kb import {self.docs}"))
        self.assertIn("批量导入 2 个文档", preview.text)
        self.assertIn("风险说明", preview.text)
        self.assertIn("数据记录 1", preview.text)
        self.assertFalse(self.client.embed.called)
        self.assertIn("已索引 2", self.confirm(preview).text)
        hits = self.service.search.search("chat", "admin", "接口联调延期")
        self.assertIn("风险说明：接口联调延期", hits[0].text)
        self.assertIn("record:2", hits[0].source_locator)
        self.assertTrue(hits[0].document_version_id)
        calls = self.client.embed.call_count
        preview = self.service.handle(self.message("admin", f"/kb import {self.docs}"))
        self.assertIn("未变化 2", self.confirm(preview).text)
        self.assertEqual(calls, self.client.embed.call_count)
        with self.assertRaises(PermissionError):
            self.service.search.search("chat", "stranger", "Alpha")

    def test_file_change_and_space_rebind_invalidate_confirmation(self):
        path = self.docs / "a.csv"
        path.write_text("name,value\nAlpha,1", encoding="utf-8")
        preview = self.service.handle(self.message("admin", f"/kb import {path}"))
        path.write_text("name,value\nBeta,2", encoding="utf-8")
        result = self.confirm(preview)
        self.assertIn("预览后已变化", result.text)
        self.assertFalse(self.client.embed.called)
        preview = self.service.handle(self.message("admin", f"/kb import {path}"))
        self.store.create_space("chat", "Other", "admin")
        self.assertIn("绑定空间已变化", self.confirm(preview).text)
        self.assertFalse(self.client.embed.called)

    def test_local_options_are_bound_and_only_selected_sheet_is_indexed(self):
        def fill(w):
            w.active.title = "Selected"
            w.active.append(["Alpha", 0])
            w.create_sheet("Private").append(["Secret", 9])

        path = self.docs / "a.xlsx"
        path.write_bytes(workbook_bytes(fill))
        preview = self.service.handle(
            self.message(
                "admin",
                f'/kb import {path} --table-options {{"header_rows":0,"sheets":["Selected"]}}',
            )
        )
        self.assertIn("Selected", preview.text)
        self.assertNotIn("Private", preview.text)
        self.assertIn("已索引 1", self.confirm(preview).text)
        self.assertTrue(self.service.search.search("chat", "admin", "Alpha"))
        self.assertFalse(self.service.search.search("chat", "admin", "Secret"))

    def test_attachment_download_preview_and_confirmation_share_snapshot(self):
        self.service.feishu_adapter = Mock()
        self.service.feishu_adapter.download_attachment.return_value = (
            b"name,value\nAlpha,0"
        )
        attachment = AttachmentDescriptor(MessageType.FILE, "file-token", "a.csv")
        preview = self.service.handle(
            self.message(
                "admin",
                "/kb import-attachment",
                message_type=MessageType.FILE,
                message_id="message-id",
                attachments=(attachment,),
            )
        )
        self.assertIn("数据记录 1", preview.text)
        self.assertFalse(self.client.embed.called)
        self.assertIn("已索引", self.confirm(preview).text)
        self.service.feishu_adapter.download_attachment.assert_called_once()
        hits = self.service.search.search("chat", "admin", "Alpha")
        self.assertEqual(hits[0].source, DocumentSource.FEISHU_UPLOAD)

    def test_location_only_refresh_updates_citations_and_reuses_vectors(self):
        original = DocumentParser().parse_bytes("a.csv", b"name,value\nAlpha,0")
        shifted = DocumentParser().parse_bytes("a.csv", b"\nname,value\nAlpha,0")
        self.assertEqual(original.text, shifted.text)
        for parsed in (original, shifted):
            pending = self.service.indexer.enqueue(
                chat_id="chat",
                actor_user_id="admin",
                source=DocumentSource.REPORT,
                source_ref="stable",
                parsed=parsed,
            )
            self.assertTrue(pending.changed)
            job = self.service.indexer.run("chat", "admin", pending)
            self.assertEqual(job.status.value, "succeeded")
        self.assertEqual(self.client.embed.call_count, 1)
        hits = self.service.search.search("chat", "admin", "Alpha")
        self.assertIn("record:3", hits[0].source_locator)

    def test_preview_validation_failure_does_not_index_any_file(self):
        (self.docs / "a.csv").write_text("Name,Value\nAlpha,1", encoding="utf-8")
        (self.docs / "b.csv").write_text('Name,Value\n"broken', encoding="utf-8")
        preview = self.service.handle(self.message("admin", f"/kb import {self.docs}"))
        self.assertIsNone(preview.card)
        self.assertIn("CSV 解析失败", preview.text)
        self.assertEqual(self.store.list_documents("chat", "admin"), [])
        self.assertFalse(self.client.embed.called)

    def test_missing_formula_cache_is_partial_success_in_preview_and_result(self):
        def fill(w):
            w.active.append(["名称", "结果"])
            w.active.append(["Alpha", "=1+1"])

        path = self.docs / "a.xlsx"
        path.write_bytes(workbook_bytes(fill))
        preview = self.service.handle(self.message("admin", f"/kb import {path}"))
        self.assertIn("公式缺失缓存", preview.text)
        self.assertIn("部分成功", self.confirm(preview).text)
        hit = self.service.search.search("chat", "admin", "Alpha")[0]
        self.assertIn("缺失缓存，未重算", hit.text)


class CloudServiceTests(KnowledgeServiceCase):
    def setUp(self):
        super().setUp()
        self.service.handle(self.message("admin", "/kb create Team"))
        self.api = FakeCloudAPI()
        self.service.docs_connector = connector(self.api)
        self.service.search.retrieval = RetrievalSettings(mode="keyword")
        self.client = Mock(wraps=self.service.indexer.client)
        self.service.indexer.client = self.client

    def prepare(self, url="https://team.feishu.cn/docx/doc1", selection=None):
        return self.service.handle(
            self.message(
                "admin",
                f"/kb import-doc {url}"
                + (" --options " + json.dumps(selection) if selection else ""),
            )
        )

    def test_preview_index_retrieve_wiki_dedup_refresh_and_revision_separation(self):
        preview = self.prepare()
        self.assertIn("数量", preview.text)
        self.assertFalse(self.client.embed.called)
        self.assertIn("完整成功", self.confirm(preview).text)
        hits = self.service.search.search("chat", "admin", "Alpha")
        self.assertTrue(hits)
        self.assertIn("/docx/doc1", hits[0].source_url)
        count = self.client.embed.call_count
        self.assertIn(
            "未变化 1",
            self.confirm(self.prepare("https://team.feishu.cn/wiki/wiki1")).text,
        )
        self.assertEqual(len(self.store.list_documents("chat", "admin")), 1)
        self.assertEqual(self.client.embed.call_count, count)
        source = self.store.list_cloud_sources("chat", "admin")[0]
        self.api.revision = 8
        preview = self.service.handle(
            self.message("admin", "/kb refresh " + source["source_ref"])
        )
        self.assertIn("未变化 1", self.confirm(preview).text)
        source = self.store.list_cloud_sources("chat", "admin")[0]
        self.assertEqual(source["remote_revision"], "8")
        version = self.store._conn.execute(
            "SELECT source_version FROM document_versions"
        ).fetchone()[0]
        self.assertNotEqual(version, "8")
        self.assertEqual(self.client.embed.call_count, count)

    def test_transient_refresh_keeps_previous_content_but_revocation_disables_search(
        self,
    ):
        self.confirm(self.prepare())
        source = self.store.list_cloud_sources("chat", "admin")[0]
        old_preview = self.prepare()
        endpoint = "/open-apis/docx/v1/documents/doc1"
        self.api.overrides[endpoint] = httpx.Response(500)
        failed = self.service.handle(
            self.message("admin", "/kb refresh " + source["source_ref"])
        )
        self.assertIsNone(failed.card)
        self.assertTrue(self.service.search.search("chat", "admin", "Alpha"))
        self.api.overrides[endpoint] = httpx.Response(403)
        self.service.handle(
            self.message("admin", "/kb refresh " + source["source_ref"])
        )
        self.assertEqual(self.service.search.search("chat", "admin", "Alpha"), [])
        self.assertIn("新状态", self.confirm(old_preview).text)
        self.assertEqual(self.service.search.search("chat", "admin", "Alpha"), [])

    def test_failed_embedding_refresh_keeps_current_serving_version(self):
        self.confirm(self.prepare())
        original = self.store.list_chunks("chat", "admin")[0].document_version_id
        self.api.blocks[2] = text_block("intro", "新的说明")
        self.client.embed.side_effect = RuntimeError("offline embedding failure")
        completed = self.confirm(self.prepare())
        self.assertIn("失败", completed.text)
        self.assertEqual(
            self.store.list_chunks("chat", "admin")[0].document_version_id, original
        )
        self.assertTrue(self.service.search.search("chat", "admin", "接口联调延期"))

    def test_base_complete_empty_snapshot_removes_records_only_after_confirmation(self):
        url = "https://team.feishu.cn/base/app1"
        self.confirm(self.prepare(url, BASE_SELECTION))
        self.api.records = []
        preview = self.prepare(url, BASE_SELECTION)
        self.assertTrue(self.service.search.search("chat", "admin", "Alpha"))
        self.confirm(preview)
        self.assertFalse(self.service.search.search("chat", "admin", "Alpha"))

    def test_scope_changes_create_separate_sources(self):
        url = "https://team.feishu.cn/sheets/spread1"
        self.confirm(self.prepare(url, SHEET_SELECTION))
        self.confirm(self.prepare(url, {"sheet_id": "sheet1", "range": "A1:B2"}))
        self.assertEqual(len(self.store.list_cloud_sources("chat", "admin")), 2)

    def test_partial_embed_does_not_claim_whole_document_success(self):
        self.api.blocks[0]["children"].append("embed")
        self.api.blocks.append(
            {"block_id": "embed", "block_type": 18, "bitable": {"token": "opaque"}}
        )
        preview = self.prepare()
        self.assertIn("未选择范围", preview.text)
        self.assertIn("部分成功", self.confirm(preview).text)
        self.assertEqual(
            self.store.list_cloud_sources("chat", "admin")[0]["status"], "partial"
        )

    def test_revocation_during_embedding_cannot_publish_or_reenable_source(self):
        self.confirm(self.prepare())
        source = self.store.list_cloud_sources("chat", "admin")[0]
        old_version = self.store.list_chunks("chat", "admin")[0].document_version_id
        self.api.blocks[2] = text_block("intro", "New revision notes")
        delegate = self.client._mock_wraps.embed

        def embed(texts):
            self.store.cloud_failure(
                "chat", "admin", source["source_ref"], "revoked", "permission revoked"
            )
            return delegate(texts)

        self.client.embed.side_effect = embed
        self.assertIn("新状态", self.confirm(self.prepare()).text)
        current = self.store._conn.execute(
            "SELECT current_version_id FROM knowledge_documents WHERE id=?",
            (source["document_id"],),
        ).fetchone()[0]
        self.assertEqual(current, old_version)
        self.assertEqual(
            self.store.list_cloud_sources("chat", "admin")[0]["status"], "revoked"
        )
        self.assertFalse(self.service.search.search("chat", "admin", "Alpha"))

    def test_budget_pause_keeps_previous_citations_and_successful_revision(self):
        self.confirm(self.prepare())
        self.api.revision = 8
        self.api.blocks[2] = text_block(
            "intro", "A long new paragraph requiring embedding"
        )
        self.service.indexer.settings = replace(
            self.service.indexer.settings, embedding_daily_token_budget=1
        )
        calls = self.client.embed.call_count
        result = self.confirm(self.prepare())
        self.assertIn("失败", result.text)
        self.assertEqual(self.client.embed.call_count, calls)
        hit = self.service.search.search("chat", "admin", "接口联调延期")[0]
        self.assertEqual(hit.source_status, "paused_budget")
        self.assertEqual(hit.source_revision, "7")
        self.assertTrue(hit.source_read_at)

    def test_known_resource_revocation_disables_other_selected_ranges(self):
        url = "https://team.feishu.cn/sheets/spread1"
        self.confirm(self.prepare(url, SHEET_SELECTION))
        self.confirm(self.prepare(url, {"sheet_id": "sheet1", "range": "A1:B2"}))
        self.api.overrides["/open-apis/sheets/v3/spreadsheets/spread1/sheets/query"] = (
            httpx.Response(403)
        )
        self.prepare(url, SHEET_SELECTION)
        self.assertTrue(
            all(
                s["status"] == "revoked"
                for s in self.store.list_cloud_sources("chat", "admin")
            )
        )
        self.assertFalse(self.service.search.search("chat", "admin", "Alpha"))

    def test_table_qa_evidence_multi_source_and_no_full_aggregation_claim(self):
        self.confirm(
            self.prepare("https://team.feishu.cn/sheets/spread1", SHEET_SELECTION)
        )
        self.confirm(self.prepare("https://team.feishu.cn/base/app1", BASE_SELECTION))
        from tele_bot.mcp.knowledge import KnowledgeMCPService

        space = self.store.get_space("chat", "admin")
        api = KnowledgeMCPService(self.store, self.service.search, "admin")
        result = api.search(space.id, "Alpha")
        self.assertEqual(len({h["source_ref"] for h in result["hits"]}), 2)
        for hit in result["hits"]:
            self.assertIn("Alpha", hit["text"])
            self.assertIn("0", hit["text"])
            self.assertTrue(hit["source_read_at"])
            self.assertTrue(hit["source_locator"])
        unsupported = api.search(space.id, "ZKQ999")
        self.assertEqual(unsupported["hits"], [])
        nearest = api.search(space.id, "不存在的星际项目ZKQ999")
        self.assertTrue(all("ZKQ999" not in hit["text"] for hit in nearest["hits"]))
        aggregate = api.search(space.id, "Alpha 数量 合计")
        self.assertEqual(aggregate["full_source_coverage"], "not_verified")
        self.assertEqual(aggregate["coverage"], "retrieved_chunks_only")

    def test_registry_persists_selection_across_connection_reopen(self):
        from pathlib import Path

        from tele_bot.persistence.kb_store import KnowledgeStore

        path = Path(self.temp.name) / "registry.sqlite"
        self.store.close()
        self.store = KnowledgeStore(path)
        self.store.create_space("chat", "Team", "admin")
        self.service.store = self.service.indexer.store = self.service.search.store = (
            self.store
        )
        self.confirm(self.prepare("https://team.feishu.cn/base/app1", BASE_SELECTION))
        self.store.close()
        self.store = KnowledgeStore(path)
        self.service.store = self.service.indexer.store = self.service.search.store = (
            self.store
        )
        saved = self.store.list_cloud_sources("chat", "admin")[0]
        self.assertEqual(
            json.loads(saved["selection_json"])["field_ids"],
            BASE_SELECTION["field_ids"],
        )
        self.assertIn(
            "未变化 1",
            self.confirm(
                self.service.handle(
                    self.message("admin", "/kb refresh " + saved["source_ref"])
                )
            ).text,
        )

    def test_revoked_source_stays_disabled_after_later_transient_or_index_failure(self):
        self.confirm(self.prepare())
        source = self.store.list_cloud_sources("chat", "admin")[0]
        self.api.overrides["/open-apis/docx/v1/documents/doc1"] = httpx.Response(403)
        self.service.handle(
            self.message("admin", "/kb refresh " + source["source_ref"])
        )
        self.api.overrides["/open-apis/docx/v1/documents/doc1"] = httpx.Response(500)
        self.service.handle(
            self.message("admin", "/kb refresh " + source["source_ref"])
        )
        self.assertEqual(
            self.store.list_cloud_sources("chat", "admin")[0]["status"], "revoked"
        )
        self.api.overrides.clear()
        self.api.blocks[2] = text_block("intro", "new protected facts")
        self.client.embed.side_effect = RuntimeError("embedding failed after read")
        self.confirm(self.prepare())
        self.assertEqual(
            self.store.list_cloud_sources("chat", "admin")[0]["status"], "revoked"
        )
        self.assertFalse(self.service.search.search("chat", "admin", "Alpha"))

    def test_local_delete_invalidates_older_cloud_confirmation(self):
        self.confirm(self.prepare())
        preview = self.prepare()
        source = self.store.list_cloud_sources("chat", "admin")[0]
        self.store.delete_document("chat", "admin", source["document_id"])
        self.assertIn("新状态", self.confirm(preview).text)
        self.assertFalse(self.service.search.search("chat", "admin", "Alpha"))
