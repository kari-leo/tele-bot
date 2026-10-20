"""Latency, pricing and failure attribution for replayable routing evaluations."""

from collections import Counter
import math


def distribution(values):
    values = sorted(value for value in values if value is not None and math.isfinite(value))
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "max": None}
    def percentile(fraction):
        position = (len(values) - 1) * fraction
        low, high = math.floor(position), math.ceil(position)
        return values[low] + (values[high] - values[low]) * (position - low)
    return {"count": len(values), "mean": sum(values) / len(values),
            "p50": percentile(0.5), "p95": percentile(0.95), "max": values[-1]}


def normalized_usage(response):
    usage = getattr(response, "usage_metadata", None) or {}
    raw = (getattr(response, "response_metadata", None) or {}).get("token_usage", {}) or {}
    input_tokens = usage.get("input_tokens", raw.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", raw.get("completion_tokens"))
    details = usage.get("input_token_details", {}) or {}
    cached = details.get("cache_read", (raw.get("prompt_tokens_details", {}) or {}).get("cached_tokens"))
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "cached_input_tokens": cached, "usage_reported": input_tokens is not None and output_tokens is not None}


def price_call(usage, profile):
    if not profile or not usage.get("usage_reported"):
        return None
    incoming, outgoing = usage["input_tokens"], usage["output_tokens"]
    if incoming < 0 or outgoing < 0:
        raise ValueError("token usage cannot be negative")
    tier = next((tier for tier in profile["tiers"] if incoming <= tier["max_input_tokens"]), None)
    if tier is None:
        raise ValueError("input tokens exceed supported pricing tiers")
    cached = min(incoming, max(0, usage.get("cached_input_tokens") or 0))
    return ((incoming - cached) * tier["input_per_million"]
            + cached * tier["cached_input_per_million"] + outgoing * tier["output_per_million"]) / 1_000_000


BUCKET_ORDER = (
    "api_timeout", "api_rate_limit", "api_network", "api_error", "invalid_tool_name",
    "invalid_arguments", "invalid_tool_call", "budget_exhausted", "skill_unavailable",
    "tool_missing", "tool_extra", "skill_missing", "skill_extra", "skill_loaded_late", "other_runtime_error",
)


def failure_buckets(case, prediction, row):
    tags = set()
    error = prediction.get("error") or ""
    lower = error.lower()
    if error:
        if "timeout" in lower or "timed out" in lower:
            tags.add("api_timeout")
        elif "ratelimit" in lower or "429" in lower:
            tags.add("api_rate_limit")
        elif "connection" in lower or "network" in lower:
            tags.add("api_network")
        elif any(term in lower for term in ("api", "401", "403", "500", "502", "503")):
            tags.add("api_error")
        else:
            tags.add("other_runtime_error")
    for failure in prediction.get("tool_errors", []):
        text = failure["error"].lower()
        tags.add("invalid_tool_name" if "not a valid tool" in text else "invalid_arguments")
    if prediction.get("invalid_tool_calls"):
        tags.add("invalid_tool_call")
    if prediction.get("budget_exhausted"):
        tags.add("budget_exhausted")
    for dimension in ("tool", "skill"):
        if row[dimension]["fn"]:
            tags.add(dimension + "_missing")
        if row[dimension]["fp"]:
            tags.add(dimension + "_extra")
    if row["extra_skill_attempts"]:
        tags.add("skill_extra")
    if any(event.get("status") == "unavailable" for event in prediction.get("execution_events", [])):
        tags.add("skill_unavailable")
    if prediction.get("late_skills"):
        tags.add("skill_loaded_late")
    ordered = [bucket for bucket in BUCKET_ORDER if bucket in tags]
    return ordered, ordered[0] if ordered else None


def measured_case(prediction):
    trace = prediction.get("trace", [])
    decisions = [step for i, step in enumerate(trace) if i == 0 or step.get("tool_calls") or step.get("invalid_tool_calls")]
    costs = [step.get("estimated_cost_cny") for step in trace]
    route_costs = [step.get("estimated_cost_cny") for step in decisions]
    timing = prediction.get("timing", {})
    events = prediction.get("execution_events", [])
    return {**timing,
            "model_calls": prediction.get("model_calls", len(trace)),
            "input_tokens": sum(step.get("usage", {}).get("input_tokens") or 0 for step in trace),
            "output_tokens": sum(step.get("usage", {}).get("output_tokens") or 0 for step in trace),
            "cached_input_tokens": sum(step.get("usage", {}).get("cached_input_tokens") or 0 for step in trace),
            "usage_missing_calls": sum(not step.get("usage", {}).get("usage_reported", False) for step in trace),
            "route_model_ms": sum(step.get("latency_ms", 0) for step in decisions) if trace and "latency_ms" in trace[0] else None,
            "estimated_total_cost_cny": sum(cost for cost in costs if cost is not None) if any(cost is not None for cost in costs) else None,
            "estimated_route_cost_cny": sum(cost for cost in route_costs if cost is not None) if any(cost is not None for cost in route_costs) else None,
            "cost_complete": bool(trace) and all(cost is not None for cost in costs),
            "skill_execution_ms": sum(event["duration_ms"] for event in events if event["name"] == "load_skill") if "execution_events" in prediction else None,
            "mock_business_execution_ms": sum(event["duration_ms"] for event in events if event["name"] != "load_skill") if "execution_events" in prediction else None}


def execution_statistics(cases, predictions):
    """Per actual call, so missing Skill calls never become zero-latency samples."""
    groups = {}
    for case in cases:
        for kind, names in (("tool", case["tools"]), ("skill", case["skills"])):
            for name in names:
                groups.setdefault((kind, name), [])
    for prediction in predictions:
        for event in prediction.get("execution_events", []):
            kind = "skill" if event["name"] == "load_skill" else "tool"
            name = event.get("skill") if kind == "skill" else event["name"]
            groups.setdefault((kind, name or "invalid-name"), []).append(event)
    return [{"kind": kind, "name": name, "execution": "real_skill_load" if kind == "skill" else "simulated",
             "calls": len(events), "successful_calls": sum(event["status"] in {"loaded", "complete"} for event in events),
             "duration_ms": distribution([event["duration_ms"] for event in events])}
            for (kind, name), events in sorted(groups.items())]


def summarize_group(cases, predictions, scored_rows, primary=None):
    metrics = [measured_case(prediction) for prediction in predictions]
    exact = sum(not row[primary]["fn"] and not row[primary]["fp"] and not row["failed"]
                and (primary != "skill" or not row["extra_skill_attempts"] and not prediction.get("late_skills"))
                for row, prediction in zip(scored_rows, predictions, strict=True)) if primary else None
    latencies = {key: distribution([item.get(key) for item in metrics]) for key in
                 ("elapsed_ms", "first_decision_ms", "first_business_selection_ms", "first_skill_selection_ms",
                  "model_total_ms", "route_model_ms", "skill_execution_ms", "mock_business_execution_ms")}
    primary_buckets, tags = Counter(), Counter()
    for case, prediction, row in zip(cases, predictions, scored_rows, strict=True):
        buckets, first = failure_buckets(case, prediction, row)
        tags.update(buckets)
        if first:
            primary_buckets[first] += 1
    costs = [item["estimated_total_cost_cny"] for item in metrics if item["estimated_total_cost_cny"] is not None]
    route_costs = [item["estimated_route_cost_cny"] for item in metrics if item["estimated_route_cost_cny"] is not None]
    return {"case_count": len(cases), "primary": primary, "correct_cases": exact,
            "correct_call_rate": exact / len(cases) if primary and cases else None,
            "strict_passed": sum(row["pass"] for row in scored_rows),
            "latency_ms": latencies, "model_calls": sum(item["model_calls"] for item in metrics),
            "input_tokens": sum(item["input_tokens"] for item in metrics),
            "output_tokens": sum(item["output_tokens"] for item in metrics),
            "cached_input_tokens": sum(item["cached_input_tokens"] for item in metrics),
            "usage_missing_calls": sum(item["usage_missing_calls"] for item in metrics),
            "estimated_total_cost_cny": sum(costs) if costs else None,
            "estimated_route_cost_cny": sum(route_costs) if route_costs else None,
            "cost_complete": all(item["cost_complete"] for item in metrics),
            "primary_failure_buckets": dict(primary_buckets), "failure_tags": dict(tags)}
