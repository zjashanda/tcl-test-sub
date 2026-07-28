# TCLTestSub 结果判定

## 证据优先级

1. `serial_logs/` 连续原始串口证据。
2. `rounds.csv`、`results.csv` 和 `task_events.jsonl`。
3. 经复核生成的汇总。
4. 脚本初判和终端输出。

## 状态

- `RECOGNIZED`、`WAKE_RECOGNIZED`、`PASS`：当前有效场景达到目标。
- `ASR_TIMEOUT`：已进入识别流程并出现 `esr timeout`。
- `NO_RECOGNITION`、`NO_ASR`：窗口内没有有效结果，也无明确超时。
- `WAKE_FAIL`：电脑唤醒音频成功播放，但设备没有有效唤醒证据。
- `AUDIO_FAIL`、`BLOCKED_AUDIO`：电脑播放失败或音频缺失。
- `INTENT_MISMATCH`：存在识别结果但脚本 oracle 未匹配，必须复核 oracle。
- `PLAYER_FAIL`：设备播放器明确错误或没有有效播放链。
- `CRITICAL_FIRMWARE_MARKER`、`CRITICAL`：固件严重异常。
- `STOPPED`：安全停止，不等于完成计划。

## 分场景判定

唤醒识别压测必须把正常窗口与边界窗口分开。正常识别率使用 `RECOGNIZED 正常轮 / 全部有效正常轮`；边界 `ASR_TIMEOUT` 或 `NO_RECOGNITION` 可符合预期，不并入正常失败率。

交替间隔压测必须同时看唤醒率、实际间隔和播放器链。唤醒 100% 但严格 2 秒未达到或播放器持续失效时，整项仍 FAIL。

核心命令至少分为电脑播放/唤醒层、算法 keyword/intent 层、应用执行层。算法输出后出现 `dont support this keyword`，结论是“算法识别成功、应用映射未支持”，不能算端到端 PASS。

在线交互 PASS 至少要求唤醒、在线 ASR、回复证据和有效播放器状态。云端 `code: 200` 不能单独证明用户听到回复。

## 严重异常

以下为任务级 sticky failure：panic、crash、fatal、exception、assert、非预期 reboot/reset、watchdog reset、串口断开、播放器持续失效、写盘失败和未处理异常。后续恢复不能删除同一任务中的严重异常。

`wdt not feed`、`url context is null`、单独的 `soundplayer status: 0XEF` 和 `dont support this keyword` 默认是告警或分层证据，不单独等于重启。

## 最终汇总

最终报告必须说明结束原因、计划/完成数量、正常/边界/受阻数量、成功率分母、首次失败和严重异常、是否恢复、串口完整性，以及电脑播放、算法识别、应用映射和设备播放器各层结论。短时 PASS 不覆盖历史长压 FAIL。
