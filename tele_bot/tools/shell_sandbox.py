from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from tele_bot.config.paths import default_allowed_roots, is_windows_runtime, runtime_root


@dataclass(frozen=True)
class ShellSandboxTool:
    timeout_seconds: int = 10
    max_output_chars: int = 4000
    root: Path = field(default_factory=runtime_root)
    allowed_commands: tuple[str, ...] = (
        "ls",
        "dir",
        "cat",
        "type",
        "head",
        "tail",
        "grep",
        "find",
        "wc",
        "du",
        "df",
    )
    forbidden_tokens: tuple[str, ...] = ("rm", "chmod", "chown", "sudo", "curl", "wget", "apt", "pip", ";", "&&", "||", "|", ">", "<")
    allowed_roots: tuple[Path, ...] = field(default_factory=default_allowed_roots)

    def execute_shell(self, command: str) -> dict:
        normalized = command.strip()
        if not normalized:
            raise ValueError("command is required")

        for token in self.forbidden_tokens:
            if token in normalized:
                raise ValueError(f"command contains forbidden token: {token}")

        argv = shlex.split(normalized, posix=not is_windows_runtime())
        if not argv:
            raise ValueError("command is required")

        executable = argv[0].lower()
        if executable not in self.allowed_commands:
            raise ValueError(f"command is not allowed: {executable}")

        cwd, final_argv = self._extract_cwd_and_args(argv)
        if is_windows_runtime():
            final_argv = self._windows_argv(final_argv, cwd)
        completed = subprocess.run(
            final_argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            check=False,
            cwd=str(cwd),
        )
        stdout, stdout_truncated = self._truncate(completed.stdout)
        stderr, stderr_truncated = self._truncate(completed.stderr)
        return {
            "command": normalized,
            "returncode": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": stdout_truncated or stderr_truncated,
        }

    def _extract_cwd_and_args(self, argv: list[str]) -> tuple[Path, list[str]]:
        default_cwd = self._resolve_allowed_path(str(self.root))
        if len(argv) < 2:
            return default_cwd, argv

        last = argv[-1]
        if last.startswith("-"):
            return default_cwd, argv

        path = self._resolve_allowed_path(last)
        if not path.exists() or not path.is_dir():
            return default_cwd, argv
        return path, [argv[0], *argv[1:-1]] if len(argv) > 2 else [argv[0]]

    def _resolve_allowed_path(self, raw_path: str, cwd: Path | None = None) -> Path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = (cwd or self.root) / candidate
        candidate = candidate.resolve()
        for root in self.allowed_roots:
            resolved_root = root.resolve()
            if candidate == resolved_root or resolved_root in candidate.parents:
                return candidate
        raise ValueError(f"path is outside allowed roots: {candidate}")

    def _windows_argv(self, argv: list[str], cwd: Path) -> list[str]:
        command = argv[0].lower()
        args = argv[1:]

        if command in ("ls", "dir"):
            return self._powershell("Get-ChildItem -Force -LiteralPath $args[0]", cwd)

        if command in ("cat", "type"):
            path = self._last_path_arg(args, cwd)
            return self._powershell("Get-Content -LiteralPath $args[0]", path)

        if command == "head":
            count = self._line_count(args, default=10)
            path = self._last_path_arg(args, cwd)
            return self._powershell(
                "Get-Content -LiteralPath $args[0] -TotalCount ([int]$args[1])",
                path,
                str(count),
            )

        if command == "tail":
            count = self._line_count(args, default=10)
            path = self._last_path_arg(args, cwd)
            return self._powershell(
                "Get-Content -LiteralPath $args[0] -Tail ([int]$args[1])",
                path,
                str(count),
            )

        if command == "find":
            return self._powershell("Get-ChildItem -Force -Recurse -LiteralPath $args[0]", cwd)

        if command == "grep":
            if not args:
                raise ValueError("grep requires a pattern")
            pattern = args[0]
            path = self._resolve_allowed_path(args[-1], cwd) if len(args) > 1 else cwd
            if path.is_dir():
                return self._powershell(
                    "Get-ChildItem -Recurse -File -LiteralPath $args[1] | "
                    "Select-String -Pattern $args[0]",
                    pattern,
                    path,
                )
            return self._powershell("Select-String -Pattern $args[0] -LiteralPath $args[1]", pattern, path)

        raise ValueError(f"command is not supported on Windows: {command}")

    @staticmethod
    def _powershell(script: str, *args: object) -> list[str]:
        for index, arg in enumerate(args):
            script = script.replace(f"$args[{index}]", ShellSandboxTool._ps_literal(str(arg)))
        return [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ]

    @staticmethod
    def _ps_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def _last_path_arg(self, args: list[str], cwd: Path) -> Path:
        if not args:
            return cwd
        return self._resolve_allowed_path(args[-1], cwd)

    @staticmethod
    def _line_count(args: list[str], default: int) -> int:
        for index, arg in enumerate(args):
            if arg == "-n" and index + 1 < len(args):
                try:
                    return max(1, int(args[index + 1]))
                except ValueError:
                    return default
        return default

    def _truncate(self, output: str) -> tuple[str, bool]:
        if len(output) <= self.max_output_chars:
            return output, False
        return output[: self.max_output_chars], True
