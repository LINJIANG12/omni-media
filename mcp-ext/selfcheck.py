#!/usr/bin/env python3
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from omni_media.server import create_server

def main():
    print("omni-media-ext compatibility selfcheck:")
    s = create_server(mode="ext")
    tools = set(s._tool_manager._tools.keys())
    assert "read_media" in tools and "inspect_media" in tools
    print("[PASS] ext tools ready:", tools)
    print("[OK] 全部自检通过")

if __name__ == "__main__":
    main()
