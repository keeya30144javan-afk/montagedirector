"""Entry point for the packaged app (PyInstaller) and for `python run_app.py`."""
import multiprocessing
import os
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    # librosa compiles some functions with numba and caches them; a frozen app's own
    # folder isn't writable, so point the cache at the user's data folder.
    from montage_director.paths import data_dir

    os.environ.setdefault("NUMBA_CACHE_DIR", str(data_dir() / "numba_cache"))
    from montage_director.__main__ import run

    sys.exit(run())
