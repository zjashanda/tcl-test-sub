# 音频测试资料

## 范围

- `audio/generated/`：2774 个 MP3，包含唤醒词、核心命令、泛化文本、参数展开及反集相关测试资料。
- `audio/online_interaction/`：4 个 MP3，分别用于天气、笑话、音乐和停止播放在线探测。
- `testdata/lexicon/complete_core_commands.csv`：当前正式核心命令 489 条，与 `audio/generated/` 一一检查。

用户所称“wav 音频测试资料”在当前工程中实际为 MP3 文件；仓库保持原格式和原中文文件名，不做无必要转码。

这些是当前工程已有测试资产。原工程没有为每个文件完整保留 TTS 供应商、模型、音色和合成参数，因此 manifest 明确记录 `generation_metadata=not_preserved`，不补造不可验证的生成信息。

## Manifest

- `audio/manifests/audio_manifest.csv`：每个文件的相对路径、类别、文本、大小、SHA-256、容器、codec、采样率、声道和时长。
- `audio/manifests/audio_manifest_summary.json`：文件数、分类数、总字节、重复 hash 和无效音频统计。

构建 manifest：

```powershell
python -u scripts\build_audio_manifest.py build
```

验证现有 manifest：

```powershell
python -u scripts\build_audio_manifest.py verify
```

构建依赖 `ffprobe`。正式发布要求：文件数量非零、全部文件可解析、大小和时长大于零、SHA-256 不重复，manifest 与实际目录完全一致。

## 使用规则

- 播放前按显式声卡 key probe，禁止回退默认设备。
- 不使用文件顺序作为 case ID 或测试 oracle；通过词表或明确文本关联。
- 不改名或覆盖既有音频来迎合测试结果。
- 新增、替换或删除音频后必须重建 manifest，并同步词表覆盖统计。
- 不把运行时临时音频写回 `audio/generated/`；临时产物放调用者工作区 `result/`。
