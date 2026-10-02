"""Stdio MCP server exposing the authorized project knowledge base."""

from __future__ import annotations

from mcp.server import MCPServer
from mcp_types import ToolAnnotations

from tele_bot.config.llm.settings import AliBailianSettings
from tele_bot.config.mcp import KnowledgeMCPSettings
from tele_bot.config.runtime import RuntimeSettings
from tele_bot.knowledge.search import KnowledgeSearch
from tele_bot.llm.embeddings import AliBailianEmbeddingClient
from tele_bot.mcp.knowledge import KnowledgeMCPService
from tele_bot.persistence.kb_store import KnowledgeStore

_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def build_service() -> KnowledgeMCPService:
    runtime = RuntimeSettings.from_env()
    llm = AliBailianSettings.from_env()
    identity = KnowledgeMCPSettings.from_env()
    store = KnowledgeStore(
        runtime.kb_sqlite_path,
        max_chunks_per_space=runtime.kb_max_chunks_per_space,
    )
    embeddings = AliBailianEmbeddingClient(llm)
    return KnowledgeMCPService(
        store=store,
        search_engine=KnowledgeSearch(store, embeddings, llm),
        user_id=identity.user_id,
    )


def create_server(service: KnowledgeMCPService | None = None) -> MCPServer:
    kb = service or build_service()
    server = MCPServer("tele-bot-knowledge")

    @server.tool(annotations=_READ_ONLY)
    def kb_list_spaces() -> dict:
        """List knowledge project spaces readable by the configured MCP user."""
        return kb.list_spaces()

    @server.tool(annotations=_READ_ONLY)
    def kb_search(project_space_id: str, query: str, limit: int = 5) -> dict:
        """Search one project space; membership is enforced server-side."""
        return kb.search(project_space_id, query, limit)

    @server.tool(annotations=_READ_ONLY)
    def kb_list_documents(project_space_id: str) -> dict:
        """List indexed documents in an authorized project space."""
        return kb.list_documents(project_space_id)

    @server.tool(annotations=_READ_ONLY)
    def kb_budget(project_space_id: str) -> dict:
        """Read today's embedding usage for an authorized project space."""
        return kb.budget(project_space_id)

    return server


def main() -> None:
    create_server().run()


if __name__ == "__main__":
    main()
