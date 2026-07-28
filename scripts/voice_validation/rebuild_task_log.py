"""Rebuild human-readable task logs from preserved structured events."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tool_timeline import ToolTimeline


def rebuild(run_dir: Path) -> None:
    events_path = run_dir / "task_events.jsonl"
    if not events_path.is_file():
        raise SystemExit(f"structured event log not found: {events_path}")

    timeline = ToolTimeline(
        run_dir / "task.log",
        echo=False,
        write_machine_events=False,
    )
    try:
        with events_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as error:
                    raise SystemExit(
                        f"invalid JSON at {events_path}:{line_number}: {error}"
                    ) from error
                event = str(payload.pop("event"))
                level = str(payload.pop("level", "INFO"))
                event_at = str(payload.pop("event_at", "")) or None
                payload.pop("logged_at", None)
                timeline.emit(
                    event,
                    level=level,
                    at=event_at,
                    echo=False,
                    **payload,
                )
    finally:
        timeline.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rebuild task.log and errors.log from task_events.jsonl"
    )
    parser.add_argument("run_dir", type=Path)
    return parser


if __name__ == "__main__":
    rebuild(build_parser().parse_args().run_dir)
