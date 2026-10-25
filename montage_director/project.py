"""One edit project: inputs, settings and the analyse -> direct -> plan -> render pipeline."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

from .direction import DEFAULT_STYLE, Direction, asks_for_effects, read_prompt, style_direction
from .director_ai import DEFAULT_MODEL, AIDirector
from .footage import Footage, analyze_footage
from .music import SongMap, analyze_song
from .paths import workspace_dir
from .plan import EditPlan
from .planner import OutputSettings, make_plan
from .render import EXPORT, EXPORT_BEST, PREVIEW, Quality, Renderer

log = logging.getLogger(__name__)

ASPECTS = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080), "4:3": (1440, 1080)}


@dataclass
class Settings:
    style: str = DEFAULT_STYLE
    prompt: str = ""
    use_ai: bool = False
    api_key: str = ""
    model: str = DEFAULT_MODEL
    aspect: str = "auto"       # auto (16:9 unless the prompt asks otherwise) or a key of ASPECTS
    fit: str = "fill"
    fps: int = 30
    song_start: float = 0.0
    song_end: float | None = None
    seed: int | None = None


@dataclass
class Project:
    footage_paths: list[str] = field(default_factory=list)
    song_path: str = ""
    settings: Settings = field(default_factory=Settings)
    footage: Footage | None = None
    song: SongMap | None = None
    direction: Direction | None = None
    plan: EditPlan | None = None
    ai: AIDirector | None = None
    preview_path: Path | None = None
    manual_drops: list[float] | None = None   # drops the user marked on the timeline
    renderer: Renderer = field(default_factory=Renderer)
    _analysed_for: tuple = ()

    # ----- steps ------------------------------------------------------------- #
    def analyse(self, cancel=None, progress=None) -> None:
        key = (tuple(self.footage_paths), self.song_path)
        if self._analysed_for == key and self.footage and self.song:
            return
        if not self.footage_paths:
            raise ValueError("Add some footage first.")
        if not self.song_path:
            raise ValueError("Add a song first.")
        p = progress or (lambda f, m: None)
        self.song = analyze_song(self.song_path, cancel, lambda f, m: p(0.15 * f, m))
        self.footage = analyze_footage(self.footage_paths, cancel, lambda f, m: p(0.15 + 0.85 * f, m))
        self._analysed_for = key
        self.ai = None  # new material: start a fresh AI conversation
        self.manual_drops = None

    def song_range(self) -> tuple[float, float]:
        s = self.settings
        start = max(0.0, s.song_start or 0.0)
        end = self.song.duration if not s.song_end else min(s.song_end, self.song.duration)
        if end - start < 3:
            raise ValueError("The selected part of the song must be at least 3 seconds long.")
        return start, end

    def direct(self, cancel=None, progress=None) -> Direction:
        s = self.settings
        start, end = self.song_range()
        if s.use_ai:
            self.ai = AIDirector(api_key=s.api_key, model=s.model)
            aspect = s.aspect if s.aspect != "auto" else "16:9 (or vertical 9:16 if the idea asks for it)"
            self.direction = self.ai.direct(s.prompt, self.song, self.footage, start, end, aspect, s.style,
                                            cancel, progress)
        else:
            self.direction = read_prompt(s.prompt, style_direction(s.style))
            self.direction.summary = ""
            if self.direction.style.pure and asks_for_effects(s.prompt):
                self.direction.style.pure = False  # they asked for effects on top of clean cuts
        if self.manual_drops is not None:  # the user's own drop marks win
            self.direction.drops = list(self.manual_drops)
        return self.direction

    def refine(self, request: str) -> Direction:
        if self.direction is None:
            raise ValueError("Make an edit first.")
        if self.settings.use_ai and self.ai is not None:
            self.direction = self.ai.refine(request)
            if self.manual_drops is not None:
                self.direction.drops = list(self.manual_drops)
        else:
            # Free mode: the refinement is read as extra keywords on top of the current edit.
            self.direction = read_prompt(request, self.direction)
            self.settings.prompt = (self.settings.prompt + ". " + request).strip(". ")
        return self.direction

    def refine_region(self, request: str, start: float, end: float) -> list[str]:
        """Change only one selected part of the song. Returns what was understood."""
        if self.direction is None:
            raise ValueError("Make an edit first.")
        start, end = max(0.0, min(start, end)), max(start, end)
        if end - start < 0.5:
            raise ValueError("Select a longer part of the song (drag across the timeline).")
        span = f"{int(start // 60)}:{start % 60:04.1f} to {int(end // 60)}:{end % 60:04.1f}"
        if self.settings.use_ai and self.ai is not None:
            self.direction = self.ai.refine(
                f"Only for the part of the song from {span} (song seconds {start:.2f} to {end:.2f}): "
                f"{request.strip()}. Make that part its own section with exactly those bounds, and leave "
                f"everything outside it as it is.")
            if self.manual_drops is not None:
                self.direction.drops = list(self.manual_drops)
            return [f"{span}: sent to the AI Director"]
        from .direction import region_direction

        sd, notes = region_direction(request, start, end)
        if sd is None:
            raise ValueError("I didn't understand that change for the selected part. Try words like "
                             "quicker cuts, slower cuts, hold one shot, cut on the hits, punches, fight scenes, "
                             "flashes, shake, slow motion, or back to the flow.")
        # A newer edit of the same stretch replaces the older one.
        self.direction.regions = [r for r in self.direction.regions if r.end <= start or r.start >= end]
        self.direction.regions.append(sd)
        return notes

    def remove_region(self, t: float) -> bool:
        if self.direction is None:
            return False
        before = len(self.direction.regions)
        self.direction.regions = [r for r in self.direction.regions if not (r.start <= t < r.end)]
        return len(self.direction.regions) != before

    def output_settings(self) -> OutputSettings:
        s = self.settings
        aspect = s.aspect
        if aspect == "auto":
            aspect = (self.direction.aspect if self.direction and self.direction.aspect else None) or "16:9"
        w, h = ASPECTS.get(aspect, ASPECTS["16:9"])
        start, end = self.song_range()
        if s.seed is None:
            s.seed = random.randint(1, 10 ** 6)
        return OutputSettings(width=w, height=h, fps=s.fps, fit=s.fit, song_start=start, song_end=end, seed=s.seed)

    def make_plan(self) -> EditPlan:
        self.plan = make_plan(self.song, self.footage, self.direction, self.output_settings())
        return self.plan

    def reroll(self) -> EditPlan:
        self.settings.seed = random.randint(1, 10 ** 6)
        return self.make_plan()

    def render_preview(self, cancel=None, progress=None) -> Path:
        out = workspace_dir() / "previews" / "preview.mp4"
        # A fresh name each time so video players never show a stale cached file.
        out = out.with_name(f"preview_{int(time.time() * 1000)}.mp4")
        for old in out.parent.glob("preview_*.mp4") if out.parent.is_dir() else []:
            try:
                old.unlink()
            except OSError:
                pass
        self.preview_path = self.renderer.render(self.plan, out, PREVIEW, cancel, progress)
        return self.preview_path

    def export(self, out: str | Path, best: bool = False, cancel=None, progress=None) -> Path:
        quality: Quality = EXPORT_BEST if best else EXPORT
        return self.renderer.render(self.plan, out, quality, cancel, progress)

    def default_export_path(self) -> Path:
        name = Path(self.song_path).stem if self.song_path else "edit"
        safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in name).strip() or "edit"
        folder = workspace_dir() / "Exports"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{safe} - {self.settings.style}.mp4"
        i = 2
        while path.exists():
            path = folder / f"{safe} - {self.settings.style} ({i}).mp4"
            i += 1
        return path

    def stop(self) -> None:
        self.renderer.cancel()
