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
MUSIC_VERSION = 4
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
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
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

    sections = _sections(y, sr, beat_frames, beats, downbeats, energy, energy_rate, duration, librosa)
    check_cancel(cancel)
    drops = _drops(sections, energy, energy_rate, downbeats)

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
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(asdict(song)), encoding="utf-8")
    if progress:
        progress(1.0, f"{tempo:.0f} BPM, {len(sections)} sections, {len(drops)} drop(s)")
    return song


def _sections(y, sr, beat_frames, beats, downbeats, energy, energy_rate, duration, librosa) -> list[Section]:
    """Segment the song with agglomerative clustering on beat-synced timbre."""
    mfcc = librosa.feature.mfcc(y=y, sr=sr, hop_length=HOP, n_mfcc=13)
    chroma = librosa.feature.chroma_stft(y=y, sr=sr, hop_length=HOP)
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


def _drops(sections, energy, energy_rate, downbeats) -> list[float]:
    """Moments where the energy jumps: the start of loud sections after quieter ones."""
    drops = []
    for prev, cur in zip(sections[:-1], sections[1:]):
        if cur.label == "drop" and cur.energy - prev.energy >= 0.25:
            drops.append(cur.start)
    # Also catch sudden jumps inside sections (e.g. a drop the segmenter missed).
    w = int(2 * energy_rate)
    e = np.asarray(energy)
    db = np.asarray(downbeats)
    for i in range(w, len(e) - w, int(energy_rate)):
        before, after = e[i - w:i].mean(), e[i:i + w].mean()
        if after - before > 0.35 and after > 0.6:
            t = i / energy_rate
            if len(db):
                t = float(db[np.argmin(np.abs(db - t))])
            if all(abs(t - d) > 8 for d in drops):
                drops.append(round(t, 3))
    return sorted(drops)


def _from_json(d: dict) -> SongMap:
    d["sections"] = [Section(**s) for s in d["sections"]]
    d["hits"] = [tuple(h) for h in d["hits"]]
    return SongMap(**d)
