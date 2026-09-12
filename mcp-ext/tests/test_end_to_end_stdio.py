"""端到端：真起 MCP stdio 服务 + 真媒体 + 仿真外部端点，跑完整调用链。

覆盖的重点是「协议之外」的一切：工具面契约、配置注入、切片与续读分页、状态注释、
以及各类非法入参必须变成 `isError` 而不是静默返回空串。

全程不需要任何真实 API Key：外部端点是进程内的 `stub_endpoint`。

测试函数写成同步 + `asyncio.run(...)`：不想为了跑测试再引入 pytest-asyncio 这类插件，
保持「装完 mcp 就能跑」的轻量前提。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from stub_endpoint import GEMINI_GENERATE, OPENAI_CHAT, OPENAI_TRANSCRIPTIONS

REPO_ROOT = Path(__file__).resolve().parent.parent

_STATUS_RE = re.compile(r"<!-- (OMNI_STATUS): (\{.*?\}) -->")


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------

def _server_params(config_path: Path) -> StdioServerParameters:
    env = {
        "PYTHONPATH": str(REPO_ROOT),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
    }
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "omni_media_ext.server", "--config", str(config_path)],
        cwd=str(REPO_ROOT),
        env=env,
    )


def _write_config(path: Path, stub) -> Path:
    data = {
        "active": "gem",
        "defaults": {"slice_minutes": 1, "max_payload_mb": 18, "timeout_sec": 30, "max_retries": 0},
        "endpoints": {
            "gem": {
                "protocol": "gemini",
                "base_url": stub.base_gemini,
                "api_key": "e2e-gemini-key-1234567890",
                "model": "gemini-e2e",
            },
            "oai": {
                "protocol": "openai",
                "openai_mode": "chat",
                "base_url": stub.base_openai,
                "api_key": "e2e-openai-key-1234567890",
                "model": "gpt-4o-audio-preview",
            },
            "whisper": {
                "protocol": "openai",
                "openai_mode": "transcriptions",
                "base_url": stub.base_openai,
                "api_key": "e2e-whisper-key-1234567890",
                "model": "whisper-1",
                "text_model": "gpt-4o-mini",
                "language": "zh",
            },
        },
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _text(result: Any) -> str:
    parts: List[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _is_error(result: Any) -> bool:
    return bool(getattr(result, "isError", None) or getattr(result, "is_error", False))


def _status_of(text: str) -> Dict[str, Any]:
    match = _STATUS_RE.search(text)
    assert match, f"返回文本里没有 OMNI_STATUS 注释:\n{text[:400]}"
    assert match.group(1) == "OMNI_STATUS", "分页标签必须与原生听音版同名（OMNI_STATUS）"
    return json.loads(match.group(2))


@pytest.fixture()
def e2e_config(tmp_path: Path, stub) -> Path:
    return _write_config(tmp_path / "config.json", stub)


# ---------------------------------------------------------------------------
# 主链路
# ---------------------------------------------------------------------------

def test_end_to_end_full_surface(e2e_config: Path, audio_m4a: Path, audio_long_m4a: Path, stub):
    asyncio.run(_full_surface(e2e_config, audio_m4a, audio_long_m4a, stub))


async def _full_surface(e2e_config: Path, audio_m4a: Path, audio_long_m4a: Path, stub) -> None:
    async with stdio_client(_server_params(e2e_config)) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.server_info.name == "OmniMedia-Ext-Server"

            # ---- 1. 工具面：恰好两个工具，且绝不出现 read_audio ----
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert names == {"read_media", "inspect_media"}, names
            assert "read_audio" not in names and "ask_media" not in names

            read_tool = next(t for t in tools.tools if t.name == "read_media")
            schema = getattr(read_tool, "input_schema", None) or read_tool.inputSchema  # 兼容两种字段名
            assert set(schema["properties"]) == {
                "file_path", "mode", "instruction", "endpoint", "start_time", "duration_minutes"
            }
            assert schema["required"] == ["file_path"]

            # ---- 2. 资源：脱敏端点清单 ----
            resources = await session.list_resources()
            uris = {str(r.uri) for r in resources.resources}
            assert "media://endpoints" in uris
            resource = await session.read_resource("media://endpoints")
            payload = json.loads(resource.contents[0].text)
            assert payload["active"] == "gem"
            assert set(payload["endpoints"]) == {"gem", "oai", "whisper"}
            dumped = json.dumps(payload, ensure_ascii=False)
            assert "e2e-gemini-key-1234567890" not in dumped
            assert "e2e-...7890" in dumped  # 前4 + ... + 后4

            # ---- 3. inspect_media（纯本地，不需要端点）----
            inspect_result = await session.call_tool("inspect_media", {"file_path": str(audio_m4a)})
            assert not _is_error(inspect_result)
            inspect_text = _text(inspect_result)
            assert "媒体文件探测报告" in inspect_text
            assert "16000" in inspect_text or "16kHz" in inspect_text
            assert "Qwen" not in inspect_text and "DeepSeek" not in inspect_text

            # ---- 4. read_media：Gemini 协议（配置里的 active）----
            stub.reset_requests()
            gem = await session.call_tool("read_media", {"file_path": str(audio_m4a), "mode": "transcribe"})
            assert not _is_error(gem), _text(gem)
            gem_text = _text(gem)
            assert "STUB-GEMINI" in gem_text
            status = _status_of(gem_text)
            assert status["endpoint"] == "gem" and status["protocol"] == "gemini"
            assert status["model"] == "gemini-e2e"
            assert status["is_finished"] is True and status["status"] == "COMPLETED"
            assert status["total_duration"].startswith("00:00:0")
            assert status["clamped"] is False
            # 与原生听音版共享的契约：mode 是**切片模式**，任务预设放在 task 里
            assert status["mode"] == "oneshot", status["mode"]
            assert status["task"] == "transcribe", status["task"]
            assert status["channel"] == "external-model"
            assert "全篇音频已处理完毕" in gem_text
            assert len(stub.requests_for(GEMINI_GENERATE)) == 1

            # ---- 5. read_media：OpenAI chat 协议（显式切换端点）----
            stub.reset_requests()
            oai = await session.call_tool(
                "read_media",
                {"file_path": str(audio_m4a), "mode": "qa", "instruction": "这一讲讲了什么？", "endpoint": "oai"},
            )
            assert not _is_error(oai), _text(oai)
            oai_text = _text(oai)
            assert "STUB-OPENAI-CHAT" in oai_text
            assert _status_of(oai_text)["protocol"] == "openai"
            chat_requests = stub.requests_for(OPENAI_CHAT)
            assert len(chat_requests) == 1
            assert chat_requests[0].json()["messages"][0]["content"][1]["type"] == "input_audio"
            # 用户 instruction 必须真的被拼进提示词
            assert "这一讲讲了什么？" in chat_requests[0].json()["messages"][0]["content"][0]["text"]

            # ---- 6. read_media：transcriptions 两段式（summarize）----
            stub.reset_requests()
            whisper = await session.call_tool(
                "read_media",
                {"file_path": str(audio_m4a), "mode": "summarize", "endpoint": "whisper"},
            )
            assert not _is_error(whisper), _text(whisper)
            whisper_text = _text(whisper)
            routes = [r.route for r in stub.requests]
            assert routes == [OPENAI_TRANSCRIPTIONS, OPENAI_CHAT], routes

            # 返回的是**第二段**（总结）的结果，模型名也应是 text_model
            assert "STUB-OPENAI-CHAT" in whisper_text
            status = _status_of(whisper_text)
            assert status["model"] == "gpt-4o-mini"

            # 但第一段产出的逐字稿必须真的被带进第二段的提示词里
            second_prompt = stub.requests_for(OPENAI_CHAT)[0].json()["messages"][0]["content"]
            assert isinstance(second_prompt, str)
            assert "STUB-ASR 第一段" in second_prompt
            assert "STUB-ASR 第二段" in second_prompt

            # ---- 7. 分页串联：130 秒媒体 + 60 秒预算 = 3 片 ----
            stub.reset_requests()
            seen_starts: List[str] = []
            start: str | None = None
            for _ in range(5):  # 上限 5 次，够读完 3 片
                arguments: Dict[str, Any] = {"file_path": str(audio_long_m4a), "mode": "transcribe"}
                if start:
                    arguments["start_time"] = start
                page = await session.call_tool("read_media", arguments)
                assert not _is_error(page), _text(page)
                page_text = _text(page)
                page_status = _status_of(page_text)
                seen_starts.append(page_status["start_time"])
                if page_status["is_finished"]:
                    break
                start = page_status["next_start_time"]
                assert "续读下一分卷参数" in page_text
                assert page_status["next_duration_minutes"] == 1.0
            else:
                pytest.fail("分页循环没有在 5 次内结束")

            assert seen_starts == ["00:00:00", "00:01:00", "00:02:00"], seen_starts
            assert len(stub.requests_for(GEMINI_GENERATE)) == 3

            # 多卷时的契约：mode 必须是 chunked，且续读字段齐备
            paged = await session.call_tool(
                "read_media", {"file_path": str(audio_long_m4a), "mode": "transcribe", "duration_minutes": 1}
            )
            paged_status = _status_of(_text(paged))
            assert paged_status["mode"] == "chunked"
            assert paged_status["is_finished"] is False
            assert paged_status["next_start_time"] == "00:01:00"

            # ---- 8. 非法入参必须是 isError，而不是空串或静默成功 ----
            cases = [
                ({"file_path": str(audio_m4a), "mode": "telepathy"}, "不支持的 mode"),
                ({"file_path": str(audio_m4a), "mode": "custom"}, "custom"),
                ({"file_path": str(audio_m4a), "endpoint": "ghost"}, "未知端点"),
                ({"file_path": str(audio_m4a), "duration_minutes": 0}, "必须大于 0"),
                ({"file_path": str(audio_m4a), "duration_minutes": -1}, "必须大于 0"),
                ({"file_path": str(audio_m4a), "start_time": "99:00:00"}, "超出媒体时长"),
                ({"file_path": str(audio_m4a.parent / "nope.m4a")}, "不存在"),
                ({"file_path": str(Path(__file__))}, "不支持的文件扩展名"),
            ]
            for arguments, needle in cases:
                failed = await session.call_tool("read_media", arguments)
                assert _is_error(failed), f"这批入参本应报错: {arguments}"
                assert needle in _text(failed), (arguments, _text(failed)[:300])


# ---------------------------------------------------------------------------
# 配置坏掉时的行为
# ---------------------------------------------------------------------------

def test_broken_config_keeps_inspect_working(tmp_path: Path, audio_m4a: Path):
    """配置缺失/非法时：read_media 明确报错，但本地探测能力不受影响。"""
    asyncio.run(_broken_config(tmp_path / "nope.json", audio_m4a))


async def _broken_config(missing: Path, audio_m4a: Path) -> None:
    async with stdio_client(_server_params(missing)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            inspect_result = await session.call_tool("inspect_media", {"file_path": str(audio_m4a)})
            assert not _is_error(inspect_result)
            assert "媒体文件探测报告" in _text(inspect_result)

            read_result = await session.call_tool("read_media", {"file_path": str(audio_m4a)})
            assert _is_error(read_result)
            assert "--config 指定的配置文件不存在" in _text(read_result)

            resource = await session.read_resource("media://endpoints")
            assert "配置不可用" in resource.contents[0].text


def test_endpoint_without_key_errors_before_network(tmp_path: Path, stub, audio_m4a: Path):
    """端点未填密钥时，应在发请求之前就给出可操作的报错。"""
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "active": "gem",
                "endpoints": {
                    "gem": {
                        "protocol": "gemini",
                        "base_url": stub.base_gemini,
                        "api_key": "REPLACE_ME",
                        "model": "gemini-e2e",
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    asyncio.run(_no_key(config, stub, audio_m4a))


async def _no_key(config: Path, stub, audio_m4a: Path) -> None:
    async with stdio_client(_server_params(config)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("read_media", {"file_path": str(audio_m4a)})
            assert _is_error(result)
            assert "未配置 api_key" in _text(result) or "REPLACE_ME" in _text(result)

    # 关键：一个请求都不应该发出去
    assert stub.requests == []
