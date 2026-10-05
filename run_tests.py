"""Run one test area or the complete suite with the current Python interpreter.

Examples:
    python run_tests.py schedule
    python run_tests.py codex -k model
    python run_tests.py all
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TESTS = ROOT / "tests"
GROUPS = {
    "core": (
        "test_agent.py", "test_alibailian_client.py", "test_executor.py",
        "test_router.py", "test_runtime_paths.py", "test_service.py",
        "test_state_store.py",
    ),
    "schedule": ("test_scheduled.py", "test_scheduled_execution.py", "test_mcp_schedule.py"),
    "codex": ("test_codex_commands.py", "test_codex_runner.py", "test_codex_service.py"),
    "feishu": ("test_feishu_adapter.py",),
    "knowledge": (
        "test_embeddings.py", "test_kb_store.py", "test_knowledge_ingestion.py",
        "test_knowledge_search.py", "test_knowledge_sources.py",
        "test_mcp_knowledge.py", "test_message_and_kb_models.py",
        "test_project_space_permissions.py",
    ),
    "tools": (
        "test_blog_publish_git_push_flow.py", "test_file_mutations.py",
        "test_git_push_tool.py", "test_opencli_search_tool.py",
        "test_write_report_git_push_flow.py",
    ),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("module", nargs="?", choices=("all", *GROUPS), default="all")
    parser.add_argument("--list", action="store_true", help="show available modules")
    args, pytest_args = parser.parse_known_args(argv)
    if args.list:
        for name, files in GROUPS.items():
            print(f"{name}: {', '.join(files)}")
        print("all: every test in tests/")
        return 0
    if args.module == "all":
        selected = [TESTS]
    else:
        selected = [TESTS / name for name in GROUPS[args.module]]
        missing = [str(path) for path in selected if not path.is_file()]
        if missing:
            parser.error(f"missing test files: {', '.join(missing)}")
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
               *(str(path) for path in selected), *pytest_args]
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
