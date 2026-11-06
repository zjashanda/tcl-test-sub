"""离线桩：模拟 lstts_core，仅用于验证 regen_audio.py 的编排与 manifest 契约。

不访问网络、不调用真实 TTS。真实环境请安装：
  pip install "lstts-core @ git+https://github.com/zjashanda/lstts.git@main"
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path
from typing import Any

__version__ = "stub-0.0.0"


def load_config(name: str = "index_tts.json", path: str | Path | None = None) -> dict[str, Any]:
    return {"base_url": "http://stub.invalid", "token": "stub", "request_timeout": 1}


def synthesize_one(text: str, output: str | Path, *, voice: str | Path = "付小春",
                   emotion: str | Path | None = None, config: dict | None = None,
                   timeout_s: float | None = None, validate_audio: bool = True) -> dict[str, Any]:
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    rate = 24000
    frames = int(rate * 0.35)  # 时长随文本长度轻微变化，便于区分条目
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        for index in range(frames):
            value = int(3000 * math.sin(2 * math.pi * 220 * index / rate))
            handle.writeframes(struct.pack("<h", value))
    return {"audio_path": str(target), "seconds": frames / rate, "peak_dbfs": -20.0, "notes": []}


def inspect_wav(path: str | Path, *, strict: bool = False) -> dict[str, Any]:
    with wave.open(str(path), "rb") as handle:
        rate, frames = handle.getframerate(), handle.getnframes()
        width, channels = handle.getsampwidth(), handle.getnchannels()
    duration = frames / rate if rate else 0.0
    if strict and not (width == 2 and duration > 0.2):
        raise RuntimeError(f"stub output validation failed: rate={rate} duration={duration}")
    return {
        "container": "wav", "codec": "pcm_s16le", "sample_rate": rate, "channels": channels,
        "duration_seconds": round(duration, 6), "mean_volume_db": -23.0, "peak_volume_db": -20.0,
    }
