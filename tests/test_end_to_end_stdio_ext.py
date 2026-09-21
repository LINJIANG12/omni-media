"""端到端：真起 MCP stdio 服务（`mode=ext`）+ 真媒体 + 仿真外部端点，跑完整调用链。

覆盖的重点是「协议之外」的一切：工具面契约、配置注入、切片与续读分页、状态注释、
直写落盘、以及各类非法入参不得静默成功。

全程不需要任何真实 API Key：外部端点是进程内的 `stub_endpoint`，配置由
`endpoint_config.write_ext_config` 写到 tmp_path——**绝不**走仓库根的本地实配
`config.json`（那份含明文密钥，会让测试真出网）。

迁移自 `mcp-ext/tests/test_end_to_end_stdio.py`。除导入前缀与 `--mode ext` 外，
旧文件有三处必须按新事实改写：

1. 工具面：`read_media` 的参数是 `prompt`（旧版叫 `instruction`），并新增 `output_file`；
2. 状态载荷：统一后的键集是 `current_start/current_duration/...`，旧版的 `status` /
   `mode` / `task` / `channel` / `clamped` / `total_duration` 都不存在；
3. 错误可观测性：mcp 2.x 把工具里的**意外异常**当成崩溃，只把
   `Error executing tool <name>` 交给客户端；**`ToolError` 才带正文**。第二阶段 B6 起，
   `read_media` / `read_audio` 把预期内的失败（非法入参、配置坏掉、上游失败）统一翻成
   `ToolError`，所以传输层现在也能读到可操作的原因——见
   `test_broken_config_keeps_local_probe_working` 与
   `test_invalid_arguments_become_tool_errors_without_any_request`。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from typing import Any, Dict

import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver.exceptions import ToolError

from endpoint_config import stub_endpoints
from omni_media.server import create_server
from stdio_support import (
    body_of,
    is_error,
    server_params,
    status_of,
    text_of,
)
from stub_endpoint import GEMINI_GENERATE, OPENAI_CHAT, OPENAI_TRANSCRIPTIONS, StubEndpoint

REPO_ROOT = Path(__file__).resolve().parent.parent

_READ_MEDIA_PROPERTIES = {
    "file_path", "prompt", "mode", "endpoint", "start_time", "duration_minutes", "output_file",
}


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def _session(config_path: Path, mode: str = "ext"):
    async with stdio_client(server_params(mode, config_path)) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            yield session, init


def _read_media_tool(config_path: Path):
    """进程内拿到 `read_media` 的真实现（`create_server` 注册的就是同一个可调用体）。

    第二阶段 B6 起，预期内的失败在这里已经是 `ToolError`——传输层不再把它压成通用文案，
    所以「报错必须说清原因」既可以在这里断言，也可以在 stdio 客户端侧断言。
    """
    return create_server(mode="ext", config_path=str(config_path))._tool_manager._tools["read_media"].fn


def _input_schema(tool: Any) -> Dict[str, Any]:
    return getattr(tool, "input_schema", None) or tool.inputSchema


# ---------------------------------------------------------------------------
# 工具面
# ---------------------------------------------------------------------------

def test_ext_mode_tool_surface(ext_config: Path):
    asyncio.run(_assert_surface(ext_config))


async def _assert_surface(config_path: Path) -> None:
    async with _session(config_path) as (session, init):
        assert init.server_info.name == "OmniMedia-Server"

        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert names == {"read_media", "inspect_media"}, names
        assert "read_audio" not in names and "ask_media" not in names

        read_tool = next(tool for tool in tools.tools if tool.name == "read_media")
        schema = _input_schema(read_tool)
        assert set(schema["properties"]) == _READ_MEDIA_PROPERTIES
        assert schema["required"] == ["file_path"]

        annotations = read_tool.annotations
        # readOnlyHint=False 是 B4 的修复：传 `output_file` 时会写盘，注解如实反映。
        assert annotations.read_only_hint is False
        assert annotations.destructive_hint is False
        assert annotations.idempotent_hint is True
        assert annotations.open_world_hint is True, "代读通道会连外部模型"

        inspect_annotations = next(tool for tool in tools.tools if tool.name == "inspect_media").annotations
        assert inspect_annotations.read_only_hint is True
        assert inspect_annotations.open_world_hint is False

        # 统一包不再注册任何 MCP resource（旧版的脱敏端点清单 media://endpoints 已删除：
        # 端点详情改由 `omni-media config show` 提供，MCP 面只留工具）
        resources = await session.list_resources()
        assert {str(r.uri) for r in resources.resources} == set()


def test_ext_stdio_has_no_native_audio_tool(audio_m4a: Path, ext_config: Path):
    asyncio.run(_assert_unknown_tool(ext_config, "read_audio", audio_m4a))


async def _assert_unknown_tool(config_path: Path, tool_name: str, media: Path) -> None:
    async with _session(config_path) as (session, _):
        result = await session.call_tool(tool_name, {"file_path": str(media)})
        assert is_error(result)
        assert f"Unknown tool: {tool_name}" in text_of(result)


def test_inspect_media_works_without_any_endpoint(ext_config: Path, audio_m4a: Path):
    asyncio.run(_assert_inspect(ext_config, audio_m4a))


async def _assert_inspect(config_path: Path, media: Path) -> None:
    async with _session(config_path) as (session, _):
        result = await session.call_tool("inspect_media", {"file_path": str(media)})
        assert not is_error(result), text_of(result)
        text = text_of(result)
        assert "媒体文件探测报告" in text
        assert "16000" in text or "16kHz" in text
        assert "Qwen" not in text and "DeepSeek" not in text


# ---------------------------------------------------------------------------
# read_media：三种线上协议
# ---------------------------------------------------------------------------

def test_read_media_gemini_transcribe(stub: StubEndpoint, ext_config: Path, audio_m4a: Path):
    asyncio.run(_gemini_transcribe(stub, ext_config, audio_m4a))


async def _gemini_transcribe(stub: StubEndpoint, config_path: Path, media: Path) -> None:
    stub.reset_requests()
    async with _session(config_path) as (session, _):
        result = await session.call_tool("read_media", {"file_path": str(media), "mode": "transcribe"})
        assert not is_error(result), text_of(result)
        text = text_of(result)
        assert "STUB-GEMINI" in text

        status = status_of(text)
        assert status["contract_version"] == 1
        assert status["endpoint"] == "gem"
        assert status["protocol"] == "gemini"
        assert status["model"] == "gemini-stub"
        assert status["current_start"] == "00:00:00"
        assert status["current_duration"] == "00:00:03"
        assert status["is_finished"] is True
        assert status["next_start_time"] is None
        assert status["output_file"] is None
        assert status["chars_written"] == len(body_of(text))

    assert [request.route for request in stub.requests] == [GEMINI_GENERATE]


def test_read_media_openai_chat_honours_prompt_and_endpoint(
    stub: StubEndpoint, ext_config: Path, audio_m4a: Path
):
    asyncio.run(_openai_chat(stub, ext_config, audio_m4a))


async def _openai_chat(stub: StubEndpoint, config_path: Path, media: Path) -> None:
    stub.reset_requests()
    instruction = "这一讲讲了什么？"
    async with _session(config_path) as (session, _):
        result = await session.call_tool(
            "read_media",
            {"file_path": str(media), "mode": "qa", "prompt": instruction, "endpoint": "oai"},
        )
        assert not is_error(result), text_of(result)
        text = text_of(result)
        assert "STUB-OPENAI-CHAT" in text

        status = status_of(text)
        assert status["endpoint"] == "oai"
        assert status["protocol"] == "openai"
        assert status["model"] == "gpt-4o-audio-preview"

    chat_requests = stub.requests_for(OPENAI_CHAT)
    assert len(chat_requests) == 1
    content = chat_requests[0].json()["messages"][0]["content"]
    assert [block["type"] for block in content] == ["text", "input_audio"]
    # 用户 prompt 必须真的被拼进提示词（mode_prompt 的职责）
    assert instruction in content[0]["text"]


def test_read_media_transcriptions_two_stage_summarize(
    stub: StubEndpoint, ext_config: Path, audio_m4a: Path
):
    asyncio.run(_two_stage(stub, ext_config, audio_m4a))


async def _two_stage(stub: StubEndpoint, config_path: Path, media: Path) -> None:
    stub.reset_requests()
    async with _session(config_path) as (session, _):
        result = await session.call_tool(
            "read_media", {"file_path": str(media), "mode": "summarize", "endpoint": "whisper"}
        )
        assert not is_error(result), text_of(result)
        text = text_of(result)
        assert "STUB-OPENAI-CHAT" in text

        status = status_of(text)
        # status 里的 `model` 报的是**端点配置的模型**（whisper-1），不是第二段实际用的
        # text_model。第二段用的是哪个模型，要看真正发出去的那个请求体。
        assert status["model"] == "whisper-1"

    routes = [request.route for request in stub.requests]
    assert routes == [OPENAI_TRANSCRIPTIONS, OPENAI_CHAT], routes

    # 第二段必须用 text_model 走 chat（两段式的意义就在这里）
    assert stub.requests_for(OPENAI_CHAT)[0].json()["model"] == "gpt-4o-mini"

    # 第一段产出的逐字稿必须真的被带进第二段的提示词里
    second_prompt = stub.requests_for(OPENAI_CHAT)[0].json()["messages"][0]["content"]
    assert isinstance(second_prompt, str)
    assert "STUB-ASR 第一段" in second_prompt
    assert "STUB-ASR 第二段" in second_prompt


# ---------------------------------------------------------------------------
# read_media：直写落盘（output_file）
# ---------------------------------------------------------------------------

def test_read_media_direct_to_disk_returns_receipt_only(
    ext_config: Path, audio_m4a: Path, tmp_path: Path
):
    asyncio.run(_direct_to_disk(ext_config, audio_m4a, tmp_path))


async def _direct_to_disk(config_path: Path, media: Path, tmp_path: Path) -> None:
    target = tmp_path / "notes" / "transcript.md"
    async with _session(config_path) as (session, _):
        result = await session.call_tool(
            "read_media",
            {"file_path": str(media), "mode": "transcribe", "output_file": str(target)},
        )
        assert not is_error(result), text_of(result)
        text = text_of(result)

    assert target.is_file(), "必须真的把全文写进目标文件"
    written = target.read_text(encoding="utf-8")
    assert "STUB-GEMINI" in written

    status = status_of(text)
    assert status["output_file"] == target.as_posix()
    assert status["chars_written"] == len(written)
    assert "Direct-to-Disk" in text
    assert "已由 MCP 直写磁盘" in text
    # 回执里不得再重复全文——这正是 output_file 存在的理由
    assert "STUB-GEMINI" not in text


def test_read_media_direct_to_disk_appends_on_continuation(
    ext_config: Path, audio_long_m4a: Path, tmp_path: Path
):
    asyncio.run(_append_pages(ext_config, audio_long_m4a, tmp_path))


async def _append_pages(config_path: Path, media: Path, tmp_path: Path) -> None:
    target = tmp_path / "append.md"
    async with _session(config_path) as (session, _):
        first = await session.call_tool(
            "read_media",
            {"file_path": str(media), "mode": "transcribe", "output_file": str(target), "duration_minutes": 1},
        )
        assert not is_error(first), text_of(first)
        first_content = target.read_text(encoding="utf-8")
        assert first_content.count("STUB-GEMINI") == 1
        assert status_of(text_of(first))["is_finished"] is False

        second = await session.call_tool(
            "read_media",
            {
                "file_path": str(media),
                "mode": "transcribe",
                "output_file": str(target),
                "start_time": "00:01:00",
                "duration_minutes": 1,
            },
        )
        assert not is_error(second), text_of(second)
        assert status_of(text_of(second))["chars_written"] == len(target.read_text(encoding="utf-8"))

    content = target.read_text(encoding="utf-8")
    assert content.count("STUB-GEMINI") == 2, "续卷必须追加而不是覆写"
    assert content.startswith(first_content)
    assert "\n\n" in content


# ---------------------------------------------------------------------------
# 分页续读：130 秒媒体 + 60 秒预算 = 3 片
# ---------------------------------------------------------------------------

def test_read_media_pagination_walks_three_pages(
    stub: StubEndpoint, ext_config: Path, audio_long_m4a: Path
):
    asyncio.run(_paginate(stub, ext_config, audio_long_m4a))


async def _paginate(stub: StubEndpoint, config_path: Path, media: Path) -> None:
    expected_next = {
        "00:00:00": ("00:01:00", 1.0),   # 剩余 70s，预算上限仍是 1 分钟
        "00:01:00": ("00:02:00", 0.17),  # 剩余 10s，只够 0.17 分钟
    }

    stub.reset_requests()
    seen_starts = []
    start = None
    async with _session(config_path) as (session, _):
        for _ in range(5):  # 上限 5 次，够读完 3 片
            arguments: Dict[str, Any] = {"file_path": str(media), "mode": "transcribe"}
            if start:
                arguments["start_time"] = start
            page = await session.call_tool("read_media", arguments)
            assert not is_error(page), text_of(page)

            page_text = text_of(page)
            status = status_of(page_text)
            seen_starts.append(status["current_start"])
            if status["is_finished"]:
                break
            expected_start, expected_budget = expected_next[status["current_start"]]
            assert status["next_start_time"] == expected_start
            assert status["next_duration_minutes"] == pytest.approx(expected_budget, abs=0.01)
            assert "分卷续读提示" in page_text
            start = status["next_start_time"]
        else:
            pytest.fail("分页循环没有在 5 次内结束")

    assert seen_starts == ["00:00:00", "00:01:00", "00:02:00"], seen_starts
    assert len(stub.requests_for(GEMINI_GENERATE)) == 3


# ---------------------------------------------------------------------------
# 非法入参
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "case",
    ["bad_mode", "missing_file", "unsupported_ext", "unknown_endpoint", "relative_output_file"],
)
def test_invalid_arguments_become_tool_errors_without_any_request(
    case: str, stub: StubEndpoint, ext_config: Path, audio_m4a: Path, tmp_path: Path
):
    asyncio.run(_invalid_argument(case, stub, ext_config, audio_m4a, tmp_path))


async def _invalid_argument(
    case: str, stub: StubEndpoint, config_path: Path, media: Path, tmp_path: Path
) -> None:
    arguments: Dict[str, Any] = {
        "bad_mode": {"file_path": str(media), "mode": "telepathy"},
        "missing_file": {"file_path": str(tmp_path / "nope.m4a")},
        "unsupported_ext": {"file_path": str(Path(__file__))},
        "unknown_endpoint": {"file_path": str(media), "endpoint": "ghost"},
        "relative_output_file": {"file_path": str(media), "output_file": "relative/out.md"},
    }[case]

    stub.reset_requests()
    async with _session(config_path) as (session, _):
        result = await session.call_tool("read_media", arguments)

    assert is_error(result), f"{case} 本应报错而不是静默成功"
    # 入参守卫必须在任何出网之前生效
    assert stub.requests == [], f"{case} 在报错前已经发出了请求"


@pytest.mark.parametrize(
    "case, needle",
    [
        ("missing_file", "文件不存在"),
        ("unsupported_ext", "不支持的媒体格式"),
        ("bad_mode", "无效的 mode"),
        ("unknown_endpoint", "未知端点"),
        ("relative_output_file", "必须为绝对路径"),
        ("bad_start_time", "无法解析时间戳"),
        ("negative_duration", "duration_minutes 必须 > 0"),
        ("zero_duration", "duration_minutes 必须 > 0"),
        ("start_beyond_end", "超出媒体总时长"),
        ("custom_without_prompt", "必须同时提供 prompt"),
    ],
)
def test_in_process_guards_keep_actionable_messages(
    case: str, needle: str, make_ext_config, audio_m4a: Path, tmp_path: Path
):
    """预期内的失败必须是 `ToolError`，且文案里带着可照做的原因。

    第二阶段 B6 起统一翻成 `ToolError`（以前是裸 `ValueError` / `FileNotFoundError` /
    `ConfigError`，会被 SDK 当崩溃、把正文留在服务端）；B7/B8 的三种新守卫也一并钉在这里。
    """
    config_path = make_ext_config()
    read_media = _read_media_tool(config_path)

    kwargs = {
        "missing_file": dict(file_path=str(tmp_path / "nope.m4a")),
        "unsupported_ext": dict(file_path=str(Path(__file__))),
        "bad_mode": dict(file_path=str(audio_m4a), mode="telepathy"),
        "unknown_endpoint": dict(file_path=str(audio_m4a), endpoint="ghost"),
        "relative_output_file": dict(file_path=str(audio_m4a), output_file="relative/out.md"),
        "bad_start_time": dict(file_path=str(audio_m4a), start_time="not-a-time"),
        "negative_duration": dict(file_path=str(audio_m4a), duration_minutes=-1),
        "zero_duration": dict(file_path=str(audio_m4a), duration_minutes=0),
        "start_beyond_end": dict(file_path=str(audio_m4a), start_time="99:00:00"),
        "custom_without_prompt": dict(file_path=str(audio_m4a), mode="custom"),
    }[case]

    with pytest.raises(ToolError, match=re.escape(needle)):
        asyncio.run(read_media(**kwargs))


def test_relative_output_file_is_never_created(make_ext_config, audio_m4a: Path, tmp_path: Path):
    """相对路径必须被拒绝，且不得在工作目录里留下半个文件。"""
    relative = "omni-media-relative-out/out.md"
    read_media = _read_media_tool(make_ext_config())
    with pytest.raises(ToolError, match="绝对路径"):
        asyncio.run(read_media(file_path=str(audio_m4a), output_file=relative))
    assert not (Path.cwd() / relative).parent.exists()


# ---------------------------------------------------------------------------
# 配置坏掉 / 端点未就绪时的行为
# ---------------------------------------------------------------------------

def test_broken_config_keeps_local_probe_working(tmp_path: Path, audio_m4a: Path):
    asyncio.run(_broken_config(tmp_path / "nope.json", audio_m4a))


async def _broken_config(missing: Path, media: Path) -> None:
    async with _session(missing) as (session, _):
        inspected = await session.call_tool("inspect_media", {"file_path": str(media)})
        assert not is_error(inspected)
        assert "媒体文件探测报告" in text_of(inspected)

        failed = await session.call_tool("read_media", {"file_path": str(media)})
        assert is_error(failed)
        # B6：文案必须真的过边界。以前这里是裸 `ConfigError`，被 SDK 当崩溃压成
        # 一句 `Error executing tool read_media`（原因留在服务端）；现在翻成 `ToolError`，
        # SDK 会保留正文（前缀是它自己加的），调用方读得到「配置不存在」这个原因。
        body = text_of(failed)
        assert body.startswith("Error executing tool read_media"), body
        assert "不存在" in body, body

        # 工具面与配置是否可用无关：配置是**调用时**加载的，不是启动时定的
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert names == {"read_media", "inspect_media"}


def test_missing_config_reports_actionable_error(tmp_path: Path, audio_m4a: Path):
    read_media = _read_media_tool(tmp_path / "nope.json")
    with pytest.raises(ToolError, match="不存在"):
        asyncio.run(read_media(file_path=str(audio_m4a)))


def test_endpoint_without_key_errors_before_any_request(
    make_ext_config, stub: StubEndpoint, audio_m4a: Path
):
    endpoints = stub_endpoints(stub)
    endpoints["gem"]["api_key"] = "REPLACE_ME"
    config_path = make_ext_config(endpoints=endpoints)

    # 进程内：原因必须可操作
    read_media = _read_media_tool(config_path)
    with pytest.raises(ToolError, match="api_key"):
        asyncio.run(read_media(file_path=str(audio_m4a)))
    assert stub.requests == [], "密钥未就绪时不该发出任何请求"

    # 传输层：至少必须是 is_error，不能静默返回空串
    asyncio.run(_no_key(config_path, stub, audio_m4a))
    assert stub.requests == []


async def _no_key(config_path: Path, stub: StubEndpoint, media: Path) -> None:
    async with _session(config_path) as (session, _):
        result = await session.call_tool("read_media", {"file_path": str(media)})
        assert is_error(result)
        assert text_of(result).startswith("Error executing tool read_media")
