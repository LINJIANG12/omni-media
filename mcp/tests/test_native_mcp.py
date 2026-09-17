"""Offline contract and end-to-end tests for the native OmniMedia MCP server."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from omni_media_mcp import cli
from omni_media_mcp.core.proc import run_quiet
from omni_media_mcp.core.preprocessor import MediaPreprocessor
from omni_media_mcp.server import STATUS_CONTRACT_VERSION, inspect_media, read_audio

REPO_ROOT = Path(__file__).resolve().parent.parent
_STATUS_RE = re.compile(r"<!-- OMNI_STATUS: (\{.*?\}) -->")


@pytest.fixture(scope="session")
def audio_m4a(tmp_path_factory: pytest.TempPathFactory) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("系统 PATH 中没有 ffmpeg")
    out = tmp_path_factory.mktemp("native-media") / "sample.m4a"
    result = run_quiet(
        [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-ar", "16000", "-ac", "1",
            "-acodec", "aac", "-b:a", "32k",
            str(out),
        ],
        timeout=120,
    )
    if result.returncode != 0 or not out.is_file():
        pytest.skip(f"ffmpeg 生成样本失败: {result.stderr}")
    return out


def _status(text: str) -> dict:
    match = _STATUS_RE.search(text)
    assert match, text[:500]
    return json.loads(match.group(1))


def test_inspect_media_returns_probe_report(audio_m4a: Path):
    result = asyncio.run(inspect_media(file_path=str(audio_m4a)))
    assert "媒体文件探测报告" in result


def test_read_audio_file_channel_and_contract(audio_m4a: Path):
    result = asyncio.run(
        read_audio(
            file_path=str(audio_m4a),
            output_mode="file",
            duration_minutes=0.02,
        )
    )
    assert isinstance(result, str)
    status = _status(result)
    assert status["contract_version"] == STATUS_CONTRACT_VERSION == 1
    assert status["mode"] == "chunked"
    assert status["is_finished"] is False
    assert status["next_start_time"]
    assert Path(re.search(r"`([^`]+\.m4a)`", result).group(1)).is_file()


def test_read_audio_inline_channel(audio_m4a: Path):
    result = asyncio.run(read_audio(file_path=str(audio_m4a), output_mode="inline"))
    assert getattr(result, "data", b"")
    assert getattr(result, "_format", "") in {"mp4", "m4a"}


def test_missing_ffmpeg_is_actionable(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-winget"))
    with pytest.raises(RuntimeError, match="ffmpeg"):
        MediaPreprocessor._find_ffmpeg()


def test_print_config_is_host_neutral_json(capsys):
    assert cli.main(["print-config"]) == 0
    data = json.loads(capsys.readouterr().out)
    entry = data["mcpServers"]["omni-media"]
    assert entry["args"] == ["-m", "omni_media_mcp.server"]
    assert set(entry["env"]) == {"PYTHONPATH"}
    assert Path(entry["env"]["PYTHONPATH"]) == REPO_ROOT


def test_apply_and_unapply_use_same_generic_command(tmp_path: Path, capsys):
    host_config = tmp_path / "dsh.json"
    assert cli.main(["apply", "--target", "dsh", "--config", str(host_config), "--yes"]) == 0
    capsys.readouterr()
    entry = json.loads(host_config.read_text(encoding="utf-8"))["mcpServers"]["omni-media"]
    assert entry["args"] == ["-m", "omni_media_mcp.server"]

    assert cli.main(["unapply", "--target", "dsh", "--config", str(host_config), "--yes"]) == 0
    capsys.readouterr()
    assert "omni-media" not in json.loads(host_config.read_text(encoding="utf-8"))["mcpServers"]


def test_codex_apply_rejects_missing_bundled_skill(tmp_path: Path, monkeypatch):
    from omni_media_mcp.adapters.codex import CodexAdapter

    adapter = CodexAdapter(
        custom_config_path=tmp_path / "codex.json",
        custom_skill_dir=tmp_path / "skills",
    )
    monkeypatch.setattr(adapter, "get_bundled_skill_path", lambda: tmp_path / "missing.md")
    ok, message = adapter.apply()
    assert ok is False
    assert "缺失" in message
    assert not (tmp_path / "codex.json").exists()


def test_stdio_end_to_end(audio_m4a: Path):
    asyncio.run(_stdio_end_to_end(audio_m4a))


async def _stdio_end_to_end(audio_m4a: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "omni_media_mcp.server"],
        cwd=str(REPO_ROOT),
        env={
            "PYTHONPATH": str(REPO_ROOT),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8",
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {"read_audio", "inspect_media"}
            result = await session.call_tool(
                "inspect_media",
                {"file_path": str(audio_m4a)},
            )
            text = "\n".join(
                block.text for block in result.content if hasattr(block, "text")
            )
            assert "媒体文件探测报告" in text
