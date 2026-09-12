"""Centralized safety limits, whitelists, and shared constants for OmniMedia-Ext.

This module is the single source of truth for subprocess timeouts, media-extension
whitelists, and enum-like whitelists so callers (server.py, config.py, core helpers)
do not duplicate magic numbers or hardcoded extension sets.

与原生听音版（omni-media-mcp）的差异：本版本把音频交给**外部模型**处理，因此

* 不再有「内联 Audio 数据块」相关的体积上限（MAX_INLINE_BYTES / MAX_SAFE_INLINE_BYTES）；
* 不再有 One-Shot 阈值（MAX_ONESHOT_MINUTES）与「默认安全切片」常量，
  切片预算改由配置文件 `defaults.slice_minutes` 决定（这里只留兜底默认值）；
* 不再有 `get_slices_cache_dir()`：切片全部走 ManagedTempDir，退出即清，
  **不往用户主目录写任何缓存**。

本模块中的 DEFAULT_* 值只是配置缺省项，运行期一律以配置文件为准。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Resource-safety limits
# ---------------------------------------------------------------------------

# Maximum wall-clock seconds allowed for any single ffmpeg/ffprobe subprocess.
# Prevents a runaway encode from blocking the MCP event loop indefinitely.
SUBPROCESS_TIMEOUT_SEC: int = 300

# Maximum wall-clock seconds for a metadata probe (ffprobe / ffmpeg -i). Probes
# must fail fast: they run on the request path before any real work begins.
PROBE_TIMEOUT_SEC: int = 15

# Maximum concurrent FFmpeg processes to prevent CPU/IO thrashing under multi-agent workloads
MAX_CONCURRENT_FFMPEG: int = 3

# High-compression voice bitrate for speech LLM perception
AUDIO_BITRATE_VOICE: str = "32k"

# ---------------------------------------------------------------------------
# 外部模型请求预算（配置文件 defaults 段的缺省值）
# ---------------------------------------------------------------------------

# 单次请求携带的媒体体积上限（MiB）。超过时由 server 侧自动收窄本次切片时长。
DEFAULT_MAX_PAYLOAD_MB: int = 18

# 未显式传 duration_minutes 时的单片时长预算（分钟）。
DEFAULT_SLICE_MINUTES: float = 10.0

# 单次 HTTP 请求超时（秒）。
DEFAULT_TIMEOUT_SEC: int = 300

# 仅对 429/5xx/网络异常重试的次数（4xx 鉴权类错误绝不重试）。
DEFAULT_MAX_RETRIES: int = 2

# HTTP 错误响应体回显上限（字符），避免把整页 HTML 塞进异常信息。
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
# Whitelists for free-form string parameters exposed as MCP tool arguments
# ---------------------------------------------------------------------------

# read_media 的任务预设。'custom' 不预置提示词，完全依赖调用方传入的 instruction。
MODE_WHITELIST: frozenset[str] = frozenset({"transcribe", "summarize", "qa", "custom"})

# 支持的线上协议；新增协议必须同时落到 providers/registry.py 与配置校验里。
PROTOCOL_WHITELIST: frozenset[str] = frozenset({"gemini", "openai"})

# OpenAI 协议的两种线上形状。
OPENAI_MODE_WHITELIST: frozenset[str] = frozenset({"chat", "transcriptions"})

# OpenAI chat 模式 input_audio 只接受这两种容器。
OPENAI_AUDIO_FORMATS: frozenset[str] = frozenset({"mp3", "wav"})
