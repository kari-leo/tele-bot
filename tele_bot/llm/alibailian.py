from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import OpenAI

from tele_bot.tools import OpenCLISearchTool


@dataclass(frozen=True)
class AgentLLMResponse:
    reply_text: str
    model: str
    report_path: str | None = None
    tool_result_summary: str | None = None
    used_tool: bool = False
    raw_output: str | None = None


@dataclass(frozen=True)
class AliBailianChatClient:
    api_key: str | None
    base_url: str
    model: str
    timeout_seconds: int = 60
    fallback_model: str = "qwen-plus"
    report_dir: Path | None = None

    def generate_reply(self, prompt: str) -> str:
        response = self.generate_response(
            prompt=prompt,
            system_prompt="你是一个中文助手。只输出自然语言短回复。",
            model=self.model,
            allow_tools=False,
            search_required=False,
            allow_markdown=False,
            save_markdown=False,
            conversation_turns=None,
        )
        return response.reply_text

    def generate_response(
        self,
        *,
        prompt: str,
        system_prompt: str,
        model: str,
        allow_tools: bool,
        search_required: bool,
        allow_markdown: bool,
        save_markdown: bool,
        conversation_turns: list[Any] | None,
    ) -> AgentLLMResponse:
        if not self.api_key:
            raise ValueError("ALIBAILIAN_API_KEY is required to call Qwen")

        with _without_unsupported_proxy_env():
            client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout_seconds,
            )
            try:
                response = self._generate_response(
                    client=client,
                    model=model,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    allow_tools=allow_tools,
                    search_required=search_required,
                    conversation_turns=conversation_turns,
                )
            except Exception as exc:
                if not self._is_model_not_found(exc):
                    raise RuntimeError(f"AliBailian request failed: {exc}") from exc

                if model == self.fallback_model:
                    raise RuntimeError(f"AliBailian request failed: {exc}") from exc

                try:
                    response = self._generate_response(
                        client=client,
                        model=self.fallback_model,
                        prompt=prompt,
                        system_prompt=system_prompt,
                        allow_tools=allow_tools,
                        search_required=search_required,
                        conversation_turns=conversation_turns,
                    )
                except Exception as exc:
                    raise RuntimeError(f"AliBailian request failed: {exc}") from exc

        final_output = response["output_text"]
        used_tool = response["used_tool"]
        tool_result_summary = response["tool_result_summary"]
        effective_model = response["model"]

        if allow_markdown:
            reply_text = final_output.strip()
        else:
            reply_text = self._strip_markdown(final_output)

        report_path: Path | None = None
        if allow_markdown and save_markdown:
            report_path = self._write_report(prompt=prompt, markdown=final_output)
            summary = self._extract_summary(final_output)
            reply_text = self._build_telegram_reply(summary=summary, report_path=report_path)

        return AgentLLMResponse(
            reply_text=reply_text,
            model=effective_model,
            report_path=str(report_path) if report_path is not None else None,
            tool_result_summary=tool_result_summary,
            used_tool=used_tool,
            raw_output=final_output,
        )

    def _generate_response(
        self,
        *,
        client: OpenAI,
        model: str,
        prompt: str,
        system_prompt: str,
        allow_tools: bool,
        search_required: bool,
        conversation_turns: list[Any] | None,
    ) -> dict[str, Any]:
        search_tool = OpenCLISearchTool(timeout_seconds=self.timeout_seconds)
        tools = [search_tool.schema] if allow_tools else []
        tool_summaries: list[str] = []
        used_tool = False
        response = client.responses.create(
            model=model,
            input=self._build_input_payload(prompt=prompt, conversation_turns=conversation_turns),
            instructions=system_prompt,
            tools=tools,
        )

        for _ in range(4):
            tool_outputs, new_summaries = self._build_tool_outputs(
                response=response,
                search_tool=search_tool,
                allow_tools=allow_tools,
            )
            if not tool_outputs:
                break

            used_tool = True
            tool_summaries.extend(new_summaries)

            response_id = self._get_value(response, "id")
            if not response_id:
                raise RuntimeError("AliBailian response missing id for tool follow-up")

            response = client.responses.create(
                model=model,
                previous_response_id=str(response_id),
                input=tool_outputs,
                instructions=system_prompt,
                tools=tools,
            )

        if search_required and not used_tool:
            raise RuntimeError("当前请求需要联网搜索，但模型未调用 opencli_search")

        output_text = self._extract_output_text(response)
        if not output_text:
            raise RuntimeError("AliBailian response missing output text")

        return {
            "output_text": output_text.strip(),
            "used_tool": used_tool,
            "tool_result_summary": "；".join(tool_summaries) if tool_summaries else None,
            "model": model,
        }

    def _build_tool_outputs(
        self,
        *,
        response: Any,
        search_tool: OpenCLISearchTool,
        allow_tools: bool,
    ) -> tuple[list[dict[str, str]], list[str]]:
        outputs: list[dict[str, str]] = []
        summaries: list[str] = []

        for item in self._get_value(response, "output", []) or []:
            if self._get_value(item, "type") != "function_call":
                continue

            if not allow_tools:
                raise RuntimeError("Model attempted tool call without Router approval")

            name = self._get_value(item, "name")
            if name != search_tool.name:
                raise RuntimeError(f"Unsupported tool call: {name}")

            arguments = self._parse_tool_arguments(self._get_value(item, "arguments", "{}"))
            tool_result = search_tool.execute(str(arguments.get("query", "")))
            summaries.append(self._summarize_tool_result(tool_result))
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": str(self._get_value(item, "call_id")),
                    "output": json.dumps(tool_result, ensure_ascii=False),
                }
            )

        return outputs, summaries

    def _write_report(self, *, prompt: str, markdown: str) -> Path:
        report_dir = self.report_dir or Path(__file__).resolve().parents[2] / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)

        slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")
        if not slug:
            slug = "research-report"
        slug = slug[:40]

        filename = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{slug}.md"
        report_path = report_dir / filename
        report_path.write_text(markdown.strip() + "\n", encoding="utf-8")
        return report_path

    def _extract_summary(self, markdown: str) -> str:
        for raw_line in markdown.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            return self._trim_summary(line)

        collapsed = re.sub(r"[#*_`>-]", " ", markdown)
        collapsed = re.sub(r"\s+", " ", collapsed).strip()
        if not collapsed:
            return "已生成报告。"
        return self._trim_summary(collapsed)

    def _build_telegram_reply(self, *, summary: str, report_path: Path) -> str:
        repo_root = Path(__file__).resolve().parents[2]
        try:
            report_location = report_path.relative_to(repo_root).as_posix()
        except ValueError:
            report_location = str(report_path)

        return f"{summary}\n\n报告: {report_location}"

    @staticmethod
    def _trim_summary(summary: str) -> str:
        collapsed = re.sub(r"\s+", " ", summary).strip()
        if len(collapsed) <= 280:
            return collapsed
        return collapsed[:277] + "..."

    def _build_input_payload(self, *, prompt: str, conversation_turns: list[Any] | None) -> list[dict[str, str]]:
        payload: list[dict[str, str]] = []
        for turn in conversation_turns or []:
            role = self._get_value(turn, "role")
            content = self._get_value(turn, "content")
            if not role or not content:
                continue
            payload.append({"role": str(role), "content": str(content)})

        if not payload or payload[-1].get("role") != "user" or payload[-1].get("content") != prompt:
            payload.append({"role": "user", "content": prompt})

        return payload

    @staticmethod
    def _summarize_tool_result(tool_result: dict[str, Any]) -> str:
        results = tool_result.get("results", []) if isinstance(tool_result, dict) else []
        if not results:
            return f"搜索 `{tool_result.get('query', '')}` 未返回结果" if isinstance(tool_result, dict) else "搜索未返回结果"

        first = results[0]
        title = first.get("title") or "未命名结果"
        url = first.get("url") or ""
        query = tool_result.get("query", "") if isinstance(tool_result, dict) else ""
        return f"搜索 `{query}` 首条结果：{title} {url}".strip()

    @staticmethod
    def _strip_markdown(text: str) -> str:
        plain = re.sub(r"```[\s\S]*?```", " ", text)
        plain = re.sub(r"^#{1,6}\s*", "", plain, flags=re.MULTILINE)
        plain = re.sub(r"^[\-*+]\s+", "", plain, flags=re.MULTILINE)
        plain = re.sub(r"`([^`]+)`", r"\1", plain)
        plain = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", plain)
        plain = re.sub(r"\s+", " ", plain).strip()
        return plain

    @staticmethod
    def _parse_tool_arguments(raw_arguments: Any) -> dict[str, Any]:
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if raw_arguments is None:
            return {}
        try:
            return json.loads(str(raw_arguments))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"AliBailian tool arguments are invalid JSON: {raw_arguments}") from exc

    @staticmethod
    def _extract_output_text(response: Any) -> str:
        output_text = AliBailianChatClient._get_value(response, "output_text")
        if output_text:
            return str(output_text).strip()

        chunks: list[str] = []
        for item in AliBailianChatClient._get_value(response, "output", []) or []:
            if AliBailianChatClient._get_value(item, "type") != "message":
                continue

            for content in AliBailianChatClient._get_value(item, "content", []) or []:
                content_type = AliBailianChatClient._get_value(content, "type")
                if content_type not in {"output_text", "text"}:
                    continue

                text = AliBailianChatClient._get_value(content, "text")
                if text:
                    chunks.append(str(text))

        return "\n".join(chunks).strip()

    @staticmethod
    def _get_value(source: Any, key: str, default: Any = None) -> Any:
        if isinstance(source, dict):
            return source.get(key, default)
        return getattr(source, key, default)

    @staticmethod
    def _is_model_not_found(exc: Exception) -> bool:
        message = str(exc)
        return (
            "model_not_found" in message
            or "model not found" in message
            or "does not exist" in message
        )


@contextmanager
def _without_unsupported_proxy_env():
    proxy_keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    removed: dict[str, str] = {}
    try:
        for key in proxy_keys:
            value = os.environ.get(key)
            if not value or not value.startswith("socks://"):
                continue
            removed[key] = value
            os.environ.pop(key, None)
        yield
    finally:
        os.environ.update(removed)
