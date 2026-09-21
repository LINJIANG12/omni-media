"""真端点端到端实测：用真实配置文件与真实密钥跑一次 `read_media`（外部模型通道）。

这是**手动验证脚本**，不进自动化门禁（需要真密钥、会真花钱）。自动化测试用的是
进程内仿真端点，见 `tests/test_end_to_end_stdio_ext.py`。

用法（路径与端点都从参数来，脚本里不硬编码任何机器路径）：

    # 1) 先确认配置就绪
    python -m omni_media.cli status

    # 2) 跑一次 1 分钟切片的逐字稿
    python examples/read_media.py "D:/courses/某课程/audio/P01.m4a" --duration 1

    # 3) 换端点 / 换任务预设
    python examples/read_media.py "D:/x.mp3" --endpoint openai-audio --mode summarize
    python examples/read_media.py "D:/x.mp3" --mode qa --prompt "讲了什么？"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent
_STATUS_RE = re.compile(r"<!-- OMNI_STATUS: (\{.*?\}) -->")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="omni-media 外部模型通道真端点端到端实测")
    parser.add_argument("media", help="本地音视频文件的绝对路径")
    parser.add_argument("--config", "-c", default=None, help="配置文件路径（缺省走默认查找顺序）")
    parser.add_argument("--endpoint", "-e", default=None, help="配置里的端点名（缺省用 active）")
    parser.add_argument(
        "--mode", "-m", default="transcribe",
        choices=["transcribe", "summarize", "qa", "custom"], help="任务预设",
    )
    parser.add_argument("--prompt", "-p", default=None, help="自定义提示词（custom 必填）")
    parser.add_argument("--start", default=None, help="起始时间戳，如 00:00:00")
    parser.add_argument("--duration", "-d", type=float, default=None, help="本次切片分钟数（缺省走配置 slice_minutes，通常 30）")
    parser.add_argument("--output-file", "-o", default=None, help="转录产物直写落盘路径（绝对路径或相对路径）")
    parser.add_argument("--full", action="store_true", help="自动分卷续读直到整段媒体听完 (is_finished=True)")
    parser.add_argument("--skip-inspect", action="store_true", help="跳过 inspect_media")
    return parser


def section(title: str) -> None:
    print("\n" + "=" * 66)
    print(title)
    print("=" * 66)


async def main() -> int:
    args = build_parser().parse_args()

    media = Path(args.media).expanduser().resolve()
    if not media.is_file():
        print(f"媒体文件不存在: {media}", file=sys.stderr)
        return 2

    env = {
        "PYTHONPATH": str(REPO_ROOT),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
    }
    # 显式钉住 ext 通道：不写就是 all 模式，工具面会多出 read_audio，
    # 就验证不出「只挂外部模型通道」时的真实行为。
    server_args = ["-m", "omni_media.server", "--mode", "ext"]
    if args.config:
        server_args += ["--config", str(Path(args.config).expanduser().resolve())]

    params = StdioServerParameters(
        command=sys.executable, args=server_args, cwd=str(REPO_ROOT), env=env
    )

    print(f"Python    : {sys.version.split()[0]}")
    print(f"Media     : {media} ({media.stat().st_size / 1024:.0f} KiB)")
    print(f"Config    : {args.config or '(默认查找顺序)'}")
    print(f"Endpoint  : {args.endpoint or '(配置的 active)'}")
    print(f"Mode      : {args.mode} / duration={args.duration}min")

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            section("[1/3] MCP 握手与工具清单")
            init = await session.initialize()
            print(f"  server: {init.server_info.name} v{init.server_info.version}")
            print(f"  protocol: {init.protocol_version}")
            tools = await session.list_tools()
            for tool in tools.tools:
                print(f"  - {tool.name}")

            if not args.skip_inspect:
                section("[2/3] inspect_media（本地，不花钱）")
                result = await session.call_tool("inspect_media", {"file_path": str(media)})
                for block in result.content:
                    print(getattr(block, "text", ""))

            section("[3/3] read_media（真调外部模型）")
            arguments: dict = {
                "file_path": str(media),
                "mode": args.mode,
            }
            if args.duration is not None:
                arguments["duration_minutes"] = args.duration
            if args.output_file:
                arguments["output_file"] = str(Path(args.output_file).expanduser().resolve())
            if args.endpoint:
                arguments["endpoint"] = args.endpoint
            if args.prompt:
                arguments["prompt"] = args.prompt
            if args.start:
                arguments["start_time"] = args.start

            iteration = 1
            while True:
                if iteration > 1:
                    print(f"\n--- [分卷续读] 轮次 {iteration}: start_time={arguments.get('start_time')}, duration={arguments.get('duration_minutes')} ---")
                result = await session.call_tool("read_media", arguments)
                text = "\n".join(
                    getattr(block, "text", "") for block in result.content if getattr(block, "text", None)
                )

                if getattr(result, "isError", None) or getattr(result, "is_error", False):
                    print("[工具返回错误]")
                    print(text)
                    return 1

                status_match = _STATUS_RE.search(text)
                status = json.loads(status_match.group(1)) if status_match else {}
                if status:
                    print("状态注释:")
                    print(json.dumps(status, ensure_ascii=False, indent=2))

                body = _STATUS_RE.sub("", text).strip()
                if not args.output_file:
                    print("\n模型返回正文:")
                    print(body)
                else:
                    print(body)

                if not args.full or status.get("is_finished", True):
                    break

                next_start = status.get("next_start_time")
                if not next_start:
                    break
                arguments["start_time"] = next_start
                if status.get("next_duration_minutes") is not None:
                    arguments["duration_minutes"] = status.get("next_duration_minutes")
                iteration += 1

    print("\n*** 真端点端到端调用成功 ***")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
