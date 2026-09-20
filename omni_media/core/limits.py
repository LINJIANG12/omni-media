"""Centralized safety limits, whitelists, and shared constants for OmniMedia."""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Resource-safety limits
# ---------------------------------------------------------------------------
SUBPROCESS_TIMEOUT_SEC: int = 300
PROBE_TIMEOUT_SEC: int = 15
MAX_INLINE_BYTES: int = 20 * 1024 * 1024  # 20 MiB
MAX_SAFE_INLINE_BYTES: int = 8 * 1024 * 1024  # 8 MiB
DEFAULT_SAFE_SLICE_MINUTES: float = 10.0
MAX_ONESHOT_MINUTES: float = 75.0
MAX_CONCURRENT_FFMPEG: int = 3
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
PROTOCOL_WHITELIST: frozenset[str] = frozenset({"gemini", "openai"})
OPENAI_MODE_WHITELIST: frozenset[str] = frozenset({"chat", "transcriptions"})
OPENAI_AUDIO_FORMATS: frozenset[str] = frozenset({"mp3", "wav"})

# Slices cache directory
def get_slices_cache_dir() -> Path:
    """Returns directory path for caching audio/video slices."""
    base = Path(os.environ.get("OMNI_MEDIA_CACHE_DIR") or (Path.home() / ".cache" / "omni-media" / "slices"))
    base.mkdir(parents=True, exist_ok=True)
    return base
