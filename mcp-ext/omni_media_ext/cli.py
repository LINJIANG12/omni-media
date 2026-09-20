"""Backward-compatibility shim for omni_media_ext.cli."""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from omni_media.cli import main_ext as main

if __name__ == "__main__":
    main()
