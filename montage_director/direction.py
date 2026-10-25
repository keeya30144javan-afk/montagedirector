"""Styles and Directions.

A Style is a named recipe (cut speed per song section, effect amounts, look).
A Direction is what a director decides for one edit: a style plus
per-section overrides, specific moments ("freeze at 1:05") and a look.
Both the Free Director (keywords) and the AI Director produce Directions.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

from .plan import Look

SECTION_LABELS = ("intro", "verse", "build", "drop", "breakdown", "outro")
EFFECTS = ("flash", "zoom", "shake", "rgb", "slowmo", "reverse", "invert")
CUT_CHOICES = (0.5, 1.0, 2.0, 4.0, 8.0)  # beats per cut
MOMENT_ACTIONS = ("flash", "freeze", "zoom", "shake", "rgb", "invert", "slowmo",
                  "reverse", "fade_white", "fade_black", "cut")


def _fx(**kw) -> dict[str, float]:
    return {k: float(kw.get(k, 0.0)) for k in EFFECTS}


@dataclass
class Style:
    id: str
    name: str
    description: str
    cut_every: dict[str, float]               # beats per cut, by section label
    effects: dict[str, dict[str, float]]      # section label -> effect -> 0..1
    flash_top: float = 0.06                   # share of song hits that get a flash
    flash_frames: int = 3
    freeze_before_drop: bool = False
    white_into_drop: bool = False             # fade from white on the drop instead of a hard flash
    blur_into_sections: bool = False
    energy_bias: float = 0.0                  # push shot choice toward calmer (-) or wilder (+) footage
    look: Look = field(default_factory=Look)
    pure: bool = False                        # cuts only: no automatic effects, fades or speed changes
    cut_on_fills: bool = False                # add cuts on the hits of drum fills


def _cuts(intro, verse, build, drop, breakdown, outro):
    return dict(zip(SECTION_LABELS, (intro, verse, build, drop, breakdown, outro)))


STYLES: dict[str, Style] = {s.id: s for s in [
    Style(
        "hype", "Hype AMV/GMV",
        "Fast cuts that hit every beat on the drop, punches and shake on the big hits.",
        _cuts(2, 1, 1, 0.5, 2, 4),
        {"intro": _fx(flash=0.2, zoom=0.2), "verse": _fx(flash=0.4, zoom=0.4, shake=0.2, rgb=0.1),
         "build": _fx(flash=0.4, zoom=0.6, shake=0.3), "drop": _fx(flash=0.8, zoom=0.7, shake=0.7, rgb=0.35, invert=0.1),
         "breakdown": _fx(slowmo=0.4, flash=0.1), "outro": _fx(slowmo=0.3)},
        flash_top=0.08, flash_frames=3, freeze_before_drop=True, energy_bias=0.15,
        look=Look(grade="y2k", glow=0.25), cut_on_fills=True,
    ),
    Style(
        "flash2000s", "2000s Flash Edit",
        "Classic YouTube-era edit: white flashes, zoom punches, glow and ghost trails.",
        _cuts(2, 2, 1, 1, 2, 4),
        {"intro": _fx(flash=0.5, zoom=0.3, slowmo=0.2), "verse": _fx(flash=0.6, zoom=0.5),
         "build": _fx(flash=0.7, zoom=0.7, shake=0.2), "drop": _fx(flash=1.0, zoom=0.8, shake=0.5, rgb=0.2),
         "breakdown": _fx(flash=0.3, slowmo=0.5), "outro": _fx(flash=0.3, slowmo=0.4)},
        flash_top=0.14, flash_frames=4, white_into_drop=True, blur_into_sections=True,
        look=Look(grade="y2k", glow=0.45, trails=0.25), cut_on_fills=True,
    ),
    Style(
        "sad", "Sad / Emotional",
        "Long, slow shots, soft fades, washed-out colour and dreamy ghosting.",
        _cuts(4, 4, 2, 2, 4, 8),
        {"intro": _fx(slowmo=0.6), "verse": _fx(slowmo=0.5), "build": _fx(slowmo=0.3, zoom=0.1),
         "drop": _fx(flash=0.2, slowmo=0.3, zoom=0.15), "breakdown": _fx(slowmo=0.7), "outro": _fx(slowmo=0.7)},
        flash_top=0.02, flash_frames=6, white_into_drop=True, blur_into_sections=True, energy_bias=-0.3,
        look=Look(grade="sad", glow=0.3, trails=0.45, letterbox=True),
    ),
    Style(
        "cinematic", "Cinematic Trailer",
        "Letterboxed, colour graded, steady pacing that tightens on the drop.",
        _cuts(4, 2, 1, 1, 4, 4),
        {"intro": _fx(slowmo=0.3), "verse": _fx(zoom=0.1), "build": _fx(zoom=0.3),
         "drop": _fx(flash=0.3, zoom=0.4, shake=0.3), "breakdown": _fx(slowmo=0.4), "outro": _fx(slowmo=0.4)},
        flash_top=0.03, flash_frames=3, freeze_before_drop=True,
        look=Look(grade="cinematic", glow=0.15, letterbox=True),
    ),
    Style(
        "glitch", "Glitch / VHS",
        "RGB splits, negative strobes and shake over a grainy VHS look.",
        _cuts(2, 1, 1, 0.5, 2, 2),
        {"intro": _fx(rgb=0.4), "verse": _fx(rgb=0.6, shake=0.3, invert=0.1), "build": _fx(rgb=0.7, shake=0.5, zoom=0.3),
         "drop": _fx(rgb=0.9, shake=0.8, invert=0.35, zoom=0.5, flash=0.3), "breakdown": _fx(rgb=0.3, reverse=0.3),
         "outro": _fx(rgb=0.3)},
        flash_top=0.04, flash_frames=2, energy_bias=0.1,
        look=Look(grade="vhs", grain=0.5), cut_on_fills=True,
    ),
    Style(
        "clean", "Clean Beat Sync",
        "A cut on every beat, nothing else: no effects, no colour changes. "
        "Say \"every 2 beats\" or \"every bar\" for slower cutting.",
        _cuts(1, 1, 1, 1, 1, 1),
        {label: _fx() for label in SECTION_LABELS},
        flash_top=0.0, flash_frames=2, pure=True,
    ),
]}
DEFAULT_STYLE = "hype"


@dataclass
class SectionDirection:
    start: float
    end: float
    cut_every: float | None = None            # beats per cut; None = style default
    energy: float | None = None               # 0..1 target shot intensity; None = follow the music
    shots: list[int] = field(default_factory=list)  # preferred shots, in the order to use them
    effects: dict[str, float] = field(default_factory=dict)
    note: str = ""
    cue: str | None = None      # a musical event this starts at ("solo", "drop", ...), resolved by the planner
    hold: bool = False          # one held shot for the whole section
    still: bool = False         # ...and it should be a calm, still shot
    sync_hits: bool = False     # cut on the actual drum hits, not the beat grid
    fixed: bool = False         # start/end were chosen exactly (a selected region): don't move them
    camera: str | None = None   # prefer shots with this camera move: pan | zoom | shaky | still
    occurrence: int | None = None  # which time the cue happens: 1 = first, 2 = second, -1 = last
    span: str | None = None     # a stretch relative to the edit: "tail:20", "head:30", "half", "firsthalf"
    prefer: dict[str, float] = field(default_factory=dict)  # shot content: motion, bright, warm, sat, hue
    sync_on: str = "hits"       # with sync_hits: cut on "hits" (kicks + snares), "kick" or "snare"
    ramp: bool = False          # cuts get quicker and quicker toward the end of the section
    velocity: bool = False      # velocity edit: fast clips that slow down on the big beats
    drum_fx: dict[str, str] = field(default_factory=dict)   # effect -> drum it lands on ("snare", "kick", ...)
    what: str = ""              # how the free reader summed it up, for the timeline under the player


@dataclass
class Moment:
    time: float      # song seconds
    action: str
    duration: float = 0.0
    tapped: bool = False  # placed by the user in Tap to Direct (exact: snapped to a hit, never moved)


@dataclass
class Direction:
    style: Style
    look: Look
    sections: list[SectionDirection] = field(default_factory=list)
    moments: list[Moment] = field(default_factory=list)
    avoid_shots: list[int] = field(default_factory=list)
    summary: str = ""
    notes: list[str] = field(default_factory=list)   # what the director understood, for the UI
    aspect: str | None = None                       # "16:9" | "9:16" | "1:1" suggested by the prompt
    repeats: str = "rarely"                         # never | rarely | allowed: reusing a scene
    regions: list[SectionDirection] = field(default_factory=list)  # per-region refinements (win over sections)
    tap_cuts: list[float] = field(default_factory=list)            # cuts the user tapped (song seconds)
    drops: list[float] | None = None                # song seconds; None = use the song analysis
    # Free-reader extras, by song-section label ("drop", "verse"...) or "all":
    prefer: dict[str, dict[str, float]] = field(default_factory=dict)  # what kind of shots to pick
    sync: dict[str, str] = field(default_factory=dict)       # cut on the drums instead of the beat grid
    drum_fx: list[dict] = field(default_factory=list)        # {"effect", "drum", "label"}: effect on each drum hit
    ramp: list[str] = field(default_factory=list)            # labels whose cuts speed up toward the end
    velocity: list[str] = field(default_factory=list)        # labels cut as a velocity edit
    unclear: list[str] = field(default_factory=list)         # parts of the prompt the free reader didn't get
    rate_locked: list[str] = field(default_factory=list)     # labels whose beats-per-cut the user set exactly

    def section_direction(self, t: float) -> SectionDirection | None:
        for s in self.sections:
            if s.start <= t < s.end:
                return s
        return None


def style_direction(style_id: str) -> Direction:
    style = copy.deepcopy(STYLES.get(style_id, STYLES[DEFAULT_STYLE]))
    return Direction(style=style, look=copy.deepcopy(style.look))


# --------------------------------------------------------------------------- #
# Free Director: keyword prompt reader
# --------------------------------------------------------------------------- #
_SECTION_WORDS = {
    "intro": "intro", "beginning": "intro", "start": "intro", "opening": "intro",
    "verse": "verse", "chorus": "drop", "drop": "drop", "drops": "drop", "hook": "drop",
    "build": "build", "buildup": "build", "build-up": "build", "breakdown": "breakdown",
    "bridge": "breakdown", "outro": "outro", "ending": "outro", "end": "outro",
}
_FASTER = ("fast", "faster", "hype", "hyped", "aggressive", "crazy", "insane", "hard", "harder",
           "intense", "chaotic", "go off", "goes off", "wild", "quick")
_SLOWER = ("slow", "slower", "chill", "calm", "emotional", "sad", "soft", "gentle", "peaceful",
           "relaxed", "smooth")
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def _has(text: str, words) -> bool:
    return any(re.search(r"\b" + re.escape(w) + r"\b", text) for w in words)


def _negated(text: str, word: str) -> bool:
    return bool(re.search(r"\b(no|without|less|fewer|don'?t use|stop|zero)\s+(\w+\s+)?" + word, text))


def read_prompt(prompt: str, base: Direction) -> Direction:
    """Adjust a style-based Direction from a plain-English prompt (see reader.py)."""
    from .reader import read_prompt as _read

    return _read(prompt, base)


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "eight": 8, "other": 2, "single": 1, "each": 1}


def _cut_rate(clause: str) -> float | None:
    """Beats per cut from phrases like 'every 2 beats', 'every other beat', 'every bar', 'twice a beat'."""
    if re.search(r"\b(twice (a|per|every) beat|half[- ]beats?|double[- ]time|every half beat)\b", clause):
        return 0.5
    m = re.search(r"\b(?:every|each|on (?:every|each|the))\s+(\d+|one|two|three|four|eight|other|single)?\s*"
                  r"(beat|beats|bar|bars|measure|measures)\b", clause)
    if not m:
        return None
    n = m.group(1)
    count = float(_NUM.get(n, n)) if n else 1.0
    per = 4.0 if m.group(2).startswith(("bar", "measure")) else 1.0
    return min((0.5, 1.0, 2.0, 4.0, 8.0), key=lambda c: abs(c - count * per))


def _rate_label(rate: float) -> str:
    return {0.5: "half beat", 1.0: "beat", 2.0: "2 beats", 4.0: "bar", 8.0: "2 bars"}[rate]


_EFFECT_WORDS = ("flash", "shake", "zoom", "glitch", "rgb", "slow mo", "slowmo", "slow-mo", "slow motion",
                 "reverse", "strobe", "invert", "glow", "bloom", "trail", "ghost", "freeze", "fade", "letterbox",
                 "vhs", "grain", "black and white", "b&w", "monochrome", "y2k", "cinematic", "grade", "effect")


def asks_for_effects(prompt: str) -> bool:
    """True when a prompt explicitly asks for an effect or a look."""
    text = prompt.lower()
    return any(w in text for w in _EFFECT_WORDS) and not re.search(r"\b(no|without)\s+(effects|fx)\b", text)


def read_timeline(prompt: str, d: Direction, notes: list[str], window: tuple[float, float] | None = None) -> None:
    from .reader import read_timeline as _read

    _read(prompt, d, notes, window)


def region_direction(text: str, start: float, end: float):
    """A refinement for one selected region of the song ("quicker cuts with punches")."""
    from .reader import region_direction as _region

    return _region(text, start, end)
