"""Explicit Codex command parser."""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class CodexCommand:
    mode: str
    prompt: str
    workspace: str | None = None


EXPLICIT_PREFIX = "/codex"


def parse_codex_command(text: str) -> CodexCommand | None:
    """Parse only an explicit /codex command; natural language never matches."""
    parts = text.strip().split(maxsplit=2)
    if not parts or parts[0].lower() != EXPLICIT_PREFIX:
        return None
    if len(parts) < 2 or parts[1].lower() not in {"inspect", "plan", "apply"}:
        raise ValueError(
            "用法：/codex inspect <任务>、/codex plan <任务> 或 /codex apply <任务或令牌>"
        )
    if len(parts) < 3 or not parts[2].strip():
        raise ValueError("Codex 命令缺少任务或确认令牌")
    prompt = parts[2].strip()
    workspace = None
    match = re.search(r"(?:在|目录|workspace)\s+([A-Za-z]:\\[^\s，。；;]+|/[^\s，。；;]+)", prompt, re.IGNORECASE)
    if match:
        workspace = match.group(1).rstrip(".,，。；;")
    return CodexCommand(mode=parts[1].lower(), prompt=prompt, workspace=workspace)
