"""Long-running local TCL wake/recognition stability stress test.

No network, cloud control, serial write, reboot, or firmware update is used.
The task continuously logs COM20 and COM22 from start to finish. All file
timestamps use the PC's Beijing-local clock as YYYY-MM-DD HH:mm:ss.SSS.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import serial

from tool_timeline import ToolTimeline, elapsed_seconds


ROOT = Path(__file__).resolve().parents[2]
PLAY_SCRIPT = Path.home() / ".codex" / "skills" / "listenai-play" / "scripts" / "listenai_play.py"
DEFAULT_COMMAND_CSV = ROOT / "testdata" / "lexicon" / "complete_core_commands.csv"
DEFAULT_AUDIO_DIR = ROOT / "audio" / "generated"
DEFAULT_LOG_ROOT = Path.cwd() / "result"
DEVICE_KEY = "VID_8765&PID_5678:9_387C0FC7_0_0000"
WAKE_TEXT = "小T小T"
BAUDRATE = 921600
WAKE_JSON = re.compile(r'Wakeup:\s*\{.*?"keyword":"([^"]+)".*?"intent":"([^"]+)"', re.I)
ESR_TIMEOUT = re.compile(r"\besr timeout\b", re.I)
MODE_EXIT = re.compile(r"MODE\s+1\s*->\s*0", re.I)
CRITICAL_PATTERNS = {
    "panic": re.compile(r"\bpanic\b", re.I),
    "crash": re.compile(r"\bcrash\b", re.I),
    "reboot_or_reset": re.compile(r"\breboot\b|system reset|hardware reset|reset reason", re.I),
    "assert_failed": re.compile(r"assert failed|assertion failed", re.I),
    "exception": re.compile(r"\bexception\b|guru meditation|fatal error", re.I),
    "watchdog_reset": re.compile(r"watchdog.{0,20}(reset|reboot)|wdt.{0,20}(reset|reboot)", re.I),
}
PLAYER_FAILURE_PATTERNS = {
    "player_malloc_probe_fail": re.compile(r"malloc probe buf fail", re.I),
    "player_event_error": re.compile(r"PLAYER_EVENT_ERROR|PLAYER_AGENT_MSG_PLAY_ERROR", re.I),
}
WARNING_PATTERNS = {
    "wdt_not_feed": re.compile(r"wdt not feed", re.I),
    "app_unsupported": re.compile(r"dont support this keyword", re.I),
    "player_url_context_null": re.compile(r"url context is null", re.I),
}


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def task_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def wait_interruptible(seconds: float, stop_file: Path, progress_callback) -> bool:
    deadline = time.monotonic() + max(0, seconds)
    while time.monotonic() < deadline:
        if stop_file.exists():
            return False
        progress_callback()
        time.sleep(min(0.5, max(0.01, deadline - time.monotonic())))
    return not stop_file.exists()


class ContinuousSerialLogger:
    def __init__(self, run_dir: Path, ports: dict[str, str], timeline: ToolTimeline) -> None:
        self.run_dir = run_dir
        self.ports = ports
        self.timeline = timeline
        self.logs_dir = run_dir / "serial_logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.events: list[dict[str, str]] = []
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.errors: dict[str, str] = {}
        self.threads: list[threading.Thread] = []
        self.merged = (self.logs_dir / "merged.log").open("a", encoding="utf-8", newline="")
        self.handles = {
            port: (self.logs_dir / f"{port}_{role}.log").open("a", encoding="utf-8", newline="")
            for port, role in ports.items()
        }

    def start(self) -> None:
        self.timeline.emit("SERIAL_LOGGER_START", ports=self.ports, baudrate=BAUDRATE)
        for port, role in self.ports.items():
            thread = threading.Thread(target=self._run_port, args=(port, role), daemon=True)
            thread.start()
            self.threads.append(thread)

    def _run_port(self, port: str, role: str) -> None:
        partial = ""
        try:
            with serial.Serial(port, BAUDRATE, timeout=0.2) as handle:
                self.timeline.emit("SERIAL_PORT_OPEN", port=port, role=role, baudrate=BAUDRATE)
                while not self.stop_event.is_set():
                    data = handle.read(handle.in_waiting or 1)
                    if not data:
                        continue
                    partial += data.decode("utf-8", errors="replace").replace("\r", "")
                    while "\n" in partial:
                        line, partial = partial.split("\n", 1)
                        self.append(port, role, line)
                if partial.strip():
                    self.append(port, role, partial)
        except Exception as error:
            self.errors[port] = str(error)
            self.timeline.emit("SERIAL_PORT_ERROR", level="ERROR", port=port, role=role, error=str(error))

    def append(self, port: str, role: str, line: str) -> None:
        if not line:
            return
        event = {"timestamp": now(), "port": port, "role": role, "line": line}
        rendered = f"{event['timestamp']} [{port}/{role}] {line}\n"
        with self.lock:
            self.events.append(event)
            cursor = len(self.events)
            self.handles[port].write(rendered)
            self.handles[port].flush()
            self.merged.write(rendered)
            self.merged.flush()
        self.timeline.serial_marker(event["timestamp"], port, role, line, cursor)

    def cursor(self) -> int:
        with self.lock:
            return len(self.events)

    def since(self, cursor: int) -> list[dict[str, str]]:
        with self.lock:
            return list(self.events[cursor:])

    def close(self) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=2)
        for handle in self.handles.values():
            handle.close()
        self.merged.close()
        self.timeline.emit("SERIAL_LOGGER_STOP", serial_errors=self.errors, final_cursor=self.cursor())


def run_audio(text: str, audio_dir: Path, tool_log: Path) -> dict:
    audio_file = audio_dir / f"{text}.mp3"
    started_at = now()
    if not audio_file.is_file() or audio_file.stat().st_size == 0:
        payload = {"text": text, "audio_file": str(audio_file), "started_at": started_at, "ended_at": now(), "returncode": 2, "error": "missing_audio"}
    else:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        try:
            completed = subprocess.run(
                ["python", str(PLAY_SCRIPT), "play", "--audio-file", str(audio_file), "--device-key", DEVICE_KEY, "--skip-probe"],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=30,
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            payload = {
                "text": text,
                "audio_file": str(audio_file),
                "started_at": started_at,
                "ended_at": now(),
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        except subprocess.TimeoutExpired as error:
            payload = {
                "text": text,
                "audio_file": str(audio_file),
                "started_at": started_at,
                "ended_at": now(),
                "returncode": 124,
                "error": f"playback_timeout: {error}",
            }
    with tool_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return payload


def probe_audio_device() -> tuple[bool, str]:
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    try:
        completed = subprocess.run(
            ["python", str(PLAY_SCRIPT), "probe", "--device-key", DEVICE_KEY],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=30,
            startupinfo=startupinfo,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    details = (completed.stderr or completed.stdout or "").strip()
    return completed.returncode == 0, details


def get_commands(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle) if row.get("text")]


def parse_round(events: list[dict[str, str]], csk_port: str) -> dict:
    wakes = []
    for event in events:
        if event["port"] != csk_port:
            continue
        match = WAKE_JSON.search(event["line"])
        if match:
            wakes.append(
                {
                    "keyword": match.group(1),
                    "intent": match.group(2),
                    "line": event["line"],
                    "timestamp": event["timestamp"],
                }
            )
    command_wakes = [item for item in wakes if item["keyword"] != "xiao ti xiao ti"]
    timeouts = [event for event in events if ESR_TIMEOUT.search(event["line"])]
    critical = [name for name, pattern in CRITICAL_PATTERNS.items() if any(pattern.search(event["line"]) for event in events)]
    player_failures = [
        name
        for name, pattern in PLAYER_FAILURE_PATTERNS.items()
        if any(pattern.search(event["line"]) for event in events)
    ]
    warnings = [name for name, pattern in WARNING_PATTERNS.items() if any(pattern.search(event["line"]) for event in events)]
    return {
        "wake_detected": bool(wakes),
        "wake_timestamp": wakes[0]["timestamp"] if wakes else "",
        "command_keyword": command_wakes[-1]["keyword"] if command_wakes else "",
        "command_intent": command_wakes[-1]["intent"] if command_wakes else "",
        "command_timestamp": command_wakes[-1]["timestamp"] if command_wakes else "",
        "asr_timeout": bool(timeouts),
        "asr_timeout_timestamp": timeouts[-1]["timestamp"] if timeouts else "",
        "recognition_exit": any(MODE_EXIT.search(event["line"]) for event in events),
        "critical_markers": " | ".join(critical),
        "player_failure_markers": " | ".join(player_failures),
        "warning_markers": " | ".join(warnings),
    }


def run_stress(args: argparse.Namespace) -> int:
    commands = get_commands(args.command_csv)
    if args.commands_file:
        requested = [line.strip() for line in args.commands_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        by_text = {command["text"]: command for command in commands}
        missing = [text for text in requested if text not in by_text]
        if missing:
            raise SystemExit(f"commands missing from command CSV: {missing}")
        commands = [by_text[text] for text in requested]
    if not commands:
        raise SystemExit("no commands selected")
    rng = random.Random(args.seed)
    run_dir = args.logs_root / f"{task_stamp()}_wake_recognition_stress"
    tool_dir = run_dir / "tool_logs"
    tool_dir.mkdir(parents=True, exist_ok=False)
    stop_file = run_dir / "STOP"
    progress_file = run_dir / "progress.json"
    rounds_file = run_dir / "rounds.csv"
    tool_log = tool_dir / "playback.jsonl"
    timeline = ToolTimeline(run_dir / "task.log")
    config = vars(args).copy()
    config.update({"started_at": now(), "device_key": DEVICE_KEY, "command_pool_size": len(commands), "timestamp_policy": "Beijing local clock without timezone suffix"})
    (run_dir / "task_config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger = ContinuousSerialLogger(run_dir, {args.csk_port: "csk_ap", args.upper_port: "upper"}, timeline)
    timeline.emit(
        "TASK_CONFIG",
        task_name="TCL 唤醒与命令词稳定性压测",
        task_type="wake_recognition_round",
        run_dir=str(run_dir),
        target_rounds=args.rounds,
        duration_minutes=args.duration_minutes,
        command_pool_size=len(commands),
        interval_mode=args.interval_mode,
        interval_description=(
            f"固定 {args.fixed_interval_s:g} 秒"
            if args.interval_mode == "fixed"
            else f"随机 {args.min_interval_s:g}-{args.max_interval_s:g} 秒"
        ),
        wake_command_delay_mode=args.wake_command_delay_mode,
        wake_command_delay_description=(
            f"固定 {args.fixed_wake_command_delay_s:g} 秒"
            if args.wake_command_delay_mode == "fixed"
            else f"随机 {args.min_wake_command_delay_s:g}-{args.max_wake_command_delay_s:g} 秒"
        ),
        boundary_ratio=args.boundary_ratio,
        boundary_delay_description=(
            f"固定 {args.fixed_boundary_delay_s:g} 秒"
            if args.boundary_delay_mode == "fixed"
            else f"随机 {args.boundary_min_s:g}-{args.boundary_max_s:g} 秒"
        ),
        wake_wait_s=args.wake_wait_s,
        recognition_observe_s=args.recognition_observe_s,
        csk_port=args.csk_port,
        upper_port=args.upper_port,
        baudrate=BAUDRATE,
    )
    timeline.set_context(target_rounds=args.rounds)

    required_audio = [WAKE_TEXT, *(command["text"] for command in commands)]
    missing_audio = [
        text
        for text in required_audio
        if not (args.audio_dir / f"{text}.mp3").is_file()
        or (args.audio_dir / f"{text}.mp3").stat().st_size == 0
    ]
    timeline.emit(
        "AUDIO_ASSET_CHECK",
        level="INFO" if not missing_audio else "ERROR",
        checked_count=len(required_audio),
        missing_count=len(missing_audio),
        missing_examples=missing_audio[:10],
    )
    if missing_audio:
        timeline.set_context(round=None, phase="finished")
        timeline.emit(
            "TASK_END",
            completed_rounds=0,
            target_rounds=args.rounds,
            status_counts={"BLOCKED_AUDIO": 1},
            outcome="FAILED",
            error=f"missing audio assets: {len(missing_audio)}",
            serial_errors={},
            serial_marker_counts={},
            serial_failure_marker_counts={},
            final_cursor=0,
        )
        timeline.close()
        raise SystemExit(f"missing or empty audio files: {missing_audio[:10]}")

    timeline.emit("AUDIO_DEVICE_PROBE_START")
    audio_device_ok, audio_device_details = probe_audio_device()
    timeline.emit(
        "AUDIO_DEVICE_PROBE_END",
        level="INFO" if audio_device_ok else "ERROR",
        success=audio_device_ok,
        details=audio_device_details,
    )
    if not audio_device_ok:
        timeline.set_context(round=None, phase="finished")
        timeline.emit(
            "TASK_END",
            completed_rounds=0,
            target_rounds=args.rounds,
            status_counts={"BLOCKED_AUDIO": 1},
            outcome="FAILED",
            error=audio_device_details or "audio device probe failed",
            serial_errors={},
            serial_marker_counts={},
            serial_failure_marker_counts={},
            final_cursor=0,
        )
        timeline.close()
        raise SystemExit(audio_device_details or "audio device probe failed")

    logger.start()
    time.sleep(2)
    started_monotonic = time.monotonic()
    started_at = now()
    timeline.emit(
        "TASK_START",
        at=started_at,
        run_dir=str(run_dir),
        target_rounds=args.rounds,
        interval_mode=args.interval_mode,
        wake_command_delay_mode=args.wake_command_delay_mode,
        boundary_ratio=args.boundary_ratio,
        command_pool_size=len(commands),
    )
    rounds: list[dict] = []
    counters = Counter()
    current = {"phase": "initializing", "round": 0, "command": ""}

    def write_progress() -> None:
        progress_file.write_text(
            json.dumps(
                {
                    "updated_at": now(),
                    "started_at": started_at,
                    "elapsed_seconds": round(time.monotonic() - started_monotonic, 1),
                    "finished": False,
                    "phase": current["phase"],
                    "current_round": current["round"],
                    "current_command": current["command"],
                    "completed_rounds": len(rounds),
                    "target_rounds": args.rounds,
                    "round_status_counts": dict(counters),
                    "serial_errors": logger.errors,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    fields = [
        "round", "started_at", "ended_at", "interval_mode", "pre_round_interval_s", "boundary_case", "wake_to_command_delay_s",
        "command", "expected_intents", "expected_semantics", "wake_playback_rc", "command_playback_rc", "status",
        "actual_keyword", "actual_intent", "asr_timeout", "recognition_exit", "critical_markers", "player_failure_markers",
        "warning_markers", "evidence_start", "evidence_end",
    ]
    task_error = ""
    try:
        with rounds_file.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for number in range(1, args.rounds + 1):
                if args.duration_minutes and time.monotonic() - started_monotonic >= args.duration_minutes * 60:
                    timeline.emit("TASK_DURATION_LIMIT", elapsed_s=round(time.monotonic() - started_monotonic, 3))
                    break
                if stop_file.exists():
                    current["phase"] = "stop_requested"
                    timeline.set_context(round=number, phase=current["phase"])
                    timeline.emit("STOP_REQUESTED", level="WARN", stop_file=str(stop_file))
                    write_progress()
                    break
                command = commands[(number - 1) % len(commands)] if args.cycle_commands else rng.choice(commands)
                current.update({"phase": "interval_wait", "round": number, "command": command["text"]})
                active_round = number - 1 if number > 1 else 1
                timeline.set_context(
                    round=active_round,
                    next_round=number if number > 1 else None,
                    phase=current["phase"],
                    command=None,
                    next_command=command["text"],
                )
                interval = args.fixed_interval_s if args.interval_mode == "fixed" else rng.uniform(args.min_interval_s, args.max_interval_s)
                write_progress()
                timeline.emit(
                    "ROUND_PLAN",
                    planned_round=number,
                    source_round=number - 1 if number > 1 else None,
                    command=command["text"],
                    pre_interval_s=round(interval, 3),
                    expected_intents=command.get("expected_intents", ""),
                )
                interval_started = time.monotonic()
                timeline.emit("INTERVAL_WAIT_START", wait_s=round(interval, 3))
                if not wait_interruptible(interval, stop_file, write_progress):
                    timeline.emit(
                        "INTERVAL_WAIT_INTERRUPTED",
                        level="WARN",
                        elapsed_s=round(time.monotonic() - interval_started, 3),
                    )
                    break
                timeline.emit("INTERVAL_WAIT_END", elapsed_s=round(time.monotonic() - interval_started, 3))
                start_cursor = logger.cursor()
                round_started = now()
                current["phase"] = "wake_playback"
                timeline.set_context(
                    round=number,
                    next_round=None,
                    phase=current["phase"],
                    command=command["text"],
                    next_command=None,
                )
                write_progress()
                timeline.emit(
                    "ROUND_START",
                    current_round=number,
                    target_rounds=args.rounds,
                    command=command["text"],
                    evidence_start=start_cursor,
                )
                timeline.emit("WAKE_PLAYBACK_START", text=WAKE_TEXT, evidence_cursor=start_cursor)
                wake = run_audio(WAKE_TEXT, args.audio_dir, tool_log)
                timeline.emit(
                    "WAKE_PLAYBACK_END",
                    text=WAKE_TEXT,
                    audio_rc=wake["returncode"],
                    elapsed_s=elapsed_seconds(wake),
                    evidence_cursor=logger.cursor(),
                    level="INFO" if wake["returncode"] == 0 else "ERROR",
                )
                wake_deadline = time.monotonic() + args.wake_wait_s
                current["phase"] = "wake_detection_wait"
                timeline.set_context(round=number, phase=current["phase"])
                timeline.emit("WAKE_DETECTION_WAIT_START", timeout_s=args.wake_wait_s)
                while time.monotonic() < wake_deadline and not parse_round(logger.since(start_cursor), args.csk_port)["wake_detected"]:
                    if stop_file.exists():
                        break
                    time.sleep(0.1)
                after_wake = logger.since(start_cursor)
                wake_evidence = parse_round(after_wake, args.csk_port)
                wake_ok = wake["returncode"] == 0 and wake_evidence["wake_detected"]
                timeline.emit(
                    "WAKE_RESULT",
                    level="INFO" if wake_ok else "ERROR",
                    status="RECOGNIZED" if wake_ok else "NOT_RECOGNIZED",
                    audio_rc=wake["returncode"],
                    detected_at=wake_evidence["wake_timestamp"],
                    evidence_cursor=logger.cursor(),
                )
                boundary_case = rng.random() < args.boundary_ratio
                if boundary_case:
                    command_delay = (
                        args.fixed_boundary_delay_s
                        if args.boundary_delay_mode == "fixed"
                        else rng.uniform(args.boundary_min_s, args.boundary_max_s)
                    )
                else:
                    command_delay = (
                        args.fixed_wake_command_delay_s
                        if args.wake_command_delay_mode == "fixed"
                        else rng.uniform(args.min_wake_command_delay_s, args.max_wake_command_delay_s)
                    )
                command_playback = {"returncode": ""}
                if not wake_ok:
                    status = "WAKE_FAIL" if wake["returncode"] == 0 else "AUDIO_FAIL"
                    timeline.emit(
                        "COMMAND_SKIPPED",
                        level="ERROR",
                        command=command["text"],
                        reason=status,
                    )
                else:
                    current["phase"] = "boundary_wait" if boundary_case else "wake_to_command_wait"
                    timeline.set_context(round=number, phase=current["phase"])
                    write_progress()
                    command_wait_started = time.monotonic()
                    timeline.emit(
                        "COMMAND_DELAY_START",
                        wait_s=round(command_delay, 3),
                        boundary_case=boundary_case,
                    )
                    if not wait_interruptible(command_delay, stop_file, write_progress):
                        timeline.emit(
                            "COMMAND_DELAY_INTERRUPTED",
                            level="WARN",
                            elapsed_s=round(time.monotonic() - command_wait_started, 3),
                        )
                        break
                    timeline.emit(
                        "COMMAND_DELAY_END",
                        elapsed_s=round(time.monotonic() - command_wait_started, 3),
                        boundary_case=boundary_case,
                    )
                    command_cursor = logger.cursor()
                    current["phase"] = "command_playback"
                    timeline.set_context(round=number, phase=current["phase"])
                    write_progress()
                    timeline.emit(
                        "COMMAND_PLAYBACK_START",
                        command=command["text"],
                        expected_intents=command.get("expected_intents", ""),
                        evidence_cursor=command_cursor,
                    )
                    command_playback = run_audio(command["text"], args.audio_dir, tool_log)
                    timeline.emit(
                        "COMMAND_PLAYBACK_END",
                        level="INFO" if command_playback["returncode"] == 0 else "ERROR",
                        command=command["text"],
                        audio_rc=command_playback["returncode"],
                        elapsed_s=elapsed_seconds(command_playback),
                        evidence_cursor=logger.cursor(),
                    )
                    deadline = time.monotonic() + args.recognition_observe_s
                    current["phase"] = "recognition_wait"
                    timeline.set_context(round=number, phase=current["phase"])
                    timeline.emit(
                        "RECOGNITION_WAIT_START",
                        command=command["text"],
                        timeout_s=args.recognition_observe_s,
                        evidence_cursor=command_cursor,
                    )
                    while time.monotonic() < deadline:
                        evidence = parse_round(logger.since(command_cursor), args.csk_port)
                        if evidence["command_intent"] or evidence["asr_timeout"] or evidence["critical_markers"]:
                            break
                        if stop_file.exists():
                            break
                        time.sleep(0.1)
                    evidence = parse_round(logger.since(command_cursor), args.csk_port)
                    if command_playback["returncode"] != 0:
                        status = "AUDIO_FAIL"
                    elif evidence["critical_markers"]:
                        status = "CRITICAL_FIRMWARE_MARKER"
                    elif evidence["player_failure_markers"]:
                        status = "PLAYER_FAIL"
                    elif evidence["command_intent"]:
                        status = "RECOGNIZED"
                    elif evidence["asr_timeout"]:
                        status = "ASR_TIMEOUT"
                    else:
                        status = "NO_RECOGNITION"
                    result_level = "INFO" if status == "RECOGNIZED" else "CRITICAL" if status == "CRITICAL_FIRMWARE_MARKER" else "WARN"
                    timeline.emit(
                        "RECOGNITION_RESULT",
                        level=result_level,
                        command=command["text"],
                        status=status,
                        actual_keyword=evidence["command_keyword"] or "-",
                        actual_intent=evidence["command_intent"] or "-",
                        detected_at=evidence["command_timestamp"] or evidence["asr_timeout_timestamp"],
                        asr_timeout=evidence["asr_timeout"],
                        critical=evidence["critical_markers"] or "-",
                        player_failures=evidence["player_failure_markers"] or "-",
                        warnings=evidence["warning_markers"] or "-",
                        evidence_cursor=logger.cursor(),
                    )
                end_cursor = logger.cursor()
                all_evidence = logger.since(start_cursor)
                parsed = parse_round(all_evidence, args.csk_port)
                if parsed["critical_markers"] and status != "CRITICAL_FIRMWARE_MARKER":
                    previous_status = status
                    status = "CRITICAL_FIRMWARE_MARKER"
                    timeline.emit(
                        "ROUND_STATUS_OVERRIDE",
                        level="CRITICAL",
                        previous_status=previous_status,
                        status=status,
                        critical=parsed["critical_markers"],
                        evidence_cursor=logger.cursor(),
                    )
                elif parsed["player_failure_markers"] and status != "PLAYER_FAIL":
                    previous_status = status
                    status = "PLAYER_FAIL"
                    timeline.emit(
                        "ROUND_STATUS_OVERRIDE",
                        level="ERROR",
                        previous_status=previous_status,
                        status=status,
                        player_failures=parsed["player_failure_markers"],
                        evidence_cursor=logger.cursor(),
                    )
                evidence_path = run_dir / "round_evidence" / f"round_{number:05d}.log"
                evidence_path.parent.mkdir(exist_ok=True)
                evidence_path.write_text(
                    "\n".join(f"{item['timestamp']} [{item['port']}/{item['role']}] {item['line']}" for item in all_evidence) + "\n",
                    encoding="utf-8",
                )
                timeline.emit(
                    "EVIDENCE_SAVED",
                    evidence_file=str(evidence_path.relative_to(run_dir)),
                    evidence_start=start_cursor,
                    evidence_end=end_cursor,
                )
                result = {
                    "round": number,
                    "started_at": round_started,
                    "ended_at": now(),
                    "interval_mode": args.interval_mode,
                    "pre_round_interval_s": f"{interval:.3f}",
                    "boundary_case": boundary_case,
                    "wake_to_command_delay_s": f"{command_delay:.3f}",
                    "command": command["text"],
                    "expected_intents": command.get("expected_intents", ""),
                    "expected_semantics": command.get("standard_semantics", ""),
                    "wake_playback_rc": wake["returncode"],
                    "command_playback_rc": command_playback.get("returncode", ""),
                    "status": status,
                    "actual_keyword": parsed["command_keyword"],
                    "actual_intent": parsed["command_intent"],
                    "asr_timeout": parsed["asr_timeout"],
                    "recognition_exit": parsed["recognition_exit"],
                    "critical_markers": parsed["critical_markers"],
                    "player_failure_markers": parsed["player_failure_markers"],
                    "warning_markers": parsed["warning_markers"],
                    "evidence_start": start_cursor,
                    "evidence_end": end_cursor,
                }
                writer.writerow(result)
                handle.flush()
                rounds.append(result)
                counters[status] += 1
                current["phase"] = "round_complete"
                timeline.set_context(round=number, phase=current["phase"])
                write_progress()
                round_level = (
                    "INFO"
                    if status == "RECOGNIZED"
                    else "CRITICAL"
                    if status == "CRITICAL_FIRMWARE_MARKER"
                    else "ERROR"
                    if status == "PLAYER_FAIL"
                    else "WARN"
                )
                timeline.emit(
                    "ROUND_END",
                    level=round_level,
                    current_round=number,
                    target_rounds=args.rounds,
                    command=command["text"],
                    status=status,
                    actual_keyword=parsed["command_keyword"] or "-",
                    actual_intent=parsed["command_intent"] or "-",
                    asr_timeout=parsed["asr_timeout"],
                    recognition_exit=parsed["recognition_exit"],
                    critical=parsed["critical_markers"] or "-",
                    player_failures=parsed["player_failure_markers"] or "-",
                    warnings=parsed["warning_markers"] or "-",
                    evidence_start=start_cursor,
                    evidence_end=end_cursor,
                )
    except BaseException as error:
        task_error = f"{type(error).__name__}: {error}"
        timeline.emit(
            "TASK_EXCEPTION",
            level="CRITICAL",
            error_type=type(error).__name__,
            error=str(error),
            evidence_cursor=logger.cursor(),
            serial_errors=logger.errors,
        )
        raise
    finally:
        logger.close()
        ended_at = now()
        critical_counts = Counter(marker for row in rounds for marker in row["critical_markers"].split(" | ") if marker)
        player_failure_counts = Counter(
            marker for row in rounds for marker in row["player_failure_markers"].split(" | ") if marker
        )
        warning_counts = Counter(marker for row in rounds for marker in row["warning_markers"].split(" | ") if marker)
        normal_rounds = [row for row in rounds if not row["boundary_case"]]
        boundary_rounds = [row for row in rounds if row["boundary_case"]]
        normal_status_counts = Counter(row["status"] for row in normal_rounds)
        boundary_status_counts = Counter(row["status"] for row in boundary_rounds)
        normal_failed_rounds = sum(
            count for status, count in normal_status_counts.items() if status != "RECOGNIZED"
        )
        boundary_expected_rounds = sum(
            count
            for status, count in boundary_status_counts.items()
            if status in {"ASR_TIMEOUT", "NO_RECOGNITION"}
        )
        boundary_unexpected_rounds = sum(
            count
            for status, count in boundary_status_counts.items()
            if status not in {"RECOGNIZED", "ASR_TIMEOUT", "NO_RECOGNITION"}
        )
        outcome = (
            "FAILED"
            if task_error
            or logger.errors
            or timeline.failure_counts()
            or normal_failed_rounds
            or boundary_unexpected_rounds
            else "STOPPED"
            if stop_file.exists()
            else "COMPLETED"
        )
        summary = {
            "started_at": started_at,
            "ended_at": ended_at,
            "completed_rounds": len(rounds),
            "target_rounds": args.rounds,
            "duration_minutes": args.duration_minutes,
            "duration_limit_reached": bool(
                args.duration_minutes
                and time.monotonic() - started_monotonic >= args.duration_minutes * 60
            ),
            "status_counts": dict(counters),
            "normal_window": {
                "rounds": len(normal_rounds),
                "status_counts": dict(normal_status_counts),
                "recognized": normal_status_counts.get("RECOGNIZED", 0),
                "failed": normal_failed_rounds,
                "recognition_rate": round(
                    normal_status_counts.get("RECOGNIZED", 0) / len(normal_rounds), 6
                )
                if normal_rounds
                else 0,
            },
            "boundary_window": {
                "rounds": len(boundary_rounds),
                "status_counts": dict(boundary_status_counts),
                "expected_timeout_or_no_recognition": boundary_expected_rounds,
                "unexpected_results": boundary_unexpected_rounds,
                "expected_rate": round(boundary_expected_rounds / len(boundary_rounds), 6)
                if boundary_rounds
                else 0,
            },
            "critical_marker_round_counts": dict(critical_counts),
            "player_failure_marker_round_counts": dict(player_failure_counts),
            "warning_marker_round_counts": dict(warning_counts),
            "serial_errors": logger.errors,
            "stop_requested": stop_file.exists(),
            "task_log": "task.log",
            "task_events": "task_events.jsonl",
            "errors_log": "errors.log",
            "outcome": outcome,
            "error": task_error,
            "serial_marker_counts": timeline.marker_counts(),
            "serial_failure_marker_counts": timeline.failure_counts(),
        }
        (run_dir / "summary_final.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            "# TCL 唤醒识别稳定性压测结果",
            "",
            f"- 开始：{started_at}",
            f"- 结束：{ended_at}",
            f"- 完成轮次：{len(rounds)} / {args.rounds}",
            f"- 运行时长限制：{args.duration_minutes:g} 分钟" if args.duration_minutes else "- 运行时长限制：无",
            f"- 间隔模式：{args.interval_mode}",
            f"- 超时边界注入比例：{args.boundary_ratio}",
            "- 串口日志：COM20/COM22 从任务开始到结束连续记录。",
            "- 中文工具主日志：`task.log`。",
            "- 结构化工具事件：`task_events.jsonl`。",
            "- 异常汇总：`errors.log`。",
            "",
            "## 轮次状态",
            "",
            "| 状态 | 数量 |",
            "| --- | ---: |",
        ]
        for name, count in sorted(counters.items()):
            lines.append(f"| {name} | {count} |")
        lines.extend(
            [
                "",
                "## 场景判定",
                "",
                f"- 正常识别窗口：{len(normal_rounds)} 轮，识别成功 {normal_status_counts.get('RECOGNIZED', 0)} 轮，"
                f"真实失败 {normal_failed_rounds} 轮，识别率 "
                f"{normal_status_counts.get('RECOGNIZED', 0) / len(normal_rounds):.2%}。"
                if normal_rounds
                else "- 正常识别窗口：0 轮。",
                f"- 超时边界窗口：{len(boundary_rounds)} 轮，按预期超时或无识别 {boundary_expected_rounds} 轮，"
                f"非预期结果 {boundary_unexpected_rounds} 轮。",
                "- 边界轮用于验证识别超时状态机，不并入正常窗口识别失败率。",
            ]
        )
        lines.extend(["", "## 固件/日志异常", ""])
        lines.append(f"- 串口错误：{json.dumps(logger.errors, ensure_ascii=False)}")
        lines.append(f"- 严重 marker 轮次：{json.dumps(dict(critical_counts), ensure_ascii=False)}")
        lines.append(f"- 播放器失败 marker 轮次：{json.dumps(dict(player_failure_counts), ensure_ascii=False)}")
        lines.append(f"- 告警 marker 轮次：{json.dumps(dict(warning_counts), ensure_ascii=False)}")
        (run_dir / "summary_final.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        progress_file.write_text(
            json.dumps({"updated_at": ended_at, "finished": True, "completed_rounds": len(rounds), "target_rounds": args.rounds, "round_status_counts": dict(counters), "serial_errors": logger.errors}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        timeline.set_context(round=None, next_round=None, command=None, next_command=None, phase="finished")
        timeline.emit(
            "TASK_END",
            at=ended_at,
            run_dir=str(run_dir),
            completed_rounds=len(rounds),
            target_rounds=args.rounds,
            status_counts=dict(counters),
            outcome=outcome,
            error=task_error,
            serial_errors=logger.errors,
            serial_marker_counts=timeline.marker_counts(),
            serial_failure_marker_counts=timeline.failure_counts(),
            final_cursor=logger.cursor(),
        )
        timeline.close()
    return 0 if outcome == "COMPLETED" else 3 if outcome == "STOPPED" else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="TCL local wake/recognition long stability stress")
    value.add_argument("--command-csv", type=Path, default=DEFAULT_COMMAND_CSV)
    value.add_argument("--commands-file", type=Path, default=None, help="UTF-8 command texts to run; duplicate lines are retained")
    value.add_argument("--cycle-commands", action="store_true", help="run selected commands in file order instead of random sampling")
    value.add_argument("--audio-dir", type=Path, default=DEFAULT_AUDIO_DIR)
    value.add_argument("--logs-root", type=Path, default=DEFAULT_LOG_ROOT)
    value.add_argument("--csk-port", default="COM20")
    value.add_argument("--upper-port", default="COM22")
    value.add_argument("--rounds", type=int, default=1000)
    value.add_argument("--duration-minutes", type=float, default=0, help="0 means no duration limit")
    value.add_argument("--interval-mode", choices=["fixed", "random"], default="random")
    value.add_argument("--fixed-interval-s", type=float, default=5.0)
    value.add_argument("--min-interval-s", type=float, default=2.0)
    value.add_argument("--max-interval-s", type=float, default=10.0)
    value.add_argument("--wake-command-delay-mode", choices=["fixed", "random"], default="fixed")
    value.add_argument("--fixed-wake-command-delay-s", type=float, default=1.6)
    value.add_argument("--min-wake-command-delay-s", type=float, default=1.0)
    value.add_argument("--max-wake-command-delay-s", type=float, default=3.0)
    value.add_argument("--boundary-ratio", type=float, default=0.2)
    value.add_argument("--boundary-delay-mode", choices=["fixed", "random"], default="random")
    value.add_argument("--fixed-boundary-delay-s", type=float, default=14.0)
    value.add_argument("--boundary-min-s", type=float, default=13.0)
    value.add_argument("--boundary-max-s", type=float, default=15.5)
    value.add_argument("--wake-wait-s", type=float, default=7.0)
    value.add_argument("--recognition-observe-s", type=float, default=17.0)
    value.add_argument("--seed", type=int, default=20260722)
    return value


if __name__ == "__main__":
    raise SystemExit(run_stress(parser().parse_args()))
