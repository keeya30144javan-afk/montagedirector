"""Song analysis: tempo, beats, bars, sections, drops and hits.

Produces a SongMap: everything the directors need to know about the music,
in seconds. Results are cached per file.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .ffmpeg_tools import MediaError, check_cancel, decode_audio
from .paths import cache_dir, file_key

log = logging.getLogger(__name__)

SR = 22050
HOP = 512
MUSIC_VERSION = 8
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}


@dataclass
class Section:
    start: float
    end: float
    label: str        # intro | verse | build | drop | breakdown | outro
    energy: float     # 0..1 relative to the song

    def to_dict(self):
        return {"start": round(self.start, 2), "end": round(self.end, 2),
                "label": self.label, "energy": round(self.energy, 2)}


@dataclass
class SongMap:
    path: str
    duration: float
    tempo: float
    beats: list[float]
    downbeats: list[float]
    sections: list[Section]
    drops: list[float]
    hits: list[tuple[float, float]]          # (time, strength 0..1), strongest transients
    energy: list[float] = field(default_factory=list)  # 0..1 sampled at ENERGY_RATE
    energy_rate: float = 10.0
    kicks: list[float] = field(default_factory=list)    # drum hits by instrument (seconds)
    snares: list[float] = field(default_factory=list)
    hats: list[float] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)    # what an editor cuts to (see listening.py)
    feel: dict = field(default_factory=dict)            # overall feel: pace, steady beat, drive... (listening.py)

    def events_of(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["kind"] == kind]

    def energy_at(self, t0: float, t1: float | None = None) -> float:
        if not self.energy:
            return 0.5
        a = int(max(0, t0) * self.energy_rate)
        b = int(max(a + 1, (t1 if t1 is not None else t0) * self.energy_rate + 1))
        chunk = self.energy[a:b] or self.energy[-1:]
        return float(np.mean(chunk))

    def section_at(self, t: float) -> Section:
        for s in self.sections:
            if s.start <= t < s.end:
                return s
        return self.sections[-1]

    def to_prompt_dict(self, start: float, end: float) -> dict:
        """Compact description for the AI director, limited to [start, end)."""
        return {
            "tempo_bpm": round(self.tempo, 1),
            "beat_seconds": round(60.0 / self.tempo, 3) if self.tempo else None,
            "range": [round(start, 2), round(end, 2)],
            "sections": [s.to_dict() for s in self.sections if s.end > start and s.start < end],
            "drops": [round(d, 2) for d in self.drops if start <= d < end],
            "bar_starts": [round(b, 2) for b in self.downbeats if start <= b < end],
            "feel": {k: v for k, v in self.feel.items() if k in ("pace", "steady", "drive", "low_end", "dynamics", "words")},
            "heard_events": [{"kind": e["kind"], "time": round(e["time"], 2), "end": round(e.get("end") or e["time"], 2)}
                             for e in self.events if e["kind"] != "fill" and start <= e["time"] < end],
            "biggest_hits": [round(t, 2) for t, _ in sorted(
                (h for h in self.hits if start <= h[0] < end), key=lambda h: -h[1])[:24]],
        }


def analyze_song(path: str | Path, cancel=None, progress=None) -> SongMap:
    path = Path(path)
    if not path.is_file():
        raise MediaError(f"Song not found: {path}")
    out = cache_dir() / "music" / f"{file_key(path, MUSIC_VERSION)}.json"
    if out.is_file():
        try:
            return _from_json(json.loads(out.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    import librosa  # slow import; keep it out of app start-up

    if progress:
        progress(0.05, f"Listening to {path.name}")
    try:
        y, sr = librosa.load(str(path), sr=SR, mono=True)
    except Exception as exc:
        log.debug("librosa could not decode %s (%s); using ffmpeg", path.name, exc)
        y, sr = decode_audio(path, SR), SR
    duration = len(y) / sr
    if duration < 3:
        raise MediaError("The song is shorter than 3 seconds.")
    check_cancel(cancel)

    if progress:
        progress(0.25, "Finding the beat")
    # One spectrogram for everything (beats, sections, drops, listening).
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP)).astype(np.float32)
    mel = librosa.feature.melspectrogram(S=S ** 2, sr=sr)
    onset_env = librosa.onset.onset_strength(S=librosa.power_to_db(mel), sr=sr, hop_length=HOP)
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr, hop_length=HOP)
    tempo = float(np.atleast_1d(tempo)[0]) or 120.0
    beats = librosa.frames_to_time(beat_frames, sr=sr, hop_length=HOP)
    if len(beats) < 8:  # beatless audio: fall back to a steady grid
        beats = np.arange(0.0, duration, 60.0 / tempo)
        beat_frames = librosa.time_to_frames(beats, sr=sr, hop_length=HOP)
    check_cancel(cancel)

    # Downbeat phase: the bar position whose beats carry the most energy.
    strengths = onset_env[np.clip(beat_frames, 0, len(onset_env) - 1)]
    phase = int(np.argmax([strengths[p::4].mean() if len(strengths[p::4]) else 0 for p in range(4)]))
    downbeats = beats[phase::4]

    if progress:
        progress(0.45, "Mapping intro, build-ups and drops")
    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]
    rms_t = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=HOP)
    energy_rate = 10.0
    grid = np.arange(0, duration, 1.0 / energy_rate)
    energy = np.interp(grid, rms_t, rms)
    energy = np.convolve(energy, np.ones(5) / 5, mode="same")
    lo, hi = np.percentile(energy, 3), np.percentile(energy, 98)
    energy = np.clip((energy - lo) / max(hi - lo, 1e-9), 0, 1)

    sections = _sections(y, sr, beat_frames, beats, downbeats, energy, energy_rate, duration, librosa, S, mel)
    check_cancel(cancel)
    drops = _detect_drops(y, sr, onset_env, beats, downbeats, tempo, duration, librosa, S)
    sections = _align_sections(sections, drops, energy, energy_rate, duration)
    check_cancel(cancel)
    if progress:
        progress(0.6, "Listening to the drums, bass and melody")
    from .listening import listen

    heard = listen(y, sr, beats, downbeats, tempo, duration, drops, librosa, S)

    if progress:
        progress(0.8, "Picking the biggest hits")
    peaks = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr, hop_length=HOP,
                                       units="frames", backtrack=False)
    hits: list[tuple[float, float]] = []
    if len(peaks):
        s = onset_env[peaks]
        s = (s - s.min()) / max(s.max() - s.min(), 1e-9)
        times = librosa.frames_to_time(peaks, sr=sr, hop_length=HOP)
        hits = [(round(float(t), 3), round(float(v), 3)) for t, v in zip(times, s)]

    song = SongMap(
        path=str(path), duration=round(duration, 3), tempo=round(tempo, 2),
        beats=[round(float(b), 3) for b in beats],
        downbeats=[round(float(b), 3) for b in downbeats],
        sections=sections, drops=drops, hits=hits,
        energy=[round(float(e), 3) for e in energy], energy_rate=energy_rate,
        kicks=heard["kicks"], snares=heard["snares"], hats=heard["hats"], events=heard["events"],
        feel=heard.get("feel", {}),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(asdict(song)), encoding="utf-8")
    if progress:
        progress(1.0, f"{tempo:.0f} BPM, {len(sections)} sections, {len(drops)} drop(s)")
    return song


def _sections(y, sr, beat_frames, beats, downbeats, energy, energy_rate, duration, librosa, S=None,
              mel=None) -> list[Section]:
    """Segment the song with agglomerative clustering on beat-synced timbre."""
    if S is None:
        S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    if mel is None:
        mel = librosa.feature.melspectrogram(S=S ** 2, sr=sr)
    mfcc = librosa.feature.mfcc(S=librosa.power_to_db(mel), sr=sr, n_mfcc=13)
    chroma = librosa.feature.chroma_stft(S=S ** 2, sr=sr)
    feats = np.vstack([librosa.util.normalize(mfcc, axis=1), chroma])
    sync = librosa.util.sync(feats, beat_frames, aggregate=np.median)
    k = int(np.clip(round(duration / 9.0), 4, 24))
    k = min(k, max(2, sync.shape[1] - 1))
    try:
        bounds_beats = librosa.segment.agglomerative(sync, k)
    except Exception:
        bounds_beats = np.linspace(0, sync.shape[1] - 1, k + 1).astype(int)[:-1]
    sync_times = np.r_[0.0, beats]  # sync() prepends a column for frames before the first beat
    starts = sorted({0.0, *[float(sync_times[min(b, len(sync_times) - 1)]) for b in bounds_beats]})

    # Snap boundaries to the nearest downbeat and drop tiny sections.
    db = np.asarray(downbeats)
    snapped = [0.0]
    for t in starts[1:]:
        if len(db):
            t = float(db[np.argmin(np.abs(db - t))])
        if t - snapped[-1] >= 6.0 and duration - t >= 4.0:
            snapped.append(t)
    edges = snapped + [duration]

    def mean_energy(a, b):
        seg = energy[int(a * energy_rate):max(int(a * energy_rate) + 1, int(b * energy_rate))]
        return float(np.mean(seg)) if len(seg) else 0.0

    raw = [mean_energy(a, b) for a, b in zip(edges[:-1], edges[1:])]
    lo, hi = min(raw), max(raw)
    rel = [(e - lo) / max(hi - lo, 1e-9) for e in raw]
    # Loud genres (rock, metal, EDM) sit near the top all the time, so also rank
    # each section against the others: only the hottest third counts as a drop.
    order = np.argsort(np.argsort(raw))
    rank = [float(o) / max(1, len(raw) - 1) for o in order]
    sections: list[Section] = []
    for i, (a, b) in enumerate(zip(edges[:-1], edges[1:])):
        e = rel[i]
        prev_e = rel[i - 1] if i else 0.0
        next_e = rel[i + 1] if i + 1 < len(rel) else 0.0
        first_half = mean_energy(a, (a + b) / 2)
        second_half = mean_energy((a + b) / 2, b)
        if i == 0 and e < 0.5:
            label = "intro"
        elif i == len(rel) - 1 and e < 0.6:
            label = "outro"
        elif e >= 0.66 and rank[i] >= 0.55:
            label = "drop"
        elif second_half > first_half * 1.25 and next_e > e:
            label = "build"
        elif e < 0.35 and prev_e >= 0.6:
            label = "breakdown"
        else:
            label = "verse"
        if sections and sections[-1].label == label and (b - sections[-1].start) <= 40.0:
            # merge neighbours that play the same role, energy weighted by length
            prev = sections[-1]
            w0, w1 = prev.end - prev.start, b - a
            prev.energy = round((prev.energy * w0 + e * w1) / (w0 + w1), 3)
            prev.end = round(b, 3)
        else:
            sections.append(Section(round(a, 3), round(b, 3), label, round(e, 3)))
    return sections


def _detect_drops(y, sr, onset_env, beats, downbeats, tempo, duration, librosa, S=None) -> list[float]:
    """Find the moments the beat drops.

    A drop is where the low end (kick + bass), overall loudness and hit density
    jump up together, usually right after a riser, a cut-out or a quiet bar.
    Every downbeat is scored by how much harder the next two bars hit than the
    two bars before it, with a bonus for a dip just before it. The winners are
    then moved to the exact beat where the low end slams back in.
    """
    if S is None:
        S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    rate = sr / HOP  # analysis frames per second

    def norm(x, smooth_s):
        k = max(1, int(smooth_s * rate))
        x = np.convolve(x, np.ones(k) / k, mode="same")
        return np.clip(x / max(np.percentile(x, 95), 1e-9), 0, 1.5)

    low_raw = S[freqs < 150].sum(axis=0)
    low = norm(low_raw, 0.25)
    loud = norm(np.sqrt((S ** 2).mean(axis=0)), 0.25)
    n = min(len(low), len(loud), len(onset_env))
    density = norm(onset_env[:n], 1.0)
    intensity = 0.45 * low[:n] + 0.35 * loud[:n] + 0.20 * density

    beat = 60.0 / tempo if tempo else 0.5
    bar = 4 * beat

    def mean(a, b):
        i, j = max(0, int(a * rate)), min(n, int(b * rate))
        return float(intensity[i:j].mean()) if j > i else 0.0

    candidates = []
    for d in downbeats:
        if d < 2 * bar or d > duration - 2 * bar:
            continue
        after = mean(d, d + 2 * bar)
        before = mean(d - 2 * bar, d - 0.05)
        last_beat = mean(d - beat, d - 0.05)
        contrast = after - before
        dip = after - last_beat
        score = contrast + 0.35 * max(0.0, dip - contrast)
        if after >= 0.55 and contrast >= 0.15:
            candidates.append((score, float(d)))

    # Strongest first; drops are at least ~12 s apart.
    chosen: list[float] = []
    for score, d in sorted(candidates, reverse=True):
        if score < 0.2 or any(abs(d - c) < max(12.0, 4 * bar) for c in chosen):
            continue
        chosen.append(d)

    # Land exactly where the kick/bass comes back: biggest low-end jump within
    # half a beat, then snapped to the closest beat.
    low_jump = np.r_[0.0, np.diff(low_raw)]
    beats_a = np.asarray(beats)
    refined = []
    for d in chosen:
        i0, i1 = max(0, int((d - beat * 0.5) * rate)), min(len(low_jump), int((d + beat * 0.5) * rate) + 1)
        t = (i0 + int(np.argmax(low_jump[i0:i1]))) / rate if i1 > i0 else d
        if len(beats_a):
            t = float(beats_a[np.argmin(np.abs(beats_a - t))])
        refined.append(round(t, 3))
    return sorted(set(refined))


def _align_sections(sections, drops, energy, energy_rate, duration) -> list[Section]:
    """Make every drop start a 'drop' section so the fast cutting begins exactly on it."""
    out = [Section(s.start, s.end, s.label, s.energy) for s in sections]
    for d in drops:
        i = next((k for k, s in enumerate(out) if s.start <= d < s.end), None)
        if i is None:
            continue
        sec = out[i]
        if abs(sec.start - d) < 0.05:
            sec.label = "drop"
            continue
        if i > 0 and abs(sec.start - d) <= 4.0 and d > out[i - 1].start + 2.0:
            # boundary was close: move it onto the drop
            out[i - 1].end = d
            sec.start = d
            sec.label = "drop"
            continue
        if sec.end - d < 2.0:
            continue
        # split: the part from the drop onward is the drop
        tail = energy[int(d * energy_rate):int(sec.end * energy_rate)]
        tail_e = float(np.mean(tail)) if len(tail) else sec.energy
        out.insert(i + 1, Section(round(d, 3), sec.end, "drop", round(max(sec.energy, min(1.0, tail_e)), 3)))
        sec.end = round(d, 3)
    # Tidy: drop zero-length sections, merge neighbours that are both drops.
    tidy: list[Section] = []
    for s in out:
        if s.end - s.start < 0.05:
            continue
        if tidy and tidy[-1].label == s.label == "drop" and s.start not in drops:
            tidy[-1].end = s.end
        else:
            tidy.append(s)
    return tidy


def _from_json(d: dict) -> SongMap:
    d["sections"] = [Section(**s) for s in d["sections"]]
    d["hits"] = [tuple(h) for h in d["hits"]]
    return SongMap(**d)
