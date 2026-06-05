from __future__ import annotations

import re
from dataclasses import dataclass, field

from tele_bot.router.models import AgentMode, ConversationState, RouterDecision


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
    time_sensitive_pattern: str = r"(最近|最新|刚刚发布|版本|release|更新日志|官网|文档|202[0-9])"


@dataclass
class Router:
    config: RouterConfig = field(default_factory=RouterConfig)

    def route(self, user_text: str, state: ConversationState) -> RouterDecision:
        normalized = self._normalize(user_text)
        markdown_requested = self._contains_any(normalized, self.config.markdown_keywords)
        reasoning_requested = self._contains_any(normalized, self.config.reasoning_keywords)
        search_requested = self._contains_any(normalized, self.config.search_keywords) or self._looks_time_sensitive(normalized)

        if markdown_requested:
            return RouterDecision(
                mode=AgentMode.MARKDOWN,
                model=self.config.reasoning_model if reasoning_requested else self.config.default_model,
                allow_markdown=True,
                save_markdown=True,
                allow_tools=search_requested,
                search_required=search_requested,
                reason="explicit_markdown_request",
            )

        if reasoning_requested:
            return RouterDecision(
                mode=AgentMode.REASONING,
                model=self.config.reasoning_model,
                allow_markdown=False,
                save_markdown=False,
                allow_tools=search_requested,
                search_required=search_requested,
                reason="complex_reasoning_request",
            )

        if search_requested:
            return RouterDecision(
                mode=AgentMode.CHAT,
                model=self.config.default_model,
                allow_markdown=False,
                save_markdown=False,
                allow_tools=True,
                search_required=True,
                reason="search_required_chat",
            )

        if state.mode == AgentMode.MARKDOWN and state.report_paths:
            return RouterDecision(
                mode=AgentMode.CHAT,
                model=self.config.default_model,
                allow_markdown=False,
                save_markdown=False,
                allow_tools=False,
                search_required=False,
                reason="reset_after_markdown",
            )

        return RouterDecision(
            mode=AgentMode.CHAT,
            model=self.config.default_model,
            allow_markdown=False,
            save_markdown=False,
            allow_tools=False,
            search_required=False,
            reason="default_chat",
        )

    @staticmethod
    def _normalize(user_text: str) -> str:
        return re.sub(r"\s+", " ", user_text.lower()).strip()

    @staticmethod
    def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
        return any(keyword.lower() in text for keyword in keywords)

    def _looks_time_sensitive(self, text: str) -> bool:
        return bool(re.search(self.config.time_sensitive_pattern, text, flags=re.IGNORECASE))