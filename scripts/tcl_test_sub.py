#!/usr/bin/env python3
"""List, precheck, dispatch, and repair TCLTestSub project tasks."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = Path.cwd().resolve()
TOOLS_ROOT = SKILL_ROOT / "scripts" / "voice_validation"
PLAY_SCRIPT = Path.home() / ".codex" / "skills" / "listenai-play" / "scripts" / "listenai_play.py"
DEVICE_KEY = "VID_8765&PID_5678:9_387C0FC7_0_0000"

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")

CAPABILITIES = {
    "wake-recognition": (
        TOOLS_ROOT / "run_tcl_wake_recognition_stress.py",
        "唤醒加命令识别长稳、固定/随机间隔和超时边界注入",
    ),
    "wake-interval": (
        TOOLS_ROOT / "run_tcl_wake_interval_stress.py",
        "普通话唤醒词按 2 秒/20 秒目标间隔交替压测",
    ),
    "core-command": (
        TOOLS_ROOT / "run_core_command_sweep.py",
        "当前核心命令清单离线识别覆盖",
    ),
    "online-interaction": (
        TOOLS_ROOT / "run_tcl_online_interaction_probe.py",
        "天气、笑话、音乐和停止播放在线链路/播放器探测",
    ),
}


def audio_files(mode: str) -> list[Path]:
    files = [SKILL_ROOT / "audio" / "generated" / "小T小T.mp3"]
    if mode in {"wake-recognition", "core-command"}:
        command_csv = SKILL_ROOT / "testdata" / "lexicon" / "complete_core_commands.csv"
        if command_csv.is_file():
            with command_csv.open("r", encoding="utf-8-sig", newline="") as handle:
                files.extend(
                    SKILL_ROOT / "audio" / "generated" / f"{row['text']}.mp3"
                    for row in csv.DictReader(handle)
                    if row.get("text")
                )
    if mode == "online-interaction":
        files.extend(
            SKILL_ROOT / "audio" / "online_interaction" / f"{text}.mp3"
            for text in ("今天天气怎么样", "给我讲个笑话", "播放一首音乐", "停止播放")
        )
    return list(dict.fromkeys(files))


def check(checks: list[dict[str, object]], name: str, ok: bool, details: str) -> None:
    checks.append({"name": name, "ok": ok, "details": details})


def preflight(args: argparse.Namespace) -> int:
    checks: list[dict[str, object]] = []
    check(checks, "python>=3.10", sys.version_info >= (3, 10), sys.version.split()[0])
    for module in ("serial", "openpyxl"):
        check(checks, f"module:{module}", importlib.util.find_spec(module) is not None, module)
    check(checks, "listenai-play", PLAY_SCRIPT.is_file(), str(PLAY_SCRIPT))

    modes = list(CAPABILITIES) if args.mode == "all" else [args.mode]
    for mode in modes:
        script, _ = CAPABILITIES[mode]
        check(checks, f"script:{mode}", script.is_file(), str(script))
        if mode in {"wake-recognition", "core-command"}:
            command_csv = SKILL_ROOT / "testdata" / "lexicon" / "complete_core_commands.csv"
            check(checks, "command_csv", command_csv.is_file(), str(command_csv))
        files = audio_files(mode)
        missing = [path for path in files if not path.is_file() or path.stat().st_size == 0]
        check(
            checks,
            f"audio:{mode}",
            not missing,
            f"checked={len(files)}, missing={len(missing)}, examples={[str(path) for path in missing[:5]]}",
        )

    if args.hardware:
        try:
            from serial.tools import list_ports

            detected = {item.device.upper() for item in list_ports.comports()}
            for port in ("COM20", "COM22"):
                check(checks, f"port:{port}", port in detected, f"detected={sorted(detected)}")
        except Exception as error:
            check(checks, "serial_enumeration", False, f"{type(error).__name__}: {error}")
        if PLAY_SCRIPT.is_file():
            try:
                completed = subprocess.run(
                    [sys.executable, str(PLAY_SCRIPT), "probe", "--device-key", DEVICE_KEY],
                    cwd=WORKSPACE_ROOT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    check=False,
                    timeout=30,
                )
                details = (completed.stderr or completed.stdout or "").strip()
                check(checks, "audio_device", completed.returncode == 0, details or f"returncode={completed.returncode}")
            except (OSError, subprocess.TimeoutExpired) as error:
                check(checks, "audio_device", False, f"{type(error).__name__}: {error}")

    failed = [item for item in checks if not item["ok"]]
    print(
        json.dumps(
            {
                "skill_root": str(SKILL_ROOT),
                "workspace_root": str(WORKSPACE_ROOT),
                "mode": args.mode,
                "hardware_checked": args.hardware,
                "status": "PASS" if not failed else "BLOCKED",
                "checks": checks,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not failed else 2


def run_mode(args: argparse.Namespace) -> int:
    tool_args = list(args.tool_args)
    if tool_args and tool_args[0] == "--":
        tool_args.pop(0)
    if args.mode == "core-command" and "--command-csv" not in tool_args:
        tool_args = [
            "--command-csv",
            str(SKILL_ROOT / "testdata" / "lexicon" / "complete_core_commands.csv"),
            *tool_args,
        ]
    script, _ = CAPABILITIES[args.mode]
    if not script.is_file():
        print(f"[BLOCKED] script not found: {script}", file=sys.stderr)
        return 2
    command = [sys.executable, "-u", str(script), *tool_args]
    print("Executing:", subprocess.list2cmdline(command), flush=True)
    try:
        return subprocess.run(command, cwd=WORKSPACE_ROOT, check=False).returncode
    except KeyboardInterrupt:
        print("Interrupted; inspect the newest logs task directory.", file=sys.stderr)
        return 130


def rebuild_log(run_dir: Path) -> int:
    resolved = run_dir.resolve()
    if not resolved.is_dir():
        print(f"[BLOCKED] run directory not found: {resolved}", file=sys.stderr)
        return 2
    return subprocess.run(
        [sys.executable, str(TOOLS_ROOT / "rebuild_task_log.py"), str(resolved)],
        cwd=WORKSPACE_ROOT,
        check=False,
    ).returncode


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    sub = value.add_subparsers(dest="action", required=True)
    list_cmd = sub.add_parser("list", help="List implemented capabilities")
    list_cmd.add_argument("--json", action="store_true")
    pre = sub.add_parser("preflight", help="Check files and optionally hardware")
    pre.add_argument("--mode", choices=["all", *CAPABILITIES], default="all")
    pre.add_argument("--hardware", action="store_true")
    run = sub.add_parser("run", help="Run one implemented capability")
    run.add_argument("mode", choices=CAPABILITIES)
    run.add_argument("tool_args", nargs=argparse.REMAINDER)
    rebuild = sub.add_parser("rebuild-log", help="Rebuild task.log from task_events.jsonl")
    rebuild.add_argument("run_dir", type=Path)
    return value


def main() -> int:
    args = parser().parse_args()
    if args.action == "list":
        payload = {
            name: {"description": description, "script": str(script.relative_to(SKILL_ROOT))}
            for name, (script, description) in CAPABILITIES.items()
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for name, item in payload.items():
                print(f"{name:20} {item['description']}")
        return 0
    if args.action == "preflight":
        return preflight(args)
    if args.action == "run":
        return run_mode(args)
    return rebuild_log(args.run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
