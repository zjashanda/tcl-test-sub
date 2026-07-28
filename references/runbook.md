# TCLTestSub 运行手册

## 前置检查

在 Skill 根目录运行：

```powershell
python -u scripts\tcl_test_sub.py --help
python -u scripts\tcl_test_sub.py preflight --mode all
python -u scripts\tcl_test_sub.py preflight --mode wake-recognition --hardware
```

不带 `--hardware` 时不打开串口或声卡。硬件预检检查 COM20/COM22 枚举和指定声卡 probe。

## 唤醒识别压测

3 轮正常 smoke：

```powershell
python -u scripts\tcl_test_sub.py run wake-recognition -- --rounds 3 --interval-mode fixed --fixed-interval-s 2 --wake-command-delay-mode fixed --fixed-wake-command-delay-s 1.6 --boundary-ratio 0
```

30 分钟随机加边界：

```powershell
python -u scripts\tcl_test_sub.py run wake-recognition -- --rounds 100000 --duration-minutes 30 --interval-mode random --min-interval-s 2 --max-interval-s 10 --wake-command-delay-mode random --min-wake-command-delay-s 1 --max-wake-command-delay-s 3 --boundary-ratio 0.2 --boundary-min-s 13 --boundary-max-s 15.5 --seed 20260722
```

指定文本并按顺序循环：

```powershell
python -u scripts\tcl_test_sub.py run wake-recognition -- --commands-file testdata\lexicon\hang_up_variant_retest_commands.txt --cycle-commands --rounds 20 --boundary-ratio 0
```

## 交替间隔压测

```powershell
python -u scripts\tcl_test_sub.py run wake-interval -- --stop-at 2026-07-29T08:00:00
```

替换为未来北京时间。必须报告目标间隔和实际间隔，不能仅报告唤醒率。

## 核心命令覆盖

统一入口默认注入当前 489 条命令 CSV。

```powershell
python -u scripts\tcl_test_sub.py run core-command -- --max-cases 3
python -u scripts\tcl_test_sub.py run core-command
python -u scripts\tcl_test_sub.py run core-command -- --commands-file testdata\lexicon\timeout_retest_commands.txt
```

生成异常复测清单：

```powershell
python scripts\voice_validation\build_retry_closure.py build --first-results result\<首轮目录>\results.csv --output result\retry_commands.txt
```

## 在线交互探测

```powershell
python -u scripts\tcl_test_sub.py run online-interaction
```

固定检查天气、笑话、音乐、停止播放。云端返回成功不能单独证明设备已播报，必须检查播放器事件链。

## 停止和日志重建

前三个任务可在活动任务目录创建 `STOP`：

```powershell
New-Item -ItemType File result\<活动任务目录>\STOP
```

从机器事件重建人读日志：

```powershell
python -u scripts\tcl_test_sub.py rebuild-log result\<任务目录>
```

重建只覆盖派生的 `task.log` 和 `errors.log`，不修改串口、CSV 和机器事件事实。

## 结果文件

```text
result/YYYYMMDD_HHMMSS_<task>/
├── task_config.json
├── task.log
├── task_events.jsonl
├── errors.log
├── progress.json
├── rounds.csv 或 results.csv
├── summary_final.md/json 或 summary.md/json
├── serial_logs/
├── round_evidence/ 或 case_evidence/
└── tool_logs/playback.jsonl
```

先读汇总确定范围，再用 `task.log` 找首次异常，最后按 `serial_logs/merged.log` 的北京时间和一基行号复核。
