"""pytest 共享装置：把仓库根放进 sys.path，并提供媒体样本与仿真端点。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from omni_media_ext.core.proc import run_quiet  # noqa: E402
from stub_endpoint import StubEndpoint  # noqa: E402


@pytest.fixture()
def stub() -> StubEndpoint:
    """进程内仿真端点。"""
    with StubEndpoint() as server:
        yield server


@pytest.fixture(scope="session")
def audio_m4a(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """用 ffmpeg 合成一段 3 秒的 16kHz 单声道 m4a，作为真实媒体样本。

    用真媒体而不是空文件：这样 `MediaInspector.probe` 能给出真实时长，切片/透传/
    载荷守卫这些逻辑才真的被走到。
    """
    return _synthesize(tmp_path_factory, "sample_16k.m4a", 3)


@pytest.fixture(scope="session")
def audio_long_m4a(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """130 秒的样本：配 60 秒预算正好切成 3 片，用来验证续读分页串联。"""
    return _synthesize(tmp_path_factory, "long_16k.m4a", 130)


def _synthesize(tmp_path_factory: pytest.TempPathFactory, name: str, seconds: int) -> Path:
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("系统 PATH 中没有 ffmpeg，跳过需要真实媒体的测试")

    out = tmp_path_factory.mktemp("media") / name
    result = run_quiet(
        [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-ar", "16000", "-ac", "1",
            "-acodec", "aac", "-b:a", "32k",
            str(out),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )
    if result.returncode != 0 or not out.exists():
        pytest.skip(f"ffmpeg 生成样本失败: {result.stderr}")
    return out
