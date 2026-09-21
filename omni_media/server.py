"""OmniMedia Unified FastMCP Server: host-native & external model media reading for AI Agents."""

from __future__ import annotations

import argparse
import asyncio
import functools
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Annotated, Optional, Union

from pydantic import Field

from mcp.server.mcpserver import Audio, Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .config import ConfigError, load_config
from .core.inspector import MediaInspector
from .core.limits import (
    DEFAULT_SAFE_SLICE_MINUTES,
    MAX_CONCURRENT_FFMPEG,
    MAX_ONESHOT_MINUTES,
    MAX_SAFE_INLINE_BYTES,
    MEDIA_EXTS,
    MODE_WHITELIST,
    OUTPUT_MODE_WHITELIST,
    VIDEO_EXTS,
    get_slices_cache_dir,
)
from .core.preprocessor import MediaPreprocessor
from .core.temp_manager import ManagedTempDir
from .prompts import mode_prompt
from .providers.base import ProviderRequestError
from .providers.registry import build_endpoint_from_config

STATUS_CONTRACT_VERSION = 1
_ASYNC_FFMPEG_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_FFMPEG)

# 预期内的失败：文案必须原样过 MCP 边界（第二阶段 B6）。
# 工具抛裸 `ValueError` 时，SDK 会把正文压成 `Error executing tool read_media`，宿主 agent
# 看不到「哪个参数错了、合法值是什么」，也就无法纠正自己的调用。`ToolError` 是 SDK 认可的
# 「预期内失败」通道：`is_error=True` 且消息进 `content`。
# 只翻译这些类型；其余异常继续上抛——那是 bug，应当在服务端日志里带栈回溯。
_ANTICIPATED_FAILURES = (ConfigError, ProviderRequestError, FileNotFoundError, ValueError)


def _actionable(fn):
    """把预期内的失败翻译成 `ToolError`（理由见 `_ANTICIPATED_FAILURES` 上方注释）。"""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except _ANTICIPATED_FAILURES as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def _resolve_slice_start(start_time: Optional[str], total_sec: float) -> float:
    """解析并校验切片起点；两条通道共用（第二阶段 B8）。

    `parse_time_str` 自己对无法解析的格式抛错，这里补的是**越界**：起点已超过媒体总时长时
    切片长度必然是 0，静默返回「成功 + `current_duration=00:00:00`」会让调用方以为这一段
    听完了，实际一个字节都没听——本仓库最怕的失败模式。
    """
    if start_time is None:
        return 0.0
    parsed = MediaPreprocessor.parse_time_str(start_time)
    if parsed is None:
        return 0.0
    start_sec = max(0.0, parsed)
    if total_sec > 0 and start_sec >= total_sec:
        raise ValueError(
            f"start_time='{start_time}' 已超出媒体总时长 "
            f"{MediaPreprocessor.format_time_str(total_sec)}：该区间没有任何音频。"
        )
    return start_sec


def _resolve_slice_minutes(duration_minutes: Optional[float]) -> Optional[float]:
    """校验 `duration_minutes`；返回 `None` 表示调用方未指定（各通道再取自己的默认值）。

    注意 `0` **不算**「未指定」：ext 通道以前写的是 `duration_minutes or 配置默认值`，
    于是显式传 0 会静默改用配置默认切片，与调用方要求不符（第二阶段 B8）。
    """
    if duration_minutes is None:
        return None
    if duration_minutes <= 0:
        raise ValueError(f"duration_minutes 必须 > 0，收到 {duration_minutes}")
    return float(duration_minutes)


def create_server(mode: str = "all", config_path: Optional[str] = None) -> MCPServer:
    """Factory to create an MCP server instance configured for native, ext, or all tools."""
    mcp = MCPServer(
        "OmniMedia-Server",
        instructions="通用反重力式多模态音视频直读与代读 MCP 服务。支持全模态大模型原生听音，以及外部端点高保真转录。",
    )

    # 1. inspect_media (always registered)
    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    async def inspect_media(
        file_path: Annotated[str, Field(description="本地音频或视频文件的绝对路径")],
    ) -> str:
        """纯本地毫秒级探测音视频文件的时长、轨道、编码并给出多模态上下文预估（零网络、零凭证）。"""
        try:
            meta = await asyncio.to_thread(MediaInspector.probe, file_path)
            return meta.to_markdown()
        except Exception as e:
            return f"❌ 媒体探测失败: {e}"

    # 2. read_audio (registered for 'native' and 'all')
    if mode in ("native", "all"):
        @mcp.tool(
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            )
        )
        @_actionable
        async def read_audio(
            file_path: Annotated[str, Field(description="本地音频或视频文件的绝对路径")],
            start_time: Annotated[
                Optional[str], Field(description="切片起始时间戳，如 '00:00:00'、'00:15:00' 或秒数（可选，默认从头开始）")
            ] = None,
            duration_minutes: Annotated[
                Optional[float], Field(description="本次切片读取时长预算（分钟，可选，需 > 0。超长媒体未传时自动安全分卷）")
            ] = None,
            output_mode: Annotated[
                str, Field(description="回传通道: 'auto'(智能兼顾) | 'file'(输出本地切片文件绝对路径) | 'inline'(MCP 原生 Audio 数据块)")
            ] = "auto",
            ctx: Context = None,
        ) -> Union[Audio, str]:
            """直接读取本地音视频文件的音频流，返回原生音频数据或切片文件供当前宿主对话模型直接聆听。"""
            path = Path(file_path).resolve()
            if not path.exists():
                raise FileNotFoundError(f"文件不存在: {file_path}")
            if path.suffix.lower() not in MEDIA_EXTS:
                raise ValueError(f"不支持的媒体格式: '{path.suffix}'")
            if output_mode not in OUTPUT_MODE_WHITELIST:
                raise ValueError(f"无效的 output_mode: '{output_mode}'。可选: {sorted(OUTPUT_MODE_WHITELIST)}")

            meta = await asyncio.to_thread(MediaInspector.probe, path)
            total_sec = meta.duration_seconds

            start_sec = _resolve_slice_start(start_time, total_sec)
            requested_min = _resolve_slice_minutes(duration_minutes)

            if requested_min is not None:
                budget_sec = requested_min * 60.0
            else:
                rem_sec = max(0.0, total_sec - start_sec)
                oneshot_sec = MAX_ONESHOT_MINUTES * 60.0
                budget_sec = rem_sec if rem_sec <= oneshot_sec else (DEFAULT_SAFE_SLICE_MINUTES * 60.0)

            end_sec = min(total_sec, start_sec + budget_sec) if total_sec > 0 else start_sec + budget_sec
            slice_dur = max(0.0, end_sec - start_sec)
            is_finished = (total_sec > 0) and (end_sec >= total_sec - 1.0)

            next_start_str = None
            next_budget_min = None
            if not is_finished and total_sec > 0:
                next_start_str = MediaPreprocessor.format_time_str(end_sec)
                rem = total_sec - end_sec
                next_budget_min = round(min(rem / 60.0, DEFAULT_SAFE_SLICE_MINUTES), 2)

            is_sliced = (start_sec > 0.5) or (slice_dur < total_sec - 1.0)
            is_video = path.suffix.lower() in VIDEO_EXTS

            # Auto decision
            target_channel = output_mode
            if output_mode == "auto":
                est_bytes = slice_dur * 4000
                target_channel = "file" if (is_video or is_sliced or est_bytes > MAX_SAFE_INLINE_BYTES) else "inline"

            if target_channel == "file":
                cache_dir = get_slices_cache_dir()
                h = hashlib.sha256(f"{path.as_posix()}_{start_sec:.1f}_{slice_dur:.1f}".encode()).hexdigest()[:12]
                out_slice_file = cache_dir / f"{path.stem}_slice_{h}.m4a"

                if not out_slice_file.exists() or out_slice_file.stat().st_size == 0:
                    async with _ASYNC_FFMPEG_SEMAPHORE:
                        await asyncio.to_thread(
                            MediaPreprocessor.extract_optimized_audio,
                            input_file=path,
                            output_file=out_slice_file,
                            start_time=start_sec,
                            duration_seconds=slice_dur,
                        )

                status_payload = {
                    "contract_version": STATUS_CONTRACT_VERSION,
                    "file_name": path.name,
                    "file_path": path.as_posix(),
                    "duration_seconds": round(total_sec, 2),
                    "current_start": MediaPreprocessor.format_time_str(start_sec),
                    "current_duration": MediaPreprocessor.format_time_str(slice_dur),
                    "is_finished": is_finished,
                    "next_start_time": next_start_str,
                    "next_duration_minutes": next_budget_min,
                    "channel": "file",
                }

                status_comment = f"<!-- OMNI_STATUS: {json.dumps(status_payload, ensure_ascii=False)} -->\n\n"
                next_hint = ""
                if not is_finished and next_start_str:
                    next_hint = f"> ⚠️ **分卷续读提示**: 本音频尚未结束。下一卷续读参数: `start_time='{next_start_str}'`, `duration_minutes={next_budget_min}`。"

                return (
                    f"{status_comment}"
                    f"### 🎙️ OmniMedia 原生音频切片就绪\n\n"
                    f"- **切片本地绝对路径**: `{out_slice_file.as_posix()}`\n"
                    f"- **切片时间区间**: `[{MediaPreprocessor.format_time_str(start_sec)} - {MediaPreprocessor.format_time_str(end_sec)}]` (总时长: {MediaPreprocessor.format_time_str(total_sec)})\n\n"
                    f"{next_hint}\n\n"
                    f"> 💡 请使用原生文件读取工具直接读取该音频切片路径。"
                )

            # Inline channel
            with ManagedTempDir() as tmp_dir:
                slice_path = tmp_dir / f"{path.stem}_slice.m4a"
                async with _ASYNC_FFMPEG_SEMAPHORE:
                    await asyncio.to_thread(
                        MediaPreprocessor.extract_optimized_audio,
                        input_file=path,
                        output_file=slice_path,
                        start_time=start_sec,
                        duration_seconds=slice_dur,
                    )
                audio_bytes = slice_path.read_bytes()

            status_payload = {
                "contract_version": STATUS_CONTRACT_VERSION,
                "file_name": path.name,
                "duration_seconds": round(total_sec, 2),
                "current_start": MediaPreprocessor.format_time_str(start_sec),
                "current_duration": MediaPreprocessor.format_time_str(slice_dur),
                "is_finished": is_finished,
                "next_start_time": next_start_str,
                "next_duration_minutes": next_budget_min,
                "channel": "inline",
            }
            if ctx:
                await ctx.info(f"OMNI_STATUS: {json.dumps(status_payload, ensure_ascii=False)}")
            return Audio(data=audio_bytes, format="mp4")

    # 3. read_media (registered for 'ext' and 'all')
    if mode in ("ext", "all"):
        @mcp.tool(
            annotations=ToolAnnotations(
                # readOnlyHint=False 是**如实**反映：传 `output_file` 时本工具会写盘
                # （首卷原子覆写、续卷追加）。MCP 注解是静态的，做不到「按参数变化」，
                # 所以只能取「可能会写」这个保守且真实的值（第二阶段 B4）。
                # destructiveHint 仍为 False：写入是新增/追加，不销毁既有数据。
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
            )
        )
        @_actionable
        async def read_media(
            file_path: Annotated[str, Field(description="本地音频或视频文件的绝对路径")],
            prompt: Annotated[Optional[str], Field(description="给外部模型的专属指示或提示词（可选）")] = None,
            mode: Annotated[
                str, Field(description="任务模式：'transcribe'(逐字稿，推荐) | 'summarize'(技术教材) | 'qa'(精准答疑) | 'custom'")
            ] = "transcribe",
            endpoint: Annotated[Optional[str], Field(description="使用的模型端点名称（可选，缺省取 active 端点）")] = None,
            start_time: Annotated[Optional[str], Field(description="切片起始时间戳，如 '00:00:00' 或秒数（可选）")] = None,
            duration_minutes: Annotated[Optional[float], Field(description="切片时长（分钟，可选）")] = None,
            output_file: Annotated[
                Optional[str],
                Field(description="转录产物直写落盘路径（必须为绝对路径）。若指定，MCP 将直接写入目标文件，会话中仅返回轻量收据，杜绝上下文爆炸与二次总结"),
            ] = None,
            ctx: Context = None,
        ) -> str:
            """使用配置好的外部大模型（Gemini / OpenAI 协议）对音视频进行转录、总结或抗幻觉问答。"""
            cfg = load_config(config_path)
            ep = cfg.resolve(endpoint)
            ep.require_ready()

            path = Path(file_path).resolve()
            if not path.exists():
                raise FileNotFoundError(f"文件不存在: {file_path}")
            if path.suffix.lower() not in MEDIA_EXTS:
                raise ValueError(f"不支持的媒体格式: '{path.suffix}'")
            if mode not in MODE_WHITELIST:
                raise ValueError(f"无效的 mode: '{mode}'。可选: {sorted(MODE_WHITELIST)}")

            output_path: Optional[Path] = None
            if output_file is not None and str(output_file).strip():
                output_path = Path(str(output_file).strip())
                if not output_path.is_absolute():
                    raise ValueError(f"output_file 必须为绝对路径，收到: '{output_file}'")
                output_path.parent.mkdir(parents=True, exist_ok=True)

            meta = await asyncio.to_thread(MediaInspector.probe, path)
            total_sec = meta.duration_seconds

            start_sec = _resolve_slice_start(start_time, total_sec)
            requested_min = _resolve_slice_minutes(duration_minutes)
            dur_min = requested_min if requested_min is not None else cfg.defaults.slice_minutes
            budget_sec = dur_min * 60.0
            end_sec = min(total_sec, start_sec + budget_sec) if total_sec > 0 else start_sec + budget_sec
            slice_dur = max(0.0, end_sec - start_sec)
            is_finished = (total_sec > 0) and (end_sec >= total_sec - 1.0)

            next_start_str = MediaPreprocessor.format_time_str(end_sec) if not is_finished else None
            next_budget_min = round(min((total_sec - end_sec) / 60.0, cfg.defaults.slice_minutes), 2) if not is_finished else None

            # Prepare audio slice
            client = build_endpoint_from_config(cfg, ep.name)
            ext = ".mp3" if getattr(ep, "audio_format", "") == "mp3" else ".m4a"

            with ManagedTempDir() as tmp_dir:
                slice_path = tmp_dir / f"{path.stem}_ext_slice{ext}"
                async with _ASYNC_FFMPEG_SEMAPHORE:
                    if ext == ".mp3":
                        await asyncio.to_thread(
                            MediaPreprocessor.transcode_to_mp3,
                            input_file=path,
                            output_file=slice_path,
                            start_time=start_sec,
                            duration_seconds=slice_dur,
                        )
                    else:
                        await asyncio.to_thread(
                            MediaPreprocessor.extract_optimized_audio,
                            input_file=path,
                            output_file=slice_path,
                            start_time=start_sec,
                            duration_seconds=slice_dur,
                        )

                full_prompt = mode_prompt(mode, prompt)
                res = await asyncio.to_thread(
                    client.process,
                    media_path=slice_path,
                    prompt=full_prompt,
                    mode=mode,
                )

            chars_written = len(res.text)
            if output_path is not None:
                # 首卷（start_sec <= 0.5）采用唯一 UUID 临时文件 + os.replace 原子覆写
                if start_sec <= 0.5 or not output_path.exists():
                    tmp_target = output_path.parent / f".{output_path.name}.tmp.{uuid.uuid4().hex[:8]}"
                    tmp_target.write_text(res.text, encoding="utf-8")
                    os.replace(tmp_target, output_path)
                else:
                    # 续卷（start_sec > 0.5）追加写入；防御性确保上一卷末尾包含换行符，防止跨卷文本粘连
                    existing_text = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
                    sep = "\n\n" if (existing_text and not existing_text.endswith("\n")) else ""
                    with open(output_path, "a", encoding="utf-8") as af:
                        af.write(f"{sep}{res.text}")
                    chars_written = len(existing_text) + len(sep) + len(res.text)

            status_payload = {
                "contract_version": STATUS_CONTRACT_VERSION,
                "file_name": path.name,
                "file_path": path.as_posix(),
                "duration_seconds": round(total_sec, 2),
                "current_start": MediaPreprocessor.format_time_str(start_sec),
                "current_duration": MediaPreprocessor.format_time_str(slice_dur),
                "is_finished": is_finished,
                "next_start_time": next_start_str,
                "next_duration_minutes": next_budget_min,
                "endpoint": ep.name,
                "model": ep.model,
                "protocol": ep.protocol,
                "elapsed_sec": round(res.elapsed_sec, 2),
                "output_file": output_path.as_posix() if output_path else None,
                "chars_written": chars_written,
            }

            status_comment = f"<!-- OMNI_STATUS: {json.dumps(status_payload, ensure_ascii=False)} -->\n\n"
            next_hint = ""
            if not is_finished and next_start_str:
                next_hint = f"\n\n> ⚠️ **分卷续读提示**: 下一卷续读参数: `start_time='{next_start_str}'`, `duration_minutes={next_budget_min}`。"

            if output_path is not None:
                receipt = (
                    f"{status_comment}"
                    f"✅ **[Direct-to-Disk] 转录逐字稿已由 MCP 直写磁盘**\n"
                    f"- 落盘路径: `{output_path.as_posix()}`\n"
                    f"- 本次写入: {len(res.text):,} 字符 (累计文件: {chars_written:,} 字符, 耗时 {res.elapsed_sec:.1f}s)\n"
                    f"- 分卷状态: {'100% 完成 (is_finished=true)' if is_finished else f'进行中（待续读，下一卷起始 {next_start_str}）'}\n\n"
                    f"⚠️ **【纪律约束】** 全文已安全落盘，未载入上下文。无需且严禁 Agent 在对话中重复输出全文或做二次总结！"
                    f"{next_hint}"
                )
                return receipt

            return f"{status_comment}{res.text}{next_hint}"

    return mcp


def _serve(default_mode: str) -> None:
    """按指定默认模式起 stdio 服务（`--mode` 仍可显式覆盖）。"""
    parser = argparse.ArgumentParser(description="OmniMedia Unified MCP Server")
    parser.add_argument(
        "--mode",
        choices=["all", "native", "ext"],
        default=default_mode,
        help=f"Server mode (default: {default_mode})",
    )
    parser.add_argument("--config", help="Path to config.json for external endpoints")
    args = parser.parse_args()

    mcp = create_server(mode=args.mode, config_path=args.config)
    mcp.run(transport="stdio")


def main() -> None:
    """`omni-media-mcp` 入口：两类工具都注册（人工排查用；正常挂载请用下面两个专用入口）。"""
    _serve("all")


def main_native() -> None:
    """`omni-media-mcp` 的正式用途：宿主自带音频模态时装这个（只暴露 `read_audio`，零凭证）。

    与 `main_ext` 分成两个入口而不是共用一个 `mode=all`，是因为宿主是按**注册名**区分两条
    听音通道的：注册名说「原生」，工具面就必须只有 `read_audio`。共用一个 all 模式会让
    「装的是哪条通道」在工具列表上完全看不出来。
    """
    _serve("native")


def main_ext() -> None:
    """`omni-media-ext-mcp` 入口：宿主只有文本能力时装这个（只暴露 `read_media`，走外部模型）。"""
    _serve("ext")


if __name__ == "__main__":
    main()
