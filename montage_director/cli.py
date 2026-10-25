"""Command-line interface: python -m montage_director make --footage ... --song ..."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .direction import STYLES
from .director_ai import MODELS, AIDirectorError
from .ffmpeg_tools import Cancelled, MediaError
from .project import ASPECTS, Project, Settings


def _time(value: str) -> float:
    if ":" in value:
        m, s = value.split(":", 1)
        return int(m) * 60 + float(s)
    return float(value)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="montage-director", description="Make a beat-synced music edit.")
    p.add_argument("--footage", nargs="+", required=True, help="video files and/or folders")
    p.add_argument("--song", required=True)
    p.add_argument("--prompt", default="", help="your idea for the edit")
    p.add_argument("--style", choices=list(STYLES), default="hype")
    p.add_argument("--ai", action="store_true", help="use the AI Director (needs ANTHROPIC_API_KEY)")
    p.add_argument("--model", choices=list(MODELS), default=None)
    p.add_argument("--aspect", choices=["auto", *ASPECTS], default="auto")
    p.add_argument("--fit", choices=["fill", "fit", "blur"], default="fill")
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--start", type=_time, default=0.0, help="song start, seconds or m:ss")
    p.add_argument("--end", type=_time, default=None, help="song end, seconds or m:ss")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--preview", action="store_true", help="fast low-resolution render")
    p.add_argument("--best", action="store_true", help="slowest, highest quality export")
    p.add_argument("--out", default=None, help="output .mp4 (default: Documents/Montage Director/Exports)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = Settings(style=a.style, prompt=a.prompt, use_ai=a.ai,
                        api_key=os.environ.get("ANTHROPIC_API_KEY", ""), aspect=a.aspect, fit=a.fit,
                        fps=a.fps, song_start=a.start, song_end=a.end, seed=a.seed)
    if a.model:
        settings.model = a.model
    proj = Project(footage_paths=a.footage, song_path=a.song, settings=settings)

    def show(frac, msg):
        print(f"\r[{frac * 100:5.1f}%] {msg:<60}", end="", flush=True)

    try:
        proj.analyse(progress=show)
        print()
        d = proj.direct()
        if d.summary:
            print("Director:", d.summary)
        for note in d.notes:
            print("  -", note)
        plan = proj.make_plan()
        print(f"{len(plan.cuts)} cuts, {plan.duration:.1f}s, {plan.width}x{plan.height} (seed {proj.settings.seed})")
        if a.preview:
            out = proj.render_preview(progress=show)
        else:
            out = proj.export(a.out or proj.default_export_path(), best=a.best, progress=show)
        print(f"\nSaved {out}")
        if proj.ai:
            print(f"AI cost: ${proj.ai.usage.cost:.3f}")
    except (MediaError, AIDirectorError, ValueError) as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    except (Cancelled, KeyboardInterrupt):
        print("\nStopped.", file=sys.stderr)
        return 130
    return 0
