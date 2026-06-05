from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from tele_bot.router.models import AgentMode, ConversationState, ConversationTurn


class InMemoryConversationStateStore:
    def __init__(self, max_turns: int = 12) -> None:
        self.max_turns = max_turns
        self._states: dict[str, ConversationState] = {}

    def load(self, chat_id: str) -> ConversationState:
        return self._states.get(chat_id, ConversationState(chat_id=chat_id))

    def append_turn(self, chat_id: str, role: str, content: str) -> ConversationState:
        state = self.load(chat_id)
        trimmed_turns = (state.turns + [ConversationTurn(role=role, content=content)])[-self.max_turns :]
        updated = replace(state, turns=trimmed_turns)
        self._states[chat_id] = updated
        return updated

    def set_mode(self, chat_id: str, mode: AgentMode) -> ConversationState:
        state = self.load(chat_id)
        updated = replace(state, mode=mode)
        self._states[chat_id] = updated
        return updated

    def set_tool_summary(self, chat_id: str, summary: str) -> ConversationState:
        state = self.load(chat_id)
        updated = replace(state, last_tool_result_summary=summary)
        self._states[chat_id] = updated
        return updated

    def add_report_path(self, chat_id: str, report_path: str) -> ConversationState:
        state = self.load(chat_id)
        updated = replace(state, report_paths=state.report_paths + [report_path])
        self._states[chat_id] = updated
        return updated

    def build_prompt_context(self, chat_id: str, mode: AgentMode) -> list[ConversationTurn]:
        state = self.load(chat_id)
        turns = state.turns[-self.max_turns :]

        if mode == AgentMode.CHAT:
            return [turn for turn in turns if "# " not in turn.content]

        if mode == AgentMode.MARKDOWN:
            return turns[-8:]

        return turns


class JsonFileConversationStateStore:
    def __init__(self, directory: Path, max_turns: int = 12) -> None:
        self.directory = directory
        self.max_turns = max_turns
        self.directory.mkdir(parents=True, exist_ok=True)

    def load(self, chat_id: str) -> ConversationState:
        path = self._path_for(chat_id)
        if not path.exists():
            return ConversationState(chat_id=chat_id)

        payload = json.loads(path.read_text(encoding="utf-8"))
        turns = [ConversationTurn(role=item["role"], content=item["content"]) for item in payload.get("turns", [])]
        return ConversationState(
            chat_id=payload.get("chat_id", chat_id),
            mode=AgentMode(payload.get("mode", AgentMode.CHAT.value)),
            turns=turns,
            last_tool_result_summary=payload.get("last_tool_result_summary"),
            report_paths=list(payload.get("report_paths", [])),
        )

    def append_turn(self, chat_id: str, role: str, content: str) -> ConversationState:
        state = self.load(chat_id)
        updated = replace(
            state,
            turns=(state.turns + [ConversationTurn(role=role, content=content)])[-self.max_turns :],
        )
        self._save(updated)
        return updated

    def set_mode(self, chat_id: str, mode: AgentMode) -> ConversationState:
        state = self.load(chat_id)
        updated = replace(state, mode=mode)
        self._save(updated)
        return updated

    def set_tool_summary(self, chat_id: str, summary: str) -> ConversationState:
        state = self.load(chat_id)
        updated = replace(state, last_tool_result_summary=summary)
        self._save(updated)
        return updated

    def add_report_path(self, chat_id: str, report_path: str) -> ConversationState:
        state = self.load(chat_id)
        updated = replace(state, report_paths=state.report_paths + [report_path])
        self._save(updated)
        return updated

    def build_prompt_context(self, chat_id: str, mode: AgentMode) -> list[ConversationTurn]:
        state = self.load(chat_id)
        turns = state.turns[-self.max_turns :]

        if mode == AgentMode.CHAT:
            return [turn for turn in turns if "# " not in turn.content]

        if mode == AgentMode.MARKDOWN:
            return turns[-8:]

        return turns

    def _save(self, state: ConversationState) -> None:
        payload = {
            "chat_id": state.chat_id,
            "mode": state.mode.value,
            "turns": [{"role": turn.role, "content": turn.content} for turn in state.turns],
            "last_tool_result_summary": state.last_tool_result_summary,
            "report_paths": state.report_paths,
        }
        self._path_for(state.chat_id).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _path_for(self, chat_id: str) -> Path:
        safe_chat_id = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in chat_id)
        return self.directory / f"{safe_chat_id}.json"