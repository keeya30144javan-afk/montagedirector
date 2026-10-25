"""Where the app keeps settings, caches and default project folders."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

APP_NAME = "Montage Director"
APP_SLUG = "MontageDirector"


def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    path = Path(os.environ.get("MONTAGE_DATA_DIR") or base / APP_SLUG)
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    path = data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def workspace_dir() -> Path:
    docs = Path.home() / "Documents"
    path = (docs if docs.is_dir() else Path.home()) / APP_NAME
    return path


def file_key(path: Path, *extra) -> str:
    """Stable cache key for a file: path, size and modification time."""
    st = path.stat()
    raw = f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}|" + "|".join(map(str, extra))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
