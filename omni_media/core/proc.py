"""Quiet subprocess runner suppressing console popups on Windows."""

from __future__ import annotations

import os
import subprocess
from typing import Any, Sequence, Union

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PathLike = Union[str, "os.PathLike[str]"]


def quiet_kwargs(**kwargs: Any) -> dict:
    """Add flags to prevent console window flashing on Windows."""
    if os.name == "nt" and CREATE_NO_WINDOW:
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    if kwargs.get("text") or kwargs.get("universal_newlines"):
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    return kwargs


def run_quiet(cmd: Sequence[PathLike], **kwargs: Any) -> subprocess.CompletedProcess:
    """Run a subprocess silently, capturing output and preventing popups."""
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.PIPE)
    return subprocess.run(cmd, **quiet_kwargs(**kwargs))
