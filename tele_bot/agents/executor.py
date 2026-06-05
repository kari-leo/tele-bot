from __future__ import annotations

from dataclasses import dataclass

from tele_bot.models import IncomingMessage
from tele_bot.router import InMemoryConversationStateStore, Router, RouterConfig
from tele_bot.router.prompts import prompt_for_mode


@dataclass(frozen=True)
class AgentExecutionResult:
    reply_text: str
    mode: str
    model: str
    report_path: str | None = None
    tool_result_summary: str | None = None
    used_tool: bool = False


class ControlledAgentExecutor:
    def __init__(
        self,
        *,
        llm_client,
        router: Router | None = None,
        state_store: InMemoryConversationStateStore | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.router = router or Router(config=RouterConfig())
        self.state_store = state_store or InMemoryConversationStateStore()

    def handle(self, message: IncomingMessage) -> AgentExecutionResult:
        state = self.state_store.load(message.chat_id)
        decision = self.router.route(message.text, state)
        self.state_store.set_mode(message.chat_id, decision.mode)
        self.state_store.append_turn(message.chat_id, role="user", content=message.text)
        prompt_context = self.state_store.build_prompt_context(message.chat_id, decision.mode)

        try:
            llm_response = self.llm_client.generate_response(
                prompt=message.text,
                system_prompt=prompt_for_mode(decision.mode.value),
                model=decision.model,
                allow_tools=decision.allow_tools,
                search_required=decision.search_required,
                allow_markdown=decision.allow_markdown,
                save_markdown=decision.save_markdown,
                conversation_turns=prompt_context,
            )
            reply_text = llm_response.reply_text
            if llm_response.tool_result_summary:
                self.state_store.set_tool_summary(message.chat_id, llm_response.tool_result_summary)
            if llm_response.report_path:
                self.state_store.add_report_path(message.chat_id, llm_response.report_path)
        except Exception as exc:
            reply_text = f"处理失败：{exc}"
            llm_response = AgentExecutionResult(
                reply_text=reply_text,
                mode=decision.mode.value,
                model=decision.model,
            )

        self.state_store.append_turn(message.chat_id, role="assistant", content=reply_text)
        return AgentExecutionResult(
            reply_text=reply_text,
            mode=decision.mode.value,
            model=decision.model,
            report_path=llm_response.report_path,
            tool_result_summary=llm_response.tool_result_summary,
            used_tool=llm_response.used_tool,
        )