"""
Adviser tool — wraps `opencli chatgpt ask` as a second-opinion adviser the
main ReAct agent can consult.

Design notes:
- Stateless: each call uses `--new` (no multi-turn ChatGPT session).
- High latency: ChatGPT-via-opencli can take 1-5 minutes; timeout defaults to
  180s — pragmatic ceiling that drops requests stuck in the long tail rather
  than blocking the main agent for full p99 latency.
- Failure-degrades: if opencli errors or times out, returns a degraded string
  instead of raising, so the main agent can continue without the second opinion.
- In-session cache: identical (question, context) tuples within the same
  AdviserTool instance return the cached reply, avoiding redundant multi-minute
  waits. Cache is bounded (default 16 entries, LRU).
- Progress callback: optional hook for future Phase 4-B streaming progress —
  called with a short status string before the long-running subprocess starts.
- Outbound sanitisation: the `context` parameter is scrubbed of secrets
  (API keys, tokens, .env file contents, password-shaped strings) BEFORE
  hitting the network. Defence-in-depth: even if the main agent passes raw
  sensitive content, it never reaches ChatGPT.
- Degraded-reply marker: the `DEGRADED_MARKER` prefix lets the executor layer
  detect failure mode programmatically and strip it from user-facing text —
  the "don't mention adviser failure" rule no longer relies only on LLM
  obedience to the prompt.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

ADVISER_SYSTEM_PROMPT = (
    "You are a senior software architect acting as an adviser to another AI agent.\n\n"
    "Your role:\n"
    "- Give one clear recommendation and the main tradeoff.\n"
    "- Disagree when warranted; flag risks the agent may have missed.\n"
    "- Be concise: 1-5 sentences, no preamble, no apologies.\n"
    "- When reviewing code, point to specific lines/patterns, not vague concerns.\n"
    "- When uncertain, say so explicitly — don't fabricate confidence.\n\n"
    "You are NOT the executor. The agent will act on or ignore your advice."
)

# Sentinel prefix on every degraded reply. Executor layer scans final
# AIMessage content for this marker and strips/replaces before sending
# to Telegram. Tests assert this prefix is stable.
DEGRADED_MARKER = "[ADVISER_DEGRADED]"

DEGRADED_REPLY = (
    f"{DEGRADED_MARKER} ⚠️ adviser 暂不可用（{{reason}}）。请基于现有信息自行决定，"
    "不要在 reply 中提及 adviser 失败的细节。"
)

# Patterns for outbound context sanitisation. Different from log sanitisation:
# here we are protecting against data exfiltration to an external LLM service,
# not just log readability.
_SECRET_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # KEY=value forms commonly seen in .env (sk-..., ghp_..., AIza...)
    re.compile(r"(?im)^([A-Z_][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|API_KEY))\s*=.*$"),
    # Inline assignments in code/JSON: api_key = "sk-..." / "token": "..."
    re.compile(r'(?i)(api[_-]?key|access[_-]?token|secret[_-]?key|password|passwd)\s*[:=]\s*["\']?[^\s"\']{6,}["\']?'),
    # Long token-shaped strings: sk-* (OpenAI), ghp_* (GitHub), AIza* (Google)
    re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{20,})\b"),
)

_REDACTED = "<redacted>"


def sanitize_for_adviser(text: str) -> str:
    """Strip secret-shaped content before sending to the external adviser.

    Preserves structure (line counts, code shape) so the adviser can still
    give useful advice — only replaces the secret value, not the surrounding
    context.
    """
    if not text:
        return text
    sanitized = text
    for pattern in _SECRET_LINE_PATTERNS:
        sanitized = pattern.sub(
            lambda m: (
                f"{m.group(1)}={_REDACTED}" if "=" in m.group(0)[:30] and m.group(1) and m.group(1).isupper()
                else _REDACTED
            ),
            sanitized,
        )
    return sanitized

ProgressCallback = Callable[[str], None]


@dataclass
class AdviserTool:
    timeout_seconds: int = 180
    cache_size: int = 16
    progress_callback: ProgressCallback | None = None
    _cache: "OrderedDict[str, str]" = field(default_factory=OrderedDict, init=False, repr=False)

    def consult(self, question: str, context: str = "") -> str:
        q = question.strip()
        if not q:
            raise ValueError("question is required")

        # Sanitise outbound context BEFORE caching, prompt-building, or network.
        # Cache key uses the sanitised form so equivalent secret-bearing inputs
        # collapse to one cache entry.
        ctx = sanitize_for_adviser(context.strip())
        cache_key = self._make_cache_key(q, ctx)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        prompt = self._build_prompt(q, ctx)
        self._emit_progress(f"💬 正在咨询 adviser：『{q[:80]}』 (1-5 分钟)")

        try:
            completed = subprocess.run(
                ["opencli", "chatgpt", "ask", "--new", "-f", "plain", prompt],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return DEGRADED_REPLY.format(reason=f"timeout after {self.timeout_seconds}s")
        except FileNotFoundError:
            return DEGRADED_REPLY.format(reason="opencli binary not found")
        except Exception as exc:  # noqa: BLE001 — degrade rather than crash main loop
            return DEGRADED_REPLY.format(reason=f"unexpected error: {type(exc).__name__}")

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()[:200]
            return DEGRADED_REPLY.format(reason=f"opencli exit {completed.returncode}: {stderr}")

        reply = self._extract_reply(completed.stdout)
        if not reply:
            return DEGRADED_REPLY.format(reason="empty response from adviser")

        self._cache_put(cache_key, reply)
        return reply

    def _build_prompt(self, question: str, context: str) -> str:
        parts = [ADVISER_SYSTEM_PROMPT, ""]
        if context:
            parts.extend(["Context:", context, ""])
        parts.extend(["Question:", question])
        return "\n".join(parts)

    @staticmethod
    def _make_cache_key(question: str, context: str) -> str:
        digest = hashlib.sha256()
        digest.update(question.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(context.encode("utf-8"))
        return digest.hexdigest()

    def _cache_get(self, key: str) -> str | None:
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def _cache_put(self, key: str, value: str) -> None:
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

    def _emit_progress(self, message: str) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(message)
        except Exception:  # noqa: BLE001 — progress is best-effort
            pass

    @staticmethod
    def _extract_reply(stdout: str) -> str:
        """Strip opencli plain-format metadata. Output looks like:

            conversationId: <uuid>
            conversationUrl: https://chatgpt.com/c/<uuid>
            response: <actual reply, possibly multiline>

        We want only the content after the `response:` marker. Fall back to
        raw stripped text if no marker is found (older opencli format).
        """
        text = stdout.strip()
        if not text:
            return ""
        marker = re.search(r"(?im)^response:\s*", text)
        if marker:
            return text[marker.end():].strip()
        return text
