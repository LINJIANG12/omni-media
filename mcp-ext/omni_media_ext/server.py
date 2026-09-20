"""Backward-compatibility shim forwarding to unified omni_media.server."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure parent omni-media directory is on sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from omni_media.server import create_server, main as _main

mcp = create_server(mode="ext")


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
