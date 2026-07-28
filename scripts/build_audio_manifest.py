#!/usr/bin/env python3
"""Build and verify the bundled MP3 audio manifest."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
BEIJING = timezone(timedelta(hours=8))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe(path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels:format=format_name,duration",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=15,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "ffprobe failed").strip())
    payload = json.loads(completed.stdout)
    stream = (payload.get("streams") or [{}])[0]
    media_format = payload.get("format") or {}
    return {
        "container": str(media_format.get("format_name") or ""),
        "codec": str(stream.get("codec_name") or ""),
        "sample_rate": int(stream.get("sample_rate") or 0),
        "channels": int(stream.get("channels") or 0),
        "duration_seconds": round(float(media_format.get("duration") or 0), 6),
    }


def inspect(path: Path, category: str) -> dict[str, object]:
    metadata = probe(path)
    return {
        "relative_path": path.relative_to(SKILL_ROOT).as_posix(),
        "category": category,
        "text": path.stem,
        "source": "existing_project_asset",
        "generation_metadata": "not_preserved",
        "size_bytes": path.stat().st_size,
        "sha256": sha256(path),
        **metadata,
    }


def audio_items() -> list[tuple[Path, str]]:
    groups = (
        (SKILL_ROOT / "audio" / "generated", "voice_test"),
        (SKILL_ROOT / "audio" / "online_interaction", "online_interaction"),
    )
    return [
        (path, category)
        for root, category in groups
        for path in sorted(root.glob("*.mp3"), key=lambda item: item.name)
    ]


def build(workers: int) -> int:
    items = audio_items()
    if not items:
        raise SystemExit("no MP3 files found")
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        rows = list(pool.map(lambda item: inspect(*item), items))
    hashes = [str(row["sha256"]) for row in rows]
    failures = [
        row for row in rows
        if row["container"] != "mp3" or row["codec"] != "mp3" or not row["sample_rate"]
        or not row["channels"] or not row["duration_seconds"] or not row["size_bytes"]
    ]
    summary = {
        "schema_version": 1,
        "generated_at": datetime.now(BEIJING).isoformat(timespec="milliseconds"),
        "timezone": "Asia/Shanghai",
        "file_count": len(rows),
        "category_counts": {
            category: sum(row["category"] == category for row in rows)
            for category in sorted({str(row["category"]) for row in rows})
        },
        "audio_spec_counts": {
            spec: sum(
                f"{row['container']}/{row['codec']}/{row['sample_rate']}Hz/{row['channels']}ch" == spec
                for row in rows
            )
            for spec in sorted({
                f"{row['container']}/{row['codec']}/{row['sample_rate']}Hz/{row['channels']}ch"
                for row in rows
            })
        },
        "total_bytes": sum(int(row["size_bytes"]) for row in rows),
        "duplicate_sha256_count": len(hashes) - len(set(hashes)),
        "invalid_audio_count": len(failures),
        "manifest": "audio_manifest.csv",
    }
    output_dir = SKILL_ROOT / "audio" / "manifests"
    output_dir.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0])
    with (output_dir / "audio_manifest.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    (output_dir / "audio_manifest_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not failures and not summary["duplicate_sha256_count"] else 2


def verify() -> int:
    manifest = SKILL_ROOT / "audio" / "manifests" / "audio_manifest.csv"
    if not manifest.is_file():
        raise SystemExit(f"manifest not found: {manifest}")
    with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    actual = {path.relative_to(SKILL_ROOT).as_posix(): path for path, _ in audio_items()}
    expected = {row["relative_path"]: row for row in rows}
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    mismatches = []
    for relative_path in sorted(set(expected) & set(actual)):
        path = actual[relative_path]
        row = expected[relative_path]
        if int(row["size_bytes"]) != path.stat().st_size or row["sha256"] != sha256(path):
            mismatches.append(relative_path)
    payload = {
        "status": "PASS" if not missing and not extra and not mismatches else "FAIL",
        "manifest_files": len(rows),
        "actual_files": len(actual),
        "missing": missing[:10],
        "extra": extra[:10],
        "mismatches": mismatches[:10],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "PASS" else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "verify"))
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    return build(args.workers) if args.action == "build" else verify()


if __name__ == "__main__":
    raise SystemExit(main())
