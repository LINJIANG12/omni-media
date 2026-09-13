"""OmniMedia-Ext FastMCP Server：把音视频交给**配置好的外部模型**代读。

工具面（刻意保持最小）：

* ``read_media``  —— 唯一代读入口。切片 → 交给配置里选定的外部端点 → 返回文本。
* ``inspect_media`` —— 纯本地 ffprobe 探测，不联网、不需要任何凭证。

资源：

* ``media://endpoints`` —— 脱敏后的端点清单，供调用方挑选 ``endpoint`` 参数。

与原生听音版（`omni-media-mcp`）的关系：本服务**不提供** ``read_audio``。那个工具的
语义是「把音频交给宿主模型去听」，与本版本「服务自己调外部模型」的定位直接冲突；
两者不能在同一份代码里并存，否则调用方永远不知道该走哪条路。
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
from pathlib import Path
from typing import Annotated, Any, Callable, Dict, Optional

from pydantic import Field

try:
    from mcp.server.mcpserver import Context, MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x（FastMCP 时代）的兼容路径
    from mcp.server.fastmcp import Context, FastMCP as MCPServer
    from mcp.server.fastmcp.exceptions import ToolError

from .config import Config, ConfigError, load_config
from .core.inspector import MediaInspector
from .core.limits import (
    AUDIO_BITRATE_VOICE,
    MAX_CONCURRENT_FFMPEG,
    MEDIA_EXTS,
    MODE_WHITELIST,
    VIDEO_EXTS,
)
from .core.preprocessor import MediaPreprocessor
from .core.temp_manager import ManagedTempDir
from .prompts import mode_prompt
from .providers.base import BaseEndpoint, ProviderRequestError
from .providers.registry import build_endpoint_from_config

# Concurrency semaphore to throttle ffmpeg processes across async tasks
_ASYNC_FFMPEG_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_FFMPEG)

# 切片体积预估用的人声码率（32kbps ≈ 4000 字节/秒）。
_BYTES_PER_AUDIO_SEC = int(AUDIO_BITRATE_VOICE.rstrip("k")) * 1000 / 8

# 由 --config 传入的显式配置路径；None 时走默认查找顺序。
_CONFIG_PATH: Optional[str] = None

# ---------------------------------------------------------------------------
# 与原生听音版（omni-media-mcp）共享的线上契约
# ---------------------------------------------------------------------------
# 两个 MCP 是「同一个岗位的两种实现」：有原生音频的宿主挂原生版，没有的挂本版本。
# 为了让调用方代码可以无感切换，分页协议刻意保持一致：
#   * 同一行内注释标签 `<!-- OMNI_STATUS: {...} -->`（原生版 server.py 里同名）；
#   * 同一套共有键与语义（见 STATUS_SHARED_KEYS）；
#   * 同样的续读循环（is_finished=false → 用 next_start_time 再调一次）。
# 本版本独有的信息一律放在不与共有键冲突的扩展键里（channel / task / endpoint / …）。
STATUS_TAG = "OMNI_STATUS"

STATUS_SHARED_KEYS = (
    "status",           # COMPLETED | IN_PROGRESS
    "mode",             # 切片模式：oneshot（整篇一次读完）| chunked（分卷）
    "is_finished",      # 是否已读到媒体末尾
    "start_time",       # 本卷起始 HH:MM:SS
    "end_time",         # 本卷结束 HH:MM:SS
    "total_duration",   # 媒体总时长 HH:MM:SS，或 "未知"
)

# 仅在「还有下一卷」时出现的共有键。
STATUS_SHARED_CONTINUATION_KEYS = ("next_start_time", "next_duration_minutes")

# Initialize MCPServer
mcp = MCPServer(
    "OmniMedia-Ext-Server",
    instructions=(
        "外部模型代读版音视频 MCP 服务：把本地音视频切片后交给配置文件里选定的外部模型"
        "（Gemini 协议 / OpenAI 协议）完成转录、教材级总结与抗幻觉问答，返回文本结果。"
    ),
)


def configure(config_path: Optional[str] = None) -> None:
    """设置配置路径（供 CLI / 测试注入）。"""
    global _CONFIG_PATH
    _CONFIG_PATH = config_path


def surfaced(fn: Callable) -> Callable:
    """把「预期内的领域错误」翻成 `ToolError`，让调用方拿到原文。

    为什么必须翻译：mcp 2.x 只对 `ToolError` 透传异常文本，其余异常一律被替换成
    `Error executing tool <name>`（见 mcp/server/mcpserver/tools/base.py:208）。
    不翻译的话，「端点未配置 api_key，请在 config.json 里补上」这类**可操作**信息
    就全丢了，调用方只看到一个通用错误名。

    只翻译明确属于领域内的异常类型；真正的 bug（TypeError、KeyError 等）仍然按崩
    溃上报，不会被伪装成用户的输入错误。
    """
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except (ConfigError, ProviderRequestError, FileNotFoundError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def _load_config() -> Config:
    """每次工具调用都重新读盘。

    配置很小（<2KB），重读的成本可以忽略，换来的是「改完配置文件立刻生效、不用重启
    MCP 服务」这个很实用的性质。
    """
    return load_config(_CONFIG_PATH)


# ---------------------------------------------------------------------------
# 切片规划
# ---------------------------------------------------------------------------

def plan_slice(
    total_duration: float,
    start_sec: float,
    duration_minutes: Optional[float],
    default_slice_minutes: float,
    payload_budget_bytes: int,
) -> Dict[str, Any]:
    """把「用户要的时长」折算成「这次实际读的时长」。

    三重约束：① 用户给的 ``duration_minutes``（或配置默认）；② 端点载荷预算折算出的
    时长上限；③ 媒体剩余时长。被 ② 收窄时 ``clamped=True``，调用方会在状态注释里看到。
    """
    requested_min = float(duration_minutes) if duration_minutes is not None else float(default_slice_minutes)
    slice_sec = requested_min * 60.0

    payload_cap_sec = max(1.0, payload_budget_bytes / _BYTES_PER_AUDIO_SEC)
    clamped = False
    if slice_sec > payload_cap_sec:
        slice_sec = payload_cap_sec
        clamped = True

    if total_duration > 0:
        remaining = max(0.0, total_duration - start_sec)
        if slice_sec > remaining:
            slice_sec = remaining

    end_sec = start_sec + slice_sec
    if total_duration > 0:
        end_sec = min(total_duration, end_sec)

    duration_known = total_duration > 0
    has_next = duration_known and end_sec < total_duration - 0.5

    return {
        "slice_seconds": slice_sec,
        "end_sec": end_sec,
        "requested_minutes": requested_min,
        "effective_minutes": round(slice_sec / 60.0, 3),
        "clamped": clamped,
        "duration_known": duration_known,
        "has_next": has_next,
    }


def _is_16k_mono(meta: Any) -> bool:
    """源文件是否已是 16kHz 单声道（是则不重编码）。"""
    streams = getattr(meta, "streams", None)
    if not streams:
        return False
    for stream in streams:
        if stream.get("codec_type") == "audio":
            return stream.get("sample_rate") == 16000 and stream.get("channels") == 1
    return False


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

@mcp.tool()
@surfaced
async def read_media(
    file_path: Annotated[str, Field(description="本地音频或视频文件的绝对路径")],
    mode: Annotated[
        str,
        Field(description="任务预设: 'transcribe'(逐字稿) | 'summarize'(教材级总结) | 'qa'(抗幻觉问答) | 'custom'(纯自定义)"),
    ] = "transcribe",
    instruction: Annotated[
        Optional[str], Field(description="自定义附加提示词（可选）；mode='custom' 时为必填")
    ] = None,
    endpoint: Annotated[
        Optional[str], Field(description="配置里的端点名（可选，缺省用配置的 active）")
    ] = None,
    start_time: Annotated[
        Optional[str], Field(description="切片起始时间戳，如 '00:00:00'、'00:15:00' 或秒数（可选，默认从头开始）")
    ] = None,
    duration_minutes: Annotated[
        Optional[float], Field(description="本次切片时长预算（分钟，可选，需 > 0；缺省用配置 defaults.slice_minutes）")
    ] = None,
    ctx: Context = None,
) -> str:
    """读取本地音视频，委托**配置好的外部模型**完成转录/总结/问答，返回文本。

    与宿主模型自身的音频能力无关：本服务自己发 HTTP 请求，因此宿主是否具备音频模态
    都不影响结果。端点（协议、base_url、model、api_key）全部来自配置文件。

    超长媒体按 `duration_minutes` 切片，一次调用处理一片，返回值里带机器可读的
    `OMNI_STATUS` 与续读参数；调用方按 `start_time` 循环即可读完整篇。

    Args:
        file_path: 本地音视频绝对路径（支持 mp4/mkv/mov/avi/flv/webm/mp3/wav/m4a/aac/flac 等）。
        mode: 'transcribe' 逐字稿 / 'summarize' 教材级总结 / 'qa' 问答 / 'custom' 纯自定义指令。
        instruction: 自定义附加提示词；mode='custom' 时必须非空。
        endpoint: 配置里的端点名；缺省用配置的 `active`。
        start_time: 切片起始时间戳（'HH:MM:SS'、'MM:SS' 或秒数）。
        duration_minutes: 本次切片时长（分钟），> 0。

    Returns:
        固定契约的 Markdown：`<!-- OMNI_STATUS: {...} -->` + 模型正文 +
        （未读完时）续读参数提示。

    Raises:
        FileNotFoundError: 媒体文件不存在。
        ValueError: 参数非法、路径不是文件、扩展名不支持、mode 不在白名单、custom 缺 instruction。
        ConfigError: 配置文件缺失/非法、端点名不存在、端点未配置 api_key。
        ProviderRequestError: 外部端点请求失败（含 HTTP 状态码与响应体摘要）。
    """
    mode_key = (mode or "transcribe").strip().lower()
    if mode_key not in MODE_WHITELIST:
        raise ValueError(f"不支持的 mode: '{mode}'。支持: {sorted(MODE_WHITELIST)}")
    if mode_key == "custom" and not (instruction and instruction.strip()):
        raise ValueError("mode='custom' 时需提供非空 instruction。")

    path = Path(file_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"媒体文件不存在: `{file_path}`")
    if not path.is_file():
        raise ValueError(f"路径不是文件: `{file_path}`")
    ext = path.suffix.lower()
    if ext not in MEDIA_EXTS:
        raise ValueError(f"不支持的文件扩展名: '{ext}'。支持的媒体扩展名: {sorted(MEDIA_EXTS)}")

    if duration_minutes is not None and duration_minutes <= 0:
        raise ValueError(f"duration_minutes 必须大于 0，收到: {duration_minutes}")

    start_sec = MediaPreprocessor.parse_time_str(start_time)
    if start_sec is None:
        start_sec = 0.0

    async def progress(pct: float, message: str) -> None:
        """上报进度。

        只用 `report_progress`：mcp 2.x 里 `Context.info` 等日志方法属于已废弃的
        logging 能力（SEP-2577），而且它们是**协程**，不 await 会被静默丢弃。
        进度上报失败绝不能影响主流程，因此整体吞掉异常。
        """
        if ctx is None:
            return
        try:
            await ctx.report_progress(progress=pct, total=1.0, message=message)
        except Exception:
            pass

    # ---- 配置与端点（凭证未就绪在这里就会给出可操作的报错，而不是等 401）----
    config = _load_config()
    endpoint_impl: BaseEndpoint = build_endpoint_from_config(config, endpoint)
    endpoint_impl.endpoint.require_ready()
    await progress(0.05, f"已选中端点 `{endpoint_impl.name}`（{endpoint_impl.protocol} / {endpoint_impl.model}）")

    # ---- 探测总时长（软失败：合成/异常文件探测不出来也要能继续）----
    meta = None
    total_duration = 0.0
    try:
        meta = await asyncio.to_thread(MediaInspector.probe, path)
        total_duration = float(meta.duration_seconds or 0.0)
    except Exception:
        total_duration = 0.0

    if total_duration > 0 and start_sec >= total_duration:
        raise ValueError(
            f"start_time 超出媒体时长：起始 {MediaPreprocessor.format_time_str(start_sec)} "
            f">= 总时长 {MediaPreprocessor.format_time_str(total_duration)}"
        )

    plan = plan_slice(
        total_duration=total_duration,
        start_sec=start_sec,
        duration_minutes=duration_minutes,
        default_slice_minutes=config.defaults.slice_minutes,
        payload_budget_bytes=endpoint_impl.payload_budget_bytes(),
    )
    end_sec = plan["end_sec"]

    # ---- 切片 ----
    prompt = mode_prompt(mode_key, instruction)
    await progress(0.2, "准备媒体切片…")

    with ManagedTempDir(prefix="omni_ext_media_") as tmp_dir:
        # 需要切片的三种情形：后面还有内容、从中间开始读、总时长探测不出来
        # （探测不出来时也必须按预算截断，否则会把整个文件塞进一次请求）。
        is_sliced = plan["has_next"] or start_sec > 0 or not plan["duration_known"]
        needs_ffmpeg = (
            is_sliced
            or path.suffix.lower() in VIDEO_EXTS
            or not _is_16k_mono(meta)
        )

        if needs_ffmpeg:
            media_for_request = tmp_dir / f"{path.stem}_16k.m4a"
            async with _ASYNC_FFMPEG_SEMAPHORE:
                await asyncio.to_thread(
                    MediaPreprocessor.extract_optimized_audio,
                    input_file=path,
                    output_file=media_for_request,
                    start_time=start_sec if is_sliced else None,
                    duration_seconds=plan["slice_seconds"] if is_sliced else None,
                )
        else:
            # 源文件本就是 16kHz 单声道且要读全篇：直接透传，省一次重编码。
            media_for_request = path

        if not media_for_request.exists() or media_for_request.stat().st_size == 0:
            raise RuntimeError(f"音频切片生成失败: `{media_for_request}`")

        await progress(0.4, "切片就绪，正在请求外部模型…")
        try:
            result = await asyncio.to_thread(
                endpoint_impl.process,
                media_for_request,
                prompt,
                mode_key,
            )
        except ProviderRequestError:
            raise
        except ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一包装成可读错误
            raise RuntimeError(f"外部模型处理失败: {exc}") from exc

    await progress(1.0, "完成。")

    # ---- 返回契约（与原生听音版 read_audio **同构**）----
    # 分页字段一律沿用原生版的 `<!-- OMNI_STATUS: {...} -->` 与同一套键名/语义，
    # 这样同一段调用方代码（例如 video2book 的「is_finished=false 就按
    # next_start_time 续读」）在两个 MCP 之间可以无感切换。
    #
    # 注意 `mode` 一词在原生版里指**切片模式**（oneshot / chunked），因此本版本把
    # 任务预设放在 `task` 里，避免同一个键在两份契约里含义不同而被静默误读。
    start_fmt = MediaPreprocessor.format_time_str(start_sec)
    end_fmt = MediaPreprocessor.format_time_str(end_sec)
    total_fmt = (
        MediaPreprocessor.format_time_str(total_duration) if plan["duration_known"] else "未知"
    )

    status: Dict[str, Any] = {
        "status": "IN_PROGRESS" if plan["has_next"] else "COMPLETED",
        # 契约共有字段（与原生版逐字一致）
        "mode": "chunked" if is_sliced else "oneshot",
        "is_finished": not plan["has_next"],
        "start_time": start_fmt,
        "end_time": end_fmt,
        "total_duration": total_fmt,
        # 本版本扩展字段（原生版没有；命名不与共有字段冲突）
        "channel": "external-model",
        "task": mode_key,
        "endpoint": result.endpoint_name,
        "protocol": result.protocol,
        "model": result.model,
        "elapsed_sec": round(result.elapsed_sec, 2),
        "clamped": plan["clamped"],
    }
    if result.finish_reason:
        status["finish_reason"] = result.finish_reason
    if plan["has_next"]:
        status["next_start_time"] = end_fmt
        status["next_duration_minutes"] = plan["effective_minutes"]

    blocks = [f"<!-- {STATUS_TAG}: {json.dumps(status, ensure_ascii=False)} -->", ""]

    if result.finish_reason == "MAX_TOKENS":
        blocks.append("> ⚠️ 本次回答达到模型长度上限，内容可能被截断：建议调小 duration_minutes 后重读本片。")
        blocks.append("")

    blocks.append(result.text)

    if plan["clamped"]:
        blocks.append("")
        blocks.append(
            f"> 📦 **切片已按端点载荷预算收窄**：本次实读 {plan['effective_minutes']} 分钟"
            f"（原请求 {plan['requested_minutes']} 分钟）。需要完整覆盖请按续读参数继续。"
        )

    blocks.append("")
    if plan["has_next"]:
        blocks.append(
            f'> ⏱️ **续读下一分卷参数**: `start_time="{end_fmt}", duration_minutes={plan["effective_minutes"]}`'
        )
    elif not plan["duration_known"]:
        blocks.append("> ⏱️ **分页状态**: 未能探测媒体总时长，分页信息不可用。")
    else:
        blocks.append("> ⏱️ **分页状态**: 全篇音频已处理完毕。")

    return "\n".join(blocks)


@mcp.tool()
@surfaced
async def inspect_media(
    file_path: Annotated[str, Field(description="本地音视频文件绝对路径")],
) -> str:
    """毫秒级探测音视频媒体文件的时长、编码、轨道、体积与规格。

    纯本地 ffprobe，不联网、不需要任何凭证，因此即使配置文件还没写好也能用来估预算。

    Args:
        file_path: 本地音视频文件绝对路径。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 路径不是文件或扩展名不支持。
        RuntimeError: 探测过程失败。
    """
    path = Path(file_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"媒体文件不存在: `{file_path}`")
    if not path.is_file():
        raise ValueError(f"路径不是文件: `{file_path}`")
    ext = path.suffix.lower()
    if ext not in MEDIA_EXTS:
        raise ValueError(f"不支持的文件扩展名: '{ext}'。支持的媒体扩展名: {sorted(MEDIA_EXTS)}")

    try:
        meta = await asyncio.to_thread(MediaInspector.probe, path)
        return meta.to_markdown()
    except ValueError:
        raise
    except Exception as e:
        raise RuntimeError(f"探测失败: {e}") from e


@mcp.resource("media://endpoints")
def endpoints_resource() -> str:
    """当前配置文件里的端点清单（api_key 已脱敏）与请求预算默认值。"""
    try:
        config = _load_config()
    except ConfigError as exc:
        return f"# 配置不可用\n\n{exc}\n"
    return json.dumps(config.to_public_dict(), ensure_ascii=False, indent=2) + "\n"


def main(argv: Optional[list[str]] = None) -> None:
    """Run the FastMCP server on stdio transport."""
    parser = argparse.ArgumentParser(
        prog="omni-media-ext-mcp",
        description="OmniMedia-Ext MCP 服务（外部模型代读，配置来自配置文件）",
    )
    parser.add_argument(
        "--config",
        "-c",
        default=None,
        help="配置文件路径；缺省按 <repo_root>/config.json → ~/.omni-media-ext/config.json 顺序查找",
    )
    # 宿主有时会追加自己的参数，这里只取自己认识的，避免因此起不来。
    args, _unknown = parser.parse_known_args(argv)

    configure(args.config)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
