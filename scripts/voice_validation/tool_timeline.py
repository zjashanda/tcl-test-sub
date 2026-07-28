"""Thread-safe human and machine timelines for voice validation tools."""

from __future__ import annotations

import json
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


FAILURE_MARKERS = {
    "player_malloc_probe_fail",
    "player_event_error",
    "cloud_asr_error_1007",
    "asr_data_null",
    "assert_failed",
    "watchdog_reset",
    "system_reset",
    "panic",
    "fatal_error",
    "exception",
    "crash",
    "reboot",
}

PASS_STATUSES = {"PASS", "RECOGNIZED", "WAKE_RECOGNIZED"}
WARNING_STATUSES = {"WARN", "WARNING", "SKIPPED", "STOPPED", "ABORTED"}

STATUS_DESCRIPTIONS = {
    "PASS": "测试通过",
    "RECOGNIZED": "命令识别成功",
    "WAKE_RECOGNIZED": "设备唤醒成功",
    "AUDIO_FAIL": "电脑端音频播放失败",
    "BLOCKED_AUDIO": "音频播放失败，当前测试受阻",
    "WAKE_FAIL": "播放唤醒词后设备未成功唤醒",
    "ASR_TIMEOUT": "设备识别超时",
    "NO_RECOGNITION": "未获得有效命令识别结果",
    "NO_ASR": "未获得有效 ASR 结果",
    "INTENT_MISMATCH": "识别结果与预期意图不一致",
    "PLAYER_FAIL": "设备播放器发生异常",
    "CRITICAL_FIRMWARE_MARKER": "检测到固件严重异常",
    "CRITICAL": "检测到严重异常",
    "ONLINE_ASR_FAIL": "在线 ASR 服务未正常启动",
    "ONLINE_ASR_ERROR": "在线 ASR 返回错误",
    "NO_ONLINE_RESPONSE": "未获得有效在线回复",
    "ABORTED": "当前测试被中止",
    "BOUNDARY_ASR_TIMEOUT_EXPECTED": "边界用例按预期触发 ASR 超时",
    "BOUNDARY_NO_RECOGNITION_EXPECTED": "边界用例按预期未获得命令识别结果",
    "BOUNDARY_RECOGNIZED": "边界用例仍成功识别",
}

MARKER_DESCRIPTIONS = {
    "player_malloc_probe_fail": "设备播放器内存申请失败（malloc probe buf fail）",
    "player_event_error": "设备播放器报告播放错误（PLAYER_EVENT_ERROR）",
    "cloud_asr_error_1007": "在线 ASR 返回 1007 错误",
    "asr_data_null": "在线 ASR 数据为空",
    "assert_failed": "固件触发断言失败",
    "watchdog_reset": "设备发生看门狗复位",
    "system_reset": "设备发生系统或硬件复位",
    "panic": "固件发生 panic",
    "fatal_error": "固件报告 fatal error",
    "exception": "固件报告 exception",
    "crash": "固件发生 crash",
    "reboot": "检测到设备重启",
    "network_down": "设备网络连接断开",
    "wdt_not_feed": "检测到 wdt not feed",
    "player_url_context_null": "播放器 URL 上下文为空",
}

ROLE_NAMES = {
    "csk_ap": "CSK AP",
    "upper": "上位机",
}

SEPARATOR = "=" * 78
SUB_SEPARATOR = "-" * 78


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def elapsed_seconds(payload: dict[str, Any]) -> float | None:
    started = payload.get("started_at")
    ended = payload.get("ended_at") or payload.get("finished_at")
    if not started or not ended:
        return None
    try:
        return round((datetime.fromisoformat(str(ended)) - datetime.fromisoformat(str(started))).total_seconds(), 3)
    except ValueError:
        return None


class ToolTimeline:
    def __init__(self, path: Path, echo: bool = True, write_machine_events: bool = True) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.events_path = path.with_name("task_events.jsonl")
        self.errors_path = path.with_name("errors.log")
        self.echo = echo
        self.lock = threading.Lock()
        self.context: dict[str, Any] = {}
        self.serial_marker_counts: dict[str, int] = {}
        self.handle = path.open("w", encoding="utf-8", newline="")
        self.events_handle = (
            self.events_path.open("w", encoding="utf-8", newline="")
            if write_machine_events
            else None
        )
        self.errors_handle = self.errors_path.open("w", encoding="utf-8", newline="")
        self.task_name = "TCL 语音压测"
        self.task_type = ""
        self.task_started_at = ""
        self.target_total = 0
        self.duration_minutes = 0.0
        self.duration_limit_reached = False
        self.unit_starts: dict[tuple[str, int], str] = {}
        self.unit_evidence: dict[tuple[str, int], dict[str, Any]] = {}
        self.rendered_headers: set[tuple[str, int]] = set()
        self.result_counts: Counter[str] = Counter()
        self.alert_counts: Counter[str] = Counter()
        self.alert_first: dict[str, dict[str, Any]] = {}
        self.alert_last: dict[str, dict[str, Any]] = {}
        self.warning_units = 0
        self.boundary_units: set[tuple[str, int]] = set()
        self.first_failure: dict[str, Any] | None = None

    def set_context(self, **fields: Any) -> None:
        with self.lock:
            self.context.update(fields)

    def emit(
        self,
        event: str,
        *,
        level: str = "INFO",
        at: str | None = None,
        echo: bool | None = None,
        **fields: Any,
    ) -> None:
        logged_at = now_text()
        event_at = at or logged_at
        with self.lock:
            values = {**self.context, **fields}
            clean_values = {
                name: value
                for name, value in values.items()
                if value is not None and value != ""
            }
            machine_event = {
                "logged_at": logged_at,
                "event_at": event_at,
                "level": level,
                "event": event,
                **clean_values,
            }
            if self.events_handle is not None:
                self.events_handle.write(json.dumps(machine_event, ensure_ascii=False, separators=(",", ":")) + "\n")
                self.events_handle.flush()

            human_lines = self._render_human(event, level, event_at, values)
            for line in human_lines:
                self.handle.write(line + "\n")
            self.handle.flush()

            error_lines = self._render_error(event, level, event_at, values)
            for line in error_lines:
                self.errors_handle.write(line + "\n")
            if error_lines:
                self.errors_handle.flush()
        should_echo = self.echo if echo is None else echo
        if should_echo and human_lines:
            print("\n".join(human_lines), flush=True)

    def serial_marker(self, timestamp: str, port: str, role: str, line: str, cursor: int) -> None:
        classified = classify_serial_line(line)
        if classified is None:
            return
        level, marker = classified
        with self.lock:
            self.serial_marker_counts[marker] = self.serial_marker_counts.get(marker, 0) + 1
        self.emit(
            "SERIAL_ALERT" if level in {"WARN", "ERROR", "CRITICAL"} else "SERIAL_EVENT",
            level=level,
            at=timestamp,
            echo=level in {"WARN", "ERROR", "CRITICAL"},
            marker=marker,
            port=port,
            role=role,
            serial_cursor=cursor,
        )

    def marker_counts(self) -> dict[str, int]:
        with self.lock:
            return dict(self.serial_marker_counts)

    def failure_counts(self) -> dict[str, int]:
        with self.lock:
            return {
                marker: count
                for marker, count in self.serial_marker_counts.items()
                if marker in FAILURE_MARKERS and count > 0
            }

    def close(self) -> None:
        with self.lock:
            if not self.handle.closed:
                self.handle.flush()
                self.handle.close()
            if self.events_handle is not None and not self.events_handle.closed:
                self.events_handle.flush()
                self.events_handle.close()
            if not self.errors_handle.closed:
                self.errors_handle.flush()
                self.errors_handle.close()

    def _render_human(self, event: str, level: str, at: str, values: dict[str, Any]) -> list[str]:
        clock = self._clock(at)

        if event == "TASK_CONFIG":
            self.task_type = str(values.get("task_type") or self._infer_task_type(values))
            self.task_name = str(values.get("task_name") or self._default_task_name(self.task_type))
            self.target_total = int(values.get("target_rounds") or values.get("total_cases") or 0)
            self.duration_minutes = float(values.get("duration_minutes") or 0)
            return self._render_config(at, values)

        if event == "SERIAL_LOGGER_START":
            return [self._line(clock, "[准备] 开始连续采集设备串口日志。")]

        if event == "SERIAL_PORT_OPEN":
            role = ROLE_NAMES.get(str(values.get("role")), str(values.get("role") or "设备"))
            return [
                self._line(
                    clock,
                    f"[准备] {role} 串口 {values.get('port')} 打开成功，波特率 {values.get('baudrate')}。",
                )
            ]

        if event == "SERIAL_PORT_ERROR":
            role = ROLE_NAMES.get(str(values.get("role")), str(values.get("role") or "设备"))
            return [self._line(clock, f"[异常] {role} 串口 {values.get('port')} 打开或读取失败：{values.get('error')}")]

        if event == "AUDIO_DEVICE_PROBE_START":
            return [self._line(clock, "[准备] 检查指定 USB 音频播放设备。")]

        if event == "AUDIO_DEVICE_PROBE_END":
            if values.get("success"):
                return [self._line(clock, "[准备] USB 音频播放设备检查通过，可以正常播放。")]
            return [self._line(clock, f"[异常] USB 音频播放设备检查失败：{values.get('details') or '未知错误'}。")]

        if event == "AUDIO_ASSET_CHECK":
            missing = int(values.get("missing_count") or 0)
            if missing:
                examples = self._display_value(values.get("missing_examples") or [])
                return [
                    self._line(
                        clock,
                        f"[异常] 音频资源检查失败：缺少 {missing} 个文件，示例：{examples}。",
                    )
                ]
            return [
                self._line(
                    clock,
                    f"[准备] 音频资源检查通过，共检查 {values.get('checked_count', 0)} 个文件。",
                )
            ]

        if event == "TASK_START":
            self.task_started_at = at
            return ["", SEPARATOR, self._line(clock, "测试正式开始。"), SEPARATOR]

        if event == "ROUND_PLAN":
            number = int(values.get("planned_round") or values.get("round") or 0)
            key = ("round", number)
            self.rendered_headers.add(key)
            command = str(values.get("command") or values.get("next_command") or "")
            total = int(values.get("target_rounds") or self.target_total)
            lines = ["", SEPARATOR, self._unit_title("round", number, total, command)]
            if values.get("pre_interval_s") is not None:
                lines.append(f"轮前等待：{self._seconds(values.get('pre_interval_s'))}")
            expected = values.get("expected_intents")
            if expected:
                lines.append(f"预期意图：{expected}")
            lines.append(SUB_SEPARATOR)
            return lines

        if event == "ROUND_START":
            number = int(values.get("current_round") or values.get("round") or 0)
            key = ("round", number)
            self.unit_starts[key] = at
            if key in self.rendered_headers:
                return []
            self.rendered_headers.add(key)
            total = int(values.get("target_rounds") or self.target_total)
            command = str(values.get("command") or "")
            lines = ["", SEPARATOR, self._unit_title("round", number, total, command)]
            if values.get("target_interval_s") is not None:
                lines.append(f"目标唤醒间隔：{self._seconds(values.get('target_interval_s'))}")
            if values.get("actual_interval_s") is not None:
                lines.append(f"实际唤醒间隔：{self._seconds(values.get('actual_interval_s'))}")
            lines.append(SUB_SEPARATOR)
            return lines

        if event == "CASE_START":
            number = int(values.get("current_case") or values.get("case") or 0)
            key = ("case", number)
            self.unit_starts[key] = at
            self.rendered_headers.add(key)
            total = int(values.get("total_cases") or self.target_total)
            command = str(values.get("command") or "")
            lines = ["", SEPARATOR, self._unit_title("case", number, total, command)]
            if values.get("category"):
                lines.append(f"用例类型：{values.get('category')}")
            if values.get("expected_intents"):
                lines.append(f"预期意图：{self._display_value(values.get('expected_intents'))}")
            lines.append(SUB_SEPARATOR)
            return lines

        if event == "INTERVAL_WAIT_START":
            seconds = values.get("wait_s")
            if seconds is None:
                seconds = values.get("remaining_wait_s")
            if seconds is None or float(seconds) <= 0:
                return []
            return [self._line(clock, f"轮前等待 {self._seconds(seconds)}。")]

        if event == "INTERVAL_WAIT_END":
            if float(values.get("elapsed_s") or 0) <= 0.01:
                return []
            return [self._line(clock, f"轮前等待完成，实际耗时 {self._seconds(values.get('elapsed_s'))}。")]

        if event == "INTERVAL_WAIT_INTERRUPTED":
            return [self._line(clock, f"[警告] 轮前等待被中止，已等待 {self._seconds(values.get('elapsed_s'))}。")]

        if event == "WAKE_PLAYBACK_START":
            return [self._line(clock, f"开始播放唤醒词“{values.get('text') or '小T小T'}”。")]

        if event == "WAKE_PLAYBACK_END":
            ok = int(values.get("audio_rc") or 0) == 0
            result = "正常" if ok else f"失败，返回码 {values.get('audio_rc')}"
            prefix = "" if ok else "[异常] "
            return [
                self._line(
                    clock,
                    f"{prefix}唤醒词播放完成，电脑端播放{result}，耗时 {self._seconds(values.get('elapsed_s'))}。",
                )
            ]

        if event == "WAKE_DETECTION_WAIT_START":
            return [self._line(clock, f"等待设备唤醒结果，最长等待 {self._seconds(values.get('timeout_s'))}。")]

        if event == "WAKE_RESULT":
            recognized = str(values.get("status")) == "RECOGNIZED"
            if recognized:
                detected = f"，设备响应时间 {values.get('detected_at')}" if values.get("detected_at") else ""
                return [self._line(clock, f"设备唤醒成功{detected}。")]
            return [self._line(clock, "[异常] 设备未成功唤醒。")]

        if event == "COMMAND_DELAY_START":
            key = self._current_unit_key(values)
            if key and values.get("boundary_case"):
                self.boundary_units.add(key)
            boundary = "，本轮为识别超时边界用例" if values.get("boundary_case") else ""
            return [self._line(clock, f"唤醒后等待 {self._seconds(values.get('wait_s'))} 再播放命令{boundary}。")]

        if event == "COMMAND_DELAY_END":
            return [self._line(clock, f"唤醒后等待完成，实际耗时 {self._seconds(values.get('elapsed_s'))}。")]

        if event == "COMMAND_DELAY_INTERRUPTED":
            return [self._line(clock, f"[警告] 唤醒后等待被中止，已等待 {self._seconds(values.get('elapsed_s'))}。")]

        if event == "COMMAND_PLAYBACK_START":
            return [self._line(clock, f"开始播放命令“{values.get('command')}”。")]

        if event == "COMMAND_PLAYBACK_END":
            ok = int(values.get("audio_rc") or 0) == 0
            result = "正常" if ok else f"失败，返回码 {values.get('audio_rc')}"
            prefix = "" if ok else "[异常] "
            return [
                self._line(
                    clock,
                    f"{prefix}命令“{values.get('command')}”播放完成，电脑端播放{result}，耗时 {self._seconds(values.get('elapsed_s'))}。",
                )
            ]

        if event == "COMMAND_SKIPPED":
            return [self._line(clock, f"[异常] 跳过命令“{values.get('command')}”，原因：{self._status_description(values.get('reason'))}。")]

        if event == "WAKE_REUSED":
            return [self._line(clock, "沿用上一条命令的有效识别会话，本条用例不重复播放唤醒词。")]

        if event == "RECOGNITION_WAIT_START":
            return [self._line(clock, f"等待命令识别结果，最长等待 {self._seconds(values.get('timeout_s'))}。")]

        if event == "RECOGNITION_RESULT":
            status = self._effective_status(
                self._current_unit_key(values),
                str(values.get("status") or ""),
            )
            description = self._status_description(status)
            prefix = "" if self._status_kind(status) in {"pass", "expected"} else "[异常] "
            lines = [self._line(clock, f"{prefix}{description}。")]
            keyword = values.get("actual_keyword")
            intent = values.get("actual_intent")
            if keyword and keyword != "-":
                lines.append(f"                 keyword：{keyword}")
            if intent and intent != "-":
                lines.append(f"                 intent ：{intent}")
            if values.get("critical") not in {None, "", "-"}:
                lines.append(f"                 严重异常：{values.get('critical')}")
            if values.get("player_failures") not in {None, "", "-"}:
                lines.append(f"                 播放器异常：{values.get('player_failures')}")
            return lines

        if event == "RESPONSE_OBSERVE_START":
            return [self._line(clock, f"等待在线交互回复，最长观察 {self._seconds(values.get('timeout_s'))}。")]

        if event == "RESPONSE_OBSERVE_END":
            return [self._line(clock, f"在线回复观察结束，实际耗时 {self._seconds(values.get('elapsed_s'))}。")]

        if event == "INTERACTION_RESULT":
            status = str(values.get("status") or "")
            prefix = "" if status in PASS_STATUSES else "[异常] "
            lines = [self._line(clock, f"{prefix}{self._status_description(status)}。")]
            lines.append(
                "                 "
                f"唤醒={'成功' if values.get('wake_detected') else '失败'}，"
                f"在线ASR={'已启动' if values.get('online_asr_started') else '未启动'}，"
                f"在线回复={'已检测' if values.get('response_detected') else '未检测'}，"
                f"播放器={'已播放' if values.get('player_playing') else '未播放'}"
            )
            return lines

        if event == "ROUND_EVIDENCE_SUMMARY":
            key = self._current_unit_key(values)
            if key and values.get("evidence_file"):
                self.unit_evidence[key] = {
                    "file": values.get("evidence_file"),
                    "start": values.get("evidence_start"),
                    "end": values.get("evidence_end"),
                }
            return [
                self._line(
                    clock,
                    "播放器事件："
                    f"PREPARED={values.get('player_prepared', 0)}，"
                    f"PLAYING={values.get('player_playing', 0)}，"
                    f"COMPLETE={values.get('player_complete', 0)}，"
                    f"ERROR={values.get('player_error', 0)}。",
                )
            ]

        if event == "EVIDENCE_SAVED":
            key = self._current_unit_key(values)
            if key:
                self.unit_evidence[key] = {
                    "file": values.get("evidence_file"),
                    "start": values.get("evidence_start"),
                    "end": values.get("evidence_end"),
                }
            return []

        if event in {"ROUND_STATUS_OVERRIDE", "CASE_STATUS_OVERRIDE"}:
            return [
                self._line(
                    clock,
                    "[异常] 根据本轮完整串口证据，将结果从"
                    f"“{self._status_description(values.get('previous_status'))}”修正为"
                    f"“{self._status_description(values.get('status'))}”。",
                )
            ]

        if event == "ROUND_END":
            return self._render_unit_end("round", at, values)

        if event == "CASE_END":
            return self._render_unit_end("case", at, values)

        if event == "NEXT_CASE_DELAY_START":
            return [self._line(clock, f"等待 {self._seconds(values.get('wait_s'))} 后执行下一条用例。")]

        if event == "NEXT_CASE_DELAY_END":
            return []

        if event == "SERIAL_ALERT":
            self.alert_counts[level] += 1
            marker = str(values.get("marker") or "unknown")
            alert = {
                "at": at,
                "level": level,
                "port": values.get("port"),
                "role": values.get("role"),
                "cursor": values.get("serial_cursor"),
            }
            first_occurrence = marker not in self.alert_first
            if first_occurrence:
                self.alert_first[marker] = alert
            self.alert_last[marker] = alert
            if level == "WARN" and not first_occurrence:
                return []
            description = MARKER_DESCRIPTIONS.get(marker, f"检测到串口异常标记 {marker}")
            role = ROLE_NAMES.get(str(values.get("role")), str(values.get("role") or "设备"))
            label = "严重异常" if level == "CRITICAL" else "异常" if level == "ERROR" else "警告"
            return [
                self._line(
                    clock,
                    f"[{label}] {role} {description}，证据：serial_logs\\merged.log 第 {values.get('serial_cursor')} 行。",
                )
            ]

        if event == "SERIAL_EVENT":
            return []

        if event == "TASK_DURATION_LIMIT":
            self.duration_limit_reached = True
            return [self._line(clock, f"已达到预设运行时长 {self._seconds(values.get('elapsed_s'))}，停止创建新一轮测试。")]

        if event == "STOP_REQUESTED":
            return [self._line(clock, "[警告] 检测到 STOP 文件，将安全停止测试。")]

        if event == "TASK_EXCEPTION":
            return [
                self._line(
                    clock,
                    f"[严重异常] 工具运行异常：{values.get('error_type')}：{values.get('error')}。",
                )
            ]

        if event == "SERIAL_LOGGER_STOP":
            errors = values.get("serial_errors") or {}
            if errors:
                return [self._line(clock, f"[异常] 串口日志采集结束，串口错误：{self._display_value(errors)}。")]
            return [
                self._line(
                    clock,
                    f"串口日志采集结束，共保存 {values.get('final_cursor', 0)} 行合并串口证据。",
                )
            ]

        if event == "TASK_END":
            return self._render_task_end(at, values)

        if event == "TOOL_NOTE":
            return []

        if level in {"WARN", "ERROR", "CRITICAL"}:
            label = "严重异常" if level == "CRITICAL" else "异常" if level == "ERROR" else "警告"
            details = self._compact_details(values)
            return [self._line(clock, f"[{label}] 工具报告未分类事件 {event}{details}。")]
        return []

    def _render_config(self, at: str, values: dict[str, Any]) -> list[str]:
        lines = [SEPARATOR, self.task_name, SEPARATOR, f"任务创建：{at}"]
        if values.get("run_dir"):
            lines.append(f"任务目录：{values.get('run_dir')}")
        if self.target_total:
            label = "计划轮次" if "round" in self.task_type else "计划用例"
            lines.append(f"{label}：{self.target_total}")
        if self.duration_minutes:
            lines.append(f"运行时长：{self.duration_minutes:g} 分钟，到时完成当前轮后自动停止")
        if values.get("stop_at"):
            lines.append(f"自动停止：{values.get('stop_at')}")
        if values.get("command_pool_size"):
            lines.append(f"命令词数量：{values.get('command_pool_size')}")
        if values.get("cases"):
            lines.append(f"测试用例：{'、'.join(map(str, values.get('cases')))}")
        if values.get("interval_description"):
            lines.append(f"轮前间隔：{values.get('interval_description')}")
        elif values.get("interval_pattern_s"):
            lines.append(f"唤醒间隔：{' / '.join(map(str, values.get('interval_pattern_s')))} 秒交替")
        if values.get("wake_command_delay_description"):
            lines.append(f"唤醒后延时：{values.get('wake_command_delay_description')}")
        elif values.get("wake_command_delay_s") is not None:
            lines.append(f"唤醒后延时：{self._seconds(values.get('wake_command_delay_s'))}")
        if values.get("boundary_ratio"):
            lines.append(
                f"边界用例：{float(values.get('boundary_ratio')):.0%} 轮次使用 "
                f"{values.get('boundary_delay_description') or '识别超时边界延时'}"
            )
        if values.get("wake_wait_s") is not None:
            lines.append(f"唤醒等待上限：{self._seconds(values.get('wake_wait_s'))}")
        recognition_timeout = values.get("recognition_observe_s")
        if recognition_timeout is None:
            recognition_timeout = values.get("max_observe_s")
        if recognition_timeout is not None:
            lines.append(f"识别等待上限：{self._seconds(recognition_timeout)}")
        csk_port = values.get("csk_port")
        upper_port = values.get("upper_port")
        if csk_port or upper_port:
            lines.append(
                f"串口配置：CSK AP={csk_port or '-'}，上位机={upper_port or '-'}，"
                f"波特率={values.get('baudrate') or '-'}"
            )
        lines.extend(
            [
                "播放设备：指定 USB 音频播放设备",
                "安全停止：在任务目录创建空文件 STOP",
                "人读日志：task.log",
                "结构化事件：task_events.jsonl",
                "异常汇总：errors.log",
                "原始串口：serial_logs\\merged.log",
                SEPARATOR,
            ]
        )
        return lines

    def _render_unit_end(self, kind: str, at: str, values: dict[str, Any]) -> list[str]:
        number_field = "current_round" if kind == "round" else "current_case"
        number = int(values.get(number_field) or values.get(kind) or 0)
        key = (kind, number)
        status = self._effective_status(
            key,
            str(values.get("status") or "UNKNOWN"),
        )
        result_kind = self._status_kind(status)
        self.result_counts[result_kind] += 1
        description = self._status_description(status)
        if result_kind == "failure" and self.first_failure is None:
            self.first_failure = {"kind": kind, "number": number, "status": status, "at": at}

        unit_name = "轮" if kind == "round" else "条用例"
        tag = (
            "通过"
            if result_kind == "pass"
            else "符合预期"
            if result_kind == "expected"
            else "警告"
            if result_kind == "warning"
            else "失败"
        )
        elapsed = self._elapsed(self.unit_starts.get(key), at)
        elapsed_text = f"，耗时 {self._seconds(elapsed)}" if elapsed is not None else ""
        lines = ["", f"[{tag}] 第 {number} {unit_name}完成{elapsed_text}：{description}。"]

        critical = values.get("critical")
        player_failures = values.get("player_failures")
        warnings = values.get("warnings")
        if warnings not in (None, "", "-", []):
            self.warning_units += 1
        if critical not in (None, "", "-", []):
            lines.append(f"严重异常：{self._display_value(critical)}")
        if player_failures not in (None, "", "-", []):
            lines.append(f"播放器异常：{self._display_value(player_failures)}")
        if warnings not in (None, "", "-", []):
            lines.append(f"附加告警：{self._display_value(warnings)}")

        lines.append(self._progress_line(at, kind))
        evidence = self.unit_evidence.get(key, {})
        start = values.get("evidence_start", evidence.get("start"))
        end = values.get("evidence_end", evidence.get("end"))
        if start is not None and end is not None and int(end) > int(start):
            lines.append(
                f"串口证据：serial_logs\\merged.log 第 {int(start) + 1}-{int(end)} 行"
            )
        evidence_file = evidence.get("file")
        if evidence_file:
            lines.append(f"逐轮证据：{evidence_file}")
        lines.append(SEPARATOR)
        return lines

    def _render_task_end(self, at: str, values: dict[str, Any]) -> list[str]:
        outcome = str(values.get("outcome") or "COMPLETED")
        completed = int(values.get("completed_rounds") or values.get("completed_cases") or sum(self.result_counts.values()))
        unit_name = "轮" if "round" in self.task_type else "条用例"
        elapsed = self._elapsed(self.task_started_at, at)
        pass_count = self.result_counts["pass"]
        expected_count = self.result_counts["expected"]
        failure_count = self.result_counts["failure"]
        warning_count = self.result_counts["warning"] + self.warning_units
        normal_total = pass_count + failure_count
        recognition_rate = pass_count / normal_total if normal_total else 0.0
        expected_rate = (pass_count + expected_count) / completed if completed else 0.0

        has_critical_alert = any(
            alert.get("level") == "CRITICAL"
            for alert in self.alert_first.values()
        )
        if self.duration_limit_reached and outcome == "COMPLETED":
            result_text = "已达到预设时长，压测正常完成"
            reason = "达到预设运行时长"
        elif self.duration_limit_reached and failure_count == 0 and has_critical_alert:
            result_text = "已达到预设时长；测试场景均符合预期，但检测到设备严重异常"
            reason = "设备日志出现严重异常，详见首次严重异常和 errors.log"
        elif self.duration_limit_reached:
            result_text = "已达到预设时长，但测试期间检测到失败或异常"
            reason = "达到预设运行时长，且存在失败或异常"
        elif outcome == "STOPPED":
            result_text = "测试已按 STOP 请求安全停止"
            reason = "人工停止"
        elif outcome == "FAILED":
            result_text = "测试结束，检测到失败或异常"
            reason = "测试结果包含失败、串口错误或工具异常"
        else:
            result_text = "测试正常完成"
            reason = "计划执行完成"

        lines = [
            "",
            SEPARATOR,
            "测试任务结束",
            SEPARATOR,
            f"结束时间：{at}",
            f"最终结论：{result_text}",
            f"结束原因：{reason}",
        ]
        if elapsed is not None:
            lines.append(f"实际耗时：{self._seconds(elapsed)}")
        lines.append(f"完成数量：{completed} {unit_name}")
        if self.task_type == "wake_recognition_round":
            lines.extend(
                [
                    f"正常窗口识别通过：{pass_count}    边界轮符合预期：{expected_count}    "
                    f"真实失败：{failure_count}    告警轮次：{warning_count}",
                    f"正常窗口识别率：{recognition_rate:.2%}    场景符合预期率：{expected_rate:.2%}",
                ]
            )
        else:
            success_rate = pass_count / completed if completed else 0.0
            lines.append(
                f"通过：{pass_count}    失败：{failure_count}    告警：{warning_count}    "
                f"通过率：{success_rate:.2%}"
            )
        status_counts = values.get("status_counts") or {}
        if status_counts:
            lines.append(
                "原始状态明细：" + "，".join(f"{name}={count}" for name, count in status_counts.items())
            )
        if self.first_failure:
            first_name = "轮" if self.first_failure["kind"] == "round" else "条用例"
            lines.append(
                f"首次失败：第 {self.first_failure['number']} {first_name}，"
                f"{self._status_description(self.first_failure['status'])}，时间 {self.first_failure['at']}"
            )
        critical_alerts = [
            (marker, alert)
            for marker, alert in self.alert_first.items()
            if alert.get("level") == "CRITICAL"
        ]
        if critical_alerts:
            marker, alert = critical_alerts[0]
            role = ROLE_NAMES.get(str(alert.get("role")), str(alert.get("role") or "设备"))
            lines.append(
                f"首次严重异常：{alert.get('at')}，{role} "
                f"{MARKER_DESCRIPTIONS.get(marker, marker)}，"
                f"证据 serial_logs\\merged.log 第 {alert.get('cursor')} 行"
            )
        serial_failures = values.get("serial_failure_marker_counts") or {}
        if serial_failures:
            lines.append(
                "串口异常统计："
                + "，".join(
                    f"{MARKER_DESCRIPTIONS.get(str(name), str(name))}={count}"
                    for name, count in serial_failures.items()
                )
            )
        if self.alert_counts:
            lines.append(
                "串口告警事件："
                + "，".join(f"{level}={count}" for level, count in self.alert_counts.items())
            )
        marker_counts = values.get("serial_marker_counts") or {}
        warning_markers = {
            marker: count
            for marker, count in marker_counts.items()
            if marker in {"network_down", "wdt_not_feed", "player_url_context_null"}
        }
        if warning_markers:
            lines.append(
                "设备告警统计："
                + "，".join(
                    f"{MARKER_DESCRIPTIONS.get(str(marker), str(marker))}={count}"
                    for marker, count in warning_markers.items()
                )
            )
        if values.get("serial_errors"):
            lines.append(f"串口读取错误：{self._display_value(values.get('serial_errors'))}")
        if values.get("error"):
            lines.append(f"工具异常：{values.get('error')}")
        lines.extend(
            [
                "结果明细：rounds.csv 或 results.csv",
                "异常汇总：errors.log",
                "结构化事件：task_events.jsonl",
                "完整串口：serial_logs\\merged.log",
                SEPARATOR,
            ]
        )
        return lines

    def _render_error(self, event: str, level: str, at: str, values: dict[str, Any]) -> list[str]:
        if event == "SERIAL_ALERT":
            marker = str(values.get("marker") or "unknown")
            if level == "WARN" and self.alert_counts[level] > 1 and marker in self.alert_first:
                first = self.alert_first[marker]
                if first.get("cursor") != values.get("serial_cursor"):
                    return []
            role = ROLE_NAMES.get(str(values.get("role")), str(values.get("role") or "设备"))
            return [
                f"{at} [{level}] {role} {MARKER_DESCRIPTIONS.get(marker, marker)}；"
                f"证据=serial_logs\\merged.log 第 {values.get('serial_cursor')} 行"
            ]
        if event in {"ROUND_END", "CASE_END"}:
            key = self._current_unit_key(values)
            status = self._effective_status(key, str(values.get("status") or "UNKNOWN"))
            if self._status_kind(status) != "failure":
                return []
            kind = "轮" if event == "ROUND_END" else "条用例"
            number = values.get("current_round") if event == "ROUND_END" else values.get("current_case")
            start = values.get("evidence_start")
            end = values.get("evidence_end")
            evidence = ""
            if start is not None and end is not None:
                evidence = f"；证据=serial_logs\\merged.log 第 {int(start) + 1}-{int(end)} 行"
            return [f"{at} [FAIL] 第 {number} {kind}：{self._status_description(status)}{evidence}"]
        if event == "SERIAL_PORT_ERROR":
            return [f"{at} [ERROR] 串口 {values.get('port')} 失败：{values.get('error')}"]
        if event == "TASK_EXCEPTION":
            return [f"{at} [CRITICAL] 工具异常：{values.get('error_type')}：{values.get('error')}"]
        if event == "TASK_END":
            lines = []
            for marker, count in (values.get("serial_marker_counts") or {}).items():
                if marker not in self.alert_first:
                    continue
                first = self.alert_first[marker]
                last = self.alert_last.get(marker, first)
                lines.append(
                    f"{at} [SUMMARY] {MARKER_DESCRIPTIONS.get(str(marker), str(marker))}："
                    f"共 {count} 次；首次=第 {first.get('cursor')} 行 {first.get('at')}；"
                    f"末次=第 {last.get('cursor')} 行 {last.get('at')}"
                )
            if str(values.get("outcome")) == "FAILED":
                lines.append(f"{at} [FAIL] 任务结束，测试期间检测到失败或异常。")
            return lines
        return []

    def _progress_line(self, at: str, kind: str) -> str:
        completed = sum(self.result_counts.values())
        pass_count = self.result_counts["pass"]
        expected_count = self.result_counts["expected"]
        failure_count = self.result_counts["failure"]
        warning_count = self.result_counts["warning"] + self.warning_units
        normal_total = pass_count + failure_count
        recognition_rate = pass_count / normal_total if normal_total else 0.0
        unit = "轮" if kind == "round" else "条"
        if self.duration_minutes and self.task_started_at:
            elapsed = self._elapsed(self.task_started_at, at) or 0.0
            progress = (
                f"累计进度：已完成 {completed} {unit} | 已运行 {self._duration_text(elapsed)} / "
                f"{self._duration_text(self.duration_minutes * 60)}"
            )
        elif self.target_total:
            progress = f"累计进度：{completed}/{self.target_total}"
        else:
            progress = f"累计进度：已完成 {completed} {unit}"
        if self.task_type == "wake_recognition_round":
            return (
                f"{progress}    正常识别：{pass_count}    边界符合预期：{expected_count}    "
                f"真实失败：{failure_count}    告警轮次：{warning_count}    "
                f"正常窗口识别率：{recognition_rate:.2%}"
            )
        success_rate = pass_count / completed if completed else 0.0
        return (
            f"{progress}    通过：{pass_count}    失败：{failure_count}    "
            f"告警：{warning_count}    通过率：{success_rate:.2%}"
        )

    def _unit_title(self, kind: str, number: int, total: int, command: str) -> str:
        label = "轮" if kind == "round" else "条用例"
        total_text = f"/{total}" if total else ""
        command_text = f"    命令：{command}" if command else ""
        return f"[第 {number}{total_text} {label}]{command_text}"

    def _current_unit_key(self, values: dict[str, Any]) -> tuple[str, int] | None:
        if values.get("round") is not None:
            return "round", int(values.get("round"))
        if values.get("case") is not None:
            return "case", int(values.get("case"))
        return None

    def _effective_status(self, key: tuple[str, int] | None, status: str) -> str:
        if key in self.boundary_units:
            if status == "ASR_TIMEOUT":
                return "BOUNDARY_ASR_TIMEOUT_EXPECTED"
            if status == "NO_RECOGNITION":
                return "BOUNDARY_NO_RECOGNITION_EXPECTED"
            if status == "RECOGNIZED":
                return "BOUNDARY_RECOGNIZED"
        return status

    @staticmethod
    def _infer_task_type(values: dict[str, Any]) -> str:
        if values.get("interval_pattern_s"):
            return "wake_interval_round"
        if values.get("cases"):
            return "online_case"
        if values.get("command_pool_size"):
            return "wake_recognition_round"
        return "core_command_case"

    @staticmethod
    def _default_task_name(task_type: str) -> str:
        return {
            "wake_recognition_round": "TCL 唤醒与命令词稳定性压测",
            "wake_interval_round": "TCL 普通话唤醒交替间隔压测",
            "online_case": "TCL 在线交互功能验证",
            "core_command_case": "TCL 核心命令词离线识别批测",
        }.get(task_type, "TCL 语音压测")

    @staticmethod
    def _status_kind(status: str) -> str:
        if status in {
            "BOUNDARY_ASR_TIMEOUT_EXPECTED",
            "BOUNDARY_NO_RECOGNITION_EXPECTED",
            "BOUNDARY_RECOGNIZED",
        }:
            return "expected"
        if status in PASS_STATUSES:
            return "pass"
        if status in WARNING_STATUSES:
            return "warning"
        return "failure"

    @staticmethod
    def _status_description(status: Any) -> str:
        value = str(status or "UNKNOWN")
        return STATUS_DESCRIPTIONS.get(value, value)

    @staticmethod
    def _clock(timestamp: str) -> str:
        return timestamp[11:23] if len(timestamp) >= 23 and timestamp[4:5] == "-" else timestamp

    @staticmethod
    def _line(clock: str, message: str) -> str:
        return f"{clock}  {message}"

    @staticmethod
    def _elapsed(started_at: str | None, ended_at: str | None) -> float | None:
        if not started_at or not ended_at:
            return None
        try:
            return max(0.0, (datetime.fromisoformat(ended_at) - datetime.fromisoformat(started_at)).total_seconds())
        except ValueError:
            return None

    @staticmethod
    def _seconds(value: Any) -> str:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return "未知"
        if seconds >= 60:
            return ToolTimeline._duration_text(seconds)
        return f"{seconds:.2f} 秒"

    @staticmethod
    def _duration_text(seconds: float) -> str:
        total = max(0, int(round(seconds)))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    @staticmethod
    def _display_value(value: Any) -> str:
        if isinstance(value, dict):
            return "，".join(f"{name}={item}" for name, item in value.items())
        if isinstance(value, (list, tuple, set)):
            return "、".join(map(str, value))
        return str(value)

    @staticmethod
    def _compact_details(values: dict[str, Any]) -> str:
        selected = []
        for name in ("reason", "error", "status", "command"):
            if values.get(name) is not None and values.get(name) != "":
                selected.append(f"{name}={values.get(name)}")
        return "（" + "，".join(selected) + "）" if selected else ""


def classify_serial_line(line: str) -> tuple[str, str] | None:
    value = line.lower()
    if "malloc probe buf fail" in value:
        return "ERROR", "player_malloc_probe_fail"
    if "player_event_error" in value or "player_agent_msg_play_error" in value:
        return "ERROR", "player_event_error"
    if "assert failed" in value or "assertion failed" in value:
        return "CRITICAL", "assert_failed"
    if "watchdog reset" in value or "watchdog reboot" in value or "wdt reset" in value or "wdt reboot" in value:
        return "CRITICAL", "watchdog_reset"
    if "system reset" in value or "hardware reset" in value or "reset reason" in value:
        return "CRITICAL", "system_reset"
    if "panic" in value:
        return "CRITICAL", "panic"
    if "fatal error" in value or "guru meditation" in value:
        return "CRITICAL", "fatal_error"
    if "exception" in value:
        return "CRITICAL", "exception"
    if "crash" in value:
        return "CRITICAL", "crash"
    if "reboot" in value:
        return "CRITICAL", "reboot"
    if "wifi state: 0" in value or "mqtt state: 0" in value or "websocket state: 0" in value:
        return "WARN", "network_down"
    if "wdt not feed" in value:
        return "WARN", "wdt_not_feed"
    if "url context is null" in value:
        return "WARN", "player_url_context_null"
    if "player_event_prepared" in value:
        return "INFO", "player_prepared"
    if "player_event_playing" in value:
        return "INFO", "player_playing"
    if "player_event_playback_complete" in value:
        return "INFO", "player_playback_complete"
    if "soundplayer status: 0xef" in value:
        return "INFO", "soundplayer_prepare_status_0xef"
    if "start online asr service, state: asr_state_wakeup" in value:
        return "INFO", "online_asr_start"
    if "asr status: asr_status_start" in value:
        return "INFO", "cloud_asr_start"
    if "record timeout" in value:
        return "INFO", "asr_record_timeout"
    if "esr timeout" in value:
        return "INFO", "esr_timeout"
    if "code: 1007" in value or "invalid payload data" in value:
        return "ERROR", "cloud_asr_error_1007"
    if "asr data is null" in value or "no asr data" in value:
        return "ERROR", "asr_data_null"
    if "code: 200" in value:
        return "INFO", "cloud_reply_200"
    return None
