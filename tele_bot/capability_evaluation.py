"""Evaluate real tool selection and successful Skill loading without business effects.

python -m tele_bot.capability_evaluation --live --output evaluation/CAPABILITY_REPORT.md
python -m tele_bot.capability_evaluation --predictions evaluation/CAPABILITY_REPORT.json

The offline tests validate scoring, not model routing quality. --live uses the
configured chat model and production tool schemas with bounded mock execution.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import perf_counter, sleep
from datetime import datetime, timedelta, timezone

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from tele_bot.skills.loader import SkillLoader
from tele_bot.tools.lc_adapters import build_core_tools
from tele_bot.workflows.react_graph import build_react_graph
from tele_bot.capability_metrics import (
    execution_statistics, failure_buckets, measured_case, normalized_usage, price_call, summarize_group,
)

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evaluation" / "capability_100_cases.json"


class NeverExecute:
    """An injected dependency that fails if production code ever executes it."""
    def __getattr__(self, name):
        raise AssertionError(f"evaluation attempted a real business operation: {name}")


def production_catalog(loader, disabled=()):
    """Use actual registered descriptions/schemas; don't invent a second registry."""
    blocked = NeverExecute()
    tools = build_core_tools(
        filesystem_tool=blocked, shell_tool=blocked, search_tool=blocked,
        knowledge_tool=blocked, write_report_tool=blocked, blog_publish_tool=blocked,
        domain_hotspot_tool=blocked, adviser_tool=blocked,
        include_adviser=True, include_blog_publish=True, include_domain_hotspot=True,
        feishu_adapter=blocked, codex_service=blocked, schedule_service=blocked,
        knowledge_search=blocked, skill_loader=loader,
    )
    tools = [tool for tool in tools if tool.name not in set(disabled)]
    loader.available_tools = {tool.name for tool in tools}
    return tools


def validate_cases(corpus):
    cases = corpus["cases"]
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("case IDs must be unique and corpus must be nonempty")
    all_tools = {tool.name for tool in production_catalog(SkillLoader())} - {"load_skill"}
    profiles = {}
    for case in cases:
        if not case.get("prompt", "").strip():
            raise ValueError(f"empty prompt: {case['id']}")
        for key in ("tools", "skills", "allowed_tools", "allowed_skills", "disabled_tools"):
            labels = case.get(key, [])
            if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels) or len(set(labels)) != len(labels):
                raise ValueError(f"invalid {key}: {case['id']}")
        disabled = set(case.get("disabled_tools", []))
        if set(case["tools"]) - (all_tools - disabled):
            raise ValueError(f"expected tool unavailable: {case['id']}")
        if set(case.get("allowed_tools", [])) - (all_tools - disabled):
            raise ValueError(f"allowed tool unavailable: {case['id']}")
        key = tuple(sorted(disabled))
        if key not in profiles:
            profiles[key] = SkillLoader()
            production_catalog(profiles[key], disabled)
        loader = profiles[key]
        for name in case["skills"] + case.get("allowed_skills", []):
            if loader.load_skill(name)["status"] != "loaded":
                raise ValueError(f"expected Skill unavailable: {case['id']}: {name}")
    if "suite_counts" in corpus:
        counts = dict(Counter(case.get("suite") for case in cases))
        if counts != corpus["suite_counts"]:
            raise ValueError("actual suite counts differ from corpus manifest")
        if len({case["prompt"].strip() for case in cases}) != len(cases):
            raise ValueError("suite prompts must be distinct")
        if any(not case["tools" if case["suite"] == "tool" else "skills"] for case in cases):
            raise ValueError("each primary suite case requires a positive label")
    return cases


def corpus_hash(corpus):
    return hashlib.sha256(json.dumps(corpus, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def export_inputs(corpus, cases):
    """Export the possible external model payload for local review, without API I/O."""
    loader = SkillLoader()
    catalog = production_catalog(loader)
    skills = [loader.load_skill(name) for name, _ in loader.load()]
    profiles = {}
    for disabled in {tuple(sorted(case.get("disabled_tools", []))) for case in cases}:
        profile_loader = SkillLoader()
        tools = production_catalog(profile_loader, disabled)
        profiles[",".join(disabled) or "full"] = {
            "disabled_tools": list(disabled), "system_prompt": profile_loader.build_system_prompt(),
            "tool_names": [tool.name for tool in tools],
        }
    return {"corpus_sha256": corpus_hash(corpus), "business_execution": "simulated",
            "registry_profile": "full optional tool catalog, except case-specific disabled tools",
            "tool_schemas": [convert_to_openai_tool(tool) for tool in catalog],
            "possible_skill_results": [skill for skill in skills if skill["status"] == "loaded"],
            "profiles": profiles,
            "case_inputs": [{key: case[key] for key in ("id", "prompt", "history", "disabled_tools") if key in case}
                            for case in cases]}


def simulated_result(name, args=None):
    """Public deterministic receipts; no real paths, users or accounts are touched."""
    args = args or {}
    if name == "knowledge_search":
        return json.dumps({"hits": [{"title": "设备指南", "text": "MX-204 升级前必须备份配置。",
                                     "source_ref": "manual.md", "source_locator": "text:chars:0-18"}]}, ensure_ascii=False)
    if name == "schedule_manage":
        return json.dumps({"handled": True, "text": "模拟：已整理定时任务请求，等待用户确认。"}, ensure_ascii=False)
    if name == "ask_adviser":
        return "模拟第二意见：指南适合发布；迁移建议使用维护窗口并准备回滚方案。"
    if name in {"blog_publish_from_file", "blog_publish", "blog_push"}:
        return "模拟：文章已发布且 git push 已完成。"
    if name == "blog_publish_files":
        return json.dumps({"status": "pushed", "requested_count": 2, "written_count": 2, "pushed_count": 2,
                           "files": [{"path": "D:/evaluation/guide-a.md", "status": "pushed"},
                                     {"path": "D:/evaluation/guide-b.md", "status": "pushed"}]})
    if name == "filesystem_read_file":
        start = args.get("start_line", 1)
        return json.dumps({"path": args.get("path", "D:/evaluation/source.md"), "line_count": start + 1, "start_line": start,
                           "truncated": False, "next_start_line": None,
                           "lines": [{"line": start, "text": "# 设备指南"}, {"line": start + 1, "text": "升级前备份配置。"}]}, ensure_ascii=False)
    if name == "filesystem_list_dir":
        return json.dumps({"path": args.get("path", "D:/evaluation/project"), "entries": [{"path": "README.md", "type": "file"}]})
    if name == "opencli_search":
        return json.dumps({"query": args.get("query", ""), "result_count": 1,
                           "results": [{"rank": 1, "title": "模拟官方文档检索结果", "url": "https://docs.python.org/3/",
                                        "snippet": "模拟内容：该结果为查询提供了对应的文档和说明，不需要再次检索同一问题。"}]}, ensure_ascii=False)
    if name == "domain_hotspot":
        return json.dumps({"domain": args.get("domain", "ai"), "items": [
            {"title": f"模拟热点 {number}", "link": f"https://example.invalid/news/{number}", "summary": "模拟新闻摘要"}
            for number in range(1, min(args.get("limit", 5), 10) + 1)]}, ensure_ascii=False)
    if name == "shell_execute":
        return json.dumps({"returncode": 0, "stdout": "README.md\nsource.md", "stderr": "", "truncated": False})
    if name in {"write_report", "knowledge_restore"}:
        return "D:/evaluation/generated-report.md"
    if name == "feishu_send_file":
        return "模拟：文件已发送到当前会话。"
    return json.dumps({"status": "complete", "text": "模拟工具已返回本次请求的结果。"}, ensure_ascii=False)


def simulation_tools(catalog, *, events=None, origin=None):
    tools = []
    for original in catalog:
        if original.name == "load_skill" and events is None:
            tools.append(original)
            continue
        def simulate(_original=original, **kwargs):
            started = perf_counter()
            status, skill = "complete", None
            try:
                if _original.name == "load_skill":
                    result = _original.invoke({"name": kwargs["name"]})
                    loaded = json.loads(result)
                    status, skill = loaded.get("status"), loaded.get("name")
                else:
                    result = simulated_result(_original.name, kwargs)
                return result
            except Exception:
                status = "error"
                raise
            finally:
                if events is not None:
                    events.append({"name": _original.name, "skill": skill, "status": status,
                                   "start_ms": (started - origin) * 1000,
                                   "end_ms": (perf_counter() - origin) * 1000,
                                   "duration_ms": (perf_counter() - started) * 1000,
                                   "execution": "real_skill_load" if _original.name == "load_skill" else "simulated"})
        tools.append(StructuredTool(name=original.name, description=original.description,
                                    args_schema=original.args_schema, func=simulate))
    return tools


def run_case(case, model_factory, *, max_calls=5, pricing=None):
    started = perf_counter()
    loader = SkillLoader()
    events = []
    tools = simulation_tools(production_catalog(loader, case.get("disabled_tools", [])), events=events, origin=started)
    model = model_factory(tools)
    trace, usage = [], Counter()
    budget_exhausted = False

    class RecordingModel:
        def invoke(self, messages, config=None):
            nonlocal budget_exhausted
            if len(trace) >= max_calls:
                budget_exhausted = True
                return AIMessage(content="评测模型调用预算已用尽。")
            call_started = perf_counter()
            try:
                response = model.invoke(messages, config=config)
            except Exception as exc:
                trace.append({"tool_calls": [], "invalid_tool_calls": [], "text": "", "error_type": type(exc).__name__,
                              "latency_ms": (perf_counter() - call_started) * 1000,
                              "completed_at_ms": (perf_counter() - started) * 1000, "usage": {}, "estimated_cost_cny": None})
                raise
            call_usage = normalized_usage(response)
            trace.append({"tool_calls": response.tool_calls or [], "text": str(response.content),
                          "invalid_tool_calls": getattr(response, "invalid_tool_calls", None) or [],
                          "latency_ms": (perf_counter() - call_started) * 1000,
                          "completed_at_ms": (perf_counter() - started) * 1000,
                          "usage": call_usage, "estimated_cost_cny": price_call(call_usage, pricing)})
            for key, value in (getattr(response, "usage_metadata", None) or {}).items():
                if isinstance(value, int):
                    usage[key] += value
            return response

    messages = []
    for index, item in enumerate(case.get("history", [])):
        cls = HumanMessage if item["role"] == "user" else AIMessage
        messages.append(cls(content=item["text"], id=f"history-{index}"))
    request_id = "evaluation-current-request"
    messages.append(HumanMessage(content=case["prompt"], id=request_id))
    graph = build_react_graph(RecordingModel(), tools, system_prompt=loader.build_system_prompt(), skill_loader=loader)
    error, final_messages = None, []
    config = {"configurable": {"thread_id": f"evaluation-{case['id']}"}}
    setup_ms = (perf_counter() - started) * 1000
    try:
        result = graph.invoke({"messages": messages, "iterations": 0},
                              config)
        final_messages = result["messages"]
    except Exception as exc:
        # Keep attempts, but failed/incomplete evaluation never counts as a pass.
        error = f"{type(exc).__name__}: {exc}"
        final_messages = graph.get_state(config).values.get("messages", [])
    tool_calls = [call for step in trace for call in step["tool_calls"]]
    loaded, failures = [], []
    for message in final_messages:
        if isinstance(message, ToolMessage):
            if message.status == "error":
                failures.append({"tool": message.name, "error": str(message.content)})
            if message.name == "load_skill":
                try:
                    value = json.loads(message.content)
                    if value.get("status") == "loaded":
                        loaded.append(value["name"])
                except (TypeError, ValueError, KeyError):
                    pass
    late_skills = []
    for skill in loader._read().values():
        loads = [event for event in events if event["skill"] == skill.name and event["status"] == "loaded"]
        business = [event for event in events if event["name"] in skill.required_tools]
        if loads and business and min(event["end_ms"] for event in loads) > min(event["start_ms"] for event in business):
            late_skills.append(skill.name)
    def first_selection(skill):
        return next((step["completed_at_ms"] for step in trace if any(
            (call["name"] == "load_skill") == skill for call in step["tool_calls"])), None)
    return {"id": case["id"], "tools": sorted({call["name"] for call in tool_calls if call["name"] != "load_skill"}),
            "skills": sorted(set(loaded)),
            "skill_attempts": [call.get("args", {}).get("name", "") for call in tool_calls if call["name"] == "load_skill"],
            "trace": trace, "tool_errors": failures, "error": error,
            "invalid_tool_calls": [call for step in trace for call in step["invalid_tool_calls"]],
            "budget_exhausted": budget_exhausted, "model_calls": len(trace), "usage": dict(usage),
            "execution_events": sorted(events, key=lambda event: event["start_ms"]), "late_skills": late_skills,
            "timing": {"elapsed_ms": (perf_counter() - started) * 1000, "setup_ms": setup_ms,
                       "model_total_ms": sum(step["latency_ms"] for step in trace),
                       "first_decision_ms": trace[0]["completed_at_ms"] if trace else None,
                       "first_business_selection_ms": first_selection(False),
                       "first_skill_selection_ms": first_selection(True)}}


def score_dimension(expected, selected, allowed=()):
    gold, actual, optional = set(expected), set(selected), set(allowed)
    hits = gold & actual
    extras = actual - gold - optional
    return {"tp": len(hits), "fn": len(gold - actual), "fp": len(extras),
            "recall": len(hits) / len(gold) if gold else None,
            "missing": sorted(gold - actual), "extra": sorted(extras)}


def score_cases(cases, predictions):
    if len({item["id"] for item in predictions}) != len(predictions):
        raise ValueError("duplicate prediction IDs")
    predicted = {item["id"]: item for item in predictions}
    if set(predicted) != {case["id"] for case in cases}:
        raise ValueError("predictions must contain every case exactly once")
    rows = []
    for case in cases:
        prediction = predicted[case["id"]]
        tool = score_dimension(case["tools"], prediction["tools"], case.get("allowed_tools", []))
        skill = score_dimension(case["skills"], prediction["skills"], case.get("allowed_skills", []))
        attempts = {value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
                    for value in prediction.get("skill_attempts", [])}
        invalid_skills = attempts - set(case["skills"]) - set(case.get("allowed_skills", []))
        failed = bool(prediction.get("error") or prediction.get("budget_exhausted")
                      or prediction.get("tool_errors") or prediction.get("invalid_tool_calls"))
        row = {"id": case["id"], "category": case["category"], "suite": case.get("suite", "legacy"), "tool": tool, "skill": skill,
                     "extra_skill_attempts": sorted(invalid_skills), "failed": failed,
                     "pass": not (tool["fn"] or tool["fp"] or skill["fn"] or skill["fp"] or invalid_skills
                                  or failed or prediction.get("late_skills"))}
        row["failure_tags"], row["primary_failure"] = failure_buckets(case, prediction, row)
        row["measurements"] = measured_case(prediction)
        rows.append(row)
    summary = {}
    for dimension in ("tool", "skill"):
        tp, fn, fp = (sum(row[dimension][key] for row in rows) for key in ("tp", "fn", "fp"))
        positives = [row[dimension]["recall"] for row in rows if row[dimension]["recall"] is not None]
        summary[dimension] = {"micro_recall": tp / (tp + fn) if tp + fn else None,
                              "macro_recall": sum(positives) / len(positives) if positives else None,
                              "precision": tp / (tp + fp) if tp + fp else None,
                              "tp": tp, "fn": fn, "fp": fp}
    negatives = [row for row, case in zip(rows, cases, strict=True) if not case["tools"] and not case["skills"]]
    summary.update(case_count=len(rows), passed=sum(row["pass"] for row in rows),
                   failed_runs=sum(row["failed"] for row in rows),
                   negative_count=len(negatives), negative_passed=sum(row["pass"] for row in negatives))
    groups = {}
    for suite in sorted({case.get("suite", "legacy") for case in cases}):
        selected = [(case, predicted[case["id"]], row) for case, row in zip(cases, rows, strict=True)
                    if case.get("suite", "legacy") == suite]
        group_cases, group_predictions, group_rows = map(list, zip(*selected))
        group = summarize_group(group_cases, group_predictions, group_rows, suite if suite in {"tool", "skill"} else None)
        for dimension in ("tool", "skill"):
            tp, fn, fp = (sum(row[dimension][key] for row in group_rows) for key in ("tp", "fn", "fp"))
            recalls = [row[dimension]["recall"] for row in group_rows if row[dimension]["recall"] is not None]
            group[dimension] = {"tp": tp, "fn": fn, "fp": fp, "micro_recall": tp / (tp + fn) if tp + fn else None,
                                "macro_recall": sum(recalls) / len(recalls) if recalls else None,
                                "precision": tp / (tp + fp) if tp + fp else None}
        groups[suite] = group
    return {"summary": summary, "cases": rows, "groups": groups,
            "measurements": summarize_group(cases, [predicted[case["id"]] for case in cases], rows),
            "execution_statistics": execution_statistics(cases, [predicted[case["id"]] for case in cases])}


def percent(value):
    return f"{100 * value:.1f}%" if value is not None else "N/A"


def meets_thresholds(scored, tool_recall=None, skill_recall=None):
    summary = scored["summary"]
    if summary["failed_runs"]:
        return False
    for dimension, threshold in (("tool", tool_recall), ("skill", skill_recall)):
        recall = summary[dimension]["micro_recall"]
        if threshold is not None and (recall is None or recall < threshold):
            return False
    return True


def render_report(scored, metadata):
    summary = scored["summary"]
    lines = ["# 工具与 Skill 召回评测", "", f"模型：{metadata['model']}；temperature=0；每例最多 {metadata['max_calls']} 次模型调用。",
             f"语料 SHA-256：`{metadata['corpus_sha256']}`。", "",
             "使用生产工具 schema、全局提示和 Skill 目录，通过实际 ReAct 图选择工具；业务执行使用固定模拟回执，load_skill 使用真实加载器。",
             "可用工具采用完整可选能力目录，各 unavailable 用例再移除指定工具；不依赖当前博客目录或飞书连接是否存在。",
             "工具召回统计模型发出的工具名；Skill 召回只统计成功加载的正文，口头声明或失败加载不计入。load_skill 本身不重复计入业务工具。",
             "每例按唯一名称计分，支持多个标签；可选辅助工具不算误选。正例计算 Recall，负例单列，未标注正例的召回率为 N/A。",
             "模拟结果只用于驱动后续选择，不证明参数正确、授权通过、任务执行成功或最终答案正确。", "",
              "| 对象 | Micro Recall | Macro Recall | Precision | 命中 | 漏召回 | 多余选择 |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for name, title in (("tool", "工具"), ("skill", "Skill")):
        row = summary[name]
        lines.append(f"| {title} | {percent(row['micro_recall'])} | {percent(row['macro_recall'])} | {percent(row['precision'])} | {row['tp']} | {row['fn']} | {row['fp']} |")
    if "groups" in scored:
        lines += measurement_report(scored, metadata)
    lines += ["", f"整例严格通过：{summary['passed']}/{summary['case_count']}；负例通过：{summary['negative_passed']}/{summary['negative_count']}；运行异常/截断/工具错误：{summary['failed_runs']}。",
              "负例或 unavailable Skill 的多余加载尝试也会使整例失败；不会用空集合召回率虚增分数。", "",
              "| 用例 | 分组/类别 | 工具漏召回 | 工具误选 | Skill 漏召回 | Skill 多余加载/尝试 | 主失败桶 | 结果 |",
              "|---|---|---|---|---|---|---|---|"]
    for row in scored["cases"]:
        cells = [row["id"], row.get("suite", "legacy") + "/" + row["category"], ", ".join(row["tool"]["missing"]) or "—",
                 ", ".join(row["tool"]["extra"]) or "—", ", ".join(row["skill"]["missing"]) or "—",
                 ", ".join(sorted(set(row["skill"]["extra"] + row["extra_skill_attempts"]))) or "—",
                 row.get("primary_failure") or "—",
                 "PASS" if row["pass"] else ("ERROR/LIMIT" if row["failed"] else "FAIL")]
        lines.append("| " + " | ".join(cells) + " |")
    lines += ["", "Gold 标签未提供给模型。样本为公开的人工标注小语料；本报告属于路由选择回归，不能外推所有用户表达。",
              "逐轮工具名、参数、Skill 加载尝试、错误和 token 用量保存在同名 JSON 文件，支持离线重放计分。", ""]
    return "\n".join(lines)


def measurement_report(scored, metadata):
    def number(value):
        return f"{value:.3f}" if value is not None else "N/A"
    lines = ["", "## 各 50 例的独立统计", "",
             "正确调用率：本组目标名称集合完全命中、无多余选择且无运行错误或截断；Skill 组还要求不多加载、不晚于业务工具加载。",
             "工具组的正确调用率不因关联 Skill 漏加载而降低；整例严格通过另要求工具和 Skill 同时正确。", "",
             "| 分组 | 用例数 | 正确调用率 | 本组 Micro Recall | Precision | 整例严格通过 |",
             "|---|---:|---:|---:|---:|---:|"]
    for suite, group in scored["groups"].items():
        dimension = group.get("primary")
        metric = group.get(dimension, {})
        lines.append(f"| {suite} | {group['case_count']} | {percent(group['correct_call_rate'])} | "
                     f"{percent(metric.get('micro_recall'))} | {percent(metric.get('precision'))} | {group['strict_passed']} |")
    lines += ["", "## 时间（毫秒）", "",
              "从该用例工作线程开始计时，不含线程排队。首决策含本地构建和首个模型请求；路由模型时间统计首请求及产生工具调用的请求，其他无工具回复归为回答阶段。",
              "业务执行时间为模拟工具包装函数时间，不能代表真实搜索、发布、Codex 或 adviser 服务耗时；Skill 执行时间为真实本地加载。未采集到的时间为 N/A。", "",
              "| 分组 | 项目 | 有效样本 | 均值 | P50 | P95 | 最大值 |",
              "|---|---|---:|---:|---:|---:|---:|"]
    for suite, group in scored["groups"].items():
        for metric, stats in group["latency_ms"].items():
            lines.append(f"| {suite} | {metric} | {stats['count']} | {number(stats['mean'])} | {number(stats['p50'])} | {number(stats['p95'])} | {number(stats['max'])} |")
    lines += ["", "### 按实际工具/Skill 调用计时", "",
              "以上执行耗时为每例累计值；下表只统计实际发生的调用，未调用的能力没有零延迟样本。业务工具为模拟执行，不能据此推断真实业务服务耗时。", "",
              "| 类型 | 名称 | 调用数 | 成功数 | 执行均值 ms | P50 ms | P95 ms |", "|---|---|---:|---:|---:|---:|---:|"]
    for item in scored.get("execution_statistics", []):
        stats = item["duration_ms"]
        lines.append(f"| {item['kind']} | {item['name']} | {item['calls']} | {item['successful_calls']} | "
                     f"{number(stats['mean'])} | {number(stats['p50'])} | {number(stats['p95'])} |")
    lines += ["", "## 路由成本与总模型成本", "",
              "路由成本统计首个模型请求及所有产生工具调用的模型请求；总成本另包含最终回复。输入包括常驻提示、工具 schema、Skill 正文及上下文。",
              "成本为已返回 usage 的标价估算；API 失败或缺失 usage 的未知成本不按零处理，以 cost_complete 标记。业务工具均模拟，其真实服务费用未测量。", "",
              "| 分组 | 模型调用 | 输入 tokens | 输出 tokens | 缓存命中输入 | usage 缺失调用 | 路由估算 CNY | 总估算 CNY | 费用完整 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for suite, group in scored["groups"].items():
        lines.append(f"| {suite} | {group['model_calls']} | {group['input_tokens']} | {group['output_tokens']} | "
                     f"{group['cached_input_tokens']} | {group['usage_missing_calls']} | {number(group['estimated_route_cost_cny'])} | "
                     f"{number(group['estimated_total_cost_cny'])} | {group['cost_complete']} |")
    pricing = metadata.get("pricing")
    if pricing:
        lines += ["", f"计价依据：[{pricing['deployment']}]({pricing['source']})，核验日期 {pricing['verified_on']}。",
                  "<=256K 每百万 tokens 输入/输出/缓存命中输入：2/8/0.4 元；更长输入按保存的阶梯计价。不含活动、免费额度及账户专属折扣，非账单实付。"]
    if metadata.get("storage_repair_resumes"):
        lines += ["", "本次有断点存储错误后的恢复：" + "；".join(
                      f"保留 {item['saved_case_count']} 条已保存记录，补跑 {item['unpersisted_case_count']} 条未保存记录"
                      for item in metadata["storage_repair_resumes"]) + "。",
                  "表中费用仅对应最终 100 条有完整记录的观测；中断时未保存的模型请求用量未知，额外费用未计入，不能当作本任务全部实付成本。",
                  "恢复只修复了断点写入，未改变路由、标签、计时或计价。前后执行代码哈希与恢复范围保存在 JSON metadata。"]
    else:
        lines += ["", "本记录没有计价配置，金额为 N/A，不从 token 数猜测账单。"]
    lines += ["", "## 失败分桶", "",
              "主失败桶按 API → 工具错误 → 预算 → Skill 不可用 → 工具漏召回/误选 → Skill 漏召回/误选/晚加载的顺序互斥归因。多标签桶保留全部现象，计数不能相加当作失败用例总数。", "",
              "| 分组 | 失败桶 | 主失败用例数 | 多标签用例数 |", "|---|---|---:|---:|"]
    for suite, group in scored["groups"].items():
        for bucket in sorted(set(group["primary_failure_buckets"]) | set(group["failure_tags"])):
            lines.append(f"| {suite} | {bucket} | {group['primary_failure_buckets'].get(bucket, 0)} | {group['failure_tags'].get(bucket, 0)} |")
    return lines


def write_results(output, metadata, cases, predictions):
    scored = score_cases(cases, predictions)
    scored["measurements"]["task_cost_complete"] = (scored["measurements"]["cost_complete"]
                                                    and not metadata.get("storage_repair_resumes"))
    scored["measurements"]["cost_scope"] = "recorded_cases_only"
    output.write_text(render_report(scored, metadata), encoding="utf-8")
    atomic_json(output.with_suffix(".json"), {"metadata": metadata, "predictions": predictions, "scores": scored})
    with output.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as handle:
        fieldnames = ["id", "suite", "category", "strict_pass", "primary_failure", "failure_tags",
                      "tool_recall", "skill_recall", "tool_missing", "tool_extra", "skill_missing", "skill_extra",
                      "elapsed_ms", "first_decision_ms", "model_total_ms", "route_model_ms", "model_calls",
                      "input_tokens", "output_tokens", "cached_input_tokens", "usage_missing_calls",
                      "estimated_route_cost_cny", "estimated_total_cost_cny", "cost_complete"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in scored["cases"]:
            record = {key: row["measurements"].get(key) for key in fieldnames}
            record.update(id=row["id"], suite=row["suite"], category=row["category"], strict_pass=row["pass"],
                          primary_failure=row["primary_failure"], failure_tags=",".join(row["failure_tags"]),
                          tool_recall=row["tool"]["recall"], skill_recall=row["skill"]["recall"])
            for dimension in ("tool", "skill"):
                for kind in ("missing", "extra"):
                    record[f"{dimension}_{kind}"] = ",".join(row[dimension][kind])
            writer.writerow(record)
    return scored


def atomic_json(path, value):
    """Keep the last good checkpoint if Windows temporarily locks the target."""
    path = path.resolve()
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                            prefix=path.stem + "-", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            handle.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except OSError:
                if attempt == 4:
                    raise
                sleep(0.1 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--predictions", type=Path)
    mode.add_argument("--export-inputs", type=Path, help="export reviewable model inputs locally; no API calls")
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--output", type=Path, default=ROOT / "evaluation" / "CAPABILITY_100_REPORT.md")
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--max-calls", type=int, choices=range(1, 11), default=5)
    parser.add_argument("--repeat", type=int, choices=range(1, 11), default=1)
    parser.add_argument("--min-tool-recall", type=float)
    parser.add_argument("--min-skill-recall", type=float)
    parser.add_argument("--pricing-file", type=Path, default=ROOT / "evaluation" / "qwen37_pricing.json")
    parser.add_argument("--resume", type=Path, help="resume missing cases from a partial JSON; keep failed cases")
    parser.add_argument("--compatible-harness-sha256", help="explicit old hash for a reviewed storage-only resume repair")
    args = parser.parse_args()
    corpus = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = validate_cases(corpus)
    for threshold in (args.min_tool_recall, args.min_skill_recall):
        if threshold is not None and not 0 <= threshold <= 1:
            parser.error("recall thresholds must be between 0 and 1")
    if args.export_inputs:
        args.export_inputs.write_text(json.dumps(export_inputs(corpus, cases), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Exported {len(cases)} public cases and the possible model inputs; no API calls.")
        return 0
    if args.live:
        from tele_bot.config.llm.settings import AliBailianSettings
        from tele_bot.llm.react_client import build_chat_openai
        settings = AliBailianSettings.from_env()
        pricing = json.loads(args.pricing_file.read_text(encoding="utf-8"))
        if settings.model not in pricing["model_ids"]:
            raise ValueError("configured model does not match the explicit pricing profile")
        base_model = build_chat_openai(api_key=settings.api_key, base_url=settings.base_url, model=settings.model,
                                       temperature=0, timeout=min(settings.timeout_seconds, 60), max_retries=0)
        metadata = {"model": settings.model, "max_calls": args.max_calls, "corpus_sha256": corpus_hash(corpus),
                    "repeats": args.repeat, "business_execution": "simulated", "skill_loading": "real",
                    "pricing": pricing, "workers": args.workers, "benchmark_version": 2,
                    "started_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
                    "harness_sha256": hashlib.sha256(Path(__file__).read_bytes() + Path(__file__).with_name("capability_metrics.py").read_bytes()).hexdigest()}
        if args.repeat > 1:
            cases = [{**case, "id": f"{case['id']}@{repeat + 1}"} for repeat in range(args.repeat) for case in cases]
        predictions = []
        if args.resume:
            partial = json.loads(args.resume.read_text(encoding="utf-8"))
            for key in ("model", "max_calls", "corpus_sha256", "repeats", "pricing", "harness_sha256"):
                if partial["metadata"][key] != metadata[key]:
                    if key != "harness_sha256" or args.compatible_harness_sha256 != partial["metadata"][key]:
                        raise ValueError(f"resume metadata mismatch: {key}")
            current_hash = metadata["harness_sha256"]
            metadata = partial["metadata"]
            predictions = partial["predictions"]
            if metadata["harness_sha256"] != current_hash:
                metadata.setdefault("storage_repair_resumes", []).append({
                    "previous_harness_sha256": metadata["harness_sha256"], "current_harness_sha256": current_hash,
                    "saved_case_count": len(predictions), "unpersisted_case_count": len(cases) - len(predictions),
                    "prior_unpersisted_request_cost": "unknown; excluded from observed 100-case estimates",
                    "reason": "reviewed checkpoint persistence repair; routing, fixtures, timing and pricing unchanged",
                })
                metadata["harness_sha256"] = current_hash
        completed = {prediction["id"] for prediction in predictions}
        if len(completed) != len(predictions) or completed - {case["id"] for case in cases}:
            raise ValueError("invalid IDs in resume checkpoint")
        partial_path = args.output.with_suffix(".partial.json")
        run_started = perf_counter()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(run_case, case, lambda tools: base_model.bind_tools(tools, max_tokens=4096),
                                   max_calls=args.max_calls, pricing=pricing): case["id"] for case in cases if case["id"] not in completed}
            for future in as_completed(futures):
                prediction = future.result()
                predictions.append(prediction)
                try:
                    atomic_json(partial_path, {"metadata": metadata, "predictions": predictions})
                except OSError as exc:
                    metadata.setdefault("checkpoint_errors", []).append({"completed": len(predictions), "error": str(exc)})
                    print(f"Checkpoint save failed; retaining results in memory: {type(exc).__name__}", flush=True)
                print(f"{len(predictions)}/{len(cases)} {prediction['id']} {prediction['timing']['elapsed_ms'] / 1000:.1f}s", flush=True)
        metadata["last_segment_wall_seconds"] = perf_counter() - run_started
        order = {case["id"]: i for i, case in enumerate(cases)}
        predictions.sort(key=lambda item: order[item["id"]])
    else:
        saved = json.loads(args.predictions.read_text(encoding="utf-8"))
        metadata, predictions = saved["metadata"], saved["predictions"]
        if metadata["corpus_sha256"] != corpus_hash(corpus):
            raise ValueError("saved predictions do not match this labeled corpus")
        if metadata.get("repeats", 1) > 1:
            cases = [{**case, "id": f"{case['id']}@{repeat + 1}"}
                     for repeat in range(metadata["repeats"]) for case in cases]
    scored = write_results(args.output, metadata, cases, predictions)
    print(json.dumps(scored["summary"], ensure_ascii=False, indent=2))
    return 0 if meets_thresholds(scored, args.min_tool_recall, args.min_skill_recall) else 1


if __name__ == "__main__":
    raise SystemExit(main())
