"""Writable skill data and scoped, current-run Codex delivery receipts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["status", "result", "paths"],
    "properties": {
        "status": {"type": "string", "enum": ["success", "failed"]},
        "result": {"type": "string"},
        "paths": {"type": "array", "items": {"type": "string"}},
    },
}
DELIVERABLE_SUFFIXES = {".xlsx", ".xls", ".csv", ".tsv", ".pdf", ".docx", ".pptx", ".md", ".txt"}
PROTECTED_DIRS = {".codex", ".agents", ".git", ".aws"}
SKIP_DIRS = PROTECTED_DIRS | {"node_modules", "__pycache__", ".venv", "venv"}


def scoped_path(root: Path, path: Path) -> Path:
    target = path.resolve()
    if not target.is_relative_to(root) or any(part.lower() in PROTECTED_DIRS for part in target.relative_to(root).parts):
        raise ValueError(f"runtime/output path is outside writable task scope: {target}")
    return target


def prepare_skill_runtime(workspace: Path, name: str) -> Path:
    """Keep code refreshed, with persistent data outside protected .codex.

    Original config/state are read only. A changed source config replaces the
    runtime config; otherwise task-specific config and dedupe state survive.
    """
    if Path(name).name != name or name in {".", ".."}:
        raise ValueError("invalid skill name")
    source = workspace / ".codex" / "skills" / name
    if source.is_symlink() or not source.resolve().is_relative_to(workspace):
        raise ValueError("skill source is outside authorized workspace")
    runtime = scoped_path(workspace, workspace / "telebot_runtime" / "skills" / name)
    for directory, dirs, files in os.walk(source):
        parent = Path(directory)
        for entry in [*(parent / item for item in dirs), *(parent / item for item in files)]:
            if entry.is_symlink() or not entry.resolve().is_relative_to(source.resolve()):
                raise ValueError(f"linked skill files are not supported: {entry}")
        dirs[:] = [item for item in dirs if item not in SKIP_DIRS | {"tmp"}]
        for filename in files:
            original = parent / filename
            relative = original.relative_to(source)
            destination = scoped_path(workspace, runtime / relative)
            if relative.parts[0] == "state" and destination.exists():
                continue
            if relative == Path("config.json"):
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, destination)
    config_source = source / "config.json"
    if config_source.is_file():
        fingerprint = hashlib.sha256(config_source.read_bytes()).hexdigest()
        marker = scoped_path(workspace, runtime / "source-config.sha256")
        config_target = scoped_path(workspace, runtime / "config.json")
        if not config_target.is_file() or not marker.is_file() or marker.read_text() != fingerprint:
            config = json.loads(config_source.read_text(encoding="utf-8-sig"))
            for key, value in config.items():
                if key.endswith("_path") and isinstance(value, str) and value:
                    original_path = Path(value).expanduser()
                    if not original_path.is_absolute():
                        original_path = source / original_path
                    resolved = original_path.resolve()
                    if resolved.is_relative_to(source.resolve()):
                        resolved = runtime / resolved.relative_to(source.resolve())
                    config[key] = str(scoped_path(workspace, resolved))
            config_target.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            marker.write_text(fingerprint, encoding="utf-8")
    return runtime


def artifact_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    snapshot = {}
    for directory, dirs, files in os.walk(root):
        parent = Path(directory)
        dirs[:] = [item for item in dirs if item.lower() not in SKIP_DIRS and not (parent / item).is_symlink()]
        for name in files:
            path = parent / name
            if path.suffix.lower() in DELIVERABLE_SUFFIXES and not path.is_symlink():
                resolved = scoped_path(root, path)
                stat = resolved.stat()
                snapshot[str(resolved)] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def validate_artifacts(paths, root: Path, before: dict) -> list[str]:
    if not isinstance(paths, list) or len(paths) > 20 or any(not isinstance(path, str) for path in paths):
        raise ValueError("invalid Codex attachment list")
    validated = []
    for raw in paths:
        path = Path(raw)
        target = scoped_path(root, path if path.is_absolute() else root / path)
        if not target.is_file() or target.suffix.lower() not in DELIVERABLE_SUFFIXES:
            raise ValueError(f"attachment is missing or has an unsupported type: {target}")
        stat = target.stat()
        if stat.st_size > 30 * 1024 * 1024:
            raise ValueError(f"attachment exceeds 30 MB: {target}")
        if before.get(str(target)) == (stat.st_mtime_ns, stat.st_size):
            raise ValueError(f"attachment was not generated or updated by this run: {target}")
        if str(target) not in validated:
            validated.append(str(target))
    return validated
