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

## 重生成（lstts / IndexTTS）

原语料只读：不重生成、不重编码、不改名。需要补录或比对时，用 `scripts/regen_audio.py` 把同一批文本重新合成到 `result/audio_regen/<日期>/`，产出独立 manifest，人工比对后再决定是否人工替换原文件。

安装 / 更新 lstts：

```bash
pip install "lstts-core @ git+https://github.com/zjashanda/lstts.git@main"
pip install -U --force-reinstall --no-cache-dir "lstts-core @ git+https://github.com/zjashanda/lstts.git@main"
python -c "import lstts_core; print(lstts_core.__version__)"
```

清单直接复用 `audio/manifests/audio_manifest.csv` 的 `text` 列（2778 条），无需另写词表：

```bash
# 只看计划，不装 lstts、不访问网络
python scripts/regen_audio.py plan

# 实际合成前 5 条到 result/audio_regen/<时间戳>/
python scripts/regen_audio.py build --limit 5
```

规则：

- 输出目录默认 `result/audio_regen/<时间戳>/`，`--corpus-dir audio/generated` 命中即拒绝，避免覆盖静态语料。
- 每条产物写 `regen_manifest.json` / `.csv`：文本、音色、采样率、时长、响度、体积、SHA-256、状态。
- 凭据只用 `LSTTS_TTS_TOKEN`（兼容旧 `LSTEST_TTS_TOKEN`），不写进仓库和日志。
- 离线自测：`python -m unittest discover -s tests`（桩替换 lstts_core，不访问网络）。
