"""TCL core-command offline recognition sweep with continuous dual-serial evidence.

The script uses local audio playback and serial logging only. All timestamps use
the PC's local Beijing clock in YYYY-MM-DD HH:mm:ss.SSS format without a TZ suffix.
"""

from __future__ import annotations

import argparse
import csv
import json
import queue
import re
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import openpyxl
import serial

from tool_timeline import ToolTimeline, elapsed_seconds


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKBOOK = ROOT / "TCL双麦空调离在线自由说需求文档_20260701.xlsx"
DEFAULT_AUDIO_DIR = ROOT / "audio" / "generated"
DEFAULT_COMMAND_CSV = ROOT / "testdata" / "lexicon" / "complete_core_commands.csv"
PLAY_SCRIPT = Path.home() / ".codex" / "skills" / "listenai-play" / "scripts" / "listenai_play.py"
DEVICE_KEY = "VID_8765&PID_5678:9_387C0FC7_0_0000"
WAKE_TEXT = "小T小T"
BAUDRATE = 921600
WAKE_PATTERN = re.compile(r"\bWakeup:\s*\{.*?\bkeyword\":\"xiao ti xiao ti\"", re.I)
MODE_RECOGNITION_EXIT = re.compile(r"MODE\s+1\s*->\s*0", re.I)
ASR_TIMEOUT = re.compile(r"\besr timeout\b", re.I)
RECOGNITION_MARKER = re.compile(r"offline[_ ]?asr|asr result|recognition|intent|keyword|command|离线结果|识别结果", re.I)
FAULT_MARKER = re.compile(
    r"\bpanic\b|\bcrash\b|\breboot\b|system reset|hardware reset|reset reason|"
    r"assert failed|assertion failed|\bexception\b|guru meditation|fatal error|"
    r"watchdog.{0,20}(reset|reboot)|wdt.{0,20}(reset|reboot)",
    re.I,
)
PLAYER_FAULT_MARKER = re.compile(r"malloc probe buf fail|PLAYER_EVENT_ERROR|PLAYER_AGENT_MSG_PLAY_ERROR", re.I)
WAKE_JSON = re.compile(r'Wakeup:\s*\{.*?"keyword":"([^"]+)".*?"intent":"([^"]+)"', re.I)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def task_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def normalize(text: str) -> str:
    return re.sub(r"[\s_\-，,。.:：;；\"'`()\[\]{}]", "", str(text or "")).lower()


def safe_csv(value: Any) -> str:
    return str(value or "").replace("\r", "").replace("\n", " ")


class SerialEvidence:
    def __init__(self, port_specs: dict[str, str], run_dir: Path, timeline: ToolTimeline) -> None:
        self.port_specs = port_specs
        self.run_dir = run_dir
        self.timeline = timeline
        self.logs_dir = run_dir / "serial_logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.merged_log = (self.logs_dir / "merged.log").open("a", encoding="utf-8", newline="")
        self.port_logs = {
            port: (self.logs_dir / f"{port}_{role}.log").open("a", encoding="utf-8", newline="")
            for port, role in port_specs.items()
        }
        self.events: list[dict[str, str]] = []
        self.events_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.errors: dict[str, str] = {}
        self.threads: list[threading.Thread] = []

    def note(self, message: str) -> None:
        self.timeline.emit("TOOL_NOTE", message=message)

    def start(self) -> None:
        self.timeline.emit("SERIAL_LOGGER_START", ports=self.port_specs, baudrate=BAUDRATE)
        for port, role in self.port_specs.items():
            thread = threading.Thread(target=self._read_port, args=(port, role), daemon=True)
            thread.start()
            self.threads.append(thread)
        self.note(f"serial logger started: {self.port_specs}, baudrate={BAUDRATE}")

    def _read_port(self, port: str, role: str) -> None:
        partial = ""
        try:
            with serial.Serial(port, BAUDRATE, timeout=0.2) as handle:
                self.timeline.emit("SERIAL_PORT_OPEN", port=port, role=role, baudrate=BAUDRATE)
                while not self.stop_event.is_set():
                    chunk = handle.read(handle.in_waiting or 1)
                    if not chunk:
                        continue
                    text = chunk.decode("utf-8", errors="replace").replace("\r", "")
                    partial += text
                    while "\n" in partial:
                        line, partial = partial.split("\n", 1)
                        self._append(port, role, line)
                if partial.strip():
                    self._append(port, role, partial)
        except Exception as error:
            self.errors[port] = str(error)
            self.timeline.emit("SERIAL_PORT_ERROR", level="ERROR", port=port, role=role, error=str(error))

    def _append(self, port: str, role: str, line: str) -> None:
        if not line:
            return
        item = {"timestamp": now_text(), "port": port, "role": role, "line": line}
        rendered = f"{item['timestamp']} [{port}/{role}] {line}\n"
        with self.events_lock:
            self.events.append(item)
            cursor = len(self.events)
            self.port_logs[port].write(rendered)
            self.port_logs[port].flush()
            self.merged_log.write(rendered)
            self.merged_log.flush()
        self.timeline.serial_marker(item["timestamp"], port, role, line, cursor)

    def cursor(self) -> int:
        with self.events_lock:
            return len(self.events)

    def since(self, cursor: int) -> list[dict[str, str]]:
        with self.events_lock:
            return list(self.events[cursor:])

    def close(self) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=2)
        for handle in self.port_logs.values():
            handle.close()
        self.merged_log.close()
        self.timeline.emit("SERIAL_LOGGER_STOP", serial_errors=self.errors, final_cursor=self.cursor())


def build_mapping(workbook_path: Path) -> list[dict[str, Any]]:
    workbook = openpyxl.load_workbook(workbook_path, data_only=True, read_only=True)
    core_sheet = workbook["TCL 高频词"]
    intent_sheet = workbook["TCL意图表（对比公版自由说）"]

    semantic_by_phrase: dict[str, list[str]] = defaultdict(list)
    for row in range(2, intent_sheet.max_row + 1):
        phrase = str(intent_sheet.cell(row, 2).value or "").strip()
        semantic = str(intent_sheet.cell(row, 3).value or "").strip()
        if phrase and semantic and semantic not in semantic_by_phrase[phrase]:
            semantic_by_phrase[phrase].append(semantic)

    by_command: dict[str, dict[str, Any]] = {}
    current_intent = ""
    for row in range(3, core_sheet.max_row + 1):
        intent_value = str(core_sheet.cell(row, 2).value or "").strip()
        if intent_value:
            current_intent = intent_value
        for column in (3, 4):
            command = str(core_sheet.cell(row, column).value or "").strip()
            if not command or command in {"定时N小时", "定时N小时开机", "定时N小时关机", "N度"} or command.isdigit():
                continue
            entry = by_command.setdefault(
                command,
                {"command": command, "intents": [], "source_rows": [], "expected_semantics": []},
            )
            if current_intent and current_intent not in entry["intents"]:
                entry["intents"].append(current_intent)
            if row not in entry["source_rows"]:
                entry["source_rows"].append(row)
            for semantic in semantic_by_phrase.get(current_intent, []):
                if semantic not in entry["expected_semantics"]:
                    entry["expected_semantics"].append(semantic)

    return list(by_command.values())


def build_mapping_from_csv(command_csv: Path) -> list[dict[str, Any]]:
    with command_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    entries = []
    for row in rows:
        command = str(row.get("text") or "").strip()
        if not command:
            continue
        entries.append(
            {
                "command": command,
                "intents": [item.strip() for item in str(row.get("expected_intents") or "").split("|") if item.strip()],
                "expected_semantics": [item.strip() for item in str(row.get("standard_semantics") or "").split("|") if item.strip()],
                "source_rows": [item.strip() for item in str(row.get("source_cells") or "").split("|") if item.strip()],
            }
        )
    return entries


def play_audio(text: str, audio_dir: Path, run_dir: Path, skip_probe: bool = True) -> dict[str, Any]:
    audio_file = audio_dir / f"{text}.mp3"
    started_at = now_text()
    if not audio_file.is_file() or audio_file.stat().st_size == 0:
        payload = {"text": text, "audio_file": str(audio_file), "started_at": started_at, "finished_at": now_text(), "returncode": 2, "error": "audio file missing or empty"}
    else:
        command = [
            "python",
            str(PLAY_SCRIPT),
            "play",
            "--audio-file",
            str(audio_file),
            "--device-key",
            DEVICE_KEY,
        ]
        if skip_probe:
            command.append("--skip-probe")
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        try:
            completed = subprocess.run(
                command,
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
                "finished_at": now_text(),
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        except subprocess.TimeoutExpired as error:
            payload = {
                "text": text,
                "audio_file": str(audio_file),
                "started_at": started_at,
                "finished_at": now_text(),
                "returncode": 124,
                "error": f"playback_timeout: {error}",
            }
    with (run_dir / "tool_logs" / "playback.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return payload


def has_wake(events: list[dict[str, str]], csk_port: str) -> bool:
    return any(event["port"] == csk_port and WAKE_PATTERN.search(event["line"]) for event in events)


def exited_recognition(events: list[dict[str, str]]) -> bool:
    return any(
        ASR_TIMEOUT.search(event["line"]) or MODE_RECOGNITION_EXIT.search(event["line"])
        for event in events
    )


def recognition_lines(events: list[dict[str, str]], csk_port: str) -> list[dict[str, str]]:
    return [
        event
        for event in events
        if event["port"] == csk_port and RECOGNITION_MARKER.search(event["line"]) and not WAKE_PATTERN.search(event["line"])
    ]


def intent_matches(entry: dict[str, Any], events: list[dict[str, str]], csk_port: str) -> tuple[bool, list[str]]:
    expected = [entry["command"], *entry["intents"], *entry["expected_semantics"]]
    expected_norm = [normalize(item) for item in expected if normalize(item)]
    matched = []
    for event in recognition_lines(events, csk_port):
        line_norm = normalize(event["line"])
        if any(token in line_norm for token in expected_norm):
            matched.append(event["line"])
    return bool(matched), matched


def recognized_command(events: list[dict[str, str]], csk_port: str) -> tuple[str, str, str]:
    matches = []
    for event in events:
        if event["port"] != csk_port:
            continue
        match = WAKE_JSON.search(event["line"])
        if match and match.group(1) != "xiao ti xiao ti":
            matches.append((match.group(1), match.group(2), event["timestamp"]))
    return matches[-1] if matches else ("", "", "")


def write_mapping(path: Path, entries: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["command", "expected_intents", "expected_semantics", "source_rows"])
        writer.writeheader()
        for entry in entries:
            writer.writerow(
                {
                    "command": entry["command"],
                    "expected_intents": " | ".join(entry["intents"]),
                    "expected_semantics": " | ".join(entry["expected_semantics"]),
                    "source_rows": ",".join(map(str, entry["source_rows"])),
                }
            )


def write_summary(run_dir: Path, results: list[dict[str, Any]], started_at: str, ended_at: str, logger: SerialEvidence) -> None:
    counts: dict[str, int] = defaultdict(int)
    for result in results:
        counts[result["status"]] += 1
    lines = [
        "# TCL 核心命令词离线识别批测结果",
        "",
        f"- 任务开始：{started_at}",
        f"- 任务结束：{ended_at}",
        "- 时间口径：北京时间，不显示时区。",
        f"- 指定声卡：`{DEVICE_KEY}`",
        "- CSK 主日志：`COM20@921600`",
        "- 上位机辅助日志：`COM22@921600`",
        f"- 测试命令数：{len(results)}",
        "- 会话策略：仅在识别模式退出或超时后重新播放唤醒词。",
        "- 工具执行时序：`task.log`。",
        "",
        "## 结果统计",
        "",
        "| 状态 | 数量 |",
        "| --- | ---: |",
    ]
    for status in (
        "PASS",
        "WAKE_FAIL",
        "ASR_TIMEOUT",
        "INTENT_MISMATCH",
        "NO_ASR",
        "BLOCKED_AUDIO",
        "CRITICAL_FIRMWARE_MARKER",
        "PLAYER_FAIL",
        "ABORTED",
    ):
        lines.append(f"| {status} | {counts[status]} |")
    if logger.errors:
        lines.extend(["", "## 串口错误", "", "```text", json.dumps(logger.errors, ensure_ascii=False, indent=2), "```"])
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_sweep(args: argparse.Namespace) -> int:
    entries = build_mapping_from_csv(args.command_csv) if args.command_csv else build_mapping(args.workbook)
    if args.commands_file:
        requested = [line.strip() for line in args.commands_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        requested_set = set(requested)
        entries = [entry for entry in entries if entry["command"] in requested_set]
        missing = [command for command in requested if command not in {entry["command"] for entry in entries}]
        if missing:
            raise SystemExit(f"commands missing from workbook core mapping: {missing}")
    if args.max_cases:
        entries = entries[: args.max_cases]
    run_dir = args.logs_root / f"{task_stamp()}_core_command_sweep"
    (run_dir / "tool_logs").mkdir(parents=True, exist_ok=False)
    timeline = ToolTimeline(run_dir / "task.log")
    write_mapping(run_dir / "command_intent_mapping.csv", entries)
    (run_dir / "task_config.json").write_text(
        json.dumps(
            {
                "created_at": now_text(), "workbook": str(args.workbook), "audio_dir": str(args.audio_dir),
                "device_key": DEVICE_KEY, "csk_port": args.csk_port, "upper_port": args.upper_port,
                "baudrate": BAUDRATE, "wake_text": WAKE_TEXT, "wake_to_command_gap_s": args.wake_to_command_gap_s,
                "max_observe_s": args.max_observe_s, "command_count": len(entries),
            }, ensure_ascii=False, indent=2,
        ), encoding="utf-8",
    )

    logger = SerialEvidence({args.csk_port: "csk_ap", args.upper_port: "upper"}, run_dir, timeline)
    timeline.emit(
        "TASK_CONFIG",
        task_name="TCL 核心命令词离线识别批测",
        task_type="core_command_case",
        run_dir=str(run_dir),
        total_cases=len(entries),
        wake_to_command_gap_s=args.wake_to_command_gap_s,
        wake_wait_s=args.wake_wait_s,
        recognition_observe_s=args.max_observe_s,
        next_case_gap_s=args.next_case_gap_s,
        csk_port=args.csk_port,
        upper_port=args.upper_port,
        baudrate=BAUDRATE,
    )
    timeline.set_context(total_cases=len(entries))
    logger.start()
    time.sleep(2)
    started_at = now_text()
    timeline.emit("TASK_START", at=started_at, run_dir=str(run_dir), total_cases=len(entries))
    results: list[dict[str, Any]] = []
    recognition_active = False
    results_path = run_dir / "results.csv"
    fieldnames = [
        "case", "started_at", "ended_at", "command", "expected_intents", "expected_semantics", "source_rows",
        "status", "wake_playback_rc", "command_playback_rc", "actual_recognition_lines", "fault_lines", "evidence_start", "evidence_end",
    ]
    stop_file = run_dir / "STOP"
    task_error = ""
    try:
        with results_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for index, entry in enumerate(entries, start=1):
                if stop_file.exists():
                    timeline.set_context(case=index, phase="stop_requested")
                    timeline.emit("STOP_REQUESTED", level="WARN", stop_file=str(stop_file), evidence_cursor=logger.cursor())
                    break
                case_start = now_text()
                start_cursor = logger.cursor()
                timeline.set_context(case=index, phase="case_start", command=entry["command"])
                timeline.emit(
                    "CASE_START",
                    current_case=index,
                    total_cases=len(entries),
                    command=entry["command"],
                    expected_intents=entry["intents"],
                    recognition_session_active=recognition_active,
                    evidence_start=start_cursor,
                )
                wake = {"returncode": "", "reused_recognition_mode": recognition_active}
                command_playback = {"returncode": ""}
                status = ""

                if not recognition_active:
                    timeline.set_context(case=index, phase="wake_playback", command=entry["command"])
                    timeline.emit("WAKE_PLAYBACK_START", text=WAKE_TEXT, evidence_cursor=start_cursor)
                    wake = play_audio(WAKE_TEXT, args.audio_dir, run_dir)
                    timeline.emit(
                        "WAKE_PLAYBACK_END",
                        level="INFO" if wake["returncode"] == 0 else "ERROR",
                        text=WAKE_TEXT,
                        audio_rc=wake["returncode"],
                        elapsed_s=elapsed_seconds(wake),
                        evidence_cursor=logger.cursor(),
                    )
                    if wake["returncode"] != 0:
                        status = "BLOCKED_AUDIO"
                    else:
                        wake_deadline = time.monotonic() + args.wake_wait_s
                        timeline.set_context(case=index, phase="wake_detection_wait", command=entry["command"])
                        timeline.emit("WAKE_DETECTION_WAIT_START", timeout_s=args.wake_wait_s)
                        while time.monotonic() < wake_deadline and not has_wake(logger.since(start_cursor), args.csk_port):
                            time.sleep(0.1)
                        if not has_wake(logger.since(start_cursor), args.csk_port):
                            status = "WAKE_FAIL"
                            timeline.emit(
                                "WAKE_RESULT",
                                level="ERROR",
                                status="NOT_RECOGNIZED",
                                audio_rc=wake["returncode"],
                                evidence_cursor=logger.cursor(),
                            )
                        else:
                            recognition_active = True
                            timeline.emit(
                                "WAKE_RESULT",
                                status="RECOGNIZED",
                                audio_rc=wake["returncode"],
                                evidence_cursor=logger.cursor(),
                            )
                            timeline.set_context(case=index, phase="wake_to_command_wait", command=entry["command"])
                            delay_started = time.monotonic()
                            timeline.emit("COMMAND_DELAY_START", wait_s=args.wake_to_command_gap_s)
                            time.sleep(args.wake_to_command_gap_s)
                            timeline.emit("COMMAND_DELAY_END", elapsed_s=round(time.monotonic() - delay_started, 3))
                else:
                    timeline.emit(
                        "WAKE_REUSED",
                        recognition_session_active=True,
                        reason="previous_case_kept_recognition_mode",
                        evidence_cursor=start_cursor,
                    )

                if recognition_active and not status:
                    command_cursor = logger.cursor()
                    timeline.set_context(case=index, phase="command_playback", command=entry["command"])
                    timeline.emit(
                        "COMMAND_PLAYBACK_START",
                        command=entry["command"],
                        expected_intents=entry["intents"],
                        evidence_cursor=command_cursor,
                    )
                    command_playback = play_audio(entry["command"], args.audio_dir, run_dir)
                    timeline.emit(
                        "COMMAND_PLAYBACK_END",
                        level="INFO" if command_playback["returncode"] == 0 else "ERROR",
                        command=entry["command"],
                        audio_rc=command_playback["returncode"],
                        elapsed_s=elapsed_seconds(command_playback),
                        evidence_cursor=logger.cursor(),
                    )
                    if command_playback["returncode"] != 0:
                        status = "BLOCKED_AUDIO"
                    else:
                        deadline = time.monotonic() + args.max_observe_s
                        timeline.set_context(case=index, phase="recognition_wait", command=entry["command"])
                        timeline.emit(
                            "RECOGNITION_WAIT_START",
                            command=entry["command"],
                            timeout_s=args.max_observe_s,
                            evidence_cursor=command_cursor,
                        )
                        while time.monotonic() < deadline:
                            command_events = logger.since(command_cursor)
                            # A result may arrive while the device stays in recognition mode.
                            if recognition_lines(command_events, args.csk_port) or exited_recognition(command_events):
                                time.sleep(0.5)
                                break
                            time.sleep(0.1)
                        end_cursor = logger.cursor()
                        command_events = logger.since(command_cursor)
                        matched, _ = intent_matches(entry, command_events, args.csk_port)
                        rec_lines = recognition_lines(command_events, args.csk_port)
                        timed_out = any(ASR_TIMEOUT.search(event["line"]) for event in command_events)
                        if matched:
                            status = "PASS"
                        elif timed_out:
                            status = "ASR_TIMEOUT"
                        elif rec_lines:
                            status = "INTENT_MISMATCH"
                        else:
                            status = "NO_ASR"
                        recognition_active = not exited_recognition(command_events)
                        actual_keyword, actual_intent, detected_at = recognized_command(command_events, args.csk_port)
                        timeline.emit(
                            "RECOGNITION_RESULT",
                            level="INFO" if status == "PASS" else "WARN",
                            command=entry["command"],
                            status=status,
                            actual_keyword=actual_keyword or "-",
                            actual_intent=actual_intent or "-",
                            detected_at=detected_at,
                            recognition_lines=[safe_csv(item["line"]) for item in rec_lines],
                            asr_timeout=timed_out,
                            recognition_session_active=recognition_active,
                            evidence_cursor=logger.cursor(),
                        )
                elif status:
                    timeline.emit(
                        "COMMAND_SKIPPED",
                        level="ERROR",
                        command=entry["command"],
                        reason=status,
                    )

                end_cursor = logger.cursor()

                events = logger.since(start_cursor)
                evidence_file = run_dir / "case_evidence" / f"{index:03d}_{entry['command']}.log"
                evidence_file.parent.mkdir(exist_ok=True)
                evidence_file.write_text(
                    "\n".join(f"{event['timestamp']} [{event['port']}/{event['role']}] {event['line']}" for event in events) + "\n",
                    encoding="utf-8",
                )
                rec_lines = recognition_lines(events, args.csk_port)
                fault_lines = [event["line"] for event in events if FAULT_MARKER.search(event["line"])]
                player_fault_lines = [event["line"] for event in events if PLAYER_FAULT_MARKER.search(event["line"])]
                if fault_lines and status != "CRITICAL_FIRMWARE_MARKER":
                    previous_status = status
                    status = "CRITICAL_FIRMWARE_MARKER"
                    timeline.emit(
                        "CASE_STATUS_OVERRIDE",
                        level="CRITICAL",
                        previous_status=previous_status,
                        status=status,
                        fault_count=len(fault_lines),
                        evidence_cursor=logger.cursor(),
                    )
                elif player_fault_lines and status != "PLAYER_FAIL":
                    previous_status = status
                    status = "PLAYER_FAIL"
                    timeline.emit(
                        "CASE_STATUS_OVERRIDE",
                        level="ERROR",
                        previous_status=previous_status,
                        status=status,
                        player_fault_count=len(player_fault_lines),
                        evidence_cursor=logger.cursor(),
                    )
                result = {
                    "case": index,
                    "started_at": case_start,
                    "ended_at": now_text(),
                    "command": entry["command"],
                    "expected_intents": " | ".join(entry["intents"]),
                    "expected_semantics": " | ".join(entry["expected_semantics"]),
                    "source_rows": ",".join(map(str, entry["source_rows"])),
                    "status": status,
                    "wake_playback_rc": wake["returncode"],
                    "command_playback_rc": command_playback.get("returncode", ""),
                    "actual_recognition_lines": " || ".join(safe_csv(item["line"]) for item in rec_lines),
                    "fault_lines": " || ".join(safe_csv(item) for item in [*fault_lines, *player_fault_lines]),
                    "evidence_start": start_cursor,
                    "evidence_end": end_cursor,
                }
                writer.writerow(result)
                handle.flush()
                results.append(result)
                (run_dir / "progress.json").write_text(
                    json.dumps({"updated_at": now_text(), "completed": len(results), "total": len(entries), "last_result": result, "serial_errors": logger.errors}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                timeline.set_context(case=index, phase="case_complete", command=entry["command"])
                timeline.emit(
                    "EVIDENCE_SAVED",
                    evidence_file=str(evidence_file.relative_to(run_dir)),
                    evidence_start=start_cursor,
                    evidence_end=end_cursor,
                )
                timeline.emit(
                    "CASE_END",
                    level=(
                        "CRITICAL"
                        if status == "CRITICAL_FIRMWARE_MARKER"
                        else "ERROR"
                        if status == "PLAYER_FAIL"
                        else "INFO"
                        if status == "PASS"
                        else "WARN"
                    ),
                    current_case=index,
                    total_cases=len(entries),
                    command=entry["command"],
                    status=status,
                    recognition_session_active=recognition_active,
                    fault_count=len(fault_lines),
                    player_fault_count=len(player_fault_lines),
                    evidence_start=start_cursor,
                    evidence_end=end_cursor,
                )
                if index < len(entries):
                    gap_started = time.monotonic()
                    timeline.set_context(case=index, phase="next_case_wait", command=entry["command"])
                    timeline.emit("NEXT_CASE_DELAY_START", wait_s=args.next_case_gap_s)
                    time.sleep(args.next_case_gap_s)
                    timeline.emit("NEXT_CASE_DELAY_END", elapsed_s=round(time.monotonic() - gap_started, 3))
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
        ended_at = now_text()
        write_summary(run_dir, results, started_at, ended_at, logger)
        (run_dir / "progress.json").write_text(
            json.dumps({"updated_at": ended_at, "completed": len(results), "total": len(entries), "finished": True, "serial_errors": logger.errors}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        counts = defaultdict(int)
        for result in results:
            counts[result["status"]] += 1
        failed_cases = sum(count for status, count in counts.items() if status != "PASS")
        outcome = (
            "FAILED"
            if task_error or logger.errors or timeline.failure_counts() or failed_cases
            else "STOPPED"
            if stop_file.exists()
            else "COMPLETED"
        )
        timeline.set_context(case=None, command=None, phase="finished")
        timeline.emit(
            "TASK_END",
            at=ended_at,
            run_dir=str(run_dir),
            completed_cases=len(results),
            total_cases=len(entries),
            status_counts=dict(counts),
            outcome=outcome,
            error=task_error,
            serial_errors=logger.errors,
            serial_marker_counts=timeline.marker_counts(),
            serial_failure_marker_counts=timeline.failure_counts(),
            final_cursor=logger.cursor(),
        )
        timeline.close()
    return 0 if outcome == "COMPLETED" else 3 if outcome == "STOPPED" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TCL core command sweep with continuous COM20/COM22 evidence")
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--audio-dir", type=Path, default=DEFAULT_AUDIO_DIR)
    parser.add_argument("--logs-root", type=Path, default=Path.cwd() / "result")
    parser.add_argument("--csk-port", default="COM20")
    parser.add_argument("--upper-port", default="COM22")
    parser.add_argument("--wake-wait-s", type=float, default=7.0)
    parser.add_argument("--wake-to-command-gap-s", type=float, default=1.6)
    parser.add_argument("--max-observe-s", type=float, default=17.0)
    parser.add_argument("--next-case-gap-s", type=float, default=2.0)
    parser.add_argument("--max-cases", type=int, default=0, help="use only for a controlled precheck")
    parser.add_argument("--commands-file", type=Path, default=None, help="run only the listed core commands, one command per line")
    parser.add_argument("--command-csv", type=Path, default=DEFAULT_COMMAND_CSV, help="use a traceable command CSV such as complete_core_commands.csv")
    return parser


if __name__ == "__main__":
    raise SystemExit(run_sweep(build_parser().parse_args()))
