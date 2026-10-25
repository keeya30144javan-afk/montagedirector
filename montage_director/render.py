"""Render an EditPlan with ffmpeg.

Each cut becomes its own short H.264 segment with its effects baked in.
Segments run in parallel and are cached by content hash, so a refined edit
only re-encodes the cuts that changed. Segments are then joined without
re-encoding and the song is laid on top.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

from .ffmpeg_tools import Cancelled, MediaError, ProcessGroup, check_cancel, ffmpeg_exe
from .paths import cache_dir
from .plan import Cut, EditPlan, Effect, Look

log = logging.getLogger(__name__)

RENDER_VERSION = 5
SEGMENT_CACHE_LIMIT = 3 * 1024 ** 3  # bytes


@dataclass
class Quality:
    name: str
    scale: float         # output size relative to the plan
    preset: str
    crf: int
    audio_bitrate: str


PREVIEW = Quality("preview", 1 / 3, "ultrafast", 30, "160k")
EXPORT = Quality("export", 1.0, "medium", 18, "256k")
EXPORT_BEST = Quality("best", 1.0, "slow", 16, "320k")


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def _between(windows: list[tuple[int, int]], var: str = "n") -> str:
    return "+".join(f"between({var},{a},{b})" for a, b in windows) or "0"


def segment_filter(cut: Cut, look: Look, fit: str, W: int, H: int, fps: int) -> str:
    """Build the filtergraph for one cut. Input is [0:v], output is [v]."""
    n = cut.frames
    need = n / fps * cut.speed
    pre: list[str] = []
    if cut.freeze:
        pre += ["trim=end_frame=1", "setpts=PTS-STARTPTS", f"fps={fps}",
                f"tpad=stop_mode=clone:stop={n + 2}"]
    else:
        if cut.reverse:
            pre += [f"trim=duration={need + 1 / fps:.4f}", "setpts=PTS-STARTPTS", "reverse"]
        pre += [f"setpts=(PTS-STARTPTS)/{cut.speed:.4f}", f"fps={fps}",
                "tpad=stop_mode=clone:stop_duration=2"]

    x, y, cw, ch = cut.crop
    if (cw, ch) != (1.0, 1.0):
        pre.append(f"crop=trunc(iw*{cw}/2)*2:trunc(ih*{ch}/2)*2:trunc(iw*{x}):trunc(ih*{y})")
    graph: list[str] = []
    base = ",".join(pre)
    if fit == "fit":
        graph.append(f"[0:v]{base},scale={W}:{H}:force_original_aspect_ratio=decrease:flags=bicubic,"
                     f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[f]")
    elif fit == "blur":
        graph.append(f"[0:v]{base},split[s1][s2]")
        graph.append(f"[s1]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                     f"gblur=sigma=28,eq=brightness=-0.08[bg]")
        graph.append(f"[s2]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=bicubic[fg]")
        graph.append("[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[f]")
    else:
        graph.append(f"[0:v]{base},scale={W}:{H}:force_original_aspect_ratio=increase:flags=bicubic,"
                     f"crop={W}:{H},setsar=1[f]")

    chain: list[str] = []
    by_kind: dict[str, list[Effect]] = {}
    for e in cut.effects:
        by_kind.setdefault(e.kind, []).append(e)

    # Zoom punch: jump in, ease back out (zoompan keeps one output frame per input frame).
    if "zoom" in by_kind:
        terms = []
        for e in by_kind["zoom"]:
            a, L = e.frame, max(2, e.frames)
            amt = 0.10 + 0.22 * e.intensity
            terms.append(f"between(on,{a},{a + L - 1})*{amt:.3f}*pow(1-(on-{a})/{L},2)")
        z = "1+" + "+".join(terms)
        chain.append(f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={fps}")

    # Camera shake: a slightly zoomed crop that wanders, then scaled back.
    if "shake" in by_kind:
        m = 0.045
        env = "+".join(f"between(n,{e.frame},{e.frame + e.frames - 1})*{e.intensity:.2f}"
                       f"*(1-0.6*(n-{e.frame})/{max(1, e.frames)})" for e in by_kind["shake"])
        chain.append(f"crop=w=iw*{1 - 2 * m}:h=ih*{1 - 2 * m}:"
                     f"x='iw*{m}*(1+({env})*sin(n*2.7+0.5))':y='ih*{m}*(1+({env})*cos(n*2.1))'")
        chain.append(f"scale={W}:{H}:flags=bicubic")

    for e in by_kind.get("rgb", []):
        px = max(2, int(W * 0.012 * e.intensity))
        chain.append(f"rgbashift=rh=-{px}:bh={px}:bv={px // 2}:edge=smear:"
                     f"enable='between(n,{e.frame},{e.frame + e.frames - 1})'")
    for e in by_kind.get("invert", []):
        chain.append(f"negate=enable='between(n,{e.frame},{e.frame + e.frames - 1})'")
    for e in by_kind.get("blur_in", []):
        half = max(1, e.frames // 2)
        chain.append(f"gblur=sigma={6 + 14 * e.intensity:.1f}:enable='between(n,{e.frame},{e.frame + half - 1})'")
        chain.append(f"gblur=sigma={2 + 4 * e.intensity:.1f}:enable='between(n,{e.frame + half},{e.frame + e.frames - 1})'")

    # Look (identical on every cut so the edit reads as one piece).
    grade = {
        "y2k": "eq=contrast=1.12:saturation=1.35:gamma=1.02,colorbalance=rs=0.03:bs=0.05:rh=0.04:bh=-0.03",
        "sad": "eq=saturation=0.55:contrast=0.95:brightness=-0.02,colorbalance=bs=0.08:bm=0.05:rh=-0.03,vignette=PI/5",
        "cinematic": "eq=contrast=1.1:saturation=1.08,colorbalance=rs=-0.08:bs=0.1:rh=0.08:bh=-0.08,vignette=PI/6",
        "vhs": "eq=saturation=1.25:contrast=1.05,chromashift=cbh=3:crh=-3,gblur=sigma=0.6",
        "mono": "hue=s=0,eq=contrast=1.15",
        "dream": "eq=saturation=1.15:brightness=0.03,colorbalance=rh=0.05:bh=0.06",
    }.get(look.grade)
    if grade:
        chain.append(grade)
    if look.trails > 0:
        chain.append(f"lagfun=decay={0.82 + 0.14 * min(1.0, look.trails):.3f}")
    grain = max(look.grain, 0.35 if look.grade == "vhs" else 0.0)
    if grain > 0:
        chain.append(f"noise=alls={int(6 + 20 * grain)}:allf=t")

    # Flashes and fades drawn on top of everything.
    for e in by_kind.get("flash", []) + by_kind.get("fade_white", []):
        L = max(1, e.frames)
        curve = 1.0 if e.kind == "flash" else 1.8
        for k in range(L):
            alpha = e.intensity * (1 - k / L) ** curve
            if alpha < 0.04:
                continue
            chain.append(f"drawbox=x=0:y=0:w=iw:h=ih:color={e.color}@{alpha:.2f}:t=fill:"
                         f"enable='eq(n,{e.frame + k})'")
    for e in by_kind.get("fade_black", []):
        chain.append(f"fade=t=out:s={e.frame}:n={max(1, e.frames)}:color=black")
    if look.letterbox:
        bar = _even(H * (0.11 if W > H else 0.07))
        chain.append(f"drawbox=x=0:y=0:w=iw:h={bar}:color=black:t=fill")
        chain.append(f"drawbox=x=0:y=ih-{bar}:w=iw:h={bar}:color=black:t=fill")

    tail = "[f]" + (",".join(chain) if chain else "null")
    if look.glow > 0:
        sigma = 6 + 14 * min(1.0, look.glow)
        opacity = 0.18 + 0.4 * min(1.0, look.glow)
        graph.append(f"{tail},format=gbrp,split[g1][g2]")
        graph.append(f"[g2]gblur=sigma={sigma:.1f}[g3]")
        graph.append(f"[g1][g3]blend=all_mode=screen:all_opacity={opacity:.2f},format=yuv420p[v]")
    else:
        graph.append(f"{tail},format=yuv420p[v]")
    return ";".join(graph)


class Renderer:
    def __init__(self, workers: int | None = None):
        self.procs = ProcessGroup()
        cpu = os.cpu_count() or 4
        self.workers = workers or max(2, min(6, cpu // 2))

    def cancel(self) -> None:
        self.procs.kill_all()

    def render(self, plan: EditPlan, out_path: str | Path, quality: Quality = PREVIEW,
               cancel: threading.Event | None = None, progress=None) -> Path:
        plan.validate()
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        W, H = _even(plan.width * quality.scale), _even(plan.height * quality.scale)
        seg_dir = cache_dir() / "segments"
        seg_dir.mkdir(parents=True, exist_ok=True)
        _trim_cache(seg_dir)

        jobs = []
        for cut in plan.cuts:
            graph = segment_filter(cut, plan.look, plan.fit, W, H, plan.fps)
            src = Path(cut.source)
            st = src.stat()
            key = hashlib.sha1(json.dumps({
                "v": RENDER_VERSION, "src": str(src), "size": st.st_size, "mtime": int(st.st_mtime),
                "ss": cut.src_start, "n": cut.frames, "speed": cut.speed, "freeze": cut.freeze,
                "graph": graph, "q": asdict(quality), "fps": plan.fps,
            }, sort_keys=True).encode()).hexdigest()[:20]
            jobs.append((cut, graph, seg_dir / f"{key}.mp4"))

        todo = [j for j in jobs if not j[2].is_file()]
        reused = len(jobs) - len(todo)
        if reused:
            log.info("Reusing %d of %d rendered cuts", reused, len(jobs))
        done = reused
        total = len(jobs)
        started = time.time()

        def encode(job):
            cut, graph, target = job
            need = cut.frames / plan.fps * cut.speed
            tmp = target.with_name(target.stem + f".part{threading.get_ident()}.mp4")
            cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                   "-ss", f"{max(0.0, cut.src_start):.3f}", "-t", f"{need + 0.6:.3f}",
                   "-i", cut.source, "-filter_complex", graph, "-map", "[v]", "-an",
                   "-frames:v", str(cut.frames), "-fps_mode", "cfr", "-r", str(plan.fps),
                   "-c:v", "libx264", "-preset", quality.preset, "-crf", str(quality.crf),
                   "-pix_fmt", "yuv420p", "-g", str(plan.fps * 2), "-threads", "2",
                   "-video_track_timescale", str(plan.fps * 1000), str(tmp)]
            try:
                self.procs.run(cmd, cancel)
                os.replace(tmp, target)
            finally:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)

        if progress:
            progress(done / max(total, 1) * 0.92, f"Rendering cuts ({done}/{total})")
        if todo:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = [pool.submit(encode, j) for j in todo]
                try:
                    for fut in as_completed(futures):
                        fut.result()
                        done += 1
                        if progress:
                            progress(done / total * 0.92, f"Rendering cuts ({done}/{total})")
                except BaseException:
                    for f in futures:
                        f.cancel()
                    self.procs.kill_all()
                    raise
        log.info("Cuts rendered in %.1fs", time.time() - started)
        check_cancel(cancel)

        # Join + music.
        if progress:
            progress(0.94, "Adding the music")
        work = Path(tempfile.mkdtemp(prefix="montage_", dir=cache_dir()))
        try:
            listing = work / "list.txt"
            listing.write_text("".join(
                "file '" + str(t).replace("\\", "/").replace("'", "'\\''") + "'\n" for _, _, t in jobs),
                encoding="utf-8")
            dur = plan.duration
            fade_out = min(1.5, dur / 4)
            afilters = [f"afade=t=out:st={dur - fade_out:.3f}:d={fade_out:.3f}"]
            if plan.song_start > 0.05:
                afilters.insert(0, "afade=t=in:st=0:d=0.15")
            tmp_out = out_path.with_name(out_path.stem + ".rendering" + out_path.suffix)
            cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "concat", "-safe", "0", "-i", str(listing),
                   "-ss", f"{plan.song_start:.3f}", "-t", f"{dur:.3f}", "-i", plan.song,
                   "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                   "-af", ",".join(afilters), "-c:a", "aac", "-b:a", quality.audio_bitrate,
                   "-t", f"{dur:.3f}", "-movflags", "+faststart", str(tmp_out)]
            try:
                self.procs.run(cmd, cancel)
                os.replace(tmp_out, out_path)
            finally:
                tmp_out.unlink(missing_ok=True)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if progress:
            progress(1.0, f"Saved {out_path.name}")
        return out_path


def _trim_cache(seg_dir: Path) -> None:
    """Keep the segment cache under SEGMENT_CACHE_LIMIT by deleting the oldest files."""
    try:
        files = [(p.stat().st_mtime, p.stat().st_size, p) for p in seg_dir.glob("*.mp4")]
    except OSError:
        return
    total = sum(s for _, s, _ in files)
    for _, size, p in sorted(files):
        if total <= SEGMENT_CACHE_LIMIT:
            break
        try:
            p.unlink()
            total -= size
        except OSError:
            pass
    for p in seg_dir.glob("*.part*.mp4"):  # leftovers from a crash
        if time.time() - p.stat().st_mtime > 3600:
            p.unlink(missing_ok=True)


__all__ = ["Renderer", "Quality", "PREVIEW", "EXPORT", "EXPORT_BEST", "segment_filter",
           "MediaError", "Cancelled"]
