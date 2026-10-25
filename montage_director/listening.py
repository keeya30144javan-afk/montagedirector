"""Hearing the song: instrument lanes and musical events.

The mix is split into a harmonic part (bass, chords, melody, sustained notes)
and a percussive part (drum hits) with harmonic/percussive source separation,
then into frequency lanes:

    kick   percussive, below 150 Hz       snare  percussive, 150 Hz - 5 kHz
    hats   percussive, 6 - 11 kHz          bass   harmonic, below 250 Hz
    melody harmonic, 250 Hz - 4 kHz        lead   harmonic, 900 Hz - 4 kHz

Each drum lane gets its own hit detection. From the lanes, bar by bar, we
find the moments an editor cuts to: the quiet intro before the beat comes
in, the drums speeding up, drum fills, sudden stops, risers into a drop,
lead / solo passages, groups of hard accent hits, and the last note ringing
out at the end.
"""

from __future__ import annotations

import numpy as np

HOP = 512

# Event kinds (also what the free prompt can refer to).
EVENT_LABELS = {
    "intro": "quiet intro",
    "drums_faster": "drums get faster / busier",
    "fill": "drum fill",
    "stop": "sudden stop",
    "riser": "riser",
    "solo": "lead / solo",
    "hits": "hard hits",
    "ringout": "last note rings out",
}


def listen(y, sr, beats, downbeats, tempo, duration, drops, librosa, S=None) -> dict:
    beats = np.asarray(beats, float)
    downbeats = np.asarray(downbeats, float)
    beat = 60.0 / tempo if tempo else 0.5
    rate = sr / HOP

    if S is None:
        S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP)).astype(np.float32)
    H, P = fast_hpss(S)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)

    def band(M, lo, hi):
        return M[(freqs >= lo) & (freqs < hi)].sum(axis=0)

    def norm(x, smooth_s=0.0):
        if smooth_s:
            k = max(1, int(smooth_s * rate))
            x = np.convolve(x, np.ones(k) / k, mode="same")
        return x / max(np.percentile(x, 95), 1e-9)

    kick_c, snare_c, hat_c = band(P, 30, 150), band(P, 150, 5000), band(P, 6000, 11000)
    bass = norm(band(H, 30, 250), 0.25)
    melody = norm(band(H, 250, 4000), 0.25)
    lead_ratio = band(H, 900, 4000) / (band(H, 250, 4000) + 1e-9)
    perc = norm(P.sum(axis=0), 0.1)
    full = norm(S.sum(axis=0), 0.1)
    n = S.shape[1]
    times = np.arange(n) / rate

    kicks, snares, hats = _drum_hits(kick_c, snare_c, hat_c, rate, librosa)
    level_med = max(float(np.median(full)), 1e-6)  # the song's normal loudness

    def mean(curve, a, b):
        i, j = max(0, int(a * rate)), min(len(curve), int(b * rate))
        return float(curve[i:j].mean()) if j > i else 0.0

    def count(hits, a, b):
        return int(sum(1 for t, _ in hits if a <= t < b))

    bars = list(downbeats) + [duration]
    stats = []
    for a, b in zip(bars[:-1], bars[1:]):
        stats.append({
            "start": float(a), "end": float(b),
            "kick": count(kicks, a, b), "snare": count(snares, a, b), "hat": count(hats, a, b),
            "energy": mean(full, a, b), "perc": mean(perc, a, b), "bass": mean(bass, a, b),
            "melody": mean(melody, a, b), "lead": mean(lead_ratio, a, b),
        })

    events: list[dict] = []

    # Quiet intro: until the beat (kick + bass) properly comes in.
    beat_in = None
    for i, st in enumerate(stats[:-1]):
        if st["kick"] >= 2 and st["perc"] > 0.35 and stats[i + 1]["kick"] >= 2:
            beat_in = st["start"]
            break
    first_drop = next((d for d in drops if d < duration * 0.5), None)
    if first_drop is not None and (beat_in is None or first_drop <= beat_in + 2 * 4 * beat):
        beat_in = first_drop
    if beat_in and beat_in >= 2 * beat:
        events.append({"kind": "intro", "time": 0.0, "end": round(beat_in, 3), "strength": 1.0})

    # Drums speeding up: hits per bar jump well above the last few bars and stay up.
    dens = np.array([st["kick"] + st["snare"] for st in stats], float)
    last_fast = -99
    for i in range(4, len(stats) - 1):
        base = max(2.0, float(np.median(dens[i - 4:i])))
        if dens[i] >= max(6, 1.5 * base) and dens[i + 1] >= 1.3 * base and i - last_fast > 8:
            j = i
            while j + 1 < len(stats) and dens[j + 1] >= 1.2 * base and j - i < 16:
                j += 1
            events.append({"kind": "drums_faster", "time": round(stats[i]["start"], 3),
                           "end": round(stats[j]["end"], 3), "strength": round(min(1.0, dens[i] / (2 * base)), 2)})
            last_fast = i

    # Drum fills: 3+ snare/tom hits packed tighter than a third of a beat.
    sn = [t for t, _ in snares]
    i = 0
    while i < len(sn):
        j = i
        while j + 1 < len(sn) and sn[j + 1] - sn[j] <= beat / 3 + 0.01:
            j += 1
        if j - i + 1 >= 3:
            events.append({"kind": "fill", "time": round(sn[i], 3), "end": round(sn[j] + beat / 3, 3),
                           "strength": round(min(1.0, (j - i + 1) / 4), 2), "times": [round(t, 3) for t in sn[i:j + 1]]})
        i = j + 1

    # Sudden stops: the whole band drops out for at least half a beat.
    win = max(1, int(0.5 * beat * rate))
    prev_level = None
    last_stop = -99.0
    for i in range(0, n - win, win):
        level = float(full[i:i + win].mean())
        if (prev_level is not None and prev_level > 0.6 * level_med and level < 0.18 * prev_level
                and times[i] - last_stop > 4 * beat):
            j = i + win
            while j < n - win and full[j:j + win].mean() < 0.5 * prev_level:
                j += win
            if times[j] < duration - 1.0:  # not the end of the song
                events.append({"kind": "stop", "time": round(float(times[i]), 3), "end": round(float(times[j]), 3),
                               "strength": round(1 - level / prev_level, 2)})
                last_stop = times[i]
        prev_level = level if prev_level is None else 0.6 * prev_level + 0.4 * level

    # Risers: cymbals / noise building over the bars right before a drop.
    for d in drops:
        a = d - 2 * 4 * beat
        if a <= 0:
            continue
        early, late = mean(norm(hat_c, 0.3), a, a + 4 * beat), mean(norm(hat_c, 0.3), d - 4 * beat, d - 0.05)
        if late > 1.4 * early + 0.05:
            events.append({"kind": "riser", "time": round(a, 3), "end": round(d, 3),
                           "strength": round(min(1.0, late / (early + 1e-6) / 3), 2)})

    # Lead / solo: bars where a bright melodic line stands out from the usual mix.
    lead = np.array([st["lead"] for st in stats])
    mel = np.array([st["melody"] for st in stats])
    if len(lead) >= 8:
        z = (lead - np.median(lead)) / (np.std(lead) + 1e-9)
        hot = (z > 0.9) & (mel > 0.35)
        i = 0
        while i < len(hot):
            if hot[i]:
                j = i
                while j + 1 < len(hot) and (hot[j + 1] or (j + 2 < len(hot) and hot[j + 2])):
                    j += 1
                if j - i + 1 >= 4:
                    events.append({"kind": "solo", "time": round(stats[i]["start"], 3), "end": round(stats[j]["end"], 3),
                                   "strength": round(float(min(1.0, z[i:j + 1].mean() / 2)), 2)})
                i = j + 1
            else:
                i += 1

    # Hard hits: 3-6 big accents in a row with the band dropping out between them.
    accents = sorted([(t, s) for t, s in kicks + snares if s >= 0.6])
    merged: list[tuple[float, float]] = []
    for t, s in accents:  # kick + snare on the same accent count once
        if merged and t - merged[-1][0] < 0.08:
            merged[-1] = (merged[-1][0], max(merged[-1][1], s))
        else:
            merged.append((t, s))
    def quiet_gap(a, b):  # the band drops out between two accents
        gap = mean(full, a + (b - a) * 0.4, a + (b - a) * 0.85)
        return gap < 0.5 * level_med and gap < 0.15 * max(mean(full, a, a + 0.06), mean(full, b, b + 0.06))

    i = 0
    while i < len(merged):
        group = [merged[i]]
        j = i + 1
        while j < len(merged) and merged[j][0] - group[-1][0] <= 1.25 * beat and quiet_gap(group[-1][0], merged[j][0]):
            group.append(merged[j])
            j += 1
        # A final "hit" the band keeps playing after is the groove coming back, not a stab.
        while len(group) > 3:
            nxt = merged[j][0] if j < len(merged) else group[-1][0] + beat
            if quiet_gap(group[-1][0], min(nxt, group[-1][0] + beat)):
                break
            group.pop()
        if 3 <= len(group) <= 8:
            gaps = np.diff([t for t, _ in group])
            if gaps.std() / (gaps.mean() + 1e-9) < 0.35:
                events.append({"kind": "hits", "time": round(group[0][0], 3), "end": round(group[-1][0] + 0.5 * beat, 3),
                               "strength": round(float(np.mean([s for _, s in group])), 2),
                               "times": [round(t, 3) for t, _ in group]})
                i = j
                continue
        i += 1
    # The gaps between hard hits are not "stops".
    spans = [(e["time"], e["end"]) for e in events if e["kind"] == "hits"]
    events = [e for e in events if not (e["kind"] == "stop" and any(a - 0.1 <= e["time"] <= b for a, b in spans))]

    # Ring-out: after the last real drum hit, a note or chord keeps sounding.
    strong = [t for t, s in kicks + snares if s >= 0.3]
    if strong:
        last_hit = max(strong)
        if duration - last_hit >= 2.0 and mean(melody, last_hit + 0.3, duration) + mean(bass, last_hit + 0.3, duration) > 0.05:
            events.append({"kind": "ringout", "time": round(last_hit, 3), "end": round(duration, 3), "strength": 1.0})

    events.sort(key=lambda e: e["time"])
    feel = _feel(S, H, P, freqs, kicks, snares, hats, beat, duration, stats, tempo)
    return {
        "feel": feel,
        "kicks": [round(t, 3) for t, s in kicks if s >= 0.15],
        "snares": [round(t, 3) for t, s in snares if s >= 0.15],
        "hats": [round(t, 3) for t, s in hats if s >= 0.15],
        "events": events,
    }


def fast_hpss(S: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Harmonic / percussive separation (soft masks, like librosa's hpss) about 5x faster.

    Sustained sounds don't need fine timing, so the harmonic median (along time) runs on
    4-frame blocks; drum hits don't need fine pitch, so the percussive median (along
    frequency) runs on pairs of bins. Both filters are split into independent chunks and
    run on all cores.
    """
    import os
    from concurrent.futures import ThreadPoolExecutor

    from scipy import ndimage

    F, N = S.shape
    n4 = N // 4 * 4
    blocks = S[:, :n4].reshape(F, -1, 4).mean(axis=2)
    if N > n4:
        blocks = np.concatenate([blocks, S[:, n4:].mean(axis=1, keepdims=True)], axis=1)
    f2 = F // 2 * 2
    pairs = S[:f2].reshape(-1, 2, N).mean(axis=1)
    workers = max(1, min(8, os.cpu_count() or 1))
    rows = np.array_split(np.arange(blocks.shape[0]), workers)
    cols = np.array_split(np.arange(N), workers)
    with ThreadPoolExecutor(workers) as ex:
        h_parts = ex.map(lambda r: ndimage.median_filter(blocks[r], size=(1, 9), mode="reflect"), rows)
        harm = np.concatenate(list(h_parts), axis=0)
        p_parts = ex.map(lambda c: ndimage.median_filter(pairs[:, c], size=(17, 1), mode="reflect"), cols)
        perc = np.concatenate(list(p_parts), axis=1)
    harm = np.repeat(harm, 4, axis=1)[:, :N]
    perc = np.repeat(perc, 2, axis=0)
    if F > f2:
        perc = np.concatenate([perc, perc[-1:]], axis=0)
    h2, p2 = harm * harm, perc * perc
    total = h2 + p2 + 1e-12
    return S * (h2 / total), S * (p2 / total)


def _feel(S, H, P, freqs, kicks, snares, hats, beat, duration, stats, tempo) -> dict:
    """The overall feel of the song, as far as it can be heard reliably:
    how fast the beat really is for cutting, whether there is a steady drum beat at all,
    how drum-driven and bass-heavy it is, and how much the loudness moves."""
    power_p, power_h = float((P ** 2).sum()), float((H ** 2).sum())
    drive = power_p / max(power_p + power_h, 1e-9)
    low = float((S[freqs < 120] ** 2).sum() / max(float((S ** 2).sum()), 1e-9))
    beats_total = max(1.0, duration / beat)
    strong_kicks = sum(1 for _, s in kicks if s >= 0.35)
    strong_drums = strong_kicks + sum(1 for _, s in snares if s >= 0.35)
    # A steady beat: most bars have kicks or snares in them.
    with_drums = [st for st in stats if st["kick"] + st["snare"] >= 2]
    steady = len(with_drums) >= 0.5 * max(1, len(stats)) and strong_drums >= 0.35 * beats_total
    energies = np.array([st["energy"] for st in stats]) if stats else np.array([0.5])
    dyn = float(np.percentile(energies, 90) - np.percentile(energies, 10)) if len(energies) > 3 else 0.0
    # Cut length: styles are written for a ~0.5 s beat (120 BPM). Very fast songs cut every
    # other beat, very slow ones twice per beat; songs without drums cut on phrases.
    scale = float(2.0 ** round(np.log2(0.5 / beat))) if beat > 0 else 1.0
    scale = float(np.clip(scale, 0.5, 2.0))
    if not steady:
        scale = max(scale, 2.0)
    pace = "fast" if beat < 0.4 else ("slow" if beat > 0.75 else "medium")
    words = [f"{tempo:.0f} BPM ({pace})"]
    if not steady:
        words.append("no steady drum beat")
    elif drive > 0.3:
        words.append("drum-driven")
    if low > 0.6:
        words.append("heavy low end")
    if dyn > 0.45:
        words.append("big quiet/loud contrast")
    elif dyn < 0.15:
        words.append("even loudness")
    return {"bpm": round(float(tempo), 1), "beat": round(float(beat), 3), "pace": pace, "steady": bool(steady),
            "drive": round(drive, 3), "low_end": round(low, 3), "dynamics": round(dyn, 3), "rate_scale": scale,
            "words": words}


def _drum_hits(kick_c, snare_c, hat_c, rate, librosa):
    """Find every drum hit once, then label it by which lane jumped the most.

    Lanes bleed into each other (a snare has low-end thump, a kick has click),
    so per-lane peak picking hears every hit in every lane. Instead: onsets of
    the summed percussive rise, each classified by its lane-normalised rise.
    A kick and snare played together get both labels.
    """
    rises = []
    for c in (kick_c, snare_c, hat_c):
        lc = np.log1p(c / max(np.percentile(c, 50), 1e-9))
        r = np.maximum(0.0, np.diff(lc, prepend=lc[:1]))
        rises.append(r / max(np.percentile(r, 99.5), 1e-9))
    total = rises[0] + rises[1] + 0.6 * rises[2]
    onsets = _hits(total, rate, librosa, pre_log=False)
    lanes: list[list[tuple[float, float]]] = [[], [], []]
    for t, _ in onsets:
        f = int(round(t * rate))
        w = slice(max(0, f - 1), f + 2)
        vals = [float(r[w].max()) for r in rises]
        best = max(vals)
        if best <= 0.05:
            continue
        for lane, v in enumerate(vals):
            if v >= 0.7 * best or (lane == 0 and v >= 0.5 and vals[1] == best):
                lanes[lane].append((t, float(min(1.0, v))))
    return lanes[0], lanes[1], lanes[2]


def _hits(curve: np.ndarray, rate: float, librosa, min_gap: float = 0.07,
          pre_log: bool = True) -> list[tuple[float, float]]:
    """Onsets in one lane: peaks of the rise in (log) energy, with strength 0..1."""
    if pre_log:
        c = np.log1p(curve / max(np.percentile(curve, 50), 1e-9))
        flux = np.maximum(0.0, np.diff(c, prepend=c[:1]))
    else:
        flux = np.asarray(curve, float)
    if not flux.any():
        return []
    top = max(np.percentile(flux, 99.5), 1e-9)
    delta = max(np.percentile(flux, 85), top * 0.12)
    peaks = librosa.util.peak_pick(flux, pre_max=3, post_max=3, pre_avg=12, post_avg=12, delta=delta,
                                   wait=max(1, int(min_gap * rate)))
    return [(float(p / rate), float(min(1.0, flux[p] / top))) for p in peaks]


def describe(events: list[dict]) -> list[str]:
    """Human-readable list of what was heard, for the app."""
    out = []
    for e in events:
        if e["kind"] == "fill":
            continue  # too many to list; they're marked on the timeline
        label = EVENT_LABELS.get(e["kind"], e["kind"])
        span = f"{_mmss(e['time'])}–{_mmss(e['end'])}" if e.get("end") and e["end"] - e["time"] >= 3 else _mmss(e["time"])
        extra = f" ({len(e['times'])})" if e["kind"] == "hits" and e.get("times") else ""
        out.append(f"{label}{extra} {span}")
    return out


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}"
