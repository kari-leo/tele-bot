from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from tele_bot.llm import OpenCLIGateway
from tele_bot.router.models import RouterDecision, WorkflowName
from tele_bot.router.prompts import prompt_for_workflow
from tele_bot.tools import FileSystemTool, KnowledgeTool, OpenCLISearchTool, ShellSandboxTool


class WorkflowState(TypedDict, total=False):
    prompt: str
    decision: RouterDecision
    conversation_turns: list[Any]
    tool_observations: list[dict[str, Any]]
    restore_source: str
    filesystem_action: str
    shell_command: str
    tool_result_summary: str | None
    reply_text: str
    report_path: str | None
    raw_output: str | None
    used_tool: bool
    trace: list[str]


@dataclass(frozen=True)
class WorkflowExecutionResult:
    reply_text: str
    report_path: str | None
    tool_result_summary: str | None
    used_tool: bool
    raw_output: str | None = None
    trace: tuple[str, ...] = ()


class WorkflowRunner:
    RESTORE_COMMAND = "/restore-cheap"

    def __init__(
        self,
        *,
        llm_client,
        search_tool: OpenCLISearchTool | None = None,
        knowledge_tool: KnowledgeTool | None = None,
        filesystem_tool: FileSystemTool | None = None,
        shell_tool: ShellSandboxTool | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.search_tool = search_tool or OpenCLISearchTool()
        self.knowledge_tool = knowledge_tool or KnowledgeTool(gateway=OpenCLIGateway())
        self.filesystem_tool = filesystem_tool or FileSystemTool()
        self.shell_tool = shell_tool or ShellSandboxTool()
        self._graphs = {
            WorkflowName.CHAT_REPLY.value: self._build_direct_reply_graph(),
            WorkflowName.MARKDOWN_REPLY.value: self._build_direct_reply_graph(),
            WorkflowName.REASONING_REPLY.value: self._build_direct_reply_graph(),
            WorkflowName.SEARCH_REPORT.value: self._build_search_report_graph(),
            WorkflowName.RESTORE_CHEAP.value: self._build_restore_cheap_graph(),
            WorkflowName.FILESYSTEM_INSPECT.value: self._build_filesystem_graph(),
            WorkflowName.SHELL_INSPECT.value: self._build_shell_graph(),
        }

    def invoke(
        self,
        *,
        prompt: str,
        decision: RouterDecision,
        conversation_turns: list[Any] | None,
    ) -> WorkflowExecutionResult:
        graph = self._graphs.get(decision.workflow_name)
        if graph is None:
            graph = self._graphs[WorkflowName.CHAT_REPLY.value]

        final_state = graph.invoke(
            {
                "prompt": prompt,
                "decision": decision,
                "conversation_turns": conversation_turns or [],
                "trace": [],
            }
        )
        return WorkflowExecutionResult(
            reply_text=str(final_state.get("reply_text", "")),
            report_path=final_state.get("report_path"),
            tool_result_summary=final_state.get("tool_result_summary"),
            used_tool=bool(final_state.get("used_tool", False)),
            raw_output=final_state.get("raw_output"),
            trace=tuple(final_state.get("trace", [])),
        )

    def _build_direct_reply_graph(self):
        graph = StateGraph(WorkflowState)
        graph.add_node("generate_reply", self._generate_direct_reply)
        graph.add_edge(START, "generate_reply")
        graph.add_edge("generate_reply", END)
        return graph.compile()

    def _build_search_report_graph(self):
        graph = StateGraph(WorkflowState)
        graph.add_node("search_web", self._search_web)
        graph.add_node("summarize_report", self._summarize_report)
        graph.add_edge(START, "search_web")
        graph.add_edge("search_web", "summarize_report")
        graph.add_edge("summarize_report", END)
        return graph.compile()

    def _build_restore_cheap_graph(self):
        graph = StateGraph(WorkflowState)
        graph.add_node("parse_restore_source", self._parse_restore_source)
        graph.add_node("restore_knowledge", self._restore_knowledge)
        graph.add_edge(START, "parse_restore_source")
        graph.add_edge("parse_restore_source", "restore_knowledge")
        graph.add_edge("restore_knowledge", END)
        return graph.compile()

    def _build_filesystem_graph(self):
        graph = StateGraph(WorkflowState)
        graph.add_node("inspect_filesystem", self._inspect_filesystem)
        graph.add_node("summarize_filesystem", self._summarize_filesystem)
        graph.add_edge(START, "inspect_filesystem")
        graph.add_edge("inspect_filesystem", "summarize_filesystem")
        graph.add_edge("summarize_filesystem", END)
        return graph.compile()

    def _build_shell_graph(self):
        graph = StateGraph(WorkflowState)
        graph.add_node("inspect_shell", self._inspect_shell)
        graph.add_node("summarize_shell", self._summarize_shell)
        graph.add_edge(START, "inspect_shell")
        graph.add_edge("inspect_shell", "summarize_shell")
        graph.add_edge("summarize_shell", END)
        return graph.compile()

    def _generate_direct_reply(self, state: WorkflowState) -> WorkflowState:
        decision = state["decision"]
        response = self.llm_client.generate_response(
            prompt=state["prompt"],
            system_prompt=prompt_for_workflow(decision.workflow_name, fallback_mode=decision.mode.value),
            model=decision.model,
            allow_tools=decision.allow_tools,
            search_required=decision.search_required,
            allow_markdown=decision.allow_markdown,
            save_markdown=decision.save_markdown,
            conversation_turns=state.get("conversation_turns"),
        )
        return {
            "reply_text": getattr(response, "reply_text", ""),
            "report_path": getattr(response, "report_path", None),
            "tool_result_summary": getattr(response, "tool_result_summary", None),
            "used_tool": bool(getattr(response, "used_tool", False)),
            "raw_output": getattr(response, "raw_output", None),
            "trace": [*state.get("trace", []), "generate_reply"],
        }

    def _search_web(self, state: WorkflowState) -> WorkflowState:
        search_result = self.search_tool.execute(state["prompt"])
        return {
            "tool_observations": [search_result],
            "tool_result_summary": self._summarize_tool_result(search_result),
            "used_tool": True,
            "trace": [*state.get("trace", []), "search_web"],
        }

    def _summarize_report(self, state: WorkflowState) -> WorkflowState:
        decision = state["decision"]
        observations = state.get("tool_observations", [])
        search_context = json.dumps(observations, ensure_ascii=False, indent=2)
        prompt = (
            f"用户请求：{state['prompt']}\n\n"
            "下面是已经完成的联网搜索结果，请基于这些结果写出完整 Markdown 报告。"
            "不要再次搜索，也不要引用未给出的来源。\n\n"
            f"搜索结果：\n```json\n{search_context}\n```"
        )
        response = self.llm_client.generate_response(
            prompt=prompt,
            system_prompt=prompt_for_workflow(WorkflowName.SEARCH_REPORT.value, fallback_mode=decision.mode.value),
            model=decision.model,
            allow_tools=False,
            search_required=False,
            allow_markdown=True,
            save_markdown=True,
            conversation_turns=state.get("conversation_turns"),
        )
        return {
            "reply_text": getattr(response, "reply_text", ""),
            "report_path": getattr(response, "report_path", None),
            "tool_result_summary": state.get("tool_result_summary"),
            "used_tool": True,
            "raw_output": getattr(response, "raw_output", None),
            "trace": [*state.get("trace", []), "summarize_report"],
        }

    def _parse_restore_source(self, state: WorkflowState) -> WorkflowState:
        prompt = state["prompt"].strip()
        restore_source = self._strip_restore_command(prompt)
        if not restore_source:
            raise ValueError("restore-cheap source is required")
        return {
            "restore_source": restore_source,
            "trace": [*state.get("trace", []), "parse_restore_source"],
        }

    def _restore_knowledge(self, state: WorkflowState) -> WorkflowState:
        output_path = self.knowledge_tool.restore_knowledge(state["restore_source"])
        return {
            "reply_text": f"Successfully restored knowledge document.\n\nOutput:\n{output_path}",
            "report_path": output_path,
            "tool_result_summary": None,
            "used_tool": False,
            "trace": [*state.get("trace", []), "restore_knowledge"],
        }

    def _inspect_filesystem(self, state: WorkflowState) -> WorkflowState:
        prompt = state["prompt"]
        path = self._extract_path(prompt)
        if "列出" in prompt or "目录" in prompt:
            observation = self.filesystem_tool.list_dir(path, depth=2 if "两层" in prompt else 1)
            summary = f"已读取目录 {observation['path']}，共返回 {len(observation['entries'])} 个条目"
        elif "读取" in prompt or "查看文件" in prompt or "打开文件" in prompt:
            observation = self.filesystem_tool.read_file(path)
            summary = f"已读取文件 {observation['path']}，共 {observation['line_count']} 行"
        else:
            observation = self.filesystem_tool.search_file(self._extract_keyword(prompt), path)
            summary = f"在 {observation['path']} 中搜索到 {len(observation['matches'])} 条匹配"

        return {
            "filesystem_action": summary,
            "tool_observations": [observation],
            "tool_result_summary": summary,
            "trace": [*state.get("trace", []), "inspect_filesystem"],
        }

    def _summarize_filesystem(self, state: WorkflowState) -> WorkflowState:
        decision = state["decision"]
        observation_context = json.dumps(state.get("tool_observations", []), ensure_ascii=False, indent=2)
        prompt = (
            f"用户请求：{state['prompt']}\n\n"
            f"工具结果：\n```json\n{observation_context}\n```\n\n"
            "请基于这些文件系统结果，用简洁中文直接回答用户。"
        )
        response = self.llm_client.generate_response(
            prompt=prompt,
            system_prompt=prompt_for_workflow(WorkflowName.FILESYSTEM_INSPECT.value, fallback_mode=decision.mode.value),
            model=decision.model,
            allow_tools=False,
            search_required=False,
            allow_markdown=False,
            save_markdown=False,
            conversation_turns=state.get("conversation_turns"),
        )
        return {
            "reply_text": getattr(response, "reply_text", ""),
            "report_path": None,
            "tool_result_summary": state.get("tool_result_summary"),
            "used_tool": False,
            "raw_output": getattr(response, "raw_output", None),
            "trace": [*state.get("trace", []), "summarize_filesystem"],
        }

    def _inspect_shell(self, state: WorkflowState) -> WorkflowState:
        command = self._extract_shell_command(state["prompt"])
        observation = self.shell_tool.execute_shell(command)
        summary = f"已执行命令 {command}，返回码 {observation['returncode']}"
        return {
            "shell_command": command,
            "tool_observations": [observation],
            "tool_result_summary": summary,
            "trace": [*state.get("trace", []), "inspect_shell"],
        }

    def _summarize_shell(self, state: WorkflowState) -> WorkflowState:
        decision = state["decision"]
        observation_context = json.dumps(state.get("tool_observations", []), ensure_ascii=False, indent=2)
        prompt = (
            f"用户请求：{state['prompt']}\n\n"
            f"工具结果：\n```json\n{observation_context}\n```\n\n"
            "请基于这些 shell 结果，用简洁中文直接回答用户。"
        )
        response = self.llm_client.generate_response(
            prompt=prompt,
            system_prompt=prompt_for_workflow(WorkflowName.SHELL_INSPECT.value, fallback_mode=decision.mode.value),
            model=decision.model,
            allow_tools=False,
            search_required=False,
            allow_markdown=False,
            save_markdown=False,
            conversation_turns=state.get("conversation_turns"),
        )
        return {
            "reply_text": getattr(response, "reply_text", ""),
            "report_path": None,
            "tool_result_summary": state.get("tool_result_summary"),
            "used_tool": False,
            "raw_output": getattr(response, "raw_output", None),
            "trace": [*state.get("trace", []), "summarize_shell"],
        }

    @staticmethod
    def _summarize_tool_result(tool_result: dict[str, Any]) -> str:
        results = tool_result.get("results", []) if isinstance(tool_result, dict) else []
        if not results:
            query = tool_result.get("query", "") if isinstance(tool_result, dict) else ""
            return f"搜索 `{query}` 未返回结果".strip()

        first = results[0]
        title = first.get("title") or "未命名结果"
        url = first.get("url") or ""
        query = tool_result.get("query", "") if isinstance(tool_result, dict) else ""
        return f"搜索 `{query}` 首条结果：{title} {url}".strip()

    @staticmethod
    def _extract_path(prompt: str) -> str:
        match = re.search(r"(~/(?:[A-Za-z0-9._\-/]+)?|\./(?:[A-Za-z0-9._\-/]+)?|/(?:[A-Za-z0-9._\-/]+)*)", prompt)
        if match:
            return match.group(1).rstrip("，。,.；;")
        raise ValueError("filesystem request missing path")

    @staticmethod
    def _extract_keyword(prompt: str) -> str:
        quoted = [segment for segment in prompt.split('"') if segment.strip()]
        if len(quoted) >= 2:
            return quoted[1]
        return prompt.strip()

    @staticmethod
    def _extract_shell_command(prompt: str) -> str:
        normalized = prompt.strip()
        for prefix in ("执行 ", "运行 "):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :]
                break
        for suffix in (" 看一下目录", " 看一下", " 吧", "。", "，"):
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)]
        return normalized.strip()

    @classmethod
    def _strip_restore_command(cls, prompt: str) -> str:
        stripped = prompt.replace(cls.RESTORE_COMMAND, " ")
        stripped = re.sub(r"[ \t]+\n", "\n", stripped)
        stripped = re.sub(r" {2,}", " ", stripped)
        stripped = re.sub(r"\n{3,}", "\n\n", stripped)
        return stripped.strip()