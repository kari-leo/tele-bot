from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class OpenCLIGateway:
    timeout_seconds: int = 180

    def chatgpt_ask(self, prompt: str, *, new: bool = True) -> str:
        normalized_prompt = prompt.strip()
        if not normalized_prompt:
            raise ValueError("prompt is required")

        command = ["opencli", "chatgpt", "ask"]
        if new:
            command.append("--new")
        command.extend(["-f", "plain", normalized_prompt])

        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            details = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            raise RuntimeError(f"restoration failed due to LLM gateway error: {details}")

        response = completed.stdout.strip()
        if not response:
            raise RuntimeError("restoration failed due to LLM gateway error: empty response")
        return response