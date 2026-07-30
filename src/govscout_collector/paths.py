from __future__ import annotations

import os
import sys
from pathlib import Path


def default_queue_path(
    *,
    platform: str | None = None,
    home: Path | None = None,
    local_appdata: str | Path | None = None,
) -> Path:
    current_platform = platform or sys.platform
    user_home = home or Path.home()
    if current_platform == "win32":
        base = Path(local_appdata or os.environ.get("LOCALAPPDATA", user_home / "AppData" / "Local"))
        return base / "GovScout Collector" / "collector.sqlite3"
    if current_platform == "darwin":
        return user_home / "Library" / "Application Support" / "GovScout Collector" / "collector.sqlite3"
    raise RuntimeError("GovScout Collector supports Windows and macOS")
