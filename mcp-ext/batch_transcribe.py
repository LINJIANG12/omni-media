#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch transcriber via omni-media-ext MCP server."""
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


async def transcribe_file(session: ClientSession, audio_path: Path, max_minutes: float = 30.0) -> str:
    for attempt in range(3):
        full_text_parts: list[str] = []
        start_time = "00:00:00"
        try:
            while True:
                args = {
                    "file_path": str(audio_path),
                    "mode": "transcribe",
                    "duration_minutes": max_minutes,
                }
                if start_time != "00:00:00":
                    args["start_time"] = start_time

                res = await session.call_tool("read_media", args)
                text = "\n".join(
                    getattr(block, "text", "") for block in res.content if getattr(block, "text", None)
                )

                status_match = _STATUS_RE.search(text)
                body = _STATUS_RE.sub("", text).strip()
                if body:
                    full_text_parts.append(body)

                if status_match:
                    status_data = json.loads(status_match.group(1))
                    if status_data.get("is_finished", True):
                        break
                    start_time = status_data.get("next_start_time")
                    if not start_time:
                        break
                else:
                    break

            result = "\n\n".join(full_text_parts).strip()
            if len(result) >= 150:
                return result
            print(f"    [重试 {attempt+1}/3] 返回内容过短 ({len(result)} 字符)，正在重试...", flush=True)
            await asyncio.sleep(2)
        except Exception as err:
            print(f"    [重试 {attempt+1}/3] 发生异常: {err}，正在重试...", flush=True)
            await asyncio.sleep(3)

    return "\n\n".join(full_text_parts)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio-dir", required=True)
    parser.add_argument("--subtitles-dir", required=True)
    parser.add_argument("--start-p", type=int, default=8)
    parser.add_argument("--end-p", type=int, default=79)
    args = parser.parse_args()

    audio_dir = Path(args.audio_dir)
    subtitles_dir = Path(args.subtitles_dir)
    subtitles_dir.mkdir(parents=True, exist_ok=True)

    env = {
        "PYTHONPATH": str(REPO_ROOT),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "omni_media_ext.server"],
        cwd=str(REPO_ROOT),
        env=env,
    )

    audio_files = sorted(audio_dir.glob("P*.m4a"))
    to_process: list[Path] = []
    for f in audio_files:
        m = re.match(r"P(\d+)", f.name)
        if m:
            p_num = int(m.group(1))
            if args.start_p <= p_num <= args.end_p:
                out_name = f.stem + "_clean.txt"
                out_path = subtitles_dir / out_name
                if not out_path.exists() or out_path.stat().st_size < 100:
                    to_process.append(f)

    print(f"找到 {len(to_process)} 个待转录音频分集 (P{args.start_p:02d}-P{args.end_p:02d})...")
    if not to_process:
        print("全部已存在，无需转录。")
        return 0

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for idx, af in enumerate(to_process, 1):
                out_name = af.stem + "_clean.txt"
                out_path = subtitles_dir / out_name
                print(f"[{idx}/{len(to_process)}] 正在转录: {af.name} ...", flush=True)
                try:
                    transcript = await transcribe_file(session, af)
                    out_path.write_text(transcript, encoding="utf-8")
                    print(f"  -> 已保存 ({len(transcript)} 字符): {out_path.name}")
                except Exception as e:
                    print(f"  -> 转录失败: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
