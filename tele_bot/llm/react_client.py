"""
ChatOpenAI factory for Dashscope-compatible endpoint.

Strips SOCKS proxy env vars before initialising (same fix as AliBailianChatClient).
"""

from __future__ import annotations

import os

from langchain_openai import ChatOpenAI


def _strip_socks_proxy() -> None:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        if os.environ.get(key, "").startswith("socks://"):
            os.environ.pop(key, None)


def build_chat_openai(
    *,
    api_key: str,
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
    model: str = "qwen-plus",
    temperature: float = 0,
    timeout: int = 60,
    max_retries: int = 2,
) -> ChatOpenAI:
    """Construct a ChatOpenAI pointed at the Dashscope OpenAI-compatible endpoint."""
    _strip_socks_proxy()
    return ChatOpenAI(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=temperature,
        timeout=timeout,
        max_retries=max_retries,
    )


def build_context_manager(settings, tools):
    """Separate bounded summary requests from the tool-enabled agent client."""
    from tele_bot.workflows.context_manager import ContextManager
    summarizer = build_chat_openai(
        api_key=settings.api_key, base_url=settings.base_url, model=settings.model,
        temperature=0, timeout=min(settings.timeout_seconds, 30), max_retries=0,
    ).bind(max_tokens=1200)
    return ContextManager.for_tools(tools, summarizer=summarizer)
