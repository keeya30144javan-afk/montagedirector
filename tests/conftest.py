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


@pytest.fixture(scope="session")
def band_song(tmp_path_factory):
    """120 BPM rock-style track with known events: cymbal intro until 8.0, fill at 15.5,
    drums faster 24-32, stop 37-38, lead solo 40-56, four hard hits at 56.0-57.5,
    last note rings out from 68.0."""
    import imageio_ffmpeg

    out = tmp_path_factory.mktemp("band") / "band.mp3"
    sr = 22050; beat = 0.5; dur = 74.0
    t = np.arange(int(sr * dur)) / sr
    y = np.zeros_like(t); rng = np.random.default_rng(7)
    def add(start, sig):
        i = int(start * sr); j = min(len(y), i + len(sig)); y[i:j] += sig[:j - i]
    def kick(g=1.0):
        tt = np.arange(int(0.35 * sr)) / sr; return g * np.sin(2*np.pi*(45 + 110*np.exp(-30*tt))*tt) * np.exp(-9*tt)
    def snare(g=0.6):
        tt = np.arange(int(0.25 * sr)) / sr; return g * (rng.standard_normal(len(tt)) * np.exp(-22*tt) + 0.3*np.sin(2*np.pi*190*tt)*np.exp(-30*tt))
    def hat(g=0.18):
        tt = np.arange(int(0.06 * sr)) / sr; n = rng.standard_normal(len(tt)); n = np.diff(np.r_[0, n]); return g * n * np.exp(-70*tt)
    def groove(a, b, dense=False, light=False):
        k = 0; x = a
        while x < b - 1e-6:
            pos = k % 8   # 8th notes
            if dense:  # drums faster: kick+snare on every 8th
                add(x, kick(0.8)); add(x, snare(0.45 if pos % 2 else 0.3))
            else:
                if pos in (0, 4): add(x, kick(0.55 if light else 1.0))
                if pos in (2, 6): add(x, snare(0.35 if light else 0.6))
            add(x, hat()); x += beat / 2; k += 1
    # 0-8 intro: cymbal wash only + soft pad
    m = t < 8; y[m] += 0.05 * rng.standard_normal(m.sum()) * np.exp(-((t[m] % 2) * 1.5)) + 0.05*np.sin(2*np.pi*220*t[m])
    bass_line = lambda a, b, f=55: ((t >= a) & (t < b)) * 0.28 * np.sin(2*np.pi*f*t)
    groove(8, 24); y += bass_line(8, 24)
    # fill: 16th-note snares in the last beat before 16.0
    for i in range(4): add(15.5 + i * 0.125, snare(0.7))
    groove(24, 32, dense=True); y += bass_line(24, 32)
    groove(32, 37); y += bass_line(32, 37)
    # stop 37.0-38.0: silence
    groove(38, 40); y += bass_line(38, 40)
    groove(40, 56, light=True); y += bass_line(40, 56, 82)
    # solo 40-56: bright melodic lead with vibrato
    notes = [1320, 1480, 1660, 1760, 1980, 1760, 1660, 1480]
    for i, f in enumerate(notes * 2):
        a = 40 + i * 1.0; mm = (t >= a) & (t < a + 1.0); tt = t[mm] - a
        y[mm] += 0.22 * np.sin(2*np.pi*f*tt + 6*np.sin(2*np.pi*5.5*tt)) * np.minimum(1, tt*20)
    # 4 hard hits with silence between: 56.0 56.5 57.0 57.5
    for x in (56.0, 56.5, 57.0, 57.5): add(x, kick(1.2)); add(x, snare(0.9))
    groove(58, 68); y += bass_line(58, 68)
    # last hit at 68, then a chord rings out to the end
    add(68.0, kick(1.0)); add(68.0, snare(0.7))
    mm = t >= 68.0; tt = t[mm] - 68.0
    y[mm] += sum(0.12 * np.sin(2*np.pi*f*tt) for f in (220, 277, 330, 440)) * np.exp(-tt / 2.5)
    y = (y / np.abs(y).max()).astype(np.float32)
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "f32le", "-ar", str(sr), "-ac", "1",
                    "-i", "-", "-b:a", "192k", str(out)], input=y.tobytes(), check=True)
    return out
