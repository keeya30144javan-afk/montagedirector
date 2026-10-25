import subprocess

import numpy as np
import pytest


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    """Two short videos with hard cuts and a 120 BPM song with a quiet intro and a drop."""
    import imageio_ffmpeg

    root = tmp_path_factory.mktemp("media")
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    for name, parts in {
        "a.mp4": ["color=c=0x224488:s=320x180:r=24", "testsrc2=s=320x180:r=24", "mandelbrot=s=320x180:r=24"],
        "b.mp4": ["life=s=320x180:r=24:ratio=0.4", "color=c=black:s=320x180:r=24", "rgbtestsrc=s=320x180:r=24"],
    }.items():
        args, filt = [], []
        for i, src in enumerate(parts):
            args += ["-f", "lavfi", "-t", "4", "-i", src]
            filt.append(f"[{i}:v]format=yuv420p,setsar=1[v{i}]")
        filt.append("".join(f"[v{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=1:a=0[o]")
        subprocess.run([ff, "-y", "-loglevel", "error", *args, "-filter_complex", ";".join(filt),
                        "-map", "[o]", "-c:v", "libx264", "-preset", "ultrafast", str(root / name)], check=True)

    sr, dur, beat = 22050, 20.0, 0.5
    t = np.arange(int(sr * dur)) / sr
    y = 0.05 * np.sin(2 * np.pi * 220 * t)
    for i in range(int(dur / beat)):
        s = int(i * beat * sr)
        n = min(int(0.2 * sr), len(t) - s)
        tt = np.arange(n) / sr
        loud = 0.9 if i * beat >= 8 else (0.25 if i % 4 == 0 else 0.0)
        y[s:s + n] += loud * np.sin(2 * np.pi * (50 + 80 * np.exp(-30 * tt)) * tt) * np.exp(-10 * tt)
    y = (y / np.abs(y).max()).astype(np.float32)
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "-",
                    str(root / "song.mp3")], input=y.tobytes(), check=True)
    return root


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTAGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture(scope="session")
def edm_song(tmp_path_factory):
    """128 BPM track with a riser + half-bar gap before each drop. Drops at 30.0 s and 90.0 s."""
    import imageio_ffmpeg

    sr, bpm = 22050, 128
    beat = 60 / bpm
    bar = 4 * beat
    plan = [("intro", 8), ("build", 8), ("drop", 16), ("breakdown", 8), ("build", 8), ("drop", 16), ("outro", 4)]
    t = np.arange(int(sr * sum(b for _, b in plan) * bar)) / sr
    y = np.zeros_like(t)
    rng = np.random.default_rng(1)
    pos = 0.0
    for name, bars in plan:
        a, b = pos, pos + bars * bar
        m = (t >= a) & (t < b)
        y[m] += 0.15 * np.sin(2 * np.pi * 330 * t[m])
        k = 0
        while a + k * beat < b - 1e-6:
            s0 = a + k * beat
            i = int(s0 * sr)
            n = min(int(0.3 * sr), len(t) - i)
            tt = np.arange(n) / sr
            kick = np.sin(2 * np.pi * (45 + 100 * np.exp(-35 * tt)) * tt) * np.exp(-8 * tt)
            hat = rng.standard_normal(n) * np.exp(-60 * tt) * 0.25
            if name == "drop":
                y[i:i + n] += kick + hat
            elif name == "build" and s0 < b - bar / 2:
                y[i:i + n] += (0.25 + 0.4 * k * beat / (b - a)) * hat * 3
            elif name in ("intro", "outro") and k % 4 == 0:
                y[i:i + n] += 0.35 * kick
            k += 1
        if name == "build":
            mm = (t >= a) & (t < b - bar / 2)
            y[mm] += 0.25 * rng.standard_normal(mm.sum()) * ((t[mm] - a) / (b - a)) ** 2
        if name == "drop":
            y[m] += 0.35 * np.sin(2 * np.pi * 55 * t[m])
        pos = b
    y = (y / np.abs(y).max()).astype(np.float32)
    out = tmp_path_factory.mktemp("edm") / "edm.mp3"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "f32le", "-ar", str(sr),
                    "-ac", "1", "-i", "-", str(out)], input=y.tobytes(), check=True)
    return out
