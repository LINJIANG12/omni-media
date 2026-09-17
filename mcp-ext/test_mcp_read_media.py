"""真端点端到端实测：用真实配置文件与真实密钥跑一次 `read_media`。

这是**手动验证脚本**，不进自动化门禁（需要真密钥、会真花钱）。自动化测试用的是
进程内仿真端点，见 `tests/test_end_to_end_stdio.py`。

用法（路径与端点都从参数来，脚本里不硬编码任何机器路径）：

    # 1) 先确认配置就绪
    python -m omni_media_ext.cli status

    # 2) 跑一次 1 分钟切片的逐字稿
    python test_mcp_read_media.py "D:/courses/某课程/audio/P01.m4a" --duration 1

    # 3) 换端点 / 换任务预设
    python test_mcp_read_media.py "D:/x.mp3" --endpoint openai-audio --mode summarize
    python test_mcp_read_media.py "D:/x.mp3" --config "D:/my/config.json" --mode qa --instruction "讲了什么？"
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

REPO_ROOT = Path(__file__).resolve().parent
_STATUS_RE = re.compile(r"<!-- OMNI_STATUS: (\{.*?\}) -->")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="omni-media-ext 真端点端到端实测")
    parser.add_argument("media", help="本地音视频文件的绝对路径")
    parser.add_argument("--config", "-c", default=None, help="配置文件路径（缺省走默认查找顺序）")
    parser.add_argument("--endpoint", "-e", default=None, help="配置里的端点名（缺省用 active）")
    parser.add_argument(
        "--mode", "-m", default="transcribe",
        choices=["transcribe", "summarize", "qa", "custom"], help="任务预设",
    )
    parser.add_argument("--instruction", "-i", default=None, help="自定义提示词（custom 必填）")
    parser.add_argument("--start", default=None, help="起始时间戳，如 00:00:00")
    parser.add_argument("--duration", "-d", type=float, default=1.0, help="本次切片分钟数（默认 1，省时省钱）")
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
    server_args = ["-m", "omni_media_ext.server"]
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
                "duration_minutes": args.duration,
            }
            if args.endpoint:
                arguments["endpoint"] = args.endpoint
            if args.instruction:
                arguments["instruction"] = args.instruction
            if args.start:
                arguments["start_time"] = args.start

            result = await session.call_tool("read_media", arguments)
            text = "\n".join(
                getattr(block, "text", "") for block in result.content if getattr(block, "text", None)
            )

            if getattr(result, "isError", None) or getattr(result, "is_error", False):
                print("[工具返回错误]")
                print(text)
                return 1

            status_match = _STATUS_RE.search(text)
            if status_match:
                print("状态注释:")
                print(json.dumps(json.loads(status_match.group(1)), ensure_ascii=False, indent=2))
            print("\n模型返回正文:")
            body = _STATUS_RE.sub("", text).strip()
            print(body)

    print("\n*** 真端点端到端调用成功 ***")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
