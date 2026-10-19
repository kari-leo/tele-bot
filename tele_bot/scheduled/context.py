"""Bounded scheduling references from the current ReAct model view."""

import json

from tele_bot.workflows.context_manager import clip_text, text_tokens


def scheduling_context(messages, *, budget=8000):
    """Keep recent roles/results without turning them into user authorization."""
    summaries = [{"role": "reference", "text": clip_text(str(m.content), 1800)}
                 for m in messages or [] if m.type == "human" and
                 str(m.content).startswith("Earlier context (reference only):")]
    summaries = summaries[-1:]
    records = []
    for message in reversed(messages or []):
        if message.type == "system" or str(message.content).startswith("Earlier context (reference only):"):
            continue
        record = {"role": message.type, "text": clip_text(str(message.content), 1400)}
        if getattr(message, "name", None):
            record["tool"] = message.name
        calls = getattr(message, "tool_calls", None)
        if calls:
            record["tool_calls"] = clip_text(json.dumps(calls, ensure_ascii=False), 1000)
        if text_tokens(json.dumps([*summaries, record, *records], ensure_ascii=False)) > budget:
            break
        records.insert(0, record)
    return json.dumps([*summaries, *records], ensure_ascii=False) if summaries or records else ""
