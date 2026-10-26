"""One edit project: inputs, settings and the analyse -> direct -> plan -> render pipeline."""

from __future__ import annotations

import copy
import json
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .direction import (DEFAULT_STYLE, Direction, Moment, SectionDirection, Style, asks_for_effects, read_prompt,
                        style_direction)
from .director_ai import DEFAULT_MODEL, AIDirector
from .footage import Footage, analyze_footage
from .music import SongMap, analyze_song
from .paths import workspace_dir
from .plan import EditPlan, Look
from .planner import OutputSettings, make_plan
from .render import EXPORT, EXPORT_BEST, PREVIEW, Quality, Renderer

log = logging.getLogger(__name__)

ASPECTS = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080), "4:3": (1440, 1080)}
PROJECT_VERSION = 2

# Scene keys for Tap to Direct: keys 1-9 put a kind of shot on the tapped cut.
SCENE_PRESETS = {
    "fight": ("Fight / action", {"motion": 1.0}),
    "power": ("Power-up / glow", {"bright": 1.0, "sat": 0.6}),
    "closeup": ("Close-ups / faces", {"motion": -1.0}),
    "explosion": ("Explosions / blasts", {"bright": 1.0, "motion": 0.6}),
    "calm": ("Calm / scenery", {"motion": -1.0, "bright": 0.3}),
    "dark": ("Dark scenes", {"bright": -1.0}),
    "bright": ("Bright scenes", {"bright": 1.0}),
    "colour": ("Colourful", {"sat": 1.0}),
    "group": ("Many characters (needs Describe my shots)", {"crowd": 1.0}),
    "favorites": ("My ★ favourite shots", None),
    "any": ("Anything", {}),
}
DEFAULT_SCENE_KEYS = {"1": "fight", "2": "power", "3": "closeup", "4": "explosion", "5": "calm", "6": "dark",
                      "7": "group", "8": "favorites", "9": "any"}

# Export presets: (label, quality, size override, fps override, size limit in MB)
EXPORT_PRESETS = {
    "high": ("High quality (your settings)", EXPORT, None, None, None),
    "best": ("Best quality (slower)", EXPORT_BEST, None, None, None),
    "youtube": ("YouTube 1080p60", EXPORT, (1920, 1080), 60, None),
    "tiktok": ("TikTok / Shorts / Reels (vertical)", EXPORT, (1080, 1920), 30, None),
    "discord": ("Discord (under 25 MB)", EXPORT, None, None, 24.0),
}


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
    previous_preview: Path | None = None     # the preview before the last change (for compare)
    history: list[dict] = field(default_factory=list)   # undo stack of edit states
    future: list[dict] = field(default_factory=list)    # redo stack
    path: Path | None = None                 # where this project is saved
    manual_drops: list[float] | None = None   # drops the user marked on the timeline
    taps: list[tuple[float, str]] = field(default_factory=list)  # Tap to Direct: (song seconds, kind)
    exact_taps: list[float] = field(default_factory=list)   # taps the user placed exactly (not snapped)
    texts: list[dict] = field(default_factory=list)         # text cards added in the app
    favorites: list[str] = field(default_factory=list)      # ★ shots, as "video path|start seconds"
    banned: list[str] = field(default_factory=list)         # ✕ shots
    scene_keys: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SCENE_KEYS))
    shot_tags: dict[int, dict] = field(default_factory=dict)  # AI shot descriptions (cached per video)
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
        from .director_ai import cached_tags

        self.shot_tags = cached_tags(self.footage)
        fresh = bool(self._analysed_for)  # material changed after an earlier analysis
        self._analysed_for = key
        self.ai = None  # new material: start a fresh AI conversation
        if fresh:
            self.manual_drops = None
            self.taps = []

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
            # The free reader's drum-level reading backs the AI up ("cut on the kick in the chorus",
            # "flash on every snare"): parts of the song, which the AI's timed sections can't name.
            free = read_prompt(s.prompt, style_direction(s.style))
            d = self.direction
            for lab, kind in free.sync.items():
                d.sync.setdefault(lab, kind)
            known = {(e["effect"], e["drum"]) for sd in d.sections for e in
                     ({"effect": k, "drum": v} for k, v in sd.drum_fx.items())}
            d.drum_fx += [e for e in free.drum_fx if (e["effect"], e["drum"]) not in known]
            d.ramp = sorted(set(d.ramp) | set(free.ramp))
            d.velocity = sorted(set(d.velocity) | set(free.velocity))
        else:
            self.direction = read_prompt(s.prompt, style_direction(s.style))
            self.direction.summary = ""
            if self.direction.style.pure and asks_for_effects(s.prompt):
                self.direction.style.pure = False  # they asked for effects on top of clean cuts
        if self.manual_drops is not None:  # the user's own drop marks win
            self.direction.drops = list(self.manual_drops)
        self.apply_taps()
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
        self.apply_taps()
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
        from .reader import extract_texts

        rest, cards = extract_texts(request)
        notes: list[str] = []
        for c in cards:  # text "THE END" on a selected part: shown over that part
            c["time"], c["duration"] = round(start, 3), round(min(end - start, 8.0), 3)
            self.texts.append(c)
            notes.append(f"{span}: text “{c['text']}”")
        if cards and not re.sub(r"[^a-z]", "", rest.lower()):
            return notes
        request = rest if cards else request
        sd, more = region_direction(request, start, end)
        notes += more
        if sd is None and cards:
            return notes  # the text was the whole instruction
        if sd is None:
            raise ValueError(f"I didn't understand \"{request.strip()}\" for the selected part. Try things like: "
                             "quicker cuts, slower cuts, hold one shot, cut on the hits / kick / snare, fight scenes, "
                             "explosions, close-ups, dark scenes, more flashes, flash on every snare, zoom, shake, "
                             "slow motion, glitch, faster and faster cuts, velocity edit, freeze, fade out, no effects, "
                             "or back to the flow. (Or press What can I say? under your idea for the full list.)")
        # A newer edit of the same stretch replaces the older one.
        self.direction.regions = [r for r in self.direction.regions if r.end <= start or r.start >= end]
        self.direction.regions.append(sd)
        return notes

    def apply_taps(self) -> None:
        """Put the user's taps into the current direction (replacing earlier taps)."""
        if self.direction is None:
            return
        d = self.direction
        d.tap_cuts = [t for t, k in self.taps if k == "cut" or k.startswith("scene")]
        d.tap_exact = list(self.exact_taps)
        d.tap_scenes = []
        for t, k in self.taps:
            if k.startswith("scene"):
                preset = SCENE_PRESETS.get(self.scene_keys.get(k[5:], "any"), SCENE_PRESETS["any"])
                d.tap_scenes.append({"time": t, "prefer": dict(preset[1] or {}), "favorites": preset[1] is None,
                                     "label": preset[0]})
        d.moments = [m for m in d.moments if not m.tapped]
        for t, k in self.taps:
            if k in ("flash", "zoom", "shake"):
                d.moments.append(Moment(time=t, action=k, duration=0.4 if k == "shake" else 0.0, tapped=True))
        tapped_drops = [t for t, k in self.taps if k == "drop"]
        if tapped_drops:
            base = self.manual_drops if self.manual_drops is not None else list(self.song.drops)
            # a tapped drop replaces a detected one nearby
            kept = [x for x in base if all(abs(x - t) > 4.0 for t in tapped_drops)]
            self.manual_drops = sorted(kept + tapped_drops)
            d.drops = list(self.manual_drops)

    def clear_taps(self) -> None:
        self.taps = []
        self.exact_taps = []
        self.apply_taps()

    def move_tap(self, old: float, new: float, exact: bool = False) -> bool:
        """Drag a tap to a new place (exact = don't snap it to a drum hit)."""
        for i, (t, k) in enumerate(self.taps):
            if abs(t - old) <= 1e-3:
                new = round(max(0.0, new), 3)
                self.taps[i] = (new, k)
                self.exact_taps = [x for x in self.exact_taps if abs(x - old) > 1e-3]
                if exact:
                    self.exact_taps.append(new)
                if k == "drop" and self.manual_drops is not None:
                    self.manual_drops = sorted(new if abs(d - old) <= 1e-3 else d for d in self.manual_drops)
                self.apply_taps()
                return True
        return False

    # ----- shots: favourites, never-use, AI descriptions ----------------------- #
    def shot_key(self, shot_id: int) -> str:
        s = self.footage.shots[shot_id]
        return f"{self.footage.sources[s.source].path}|{s.start:.2f}"

    def _ids(self, keys: list[str]) -> list[int]:
        if self.footage is None:
            return []
        lookup = {self.shot_key(s.id): s.id for s in self.footage.shots}
        return [lookup[k] for k in keys if k in lookup]

    def favorite_ids(self) -> list[int]:
        return self._ids(self.favorites)

    def banned_ids(self) -> list[int]:
        return self._ids(self.banned)

    def mark_shot(self, shot_id: int, mark: str | None) -> None:
        """mark: "favorite", "banned" or None (clear)."""
        key = self.shot_key(shot_id)
        self.favorites = [k for k in self.favorites if k != key]
        self.banned = [k for k in self.banned if k != key]
        if mark == "favorite":
            self.favorites.append(key)
        elif mark == "banned":
            self.banned.append(key)

    def describe_shots(self, cancel=None, progress=None) -> int:
        """Have Claude (Haiku, the cheapest model) describe every shot once: who is on screen, what
        happens, how many characters. Saved per video, then used by the free director."""
        from .director_ai import AIDirector

        ai = AIDirector(api_key=self.settings.api_key, model="claude-haiku-5-5")
        self.shot_tags = ai.describe_footage(self.footage, cancel, progress)
        return len(self.shot_tags)

    def pick_region_shot(self, start: float, end: float, shot_ids, freeze: bool = False) -> None:
        """Shots the user chose for a selected part of the song: one shot is held for the whole
        part (or frozen as a still); several shots play in order on the cuts already there."""
        if self.direction is None:
            raise ValueError("Make an edit first.")
        ids = [shot_ids] if isinstance(shot_ids, int) else [int(i) for i in shot_ids]
        if not ids:
            return
        start, end = max(0.0, min(start, end)), max(start, end)
        if end - start < 0.3:
            raise ValueError("Select a longer part of the song (drag across the timeline).")
        if len(ids) == 1:
            sd = SectionDirection(start=start, end=end, fixed=True, hold=True, cut_every=8.0, shots=ids, picked=True,
                                  freeze_frame=freeze, energy=0.3, note=f"your pick: shot {ids[0]}",
                                  what=f"your pick (shot {ids[0]}{', as a still' if freeze else ''})")
            sd.effects = {k: 0.0 for k in ("flash", "zoom", "shake", "rgb", "slowmo", "reverse", "invert")}
        else:
            sd = SectionDirection(start=start, end=end, fixed=True, keep_cuts=True, picked=True, shots=ids,
                                  note=f"your picks: {len(ids)} shots",
                                  what=f"your {len(ids)} picked shots, in order, on the cuts here")
        self.direction.regions = [r for r in self.direction.regions if r.end <= start or r.start >= end]
        self.direction.regions.append(sd)

    def remove_taps(self, times: list[float]) -> int:
        """Take out individual taps (by their time)."""
        before = len(self.taps)
        gone = [(t, k) for t, k in self.taps if any(abs(t - x) <= 1e-3 for x in times)]
        self.taps = [tk for tk in self.taps if tk not in gone]
        if self.manual_drops is not None:  # a tapped drop that's taken out stops being a drop
            dropped = [t for t, k in gone if k == "drop"]
            self.manual_drops = [d for d in self.manual_drops if all(abs(d - t) > 1e-3 for t in dropped)]
            if self.direction is not None:
                self.direction.drops = list(self.manual_drops)
        self.apply_taps()
        return before - len(self.taps)

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

    def _sync_direction(self) -> None:
        """Things kept with the project (not the prompt) go into the direction before planning."""
        d = self.direction
        d.user_texts = [dict(t) for t in self.texts]
        d.favorites = self.favorite_ids()
        d.banned = self.banned_ids()
        d.shot_tags = self.shot_tags
        d.tap_exact = list(self.exact_taps)

    def make_plan(self) -> EditPlan:
        self._sync_direction()
        self.plan = make_plan(self.song, self.footage, self.direction, self.output_settings())
        return self.plan

    def reroll(self) -> EditPlan:
        self.settings.seed = random.randint(1, 10 ** 6)
        return self.make_plan()

    def render_preview(self, cancel=None, progress=None) -> Path:
        out = workspace_dir() / "previews" / "preview.mp4"
        # A fresh name each time so video players never show a stale cached file.
        out = out.with_name(f"preview_{int(time.time() * 1000)}.mp4")
        keep = {p for p in (self.preview_path,) if p}   # the current one becomes "before"
        for old in out.parent.glob("preview_*.mp4") if out.parent.is_dir() else []:
            if old not in keep:
                try:
                    old.unlink()
                except OSError:
                    pass
        new = self.renderer.render(self.plan, out, PREVIEW, cancel, progress)
        self.previous_preview, self.preview_path = self.preview_path, new
        return new

    def export(self, out: str | Path, preset: str = "high", cancel=None, progress=None) -> Path:
        """Render the full-quality video with one of EXPORT_PRESETS."""
        if isinstance(preset, bool):  # older callers: best=True/False
            preset = "best" if preset else "high"
        _, quality, size, fps, limit_mb = EXPORT_PRESETS.get(preset, EXPORT_PRESETS["high"])
        plan = self.plan
        if size or fps:
            # Different size or frame rate: plan again with the same seed (same cuts, same shots).
            out_set = self.output_settings()
            if size:
                out_set.width, out_set.height = size
            if fps:
                out_set.fps = fps
            self._sync_direction()
            plan = make_plan(self.song, self.footage, self.direction, out_set)
        if limit_mb:
            audio_kbps = 128
            kbps = int(limit_mb * 8 * 1024 / max(plan.duration, 1.0) * 0.93) - audio_kbps
            kbps = max(300, kbps)
            scale = 1.0 if kbps >= 3500 else (2 / 3 if kbps >= 1500 else 0.5)  # smaller picture, cleaner result
            quality = Quality("limited", scale, "medium", 23, f"{audio_kbps}k", f"{kbps}k")
        return self.renderer.render(plan, out, quality, cancel, progress)

    # ----- undo / redo ------------------------------------------------------- #
    def snapshot(self) -> dict:
        return {"direction": copy.deepcopy(self.direction), "manual_drops": copy.deepcopy(self.manual_drops),
                "taps": list(self.taps), "seed": self.settings.seed, "prompt": self.settings.prompt,
                "exact_taps": list(self.exact_taps), "texts": copy.deepcopy(self.texts),
                "favorites": list(self.favorites), "banned": list(self.banned)}

    def checkpoint(self) -> None:
        """Remember the current edit before a change, so it can be undone."""
        if self.direction is None:
            return
        self.history.append(self.snapshot())
        del self.history[:-50]
        self.future.clear()

    def _restore(self, state: dict) -> None:
        self.direction = copy.deepcopy(state["direction"])
        self.manual_drops = copy.deepcopy(state["manual_drops"])
        self.taps = list(state["taps"])
        self.settings.seed = state["seed"]
        self.settings.prompt = state["prompt"]
        self.exact_taps = list(state.get("exact_taps", []))
        self.texts = copy.deepcopy(state.get("texts", []))
        self.favorites = list(state.get("favorites", []))
        self.banned = list(state.get("banned", []))

    def undo(self) -> bool:
        if not self.history:
            return False
        self.future.append(self.snapshot())
        self._restore(self.history.pop())
        return True

    def redo(self) -> bool:
        if not self.future:
            return False
        self.history.append(self.snapshot())
        self._restore(self.future.pop())
        return True

    # ----- save / open ------------------------------------------------------- #
    def to_dict(self) -> dict:
        s = asdict(self.settings)
        s.pop("api_key", None)  # never written into project files
        return {
            "app": "Montage Director", "version": PROJECT_VERSION,
            "footage": list(self.footage_paths), "song": self.song_path, "settings": s,
            "direction": _direction_to_dict(self.direction) if self.direction else None,
            "manual_drops": self.manual_drops, "taps": [list(t) for t in self.taps],
            "exact_taps": self.exact_taps, "texts": self.texts, "favorites": self.favorites,
            "banned": self.banned, "scene_keys": self.scene_keys,
        }

    def save_copy(self, path: str | Path) -> None:
        """A recovery copy (autosave): doesn't change where the project is saved."""
        data = self.to_dict()
        data["saved_as"] = str(self.path) if self.path else ""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        if path.suffix.lower() != ".mdproj":
            path = path.with_suffix(".mdproj")
        tmp = path.with_suffix(".mdproj.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")
        tmp.replace(path)
        self.path = path
        return path

    @classmethod
    def load(cls, path: str | Path, api_key: str = "") -> "Project":
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"Couldn't open {path.name}: it isn't a Montage Director project.") from exc
        if data.get("app") != "Montage Director":
            raise ValueError(f"{path.name} isn't a Montage Director project.")
        settings = Settings(**{k: v for k, v in data.get("settings", {}).items() if k in Settings.__dataclass_fields__})
        settings.api_key = api_key
        proj = cls(footage_paths=data.get("footage", []), song_path=data.get("song", ""), settings=settings)
        missing = [p for p in proj.footage_paths + [proj.song_path] if p and not Path(p).exists()]
        if missing:
            names = ", ".join(Path(p).name for p in missing[:3])
            raise ValueError(f"Some files in this project have moved or been deleted: {names}. "
                             "Put them back, or start a new project with them.")
        if data.get("direction"):
            proj.direction = _direction_from_dict(data["direction"])
        proj.manual_drops = data.get("manual_drops")
        proj.taps = [tuple(t) for t in data.get("taps", [])]
        proj.exact_taps = [float(x) for x in data.get("exact_taps", [])]
        proj.texts = [dict(t) for t in data.get("texts", [])]
        proj.favorites = [str(k) for k in data.get("favorites", [])]
        proj.banned = [str(k) for k in data.get("banned", [])]
        proj.scene_keys = {**DEFAULT_SCENE_KEYS, **{str(k): v for k, v in (data.get("scene_keys") or {}).items()
                                                    if v in SCENE_PRESETS}}
        proj.path = Path(data["saved_as"]) if data.get("saved_as") else (None if "saved_as" in data else path)
        return proj

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



def _direction_to_dict(d: Direction) -> dict:
    out = asdict(d)
    for k in ("shot_tags", "favorites", "banned", "user_texts"):  # kept with the project, filled in when planning
        out.pop(k, None)
    return out


def _direction_from_dict(data: dict) -> Direction:
    data = copy.deepcopy(data)
    style = data.pop("style")
    style["look"] = Look(**style.get("look", {}))
    sd_fields = SectionDirection.__dataclass_fields__
    mo_fields = Moment.__dataclass_fields__
    return Direction(
        style=Style(**{k: v for k, v in style.items() if k in Style.__dataclass_fields__}),
        look=Look(**data.pop("look", {})),
        sections=[SectionDirection(**{k: v for k, v in sd.items() if k in sd_fields}) for sd in data.pop("sections", [])],
        regions=[SectionDirection(**{k: v for k, v in sd.items() if k in sd_fields}) for sd in data.pop("regions", [])],
        moments=[Moment(**{k: v for k, v in m.items() if k in mo_fields}) for m in data.pop("moments", [])],
        **{k: v for k, v in data.items() if k in Direction.__dataclass_fields__},
    )
