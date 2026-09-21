"""Centralized safety limits, whitelists, and shared constants for OmniMedia."""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Resource-safety limits
# ---------------------------------------------------------------------------
SUBPROCESS_TIMEOUT_SEC: int = 300
PROBE_TIMEOUT_SEC: int = 15
# 原生直读的内联上限（超过就改走「切片落盘 + 给路径」通道，避免把大音频塞进上下文）。
# 与 MAX_ONESHOT_MINUTES 是两件事：前者是**回传体积**闸，后者是**时长**闸。
MAX_SAFE_INLINE_BYTES: int = 8 * 1024 * 1024  # 8 MiB
DEFAULT_SAFE_SLICE_MINUTES: float = float(os.environ.get("OMNI_DEFAULT_SLICE_MINUTES", "30.0"))
# 单块硬上限（分钟）：超过它就不建议整片直读，改按 DEFAULT_SAFE_SLICE_MINUTES 分卷。
# 「整片就绪」判定与探测报告里的建议文案都必须由它推导，不许再写字面量 4500。
MAX_ONESHOT_MINUTES: float = 75.0
MAX_CONCURRENT_FFMPEG: int = int(os.environ.get("OMNI_MAX_CONCURRENT_FFMPEG", "5"))
AUDIO_BITRATE_VOICE: str = "32k"
ERROR_BODY_EXCERPT_CHARS: int = 500

# ---------------------------------------------------------------------------
# Media extension whitelists
# ---------------------------------------------------------------------------
VIDEO_EXTS: frozenset[str] = frozenset(
    {".mp4", ".mkv", ".mov", ".flv", ".webm", ".avi", ".wmv", ".ts"}
)
AUDIO_EXTS: frozenset[str] = frozenset(
    {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
)
MEDIA_EXTS: frozenset[str] = frozenset(VIDEO_EXTS | AUDIO_EXTS)

# ---------------------------------------------------------------------------
# Whitelists for tool arguments
# ---------------------------------------------------------------------------
OUTPUT_MODE_WHITELIST: frozenset[str] = frozenset({"auto", "file", "inline"})
MODE_WHITELIST: frozenset[str] = frozenset({"transcribe", "summarize", "qa", "custom"})

# Slices cache directory
def get_slices_cache_dir() -> Path:
    """Returns directory path for caching audio/video slices."""
    base = Path(os.environ.get("OMNI_MEDIA_CACHE_DIR") or (Path.home() / ".cache" / "omni-media" / "slices"))
    base.mkdir(parents=True, exist_ok=True)
    return base
