"""
SqliteSaver factory for Phase 4.

Explicit factory only — `build_react_graph` keeps `MemorySaver` as the default
checkpointer. Persistence is opt-in:

    from tele_bot.persistence.sqlite_checkpointer import build_sqlite_saver
    from tele_bot.workflows.react_graph import build_react_graph

    saver = build_sqlite_saver("data/conversations.sqlite")
    graph = build_react_graph(llm, tools, checkpointer=saver)

Behavior:
- Creates parent directory if missing (mkdir -p)
- Opens sqlite3 connection with `check_same_thread=False` so the saver can be
  used from multiple threads in a single process (Phase 4 scope)
- Initializes the LangGraph checkpoint schema via `SqliteSaver.setup()`
- On corrupt DB (sqlite3.DatabaseError during setup), raises immediately —
  fail-fast per committee decision; no silent rebuild.
- For tests, accepts ":memory:" path (no mkdir, in-memory DB).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver


def build_sqlite_saver(path: str | Path) -> SqliteSaver:
    """Build an initialized SqliteSaver backed by a file at `path`.

    Args:
        path: filesystem path or ":memory:" for in-process testing.

    Returns:
        A `SqliteSaver` whose schema is already created.

    Raises:
        sqlite3.DatabaseError: if the existing file at `path` is corrupt.
    """
    path_str = str(path)
    if path_str != ":memory:":
        target = Path(path_str)
        target.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path_str, check_same_thread=False)
    saver = SqliteSaver(conn)
    # setup() creates the schema; on a corrupt file it raises DatabaseError.
    # We do NOT catch and rebuild — committee mandate is fail-fast.
    saver.setup()
    return saver
