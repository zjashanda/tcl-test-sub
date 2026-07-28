"""TCL wake-only interval stress test with continuous COM20/COM22 evidence."""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
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
DEFAULT_AUDIO_DIR = ROOT / "audio" / "generated"
DEFAULT_LOG_ROOT = Path.cwd() / "result"
DEVICE_KEY = "VID_8765&PID_5678:9_387C0FC7_0_0000"
WAKE_TEXT = "小T小T"
BAUDRATE = 921600
FINAL_WAKE = re.compile(r"(?<!Pre )Wakeup:\s*\{.*?\"keyword\":\"xiao ti xiao ti\"", re.I)
CRITICAL_PATTERNS = {
    "panic": re.compile(r"\bpanic\b", re.I),
    "crash": re.compile(r"\bcrash\b", re.I),
    "reboot": re.compile(r"\breboot\b|system reset|hardware reset|reset reason", re.I),
    "assert_failed": re.compile(r"assert failed|assertion failed", re.I),
    "exception": re.compile(r"\bexception\b|guru meditation|fatal error", re.I),
    "watchdog_reset": re.compile(r"watchdog.{0,20}(reset|reboot)|wdt.{0,20}(reset|reboot)", re.I),
}
WARNING_PATTERNS = {
    "wdt_not_feed": re.compile(r"wdt not feed", re.I),
    "watchdog": re.compile(r"\bwatchdog\b", re.I),
    "url_context_null": re.compile(r"url context is null", re.I),
    "player_malloc_probe_fail": re.compile(r"malloc probe buf fail", re.I),
    "soundplayer_status_0xef": re.compile(r"soundplayer status:\s*0XEF", re.I),
    "player_event_error": re.compile(r"PLAYER_EVENT_ERROR", re.I),
}


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def task_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def parse_stop_at(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise SystemExit(f"invalid --stop-at value: {value!r}; use YYYY-MM-DDTHH:MM:SS") from error


def wait_until(deadline: float, stop_at: datetime) -> bool:
    while time.monotonic() < deadline:
        if datetime.now() >= stop_at:
            return False
        time.sleep(min(0.5, max(0.01, deadline - time.monotonic())))
    return datetime.now() < stop_at


class SerialLogger:
    def __init__(self, run_dir: Path, ports: dict[str, str], timeline: ToolTimeline) -> None:
        self.run_dir = run_dir
        self.ports = ports
        self.timeline = timeline
        self.logs_dir = run_dir / "serial_logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.errors: dict[str, str] = {}
        self.events: list[dict[str, str]] = []
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
                        self.append(port, role, line)
                if partial.strip():
                    self.append(port, role, partial)
        except Exception as error:
            self.errors[port] = str(error)
            self.timeline.emit("SERIAL_PORT_ERROR", level="ERROR", port=port, role=role, error=str(error))

    def append(self, port: str, role: str, line: str) -> None:
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


def run_audio(text: str, audio_dir: Path, tool_log: Path) -> dict[str, object]:
    audio_file = audio_dir / f"{text}.mp3"
    started_at = now_text()
    if not audio_file.is_file() or audio_file.stat().st_size == 0:
        payload: dict[str, object] = {
            "text": text,
            "audio_file": str(audio_file),
            "started_at": started_at,
            "ended_at": now_text(),
            "returncode": 2,
            "error": "missing_audio",
        }
    else:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        try:
            completed = subprocess.run(
                [
                    "python",
                    str(PLAY_SCRIPT),
                    "play",
                    "--audio-file",
                    str(audio_file),
                    "--device-key",
                    DEVICE_KEY,
                    "--skip-probe",
                ],
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
                "ended_at": now_text(),
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        except subprocess.TimeoutExpired as error:
            payload = {
                "text": text,
                "audio_file": str(audio_file),
                "started_at": started_at,
                "ended_at": now_text(),
                "returncode": 124,
                "error": f"playback_timeout: {error}",
            }
    with tool_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return payload


def probe_audio_device() -> None:
    completed = subprocess.run(
        ["python", str(PLAY_SCRIPT), "probe", "--device-key", DEVICE_KEY],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "audio device probe failed").strip()
        raise SystemExit(details)


def wake_detected(events: list[dict[str, str]], csk_port: str) -> bool:
    return any(event["port"] == csk_port and FINAL_WAKE.search(event["line"]) for event in events)


def wake_timestamp(events: list[dict[str, str]], csk_port: str) -> str:
    return next(
        (event["timestamp"] for event in events if event["port"] == csk_port and FINAL_WAKE.search(event["line"])),
        "",
    )


def matching_lines(events: list[dict[str, str]], patterns: dict[str, re.Pattern[str]]) -> dict[str, list[str]]:
    return {
        name: [event["line"] for event in events if pattern.search(event["line"])]
        for name, pattern in patterns.items()
    }


def write_summary(
    run_dir: Path,
    rows: list[dict[str, object]],
    started_at: str,
    ended_at: str,
    logger: SerialLogger,
    timeline: ToolTimeline,
    args: argparse.Namespace,
) -> None:
    status_counts = Counter(str(row["status"]) for row in rows)
    critical_counts = Counter(marker for row in rows for marker in str(row["critical_markers"]).split(" | ") if marker)
    warning_counts = Counter(marker for row in rows for marker in str(row["warning_markers"]).split(" | ") if marker)
    actual_intervals: dict[str, list[float]] = {"2.000": [], "20.000": []}
    for row in rows:
        target = str(row["interval_s"])
        actual = str(row.get("actual_interval_s") or "")
        if target in actual_intervals and actual:
            actual_intervals[target].append(float(actual))
    interval_stats = {
        target: {
            "count": len(values),
            "minimum_seconds": round(min(values), 3),
            "mean_seconds": round(statistics.mean(values), 3),
            "median_seconds": round(statistics.median(values), 3),
            "maximum_seconds": round(max(values), 3),
        }
        for target, values in actual_intervals.items()
        if values
    }
    summary = {
        "started_at": started_at,
        "ended_at": ended_at,
        "stop_at": args.stop_at.isoformat(sep=" "),
        "completed_rounds": len(rows),
        "interval_pattern_seconds": [2, 20],
        "wake_text": WAKE_TEXT,
        "device_key": DEVICE_KEY,
        "ports": {args.csk_port: BAUDRATE, args.upper_port: BAUDRATE},
        "status_counts": dict(status_counts),
        "critical_marker_round_counts": dict(critical_counts),
        "warning_marker_round_counts": dict(warning_counts),
        "serial_errors": logger.errors,
        "wake_success_rate": round(sum(bool(row["wake_detected"]) for row in rows) / len(rows), 6) if rows else 0,
        "actual_interval_statistics": interval_stats,
        "task_log": "task.log",
        "serial_marker_counts": timeline.marker_counts(),
        "serial_failure_marker_counts": timeline.failure_counts(),
    }
    (run_dir / "summary_final.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# TCL 场景1+4 普通话唤醒交替间隔压测结果",
        "",
        f"- 开始：{started_at}",
        f"- 自动停止时间：{args.stop_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 实际结束：{ended_at}",
        f"- 唤醒词：`{WAKE_TEXT}`",
        "- 间隔模式：2 秒、20 秒交替，以相邻唤醒播放开始时间为口径。",
        f"- 完成轮次：{len(rows)}",
        f"- 唤醒识别成功率：{summary['wake_success_rate']:.2%}",
        f"- 串口：`{args.csk_port}@{BAUDRATE}`、`{args.upper_port}@{BAUDRATE}`",
        "",
        "## 状态统计",
        "",
        "| 状态 | 数量 |",
        "| --- | ---: |",
    ]
    for name, count in sorted(status_counts.items()):
        lines.append(f"| {name} | {count} |")
    if interval_stats:
        lines.extend(["", "## 实际时序", "", "| 目标间隔 | 样本 | 最小值 | 平均值 | 中位数 | 最大值 |", "| ---: | ---: | ---: | ---: | ---: | ---: |"])
        for target, stats in interval_stats.items():
            lines.append(
                f"| {float(target):.0f} 秒 | {stats['count']} | {stats['minimum_seconds']:.3f} 秒 | "
                f"{stats['mean_seconds']:.3f} 秒 | {stats['median_seconds']:.3f} 秒 | {stats['maximum_seconds']:.3f} 秒 |"
            )
    lines.extend(
        [
            "",
            "## 异常统计",
            "",
            f"- 串口错误：{json.dumps(logger.errors, ensure_ascii=False)}",
            f"- 严重 marker 轮次：{json.dumps(dict(critical_counts), ensure_ascii=False)}",
            f"- 告警 marker 轮次：{json.dumps(dict(warning_counts), ensure_ascii=False)}",
            "",
            "## 证据文件",
            "",
            "- `serial_logs/COM20_csk_ap.log`",
            "- `serial_logs/COM22_upper.log`",
            "- `serial_logs/merged.log`",
            "- `task.log`（逐轮工具执行时序、动作结果、异常摘要和串口游标）",
            "- `round_evidence/`",
            "- `rounds.csv`",
        ]
    )
    (run_dir / "summary_final.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_stress(args: argparse.Namespace) -> int:
    if args.stop_at <= datetime.now():
        raise SystemExit(f"stop time has already passed: {args.stop_at}")
    probe_audio_device()
    run_dir = args.logs_root / f"{task_stamp()}_wake_interval_stress"
    (run_dir / "tool_logs").mkdir(parents=True, exist_ok=False)
    stop_file = run_dir / "STOP"
    progress_file = run_dir / "progress.json"
    rounds_file = run_dir / "rounds.csv"
    tool_log = run_dir / "tool_logs" / "playback.jsonl"
    timeline = ToolTimeline(run_dir / "task.log")
    (run_dir / "task_config.json").write_text(
        json.dumps(
            {
                "started_at": now_text(),
                "stop_at": args.stop_at.isoformat(sep=" "),
                "device_key": DEVICE_KEY,
                "wake_text": WAKE_TEXT,
                "interval_pattern_seconds": [2, 20],
                "interval_measurement": "between playback start times",
                "csk_port": args.csk_port,
                "upper_port": args.upper_port,
                "baudrate": BAUDRATE,
                "audio_dir": str(args.audio_dir),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger = SerialLogger(run_dir, {args.csk_port: "csk_ap", args.upper_port: "upper"}, timeline)
    timeline.emit(
        "TASK_CONFIG",
        task_name="TCL 普通话唤醒交替间隔压测",
        task_type="wake_interval_round",
        run_dir=str(run_dir),
        stop_at=args.stop_at.isoformat(sep=" "),
        interval_pattern_s=[2, 20],
        wake_text=WAKE_TEXT,
        wake_wait_s=args.wake_wait_s,
        csk_port=args.csk_port,
        upper_port=args.upper_port,
        baudrate=BAUDRATE,
    )
    timeline.set_context(stop_at=args.stop_at.isoformat(sep=" "))
    logger.start()
    time.sleep(2)
    started_at = now_text()
    timeline.emit("TASK_START", at=started_at, run_dir=str(run_dir), stop_at=args.stop_at.isoformat(sep=" "))
    rows: list[dict[str, object]] = []
    current = {"phase": "starting", "round": 0, "next_interval_s": 0}
    fields = [
        "round", "started_at", "ended_at", "interval_s", "actual_interval_s", "late_by_s", "wake_playback_rc", "status",
        "wake_detected", "critical_markers", "warning_markers", "serial_errors", "evidence_start", "evidence_end",
    ]

    def write_progress(finished: bool = False) -> None:
        progress_file.write_text(
            json.dumps(
                {
                    "updated_at": now_text(),
                    "started_at": started_at,
                    "stop_at": args.stop_at.isoformat(sep=" "),
                    "finished": finished,
                    "phase": current["phase"],
                    "current_round": current["round"],
                    "next_interval_s": current["next_interval_s"],
                    "completed_rounds": len(rows),
                    "status_counts": dict(Counter(str(row["status"]) for row in rows)),
                    "serial_errors": logger.errors,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    task_error = ""
    try:
        with rounds_file.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            next_start = time.monotonic()
            previous_start: float | None = None
            round_number = 0
            while datetime.now() < args.stop_at and not stop_file.exists():
                interval = 0.0 if round_number == 0 else (2.0 if round_number % 2 == 1 else 20.0)
                current.update({"phase": "interval_wait", "round": round_number + 1, "next_interval_s": interval})
                active_round = round_number if round_number > 0 else 1
                next_round = round_number + 1
                timeline.set_context(
                    round=active_round,
                    next_round=next_round if round_number > 0 else None,
                    phase=current["phase"],
                )
                write_progress()
                if round_number > 0:
                    wait_remaining = max(0.0, next_start - time.monotonic())
                    timeline.emit(
                        "INTERVAL_WAIT_START",
                        source_round=round_number,
                        next_round=next_round,
                        target_interval_s=interval,
                        remaining_wait_s=round(wait_remaining, 3),
                    )
                    wait_started = time.monotonic()
                    if not wait_until(next_start, args.stop_at):
                        timeline.emit(
                            "INTERVAL_WAIT_INTERRUPTED",
                            level="WARN",
                            elapsed_s=round(time.monotonic() - wait_started, 3),
                            reason="stop_time_reached",
                        )
                        break
                    timeline.emit("INTERVAL_WAIT_END", elapsed_s=round(time.monotonic() - wait_started, 3))
                round_number += 1
                scheduled_start = next_start
                case_started_mono = time.monotonic()
                actual_interval = case_started_mono - previous_start if previous_start is not None else None
                previous_start = case_started_mono
                late_by = max(0.0, case_started_mono - scheduled_start) if round_number > 1 else 0.0
                case_started = now_text()
                start_cursor = logger.cursor()
                current.update({"phase": "wake_playback", "round": round_number, "next_interval_s": interval})
                timeline.set_context(round=round_number, next_round=None, phase=current["phase"])
                write_progress()
                timeline.emit(
                    "ROUND_START",
                    current_round=round_number,
                    target_interval_s=interval,
                    actual_interval_s=round(actual_interval, 3) if actual_interval is not None else None,
                    late_by_s=round(late_by, 3),
                    evidence_start=start_cursor,
                )
                timeline.emit("WAKE_PLAYBACK_START", text=WAKE_TEXT, evidence_cursor=start_cursor)
                wake = run_audio(WAKE_TEXT, args.audio_dir, tool_log)
                timeline.emit(
                    "WAKE_PLAYBACK_END",
                    level="INFO" if wake["returncode"] == 0 else "ERROR",
                    text=WAKE_TEXT,
                    audio_rc=wake["returncode"],
                    elapsed_s=elapsed_seconds(wake),
                    evidence_cursor=logger.cursor(),
                )
                next_start = case_started_mono + (2.0 if round_number % 2 == 1 else 20.0)
                observe_deadline = min(next_start, time.monotonic() + args.wake_wait_s)
                current["phase"] = "wake_observe"
                timeline.set_context(round=round_number, phase=current["phase"])
                write_progress()
                timeline.emit(
                    "WAKE_DETECTION_WAIT_START",
                    timeout_s=round(max(0.0, observe_deadline - time.monotonic()), 3),
                    evidence_cursor=start_cursor,
                )
                while time.monotonic() < observe_deadline and not wake_detected(logger.since(start_cursor), args.csk_port):
                    time.sleep(0.05)
                events = logger.since(start_cursor)
                critical = matching_lines(events, CRITICAL_PATTERNS)
                warnings = matching_lines(events, WARNING_PATTERNS)
                critical_names = [name for name, lines in critical.items() if lines]
                warning_names = [name for name, lines in warnings.items() if lines]
                detected = wake_detected(events, args.csk_port)
                timeline.emit(
                    "WAKE_RESULT",
                    level="INFO" if detected and wake["returncode"] == 0 else "ERROR",
                    status="RECOGNIZED" if detected else "NOT_RECOGNIZED",
                    audio_rc=wake["returncode"],
                    detected_at=wake_timestamp(events, args.csk_port),
                    evidence_cursor=logger.cursor(),
                )
                if wake["returncode"] != 0:
                    status = "AUDIO_FAIL"
                elif critical_names:
                    status = "CRITICAL_FIRMWARE_MARKER"
                elif "player_malloc_probe_fail" in warning_names or "player_event_error" in warning_names:
                    status = "PLAYER_FAIL"
                elif detected:
                    status = "WAKE_RECOGNIZED"
                else:
                    status = "WAKE_FAIL"
                evidence_path = run_dir / "round_evidence" / f"round_{round_number:05d}.log"
                evidence_path.parent.mkdir(exist_ok=True)
                evidence_path.write_text(
                    "\n".join(f"{item['timestamp']} [{item['port']}/{item['role']}] {item['line']}" for item in events) + "\n",
                    encoding="utf-8",
                )
                player_counts = {
                    "player_prepared": sum("PLAYER_EVENT_PREPARED" in event["line"] for event in events),
                    "player_playing": sum("PLAYER_EVENT_PLAYING" in event["line"] for event in events),
                    "player_complete": sum("PLAYER_EVENT_PLAYBACK_COMPLETE" in event["line"] for event in events),
                    "player_error": sum("PLAYER_EVENT_ERROR" in event["line"] for event in events),
                    "malloc_probe_fail": sum("malloc probe buf fail" in event["line"] for event in events),
                }
                timeline.emit(
                    "ROUND_EVIDENCE_SUMMARY",
                    level="ERROR" if critical_names or player_counts["player_error"] or player_counts["malloc_probe_fail"] else "WARN" if not detected else "INFO",
                    status=status,
                    critical=critical_names or [],
                    warnings=warning_names or [],
                    evidence_file=str(evidence_path.relative_to(run_dir)),
                    evidence_start=start_cursor,
                    evidence_end=logger.cursor(),
                    **player_counts,
                )
                row: dict[str, object] = {
                    "round": round_number,
                    "started_at": case_started,
                    "ended_at": now_text(),
                    "interval_s": f"{interval:.3f}",
                    "actual_interval_s": f"{actual_interval:.3f}" if actual_interval is not None else "",
                    "late_by_s": f"{late_by:.3f}",
                    "wake_playback_rc": wake["returncode"],
                    "status": status,
                    "wake_detected": detected,
                    "critical_markers": " | ".join(critical_names),
                    "warning_markers": " | ".join(warning_names),
                    "serial_errors": json.dumps(logger.errors, ensure_ascii=False),
                    "evidence_start": start_cursor,
                    "evidence_end": logger.cursor(),
                }
                writer.writerow(row)
                handle.flush()
                rows.append(row)
                current["phase"] = "round_complete"
                timeline.set_context(round=round_number, phase=current["phase"])
                write_progress()
                timeline.emit(
                    "ROUND_END",
                    level="CRITICAL" if critical_names else "ERROR" if status in {"AUDIO_FAIL", "WAKE_FAIL", "PLAYER_FAIL"} else "INFO",
                    current_round=round_number,
                    status=status,
                    wake_detected=detected,
                    critical=critical_names or [],
                    warnings=warning_names or [],
                    evidence_start=start_cursor,
                    evidence_end=logger.cursor(),
                )
        if stop_file.exists():
            timeline.emit("STOP_REQUESTED", level="WARN", stop_file=str(stop_file), evidence_cursor=logger.cursor())
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
        write_summary(run_dir, rows, started_at, ended_at, logger, timeline, args)
        write_progress(finished=True)
        failed_rounds = sum(
            1 for row in rows if str(row["status"]) != "WAKE_RECOGNIZED"
        )
        outcome = (
            "FAILED"
            if task_error or logger.errors or timeline.failure_counts() or failed_rounds
            else "STOPPED"
            if stop_file.exists()
            else "COMPLETED"
        )
        timeline.set_context(round=None, next_round=None, command=None, next_command=None, phase="finished")
        timeline.emit(
            "TASK_END",
            at=ended_at,
            run_dir=str(run_dir),
            completed_rounds=len(rows),
            status_counts=dict(Counter(str(row["status"]) for row in rows)),
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
    parser = argparse.ArgumentParser(description="TCL alternating 2s/20s wake-only stress")
    parser.add_argument("--audio-dir", type=Path, default=DEFAULT_AUDIO_DIR)
    parser.add_argument("--logs-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--csk-port", default="COM20")
    parser.add_argument("--upper-port", default="COM22")
    parser.add_argument("--stop-at", type=parse_stop_at, required=True)
    parser.add_argument("--wake-wait-s", type=float, default=1.0)
    return parser


if __name__ == "__main__":
    raise SystemExit(run_stress(build_parser().parse_args()))
