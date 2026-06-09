from __future__ import annotations

import re
from dataclasses import dataclass, field

from tele_bot.router.models import Capability, ConversationState, RouterDecision
from tele_bot.router.registry import (
    CapabilityRegistry,
    SkillRegistry,
    build_default_capability_registry,
    build_default_skill_registry,
)


@dataclass(frozen=True)
class RouterConfig:
    default_model: str = "qwen-plus"
    reasoning_model: str = "qwen3-max"
    markdown_keywords: tuple[str, ...] = (
        "markdown",
        ".md",
        "md文件",
        "输出md",
        "生成文档",
        "保存成文档",
        "结构化文档",
        "整理成文档",
        "报告文件",
    )
    reasoning_keywords: tuple[str, ...] = (
        "代码分析",
        "源码分析",
        "架构设计",
        "系统设计",
        "性能分析",
        "性能瓶颈",
        "并发",
        "多线程",
        "cuda",
        "ros",
        "故障排查",
        "诊断",
        "推理",
    )
    search_keywords: tuple[str, ...] = (
        "搜索",
        "查一下",
        "帮我查",
        "联网",
        "最新",
        "最近",
        "官网",
        "文档",
        "release",
        "新闻",
        "资料",
        "来源",
        "链接",
    )
    strong_search_keywords: tuple[str, ...] = (
        "搜索",
        "查一下",
        "帮我查",
        "联网",
        "最新",
        "最近",
        "release",
        "新闻",
        "来源",
        "链接",
    )
    restore_command: str = "/restore-cheap"
    filesystem_keywords: tuple[str, ...] = (
        "列出",
        "目录",
        "文件",
        "读取",
        "查看文件",
        "打开文件",
        "两层目录",
    )
    shell_keywords: tuple[str, ...] = (
        "执行",
        "运行",
        "shell",
        "ls ",
        "grep ",
        "find ",
        "cat ",
        "head ",
        "tail ",
        "wc ",
        "du ",
        "df ",
    )
    path_hint_pattern: str = r"([~/]|\.[/]|/[a-z0-9_./\-]+)"
    time_sensitive_pattern: str = r"(最近|最新|刚刚发布|版本|release|更新日志|官网|202[0-9])"


@dataclass
class Router:
    config: RouterConfig = field(default_factory=RouterConfig)
    skill_registry: SkillRegistry = field(default_factory=build_default_skill_registry)
    capability_registry: CapabilityRegistry = field(default_factory=build_default_capability_registry)

    def route(self, user_text: str, state: ConversationState) -> RouterDecision:
        normalized = self._normalize(user_text)
        restore_requested = self._contains_restore_command(normalized)
        shell_requested = self._looks_shell_request(normalized)
        filesystem_requested = self._looks_filesystem_request(normalized)
        markdown_requested = self._contains_any(normalized, self.config.markdown_keywords)
        reasoning_requested = self._contains_any(normalized, self.config.reasoning_keywords)
        search_requested = self._contains_any(normalized, self.config.search_keywords) or self._looks_time_sensitive(normalized)
        explicit_search_requested = self._contains_any(normalized, self.config.strong_search_keywords) or self._looks_time_sensitive(normalized)

        if restore_requested:
            skill = self.skill_registry.get("restore-cheap")
            return self._build_decision(skill_name=skill.name, model=self.config.default_model, reason="restore_skill_request")

        if shell_requested:
            skill = self.skill_registry.get("shell_inspect")
            return self._build_decision(skill_name=skill.name, model=self.config.default_model, reason="shell_request")

        if filesystem_requested:
            skill = self.skill_registry.get("filesystem_inspect")
            return self._build_decision(skill_name=skill.name, model=self.config.default_model, reason="filesystem_request")

        if markdown_requested and explicit_search_requested:
            skill = self.skill_registry.get("search_report")
            return self._build_decision(skill_name=skill.name, model=self.config.reasoning_model if reasoning_requested else self.config.default_model, reason="search_report_request")

        if markdown_requested:
            skill = self.skill_registry.get("markdown")
            capabilities = skill.capabilities + ((Capability.SEARCH_WEB,) if search_requested else ())
            return RouterDecision(
                skill_name=skill.name,
                workflow_name=skill.default_workflow.value,
                mode=skill.mode,
                model=self.config.reasoning_model if reasoning_requested else self.config.default_model,
                allow_markdown=skill.allow_markdown,
                save_markdown=skill.save_markdown,
                required_capabilities=capabilities,
                reason="explicit_markdown_request",
            )

        if reasoning_requested:
            skill = self.skill_registry.get("reasoning")
            capabilities = skill.capabilities + ((Capability.SEARCH_WEB,) if search_requested else ())
            return RouterDecision(
                skill_name=skill.name,
                workflow_name=skill.default_workflow.value,
                mode=skill.mode,
                model=self.config.reasoning_model,
                allow_markdown=skill.allow_markdown,
                save_markdown=skill.save_markdown,
                required_capabilities=capabilities,
                reason="complex_reasoning_request",
            )

        if search_requested:
            return self._build_decision(skill_name="search_report", model=self.config.default_model, reason="search_required_workflow")

        if state.mode.value == "markdown" and state.report_paths:
            return self._build_decision(skill_name="chat", model=self.config.default_model, reason="reset_after_markdown")

        return self._build_decision(skill_name="chat", model=self.config.default_model, reason="default_chat")

    def _build_decision(self, *, skill_name: str, model: str, reason: str) -> RouterDecision:
        skill = self.skill_registry.get(skill_name)
        capabilities = tuple(
            capability
            for capability in skill.capabilities
            if self.capability_registry.has(capability)
        )
        return RouterDecision(
            skill_name=skill.name,
            workflow_name=skill.default_workflow.value,
            mode=skill.mode,
            model=model,
            allow_markdown=skill.allow_markdown,
            save_markdown=skill.save_markdown,
            required_capabilities=capabilities,
            reason=reason,
        )

    @staticmethod
    def _normalize(user_text: str) -> str:
        return re.sub(r"\s+", " ", user_text.lower()).strip()

    @staticmethod
    def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
        return any(keyword.lower() in text for keyword in keywords)

    def _looks_time_sensitive(self, text: str) -> bool:
        return bool(re.search(self.config.time_sensitive_pattern, text, flags=re.IGNORECASE))

    def _looks_filesystem_request(self, text: str) -> bool:
        if not self._contains_any(text, self.config.filesystem_keywords):
            return False
        return bool(re.search(self.config.path_hint_pattern, text, flags=re.IGNORECASE))

    def _looks_shell_request(self, text: str) -> bool:
        return self._contains_any(text, self.config.shell_keywords)

    def _contains_restore_command(self, text: str) -> bool:
        return self.config.restore_command in text