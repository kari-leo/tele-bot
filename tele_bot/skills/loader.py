"""
SkillLoader — reads tele_bot/skills/*.md and builds a system prompt.

Each skill file may begin with a YAML front-matter block delimited by `---`.
The rest of the file is the skill body (plain markdown).

The loader concatenates all skill bodies into a single system-prompt string,
separated by `---` dividers so the LLM can distinguish skill boundaries.

Runtime constraints
-------------------
MAX_SKILL_FILES = 20
    Prevents runaway discovery if the skills dir accumulates stale files.
    Raises RuntimeError on violation.

MAX_PROMPT_CHARS = 12_000
    Keeps the system prompt well within the model's context window.
    Excess chars are truncated and a UserWarning is emitted so that
    monitoring (log capture, Sentry, etc.) can alert on the condition
    without crashing the agent.

Skipped files
    Unreadable files (OSError) are skipped and a UserWarning is emitted.
    This makes the "graceful skip" observable through standard Python
    warning infrastructure.

Usage::

    loader = SkillLoader()               # default: tele_bot/skills/
    prompt = loader.build_system_prompt()  # "" if no .md files found
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path

_SKILLS_DIR = Path(__file__).resolve().parent
_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)

MAX_SKILL_FILES = 20
MAX_PROMPT_CHARS = 12_000


def _strip_frontmatter(text: str) -> str:
    m = _FRONTMATTER_RE.match(text)
    return text[m.end():].lstrip() if m else text.lstrip()


class SkillLoader:
    """Load skill descriptions from a directory of markdown files."""

    def __init__(self, skills_dir: Path | None = None) -> None:
        self.skills_dir = skills_dir or _SKILLS_DIR

    def load(self) -> list[tuple[str, str]]:
        """Return [(skill_name, skill_body), ...] sorted by filename.

        Raises RuntimeError if more than MAX_SKILL_FILES .md files are found.
        Emits UserWarning and skips unreadable files (OSError).
        """
        files = sorted(self.skills_dir.glob("*.md"))
        if len(files) > MAX_SKILL_FILES:
            raise RuntimeError(
                f"SkillLoader: found {len(files)} skill files in {self.skills_dir}; "
                f"limit is {MAX_SKILL_FILES}. Remove stale files or raise MAX_SKILL_FILES."
            )
        result = []
        for f in files:
            try:
                raw = f.read_text(encoding="utf-8")
            except OSError as exc:
                warnings.warn(
                    f"SkillLoader: could not read {f} ({exc}); skipping.",
                    UserWarning,
                    stacklevel=2,
                )
                continue
            body = _strip_frontmatter(raw)
            if body:
                result.append((f.stem, body))
        return result

    def build_system_prompt(self) -> str:
        """Concatenate all skill bodies into one system-prompt string.

        Returns an empty string when no skill files are found.
        Truncates at MAX_PROMPT_CHARS and emits UserWarning if exceeded.
        """
        skills = self.load()
        if not skills:
            return ""
        parts = [f"## Skill: {name}\n\n{body}" for name, body in skills]
        prompt = "\n\n---\n\n".join(parts)
        if len(prompt) > MAX_PROMPT_CHARS:
            warnings.warn(
                f"SkillLoader: system prompt is {len(prompt)} chars, "
                f"truncating to {MAX_PROMPT_CHARS}. Trim skill files to avoid this.",
                UserWarning,
                stacklevel=2,
            )
            prompt = prompt[:MAX_PROMPT_CHARS]
        return prompt
