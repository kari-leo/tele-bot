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
    name: TESTS / name
    for name in ("core", "schedule", "codex", "feishu", "recall", "knowledge", "tools")
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("module", nargs="?", choices=("all", *GROUPS), default="all")
    parser.add_argument("--list", action="store_true", help="show available modules")
    args, pytest_args = parser.parse_known_args(argv)
    if args.list:
        for name, directory in GROUPS.items():
            files = ", ".join(path.name for path in sorted(directory.glob("test_*.py")))
            print(f"{name} ({directory.relative_to(ROOT).as_posix()}): {files}")
        print("all: every test in tests/")
        return 0
    if args.module == "all":
        selected = [TESTS]
    else:
        directory = GROUPS[args.module]
        if not directory.is_dir() or not any(directory.glob("test_*.py")):
            parser.error(f"test module has no tests: {directory}")
        selected = [directory]
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        *(str(path) for path in selected),
        *pytest_args,
    ]
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
