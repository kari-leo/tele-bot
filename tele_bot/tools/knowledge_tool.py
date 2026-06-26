from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from tele_bot.llm.opencli_gateway import OpenCLIGateway
from tele_bot.skills import RESTORE_PROMPT


@dataclass(frozen=True)
class KnowledgeTool:
    gateway: OpenCLIGateway

    def restore_knowledge(self, source: str, output_path: str | None = None) -> str:
        normalized_source = source.strip()
        if not normalized_source:
            raise ValueError("source is required")

        source_path = Path(normalized_source)
        if self._looks_like_path(normalized_source) and self._path_exists(source_path):
            distilled_text = source_path.read_text(encoding="utf-8")
        else:
            distilled_text = normalized_source

        resolved_output_path = (
            Path(output_path) if output_path
            else self._default_text_output_path(distilled_text)
        )

        prompt = f"{RESTORE_PROMPT}\n\nDistilled note:\n\n{distilled_text.strip()}"
        restored = self.gateway.chatgpt_ask(prompt, new=True)
        markdown = self._extract_markdown(restored)

        resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_output_path.write_text(markdown.strip() + "\n", encoding="utf-8")
        return str(resolved_output_path)

    def _default_text_output_path(self, distilled_text: str) -> Path:
        repo_root = Path(__file__).resolve().parents[2]
        title_match = re.search(r"^#\s+(.+)$", distilled_text, flags=re.MULTILINE)
        slug_source = title_match.group(1) if title_match else "restored_note"
        slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", slug_source.lower()).strip("_")
        if not slug:
            slug = "restored_note"
        reports_dir = repo_root / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        return reports_dir / f"{slug}_restored.md"

    @staticmethod
    def _extract_markdown(response_text: str) -> str:
        normalized_response = KnowledgeTool._strip_opencli_metadata(response_text)
        fenced_match = re.search(r"~~~(?:markdown)?\n([\s\S]*?)\n~~~", normalized_response)
        if fenced_match:
            return fenced_match.group(1).strip()
        return normalized_response.strip()

    @staticmethod
    def _strip_opencli_metadata(response_text: str) -> str:
        response_marker = re.search(r"(?im)^response:\s*", response_text)
        if response_marker:
            return response_text[response_marker.end() :].lstrip()
        return response_text

    @staticmethod
    def _looks_like_path(source: str) -> bool:
        if "\n" in source or "\r" in source:
            return False
        if source.startswith("#") or "```" in source or source.startswith("-"):
            return False
        return True

    @staticmethod
    def _path_exists(path: Path) -> bool:
        try:
            return path.exists()
        except OSError:
            return False