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

RENDER_VERSION = 9
SEGMENT_CACHE_LIMIT = 3 * 1024 ** 3  # bytes


@dataclass
class Quality:
    name: str
    scale: float         # output size relative to the plan
    preset: str
    crf: int
    audio_bitrate: str
    video_bitrate: str | None = None   # set for size-limited exports (e.g. "2400k")


PREVIEW = Quality("preview", 1 / 3, "ultrafast", 30, "160k")
EXPORT = Quality("export", 1.0, "faster", 18, "256k")  # ~2x "medium" speed, same look (SSIM 0.998)
EXPORT_BEST = Quality("best", 1.0, "slow", 16, "320k")


_ENCODER: str | None = None
_GPU_FAILED = False


def pick_encoder() -> str:
    """The fastest H.264 encoder that works on this machine (graphics card first)."""
    global _ENCODER
    if _ENCODER is not None:
        return _ENCODER
    import subprocess
    import sys

    from .ffmpeg_tools import _popen_kwargs

    if os.environ.get("MONTAGE_NO_GPU") or _GPU_FAILED:
        _ENCODER = "libx264"
        return _ENCODER
    if sys.platform == "darwin":
        candidates = ["h264_videotoolbox"]
    elif sys.platform == "win32":
        candidates = ["h264_nvenc", "h264_qsv", "h264_amf"]
    else:
        candidates = ["h264_nvenc", "h264_qsv"]
    _ENCODER = "libx264"
    for enc in candidates:
        try:
            r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                "color=c=black:s=320x240:r=30:d=0.3", "-pix_fmt", "yuv420p", "-c:v", enc,
                                "-frames:v", "8", "-f", "null", "-"], capture_output=True, timeout=20,
                               **_popen_kwargs())
        except Exception:
            continue
        if r.returncode == 0:
            _ENCODER = enc
            break
    log.info("Video encoder: %s", _ENCODER)
    return _ENCODER


def encoder_args(enc: str, quality: "Quality", fps: int) -> list[str]:
    g = ["-g", str(fps * 2), "-pix_fmt", "yuv420p"]
    if quality.video_bitrate:  # size-limited: steady bitrate instead of constant quality
        kbps = int(quality.video_bitrate.rstrip("k"))
        rate = ["-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.15)}k", "-bufsize", f"{kbps * 2}k"]
        if enc == "libx264":
            return ["-c:v", "libx264", "-preset", quality.preset, *rate, "-threads", "2"] + g
        return ["-c:v", enc, *rate] + g
    if enc == "h264_nvenc":
        return ["-c:v", enc, "-preset", "p2" if quality.name == "preview" else "p5", "-rc", "vbr",
                "-cq", str(quality.crf + 1), "-b:v", "0"] + g
    if enc == "h264_qsv":
        return ["-c:v", enc, "-preset", "veryfast" if quality.name == "preview" else "medium",
                "-global_quality", str(quality.crf + 2)] + g
    if enc == "h264_amf":
        return ["-c:v", enc, "-quality", "speed" if quality.name == "preview" else "balanced", "-rc", "cqp",
                "-qp_i", str(quality.crf), "-qp_p", str(quality.crf + 2)] + g
    if enc == "h264_videotoolbox":
        return ["-c:v", enc, "-q:v", str(int(max(20, min(90, 100 - quality.crf * 2.2))))] + g
    return ["-c:v", "libx264", "-preset", quality.preset, "-crf", str(quality.crf), "-threads", "2"] + g


class _UseCPU(Exception):
    """The graphics card encoder failed: redo the render on the processor."""


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def _between(windows: list[tuple[int, int]], var: str = "n") -> str:
    return "+".join(f"between({var},{a},{b})" for a, b in windows) or "0"


def segment_filter(cut: Cut, look: Look, fit: str, W: int, H: int, fps: int, idx: int = 0) -> str:
    """Build the filtergraph for one cut. Input is [idx:v], output is [v{idx}] with exactly
    cut.frames frames (so several cuts can be joined inside one ffmpeg run)."""
    return _label(_segment_graph(cut, look, fit, W, H, fps), idx, cut.frames)


def _label(graph: str, idx: int, frames: int) -> str:
    graph = graph.replace("[0:v]", f"[{idx}:v]")
    for name in ("f", "s1", "s2", "bg", "fg", "g1", "g2", "g3"):
        graph = graph.replace(f"[{name}]", f"[{name}_{idx}]")
    assert graph.endswith("[v]")
    # square pixels + exact frame count, so cuts can be joined inside one run
    return graph[:-3] + f",setsar=1,trim=end_frame={frames},setpts=PTS-STARTPTS[v{idx}]"


def _segment_graph(cut: Cut, look: Look, fit: str, W: int, H: int, fps: int) -> str:
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
        fx, fy = cut.focus
        # crop to fill, centred on the action (matters when the shapes differ, e.g. vertical edits)
        # (crop first, then scale: a vertical edit from 1080p footage would otherwise scale every
        # frame up to 3413x1920 only to throw two thirds of it away)
        ar = W / H
        graph.append(f"[0:v]{base},crop='trunc(min(iw,ih*{ar:.6f})/2)*2':'trunc(min(ih,iw/{ar:.6f})/2)*2':"
                     f"'min(max(iw*{fx:.3f}-ow/2,0),iw-ow)':'min(max(ih*{fy:.3f}-oh/2,0),ih-oh)',"
                     f"scale={W}:{H}:flags=bicubic,setsar=1[f]")

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
        # blur a quarter-size copy and scale it back: looks the same, a fraction of the work
        graph.append(f"[g2]scale={_even(W / 4)}:{_even(H / 4)}:flags=fast_bilinear,gblur=sigma={sigma / 4:.2f},"
                     f"scale={W}:{H}:flags=bilinear[g3]")
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

    def _render_batches(self, plan: EditPlan, quality: "Quality", W: int, H: int, seg_dir: Path,
                        cancel, progress, enc: str) -> list[Path]:
        graphs = [_segment_graph(c, plan.look, plan.fit, W, H, plan.fps) for c in plan.cuts]
        use_proxy = quality.name == "preview"
        keys = [_cut_key(c, g, quality, plan.fps, enc, plan.proxies.get(c.source) if use_proxy else None)
                for c, g in zip(plan.cuts, graphs)]
        runs = _batches(keys)
        files = []
        todo = []
        for a, b in runs:
            bkey = hashlib.sha1("".join(keys[a:b]).encode()).hexdigest()[:20]
            target = seg_dir / f"b_{bkey}.mp4"
            files.append(target)
            if not target.is_file():
                todo.append((a, b, target))
        cuts_total = len(plan.cuts)
        cuts_done = cuts_total - sum(b - a for a, b, _ in todo)
        if cuts_done:
            log.info("Reusing %d of %d rendered cuts", cuts_done, cuts_total)
        started = time.time()
        lock = threading.Lock()

        def encode(a: int, b: int, target: Path):
            cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y"]
            parts = []
            for i, cut in enumerate(plan.cuts[a:b]):
                need = cut.frames / plan.fps * cut.speed
                src = plan.proxies.get(cut.source, cut.source) if quality.name == "preview" else cut.source
                cmd += ["-ss", f"{max(0.0, cut.src_start):.3f}", "-t", f"{need + 0.6:.3f}", "-i", src]
                parts.append(_label(graphs[a + i], i, cut.frames))
            n = b - a
            graph = ";".join(parts) + ";" + "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[out]"
            frames = sum(c.frames for c in plan.cuts[a:b])
            tmp = target.with_name(target.stem + f".part{threading.get_ident()}.mp4")
            cmd += ["-filter_complex", graph, "-map", "[out]", "-an", "-frames:v", str(frames),
                    "-fps_mode", "cfr", "-r", str(plan.fps), *encoder_args(enc, quality, plan.fps),
                    "-video_track_timescale", str(plan.fps * 1000), str(tmp)]
            try:
                self.procs.run(cmd, cancel)
                os.replace(tmp, target)
            except MediaError:
                if enc != "libx264":
                    raise _UseCPU() from None
                raise
            finally:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)

        if progress:
            progress(cuts_done / max(cuts_total, 1) * 0.92, f"Rendering cuts ({cuts_done}/{cuts_total})")
        if todo:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = {pool.submit(encode, a, b, t): b - a for a, b, t in todo}
                try:
                    for fut in as_completed(futures):
                        fut.result()
                        with lock:
                            cuts_done += futures[fut]
                        if progress:
                            progress(cuts_done / cuts_total * 0.92, f"Rendering cuts ({cuts_done}/{cuts_total})")
                except BaseException:
                    for f in futures:
                        f.cancel()
                    self.procs.kill_all()
                    raise
        log.info("Cuts rendered in %.1fs with %s (%d runs)", time.time() - started, enc, len(todo))
        return files

    def render(self, plan: EditPlan, out_path: str | Path, quality: Quality = PREVIEW,
               cancel: threading.Event | None = None, progress=None) -> Path:
        plan.validate()
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        W, H = _even(plan.width * quality.scale), _even(plan.height * quality.scale)
        seg_dir = cache_dir() / "segments"
        seg_dir.mkdir(parents=True, exist_ok=True)
        _trim_cache(seg_dir)

        try:
            files = self._render_batches(plan, quality, W, H, seg_dir, cancel, progress, pick_encoder())
        except _UseCPU:
            global _ENCODER, _GPU_FAILED
            log.warning("Graphics card encoding failed; using the processor instead.")
            _GPU_FAILED, _ENCODER = True, None
            files = self._render_batches(plan, quality, W, H, seg_dir, cancel, progress, pick_encoder())
        check_cancel(cancel)

        # Join + music.
        if progress:
            progress(0.94, "Adding the music")
        work = Path(tempfile.mkdtemp(prefix="montage_", dir=cache_dir()))
        try:
            listing = work / "list.txt"
            listing.write_text("".join(
                "file '" + str(t).replace("\\", "/").replace("'", "'\\''") + "'\n" for t in files),
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


BATCH_MAX = 10


def _cut_key(cut: Cut, graph: str, quality: "Quality", fps: int, enc: str, proxy: str | None = None) -> str:
    src = Path(proxy or cut.source)
    st = src.stat()
    return hashlib.sha1(json.dumps({
        "v": RENDER_VERSION, "src": str(src), "size": st.st_size, "mtime": int(st.st_mtime),
        "ss": cut.src_start, "n": cut.frames, "speed": cut.speed, "freeze": cut.freeze,
        "graph": graph, "q": asdict(quality), "fps": fps, "enc": enc,
    }, sort_keys=True).encode()).hexdigest()


def _batches(keys: list[str]) -> list[tuple[int, int]]:
    """Split cuts into runs of up to BATCH_MAX. Boundaries depend on each cut's own content,
    so after a refine only the runs containing changed cuts are rendered again."""
    out, start = [], 0
    for i in range(1, len(keys) + 1):
        if i == len(keys) or i - start >= BATCH_MAX or (i - start >= 3 and int(keys[i][:4], 16) % 5 == 0):
            out.append((start, i))
            start = i
    return out


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
