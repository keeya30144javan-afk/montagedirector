"""Thin helpers around the ffmpeg binary that ships with imageio-ffmpeg."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np


class MediaError(Exception):
    """A media file could not be read or written."""


class Cancelled(Exception):
    """The user stopped the job."""


def check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


@lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    override = os.environ.get("MONTAGE_FFMPEG")
    if override:
        return override
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _popen_kwargs() -> dict:
    # No console window flashes when running under pythonw / a frozen app.
    if sys.platform == "win32":
        return {"creationflags": 0x08000000}
    return {}


@dataclass
class MediaInfo:
    path: Path
    duration: float
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_video: bool = False
    has_audio: bool = False


_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO = re.compile(r"Stream #.*?Video:.*?(\d{2,5})x(\d{2,5})")
_FPS = re.compile(r"(\d+(?:\.\d+)?)\s*fps")
_ROTATE = re.compile(r"rotate\s*:\s*(-?\d+)|rotation of (-?\d+(?:\.\d+)?)")


def probe(path: str | Path) -> MediaInfo:
    path = Path(path)
    if not path.is_file():
        raise MediaError(f"File not found: {path}")
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, errors="replace", **_popen_kwargs(),
    )
    text = proc.stderr
    m = _DURATION.search(text)
    if not m:
        raise MediaError(f"Not a readable media file: {path.name}")
    h, mi, s = m.groups()
    info = MediaInfo(path=path, duration=int(h) * 3600 + int(mi) * 60 + float(s))
    for line in text.splitlines():
        if "Video:" in line and not info.has_video and "attached pic" not in line:
            vm = _VIDEO.search(line)
            if vm:
                info.has_video = True
                info.width, info.height = int(vm.group(1)), int(vm.group(2))
                fm = _FPS.search(line)
                info.fps = float(fm.group(1)) if fm else 30.0
        elif "Audio:" in line:
            info.has_audio = True
    rm = _ROTATE.search(text)
    if rm and info.has_video:
        angle = abs(int(float(rm.group(1) or rm.group(2) or 0))) % 180
        if angle == 90:  # phone footage: ffmpeg auto-rotates on decode
            info.width, info.height = info.height, info.width
    if info.duration <= 0:
        raise MediaError(f"File has no duration: {path.name}")
    return info


def iter_frames(path: Path, fps: float, width: int, height: int, cancel=None,
                start: float = 0.0, duration: float | None = None):
    """Yield (time, HxWx3 uint8 frame) decoded at a low rate and resolution."""
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += [
        "-an", "-sn",
        "-vf", f"fps={fps},scale={width}:{height}:flags=area,format=rgb24",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    frame_bytes = width * height * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=frame_bytes * 4, **_popen_kwargs())
    try:
        i = 0
        while True:
            check_cancel(cancel)
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield start + i / fps, np.frombuffer(buf, np.uint8).reshape(height, width, 3)
            i += 1
    finally:
        proc.kill()
        proc.wait()


def decode_audio(path: Path, sr: int) -> np.ndarray:
    """Decode any audio file to mono float32 with ffmpeg."""
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
        capture_output=True, **_popen_kwargs(),
    )
    if proc.returncode != 0 or not proc.stdout:
        msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise MediaError(f"Could not decode audio '{path.name}': {msg[-1] if msg else 'unknown error'}")
    return np.frombuffer(proc.stdout, np.float32).copy()


class ProcessGroup:
    """Tracks running ffmpeg processes so Cancel can stop all of them."""

    def __init__(self):
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen] = set()

    def run(self, cmd: list[str], cancel=None) -> None:
        check_cancel(cancel)
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                **_popen_kwargs())
        with self._lock:
            self._procs.add(proc)
        try:
            _, err = proc.communicate()
        finally:
            with self._lock:
                self._procs.discard(proc)
        check_cancel(cancel)
        if proc.returncode != 0:
            lines = err.decode("utf-8", "replace").strip().splitlines()
            raise MediaError("ffmpeg failed: " + (" | ".join(lines[-3:]) if lines else "unknown error"))

    def kill_all(self) -> None:
        with self._lock:
            for proc in self._procs:
                try:
                    proc.kill()
                except OSError:
                    pass
