"""Isolated scheduled ReAct entry point with a small, enforced tool set."""

from __future__ import annotations

import json
import re
import sys
from difflib import get_close_matches
from pathlib import Path

from langchain_core.tools import tool

from tele_bot.config.llm.settings import AliBailianSettings
from tele_bot.llm.react_client import build_chat_openai, build_context_manager
from tele_bot.tools.file_system import FileSystemTool
from tele_bot.tools.bailian_web import build_web_tools, normalize_web_tool_names
from tele_bot.tools.write_report import WriteReportTool
from tele_bot.tools.codex_runner import CodexRunner, codex_failure_message
from tele_bot.tools.codex_history import CodexHistoryClient, CodexHistoryError, THREAD_ID, requests_codex_resume
from tele_bot.scheduled.codex_artifacts import (
    DELIVERABLE_SUFFIXES, OUTPUT_SCHEMA, artifact_snapshot, prepare_skill_runtime, scoped_path, validate_artifacts,
)
from tele_bot.workflows.react_graph import MAX_ITERATIONS, build_react_graph
from langchain_core.messages import HumanMessage, ToolMessage


def _requested_skill(content: str, workspace: Path) -> tuple[str, str] | None:
    """Resolve an explicitly named skill within the approved workspace."""
    names = re.findall(
        r"(?<![A-Za-z0-9._-])\$?([A-Za-z][A-Za-z0-9._-]*)\s+(?:skill|技能)(?![A-Za-z0-9_-])",
        content,
        flags=re.IGNORECASE,
    )
    if not names:
        if re.search(r"\bskill\b|技能", content, flags=re.IGNORECASE):
            raise ValueError("任务提到了 skill，但未识别到指定名称。")
        return None
    if len({name.casefold() for name in names}) != 1:
        raise ValueError("任务中出现多个 skill 名称，无法唯一确定。")
    requested = names[0]
    skill_root = workspace / ".codex" / "skills"
    available = []
    if skill_root.is_dir() and skill_root.resolve().is_relative_to(workspace):
        available = sorted(
            entry.name for entry in skill_root.iterdir()
            if entry.is_dir() and not entry.is_symlink()
            and (entry / "SKILL.md").is_file()
            and (entry / "SKILL.md").resolve().is_relative_to(workspace)
        )
    exact = [name for name in available if name.casefold() == requested.casefold()]
    by_casefold = {name.casefold(): name for name in available}
    matches = exact or [
        by_casefold[name] for name in get_close_matches(requested.casefold(), by_casefold, n=2, cutoff=0.75)
    ]
    if len(matches) != 1:
        raise ValueError(f"无法将 skill 名称 {requested} 唯一匹配到当前工作区；可用 skill：{', '.join(available) or '无'}。")
    name = matches[0]
    return name, f".codex/skills/{name}/SKILL.md"


def execute(payload: dict) -> dict:
    scope = payload["scope"]
    allowed = set(normalize_web_tool_names(scope["tools"]))
    roots = tuple(Path(path).expanduser().resolve() for path in scope["directories"])
    for root in roots:
        if not root.is_dir():
            raise ValueError(f"authorized directory no longer exists: {root}")
    if "codex_apply" in allowed:
        if allowed != {"codex_apply"} or len(roots) != 1:
            raise ValueError("Codex run requires one tool and one workspace")
        thread_id = scope.get("codex_thread_id")
        if thread_id is not None and (not isinstance(thread_id, str) or not THREAD_ID.fullmatch(thread_id)):
            return {"status": "failed", "result": "Codex 会话 ID 无效，未执行。", "paths": []}
        if not thread_id and requests_codex_resume(payload["content"]):
            return {"status": "failed", "result": "继续已有 Codex 对话缺少已授权会话 ID，未新建会话。", "paths": []}
        try:
            skill = _requested_skill(payload["content"], roots[0])
        except ValueError as exc:
            return {"status": "failed", "result": f"执行结论：失败\n{exc}", "paths": []}
        before = artifact_snapshot(roots[0])
        skill_instruction = ""
        if skill:
            runtime = prepare_skill_runtime(roots[0], skill[0])
            skill_instruction = (
                f"指定 skill：{skill[0]}\n原始只读技能：{skill[1]}。\n"
                f"请读取并执行运行副本 {runtime / 'SKILL.md'}，SKILL_DIR={runtime}。"
                "配置、脚本、画像、去重状态都使用此运行副本；状态必须持久保存，"
                "不得改写 .codex 目录或用临时去重副本代替持久状态。"
                "用户原文中的 skill 拼写以此处核实的名称为准。\n"
            )
        run_dir = scoped_path(roots[0], roots[0] / "telebot_runtime" / "runs")
        run_dir.mkdir(parents=True, exist_ok=True)
        schema = scoped_path(roots[0], run_dir / "result-schema.json")
        schema.write_text(json.dumps(OUTPUT_SCHEMA), encoding="utf-8")
        prompt = (
            "现在执行以下已授权的定时任务。"
            f"{skill_instruction}"
            "用户原始任务优先：技能的默认岗位方向和默认输出格式不得覆盖本次任务。"
            "若配置与本次岗位方向冲突，先在运行副本中调整配置和正负关键词再搜索。"
            "完成任务后核对变更；要求表格时必须生成本轮的 Excel 或 CSV 文件，"
            "在文件中分别列出新增和修改项，无变更也生成注明无变更的表格。"
            "输出文件保存到工作区 tracking_outputs；不要仅返回文字日报或旧表格。"
            f"可发送附件类型：{', '.join(sorted(DELIVERABLE_SUFFIXES))}；单个文件不超过 30 MiB，最多 20 个。"
            "paths 只填写需要发送的本轮产物，不填写源码、配置、凭据或运行时内部文件。"
            "最终回复必须符合 JSON schema：status 为 success 或 failed，result 是给用户的总结"
            "（包含实际完成和未完成事项），paths 是本轮生成或修改、需要发送的文件完整路径数组。"
            "只有全部完成才能写 success；部分失败也列出已生成文件，不要自行发消息。\n\n"
            f"任务：{payload['content']}\n计划时间：{payload['planned_at']}"
        )
        runner = CodexRunner.from_env()
        if thread_id:
            try:
                summary = CodexHistoryClient(runner).resume(
                    thread_id, prompt, workspace=str(roots[0]), output_schema=OUTPUT_SCHEMA,
                ).strip()
            except CodexHistoryError as exc:
                return {"status": "failed", "result": str(exc), "paths": []}
        else:
            result = runner.run("apply", prompt, str(roots[0]), output_schema=schema)
            if result.returncode:
                return {"status": "failed", "result": codex_failure_message(result), "paths": []}
            summary = result.stdout.strip()
        try:
            receipt = json.loads(summary)
        except ValueError:
            receipt = {"status": "success" if summary.startswith("执行结论：成功") else "failed",
                       "result": summary or "Codex 未返回总结。", "paths": []}
        if not isinstance(receipt, dict) or receipt.get("status") not in {"success", "failed"} or not isinstance(receipt.get("result"), str):
            return {"status": "failed", "result": "Codex 返回结果收据无效，无法确认执行结果；请查看该会话的最新记录。", "paths": []}
        status, summary = receipt["status"], receipt["result"]
        try:
            paths = validate_artifacts(receipt.get("paths", []), roots[0], before)
        except ValueError as exc:
            return {"status": "failed", "result": (
                "Codex 已返回执行结果，但附件校验失败，本次交付未完成。\n"
                f"Codex 报告状态：{status}\n执行摘要：{summary}\n附件错误：{exc}"
            ), "paths": []}
        if "表格" in payload["content"] and not any(Path(path).suffix.lower() in {".xlsx", ".xls", ".csv", ".tsv"} for path in paths):
            status = "failed"
            summary += "\n未完成：未生成并确认本轮表格附件。"
        return {"status": status, "result": summary, "paths": paths}
    tools = []
    paths: list[str] = []
    if allowed & {"web_search", "web_extractor"}:
        tools.extend(item for item in build_web_tools() if item.name in allowed)
    if allowed & {"filesystem_list_dir", "filesystem_read_file"}:
        fs = FileSystemTool(root=roots[0], allowed_roots=roots)
        if "filesystem_list_dir" in allowed:
            @tool
            def filesystem_list_dir(path: str, depth: int = 1) -> str:
                """List files under an authorized directory."""
                return json.dumps(fs.list_dir(path, depth), ensure_ascii=False)

            tools.append(filesystem_list_dir)
        if "filesystem_read_file" in allowed:
            @tool
            def filesystem_read_file(path: str, max_lines: int = 200, start_line: int = 1) -> str:
                """Read a UTF-8 file page under an authorized directory.

                If truncated is true, continue from next_start_line.
                """
                return json.dumps(fs.read_file(path, max_lines, start_line), ensure_ascii=False)

            tools.append(filesystem_read_file)
    if "write_report" in allowed:
        writer = WriteReportTool(reports_dir=roots[0])

        @tool
        def write_report(content: str, title: str = "", filename: str = "") -> str:
            """Save a new Markdown document in the authorized output directory."""
            path = writer.write(content, title=title or None, filename=filename or None)
            paths.append(path)
            return path

        tools.append(write_report)
    settings = AliBailianSettings.from_env()
    if not settings.api_key:
        raise RuntimeError("ALIBAILIAN_API_KEY is missing")
    llm = build_chat_openai(api_key=settings.api_key, base_url=settings.base_url,
                            model=settings.model, temperature=0, timeout=settings.timeout_seconds)
    graph = build_react_graph(llm.bind_tools(tools) if tools else llm, tools,
                              context_manager=build_context_manager(settings, tools),
                              system_prompt=("你正在执行一个已到期的定时任务。只能使用当前提供的工具。"
                                             "工具报错时不得假称任务完成；清楚汇报实际结果、文件路径和失败原因。"))
    prompt = (
        "【定时任务执行】\n"
        f"任务目标：{payload['content']}\n"
        f"触发原因：计划时间 {payload['planned_at']} 到期；执行记录 {payload['run_id']}。\n"
        f"当前时间要求：现在执行本次任务，不把截止时间理解为开始时间。\n"
        f"必要上下文：任务 ID {payload['task_id']}，用户 ID {payload['owner_id']}。\n"
        f"允许工具：{', '.join(sorted(allowed)) or '无'}。\n"
        f"允许目录：{', '.join(map(str, roots)) or '无'}。\n"
        "操作权限：仅可通过上述工具访问上述目录；不允许调用其他工具、修改已有文件、"
        "运行命令、发布内容或扩大授权。\n"
        f"执行限制：最多 {MAX_ITERATIONS} 轮；遇到工具权限或功能不足请说明未完成的部分。\n"
        "预期输出：第一行必须是‘执行结论：成功’或‘执行结论：失败’；随后说明实际完成事项、"
        "生成文件的完整路径及必要错误信息。只有任务目标确实完成才可写成功。"
    )
    result = graph.invoke({"messages": [HumanMessage(content=prompt)], "iterations": 0},
                          config={"configurable": {"thread_id": "schedule-" + payload["run_id"],
                                                   "chat_id": payload["chat_id"], "user_id": payload["owner_id"]}})
    messages = result.get("messages", [])
    final = next((str(getattr(message, "content", "")) for message in reversed(messages)
                  if getattr(message, "type", "") == "ai" and not getattr(message, "tool_calls", None)), "")
    if paths:
        final += "\n生成文件：" + "、".join(paths)
    tool_error = any(isinstance(message, ToolMessage) and getattr(message, "status", "success") == "error"
                     for message in messages)
    used_tools = {message.name for message in messages if isinstance(message, ToolMessage)}
    missing_output = "write_report" in allowed and any(word in payload["content"] for word in ("保存", "文档", "报告")) and not paths
    status = "success" if (final.lstrip().startswith("执行结论：成功") and not tool_error
                           and (not allowed or used_tools) and not missing_output) else "failed"
    return {"status": status, "result": final or "模型未返回结果。", "paths": paths}


if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
        print(json.dumps({"ok": True, **execute(request)}, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        sys.exit(1)
