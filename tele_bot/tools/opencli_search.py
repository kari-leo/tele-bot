from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OpenCLISearchTool:
    timeout_seconds: int = 60
    limit: int = 5
    name: str = "opencli_search"

    @property
    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": "Search internet using OpenCLI",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                    }
                },
                "required": ["query"],
            },
        }

    def execute(self, query: str) -> dict[str, Any]:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("opencli_search query is required")

        completed = subprocess.run(
            [
                "opencli",
                "duckduckgo",
                "search",
                normalized_query,
                "--limit",
                str(self.limit),
                "-f",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            details = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            raise RuntimeError(f"opencli_search failed: {details}")

        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("opencli_search returned invalid JSON") from exc

        results: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue

            results.append(
                {
                    "rank": item.get("rank"),
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "snippet": item.get("snippet"),
                }
            )
            if len(results) >= self.limit:
                break

        return {
            "query": normalized_query,
            "engine": "duckduckgo",
            "result_count": len(results),
            "results": results,
        }