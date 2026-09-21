"""pytest 共享装置：把仓库根放进 sys.path，并提供媒体样本、仿真端点与隔离配置。

迁移自旧 `mcp-ext/tests/conftest.py`（导入前缀 `omni_media` → `omni_media`），
并补上原生侧 `mcp/tests/` 需要的同一套媒体样本——两个旧子包合并后共用一个 tests/，
样本只合成一次即可。
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from omni_media.core.proc import run_quiet  # noqa: E402
from endpoint_config import write_ext_config  # noqa: E402
from stub_endpoint import StubEndpoint  # noqa: E402

_CONFIG_COUNTER = itertools.count(1)


@pytest.fixture()
def stub() -> StubEndpoint:
    """进程内仿真端点（标准库 http.server，本地回环，零网络零密钥）。"""
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


@pytest.fixture(scope="session")
def audio_oversize_m4a(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """240 秒的样本：整段作为一个切片提交时，会超过 1 MB 预算的载荷守卫线。

    守卫线是 `max_payload_bytes / inflation`：预算取下界 1 MB 时约 747 KiB。
    240 秒切片转 mp3（64k）约 1.9 MB，稳稳越线。
    以前触发守卫靠把 `max_payload_mb` 写成 0 把预算退化成 1 字节；配置校验上线后
    `max_payload_mb` 必须 > 0，那个技巧不再合法，所以改用真实超限样本。
    """
    return _synthesize(tmp_path_factory, "oversize_16k.m4a", 240)


@pytest.fixture()
def ext_config(tmp_path: Path, stub: StubEndpoint) -> Path:
    """一份指向仿真端点的外部端点配置（默认 active=gem，slice_minutes=1）。"""
    return write_ext_config(tmp_path / "ext-config.json", stub)


@pytest.fixture()
def make_ext_config(tmp_path: Path, stub: StubEndpoint):
    """按需生成多份互不覆盖的仿真端点配置。

    用法：``cfg = make_ext_config(active="whisper")``。
    """

    def _make(
        *,
        active: str = "gem",
        slice_minutes: float = 1.0,
        max_payload_mb: int = 18,
        timeout_sec: int = 30,
        max_retries: int = 0,
        endpoints: Optional[Dict[str, Dict[str, Any]]] = None,
        name: Optional[str] = None,
    ) -> Path:
        target = tmp_path / (name or f"ext-config-{next(_CONFIG_COUNTER)}.json")
        return write_ext_config(
            target,
            stub,
            active=active,
            slice_minutes=slice_minutes,
            max_payload_mb=max_payload_mb,
            timeout_sec=timeout_sec,
            max_retries=max_retries,
            endpoints=endpoints,
        )

    return _make


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


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
