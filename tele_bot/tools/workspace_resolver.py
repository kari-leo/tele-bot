"""Conservative discovery of workspaces from fuzzy natural-language hints."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from tele_bot.tools.workspace_policy import WorkspacePolicy

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}
_PROJECT_MARKERS = {
    ".git",
    "pyproject.toml",
    "package.json",
    "requirements.txt",
    "README.md",
    "Cargo.toml",
    "pom.xml",
}


@dataclass(frozen=True)
class WorkspaceCandidate:
    path: Path
    reason: str


class WorkspaceResolver:
    def __init__(
        self,
        policy: WorkspacePolicy,
        search_roots: tuple[Path, ...] | None = None,
        max_depth: int = 3,
        max_candidates: int = 8,
    ) -> None:
        self.policy = policy
        self.search_roots = search_roots or policy.allowed_roots
        self.max_depth = max_depth
        self.max_candidates = max_candidates

    def find(self, hint: str) -> list[WorkspaceCandidate]:
        terms = self._terms(hint)
        if not terms:
            return []
        candidates: list[WorkspaceCandidate] = []
        for root in self.search_roots:
            if not root.is_dir():
                continue
            for current, directories, files in os.walk(root):
                current_path = Path(current)
                depth = len(current_path.relative_to(root).parts)
                directories[:] = [item for item in directories if item not in _SKIP_DIRS and not item.startswith(".")]
                if depth > self.max_depth:
                    directories[:] = []
                    continue
                name = current_path.name.lower()
                path_text = str(current_path).lower()
                matched = [term for term in terms if term in name or term in path_text]
                if matched and self._looks_like_project(files, directories):
                    candidates.append(WorkspaceCandidate(current_path.resolve(), f"匹配目录线索：{', '.join(matched)}"))
                    if len(candidates) >= self.max_candidates:
                        return candidates
        unique: dict[str, WorkspaceCandidate] = {str(item.path).lower(): item for item in candidates}
        return list(unique.values())

    @staticmethod
    def _terms(hint: str) -> list[str]:
        text = re.sub(r"[：:，,。；;、/\\]+", " ", hint.lower())
        stop_words = {"这个", "那个", "项目", "目录", "工作区", "skill", "修改", "更新", "继续", "刚才", "上次"}
        terms = re.findall(r"[^\W_][\w.-]*", text, flags=re.UNICODE)
        result: list[str] = []
        for item in terms:
            item = item.strip("._-")
            if len(item) < 2 or item in stop_words or "*" in item or "?" in item:
                continue
            result.append(item)
            compact = item.replace("_", "").replace("-", "")
            if compact != item and len(compact) >= 2:
                result.append(compact)
        return result

    @staticmethod
    def _looks_like_project(files: list[str], directories: list[str]) -> bool:
        names = set(files) | set(directories)
        return bool(names & _PROJECT_MARKERS) or bool(files)