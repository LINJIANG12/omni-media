"""MCP stdio 客户端小工具：起服务、收文本、判错、取状态注释。

两个 stdio 端到端测试文件（原生 / 代读）共用，避免各写一份解析逻辑——
它们断言的是同一份线上契约，解析方式必须一致。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from mcp import StdioServerParameters

REPO_ROOT = Path(__file__).resolve().parent.parent

STATUS_RE = re.compile(r"<!-- (OMNI_STATUS): (\{.*?\}) -->")


def server_params(mode: str, config_path: Optional[Path] = None) -> StdioServerParameters:
    """按 mode（`all` / `native` / `ext`）组一份 stdio 启动参数。

    `--config` 只在显式给出时下发：原生通道不接受它，代读通道的测试则**必须**给出
    （否则 `load_config()` 会落到仓库根的本地实配 `config.json` 并真发网络请求）。
    """
    args = ["-m", "omni_media.server", "--mode", mode]
    if config_path is not None:
        args += ["--config", str(config_path)]

    return StdioServerParameters(
        command=sys.executable,
        args=args,
        cwd=str(REPO_ROOT),
        env={
            "PYTHONPATH": str(REPO_ROOT),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8",
        },
    )


def text_of(result: Any) -> str:
    """把 CallToolResult 的所有 text 块拼起来。"""
    parts = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def is_error(result: Any) -> bool:
    """mcp 2.x 用 `is_error`（pydantic 字段名），旧版本是 `isError`。"""
    return bool(getattr(result, "is_error", None) or getattr(result, "isError", False))


def status_of(text: str) -> Dict[str, Any]:
    """取分页状态注释并解析成 dict。"""
    match = STATUS_RE.search(text)
    assert match, f"返回文本里没有 OMNI_STATUS 注释:\n{text[:400]}"
    assert match.group(1) == "OMNI_STATUS", "分页标签必须叫 OMNI_STATUS"
    return json.loads(match.group(2))


def body_of(text: str) -> str:
    """去掉状态注释后的正文。"""
    return STATUS_RE.sub("", text).strip()
