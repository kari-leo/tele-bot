"""Feishu transport, schema-driven Base values and cloud source adapters."""

from __future__ import annotations

import unittest
from dataclasses import replace

import httpx

from tele_bot.knowledge.cloud_fields import FieldValueError, field_text
from tele_bot.knowledge.cloud_sources import parse_cloud_url
from tele_bot.knowledge.feishu_docs import CloudAPIError, FeishuDocsClient
from tele_bot.knowledge.ingestion import TextChunker
from tests.knowledge.cloud_fakes import (
    BASE_SELECTION,
    SHEET_SELECTION,
    FakeCloudAPI,
    connector,
    response,
    text_block,
)


class DocsClientTests(unittest.TestCase):
    def test_shared_read_budget_stops_requests_without_partial_success(self):
        api = FakeCloudAPI()
        client = connector(api, max_requests=2).client
        with client.read_scope():
            client.get("/open-apis/docx/v1/documents/doc1")
            calls = len(api.calls)
            with self.assertRaisesRegex(CloudAPIError, "预算耗尽"):
                client.get("/open-apis/docx/v1/documents/doc1")
            self.assertEqual(len(api.calls), calls)
        # A new explicit import gets a fresh independent budget.
        with client.read_scope():
            self.assertTrue(client.get("/open-apis/docx/v1/documents/doc1"))

    def test_official_origin_url_validation_and_secret_redaction(self):
        with self.assertRaises(ValueError):
            FeishuDocsClient("a", "secret", api_base_url="https://attacker.invalid")
        for url in (
            "https://user:pass@team.feishu.cn/docx/a",
            "http://team.feishu.cn/docx/a",
            "https://team.feishu.cn:444/docx/a",
            "https://team.feishu.cn.evil/docx/a",
            "https://team.feishu.cn/docs/old",
        ):
            with self.assertRaises(ValueError):
                parse_cloud_url(url)
        client = FeishuDocsClient(
            "a",
            "SECRET",
            request=lambda *a, **k: httpx.Response(
                200, json={"code": 9, "msg": "SECRET"}
            ),
        )
        with self.assertRaises(CloudAPIError) as caught:
            client.access_token()
        self.assertNotIn("SECRET", str(caught.exception))

    def test_auth_cache_limited_retry_and_error_classification(self):
        api = FakeCloudAPI()
        replies = iter(
            [httpx.Response(429, headers={"Retry-After": "0"}), response({"ok": True})]
        )
        api.overrides["/open-apis/test"] = lambda *a: next(replies)
        client = connector(api).client
        self.assertEqual(client.get("/open-apis/test"), {"ok": True})
        self.assertEqual(sum("tenant_access_token" in c[1] for c in api.calls), 1)
        for status, kind in ((403, "revoked"), (404, "not_found"), (401, "auth")):
            api.overrides["/open-apis/test"] = httpx.Response(status)
            with self.assertRaises(CloudAPIError) as caught:
                client.get("/open-apis/test")
            self.assertEqual(caught.exception.kind, kind)
        for code, kind in (
            (1770032, "revoked"),
            (1770003, "not_found"),
            (1770002, "not_found"),
            (91403, "revoked"),
            (90213, "revoked"),
            (90214, "not_found"),
            (90221, "limit"),
        ):
            api.overrides["/open-apis/test"] = response(status=400, code=code)
            with self.assertRaises(CloudAPIError) as caught:
                client.get("/open-apis/test")
            self.assertEqual(caught.exception.kind, kind)

    def test_complete_pagination_repeated_cursor_and_partial_failure(self):
        api = FakeCloudAPI()
        api.overrides["/open-apis/test"] = lambda m, p, b: (
            response({"items": [{"id": 2}], "has_more": False})
            if p.get("page_token")
            else response(
                {"items": [{"id": 1}], "has_more": True, "page_token": "next"}
            )
        )
        client = connector(api).client
        self.assertEqual(client.pages("/open-apis/test"), [{"id": 1}, {"id": 2}])
        api.overrides["/open-apis/test"] = response(
            {"items": [], "has_more": True, "page_token": "same"}
        )
        with self.assertRaisesRegex(CloudAPIError, "游标"):
            client.pages("/open-apis/test")
        api.overrides["/open-apis/test"] = lambda m, p, b: (
            httpx.Response(500)
            if p
            else response(
                {"items": [{"id": 1}], "has_more": True, "page_token": "next"}
            )
        )
        with self.assertRaises(CloudAPIError):
            client.pages("/open-apis/test")


class CloudConnectorTests(unittest.TestCase):
    def test_url_selected_sheet_range_and_base_view_are_not_silently_dropped(self):
        from tele_bot.knowledge.cloud_sources import normalize_selection

        sheet = normalize_selection(
            parse_cloud_url(
                "https://team.feishu.cn/sheets/spread1?sheet=sheet1&range=A1%3AB5"
            ),
            {},
        )
        self.assertEqual(
            sheet, {"sheet_id": "sheet1", "range": "A1:B5", "header_rows": 1}
        )
        base = normalize_selection(
            parse_cloud_url("https://team.feishu.cn/base/app1?table=tbl1&view=vew1"),
            {"field_ids": ["fldName"]},
        )
        self.assertEqual(base["view_id"], "vew1")
        self.assertEqual(base["table_id"], "tbl1")

    def setUp(self):
        self.api = FakeCloudAPI()
        self.connector = connector(self.api)

    def load(self, url, selection=None):
        return self.connector.load(self.connector.resolve(url), selection or {})

    def test_docx_ordinary_table_section_blocks_and_offsets(self):
        snapshot = self.load("https://team.feishu.cn/docx/doc1")
        self.assertIn("项目：Alpha；数量：0", snapshot.parsed.text)
        self.assertIn("接口联调延期", snapshot.parsed.text)
        self.assertEqual(len(snapshot.parsed.tables), 1)
        for chunk in TextChunker().split_document(snapshot.parsed):
            self.assertEqual(
                chunk.text, snapshot.parsed.text[chunk.start_char : chunk.end_char]
            )
            self.assertIn("doc1", chunk.source_locator)
        self.assertFalse(snapshot.parsed.missing_content)
        request = next(c for c in self.api.calls if c[1].endswith("/blocks"))
        self.assertEqual(request[2]["document_revision_id"], -1)

    def test_docx_all_blocks_paged_before_normalization(self):
        blocks = self.api.blocks
        self.api.overrides["/open-apis/docx/v1/documents/doc1/blocks"] = (
            lambda m, p, b: response(
                {"items": blocks[3:], "has_more": False}
                if p.get("page_token")
                else {"items": blocks[:3], "has_more": True, "page_token": "second"}
            )
        )
        self.assertIn(
            "Alpha", self.load("https://team.feishu.cn/docx/doc1").parsed.text
        )
        self.api.blocks = [b for b in blocks if b["block_id"] != "t4"]
        self.api.overrides.clear()
        with self.assertRaisesRegex(CloudAPIError, "缺少引用块"):
            self.load("https://team.feishu.cn/docx/doc1")

    def test_docx_multilevel_real_merge_headers(self):
        self.api.blocks = self.api.blocks[:4]
        table = self.api.blocks[3]["table"]
        table["cells"] = [f"mergedCell{i}" for i in range(9)]
        info = [{"row_span": 1, "col_span": 1} for _ in range(9)]
        info[0] = {"row_span": 2, "col_span": 1}
        info[1] = {"row_span": 1, "col_span": 2}
        info[2] = info[3] = {"row_span": 0, "col_span": 0}
        table["property"] = {"row_size": 3, "column_size": 3, "merge_info": info}
        for i, value in enumerate(
            ("项目", "金额（万元）", "", "", "预算", "实际", "Alpha", "0", "1")
        ):
            self.api.blocks.extend(
                [
                    {
                        "block_id": f"mergedCell{i}",
                        "block_type": 32,
                        "children": [f"mergedText{i}"],
                    },
                    text_block(f"mergedText{i}", value),
                ]
            )
        snapshot = self.load("https://team.feishu.cn/docx/doc1", {"header_rows": 2})
        columns = snapshot.parsed.tables[0].columns
        self.assertEqual(
            [c.name for c in columns],
            ["项目", "金额（万元） / 预算", "金额（万元） / 实际"],
        )
        self.assertIn("预算：0", snapshot.parsed.text)

    def test_embedded_over_limit_keeps_parent_and_reports_missing_source(self):
        self.api.blocks[0]["children"].append("embed")
        self.api.blocks.append(
            {
                "block_id": "embed",
                "block_type": 30,
                "sheet": {"token": "spread1_sheet1"},
            }
        )
        self.connector.limits = replace(self.connector.limits, max_rows=2)
        snapshot = self.load(
            "https://team.feishu.cn/docx/doc1",
            {
                "embeds": {
                    "embed": {
                        "url": "https://team.feishu.cn/sheets/spread1",
                        "selection": SHEET_SELECTION,
                    }
                }
            },
        )
        self.assertIn("接口联调延期", snapshot.parsed.text)
        self.assertTrue(snapshot.parsed.missing_content)
        self.assertFalse(snapshot.children)

    def test_wiki_dispatch_identity_matches_direct_and_wrong_type_is_explicit(self):
        direct = self.load("https://team.feishu.cn/docx/doc1")
        wiki = self.load("https://team.feishu.cn/wiki/wiki1")
        self.assertEqual(direct.source_ref, wiki.source_ref)
        self.assertEqual(direct.parsed.text, wiki.parsed.text)
        for kind, token, selection in (
            ("sheet", "spread1", SHEET_SELECTION),
            ("bitable", "app1", BASE_SELECTION),
        ):
            self.api.node = {"obj_type": kind, "obj_token": token}
            snapshot = self.load("https://team.feishu.cn/wiki/wiki1", selection)
            self.assertIn("Alpha", snapshot.parsed.text)
        self.api.node = {"obj_type": "mindnote", "obj_token": "other"}
        with self.assertRaisesRegex(ValueError, "暂不支持"):
            self.connector.resolve("https://team.feishu.cn/wiki/wiki1")

    def test_sheets_explicit_scope_batches_no_other_sheet_and_revision_guard(self):
        with self.assertRaisesRegex(ValueError, "必须明确"):
            self.load("https://team.feishu.cn/sheets/spread1")
        snapshot = self.load("https://team.feishu.cn/sheets/spread1", SHEET_SELECTION)
        self.assertEqual(
            [r.position for r in snapshot.parsed.tables[0].rows], [2, 3, 4, 5]
        )
        ranges = [
            c[1].split("/values/")[1] for c in self.api.calls if "/values/" in c[1]
        ]
        self.assertEqual(ranges, ["sheet1!A1:B2", "sheet1!A3:B4", "sheet1!A5:B5"])
        target = "/open-apis/sheets/v2/spreadsheets/spread1/values/sheet1!A3:B4"
        self.api.overrides[target] = response(
            {"revision": 8, "valueRange": {"range": "sheet1!A3:B4", "values": []}}
        )
        with self.assertRaisesRegex(CloudAPIError, "revision"):
            self.load("https://team.feishu.cn/sheets/spread1", SHEET_SELECTION)

    def test_sheets_merges_and_selected_range_cannot_cut_merge(self):
        self.api.sheets[0]["merges"] = [
            {
                "start_row_index": 1,
                "end_row_index": 3,
                "start_column_index": 0,
                "end_column_index": 1,
            }
        ]
        self.api.values[2][0] = None
        snapshot = self.load("https://team.feishu.cn/sheets/spread1", SHEET_SELECTION)
        self.assertEqual(snapshot.parsed.tables[0].rows[1].cells[0].value, "Alpha")
        with self.assertRaisesRegex(ValueError, "切断合并"):
            self.load(
                "https://team.feishu.cn/sheets/spread1",
                {"sheet_id": "sheet1", "range": "A1:B2"},
            )

    def test_base_fields_paginated_records_have_stable_record_and_field_identity(self):
        self.api.overrides[
            "/open-apis/bitable/v1/apps/app1/tables/tbl1/records/search"
        ] = lambda m, p, b: response(
            {"items": self.api.records[1:], "has_more": False}
            if p.get("page_token")
            else {"items": self.api.records[:1], "has_more": True, "page_token": "next"}
        )
        snapshot = self.load("https://team.feishu.cn/base/app1", BASE_SELECTION)
        self.assertEqual(
            [r.record_id for r in snapshot.parsed.tables[0].rows], ["rec1", "rec2"]
        )
        chunks = TextChunker().split_document(snapshot.parsed)
        self.assertTrue(
            all("record_id:" in c.source_locator and "fld" in c.text for c in chunks)
        )
        self.assertIn("数量 [fldQty]：0", chunks[0].text)
        search = next(c for c in self.api.calls if c[1].endswith("records/search"))
        self.assertEqual(search[3]["field_names"], ["项目", "数量"])
        self.assertFalse(snapshot.parsed.missing_content)

    def test_embedded_sources_only_read_explicit_exact_reference_mapping(self):
        self.api.blocks[0]["children"].append("embed")
        self.api.blocks.append(
            {
                "block_id": "embed",
                "block_type": 30,
                "sheet": {"token": "spread1_sheet1"},
            }
        )
        unselected = self.load("https://team.feishu.cn/docx/doc1")
        self.assertTrue(unselected.parsed.missing_content)
        self.assertFalse(any("/values/" in c[1] for c in self.api.calls))
        selection = {
            "embeds": {
                "embed": {
                    "url": "https://team.feishu.cn/sheets/spread1",
                    "selection": SHEET_SELECTION,
                }
            }
        }
        selected = self.load("https://team.feishu.cn/docx/doc1", selection)
        self.assertEqual(len(selected.children), 1)
        self.assertFalse(selected.parsed.missing_content)
        self.api.overrides["/open-apis/sheets/v3/spreadsheets/spread1/sheets/query"] = (
            httpx.Response(403)
        )
        failed = self.load("https://team.feishu.cn/docx/doc1", selection)
        self.assertTrue(failed.parsed.missing_content)
        self.assertIn("revoked", failed.references_json)
        self.assertIn("接口联调延期", failed.parsed.text)
        self.api.blocks[-1]["sheet"]["token"] = "unknown_opaque"
        with self.assertRaisesRegex(ValueError, "身份不一致"):
            self.load("https://team.feishu.cn/docx/doc1", selection)

    def test_base_unknown_fields_are_visible_even_when_empty(self):
        self.api.fields.append(
            {"field_id": "fldUnknown", "field_name": "未知", "type": 999}
        )
        selection = {**BASE_SELECTION, "field_ids": ["fldName", "fldUnknown"]}
        snapshot = self.load("https://team.feishu.cn/base/app1", selection)
        self.assertIn("未提取", snapshot.parsed.text)
        self.assertTrue(snapshot.parsed.missing_content)
        self.api.records = []
        empty = self.load("https://team.feishu.cn/base/app1", selection)
        self.assertTrue(empty.parsed.missing_content)


class BaseFieldTests(unittest.TestCase):
    def test_all_required_field_families_and_no_recursive_network(self):
        samples = [
            (1, [{"text": "正文"}], "正文"),
            (2, 0, "0"),
            (3, {"id": "o1"}, "进行中"),
            (4, ["进行中", "完成"], "进行中；完成"),
            (5, 0, "1970-01-01T08:00:00"),
            (11, [{"id": "ou1", "name": "张三"}], "张三"),
            (11, [{"id": "ou2"}], "展示名未授权"),
            (
                15,
                {"text": "官网", "link": "https://example.com"},
                "官网 (https://example.com)",
            ),
            (
                17,
                [{"name": "附件.pdf", "type": "pdf", "file_token": "file1"}],
                "未下载正文",
            ),
            (18, ["recOther"], "未展开其他表"),
            (20, {"type": 2, "value": [0]}, "公式结果：0"),
            (19, {"type": 1, "value": [{"text": "展示"}]}, "查找结果：展示"),
        ]
        for kind, value, expected in samples:
            with self.subTest(kind=kind, value=value):
                text = field_text(
                    {
                        "type": kind,
                        "property": {"options": [{"id": "o1", "name": "进行中"}]},
                    },
                    value,
                    timezone="Asia/Shanghai",
                )
                self.assertIn(expected, text)
        for kind, value in (
            (999, {}),
            (20, None),
            (20, {"error": "failed"}),
            (1, {"unknown": "object"}),
        ):
            with self.assertRaises(FieldValueError):
                field_text({"type": kind}, value)
