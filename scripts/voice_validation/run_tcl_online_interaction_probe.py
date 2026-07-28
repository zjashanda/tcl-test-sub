"""Run a short TCL online-interaction probe with dual-serial evidence."""

from __future__ import annotations

import argparse
import json
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
DEVICE_KEY = "VID_8765&PID_5678:9_387C0FC7_0_0000"
BAUDRATE = 921600
WAKE_TEXT = "小T小T"
CASES = [
    ("今天天气怎么样", 22.0, "weather"),
    ("给我讲个笑话", 25.0, "joke"),
    ("播放一首音乐", 20.0, "music"),
    ("停止播放", 8.0, "cleanup"),
]
WAKE_PATTERN = re.compile(r'(?<!Pre )Wakeup:\s*\{.*?"keyword":"xiao ti xiao ti"', re.I)
CRITICAL_PATTERNS = {
    "panic": re.compile(r"\bpanic\b", re.I),
    "crash": re.compile(r"\bcrash\b", re.I),
    "reboot_or_reset": re.compile(r"\breboot\b|system reset|hardware reset|reset reason", re.I),
    "assert_failed": re.compile(r"assert failed|assertion failed", re.I),
    "watchdog_reset": re.compile(r"watchdog.{0,20}(reset|reboot)|wdt.{0,20}(reset|reboot)", re.I),
    "exception": re.compile(r"\bexception\b|guru meditation|fatal error", re.I),
}
MARKERS = {
    "online_asr_start": re.compile(r"start online asr service, state: ASR_STATE_WAKEUP", re.I),
    "cloud_asr_start": re.compile(r"asr status: ASR_STATUS_START", re.I),
    "cloud_reply_200": re.compile(r"\bcode:\s*200\b", re.I),
    "recognized_text": re.compile(r'get asr:.*?"text"|"text"\s*:', re.I),
    "nlp_or_tts": re.compile(r"reply_type:\s*(?:NLP|TTS)|\bget\s+(?:nlp|tts)\b", re.I),
    "player_prepared": re.compile(r"PLAYER_EVENT_PREPARED", re.I),
    "player_playing": re.compile(r"PLAYER_EVENT_PLAYING", re.I),
    "player_complete": re.compile(r"PLAYER_EVENT_PLAYBACK_COMPLETE", re.I),
    "url_channel": re.compile(r"url_channel:\s*1", re.I),
    "player_error": re.compile(r"PLAYER_EVENT_ERROR|PLAYER_AGENT_MSG_PLAY_ERROR", re.I),
    "malloc_probe_fail": re.compile(r"malloc probe buf fail", re.I),
    "soundplayer_0xef": re.compile(r"soundplayer status:\s*0XEF", re.I),
    "wdt_not_feed": re.compile(r"wdt not feed", re.I),
    "network_down": re.compile(r"websocket state:\s*0|wifi state:\s*0|mqtt state:\s*0", re.I),
    "cloud_reply_1007": re.compile(r"\bcode:\s*1007\b", re.I),
    "asr_data_null": re.compile(r"asr data is NULL|no asr data", re.I),
}


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def task_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


class SerialLogger:
    def __init__(self, run_dir: Path, ports: dict[str, str], timeline: ToolTimeline) -> None:
        self.logs_dir = run_dir / "serial_logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.ports = ports
        self.timeline = timeline
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.events: list[dict[str, str]] = []
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
            thread = threading.Thread(target=self._read_port, args=(port, role), daemon=True)
            thread.start()
            self.threads.append(thread)

    def _read_port(self, port: str, role: str) -> None:
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
                        self._append(port, role, line)
                if partial.strip():
                    self._append(port, role, partial)
        except Exception as error:
            self.errors[port] = str(error)
            self.timeline.emit("SERIAL_PORT_ERROR", level="ERROR", port=port, role=role, error=str(error))

    def _append(self, port: str, role: str, line: str) -> None:
        if not line:
            return
        event = {"timestamp": now_text(), "port": port, "role": role, "line": line}
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


def play_audio(audio_file: Path, playback_log: Path, probe: bool = False) -> dict[str, object]:
    command = [
        "python",
        str(PLAY_SCRIPT),
        "play",
        "--audio-file",
        str(audio_file),
        "--device-key",
        DEVICE_KEY,
    ]
    if not probe:
        command.append("--skip-probe")
    started_at = now_text()
    try:
        completed = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=30,
        )
        payload: dict[str, object] = {
            "audio_file": str(audio_file),
            "started_at": started_at,
            "ended_at": now_text(),
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as error:
        payload = {
            "audio_file": str(audio_file),
            "started_at": started_at,
            "ended_at": now_text(),
            "returncode": 124,
            "error": f"playback_timeout: {error}",
        }
    with playback_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return payload


def count_markers(events: list[dict[str, str]]) -> dict[str, int]:
    return {
        name: sum(bool(pattern.search(event["line"])) for event in events)
        for name, pattern in MARKERS.items()
    }


def matching_lines(events: list[dict[str, str]], pattern: re.Pattern[str], limit: int = 30) -> list[str]:
    return [event["line"] for event in events if pattern.search(event["line"])][:limit]


def run_probe(args: argparse.Namespace) -> int:
    run_dir = args.logs_root / f"{task_stamp()}_online_interaction_probe"
    tool_dir = run_dir / "tool_logs"
    evidence_dir = run_dir / "case_evidence"
    tool_dir.mkdir(parents=True, exist_ok=False)
    evidence_dir.mkdir()
    playback_log = tool_dir / "playback.jsonl"
    timeline = ToolTimeline(run_dir / "task.log")
    (run_dir / "task_config.json").write_text(
        json.dumps(
            {
                "started_at": now_text(),
                "device_key": DEVICE_KEY,
                "ports": {args.csk_port: BAUDRATE, args.upper_port: BAUDRATE},
                "wake_audio": str(args.wake_audio),
                "command_audio_dir": str(args.command_audio_dir),
                "cases": [item[0] for item in CASES],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger = SerialLogger(run_dir, {args.csk_port: "csk_ap", args.upper_port: "upper"}, timeline)
    timeline.emit(
        "TASK_CONFIG",
        task_name="TCL 在线交互功能验证",
        task_type="online_case",
        run_dir=str(run_dir),
        total_cases=len(CASES),
        cases=[item[0] for item in CASES],
        wake_command_delay_s=args.wake_command_delay_s,
        case_gap_s=args.case_gap_s,
        csk_port=args.csk_port,
        upper_port=args.upper_port,
        baudrate=BAUDRATE,
    )
    timeline.set_context(total_cases=len(CASES))
    logger.start()
    time.sleep(2)
    timeline.emit("TASK_START", run_dir=str(run_dir), total_cases=len(CASES))
    results: list[dict[str, object]] = []
    task_error = ""
    try:
        for index, (text, observe_seconds, category) in enumerate(CASES, start=1):
            start_cursor = logger.cursor()
            started_at = now_text()
            timeline.set_context(case=index, phase="case_start", command=text)
            timeline.emit(
                "CASE_START",
                at=started_at,
                current_case=index,
                total_cases=len(CASES),
                category=category,
                command=text,
                observe_s=observe_seconds,
                evidence_start=start_cursor,
            )
            timeline.set_context(case=index, phase="wake_playback", command=text)
            timeline.emit("WAKE_PLAYBACK_START", text=WAKE_TEXT, evidence_cursor=start_cursor)
            wake_playback = play_audio(args.wake_audio, playback_log, probe=index == 1)
            timeline.emit(
                "WAKE_PLAYBACK_END",
                level="INFO" if wake_playback["returncode"] == 0 else "ERROR",
                text=WAKE_TEXT,
                audio_rc=wake_playback["returncode"],
                elapsed_s=elapsed_seconds(wake_playback),
                evidence_cursor=logger.cursor(),
            )
            wake_deadline = time.monotonic() + 8
            timeline.set_context(case=index, phase="wake_detection_wait", command=text)
            timeline.emit("WAKE_DETECTION_WAIT_START", timeout_s=8, evidence_cursor=start_cursor)
            while time.monotonic() < wake_deadline:
                if any(event["port"] == args.csk_port and WAKE_PATTERN.search(event["line"]) for event in logger.since(start_cursor)):
                    break
                time.sleep(0.1)
            wake_events = logger.since(start_cursor)
            wake_ok = any(event["port"] == args.csk_port and WAKE_PATTERN.search(event["line"]) for event in wake_events)
            wake_detected_at = next(
                (
                    event["timestamp"]
                    for event in wake_events
                    if event["port"] == args.csk_port and WAKE_PATTERN.search(event["line"])
                ),
                "",
            )
            timeline.emit(
                "WAKE_RESULT",
                level="INFO" if wake_ok and wake_playback["returncode"] == 0 else "ERROR",
                status="RECOGNIZED" if wake_ok else "NOT_RECOGNIZED",
                audio_rc=wake_playback["returncode"],
                detected_at=wake_detected_at,
                evidence_cursor=logger.cursor(),
            )
            timeline.set_context(case=index, phase="wake_to_command_wait", command=text)
            delay_started = time.monotonic()
            timeline.emit("COMMAND_DELAY_START", wait_s=args.wake_command_delay_s)
            time.sleep(args.wake_command_delay_s)
            timeline.emit("COMMAND_DELAY_END", elapsed_s=round(time.monotonic() - delay_started, 3))
            command_file = args.command_audio_dir / f"{text}.mp3"
            command_cursor = logger.cursor()
            timeline.set_context(case=index, phase="command_playback", command=text)
            timeline.emit("COMMAND_PLAYBACK_START", command=text, audio_file=str(command_file), evidence_cursor=command_cursor)
            command_playback = play_audio(command_file, playback_log)
            timeline.emit(
                "COMMAND_PLAYBACK_END",
                level="INFO" if command_playback["returncode"] == 0 else "ERROR",
                command=text,
                audio_rc=command_playback["returncode"],
                elapsed_s=elapsed_seconds(command_playback),
                evidence_cursor=logger.cursor(),
            )
            timeline.set_context(case=index, phase="response_observe", command=text)
            observe_started = time.monotonic()
            timeline.emit("RESPONSE_OBSERVE_START", command=text, timeout_s=observe_seconds, evidence_cursor=command_cursor)
            time.sleep(observe_seconds)
            timeline.emit("RESPONSE_OBSERVE_END", elapsed_s=round(time.monotonic() - observe_started, 3), evidence_cursor=logger.cursor())
            events = logger.since(start_cursor)
            markers = count_markers(events)
            critical = [
                name
                for name, pattern in CRITICAL_PATTERNS.items()
                if any(pattern.search(event["line"]) for event in events)
            ]
            wake_ok = any(event["port"] == args.csk_port and WAKE_PATTERN.search(event["line"]) for event in events)
            online_ok = markers["online_asr_start"] > 0 and markers["cloud_asr_start"] > 0
            response_ok = markers["recognized_text"] > 0 or markers["nlp_or_tts"] > 0
            player_ok = markers["player_playing"] > 0
            status = "PASS"
            if wake_playback["returncode"] or command_playback["returncode"]:
                status = "AUDIO_FAIL"
            elif critical:
                status = "CRITICAL"
            elif not wake_ok:
                status = "WAKE_FAIL"
            elif not online_ok:
                status = "ONLINE_ASR_FAIL"
            elif markers["cloud_reply_1007"] or markers["asr_data_null"]:
                status = "ONLINE_ASR_ERROR"
            elif not response_ok:
                status = "NO_ONLINE_RESPONSE"
            elif not player_ok or markers["player_error"] or markers["malloc_probe_fail"]:
                status = "PLAYER_FAIL"
            evidence_file = evidence_dir / f"{index:02d}_{category}.log"
            evidence_file.write_text(
                "\n".join(f"{event['timestamp']} [{event['port']}/{event['role']}] {event['line']}" for event in events) + "\n",
                encoding="utf-8",
            )
            status_level = "INFO" if status == "PASS" else "CRITICAL" if status == "CRITICAL" else "ERROR"
            timeline.emit(
                "INTERACTION_RESULT",
                level=status_level,
                command=text,
                status=status,
                wake_detected=wake_ok,
                online_asr_started=online_ok,
                response_detected=response_ok,
                player_playing=player_ok,
                player_complete=markers["player_complete"],
                player_error=markers["player_error"],
                malloc_probe_fail=markers["malloc_probe_fail"],
                cloud_reply_1007=markers["cloud_reply_1007"],
                asr_data_null=markers["asr_data_null"],
                critical=critical or [],
                evidence_cursor=logger.cursor(),
            )
            result: dict[str, object] = {
                "case": index,
                "category": category,
                "text": text,
                "started_at": started_at,
                "ended_at": now_text(),
                "status": status,
                "wake_playback_rc": wake_playback["returncode"],
                "command_playback_rc": command_playback["returncode"],
                "wake_detected": wake_ok,
                "online_asr_started": online_ok,
                "online_response_detected": response_ok,
                "player_playing_detected": player_ok,
                "markers": markers,
                "critical_markers": critical,
                "recognized_text_lines": matching_lines(events, MARKERS["recognized_text"]),
                "player_error_lines": matching_lines(events, MARKERS["player_error"]),
                "malloc_error_lines": matching_lines(events, MARKERS["malloc_probe_fail"]),
                "evidence_file": str(evidence_file),
            }
            results.append(result)
            timeline.set_context(case=index, phase="case_complete", command=text)
            timeline.emit(
                "EVIDENCE_SAVED",
                evidence_file=str(evidence_file.relative_to(run_dir)),
                evidence_start=start_cursor,
                evidence_end=logger.cursor(),
            )
            timeline.emit(
                "CASE_END",
                level=status_level,
                current_case=index,
                total_cases=len(CASES),
                command=text,
                status=status,
                evidence_start=start_cursor,
                evidence_end=logger.cursor(),
            )
            if index < len(CASES):
                timeline.set_context(case=index, phase="next_case_wait", command=text)
                gap_started = time.monotonic()
                timeline.emit("NEXT_CASE_DELAY_START", wait_s=args.case_gap_s)
                time.sleep(args.case_gap_s)
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
        totals = Counter(str(result["status"]) for result in results)
        ended_at = now_text()
        failed_cases = sum(count for status, count in totals.items() if status != "PASS")
        outcome = "FAILED" if task_error or logger.errors or timeline.failure_counts() or failed_cases else "COMPLETED"
        summary = {
            "ended_at": ended_at,
            "results": results,
            "status_counts": dict(totals),
            "serial_errors": logger.errors,
            "task_log": "task.log",
            "task_events": "task_events.jsonl",
            "errors_log": "errors.log",
            "outcome": outcome,
            "error": task_error,
            "serial_marker_counts": timeline.marker_counts(),
            "serial_failure_marker_counts": timeline.failure_counts(),
        }
        (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        timeline.set_context(case=None, command=None, phase="finished")
        timeline.emit(
            "TASK_END",
            at=ended_at,
            run_dir=str(run_dir),
            completed_cases=len(results),
            total_cases=len(CASES),
            status_counts=dict(totals),
            outcome=outcome,
            error=task_error,
            serial_errors=logger.errors,
            serial_marker_counts=timeline.marker_counts(),
            serial_failure_marker_counts=timeline.failure_counts(),
            final_cursor=logger.cursor(),
        )
        timeline.close()
    return 0 if outcome == "COMPLETED" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TCL online interaction and player probe")
    parser.add_argument("--logs-root", type=Path, default=Path.cwd() / "result")
    parser.add_argument("--wake-audio", type=Path, default=ROOT / "audio" / "generated" / "小T小T.mp3")
    parser.add_argument("--command-audio-dir", type=Path, default=ROOT / "audio" / "online_interaction")
    parser.add_argument("--csk-port", default="COM20")
    parser.add_argument("--upper-port", default="COM22")
    parser.add_argument("--wake-command-delay-s", type=float, default=0.8)
    parser.add_argument("--case-gap-s", type=float, default=4.0)
    return parser


if __name__ == "__main__":
    raise SystemExit(run_probe(build_parser().parse_args()))
