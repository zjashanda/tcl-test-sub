---
name: tcl-test-sub
description: "TCL 双麦空调语音验证与压测项目 Skill。Use when 在当前工程执行、复核或扩展 TCLTestSub、小T小T、COM20/COM22、唤醒识别长稳压测、固定或随机间隔、识别超时边界注入、2 秒/20 秒交替唤醒、核心命令词批测、在线天气/笑话/音乐交互，或分析播放器和固件异常。"
---

# TCLTestSub

## 核心用途

选择、预检、执行和复核 TCL 双麦空调语音测试。发布包内置正式脚本、核心词表和全部音频测试资料；运行结果写入调用者工作区，不写入已安装 Skill。

先读 [references/capability-matrix.md](references/capability-matrix.md)，不要把需求矩阵中的全部场景误报为已实现能力。音频范围、规格和校验方法见 [references/audio-assets.md](references/audio-assets.md)。

## 已实现入口

| 目标 | 统一模式 | 实际脚本 |
| --- | --- | --- |
| 唤醒加命令识别长稳、固定/随机间隔、超时边界注入 | `wake-recognition` | `scripts/voice_validation/run_tcl_wake_recognition_stress.py` |
| 普通话唤醒词按 2 秒/20 秒目标间隔交替运行 | `wake-interval` | `scripts/voice_validation/run_tcl_wake_interval_stress.py` |
| 当前核心命令清单离线识别覆盖 | `core-command` | `scripts/voice_validation/run_core_command_sweep.py` |
| 天气、笑话、音乐和停止播放的在线链路/播放器探测 | `online-interaction` | `scripts/voice_validation/run_tcl_online_interaction_probe.py` |

统一入口：

```powershell
python -u scripts\tcl_test_sub.py list
python -u scripts\tcl_test_sub.py preflight --mode all
```

命令示例和参数见 [references/runbook.md](references/runbook.md)。

## 标准流程

1. 读取调用者工作区 `plan.md`，不存在时创建，并同步本次目标、范围和当前硬件映射。
2. 根据能力矩阵确认场景成熟度；未实现的需求不能直接运行。
3. 运行无硬件副作用的 `preflight`，检查脚本、依赖、词表和音频。
4. 用户明确允许连接当前设备后，再使用 `preflight --hardware` 检查串口和指定声卡。
5. 正式长跑前先做 1 至 3 轮或少量用例 smoke。
6. 运行中查看 `task.log`、`progress.json` 和 `errors.log`，不要只看终端输出。
7. 结束后从 CSV、机器事件和持续串口证据复核结论；判定规则见 [references/result-interpretation.md](references/result-interpretation.md)。
8. 将实际结果和下一步同步到调用者工作区 `plan.md`。

## 当前 Profile

- `COM20@921600`：CSK AP 主日志。
- `COM22@921600`：上位机辅助日志。
- 唤醒词：`小T小T`。
- 声卡：`VID_8765&PID_5678:9_387C0FC7_0_0000`；每次运行前重新 probe。
- 播放工具：`~/.codex/skills/listenai-play/scripts/listenai_play.py`。
- 核心命令：`testdata/lexicon/complete_core_commands.csv`，当前 489 条。
- 音频：`audio/generated/` 2774 条、`audio/online_interaction/` 4 条；全部由 manifest 校验。
- 重生成只允许输出到 `result/audio_regen/<日期>/`，不覆盖内置音频；入口 `scripts/regen_audio.py`，见 [references/audio-assets.md](references/audio-assets.md)。

串口被占用时判为 `BLOCKED`，不得擅自关闭用户程序。显式声卡不存在、重复或映射异常时停止，禁止回退到默认声卡。

## 快速运行

安装已验证依赖：

```powershell
python -m pip install -r requirements.txt
```

唤醒加命令识别 3 轮 smoke：

```powershell
python -u scripts\tcl_test_sub.py run wake-recognition -- --rounds 3 --interval-mode fixed --fixed-interval-s 2 --wake-command-delay-mode fixed --fixed-wake-command-delay-s 1.6 --boundary-ratio 0
```

30 分钟随机加边界压测：

```powershell
python -u scripts\tcl_test_sub.py run wake-recognition -- --rounds 100000 --duration-minutes 30 --interval-mode random --min-interval-s 2 --max-interval-s 10 --wake-command-delay-mode random --min-wake-command-delay-s 1 --max-wake-command-delay-s 3 --boundary-ratio 0.2 --boundary-min-s 13 --boundary-max-s 15.5
```

2 秒/20 秒交替唤醒：

```powershell
python -u scripts\tcl_test_sub.py run wake-interval -- --stop-at 2026-07-29T08:00:00
```

运行前把示例时间替换为未来的北京时间。

## 结果和停止

默认结果写入调用者当前工作区：

```text
result/YYYYMMDD_HHMMSS_<task>/
```

主要证据：

- `task.log`：中文人读执行时序。
- `task_events.jsonl`：完整机器事件。
- `errors.log`：异常汇总。
- `rounds.csv` 或 `results.csv`：逐轮/逐用例事实。
- `progress.json`：实时进度。
- `serial_logs/`：COM20、COM22 和合并串口原始证据。
- `round_evidence/` 或 `case_evidence/`：逐项证据。
- `summary_final.md/json`、`summary.md` 或 `summary.json`：汇总。

`wake-recognition`、`wake-interval` 和 `core-command` 支持在活动任务目录创建空文件 `STOP`，脚本会在安全边界停止并保留结果。统一入口返回码：`0` 表示自动流程 `COMPLETED`，`2` 表示检测到失败/受阻，`3` 表示按 `STOP` 停止。返回 `0` 仍不代替产品验收复核。

## 强制判定

- panic、crash、非预期 reboot/reset、watchdog reset、assert、串口断流和播放器持续失效为任务级 sticky failure。
- 边界轮 `ASR_TIMEOUT` 或 `NO_RECOGNITION` 可符合预期，不并入正常窗口失败率。
- `wdt not feed`、`url context is null` 和 `dont support this keyword` 默认是告警或分层证据，不单独等同于重启。
- `Wakeup` JSON 的 keyword/intent 表示算法识别；`dont support this keyword` 表示应用映射未支持，两层必须分开。
- 短时 PASS 不覆盖历史长压 FAIL，后续恢复不能抹掉同一任务中的严重异常。

## 已知限制

- 当前同步播放器不能达到严格 2 秒相邻播放启动间隔；历史实测约 4 至 5 秒。修复为持久化播放器前，不得宣称严格 2 秒已覆盖。
- 100000 轮历史任务只完成 2839 轮，不能作为 100000 轮完整结论。
- 现存 490 条历史全量结果含后来排除的越界温度实例；当前 489 条需重新正式执行。
- 在线交互是四条固定短用例，不等于音乐/新闻/故事长播或完整域切换压测。
- 串口实现当前在内存保留本次已解码事件；超长运行应先设置时长并监控内存。
- 源项目历史任务使用 `logs/`；发布版默认使用 `result/`，历史结果未包含在仓库中。

## 安全边界

- 本 Skill 不自动烧录、不写串口命令、不重启、不切换电源，也不删除历史日志。
- 前三个入口只做本地音频播放和串口只读采集。
- `online-interaction` 会触发设备在线天气、笑话和音乐服务，仅在用户明确要求时运行。
- 不无参数启动根目录 BAT 后离开现场；其默认目标是 100000 轮且没有时长限制。
- 不修改内置音频、词表或历史证据来迎合结论；音频变化后必须重建并验证 manifest。

修改本 Skill 或测试脚本时同时使用 `trails` 和 `skill-creator`。新增场景必须先具备入口、前检、持续串口、状态 oracle、STOP/超时和真实 smoke，再升级能力矩阵。
