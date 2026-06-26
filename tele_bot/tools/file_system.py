from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class FileSystemTool:
    allowed_roots: tuple[Path, ...] = field(default_factory=lambda: (Path.home(), Path(__file__).resolve().parents[2], Path("/tmp")))
    max_depth: int = 2
    max_lines: int = 500
    max_matches: int = 20

    def list_dir(self, path: str, depth: int = 2) -> dict:
        target = self._resolve_allowed_path(path)
        effective_depth = min(max(depth, 1), self.max_depth)
        entries: list[dict[str, str]] = []

        for child in sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
            entries.append({"path": str(child), "type": "dir" if child.is_dir() else "file"})
            if child.is_dir() and effective_depth > 1:
                for nested in sorted(child.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
                    entries.append({"path": str(nested), "type": "dir" if nested.is_dir() else "file"})

        return {"path": str(target), "depth": effective_depth, "entries": entries}

    def read_file(self, path: str, max_lines: int | None = None) -> dict:
        target = self._resolve_allowed_path(path)
        if not target.is_file():
            raise ValueError(f"not a file: {target}")

        effective_max_lines = min(max_lines or self.max_lines, self.max_lines)
        content = target.read_text(encoding="utf-8").splitlines()
        truncated = len(content) > effective_max_lines
        lines = content[:effective_max_lines]
        return {
            "path": str(target),
            "line_count": len(content),
            "truncated": truncated,
            "lines": [{"line": index + 1, "content": line} for index, line in enumerate(lines)],
        }

    def search_file(self, keyword: str, path: str) -> dict:
        normalized_keyword = keyword.strip()
        if not normalized_keyword:
            raise ValueError("keyword is required")

        target = self._resolve_allowed_path(path)
        matches: list[dict[str, str | int]] = []
        for file_path in self._iter_files(target):
            try:
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue

            for line_number, line in enumerate(lines, start=1):
                if normalized_keyword not in line:
                    continue
                matches.append({"path": str(file_path), "line": line_number, "content": line})
                if len(matches) >= self.max_matches:
                    return {"path": str(target), "keyword": normalized_keyword, "matches": matches}

        return {"path": str(target), "keyword": normalized_keyword, "matches": matches}

    def _resolve_allowed_path(self, raw_path: str) -> Path:
        normalized = raw_path.strip()
        if not normalized:
            raise ValueError("path is required")

        candidate = Path(normalized).expanduser()
        if not candidate.is_absolute():
            candidate = self.allowed_roots[0] / candidate
        candidate = candidate.resolve()

        for root in self.allowed_roots:
            resolved_root = root.resolve()
            if candidate == resolved_root or resolved_root in candidate.parents:
                if not candidate.exists():
                    raise ValueError(f"path does not exist: {candidate}")
                return candidate

        raise ValueError(f"path is outside allowed roots: {candidate}")

    def _iter_files(self, target: Path):
        if target.is_file():
            yield target
            return

        for path in sorted(target.rglob("*")):
            if not path.is_file():
                continue
            relative_depth = len(path.relative_to(target).parts)
            if relative_depth > self.max_depth:
                continue
            yield path