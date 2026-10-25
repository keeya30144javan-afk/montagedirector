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
        look=Look(grade="y2k", glow=0.25),
    ),
    Style(
        "flash2000s", "2000s Flash Edit",
        "Classic YouTube-era edit: white flashes, zoom punches, glow and ghost trails.",
        _cuts(2, 2, 1, 1, 2, 4),
        {"intro": _fx(flash=0.5, zoom=0.3, slowmo=0.2), "verse": _fx(flash=0.6, zoom=0.5),
         "build": _fx(flash=0.7, zoom=0.7, shake=0.2), "drop": _fx(flash=1.0, zoom=0.8, shake=0.5, rgb=0.2),
         "breakdown": _fx(flash=0.3, slowmo=0.5), "outro": _fx(flash=0.3, slowmo=0.4)},
        flash_top=0.14, flash_frames=4, white_into_drop=True, blur_into_sections=True,
        look=Look(grade="y2k", glow=0.45, trails=0.25),
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
        look=Look(grade="vhs", grain=0.5),
    ),
    Style(
        "clean", "Clean Beat Sync",
        "Pure cuts on the beat. No effects, no colour changes.",
        _cuts(2, 2, 1, 1, 2, 4),
        {label: _fx() for label in SECTION_LABELS},
        flash_top=0.0, flash_frames=2,
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


@dataclass
class Moment:
    time: float      # song seconds
    action: str
    duration: float = 0.0


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
    drops: list[float] | None = None                # song seconds; None = use the song analysis

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
    """Adjust a style-based Direction from a plain-English prompt using keywords."""
    d = copy.deepcopy(base)
    text = " " + prompt.lower().strip() + " "
    notes: list[str] = []
    if not prompt.strip():
        return d

    def scale_cuts(labels, factor):
        for lab in labels:
            cur = d.style.cut_every[lab]
            d.style.cut_every[lab] = min(CUT_CHOICES, key=lambda c: abs(c - cur * factor))

    def bump(labels, effect, amount):
        for lab in labels:
            fx = d.style.effects[lab]
            fx[effect] = float(min(1.0, max(0.0, fx.get(effect, 0.0) + amount)))

    # Clause-level: "calm intro, then go crazy on the drop"
    clauses = [c for c in re.split(r"[.;,!]|\bthen\b|\bbut\b|\band\b", text) if c.strip()]
    targeted = set()
    for clause in clauses:
        labels = {lab for w, lab in _SECTION_WORDS.items() if re.search(r"\b" + re.escape(w) + r"\b", clause)}
        if not labels or _TIME.search(clause):
            continue
        targeted |= labels
        names = "/".join(sorted(labels))
        if _has(clause, _FASTER):
            scale_cuts(labels, 0.5)
            for e in ("zoom", "shake", "flash"):
                bump(labels, e, 0.3)
            notes.append(f"{names}: faster, harder")
        if _has(clause, _SLOWER):
            scale_cuts(labels, 2.0)
            bump(labels, "slowmo", 0.4)
            for e in ("shake", "zoom"):
                bump(labels, e, -0.4)
            notes.append(f"{names}: slower, calmer")
        for word, effect in (("flash", "flash"), ("zoom", "zoom"), ("shake", "shake"), ("glitch", "rgb"),
                             ("slow mo", "slowmo"), ("slowmo", "slowmo"), ("slow-mo", "slowmo"),
                             ("slow motion", "slowmo"), ("reverse", "reverse"), ("strobe", "invert")):
            if word in clause:
                if _negated(clause, word):
                    bump(labels, effect, -1.0)
                    notes.append(f"{names}: no {word}")
                else:
                    bump(labels, effect, 0.5)
                    notes.append(f"{names}: more {word}")

    # Whole-edit words (only when they were not tied to a section above)
    every = list(SECTION_LABELS)
    # Sentences with a time ("at 1:34 ...") only affect that moment, not the whole edit.
    # Sentences that name a part of the song ("crazy drop") only affect that part.
    def _names_section(c):
        return any(re.search(r"\b" + re.escape(w) + r"\b", c) for w in _SECTION_WORDS)
    global_text = " " + " ".join(c for c in clauses if not _TIME.search(c) and not _names_section(c)) + " "
    if _has(global_text, _FASTER):
        scale_cuts(every, 0.5)
        d.style.energy_bias = min(0.5, d.style.energy_bias + 0.2)
        notes.append("faster cuts, wilder shots")
    if _has(global_text, _SLOWER):
        scale_cuts(every, 2.0)
        bump(every, "slowmo", 0.3)
        d.style.energy_bias = max(-0.5, d.style.energy_bias - 0.2)
        notes.append("slower cuts, calmer shots")

    if re.search(r"\b(no|without)\s+(effects|fx)\b|\bclean\b", text):
        for lab in every:
            d.style.effects[lab] = _fx()
        d.style.flash_top = 0.0
        d.look = Look()
        notes.append("no effects")
    if "flash" in global_text:
        if _negated(global_text, "flash"):
            d.style.flash_top = 0.0
            bump(every, "flash", -1)
            notes.append("no flashes")
        elif _has(global_text, ("lots of", "more", "tons of", "many", "heavy")):
            d.style.flash_top = min(0.3, d.style.flash_top * 2 + 0.04)
            bump(every, "flash", 0.3)
            notes.append("more flashes")
    for word, effect, label in (("shake", "shake", "shake"), ("zoom", "zoom", "zoom punches"),
                                ("glitch", "rgb", "glitch / RGB split"), ("rgb", "rgb", "RGB split"),
                                ("slow mo", "slowmo", "slow motion"), ("slowmo", "slowmo", "slow motion"),
                                ("slow motion", "slowmo", "slow motion"), ("reverse", "reverse", "reversed clips"),
                                ("strobe", "invert", "negative strobes")):
        if word in global_text:
            if _negated(global_text, word):
                bump(every, effect, -1)
                notes.append(f"no {label}")
            else:
                bump(every, effect, 0.35)
                notes.append(label)

    # Look
    looks = [
        (("vhs", "retro", "grain", "grainy", "old school", "camcorder"), "vhs", "VHS look"),
        (("black and white", "b&w", "monochrome", "grayscale", "greyscale"), "mono", "black and white"),
        (("y2k", "2000s", "2010s", "old youtube", "nostalgic"), "y2k", "Y2K colour"),
        (("teal", "orange", "movie", "film", "cinematic"), "cinematic", "film grade"),
        (("sad", "melancholy", "depressing", "rain"), "sad", "sad grade"),
        (("dream", "dreamy", "ethereal"), "dream", "dreamy grade"),
    ]
    for words, grade, label in looks:
        if _has(text, words):
            d.look.grade = grade
            notes.append(label)
            break
    if _has(text, ("glow", "bloom", "glowy", "dreamy")):
        d.look.glow = max(d.look.glow, 0.5)
        notes.append("glow")
    if _has(text, ("trails", "ghost", "ghosting", "echo", "afterimage")):
        d.look.trails = max(d.look.trails, 0.5)
        notes.append("ghost trails")
    if _has(text, ("letterbox", "black bars", "cinematic bars", "widescreen")):
        d.look.letterbox = True
        notes.append("letterbox bars")
    if _has(text, ("vertical", "tiktok", "shorts", "reels", "portrait", "phone")):
        d.aspect = "9:16"
        notes.append("vertical 9:16")
    elif _has(text, ("square", "instagram post")):
        d.aspect = "1:1"
        notes.append("square 1:1")

    # Timed moments: "freeze at 1:05", "flash at 0:42", "drop hits at 1:10"
    for clause in clauses:
        if len(_TIME.findall(clause)) > 1:
            continue  # "from 2:10 to 2:30 ...": a timed section (handled below), not two single effects
        for m in _TIME.finditer(clause):
            t = int(m.group(1)) * 60 + int(m.group(2))
            actions = []
            for word, action in (("freeze", "freeze"), ("flash", "flash"), ("slow", "slowmo"), ("zoom", "zoom"),
                                 ("shake", "shake"), ("glitch", "rgb"), ("reverse", "reverse"),
                                 ("strobe", "invert"), ("white", "fade_white"), ("black", "fade_black"),
                                 ("fade", "fade_black")):
                if word in clause:
                    actions.append(action)
            if "drop" in clause and not actions:
                continue  # handled below as a drop position, with the full drop treatment
            if not actions and _has(clause, ("quick", "fast", "rapid", "flurry", "barrage", "crazy", "hype",
                                             "aggressive", "hard", "slow", "calm", "chill", "emotional", "sad",
                                             "soft", "punch", "punches", "punching", "fight", "fighting", "kick",
                                             "kicks", "attack", "attacks", "explosion", "explosions", "blast",
                                             "action", "combo", "hits")):
                continue  # a timed section (handled below), not a single effect
            actions = actions or ["flash", "zoom", "shake"]
            for a in actions:
                d.moments.append(Moment(time=float(t), action=a,
                                        duration=1.0 if a in ("freeze", "slowmo", "shake") else 0.0))
            notes.append(f"{m.group(0)}: " + ", ".join(actions))

    # Repeats: "no repeats", "don't repeat scenes", "never reuse clips" / "repeat the intro shot"
    if re.search(r"\b(no|don'?t|do not|never|without|avoid|stop)\b[\w\s]{0,20}\b(repeat|repeats|repeating|reuse|reusing|duplicate|duplicates|same (clip|scene|shot)s?)\b", text):
        d.repeats = "never"
        notes.append("no repeated scenes")
    elif re.search(r"\b(repeat|reuse|callback)\b", text):
        d.repeats = "allowed"
        notes.append("repeats allowed")

    # Timed instructions: "at 1:34 quick edits of him throwing punches",
    # "from 2:10 to 2:30 slow motion", "at 0:50 calm for 8 seconds".
    for clause in clauses:
        times = list(_TIME.finditer(clause))
        if not times or "drop" in clause:
            continue
        quick = _has(clause, ("quick", "fast", "rapid", "quick cuts", "quick edits", "flurry", "barrage",
                              "crazy", "hype", "aggressive", "hard"))
        calm = _has(clause, ("slow", "calm", "chill", "emotional", "sad", "soft", "long shots"))
        action = _has(clause, ("punch", "punches", "punching", "fight", "fighting", "kick", "kicks", "attack",
                               "attacks", "explosion", "explosions", "blast", "action", "combo", "hits"))
        if not (quick or calm or action):
            continue
        t0 = float(int(times[0].group(1)) * 60 + int(times[0].group(2)))
        if len(times) > 1:
            t1 = float(int(times[1].group(1)) * 60 + int(times[1].group(2)))
        else:
            m = re.search(r"for (\d+(?:\.\d+)?) ?(s|sec|secs|seconds|bars?)", clause)
            if m:
                t1 = t0 + float(m.group(1)) * (2.0 if m.group(2).startswith("bar") else 1.0)
            else:
                t1 = t0 + 8.0
        if t1 <= t0:
            continue
        fx = {}
        if quick or action:
            fx = {"flash": 0.6, "zoom": 0.6, "shake": 0.7 if action else 0.4, "rgb": 0.2}
        if calm:
            fx = {"slowmo": 0.6}
        d.sections.append(SectionDirection(
            start=t0, end=t1, cut_every=0.5 if quick else (4.0 if calm else 1.0),
            energy=1.0 if (quick or action) else (0.15 if calm else None), effects=fx,
            note=clause.strip()))
        notes.append(f"{times[0].group(0)}–{_mmss(t1)}: " + ("quick cuts" if quick else "calm, slow" if calm else "action")
                     + (", high-action shots" if action else ""))
    d.sections.sort(key=lambda s: s.start)

    # "the drop is at 1:05" / "drop hits at 1:05": correct the drop position
    for clause in clauses:
        if "drop" in clause:
            for m in _TIME.finditer(clause):
                t = float(int(m.group(1)) * 60 + int(m.group(2)))
                d.drops = sorted(set((d.drops or []) + [t]))

    d.notes = notes or ["(no keywords recognised: using the style as-is)"]
    return d


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"
