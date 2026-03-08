#!/usr/bin/env python3
"""Memory maintenance instinct — review daily notes and summarize."""

from datetime import datetime, timedelta
from pathlib import Path


def main():
    print("Memory Maintenance Instinct")
    workspace = Path.home() / ".openclaw" / "workspace"
    memory_dir = workspace / "memory"

    if not memory_dir.exists():
        print("No memory directory found")
        return 0

    today = datetime.now().date()
    files_read = []
    total_lines = 0
    for i in range(3):
        date = today - timedelta(days=i)
        path = memory_dir / f"{date}.md"
        if path.exists():
            content = path.read_text()
            lines = len(content.splitlines())
            total_lines += lines
            files_read.append(f"{date}: {lines} lines")

    print(f"Reviewed {len(files_read)} memory files ({total_lines} total lines):")
    for file_info in files_read:
        print(f"  - {file_info}")

    if total_lines > 100:
        print(
            "Large memory accumulation - consider updating MEMORY.md with key insights"
        )
    else:
        print("Memory files look healthy")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
