"""Deterministic scoring/harness tests; live model quality is measured by the CLI."""

import json
from collections import Counter
import unittest

from langchain_core.messages import AIMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from tele_bot.capability_evaluation import (
    CASES, corpus_hash, export_inputs, meets_thresholds, production_catalog, render_report, run_case,
    score_cases, score_dimension, simulation_tools, validate_cases,
)
from tele_bot.skills.loader import SkillLoader


def call(name, args, identifier):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": identifier, "type": "tool_call"}])


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
        self.assertEqual(Counter(case["suite"] for case in cases), {"tool": 50, "skill": 50})
        self.assertTrue(any(len(case["skills"]) > 1 for case in cases))
        self.assertTrue(all(case["tools" if case["suite"] == "tool" else "skills"] for case in cases))
        legacy = json.loads(CASES.with_name("capability_cases.json").read_text(encoding="utf-8"))
        self.assertTrue(any(not case["tools"] and not case["skills"] for case in validate_cases(legacy)))
        self.assertTrue(any(case.get("disabled_tools") for case in legacy["cases"]))
        changed = json.loads(json.dumps(corpus))
        changed["cases"][0]["prompt"] += " changed"
        self.assertNotEqual(corpus_hash(corpus), corpus_hash(changed))

    def test_simulation_preserves_production_tool_schemas(self):
        catalog = production_catalog(SkillLoader())
        simulated = simulation_tools(catalog)
        self.assertEqual([convert_to_openai_tool(tool) for tool in catalog],
                         [convert_to_openai_tool(tool) for tool in simulated])
        # These bodies would fail through NeverExecute if real tools were called.
        by_name = {tool.name: tool for tool in simulated}
        self.assertIn("模拟", by_name["ask_adviser"].invoke({"question": "test"}))
        self.assertIn("模拟", by_name["blog_push"].invoke({"file_paths": ["D:/evaluation/no-such-file.md"]}))

    def test_multilabel_counts_deduplicate_and_optional_helpers_do_not_inflate_scores(self):
        result = score_dimension(["a", "b"], ["a", "a", "optional", "wrong"], ["optional"])
        self.assertEqual((result["tp"], result["fn"], result["fp"], result["recall"]), (1, 1, 1, 0.5))
        self.assertEqual(result["missing"], ["b"])
        self.assertIsNone(score_dimension([], ["wrong"])["recall"])

    def test_negative_examples_do_not_artificially_raise_recall(self):
        cases = [{"id": "positive", "category": "multiple", "tools": ["a", "b"], "skills": ["alpha", "beta"]},
                 {"id": "negative", "category": "negative", "tools": [], "skills": []}]
        predictions = [{"id": "positive", "tools": ["a"], "skills": ["alpha"]},
                       {"id": "negative", "tools": ["wrong"], "skills": [], "skill_attempts": ["unknown"]}]
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
        model = ScriptedModel([call("load_skill", {"name": "adviser"}, "s1"),
                               call("ask_adviser", {"question": "tradeoffs?"}, "t1"), AIMessage(content="done")])
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
        case = {"id": "unavailable", "prompt": "获取第二意见。", "disabled_tools": ["ask_adviser"]}
        model = ScriptedModel([call("load_skill", {"name": "adviser"}, "s1"), AIMessage(content="我加载了 adviser。")])
        result = run_case(case, lambda tools: model)
        self.assertEqual(result["skills"], [])
        self.assertEqual(result["skill_attempts"], ["adviser"])
        self.assertEqual(result["tools"], [])

    def test_exhaustion_or_invalid_arguments_cannot_be_a_strict_pass(self):
        labels = {"id": "limited", "category": "knowledge", "prompt": "查项目资料。", "tools": ["knowledge_search"], "skills": []}
        model = ScriptedModel([call("knowledge_search", {"query": "MX-204"}, "t1")])
        result = run_case(labels, lambda tools: model, max_calls=1)
        self.assertTrue(result["budget_exhausted"])
        scored = score_cases([labels], [result])
        self.assertEqual(scored["summary"]["tool"]["micro_recall"], 1.0)
        self.assertEqual(scored["summary"]["passed"], 0)
        model = ScriptedModel([call("knowledge_search", {}, "bad1"), AIMessage(content="done")])
        invalid = run_case(labels, lambda tools: model)
        self.assertTrue(invalid["tool_errors"])
        self.assertFalse(score_cases([labels], [invalid])["cases"][0]["pass"])

    def test_unavailable_gold_labels_fail_validation(self):
        corpus = {"cases": [{"id": "bad", "category": "unavailable", "prompt": "test", "tools": [],
                             "skills": ["adviser"], "disabled_tools": ["ask_adviser"]}]}
        with self.assertRaisesRegex(ValueError, "Skill unavailable"):
            validate_cases(corpus)

    def test_report_separates_selection_from_execution(self):
        cases = [{"id": "chat", "category": "negative", "tools": [], "skills": []}]
        scored = score_cases(cases, [{"id": "chat", "tools": [], "skills": []}])
        report = render_report(scored, {"model": "scripted-harness-test", "max_calls": 5, "corpus_sha256": "fixture"})
        self.assertIn("N/A", report)
        self.assertIn("不证明参数正确", report)
        self.assertIn("1/1", report)

    def test_thresholds_use_measured_recall_and_fail_on_incomplete_runs(self):
        cases = [{"id": "a", "category": "test", "tools": ["a", "b"], "skills": ["alpha"]}]
        predictions = [{"id": "a", "tools": ["a"], "skills": ["alpha"]}]
        scored = score_cases(cases, predictions)
        self.assertTrue(meets_thresholds(scored, 0.5, 1.0))
        self.assertFalse(meets_thresholds(scored, 0.9, 1.0))
        predictions[0]["budget_exhausted"] = True
        self.assertFalse(meets_thresholds(score_cases(cases, predictions)))

    def test_local_export_hides_gold_labels_and_has_no_credentials(self):
        corpus = {"cases": [{"id": "a", "category": "knowledge", "prompt": "查项目资料", "tools": ["knowledge_search"], "skills": []}]}
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


if __name__ == "__main__":
    unittest.main()
