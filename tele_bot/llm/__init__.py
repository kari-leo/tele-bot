from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from tele_bot.llm.alibailian import AgentLLMResponse, AliBailianChatClient
	from tele_bot.llm.opencli_gateway import OpenCLIGateway

__all__ = ["AgentLLMResponse", "AliBailianChatClient", "OpenCLIGateway"]


def __getattr__(name: str):
	if name in {"AgentLLMResponse", "AliBailianChatClient"}:
		from tele_bot.llm.alibailian import AgentLLMResponse, AliBailianChatClient

		mapping = {
			"AgentLLMResponse": AgentLLMResponse,
			"AliBailianChatClient": AliBailianChatClient,
		}
		return mapping[name]

	if name == "OpenCLIGateway":
		from tele_bot.llm.opencli_gateway import OpenCLIGateway

		return OpenCLIGateway

	raise AttributeError(f"module 'tele_bot.llm' has no attribute {name!r}")
