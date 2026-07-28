# TCLTestSub

TCL 双麦空调语音验证与压测 Skill，包含可独立运行的测试脚本、核心词表和完整音频测试资料。

## Capabilities

- 唤醒加命令识别长稳压测，支持固定/随机间隔和超时边界注入。
- 普通话唤醒词按 2 秒/20 秒目标间隔交替压测。
- 当前 489 条核心命令离线识别覆盖。
- 天气、笑话、音乐和停止播放的在线链路及播放器探测。
- 双串口持续证据、中文工具日志、机器事件、STOP 安全停止和日志重建。

完整能力边界见 [`references/capability-matrix.md`](references/capability-matrix.md)。

## Audio Assets

- `audio/generated/`：2774 个 MP3 测试音频。
- `audio/online_interaction/`：4 个在线交互 MP3。
- 总计：2778 个文件，33,995,664 字节。
- 规格：全部为 MP3、24 kHz、单声道。
- SHA-256 重复：0。
- 无效音频：0。

用户所称 WAV 测试资料在源工程中实际为 MP3；仓库保留原格式和中文文件名。完整清单见：

- [`audio/manifests/audio_manifest.csv`](audio/manifests/audio_manifest.csv)
- [`audio/manifests/audio_manifest_summary.json`](audio/manifests/audio_manifest_summary.json)
- [`references/audio-assets.md`](references/audio-assets.md)

## Install

将仓库克隆到：

```text
~/.codex/skills/tcl-test-sub
```

安装依赖：

```powershell
python -m pip install -r requirements.txt
```

音频播放依赖全局 Skill `listenai-play` 和系统 `ffmpeg/ffprobe`。

## Preflight

```powershell
python -u scripts\tcl_test_sub.py list
python -u scripts\tcl_test_sub.py preflight --mode all
python -u scripts\tcl_test_sub.py preflight --mode all --hardware
```

当前硬件 profile：

- `COM20@921600`：CSK AP。
- `COM22@921600`：上位机日志。
- 声卡 key：`VID_8765&PID_5678:9_387C0FC7_0_0000`。

显式设备不可用时停止，不回退默认声卡。

## Run

3 轮唤醒识别 smoke：

```powershell
python -u scripts\tcl_test_sub.py run wake-recognition -- --rounds 3 --interval-mode fixed --fixed-interval-s 2 --wake-command-delay-mode fixed --fixed-wake-command-delay-s 1.6 --boundary-ratio 0
```

核心命令小批量检查：

```powershell
python -u scripts\tcl_test_sub.py run core-command -- --max-cases 3
```

详细命令见 [`references/runbook.md`](references/runbook.md)，判定口径见 [`references/result-interpretation.md`](references/result-interpretation.md)。

运行结果默认写入调用者工作区的 `result/`，不会写入 Skill 安装目录。

## Validate Audio

```powershell
python -u scripts\build_audio_manifest.py verify
```

新增、替换或删除音频后，先执行：

```powershell
python -u scripts\build_audio_manifest.py build
```

再提交更新后的音频和 manifest。
