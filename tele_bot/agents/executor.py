from __future__ import annotations

from dataclasses import dataclass

from tele_bot.models import IncomingMessage
from tele_bot.router import InMemoryConversationStateStore, Router, RouterConfig
from tele_bot.workflows import WorkflowRunner


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
        workflow_runner: WorkflowRunner | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.router = router or Router(config=RouterConfig())
        self.state_store = state_store or InMemoryConversationStateStore()
        self.workflow_runner = workflow_runner or WorkflowRunner(llm_client=llm_client)

    def handle(self, message: IncomingMessage) -> AgentExecutionResult:
        state = self.state_store.load(message.chat_id)
        decision = self.router.route(message.text, state)
        self.state_store.set_mode(message.chat_id, decision.mode)
        self.state_store.set_active_route(
            message.chat_id,
            skill_name=decision.skill_name,
            workflow_name=decision.workflow_name,
        )
        self.state_store.append_turn(message.chat_id, role="user", content=message.text)
        prompt_context = self.state_store.build_prompt_context(message.chat_id, decision.mode)

        try:
            workflow_result = self.workflow_runner.invoke(
                prompt=message.text,
                decision=decision,
                conversation_turns=prompt_context,
            )
            reply_text = workflow_result.reply_text
            for step in workflow_result.trace:
                self.state_store.append_workflow_trace(message.chat_id, step)
            if workflow_result.tool_result_summary:
                self.state_store.set_tool_summary(message.chat_id, workflow_result.tool_result_summary)
            if workflow_result.report_path:
                self.state_store.add_report_path(message.chat_id, workflow_result.report_path)
        except Exception as exc:
            reply_text = f"处理失败：{exc}"
            workflow_result = AgentExecutionResult(
                reply_text=reply_text,
                mode=decision.mode.value,
                model=decision.model,
            )

        self.state_store.append_turn(message.chat_id, role="assistant", content=reply_text)
        return AgentExecutionResult(
            reply_text=reply_text,
            mode=decision.mode.value,
            model=decision.model,
            report_path=workflow_result.report_path,
            tool_result_summary=workflow_result.tool_result_summary,
            used_tool=workflow_result.used_tool,
        )