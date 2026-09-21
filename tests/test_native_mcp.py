"""原生听音通道的离线契约与端到端测试。

**为什么从工具注册表取函数**：统一包把 `inspect_media` / `read_audio` 收进了
`create_server()` 的闭包（这样才能按 `--mode` 决定注册哪些工具），它们不再是模块级名字。
测试因此统一走 `create_server(mode=...)._tool_manager._tools[name].fn`——
这也顺带断言了「原生通道的工具面确实只有 read_audio + inspect_media」。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from omni_media import cli
from omni_media.core.preprocessor import MediaPreprocessor
from omni_media.server import STATUS_CONTRACT_VERSION, create_server

REPO_ROOT = Path(__file__).resolve().parent.parent
# 原生入口的 stdio 参数：注册名 omni-media 必须与 `--mode native` 成对出现，
# 否则宿主看到的是 all 模式（多出一个 read_media），「装的是哪条通道」就看不出来了。
_STDIO_ARGS = ["-m", "omni_media.server", "--mode", "native"]
_STATUS_RE = re.compile(r"<!-- OMNI_STATUS: (\{.*?\}) -->")


def _tool(mode: str, name: str) -> Callable[..., Any]:
    tools: Dict[str, Any] = create_server(mode=mode)._tool_manager._tools
    assert name in tools, f"{mode} 模式未注册 {name}，实际: {sorted(tools)}"
    return tools[name].fn


def _read_audio() -> Callable[..., Any]:
    return _tool("native", "read_audio")


def _inspect_media() -> Callable[..., Any]:
    return _tool("native", "inspect_media")


def _status(text: str) -> dict:
    match = _STATUS_RE.search(text)
    assert match, text[:500]
    return json.loads(match.group(1))


def test_inspect_media_returns_probe_report(audio_m4a: Path):
    result = asyncio.run(_inspect_media()(file_path=str(audio_m4a)))
    assert "媒体文件探测报告" in result


def test_read_audio_file_channel_and_contract(audio_m4a: Path):
    result = asyncio.run(
        _read_audio()(
            file_path=str(audio_m4a),
            output_mode="file",
            duration_minutes=0.02,
        )
    )
    assert isinstance(result, str)
    status = _status(result)
    assert status["contract_version"] == STATUS_CONTRACT_VERSION == 1
    # 原生通道回传的是**音频切片**而不是文本，所以状态里没有 ext 通道那个
    # `mode`（transcribe/summarize/qa），改用 `channel` 说明回传通道。
    assert status["channel"] == "file"
    assert status["is_finished"] is False
    assert status["next_start_time"]
    assert Path(re.search(r"`([^`]+\.m4a)`", result).group(1)).is_file()


def test_read_audio_inline_channel(audio_m4a: Path):
    result = asyncio.run(_read_audio()(file_path=str(audio_m4a), output_mode="inline"))
    assert getattr(result, "data", b"")
    assert getattr(result, "_format", "") in {"mp4", "m4a"}


def test_missing_ffmpeg_is_actionable(tmp_path: Path, monkeypatch):
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda _name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-winget"))
    with pytest.raises(RuntimeError, match="ffmpeg"):
        MediaPreprocessor._find_ffmpeg()


def test_print_config_is_host_neutral_json(capsys):
    assert cli.main(["print-config"]) == 0
    data = json.loads(capsys.readouterr().out)
    entry = data["mcpServers"]["omni-media"]
    # 注册名 omni-media ⇒ 入口必须把 --mode native 一起下发，不能只写模块名
    assert entry["args"] == _STDIO_ARGS
    assert set(entry["env"]) == {"PYTHONPATH"}
    assert Path(entry["env"]["PYTHONPATH"]) == REPO_ROOT


def test_apply_and_unapply_use_same_generic_command(tmp_path: Path, capsys):
    host_config = tmp_path / "dsh.json"
    assert cli.main(
        ["apply", "--target", "dsh", "--config-path", str(host_config), "--yes"]
    ) == 0
    capsys.readouterr()
    entry = json.loads(host_config.read_text(encoding="utf-8"))["mcpServers"]["omni-media"]
    assert entry["args"] == _STDIO_ARGS

    assert cli.main(
        ["unapply", "--target", "dsh", "--config-path", str(host_config)]
    ) == 0
    capsys.readouterr()
    assert "omni-media" not in json.loads(
        host_config.read_text(encoding="utf-8")
    )["mcpServers"]


def test_adapter_preview_reports_not_registered_and_does_not_write(tmp_path: Path):
    """未接入的宿主：预览要如实报告「尚未注册」，且**不落盘**。

    统一包把各宿主适配器收敛成一个泛型 `BaseHostAdapter`，删掉了旧的
    `apply()` 与 codex 专属的「随包技能文件」概念（`get_bundled_skill_path` 已不存在）；
    写入路径现在是 `preview_apply()` → `write_config()` 两步，由 CLI 串起来。
    """
    from omni_media.adapters.registry import get_adapter

    host_config = tmp_path / "codex.json"
    adapter = get_adapter("codex", custom_config_path=host_config)

    ok, message, data = adapter.preview_apply()
    assert ok is True
    assert "omni-media" in data["mcpServers"]
    # 预览阶段只读不写：文件必须还没被创建
    assert not host_config.exists()
    assert adapter.is_registered() is False
    assert message


def test_stdio_end_to_end(audio_m4a: Path):
    asyncio.run(_stdio_end_to_end(audio_m4a))


async def _stdio_end_to_end(audio_m4a: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=_STDIO_ARGS,
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
