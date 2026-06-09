from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ShellSandboxTool:
    timeout_seconds: int = 10
    max_output_chars: int = 4000
    allowed_commands: tuple[str, ...] = ("ls", "cat", "head", "tail", "grep", "find", "wc", "du", "df")
    forbidden_tokens: tuple[str, ...] = ("rm", "chmod", "chown", "sudo", "curl", "wget", "apt", "pip", ";", "&&", "||", "|", ">", "<")
    allowed_roots: tuple[Path, ...] = field(default_factory=lambda: (Path(__file__).resolve().parents[2], Path("/tmp")))

    def execute_shell(self, command: str) -> dict:
        normalized = command.strip()
        if not normalized:
            raise ValueError("command is required")

        for token in self.forbidden_tokens:
            if token in normalized:
                raise ValueError(f"command contains forbidden token: {token}")

        argv = shlex.split(normalized)
        if not argv:
            raise ValueError("command is required")

        executable = argv[0]
        if executable not in self.allowed_commands:
            raise ValueError(f"command is not allowed: {executable}")

        cwd, final_argv = self._extract_cwd_and_args(argv)
        completed = subprocess.run(
            final_argv,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
            cwd=str(cwd) if cwd is not None else None,
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

    def _extract_cwd_and_args(self, argv: list[str]) -> tuple[Path | None, list[str]]:
        if len(argv) < 2:
            return None, argv

        last = argv[-1]
        if not last.startswith(("/", "./", "~/")):
            return None, argv

        path = Path(last).expanduser()
        path = path.resolve()
        if not any(path == root.resolve() or root.resolve() in path.parents for root in self.allowed_roots):
            raise ValueError(f"path is outside allowed roots: {path}")
        if not path.exists() or not path.is_dir():
            return None, argv
        return path, [argv[0], *argv[1:-1]] if len(argv) > 2 else [argv[0]]

    def _truncate(self, output: str) -> tuple[str, bool]:
        if len(output) <= self.max_output_chars:
            return output, False
        return output[: self.max_output_chars], True