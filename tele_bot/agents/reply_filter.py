"""
Outbound reply filters — strip internal markers from user-facing text.

These run AFTER the ReAct loop, BEFORE sending text via TelegramAdapter.
The intent is to prevent any internal sentinel (e.g. ADVISER_DEGRADED marker)
from reaching the user, even if the LLM ignores prompt instructions to omit it.
"""

from __future__ import annotations

import re

from tele_bot.tools.adviser import DEGRADED_MARKER

# Regex matches the full degraded line — marker + adviser failure message,
# in either Chinese or any phrasing the LLM might surface.
_ADVISER_DEGRADED_LINE = re.compile(
    re.escape(DEGRADED_MARKER) + r".*?(?:\n|$)",
    re.DOTALL,
)

# Heuristic: catch LLM-paraphrased "adviser unavailable" text even without
# the marker, in case the model only includes the explanatory tail.
_ADVISER_PARAPHRASE = re.compile(
    r"(?:⚠️\s*)?[^\n]{0,40}\badviser\b[^\n]{0,80}(?:暂不可用|unavailable|失败|degraded)[^\n]*\n?",
    re.IGNORECASE,
)


def filter_outbound_reply(text: str) -> str:
    """Remove adviser-failure markers/paraphrases from user-bound text.

    Empty or whitespace-only result is preserved as-is; the executor decides
    whether to substitute a fallback message.
    """
    if not text:
        return text
    cleaned = _ADVISER_DEGRADED_LINE.sub("", text)
    cleaned = _ADVISER_PARAPHRASE.sub("", cleaned)
    # Collapse runs of blank lines created by removals.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()
