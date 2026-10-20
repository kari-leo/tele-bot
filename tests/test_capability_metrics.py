import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage

from tele_bot.capability_evaluation import ROOT, atomic_json, run_case, score_cases, write_results
from tele_bot.capability_metrics import distribution, execution_statistics, failure_buckets, measured_case, normalized_usage, price_call
from tests.test_capability_recall import ScriptedModel, call


class CapabilityMetricsTests(unittest.TestCase):
    def test_percentiles_exclude_missing_samples(self):
        result = distribution([10, None, 20, 30])
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["p50"], 20)
        self.assertAlmostEqual(result["p95"], 29)
        self.assertIsNone(distribution([])["mean"])

    def test_per_call_latency_does_not_include_missing_skill_as_zero(self):
        cases = [{"tools": [], "skills": ["adviser", "schedule"]}]
        predictions = [{"execution_events": [{"name": "load_skill", "skill": "adviser", "status": "loaded", "duration_ms": 4}]}]
        rows = {row["name"]: row for row in execution_statistics(cases, predictions)}
        self.assertEqual(rows["adviser"]["duration_ms"]["mean"], 4)
        self.assertEqual(rows["schedule"]["calls"], 0)
        self.assertIsNone(rows["schedule"]["duration_ms"]["mean"])

    def test_pricing_tracks_cached_input_and_per_request_tiers(self):
        pricing = json.loads((ROOT / "evaluation" / "qwen37_pricing.json").read_text(encoding="utf-8"))
        usage = {"usage_reported": True, "input_tokens": 100000, "output_tokens": 10000, "cached_input_tokens": 20000}
        self.assertAlmostEqual(price_call(usage, pricing), 0.248)
        usage.update(input_tokens=300000, cached_input_tokens=0)
        self.assertAlmostEqual(price_call(usage, pricing), 2.04)
        self.assertIsNone(price_call({"usage_reported": False}, pricing))
        self.assertIsNone(price_call(usage, None))

    def test_normalized_usage_supports_langchain_and_provider_details(self):
        message = AIMessage(content="done", usage_metadata={"input_tokens": 100, "output_tokens": 5, "total_tokens": 105,
                                                           "input_token_details": {"cache_read": 30}})
        usage = normalized_usage(message)
        self.assertEqual(usage["cached_input_tokens"], 30)
        self.assertTrue(usage["usage_reported"])
        self.assertFalse(normalized_usage(AIMessage(content="done"))["usage_reported"])

    def test_missing_cost_is_not_reported_as_zero(self):
        measurement = measured_case({"trace": [{"tool_calls": [], "usage": {}}], "model_calls": 1})
        self.assertIsNone(measurement["estimated_total_cost_cny"])
        self.assertFalse(measurement["cost_complete"])
        self.assertEqual(measurement["usage_missing_calls"], 1)

    def test_primary_buckets_are_exclusive_but_tags_keep_all_failures(self):
        row = {"tool": {"fn": 1, "fp": 1}, "skill": {"fn": 1, "fp": 0}, "extra_skill_attempts": []}
        prediction = {"error": "APITimeoutError: timed out", "budget_exhausted": True,
                      "tool_errors": [{"error": "wrong is not a valid tool"}]}
        tags, primary = failure_buckets({}, prediction, row)
        self.assertEqual(primary, "api_timeout")
        self.assertIn("invalid_tool_name", tags)
        self.assertIn("tool_missing", tags)
        self.assertIn("skill_missing", tags)

    def test_late_skill_is_loaded_but_fails_correct_call_rate(self):
        model = ScriptedModel([call("ask_adviser", {"question": "test"}, "t1"),
                               call("load_skill", {"name": "adviser"}, "s1"), AIMessage(content="done")])
        case = {"id": "late", "suite": "skill", "category": "adviser", "prompt": "咨询 adviser",
                "tools": ["ask_adviser"], "skills": ["adviser"]}
        prediction = run_case(case, lambda tools: model)
        self.assertEqual(prediction["skills"], ["adviser"])
        self.assertEqual(prediction["late_skills"], ["adviser"])
        self.assertGreater(prediction["timing"]["elapsed_ms"], 0)
        self.assertTrue(all(event["duration_ms"] >= 0 for event in prediction["execution_events"]))
        scored = score_cases([case], [prediction])
        self.assertEqual(scored["groups"]["skill"]["correct_call_rate"], 0)
        self.assertIn("skill_loaded_late", scored["cases"][0]["failure_tags"])

    def test_primary_tool_accuracy_is_separate_from_skill_recall(self):
        case = {"id": "tool", "suite": "tool", "category": "schedule", "tools": ["schedule_manage"], "skills": ["schedule"]}
        result = score_cases([case], [{"id": "tool", "tools": ["schedule_manage"], "skills": []}])
        self.assertEqual(result["groups"]["tool"]["correct_call_rate"], 1)
        self.assertEqual(result["groups"]["tool"]["strict_passed"], 0)

    def test_api_errors_keep_timing_and_unknown_cost(self):
        class BrokenModel:
            def invoke(self, messages, config=None):
                raise TimeoutError("API timed out")
        result = run_case({"id": "timeout", "prompt": "查资料"}, lambda tools: BrokenModel())
        self.assertEqual(result["model_calls"], 1)
        self.assertIn("TimeoutError", result["error"])
        self.assertGreaterEqual(result["trace"][0]["latency_ms"], 0)
        self.assertFalse(measured_case(result)["cost_complete"])

    def test_csv_and_json_retain_per_case_metrics(self):
        case = {"id": "saved", "suite": "tool", "category": "file", "tools": ["filesystem_list_dir"], "skills": []}
        prediction = {"id": "saved", "tools": ["filesystem_list_dir"], "skills": [], "trace": [], "model_calls": 0}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "report.md"
            write_results(path, {"model": "fixture", "max_calls": 5, "corpus_sha256": "test"}, [case], [prediction])
            self.assertIn("estimated_route_cost_cny", path.with_suffix(".csv").read_text(encoding="utf-8-sig"))
            saved = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(saved["scores"]["groups"]["tool"]["case_count"], 1)

    def test_atomic_checkpoint_keeps_last_good_data_when_replace_fails(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            atomic_json(path, {"saved": 73})
            with patch("tele_bot.capability_evaluation.os.replace", side_effect=OSError(22, "temporary lock")), patch(
                "tele_bot.capability_evaluation.sleep"
            ):
                with self.assertRaises(OSError):
                    atomic_json(path, {"saved": 74})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"saved": 73})
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
            with patch("tele_bot.capability_evaluation.os.replace", side_effect=retry), patch(
                "tele_bot.capability_evaluation.sleep"
            ):
                atomic_json(path, {"saved": 74})
            self.assertEqual(len(attempts), 2)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"saved": 74})


if __name__ == "__main__":
    unittest.main()
