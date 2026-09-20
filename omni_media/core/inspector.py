"""Media Inspection and Token Estimation Engine."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .limits import AUDIO_EXTS, PROBE_TIMEOUT_SEC, VIDEO_EXTS
from .proc import run_quiet

logger = logging.getLogger(__name__)


@dataclass
class StreamInfo:
    codec_type: str  # "audio" | "video"
    codec_name: str
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    bitrate: Optional[int] = None


@dataclass
class MediaMetadata:
    file_path: str
    file_name: str
    file_size_bytes: int
    file_size_mb: float
    duration_seconds: float
    duration_human: str
    has_audio: bool
    has_video: bool
    format_name: str
    streams: List[Dict[str, Any]]
    estimates: Dict[str, int]
    recommended_mode: str
    recommended_provider: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_markdown(self) -> str:
        streams_desc = []
        for s in self.streams:
            if s.get("codec_type") == "video":
                fps = s.get("fps")
                fps_str = f"{fps:.1f}" if fps else "可变/未知"
                streams_desc.append(
                    f"- 📹 视频轨: `{s.get('codec_name')}` {s.get('width')}x{s.get('height')} @ {fps_str}fps"
                )
            elif s.get("codec_type") == "audio":
                streams_desc.append(
                    f"- 🔊 音频轨: `{s.get('codec_name')}` {s.get('sample_rate')}Hz {s.get('channels')}声道"
                )

        streams_str = "\n".join(streams_desc) if streams_desc else "- (无音视频轨道)"
        oneshot_rec = "✅ 推荐整片直读 (≤75分钟)" if self.duration_seconds <= 4500 else "⚠️ 超过 75 分钟建议分卷切片"

        return f"""### 📊 媒体文件探测报告: `{self.file_name}`

- **文件大小**: {self.file_size_mb:.2f} MB ({self.file_size_bytes:,} 字节)
- **媒体时长**: {self.duration_human} ({self.duration_seconds:.1f} 秒)
- **封装格式**: `{self.format_name}`
- **轨道信息**:
{streams_str}

#### 🎯 多模态 Token 预算预估:
- **音频流直读预估**: ~`{self.estimates.get('audio_tokens', 0):,}` Tokens (Gemini/Host: ~32/s, OpenAI: ~21/s)
- **视音频多模态预估**: ~`{self.estimates.get('video_tokens', 0):,}` Tokens
- **整片直读判断**: {oneshot_rec}

> 💡 **系统推荐策略**: `{self.recommended_mode}`
> 推荐通道: **{self.recommended_provider}**
"""


class MediaInspector:
    """Probes media metadata using ffprobe with fallback to ffmpeg."""

    @staticmethod
    def _find_ffprobe() -> Optional[str]:
        bin_path = shutil.which("ffprobe")
        if bin_path:
            return bin_path
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            winget_path = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
            if winget_path.exists():
                for p in winget_path.glob("**/ffprobe.exe"):
                    return str(p)
        return None

    @classmethod
    def probe(cls, file_path: str | Path) -> MediaMetadata:
        path = Path(file_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"媒体文件不存在: {file_path}")

        file_size = path.stat().st_size
        file_size_mb = file_size / (1024 * 1024)

        duration = 0.0
        format_name = path.suffix.lstrip(".").lower()
        streams: List[StreamInfo] = []

        ffprobe_bin = cls._find_ffprobe()
        if ffprobe_bin:
            cmd = [
                ffprobe_bin,
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                str(path),
            ]
            try:
                res = run_quiet(cmd, text=True, timeout=PROBE_TIMEOUT_SEC)
                if res.returncode == 0 and res.stdout:
                    data = json.loads(res.stdout)
                    fmt = data.get("format", {})
                    format_name = fmt.get("format_name", format_name)
                    duration = float(fmt.get("duration", 0.0))

                    for s in data.get("streams", []):
                        ctype = s.get("codec_type")
                        cname = s.get("codec_name", "")
                        if ctype == "video":
                            fps_val = None
                            r_frame_rate = s.get("r_frame_rate", "")
                            if "/" in r_frame_rate:
                                num, den = r_frame_rate.split("/")
                                if float(den) > 0:
                                    fps_val = float(num) / float(den)
                            streams.append(
                                StreamInfo(
                                    codec_type="video",
                                    codec_name=cname,
                                    width=s.get("width"),
                                    height=s.get("height"),
                                    fps=fps_val,
                                    bitrate=int(s.get("bit_rate")) if s.get("bit_rate") else None,
                                )
                            )
                        elif ctype == "audio":
                            streams.append(
                                StreamInfo(
                                    codec_type="audio",
                                    codec_name=cname,
                                    sample_rate=int(s.get("sample_rate")) if s.get("sample_rate") else None,
                                    channels=int(s.get("channels")) if s.get("channels") else None,
                                    bitrate=int(s.get("bit_rate")) if s.get("bit_rate") else None,
                                )
                            )
            except Exception as exc:
                logger.warning("ffprobe 探测失败，回退解析: %s", exc)

        if duration <= 0:
            ffmpeg_bin = shutil.which("ffmpeg")
            if ffmpeg_bin:
                try:
                    res = run_quiet([ffmpeg_bin, "-i", str(path)], text=True, timeout=PROBE_TIMEOUT_SEC)
                    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", res.stderr)
                    if m:
                        hours, minutes, seconds = float(m.group(1)), float(m.group(2)), float(m.group(3))
                        duration = hours * 3600 + minutes * 60 + seconds
                except Exception as exc:
                    logger.warning("ffmpeg 时长回退解析失败: %s", exc)

        has_video = any(s.codec_type == "video" for s in streams)
        has_audio = any(s.codec_type == "audio" for s in streams)
        if not streams:
            if path.suffix.lower() in VIDEO_EXTS:
                has_video = True
                has_audio = True
            elif path.suffix.lower() in AUDIO_EXTS:
                has_audio = True

        hrs = int(duration // 3600)
        mins = int((duration % 3600) // 60)
        secs = int(duration % 60)
        dur_human = f"{hrs:02d}:{mins:02d}:{secs:02d}" if hrs > 0 else f"{mins:02d}:{secs:02d}"

        # Native / Gemini: 32 tokens/sec; OpenAI: ~21 tokens/sec
        audio_tokens = int(duration * 32)
        video_tokens = int(duration * 292) if has_video else audio_tokens
        openai_audio_tokens = int(duration * 21)

        estimates = {
            "audio_tokens": audio_tokens,
            "video_tokens": video_tokens,
            "gemini_audio_tokens": audio_tokens,
            "gemini_video_tokens": video_tokens,
            "openai_audio_tokens": openai_audio_tokens,
        }

        if has_video and not has_audio:
            rec_mode = "仅画面无声：无音频轨道，需使用视觉模型直接分析画面"
            rec_prov = "宿主视觉多模态内核"
        elif has_video:
            rec_mode = "标准网课/讲座视音频：默认抽取 16kHz 单声道人声切片供直读"
            rec_prov = "音频多模态内核 (read_audio / read_media)"
        else:
            rec_mode = "纯音频文件：原生单声道直读，零重编码"
            rec_prov = "音频多模态内核 (read_audio / read_media)"

        return MediaMetadata(
            file_path=str(path),
            file_name=path.name,
            file_size_bytes=file_size,
            file_size_mb=file_size_mb,
            duration_seconds=duration,
            duration_human=dur_human,
            has_audio=has_audio,
            has_video=has_video,
            format_name=format_name,
            streams=[asdict(s) for s in streams],
            estimates=estimates,
            recommended_mode=rec_mode,
            recommended_provider=rec_prov,
        )
