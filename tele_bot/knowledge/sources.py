"""Conservative local-source discovery within an administrator-bound directory."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from tele_bot.knowledge.ingestion import SUPPORTED_EXTENSIONS
from tele_bot.tools.workspace_policy import WorkspacePolicy


_SKIP_DIRECTORIES = {".git", ".svn", "node_modules", "__pycache__", ".venv", "venv"}
_ADDRESS_SEPARATORS = re.compile(
    r"目录下的|目录里的|目录中的|文件夹下的|文件夹里的|文件夹中的|"
    r"目录|文件夹|下的|里的|中的|请|将|把|在|所有|全部|文档|文件|导入|加入|知识库",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class LocalImportSelection:
    directory: Path
    files: tuple[Path, ...]


class KnowledgeSourceResolver:
    def __init__(self, policy: WorkspacePolicy, *, max_depth: int = 8, max_files: int = 200) -> None:
        if max_depth <= 0 or max_files <= 0:
            raise ValueError("max_depth and max_files must be greater than zero")
        self.policy = policy
        self.max_depth = max_depth
        self.max_files = max_files

    def resolve(self, hint: str, *, bound_directory: str) -> LocalImportSelection:
        bound = self.policy.resolve_existing(bound_directory)
        if not bound.is_dir():
            raise ValueError("项目空间绑定路径不是目录")

        direct = self._direct_path(hint)
        if direct is not None:
            target = self.policy.resolve_existing(str(direct))
            self._assert_inside(target, bound)
            directory = target if target.is_dir() else target.parent
            files = (target,) if target.is_file() else self._collect(target)
            return self._selection(directory, files)

        terms = self._terms(hint)
        if not terms:
            raise ValueError("无法从描述中提取目录线索，请补充目录名")
        candidates: list[Path] = []
        for current, directories, _files in os.walk(bound):
            current_path = Path(current)
            depth = len(current_path.relative_to(bound).parts)
            directories[:] = sorted(
                item for item in directories
                if item not in _SKIP_DIRECTORIES and not item.startswith(".")
            )
            if depth >= self.max_depth:
                directories[:] = []
            if self._ordered_path_match(current_path, terms):
                candidates.append(current_path.resolve())

        # Keep only the most specific matching directories. Ancestors can also
        # match when a term is repeated in their absolute path.
        if candidates:
            max_score = max(self._match_score(candidate, terms) for candidate in candidates)
            candidates = [
                candidate for candidate in candidates
                if self._match_score(candidate, terms) == max_score
            ]
        unique = list(dict.fromkeys(candidates))
        if not unique:
            raise ValueError(f"在已绑定目录中找不到匹配目录：{' / '.join(terms)}")
        if len(unique) > 1:
            choices = "；".join(str(path) for path in unique[:8])
            raise ValueError(f"找到多个匹配目录，请明确选择：{choices}")
        return self._selection(unique[0], self._collect(unique[0]))

    def _direct_path(self, hint: str) -> Path | None:
        normalized = hint.strip().strip('"\'')
        if not normalized:
            return None
        candidate = Path(normalized).expanduser()
        if candidate.is_absolute():
            return candidate
        workspace_candidate = (self.policy.workspace_root / candidate).resolve(strict=False)
        return workspace_candidate if workspace_candidate.exists() else None

    def _collect(self, directory: Path) -> tuple[Path, ...]:
        files: list[Path] = []
        for current, directories, names in os.walk(directory):
            current_path = Path(current)
            depth = len(current_path.relative_to(directory).parts)
            directories[:] = sorted(
                item for item in directories
                if item not in _SKIP_DIRECTORIES and not item.startswith(".")
            )
            if depth >= self.max_depth:
                directories[:] = []
            for name in sorted(names):
                path = current_path / name
                if name.startswith(".") or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                self.policy.resolve_existing(str(path))
                files.append(path.resolve())
                if len(files) > self.max_files:
                    raise ValueError(f"批量导入超过 {self.max_files} 个文件，请缩小目录范围")
        return tuple(files)

    @staticmethod
    def _selection(directory: Path, files: tuple[Path, ...]) -> LocalImportSelection:
        if not files:
            supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
            raise ValueError(f"目录内没有支持的文档（{supported}）")
        return LocalImportSelection(directory.resolve(), files)

    @staticmethod
    def _terms(hint: str) -> list[str]:
        normalized = hint.replace("\\", " ").replace("/", " ")
        parts = _ADDRESS_SEPARATORS.split(normalized)
        terms: list[str] = []
        for part in parts:
            for token in re.findall(r"[A-Za-z0-9_.-]+|[\u4e00-\u9fff]+", part):
                token = token.strip(" ._-").lower()
                if token and token not in {"从", "到", "里", "内"}:
                    terms.append(token)
        return terms

    @staticmethod
    def _ordered_path_match(path: Path, terms: list[str]) -> bool:
        components = [part.lower() for part in path.parts]
        index = 0
        for term in terms:
            while index < len(components) and term not in components[index]:
                index += 1
            if index >= len(components):
                return False
            index += 1
        return True

    @staticmethod
    def _match_score(path: Path, terms: list[str]) -> tuple[int, int]:
        components = [part.lower() for part in path.parts]
        exact = sum(1 for term in terms if term in components)
        # Prefer the shallowest directory when the same full hierarchy matches
        # both a directory and descendants under it.
        return exact, -len(components)

    @staticmethod
    def _assert_inside(target: Path, bound: Path) -> None:
        try:
            target.resolve().relative_to(bound.resolve())
        except ValueError as exc:
            raise ValueError("导入目标位于项目空间已绑定目录之外") from exc
