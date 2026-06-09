from __future__ import annotations

from dataclasses import dataclass, field

from tele_bot.router.models import AgentMode, Capability, SkillDefinition, WorkflowName


@dataclass(frozen=True)
class CapabilityDefinition:
    name: Capability
    description: str


@dataclass
class CapabilityRegistry:
    capabilities: dict[Capability, CapabilityDefinition] = field(default_factory=dict)

    def register(self, capability: Capability, description: str) -> None:
        self.capabilities[capability] = CapabilityDefinition(name=capability, description=description)

    def has(self, capability: Capability) -> bool:
        return capability in self.capabilities


@dataclass
class SkillRegistry:
    skills: dict[str, SkillDefinition] = field(default_factory=dict)

    def register(self, skill: SkillDefinition) -> None:
        self.skills[skill.name] = skill

    def get(self, name: str) -> SkillDefinition:
        return self.skills[name]


def build_default_capability_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(Capability.SEARCH_WEB, "Use controlled web search tools")
    registry.register(Capability.WRITE_REPORT, "Persist markdown reports to reports/")
    registry.register(Capability.RESTORE_KNOWLEDGE, "Restore distilled notes into markdown documents")
    registry.register(Capability.MULTI_STEP_PLANNING, "Run explicit multi-step workflows")
    registry.register(Capability.TELEGRAM_REPLY, "Render concise Telegram replies")
    registry.register(Capability.READ_FILESYSTEM, "Read directory and file contents inside allowed roots")
    registry.register(Capability.SEARCH_FILESYSTEM, "Search files inside allowed roots")
    registry.register(Capability.EXECUTE_SHELL_SANDBOX, "Run shell commands inside a strict sandbox")
    return registry


def build_default_skill_registry() -> SkillRegistry:
    registry = SkillRegistry()
    registry.register(
        SkillDefinition(
            name="chat",
            default_workflow=WorkflowName.CHAT_REPLY,
            mode=AgentMode.CHAT,
            capabilities=(Capability.TELEGRAM_REPLY,),
        )
    )
    registry.register(
        SkillDefinition(
            name="reasoning",
            default_workflow=WorkflowName.REASONING_REPLY,
            mode=AgentMode.REASONING,
            capabilities=(Capability.TELEGRAM_REPLY,),
        )
    )
    registry.register(
        SkillDefinition(
            name="markdown",
            default_workflow=WorkflowName.MARKDOWN_REPLY,
            mode=AgentMode.MARKDOWN,
            capabilities=(Capability.TELEGRAM_REPLY, Capability.WRITE_REPORT),
            allow_markdown=True,
            save_markdown=True,
        )
    )
    registry.register(
        SkillDefinition(
            name="search_report",
            default_workflow=WorkflowName.SEARCH_REPORT,
            mode=AgentMode.MARKDOWN,
            capabilities=(
                Capability.SEARCH_WEB,
                Capability.WRITE_REPORT,
                Capability.MULTI_STEP_PLANNING,
                Capability.TELEGRAM_REPLY,
            ),
            allow_markdown=True,
            save_markdown=True,
        )
    )
    registry.register(
        SkillDefinition(
            name="filesystem_inspect",
            default_workflow=WorkflowName.FILESYSTEM_INSPECT,
            mode=AgentMode.CHAT,
            capabilities=(
                Capability.READ_FILESYSTEM,
                Capability.MULTI_STEP_PLANNING,
                Capability.TELEGRAM_REPLY,
            ),
        )
    )
    registry.register(
        SkillDefinition(
            name="shell_inspect",
            default_workflow=WorkflowName.SHELL_INSPECT,
            mode=AgentMode.CHAT,
            capabilities=(
                Capability.EXECUTE_SHELL_SANDBOX,
                Capability.MULTI_STEP_PLANNING,
                Capability.TELEGRAM_REPLY,
            ),
        )
    )
    registry.register(
        SkillDefinition(
            name="restore-cheap",
            default_workflow=WorkflowName.RESTORE_CHEAP,
            mode=AgentMode.MARKDOWN,
            capabilities=(
                Capability.RESTORE_KNOWLEDGE,
                Capability.WRITE_REPORT,
                Capability.MULTI_STEP_PLANNING,
                Capability.TELEGRAM_REPLY,
            ),
            allow_markdown=False,
            save_markdown=False,
        )
    )
    return registry