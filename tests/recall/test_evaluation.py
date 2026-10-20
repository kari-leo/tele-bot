"""Capability scoring, recall harness, metrics and durable evaluation exports."""

from __future__ import annotations

import json
import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from tele_bot.capability_evaluation import (
    CASES,
    ROOT,
    atomic_json,
    corpus_hash,
    export_inputs,
    meets_thresholds,
    production_catalog,
    render_report,
    run_case,
    score_cases,
    score_dimension,
    simulation_tools,
    validate_cases,
    write_results,
)
from tele_bot.capability_metrics import (
    distribution,
    execution_statistics,
    failure_buckets,
    measured_case,
    normalized_usage,
    price_call,
)
from tele_bot.skills.loader import SkillLoader


def call(name, args, identifier):
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": args, "id": identifier, "type": "tool_call"}
        ],
    )


class ScriptedModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.views = []

    def invoke(self, messages, config=None):
        self.views.append(messages)
        return next(self.responses)


class CapabilityRecallTests(unittest.TestCase):
    def test_public_labels_match_current_production_registry(self):
        corpus = json.loads(CASES.read_text(encoding="utf-8"))
        cases = validate_cases(corpus)
        self.assertEqual(
            Counter(case["suite"] for case in cases), {"tool": 50, "skill": 50}
        )
        self.assertTrue(any(len(case["skills"]) > 1 for case in cases))
        self.assertTrue(
            all(
                case["tools" if case["suite"] == "tool" else "skills"] for case in cases
            )
        )
        legacy = json.loads(
            CASES.with_name("capability_cases.json").read_text(encoding="utf-8")
        )
        self.assertTrue(
            any(
                not case["tools"] and not case["skills"]
                for case in validate_cases(legacy)
            )
        )
        self.assertTrue(any(case.get("disabled_tools") for case in legacy["cases"]))
        changed = json.loads(json.dumps(corpus))
        changed["cases"][0]["prompt"] += " changed"
        self.assertNotEqual(corpus_hash(corpus), corpus_hash(changed))

    def test_simulation_preserves_production_tool_schemas(self):
        catalog = production_catalog(SkillLoader())
        simulated = simulation_tools(catalog)
        self.assertEqual(
            [convert_to_openai_tool(tool) for tool in catalog],
            [convert_to_openai_tool(tool) for tool in simulated],
        )
        # These bodies would fail through NeverExecute if real tools were called.
        by_name = {tool.name: tool for tool in simulated}
        self.assertIn("模拟", by_name["ask_adviser"].invoke({"question": "test"}))
        self.assertIn(
            "模拟",
            by_name["blog_push"].invoke(
                {"file_paths": ["D:/evaluation/no-such-file.md"]}
            ),
        )

    def test_multilabel_counts_deduplicate_and_optional_helpers_do_not_inflate_scores(
        self,
    ):
        result = score_dimension(
            ["a", "b"], ["a", "a", "optional", "wrong"], ["optional"]
        )
        self.assertEqual(
            (result["tp"], result["fn"], result["fp"], result["recall"]), (1, 1, 1, 0.5)
        )
        self.assertEqual(result["missing"], ["b"])
        self.assertIsNone(score_dimension([], ["wrong"])["recall"])

    def test_negative_examples_do_not_artificially_raise_recall(self):
        cases = [
            {
                "id": "positive",
                "category": "multiple",
                "tools": ["a", "b"],
                "skills": ["alpha", "beta"],
            },
            {"id": "negative", "category": "negative", "tools": [], "skills": []},
        ]
        predictions = [
            {"id": "positive", "tools": ["a"], "skills": ["alpha"]},
            {
                "id": "negative",
                "tools": ["wrong"],
                "skills": [],
                "skill_attempts": ["unknown"],
            },
        ]
        result = score_cases(cases, predictions)
        self.assertEqual(result["summary"]["tool"]["micro_recall"], 0.5)
        self.assertEqual(result["summary"]["skill"]["micro_recall"], 0.5)
        self.assertEqual(result["summary"]["tool"]["precision"], 0.5)
        self.assertEqual(result["summary"]["negative_passed"], 0)
        self.assertEqual(result["cases"][1]["extra_skill_attempts"], ["unknown"])
        with self.assertRaises(ValueError):
            score_cases(cases, predictions[:1])
        with self.assertRaises(ValueError):
            score_cases(cases, predictions + [predictions[0]])

    def test_graph_counts_successfully_loaded_skill_and_requested_tool(self):
        model = ScriptedModel(
            [
                call("load_skill", {"name": "adviser"}, "s1"),
                call("ask_adviser", {"question": "tradeoffs?"}, "t1"),
                AIMessage(content="done"),
            ]
        )
        case = {"id": "example", "prompt": "请咨询 adviser 获取第二意见。"}
        result = run_case(case, lambda tools: model)
        self.assertEqual(result["tools"], ["ask_adviser"])
        self.assertEqual(result["skills"], ["adviser"])
        self.assertEqual(result["skill_attempts"], ["adviser"])
        self.assertIsNone(result["error"])
        self.assertFalse(result["tool_errors"])
        self.assertIn("Active Skill: adviser", str(model.views[1][0].content))
        self.assertNotIn("Active Skill: adviser", str(model.views[0][0].content))

    def test_claimed_or_unavailable_skills_do_not_count_as_loaded(self):
        case = {
            "id": "unavailable",
            "prompt": "获取第二意见。",
            "disabled_tools": ["ask_adviser"],
        }
        model = ScriptedModel(
            [
                call("load_skill", {"name": "adviser"}, "s1"),
                AIMessage(content="我加载了 adviser。"),
            ]
        )
        result = run_case(case, lambda tools: model)
        self.assertEqual(result["skills"], [])
        self.assertEqual(result["skill_attempts"], ["adviser"])
        self.assertEqual(result["tools"], [])

    def test_exhaustion_or_invalid_arguments_cannot_be_a_strict_pass(self):
        labels = {
            "id": "limited",
            "category": "knowledge",
            "prompt": "查项目资料。",
            "tools": ["knowledge_search"],
            "skills": [],
        }
        model = ScriptedModel([call("knowledge_search", {"query": "MX-204"}, "t1")])
        result = run_case(labels, lambda tools: model, max_calls=1)
        self.assertTrue(result["budget_exhausted"])
        scored = score_cases([labels], [result])
        self.assertEqual(scored["summary"]["tool"]["micro_recall"], 1.0)
        self.assertEqual(scored["summary"]["passed"], 0)
        model = ScriptedModel(
            [call("knowledge_search", {}, "bad1"), AIMessage(content="done")]
        )
        invalid = run_case(labels, lambda tools: model)
        self.assertTrue(invalid["tool_errors"])
        self.assertFalse(score_cases([labels], [invalid])["cases"][0]["pass"])

    def test_unavailable_gold_labels_fail_validation(self):
        corpus = {
            "cases": [
                {
                    "id": "bad",
                    "category": "unavailable",
                    "prompt": "test",
                    "tools": [],
                    "skills": ["adviser"],
                    "disabled_tools": ["ask_adviser"],
                }
            ]
        }
        with self.assertRaisesRegex(ValueError, "Skill unavailable"):
            validate_cases(corpus)

    def test_report_separates_selection_from_execution(self):
        cases = [{"id": "chat", "category": "negative", "tools": [], "skills": []}]
        scored = score_cases(cases, [{"id": "chat", "tools": [], "skills": []}])
        report = render_report(
            scored,
            {
                "model": "scripted-harness-test",
                "max_calls": 5,
                "corpus_sha256": "fixture",
            },
        )
        self.assertIn("N/A", report)
        self.assertIn("不证明参数正确", report)
        self.assertIn("1/1", report)

    def test_thresholds_use_measured_recall_and_fail_on_incomplete_runs(self):
        cases = [
            {"id": "a", "category": "test", "tools": ["a", "b"], "skills": ["alpha"]}
        ]
        predictions = [{"id": "a", "tools": ["a"], "skills": ["alpha"]}]
        scored = score_cases(cases, predictions)
        self.assertTrue(meets_thresholds(scored, 0.5, 1.0))
        self.assertFalse(meets_thresholds(scored, 0.9, 1.0))
        predictions[0]["budget_exhausted"] = True
        self.assertFalse(meets_thresholds(score_cases(cases, predictions)))

    def test_local_export_hides_gold_labels_and_has_no_credentials(self):
        corpus = {
            "cases": [
                {
                    "id": "a",
                    "category": "knowledge",
                    "prompt": "查项目资料",
                    "tools": ["knowledge_search"],
                    "skills": [],
                }
            ]
        }
        exported = export_inputs(corpus, corpus["cases"])
        self.assertEqual(set(exported["case_inputs"][0]), {"id", "prompt"})
        self.assertTrue(exported["tool_schemas"])
        self.assertTrue(exported["possible_skill_results"])
        self.assertNotIn("api_key", exported)

    def test_manifest_rejects_wrong_counts_or_duplicate_prompts(self):
        corpus = json.loads(CASES.read_text(encoding="utf-8"))
        corpus["suite_counts"]["tool"] = 49
        with self.assertRaisesRegex(ValueError, "suite counts"):
            validate_cases(corpus)


class CapabilityMetricsTests(unittest.TestCase):
    def test_percentiles_exclude_missing_samples(self):
        result = distribution([10, None, 20, 30])
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["p50"], 20)
        self.assertAlmostEqual(result["p95"], 29)
        self.assertIsNone(distribution([])["mean"])

    def test_per_call_latency_does_not_include_missing_skill_as_zero(self):
        cases = [{"tools": [], "skills": ["adviser", "schedule"]}]
        predictions = [
            {
                "execution_events": [
                    {
                        "name": "load_skill",
                        "skill": "adviser",
                        "status": "loaded",
                        "duration_ms": 4,
                    }
                ]
            }
        ]
        rows = {row["name"]: row for row in execution_statistics(cases, predictions)}
        self.assertEqual(rows["adviser"]["duration_ms"]["mean"], 4)
        self.assertEqual(rows["schedule"]["calls"], 0)
        self.assertIsNone(rows["schedule"]["duration_ms"]["mean"])

    def test_pricing_tracks_cached_input_and_per_request_tiers(self):
        pricing = json.loads(
            (ROOT / "evaluation" / "qwen37_pricing.json").read_text(encoding="utf-8")
        )
        usage = {
            "usage_reported": True,
            "input_tokens": 100000,
            "output_tokens": 10000,
            "cached_input_tokens": 20000,
        }
        self.assertAlmostEqual(price_call(usage, pricing), 0.248)
        usage.update(input_tokens=300000, cached_input_tokens=0)
        self.assertAlmostEqual(price_call(usage, pricing), 2.04)
        self.assertIsNone(price_call({"usage_reported": False}, pricing))
        self.assertIsNone(price_call(usage, None))

    def test_normalized_usage_supports_langchain_and_provider_details(self):
        message = AIMessage(
            content="done",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 5,
                "total_tokens": 105,
                "input_token_details": {"cache_read": 30},
            },
        )
        usage = normalized_usage(message)
        self.assertEqual(usage["cached_input_tokens"], 30)
        self.assertTrue(usage["usage_reported"])
        self.assertFalse(normalized_usage(AIMessage(content="done"))["usage_reported"])

    def test_missing_cost_is_not_reported_as_zero(self):
        measurement = measured_case(
            {"trace": [{"tool_calls": [], "usage": {}}], "model_calls": 1}
        )
        self.assertIsNone(measurement["estimated_total_cost_cny"])
        self.assertFalse(measurement["cost_complete"])
        self.assertEqual(measurement["usage_missing_calls"], 1)

    def test_primary_buckets_are_exclusive_but_tags_keep_all_failures(self):
        row = {
            "tool": {"fn": 1, "fp": 1},
            "skill": {"fn": 1, "fp": 0},
            "extra_skill_attempts": [],
        }
        prediction = {
            "error": "APITimeoutError: timed out",
            "budget_exhausted": True,
            "tool_errors": [{"error": "wrong is not a valid tool"}],
        }
        tags, primary = failure_buckets({}, prediction, row)
        self.assertEqual(primary, "api_timeout")
        self.assertIn("invalid_tool_name", tags)
        self.assertIn("tool_missing", tags)
        self.assertIn("skill_missing", tags)

    def test_late_skill_is_loaded_but_fails_correct_call_rate(self):
        model = ScriptedModel(
            [
                call("ask_adviser", {"question": "test"}, "t1"),
                call("load_skill", {"name": "adviser"}, "s1"),
                AIMessage(content="done"),
            ]
        )
        case = {
            "id": "late",
            "suite": "skill",
            "category": "adviser",
            "prompt": "咨询 adviser",
            "tools": ["ask_adviser"],
            "skills": ["adviser"],
        }
        prediction = run_case(case, lambda tools: model)
        self.assertEqual(prediction["skills"], ["adviser"])
        self.assertEqual(prediction["late_skills"], ["adviser"])
        self.assertGreater(prediction["timing"]["elapsed_ms"], 0)
        self.assertTrue(
            all(event["duration_ms"] >= 0 for event in prediction["execution_events"])
        )
        scored = score_cases([case], [prediction])
        self.assertEqual(scored["groups"]["skill"]["correct_call_rate"], 0)
        self.assertIn("skill_loaded_late", scored["cases"][0]["failure_tags"])

    def test_primary_tool_accuracy_is_separate_from_skill_recall(self):
        case = {
            "id": "tool",
            "suite": "tool",
            "category": "schedule",
            "tools": ["schedule_manage"],
            "skills": ["schedule"],
        }
        result = score_cases(
            [case], [{"id": "tool", "tools": ["schedule_manage"], "skills": []}]
        )
        self.assertEqual(result["groups"]["tool"]["correct_call_rate"], 1)
        self.assertEqual(result["groups"]["tool"]["strict_passed"], 0)

    def test_api_errors_keep_timing_and_unknown_cost(self):
        class BrokenModel:
            def invoke(self, messages, config=None):
                raise TimeoutError("API timed out")

        result = run_case(
            {"id": "timeout", "prompt": "查资料"}, lambda tools: BrokenModel()
        )
        self.assertEqual(result["model_calls"], 1)
        self.assertIn("TimeoutError", result["error"])
        self.assertGreaterEqual(result["trace"][0]["latency_ms"], 0)
        self.assertFalse(measured_case(result)["cost_complete"])

    def test_csv_and_json_retain_per_case_metrics(self):
        case = {
            "id": "saved",
            "suite": "tool",
            "category": "file",
            "tools": ["filesystem_list_dir"],
            "skills": [],
        }
        prediction = {
            "id": "saved",
            "tools": ["filesystem_list_dir"],
            "skills": [],
            "trace": [],
            "model_calls": 0,
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "report.md"
            write_results(
                path,
                {"model": "fixture", "max_calls": 5, "corpus_sha256": "test"},
                [case],
                [prediction],
            )
            self.assertIn(
                "estimated_route_cost_cny",
                path.with_suffix(".csv").read_text(encoding="utf-8-sig"),
            )
            saved = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(saved["scores"]["groups"]["tool"]["case_count"], 1)

    def test_atomic_checkpoint_keeps_last_good_data_when_replace_fails(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            atomic_json(path, {"saved": 73})
            with (
                patch(
                    "tele_bot.capability_evaluation.os.replace",
                    side_effect=OSError(22, "temporary lock"),
                ),
                patch("tele_bot.capability_evaluation.sleep"),
            ):
                with self.assertRaises(OSError):
                    atomic_json(path, {"saved": 74})
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")), {"saved": 73}
            )
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_atomic_checkpoint_retries_transient_windows_error(self):
        import os

        replace = os.replace
        attempts = []

        def retry(source, destination):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError(22, "temporary lock")
            return replace(source, destination)

        with TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            with (
                patch("tele_bot.capability_evaluation.os.replace", side_effect=retry),
                patch("tele_bot.capability_evaluation.sleep"),
            ):
                atomic_json(path, {"saved": 74})
            self.assertEqual(len(attempts), 2)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")), {"saved": 74}
            )
