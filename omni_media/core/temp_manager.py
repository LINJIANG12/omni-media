"""Zero-residue Temporary Directory and File Management."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path


class ManagedTempDir:
    """Creates a temporary working directory and guarantees cleanup on exit."""

    def __init__(self, prefix: str = "omni_media_"):
        self.prefix = prefix
        self.path: Path | None = None

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix=self.prefix))
        return self.path

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.path and self.path.exists():
            try:
                shutil.rmtree(self.path, ignore_errors=True)
            except Exception:
                pass
