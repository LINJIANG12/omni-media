"""静默子进程入口：统一抑制 Windows 控制台窗口。

与技能仓库 `skill/src/core/proc.py` 同构（两仓库互相独立，故各留一份最小实现）：
ffmpeg / ffprobe 每次调用都会新建进程，在"父进程没有控制台"的场景（宿主 Agent
后台托管 + 多子智能体并发）下，Windows 会为每个控制台程序新开一个窗口，成片闪黑窗。
`run_quiet()` 在 Windows 下统一带 `CREATE_NO_WINDOW`，其它平台行为不变。
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Sequence, Union

# Windows 专用：不为子进程创建控制台窗口
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PathLike = Union[str, "os.PathLike[str]"]


def quiet_kwargs(**kwargs: Any) -> dict:
    """补齐"不弹窗 + 不读标准输入"的调用参数（不改动调用方显式传入的值）。"""
    if os.name == "nt" and CREATE_NO_WINDOW:
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    if kwargs.get("text") or kwargs.get("universal_newlines"):
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    return kwargs


def run_quiet(cmd: Sequence[PathLike], **kwargs: Any) -> "subprocess.CompletedProcess":
    """`subprocess.run` 的静默包装：默认收走标准输出/错误，Windows 下不弹控制台窗口。"""
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.PIPE)
    return subprocess.run(cmd, **quiet_kwargs(**kwargs))
