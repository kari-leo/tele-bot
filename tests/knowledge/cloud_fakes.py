"""Synthetic Feishu HTTP fixtures shared by connector and command tests."""

from __future__ import annotations

from unittest.mock import Mock

import httpx

from tele_bot.knowledge.cloud_sources import CloudSourceConnector
from tele_bot.knowledge.feishu_docs import FeishuDocsClient
from tele_bot.knowledge.ingestion import DocumentParser


def response(data=None, *, status=200, code=0):
    return httpx.Response(status, json={"code": code, "data": data or {}})


def text_block(identity, text, kind=2):
    name = "page" if kind == 1 else f"heading{kind - 2}" if 3 <= kind <= 11 else "text"
    return {
        "block_id": identity,
        "block_type": kind,
        name: {"elements": [{"text_run": {"content": text}}]},
    }


class FakeCloudAPI:
    def __init__(self):
        self.calls = []
        self.overrides = {}
        self.revision = 7
        self.fields = [
            {"field_id": "fldName", "field_name": "项目", "type": 1},
            {"field_id": "fldQty", "field_name": "数量", "type": 2},
        ]
        self.records = [
            {"record_id": "rec1", "fields": {"项目": "Alpha", "数量": 0}},
            {"record_id": "rec2", "fields": {"项目": "Beta", "数量": 2}},
        ]
        root = text_block("doc1", "Design", 1)
        root["children"] = ["heading", "intro", "table"]
        heading = text_block("heading", "项目进展", 3)
        table = {
            "block_id": "table",
            "block_type": 31,
            "table": {
                "cells": ["c1", "c2", "c3", "c4"],
                "property": {"row_size": 2, "column_size": 2},
            },
        }
        self.blocks = [root, heading, text_block("intro", "接口联调延期"), table]
        for index, value in enumerate(("项目", "数量", "Alpha", "0"), 1):
            self.blocks.extend(
                [
                    {
                        "block_id": f"c{index}",
                        "block_type": 32,
                        "children": [f"t{index}"],
                    },
                    text_block(f"t{index}", value),
                ]
            )
        self.sheets = [
            {
                "sheet_id": "sheet1",
                "title": "进度",
                "resource_type": "sheet",
                "merges": [],
                "grid_properties": {"row_count": 5, "column_count": 2},
            }
        ]
        self.values = [
            ["项目", "数量"],
            ["Alpha", "0"],
            ["Beta", "2"],
            ["Gamma", "3"],
            ["Delta", "4"],
        ]
        self.node = {"obj_type": "docx", "obj_token": "doc1"}

    def __call__(self, method, url, **kwargs):
        path = url.split("https://open.feishu.cn", 1)[-1]
        params = kwargs.get("params") or {}
        self.calls.append((method, path, params, kwargs.get("json")))
        if path in self.overrides:
            item = self.overrides[path]
            return item(method, params, kwargs.get("json")) if callable(item) else item
        if path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "test-token", "expire": 7200},
            )
        if path.endswith("spaces/get_node"):
            return response({"node": self.node})
        if path == "/open-apis/docx/v1/documents/doc1":
            return response(
                {"document": {"title": "Design", "revision_id": self.revision}}
            )
        if path.endswith("/blocks"):
            return response({"items": self.blocks, "has_more": False})
        if path.endswith("/sheets/query"):
            return response({"sheets": self.sheets})
        if path.endswith("/sheets/sheet1"):
            return response({"sheet": self.sheets[0]})
        if "/values/" in path:
            area = path.split("/values/")[1]
            import re

            first, last = [int(v) for v in re.findall(r"[A-Z]+(\d+)", area)]
            return response(
                {
                    "revision": self.revision,
                    "valueRange": {
                        "range": area,
                        "values": self.values[first - 1 : last],
                    },
                }
            )
        if path == "/open-apis/bitable/v1/apps/app1":
            return response(
                {
                    "app": {
                        "name": "项目台账",
                        "time_zone": "Asia/Shanghai",
                        "revision": self.revision,
                    }
                }
            )
        if path.endswith("/tables"):
            return response(
                {"items": [{"table_id": "tbl1", "name": "项目"}], "has_more": False}
            )
        if path.endswith("/fields"):
            return response({"items": self.fields, "has_more": False})
        if path.endswith("/records/search"):
            return response({"items": self.records, "has_more": False})
        raise AssertionError(f"unexpected API request {method} {path}")


def connector(api, **kwargs):
    client = FeishuDocsClient("app-id", "secret", request=api, sleep=Mock(), **kwargs)
    return CloudSourceConnector(client, DocumentParser(), batch_rows=2)


SHEET_SELECTION = {"sheet_id": "sheet1", "range": "A1:B5"}

BASE_SELECTION = {"table_id": "tbl1", "field_ids": ["fldName", "fldQty"]}
