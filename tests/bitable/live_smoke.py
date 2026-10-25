"""Opt-in test of the human-authorized Base, never collected by pytest.

python -m tests.bitable.live_smoke          # discover only
python -m tests.bitable.live_smoke --write  # create/update/clear/delete our test row
No production policy files are changed.
"""
import argparse
from dataclasses import replace
import json
from pathlib import Path
from uuid import uuid4

from tele_bot.bitable.client import BitableClient
from tele_bot.bitable.models import BitableError
from tele_bot.bitable.policy import BitablePolicy
from tele_bot.bitable.service import BitableService
from tele_bot.config.bitable import BitableSettings
from tele_bot.config.feishu import FeishuSettings
from tele_bot.config.paths import configure_runtime_root
from tele_bot.models import IncomingMessage
from tele_bot.persistence.bitable_store import BitableStore

BASE = "ZmTbbJudzajY18soG28c87LKnmc"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    configure_runtime_root(root)
    existing = FeishuSettings.from_env()
    settings = replace(BitableSettings.from_env(), app_id=existing.app_id,
                       app_secret=existing.app_secret, api_base_url=existing.api_base_url,
                       sqlite_path=root / ".runtime/bitable-live.sqlite")
    client = BitableClient(settings)
    report = {"base": BASE, "mode": "write" if args.write else "inspect", "steps": []}
    try:
        with client.scope():
            tables = client.call("tables", BASE, size=100)["items"]
        report["tables"] = [{"table_id": t["table_id"], "name": t.get("name")} for t in tables]
        if not tables:
            raise BitableError("not_found", "测试 Base 无可用数据表")
        table = tables[0]["table_id"]
        policy = BitablePolicy([{"alias": "live-test", "app_token": BASE, "table_id": table}], origin=settings.api_base_url)
        service = BitableService(settings, client, policy, BitableStore(settings.sqlite_path))
        message = IncomingMessage("feishu", "development-smoke", "development-smoke",
                                  "按用户已授权范围测试新增修改清空删除测试记录", "live-" + str(uuid4()))
        structure = service.inspect("live-test", message)
        report["fields"] = structure["fields"]
        if args.write:
            text = next((f["field_name"] for f in structure["fields"] if f["type"] == 1), None)
            if text is None:
                raise BitableError("validation", "测试表没有文本字段，未执行写入")
            rid = None
            try:
                created = service.write("create", "live-test", message,
                                        [{"fields": {text: "telebot-sdk-smoke-" + str(uuid4())}}])
                report["steps"].append({"step": "create", **created})
                if created.get("record_ids"):
                    rid = created["record_ids"][0]
                elif created.get("response", {}).get("records"):
                    rid = created["response"]["records"][0].get("record_id")
                if created["status"] != "succeeded":
                    raise BitableError("unknown", "新增未核实，停止后续操作，详见账本")
                rid = created["record_ids"][0]
                query = service.query("live-test", message, record_id=rid, fields=[text])
                report["steps"].append({"step": "query", "status": query["status"], "returned_count": query["returned_count"]})
                for step, patch in [("update", {"fields": {text: "telebot-sdk-smoke-updated"}}),
                                    ("clear", {"clear_fields": [text]})]:
                    result = service.write("update", "live-test", replace(message, message_id="live-" + str(uuid4())),
                                           [{"record_id": rid, **patch}])
                    report["steps"].append({"step": step, **result})
                    if result["status"] != "succeeded":
                        raise BitableError("unknown", "修改或清空未核实，停止并清理测试记录")
            finally:
                if rid:
                    delete = service.write("delete", "live-test", replace(message, message_id="live-" + str(uuid4())), [rid])
                    if delete["status"] == "awaiting_confirmation":
                        delete = service.confirm(delete["confirmation_token"], message)
                    report["steps"].append({"step": "delete", **delete})
                    report["remaining_test_records"] = [] if delete["status"] == "succeeded" else [rid]
        report["status"] = "succeeded" if not report.get("remaining_test_records") else "cleanup_failed"
    except BitableError as exc:
        report.update(status="failed", error=exc.result())
    except Exception as exc:
        report.update(status="failed", error={"kind": "internal", "type": type(exc).__name__,
                                              "message": "联调未完成；未输出原始异常以避免泄露凭据"})
    output = root / ".runtime/bitable-live-report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # Windows terminals may still use GBK; Base titles can contain emoji.
    print(json.dumps({"status": report["status"], "mode": report["mode"], "error": report.get("error"),
                      "steps": [{k: v for k, v in item.items() if k in {
                          "step", "status", "operation_id", "verification", "error", "code"}}
                                for item in report["steps"]],
                      "remaining_test_records": report.get("remaining_test_records", [])}, ensure_ascii=True))
    return 0 if report["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
