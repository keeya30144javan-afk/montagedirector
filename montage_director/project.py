"""One edit project: inputs, settings and the analyse -> direct -> plan -> render pipeline."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

from .direction import DEFAULT_STYLE, Direction, read_prompt, style_direction
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

    def song_range(self) -> tuple[float, float]:
        s = self.settings
        start = max(0.0, s.song_start or 0.0)
        end = self.song.duration if not s.song_end else min(s.song_end, self.song.duration)
        if end - start < 3:
            raise ValueError("The selected part of the song must be at least 3 seconds long.")
        return start, end

    def direct(self) -> Direction:
        s = self.settings
        start, end = self.song_range()
        if s.use_ai:
            self.ai = AIDirector(api_key=s.api_key, model=s.model)
            aspect = s.aspect if s.aspect != "auto" else "16:9 (or vertical 9:16 if the idea asks for it)"
            self.direction = self.ai.direct(s.prompt, self.song, self.footage, start, end, aspect, s.style)
        else:
            self.direction = read_prompt(s.prompt, style_direction(s.style))
            self.direction.summary = ""
        return self.direction

    def refine(self, request: str) -> Direction:
        if self.direction is None:
            raise ValueError("Make an edit first.")
        if self.settings.use_ai and self.ai is not None:
            self.direction = self.ai.refine(request)
        else:
            # Free mode: the refinement is read as extra keywords on top of the current edit.
            self.direction = read_prompt(request, self.direction)
            self.settings.prompt = (self.settings.prompt + ". " + request).strip(". ")
        return self.direction

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
