"""High-speed Media Preprocessing Engine using FFmpeg."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

from .limits import AUDIO_BITRATE_VOICE, MAX_CONCURRENT_FFMPEG, SUBPROCESS_TIMEOUT_SEC
from .proc import run_quiet

# FFmpeg 并发闸：全进程单一实例。并发上限是模块常量 `MAX_CONCURRENT_FFMPEG`，
# 不提供运行时改档口——原先的 `set_max_concurrency` 与配置键 `max_concurrency` 从未被调用过，
# 留着只会让「配了却不生效」变成陷阱。
_FFMPEG_LOCK = threading.BoundedSemaphore(MAX_CONCURRENT_FFMPEG)


def get_ffmpeg_lock() -> threading.BoundedSemaphore:
    """Retrieve current active FFmpeg semaphore."""
    return _FFMPEG_LOCK


def _atomic_replace_file(src: Path, dst: Path, retries: int = 4, delay: float = 0.25) -> None:
    """Safely replace dst with src on Windows and POSIX, handling transient file locks."""
    for attempt in range(retries):
        try:
            os.replace(src, dst)
            return
        except OSError:
            if attempt == retries - 1:
                raise
            time.sleep(delay * (attempt + 1))


class MediaPreprocessor:
    """Performs fast stream extractions and transcodings with FFmpeg."""

    @staticmethod
    def _find_ffmpeg() -> str:
        bin_path = shutil.which("ffmpeg")
        if bin_path:
            return bin_path
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            winget_path = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
            if winget_path.exists():
                for p in winget_path.glob("**/ffmpeg.exe"):
                    return str(p)
        raise RuntimeError("系统未找到 ffmpeg，请确保 ffmpeg 已安装并加入系统 PATH。")

    @staticmethod
    def parse_time_str(t: str | int | float | None) -> Optional[float]:
        if t is None:
            return None
        if isinstance(t, (int, float)):
            if t < 0:
                raise ValueError(f"时间戳不能为负数: {t}")
            return float(t)
        s = str(t).strip()
        if not s:
            return None
        parts = s.split(":")
        try:
            if len(parts) == 3:
                seconds = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            elif len(parts) == 2:
                seconds = float(parts[0]) * 60 + float(parts[1])
            else:
                seconds = float(parts[0])
        except ValueError:
            raise ValueError(f"无法解析时间戳格式: '{t}'，请使用 'HH:MM:SS' 或秒数")
        if seconds < 0:
            raise ValueError(f"时间戳不能为负数: {t}")
        return seconds

    @staticmethod
    def format_time_str(seconds: float) -> str:
        sec = max(0.0, seconds)
        hrs = int(sec // 3600)
        mins = int((sec % 3600) // 60)
        rem_sec = int(sec % 60)
        return f"{hrs:02d}:{mins:02d}:{rem_sec:02d}"

    @classmethod
    def extract_optimized_audio(
        cls,
        input_file: str | Path,
        output_file: Optional[str | Path] = None,
        start_time: Optional[str | int | float] = None,
        duration_seconds: Optional[float] = None,
        bitrate: str = AUDIO_BITRATE_VOICE,
    ) -> Path:
        """Extracts 16kHz mono AAC audio (ideal for multimodal speech LLM)."""
        src = Path(input_file).resolve()
        if not src.exists():
            raise FileNotFoundError(f"输入文件不存在: {input_file}")

        if output_file:
            dst = Path(output_file).resolve()
            dst.parent.mkdir(parents=True, exist_ok=True)
        else:
            dst = src.parent / f"{src.stem}_16k_mono.m4a"

        tmp_id = f"tmp_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        tmp_dst = dst.parent / f"{dst.stem}.{tmp_id}{dst.suffix}"

        ffmpeg = cls._find_ffmpeg()
        cmd = [ffmpeg, "-y"]

        if start_time is not None:
            cmd.extend(["-ss", str(start_time)])

        cmd.extend(["-i", str(src)])

        if duration_seconds is not None:
            cmd.extend(["-t", str(duration_seconds)])

        cmd.extend([
            "-vn",
            "-acodec", "aac",
            "-ar", "16000",
            "-ac", "1",
            "-b:a", str(bitrate),
            str(tmp_dst),
        ])

        try:
            with get_ffmpeg_lock():
                res = run_quiet(cmd, text=True, timeout=SUBPROCESS_TIMEOUT_SEC)
            if res.returncode != 0 or not tmp_dst.exists() or tmp_dst.stat().st_size == 0:
                raise RuntimeError(f"FFmpeg 音频抽取失败: {res.stderr}")
            _atomic_replace_file(tmp_dst, dst)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"FFmpeg 音频抽取超时 (>{SUBPROCESS_TIMEOUT_SEC}s)") from e
        finally:
            if tmp_dst.exists():
                try:
                    tmp_dst.unlink(missing_ok=True)
                except OSError:
                    pass

        return dst

    @classmethod
    def transcode_to_mp3(
        cls,
        input_file: str | Path,
        output_file: Optional[str | Path] = None,
        start_time: Optional[str | int | float] = None,
        duration_seconds: Optional[float] = None,
        bitrate: str = "64k",
    ) -> Path:
        """Transcodes media to 16kHz mono MP3 (required for OpenAI input_audio)."""
        src = Path(input_file).resolve()
        if not src.exists():
            raise FileNotFoundError(f"输入文件不存在: {input_file}")

        if output_file:
            dst = Path(output_file).resolve()
            dst.parent.mkdir(parents=True, exist_ok=True)
        else:
            dst = src.parent / f"{src.stem}_16k_mono.mp3"

        tmp_id = f"tmp_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        tmp_dst = dst.parent / f"{dst.stem}.{tmp_id}{dst.suffix}"

        ffmpeg = cls._find_ffmpeg()
        cmd = [ffmpeg, "-y"]

        if start_time is not None:
            cmd.extend(["-ss", str(start_time)])

        cmd.extend(["-i", str(src)])

        if duration_seconds is not None:
            cmd.extend(["-t", str(duration_seconds)])

        cmd.extend([
            "-vn",
            "-acodec", "libmp3lame",
            "-ar", "16000",
            "-ac", "1",
            "-b:a", str(bitrate),
            str(tmp_dst),
        ])

        try:
            with get_ffmpeg_lock():
                res = run_quiet(cmd, text=True, timeout=SUBPROCESS_TIMEOUT_SEC)
            if res.returncode != 0 or not tmp_dst.exists() or tmp_dst.stat().st_size == 0:
                raise RuntimeError(f"FFmpeg MP3 转码失败: {res.stderr}")
            _atomic_replace_file(tmp_dst, dst)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"FFmpeg MP3 转码超时 (>{SUBPROCESS_TIMEOUT_SEC}s)") from e
        finally:
            if tmp_dst.exists():
                try:
                    tmp_dst.unlink(missing_ok=True)
                except OSError:
                    pass

        return dst
