"""Persistence — Phase 4 SqliteSaver factory.

Note: only the explicit factory is exposed here. `build_react_graph` keeps
its `MemorySaver` default; callers must pass `checkpointer=` to opt in.
"""

from tele_bot.persistence.sqlite_checkpointer import build_sqlite_saver

__all__ = ["build_sqlite_saver"]
