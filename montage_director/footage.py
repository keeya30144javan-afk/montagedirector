"""Footage analysis: split videos into shots and score each shot.

Every source video is decoded once at a low frame rate and resolution. From
those frames we find hard cuts (histogram jumps), measure motion (frame
difference), brightness and saturation, and save a thumbnail per shot. Long
shots are split into shorter "moments" so the planner has fine-grained units.
Results are cached per file, so re-analysing the same footage is instant.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from .ffmpeg_tools import MediaError, check_cancel, iter_frames, probe
from .paths import cache_dir, file_key

log = logging.getLogger(__name__)

ANALYSIS_FPS = 6.0
THUMB_W, THUMB_H = 256, 144
STRIP_W, STRIP_H = 160, 90
ANALYSIS_VERSION = 12
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".wmv", ".flv", ".ts"}


@dataclass
class Shot:
    id: int                 # global id across all sources (assigned by Footage)
    source: int             # index into Footage.sources
    start: float            # seconds in the source file
    end: float
    motion: float           # 0..1, mean frame-to-frame change (normalised per library)
    motion_raw: float
    peak: float             # time of strongest motion inside the shot (source seconds)
    brightness: float       # 0..1
    saturation: float       # 0..1
    color: tuple[int, int, int]
    thumb: str              # path to a JPEG thumbnail
    usable: bool = True     # False for black or blank frames
    static: bool = False    # barely moves (stills, menus, talking heads)
    crop: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)  # picture inside baked-in bars
    take: int = 0           # pieces of one continuous take share this (per source)
    group: int = -1         # "same scene" group across the library: same take or a near-duplicate
    sig: list[int] = field(default_factory=list, repr=False)  # tiny colour fingerprint
    strip: str = ""         # JPEG of 3 frames (start, peak action, end) for the AI Director
    impacts: list = field(default_factory=list)   # [(source s, strength 0..1)]: punches, hits, flashes
    centers: list = field(default_factory=list)   # [(source s, x 0..1, y 0..1)]: where the action is
    camera: str = "still"   # still | pan | zoom | shaky | moving
    text: float = 0.0       # 0..1: captions, logos, title cards on screen

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Source:
    path: str
    duration: float
    width: int
    height: int
    fps: float
    proxy: str = ""          # small copy for fast previews (empty = use the original)


@dataclass
class Footage:
    sources: list[Source] = field(default_factory=list)
    shots: list[Shot] = field(default_factory=list)

    def shot(self, shot_id: int) -> Shot:
        return self.shots[shot_id]

    def usable_shots(self) -> list[Shot]:
        good = [s for s in self.shots if s.usable]
        return good or list(self.shots)

    def summary(self) -> str:
        total = sum(s.duration for s in self.sources)
        return f"{len(self.sources)} video(s), {total / 60:.1f} min, {len(self.shots)} shots"


def find_videos(paths: list[str | Path]) -> list[Path]:
    """Expand folders and keep files with a video extension."""
    found: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            found += sorted(q for q in p.iterdir() if q.is_file() and q.suffix.lower() in VIDEO_EXTENSIONS)
        elif p.is_file():
            found.append(p)
    seen, unique = set(), []
    for p in found:
        key = str(p.resolve()).lower()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def analyze_footage(paths: list[str | Path], cancel=None, progress=None) -> Footage:
    """Analyse every video (using the cache when possible) into one shot library.

    `progress(fraction, message)` is called as files are processed.
    """
    files = find_videos(paths)
    if not files:
        raise MediaError("No video files were added.")

    footage = Footage()
    per_file: list[tuple[Source, list[dict]]] = []
    done = [0.0] * len(files)

    def report(i, f):
        done[i] = f
        if progress:
            progress(sum(done) / len(files), f"Watching your footage ({int(sum(done))}/{len(files)} videos)")

    def work(i, path):
        check_cancel(cancel)
        try:
            result = _analyze_one(path, cancel, lambda f: report(i, f))
        except MediaError as exc:
            log.warning("Skipping %s: %s", path.name, exc)
            result = None
        report(i, 1.0)
        return result

    # Two videos at a time: decoding is the slow part and uses separate processes.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(work, range(len(files)), files))
    for path, result in zip(files, results):
        if result is None:
            continue
        source, shots = result
        per_file.append((source, shots))
        log.info("  %s: %d shots (%.0fs)", path.name, len(shots), source.duration)

    if not per_file:
        raise MediaError("None of the added videos could be read.")

    # Normalise motion across the whole library so "high motion" is relative
    # to what the user gave us (a calm anime and a shooter both get a range).
    raw = np.array([s["motion_raw"] for _, shots in per_file for s in shots] or [0.0])
    lo, hi = np.percentile(raw, 5), np.percentile(raw, 95)
    span = max(hi - lo, 1e-6)
    for src_index, (source, shots) in enumerate(per_file):
        footage.sources.append(source)
        for s in shots:
            shot = Shot(
                id=len(footage.shots), source=src_index,
                take=src_index * 1_000_000 + int(s.get("take", 0)),
                motion=float(np.clip((s["motion_raw"] - lo) / span, 0, 1)),
                **{k: v for k, v in s.items() if k not in ("source", "take", "overlay")},
            )
            shot._overlay = float(s.get("overlay", 0.0))
            footage.shots.append(shot)
    for src_index in range(len(footage.sources)):
        own = [s for s in footage.shots if s.source == src_index]
        overlays = [getattr(s, "_overlay", 0.0) for s in own]
        base = float(np.median(overlays)) if overlays else 0.0
        for s in own:
            s.text = round(max(s.text, float(np.clip((getattr(s, "_overlay", 0.0) - base - 0.004) * 60, 0, 1))), 2)
    _group_scenes(footage)
    if progress:
        progress(1.0, footage.summary())
    return footage


def _group_scenes(footage: Footage, threshold: float = 14.0) -> None:
    """Give every shot a scene group: pieces of the same take, plus near-identical
    moments anywhere in the library (compilations often show a clip twice)."""
    shots = footage.shots
    parent = list(range(len(shots)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    first_of_take: dict[int, int] = {}
    for s in shots:
        if s.take in first_of_take:
            union(first_of_take[s.take], s.id)
        else:
            first_of_take[s.take] = s.id

    with_sig = [s for s in shots if len(s.sig) == 16 * 9 * 3 and s.usable]
    if len(with_sig) > 1:
        ids = np.array([s.id for s in with_sig])
        sigs = np.array([s.sig for s in with_sig], np.float32)
        for i in range(0, len(sigs), 256):
            block = np.abs(sigs[i:i + 256, None, :] - sigs[None, :, :]).mean(axis=2)
            for a, b in zip(*np.nonzero(block < threshold)):
                ia, ib = i + int(a), int(b)
                if ia < ib:
                    union(int(ids[ia]), int(ids[ib]))

    roots: dict[int, int] = {}
    for s in shots:
        s.group = roots.setdefault(find(s.id), len(roots))


def scene_count(footage: Footage) -> int:
    return len({s.group for s in footage.usable_shots()})


def _analyze_one(path: Path, cancel, progress) -> tuple[Source, list[dict]]:
    info = probe(path)
    if not info.has_video:
        raise MediaError("no video stream")
    key = file_key(path, ANALYSIS_VERSION)
    out_dir = cache_dir() / "footage" / key
    meta_file = out_dir / "shots.json"
    source = Source(str(path), info.duration, info.width, info.height, info.fps)
    proxy = out_dir / "proxy.mp4"
    if meta_file.is_file():
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            if all(Path(s["thumb"]).is_file() for s in data["shots"]):
                source.proxy = str(proxy) if proxy.is_file() and proxy.stat().st_size > 1000 else ""
                return source, data["shots"]
        except (OSError, ValueError, KeyError):
            pass

    out_dir.mkdir(parents=True, exist_ok=True)
    max_frames = int(5.0 * ANALYSIS_FPS)  # long takes are split into <=5 s moments
    step = 1.0 / ANALYSIS_FPS
    shots: list[dict] = []
    seg: list[dict] = []          # per-frame stats of the moment being built
    recent_diffs: list[float] = []
    prev_gray = prev_hist = None
    expected = max(1, int(info.duration * ANALYSIS_FPS))
    count = 0
    take = 0

    def close_segment():
        if len(seg) < 2:
            seg.clear()
            return
        m = np.array([f["motion"] for f in seg[1:]])
        mid = seg[len(seg) // 2]
        thumb = out_dir / f"shot_{len(shots):05d}.jpg"
        mid_img = Image.fromarray(mid["frame"])
        mid_img.save(thumb, quality=82)
        sig = np.asarray(mid_img.resize((16, 9), Image.BILINEAR), np.uint8).reshape(-1)
        peak_i = 1 + int(np.argmax(m))
        picks = [int(len(seg) * 0.12), peak_i, min(len(seg) - 1, int(len(seg) * 0.88))]
        strip_img = Image.new("RGB", (3 * STRIP_W, STRIP_H))
        for k, i in enumerate(picks):
            strip_img.paste(Image.fromarray(seg[i]["frame"]).resize((STRIP_W, STRIP_H), Image.BILINEAR), (k * STRIP_W, 0))
        strip = out_dir / f"strip_{len(shots):05d}.jpg"
        strip_img.save(strip, quality=78)
        br = np.array([f["bright"] for f in seg])
        extra = _shot_details(seg, m, step)
        # Baked-in bars: edge columns/rows whose median brightness never rises above near-black.
        lum = np.stack([f["frame"].mean(axis=2) for f in seg])
        col_max = np.median(lum, axis=1).max(axis=0)
        row_max = np.median(lum, axis=2).max(axis=0)
        shots.append({
            "start": round(seg[0]["t"], 3),
            "end": round(min(seg[-1]["t"] + step, info.duration), 3),
            "motion_raw": float(m.mean()),
            "peak": round(seg[1 + int(np.argmax(m))]["t"], 3),
            "brightness": float(br.mean()),
            "saturation": float(np.mean([f["sat"] for f in seg])),
            "color": tuple(int(v) for v in np.mean([f["color"] for f in seg], axis=0)),
            "thumb": str(thumb),
            "usable": bool(br.mean() > 0.06),
            "static": bool(m.mean() < 0.002 and br.std() < 0.01),
            "crop": _bars(col_max, row_max),
            "take": take,
            "strip": str(strip),
            "sig": [int(v) for v in sig],
            **extra,
        })
        seg.clear()

    for t, frame in iter_frames(path, ANALYSIS_FPS, THUMB_W, THUMB_H, cancel, fast=True, proxy=proxy):
        small = frame[::4, ::4].astype(np.float32)
        gray = small.mean(axis=2)

        mx, mn = small.max(axis=2), small.min(axis=2)
        q = (frame[::2, ::2] // 64).reshape(-1, 3).astype(np.int32)
        hist = np.bincount(q[:, 0] * 16 + q[:, 1] * 4 + q[:, 2], minlength=64).astype(np.float32)
        hist /= max(hist.sum(), 1.0)

        is_cut = False
        motion = 0.0
        if prev_gray is not None:
            motion = float(np.abs(gray - prev_gray).mean() / (0.25 * 255.0 + 0.75 * gray.mean()))
            diff = float(np.abs(hist - prev_hist).sum())  # 0..2
            local = float(np.median(recent_diffs)) if recent_diffs else 0.0
            # A cut is a histogram jump that stands out from recent frames.
            # Compilations use fast transitions, so be sensitive; a hard cut also
            # usually changes the brightness layout, which motion alone rarely does.
            is_cut = len(seg) >= 2 and (
                (diff > 0.45 and diff > 3.0 * local + 0.1)
                or (diff > 0.32 and diff > 2.2 * local + 0.08 and motion > 0.12))
            recent_diffs = (recent_diffs + [diff])[-8:]
        if is_cut or len(seg) >= max_frames:
            close_segment()
            if is_cut:
                take += 1
            if is_cut:
                motion = 0.0  # the jump across a cut is not motion
        seg.append({
            "t": t, "frame": frame, "motion": motion, "g": gray, "g2": frame[::2, ::2].astype(np.float32).mean(axis=2),
            "bright": float(gray.mean() / 255.0),
            "sat": float(((mx - mn) / (mx + 1e-3)).mean()),
            "color": small.reshape(-1, 3).mean(axis=0),
        })
        if len(seg) == 2 and seg[1]["motion"] and not seg[0]["motion"]:
            seg[0]["motion"] = seg[1]["motion"]
        prev_gray, prev_hist = gray, hist
        count += 1
        if progress and count % 40 == 0:
            progress(min(0.99, count / expected))
    close_segment()

    if not shots:
        raise MediaError("too short or unreadable")
    source.proxy = str(proxy) if proxy.is_file() and proxy.stat().st_size > 1000 else ""
    meta_file.write_text(json.dumps({"source": asdict(source), "shots": shots}), encoding="utf-8")
    return source, shots


def _bars(col_max: np.ndarray, row_max: np.ndarray) -> tuple[float, float, float, float]:
    """Find letterbox/pillarbox bars: edges that never get brighter than near-black."""
    def span(profile):
        n = len(profile)
        lit = np.flatnonzero(profile > 20)
        if len(lit) == 0:
            return 0, n
        a, b = int(lit[0]), int(lit[-1]) + 1
        left, right = a, n - b
        # Real bars are symmetric, more than 3% per side, and leave at least half the frame.
        # Anything else (a dark scene, a vignette, a logo in one corner) is left alone.
        if min(left, right) < n * 0.03 or abs(left - right) > n * 0.03 or b - a < n * 0.5:
            return 0, n
        return a + 1, b - 1  # one extra pixel hides soft bar edges
    x0, x1 = span(col_max)
    y0, y1 = span(row_max)
    W, H = len(col_max), len(row_max)
    return (round(x0 / W, 4), round(y0 / H, 4), round((x1 - x0) / W, 4), round((y1 - y0) / H, 4))



def _shift(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """Global shift between two small grey frames by phase correlation (pixels: dy, dx)."""
    fa, fb = np.fft.fft2(a - a.mean()), np.fft.fft2(b - b.mean())
    r = fa * np.conj(fb)
    r /= np.abs(r) + 1e-9
    corr = np.abs(np.fft.ifft2(r))
    y, x = np.unravel_index(int(np.argmax(corr)), corr.shape)
    h, w = a.shape

    def sub(c_m, c_0, c_p):  # parabola through the peak and its neighbours: sub-pixel offset
        den = c_m - 2 * c_0 + c_p
        return 0.0 if abs(den) < 1e-12 else float(np.clip(0.5 * (c_m - c_p) / den, -0.5, 0.5))

    fy = y + sub(corr[(y - 1) % h, x], corr[y, x], corr[(y + 1) % h, x])
    fx = x + sub(corr[y, (x - 1) % w], corr[y, x], corr[y, (x + 1) % w])
    return float(fy if fy <= h / 2 else fy - h), float(fx if fx <= w / 2 else fx - w)


def _zoomed(a: np.ndarray, factor: float) -> np.ndarray:
    """Centre crop by `factor` scaled back to size (a cheap 'zoomed in' version)."""
    h, w = a.shape
    ch, cw = int(h / factor), int(w / factor)
    y0, x0 = (h - ch) // 2, (w - cw) // 2
    crop = a[y0:y0 + ch, x0:x0 + cw]
    yi = (np.arange(h) * ch / h).astype(int)
    xi = (np.arange(w) * cw / w).astype(int)
    return crop[yi][:, xi]


def _shot_details(seg: list[dict], m: np.ndarray, step: float) -> dict:
    """Impacts, where the action is, how the camera moves, and on-screen text for one shot."""
    times = [f["t"] for f in seg]
    grays = [f["g"] for f in seg]
    h, w = grays[0].shape

    # Impacts: motion spikes and sudden flashes (anime impact frames, explosions, hits).
    bright = np.array([f["bright"] for f in seg])
    score = np.zeros(len(seg))
    score[1:] = m
    flash = np.maximum(0.0, np.diff(bright, prepend=bright[:1]))
    score = score + 2.0 * flash
    base = float(np.median(score[1:])) if len(score) > 1 else 0.0
    impacts = []
    for i in range(1, len(seg)):
        left = score[i - 1]
        right = score[i + 1] if i + 1 < len(seg) else -1
        if score[i] >= left and score[i] > right and score[i] >= max(0.02, 1.4 * base):
            impacts.append((round(times[i], 3), float(score[i])))
    if impacts:
        top = max(v for _, v in impacts)
        impacts = sorted(impacts, key=lambda x: -x[1])[:6]
        impacts = sorted((t, round(v / top, 2)) for t, v in impacts)

    # Where the action is: centroid of change between frames (or of contrast when still).
    ys, xs = np.mgrid[0:h, 0:w]
    centers = []
    for i, g in enumerate(grays):
        d = np.abs(g - grays[i - 1]) if i else np.abs(g - g.mean())
        wgt = d ** 2
        tot = float(wgt.sum())
        if tot < 1e-6 or (i and float(d.mean()) < 1.5):
            wgt = np.abs(g - g.mean()) ** 2
            tot = float(wgt.sum()) or 1.0
        centers.append([float((xs * wgt).sum() / tot / w), float((ys * wgt).sum() / tot / h)])
    cx = np.convolve([c[0] for c in centers], np.ones(3) / 3, mode="same")
    cy = np.convolve([c[1] for c in centers], np.ones(3) / 3, mode="same")
    if len(centers) >= 3:
        cx[0], cx[-1], cy[0], cy[-1] = centers[0][0], centers[-1][0], centers[0][1], centers[-1][1]
    centers = [(round(times[i], 3), round(float(cx[i]), 3), round(float(cy[i]), 3)) for i in range(len(seg))]

    # Camera: how each quarter of the picture moves. All quarters the same way = pan;
    # quarters moving outward (or inward) from the centre = zoom; back and forth = shaky.
    camera = "still"
    g2 = [f["g2"] for f in seg]
    if float(m.mean()) > 0.004 and len(g2) >= 3:
        H2, W2 = g2[0].shape
        hh, hw = H2 // 2, W2 // 2
        quads = [(slice(0, hh), slice(0, hw)), (slice(0, hh), slice(hw, W2)),
                 (slice(hh, H2), slice(0, hw)), (slice(hh, H2), slice(hw, W2))]
        gx, gy, div = [], [], []
        for i in range(1, len(g2)):
            q = [_shift(g2[i][a, b], g2[i - 1][a, b]) for a, b in quads]  # (dy, dx) per quarter: TL TR BL BR
            dx = [v[1] for v in q]
            dy = [v[0] for v in q]
            gx.append(float(np.median(dx)))
            gy.append(float(np.median(dy)))
            div.append(((dx[1] + dx[3]) - (dx[0] + dx[2]) + (dy[2] + dy[3]) - (dy[0] + dy[1])) / 4.0)
        gx, gy, div = np.array(gx), np.array(gy), np.array(div)
        mag = np.hypot(gx, gy)
        zoom_mag = float(np.median(np.abs(div)))
        zoom_sign = np.sign(div[np.abs(div) >= 0.15])
        moving = mag >= 0.8
        sx, sy = np.sign(gx[np.abs(gx) >= 0.8]), np.sign(gy[np.abs(gy) >= 0.8])
        consistent = (len(sx) >= 2 and abs(sx.mean()) >= 0.7) or (len(sy) >= 2 and abs(sy.mean()) >= 0.7)
        flips = int(np.sum(np.diff(np.sign(gx[moving])) != 0) + np.sum(np.diff(np.sign(gy[moving])) != 0)) \
            if moving.sum() > 2 else 0
        # A zoom: the picture spreads out (or pulls in) step after step, more than it slides.
        if (zoom_mag >= 0.25 and len(zoom_sign) >= max(2, 0.7 * len(div))
                and abs(zoom_sign.mean()) >= 0.8 and zoom_mag >= 1.5 * float(np.median(mag))):
            camera = "zoom"
        elif moving.mean() >= 0.5 and consistent:
            camera = "pan"
        elif moving.mean() >= 0.5 and flips >= max(2, int(moving.sum())):
            camera = "shaky"
        else:
            camera = "moving"

    # On-screen text: sharp edges that stay put while the picture moves (captions, logos),
    # or a flat background with sharp lettering and no motion (title cards).
    full = [f["frame"].mean(axis=2) for f in seg[:: max(1, len(seg) // 8)]]
    edges = []
    for g in full:
        gx = np.abs(np.diff(g, axis=1))[:-1, :]
        gy = np.abs(np.diff(g, axis=0))[:, :-1]
        edges.append(np.maximum(gx, gy))
    strong = [e > 60 for e in edges]
    overlay = 0.0
    card = 0.0
    if len(strong) >= 3 and float(m.mean()) > 0.01:
        persistent = np.mean(strong, axis=0) >= 0.85
        overlay = float(persistent.mean())
    if float(m.mean()) < 0.006:
        e = edges[len(edges) // 2]
        flat = float((e < 8).mean())
        lettering = float((e > 60).mean())
        if flat >= 0.75 and 0.01 <= lettering <= 0.2:
            card = 0.9
    return {"impacts": impacts, "centers": centers, "camera": camera,
            "overlay": round(overlay, 4), "text": round(card, 2)}
