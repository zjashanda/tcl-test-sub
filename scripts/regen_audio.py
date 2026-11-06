#!/usr/bin/env python3
"""静态语音语料重生成（lstts / IndexTTS）——tcl-test-sub。

用途：把仓库内置的静态语料按原始文本重新合成，用于比对或补录，不替换原文件。
原则：输出只写 `result/audio_regen/<日期>/`，不覆盖 audio/generated；产物带 manifest 与 SHA-256。
清单直接复用 audio_manifest.csv 的 text 列（2778 条）；生成参数由 lstts 决定，原语料的旧供应商参数不可考。

示例：
  python scripts/regen_audio.py plan
  python scripts/regen_audio.py build --limit 5
  python scripts/regen_audio.py build --out-dir result/audio_regen/20261106

退出码：0 全部成功；1 参数/配置错误；2 部分或全部条目失败。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

BEIJING = timezone(timedelta(hours=8))
SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ITEMS = SKILL_ROOT / "audio/manifests/audio_manifest.csv"
DEFAULT_CORPUS_DIR = SKILL_ROOT / "audio/generated"
TEXT_COLUMNS = ("text", "文本", "content", "utterance", "phrase")
NAME_COLUMNS = ("output_name", "file", "audio_path", "relative_path", "filename", "case_id")
MANIFEST_FIELDS = (
    "case_id", "text", "audio_path", "provider", "model", "voice", "container", "codec",
    "sample_rate", "channels", "duration_seconds", "mean_volume_db", "peak_volume_db",
    "size_bytes", "sha256", "generated_at", "tool", "tool_version", "status", "error",
)


def now_iso() -> str:
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_name(value: str, fallback: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (value or "").strip())
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return cleaned[:80] or fallback


def load_items(path: Path) -> list[dict[str, Any]]:
    """读取待合成清单：CSV / JSONL / JSON（含 items 数组）。文本列自动识别。"""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if suffix in {".jsonl", ".ndjson"}:
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return payload["items"]
        raise ValueError(f"JSON 清单必须是数组或含 items 数组的对象：{path}")
    if suffix in {".txt"}:
        return [{"text": line.strip()} for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    raise ValueError(f"不支持的清单格式：{path.suffix}（支持 csv/jsonl/json/txt）")


def pick(row: dict[str, Any], columns: Iterable[str]) -> str | None:
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    for column in columns:
        value = lowered.get(column)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def normalize(rows: list[dict[str, Any]], out_dir: Path, voice: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 1):
        text = pick(row, TEXT_COLUMNS)
        if not text:
            raise ValueError(f"第 {index} 条缺少文本列（支持 {', '.join(TEXT_COLUMNS)}）")
        raw_name = pick(row, NAME_COLUMNS)
        stem = Path(raw_name).stem if raw_name else safe_name(text, f"{index:05d}")
        case_id = pick(row, ("case_id",)) or stem
        items.append(
            {
                "case_id": case_id,
                "text": text,
                "output": out_dir / f"{safe_name(stem, f'{index:05d}')}.wav",
                "voice": pick(row, ("voice", "音色")) or voice,
                "source_file": raw_name or "",
            }
        )
    return items


def guard_output(out_dir: Path, corpus_dir: Path | None) -> None:
    """拒绝把重生成结果直接写回静态语料目录。"""
    if corpus_dir is None:
        return
    resolved_out, resolved_corpus = out_dir.resolve(), corpus_dir.resolve()
    if resolved_out == resolved_corpus or resolved_corpus in resolved_out.parents:
        raise ValueError(
            f"禁止把重生成结果写回静态语料目录：{out_dir}。请换一个独立输出目录，比对确认后再人工替换。"
        )


def plan(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "event": "plan",
        "count": len(items),
        "items": [
            {"case_id": item["case_id"], "text": item["text"], "voice": item["voice"],
             "output": str(item["output"]), "source_file": item["source_file"]}
            for item in items
        ],
    }


def build(items: list[dict[str, Any]], *, emotion: str | None) -> tuple[list[dict[str, Any]], int]:
    """逐条合成 + 质量校验。lstts_core 只在真正合成时导入，plan 模式无需安装。"""
    from lstts_core import __version__ as core_version
    from lstts_core import inspect_wav, load_config, synthesize_one

    config = load_config("index_tts.json")
    records: list[dict[str, Any]] = []
    failures = 0
    for item in items:
        record: dict[str, Any] = {
            "case_id": item["case_id"], "text": item["text"], "audio_path": str(item["output"]),
            "provider": "indextts", "model": "indextts", "voice": item["voice"], "container": "wav",
            "generated_at": now_iso(), "tool": "lstts_core", "tool_version": core_version,
            "status": "FAILED", "error": "",
        }
        try:
            synthesize_one(item["text"], item["output"], voice=item["voice"], emotion=emotion, config=config)
            meta = inspect_wav(item["output"], strict=True)
            record.update(
                {
                    "codec": meta["codec"], "sample_rate": meta["sample_rate"], "channels": meta["channels"],
                    "duration_seconds": meta["duration_seconds"], "mean_volume_db": meta["mean_volume_db"],
                    "peak_volume_db": meta["peak_volume_db"], "size_bytes": item["output"].stat().st_size,
                    "sha256": sha256_file(item["output"]), "status": "PASS",
                }
            )
        except Exception as error:  # 单条失败不中断批量，符合 lstts 既有语义
            failures += 1
            record["error"] = f"{type(error).__name__}: {error}"
            item["output"].unlink(missing_ok=True)
        records.append(record)
    return records, failures


def write_manifest(path: Path, records: list[dict[str, Any]], items_source: str, voice: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "generated_at": now_iso(),
        "items_source": items_source,
        "default_voice": voice,
        "provider": "indextts",
        "note": "由 regen_audio.py 生成；静态语料原文件未改动。替换前需人工比对时长/响度/哈希。",
        "fields": list(MANIFEST_FIELDS),
        "count": len(records),
        "passed": sum(1 for record in records if record["status"] == "PASS"),
        "failed": sum(1 for record in records if record["status"] != "PASS"),
        "items": records,
    }
    csv_path = path.with_suffix(".csv")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow({field: record.get(field, "") for field in MANIFEST_FIELDS})
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="用 lstts(IndexTTS) 重生成静态语音语料，不覆盖原文件")
    sub = value.add_subparsers(dest="command", required=True)
    for name in ("plan", "build"):
        cmd = sub.add_parser(name, help="只出计划" if name == "plan" else "实际合成并产 manifest")
        cmd.add_argument("--items", type=Path, default=DEFAULT_ITEMS, help="清单文件：csv/jsonl/json/txt")
        cmd.add_argument("--out-dir", type=Path, default=None,
                         help="输出目录；默认 result/audio_regen/<时间戳>/，禁止写回原语料目录")
        cmd.add_argument("--corpus-dir", type=Path, default=DEFAULT_CORPUS_DIR, help="原静态语料目录，用于防止覆盖")
        cmd.add_argument("--voice", default="付小春", help="默认音色名或参考音频路径")
        cmd.add_argument("--emotion", default=None, help="可选情感参考音频")
        cmd.add_argument("--limit", type=int, default=0, help="只处理前 N 条，0 表示全部")
        cmd.add_argument("--manifest-out", type=Path, default=None, help="manifest 输出路径")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        rows = load_items(args.items)
        out_dir = Path(args.out_dir or (SKILL_ROOT / "result" / "audio_regen" / datetime.now().strftime("%Y%m%d_%H%M%S"))).resolve()
        guard_output(out_dir, args.corpus_dir)
        items = normalize(rows, out_dir, args.voice)
        if args.limit > 0:
            items = items[: args.limit]
        if args.command == "plan":
            print(json.dumps(plan(items), ensure_ascii=False, indent=2))
            return 0
        out_dir.mkdir(parents=True, exist_ok=True)
        records, failures = build(items, emotion=args.emotion)
        manifest_path = args.manifest_out or (out_dir / "regen_manifest.json")
        write_manifest(manifest_path, records, str(args.items), args.voice)
        print(json.dumps({"event": "complete", "count": len(records), "failed": failures,
                          "manifest": str(manifest_path)}, ensure_ascii=False))
        return 0 if failures == 0 else 2
    except (ValueError, OSError, RuntimeError) as error:
        print(json.dumps({"event": "error", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
