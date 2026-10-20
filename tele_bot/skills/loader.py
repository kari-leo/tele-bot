"""Bounded Skill catalog and explicit per-request loading of behavioral guidance."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import warnings

_SKILLS_DIR = Path(__file__).resolve().parent
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
MAX_SKILL_FILES = 20
MAX_PROMPT_CHARS = 12_000
MAX_BODY_CHARS = 20_000


def _strip_frontmatter(text: str) -> str:
    match = _FRONTMATTER_RE.match(text)
    return text[match.end():].lstrip() if match else text.lstrip()


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    required_tools: tuple[str, ...] = ()
    global_rule: bool = False


class SkillLoader:
    def __init__(self, skills_dir: Path | None = None) -> None:
        self.skills_dir = skills_dir or _SKILLS_DIR
        self._skills: dict[str, Skill] | None = None
        self.available_tools: set[str] | None = None

    def _read(self) -> dict[str, Skill]:
        if self._skills is not None:
            return self._skills
        files = sorted(self.skills_dir.glob("*.md"))
        if len(files) > MAX_SKILL_FILES:
            raise RuntimeError(f"SkillLoader: file count exceeds {MAX_SKILL_FILES}")
        skills = {}
        for file in files:
            try:
                raw = file.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError) as exc:
                warnings.warn(f"SkillLoader: unavailable {file.name}: {exc}", UserWarning)
                continue
            match = _FRONTMATTER_RE.match(raw)
            metadata = {}
            if match:
                for line in match[1].splitlines():
                    key, sep, value = line.partition(":")
                    if sep:
                        metadata[key.strip()] = value.strip().strip("\"'")
            body = _strip_frontmatter(raw).strip()
            if not body:
                continue
            name = metadata.get("name", file.stem)
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", name) or name in skills:
                raise ValueError(f"invalid or duplicate Skill name: {name}")
            description = metadata.get("description", body.splitlines()[0].lstrip("# "))
            if len(description) > 600:
                raise ValueError(f"Skill description exceeds 600 characters: {name}")
            required = tuple(value.strip() for value in metadata.get("tools", metadata.get("tool", "")).split(",") if value.strip())
            skills[name] = Skill(name, description, body, required, metadata.get("global", "false").lower() == "true")
        self._skills = skills
        return skills

    def load(self) -> list[tuple[str, str]]:
        """Compatibility read API. System prompts use catalog(), not all bodies."""
        return [(skill.name, skill.body) for skill in self._read().values()]

    def missing_tools(self, skill: Skill) -> list[str]:
        if self.available_tools is None:
            return []
        return sorted(set(skill.required_tools) - self.available_tools)

    def catalog(self) -> str:
        lines = []
        for skill in self._read().values():
            if skill.global_rule:
                continue
            missing = self.missing_tools(skill)
            availability = f" [unavailable: missing {', '.join(missing)}]" if missing else ""
            lines.append(f"- {skill.name}: {skill.description}{availability}")
        return "\n".join(lines)

    def load_skill(self, name: str) -> dict:
        skill = self._read().get(name)
        if skill is None or skill.global_rule:
            return {"status": "unavailable", "name": name, "reason": "unknown domain Skill; use catalog names"}
        missing = self.missing_tools(skill)
        if missing:
            return {"status": "unavailable", "name": name, "missing_tools": missing,
                    "reason": "Skill guidance cannot enable tools; explain capability limits"}
        if len(skill.body) > MAX_BODY_CHARS:
            return {"status": "unavailable", "name": name, "reason": "Skill body exceeds explicit size limit"}
        return {"status": "loaded", "name": name, "body": skill.body}

    def build_system_prompt(self) -> str:
        parts = [skill.body for skill in self._read().values() if skill.global_rule]
        catalog = self.catalog()
        if catalog:
            parts.append("Skills are behavioral guidance, not executable tools or permission grants. "
                         "Before using a relevant domain capability, call load_skill with its exact catalog name. "
                         "Load every relevant Skill if several match; do not load unrelated Skills. "
                         "If selection fails or a Skill is unavailable, use registered tool descriptions, "
                         "preserve global rules, and state any capability limits. Never invent a tool.\n\nSkill catalog:\n" + catalog)
        prompt = "\n\n".join(parts)
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError("global rules and Skill catalog exceed prompt budget; no requirements were truncated")
        return prompt

    def prepare_request(self, messages, base_prompt: str):
        """Pin current Skills; replace historical bodies by receipts in model view.

        Full original results remain in checkpoint storage. Active Skills are
        not cached across requests or users sharing a chat.
        """
        from langchain_core.messages import HumanMessage, ToolMessage
        current = max((i for i, message in enumerate(messages) if isinstance(message, HumanMessage)), default=-1)
        names, view = [], []
        for index, message in enumerate(messages):
            if isinstance(message, ToolMessage) and message.name == "load_skill":
                try:
                    result = json.loads(message.content)
                except (TypeError, ValueError):
                    result = {}
                if index > current and result.get("status") == "loaded" and result.get("name") not in names:
                    names.append(result.get("name"))
                receipt = {key: value for key, value in result.items() if key != "body"}
                view.append(message.model_copy(update={"content": json.dumps(receipt, ensure_ascii=False)}))
            else:
                view.append(message)
        parts = [base_prompt]
        for name in names:
            result = self.load_skill(name)
            if result["status"] == "loaded":
                parts.append(f"## Active Skill: {name}\n\n{result['body']}")
        prompt = "\n\n".join(parts)
        if len(prompt) > MAX_PROMPT_CHARS + MAX_BODY_CHARS:
            raise ValueError("active Skills exceed explicit prompt budget; split the task")
        return view, prompt


def build_skill_tool(loader: SkillLoader):
    from langchain_core.tools import tool

    @tool
    def load_skill(name: str) -> str:
        """Load the complete behavioral guidance for a relevant Skill from the catalog.

        name must be an exact catalog name. This grants no tool permissions.
        Multiple matching Skills can be loaded independently. On unavailable
        or unknown Skills follow global rules and registered tool descriptions.
        """
        return json.dumps(loader.load_skill(name), ensure_ascii=False)

    return load_skill
