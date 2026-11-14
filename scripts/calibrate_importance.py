#!/usr/bin/env python3
"""Print a read-only importance calibration table for the current player.

Loads the shared MiniLM model and the real persona.json. Titles come from the
active tasks in tasks.json, or from a plain text file passed with
``--titles-file``. It prints suggestions only; it never writes any files.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from system_sl.core.importance import load_goals, suggest_importance  # noqa: E402
from system_sl.core.vector_converter import get_shared_model  # noqa: E402
from system_sl.utils import get_tasks_file_path  # noqa: E402


def _read_tasks(path: Path) -> list[dict]:
    """Read active task dicts without running the app's migration writers."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return []

    if isinstance(data, list):
        return [task for task in data if isinstance(task, dict)]

    if isinstance(data, dict):
        tasks: list[dict] = []
        for value in data.values():
            if isinstance(value, list):
                tasks.extend(task for task in value if isinstance(task, dict))
        return tasks

    return []


def _parse_title_line(line: str) -> tuple[str, int | None] | None:
    """Parse one titles-file line into ``(title, own_label)``.

    A trailing ``" | N"`` with N in 1..5 is the player's own label. Any other
    line is just a title. Blank lines are ignored.
    """
    text = line.rstrip("\r\n")
    if not text.strip():
        return None

    if " | " in text:
        title, _, label_text = text.rpartition(" | ")
        label_text = label_text.strip()
        if label_text in {"1", "2", "3", "4", "5"}:
            title = title.strip()
            return (title, int(label_text)) if title else None

    return text.strip(), None


def _read_titles_file(path: Path) -> list[tuple[str, int | None]] | None:
    """Read titles (and optional labels) from a plain text file."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        print(f"Could not read titles file {path}: {exc}")
        return None

    entries: list[tuple[str, int | None]] = []
    for line in lines:
        parsed = _parse_title_line(line)
        if parsed is not None:
            entries.append(parsed)
    return entries


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print an importance calibration table without writing files."
    )
    parser.add_argument(
        "--titles-file",
        type=Path,
        metavar="PATH",
        help="read one task title per line instead of using tasks.json",
    )
    return parser.parse_args()


def _line(cells: list[str], widths: list[int]) -> str:
    return " | ".join(cell.ljust(widths[index]) for index, cell in enumerate(cells))


def main() -> None:
    args = _parse_args()
    persona_path = Path(get_tasks_file_path("persona.json"))
    goals = load_goals(persona_path)

    if args.titles_file is not None:
        entries = _read_titles_file(args.titles_file)
        if entries is None:
            return
        source_label = "titles"
        source_path = args.titles_file
    else:
        tasks_path = Path(get_tasks_file_path("tasks.json"))
        tasks = _read_tasks(tasks_path)
        entries = [
            (task["title"].strip(), None)
            for task in tasks
            if isinstance(task.get("title"), str) and task["title"].strip()
        ]
        source_label = "tasks"
        source_path = tasks_path

    print("=== Importance calibration ===")
    print(f"{'persona:':<8} {persona_path} ({len(goals)} goals)")
    print(f"{source_label + ':':<8} {source_path} ({len(entries)} titles)")
    print()

    if not entries:
        print("No task titles found.")
        return

    model = get_shared_model()
    rows = []
    for title, own_label in entries:
        level, matched_goal, relevance = suggest_importance(title, model=model)
        rows.append((title, relevance, level, matched_goal or "", own_label))

    rows.sort(key=lambda row: row[1], reverse=True)

    has_labels = any(row[4] is not None for row in rows)

    headers = ["title", "relevance", "suggested importance"]
    if has_labels:
        headers.append("my label")
    headers.append("matched goal")

    display_rows = []
    for title, relevance, level, matched_goal, own_label in rows:
        cells = [title, f"{relevance:.4f}", str(level)]
        if has_labels:
            cells.append("" if own_label is None else str(own_label))
        cells.append(matched_goal)
        display_rows.append(cells)

    widths = [
        max(len(headers[index]), *(len(row[index]) for row in display_rows))
        for index in range(len(headers))
    ]

    print(_line(headers, widths))
    print("-+-".join("-" * width for width in widths))
    for row in display_rows:
        print(_line(row, widths))

    relevances = [row[1] for row in rows]
    print()
    print(f"min relevance:    {min(relevances):.4f}")
    print(f"median relevance: {statistics.median(relevances):.4f}")
    print(f"max relevance:    {max(relevances):.4f}")
    print("count per importance level:")
    for level in range(1, 6):
        count = sum(1 for row in rows if row[2] == level)
        print(f"  {level}: {count}")

    if has_labels:
        labeled = [
            (level, own_label)
            for _, _, level, _, own_label in rows
            if own_label is not None
        ]
        exact = sum(1 for level, own_label in labeled if level == own_label)
        off_by_one = sum(
            1 for level, own_label in labeled if abs(level - own_label) == 1
        )
        off_by_two_or_more = sum(
            1 for level, own_label in labeled if abs(level - own_label) >= 2
        )

        print()
        print(f"label agreement ({len(labeled)} labeled titles):")
        print(f"  matched exactly:    {exact}")
        print(f"  off by one:         {off_by_one}")
        print(f"  off by two or more: {off_by_two_or_more}")


if __name__ == "__main__":
    main()