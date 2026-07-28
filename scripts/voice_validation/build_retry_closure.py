"""Build a retry command list and compare a retry task with its first-pass results."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path


RETRY_STATUSES = {"ASR_TIMEOUT", "INTENT_MISMATCH"}


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def build_retry(first_results: Path, output: Path) -> int:
    rows = read_rows(first_results)
    commands = [row["command"] for row in rows if row["status"] in RETRY_STATUSES]
    output.write_text("\n".join(commands) + "\n", encoding="utf-8")
    print(f"retry_commands={len(commands)}")
    return 0


def compare(first_results: Path, retry_results: Path, output: Path) -> int:
    first = {row["command"]: row for row in read_rows(first_results)}
    retry = {row["command"]: row for row in read_rows(retry_results)}
    rows: list[dict[str, str]] = []
    for command, first_row in first.items():
        if first_row["status"] not in RETRY_STATUSES:
            continue
        retry_row = retry.get(command, {})
        retry_status = retry_row.get("status", "NOT_RUN")
        if retry_status == "PASS":
            closure = "RETRY_PASS"
        elif retry_status == "ASR_TIMEOUT":
            closure = "STABLE_ASR_TIMEOUT"
        elif retry_status == "INTENT_MISMATCH":
            closure = "STABLE_INTENT_MISMATCH"
        else:
            closure = f"RETRY_{retry_status}"
        rows.append(
            {
                "command": command,
                "expected_intents": first_row["expected_intents"],
                "first_status": first_row["status"],
                "retry_status": retry_status,
                "closure": closure,
                "first_recognition": first_row["actual_recognition_lines"],
                "retry_recognition": retry_row.get("actual_recognition_lines", ""),
                "retry_evidence": str(retry_results.parent / "case_evidence"),
            }
        )
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["command", "closure"])
        writer.writeheader()
        writer.writerows(rows)
    counts = Counter(row["closure"] for row in rows)
    lines = [
        "# TCL 核心命令异常复测闭环",
        "",
        f"- 首轮结果：`{first_results}`",
        f"- 复测结果：`{retry_results}`",
        f"- 异常命令数：{len(rows)}",
        "",
        "| 闭环分类 | 数量 |",
        "| --- | ---: |",
    ]
    for key in sorted(counts):
        lines.append(f"| {key} | {counts[key]} |")
    output.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(output)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    build = sub.add_parser("build")
    build.add_argument("--first-results", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    compare_cmd = sub.add_parser("compare")
    compare_cmd.add_argument("--first-results", type=Path, required=True)
    compare_cmd.add_argument("--retry-results", type=Path, required=True)
    compare_cmd.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "build":
        return build_retry(args.first_results, args.output)
    return compare(args.first_results, args.retry_results, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
