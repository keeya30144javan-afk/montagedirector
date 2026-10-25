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

    # Cut rate in beats: "cut every 2 beats", "every other beat", "every bar", "on every beat"
    for clause in clauses:
        rate = _cut_rate(clause)
        if rate is None or _TIME.search(clause):
            continue
        labels = {lab for w, lab in _SECTION_WORDS.items() if re.search(r"\b" + re.escape(w) + r"\b", clause)}
        for lab in (labels or SECTION_LABELS):
            d.style.cut_every[lab] = rate
        notes.append(("/".join(sorted(labels)) + ": " if labels else "") + f"a cut every {_rate_label(rate)}")

    # Repeats: "no repeats", "don't repeat scenes", "never reuse clips" / "repeat the intro shot"
    if re.search(r"\b(no|don'?t|do not|never|without|avoid|stop)\b[\w\s]{0,20}\b(repeat|repeats|repeating|reuse|reusing|duplicate|duplicates|same (clip|scene|shot)s?)\b", text):
        d.repeats = "never"
        notes.append("no repeated scenes")
    elif re.search(r"\b(repeat|reuse|callback)\b", text):
        d.repeats = "allowed"
        notes.append("repeats allowed")

    # The prompt as a timeline: "at 1:36 quicker cuts with punches, around 2:50 when the solo
    # starts slower cuts, at the end when the note rings a held shot of him posing".
    read_timeline(prompt, d, notes)

    d.notes = notes or ["(no keywords recognised: using the style as-is)"]
    return d


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


# --------------------------------------------------------------------------- #
# Timeline reader: timed and cue-based instructions
# --------------------------------------------------------------------------- #
# Musical moments the free prompt can point at (resolved against what the app heard).
CUES = [  # most specific first
    ("hits", r"hard (drums|hits|stabs)|\b1\s*,?\s*2\s*,?\s*3\s*,?\s*4\b|\bstabs\b|\baccents\b|big hits|four hits"),
    ("solo", r"guitar solo|\bsolo\b|\blead (guitar|melody)\b"),
    ("drums_faster", r"drums?\b.{0,30}\b(faster|frantic|speed|speeding|busier|intense|crazy)|\bfrantic\b|double time drums"),
    ("ringout", r"note (ends|rings|fades)|rings? out|\bringing\b|\brings\b|last note|end of the song|\bthe ending\b|\bat the end\b"),
    ("drop", r"bass (drops|comes in|hits|kicks in)|beat (drops|comes in|kicks in)|\bthe drop\b|\bdrops\b|\bkicks in\b"),
    ("stop", r"\b(stops|silence|goes quiet|cuts out|break)\b"),
    ("fill", r"drum fills?|\bfills?\b"),
    ("intro", r"\bbeginning\b|\bstart of the song\b|\bopening\b|\bintro\b|cymbals"),
]
_PACE_QUICK = ("quick", "quicker", "quickly", "fast", "faster", "rapid", "rapidly", "crazy", "hype", "aggressive",
               "intense", "go off", "go hard", "picks back up", "flurry", "barrage")
_PACE_SLOW = ("slow", "slower", "calm", "calmer", "chill", "emotional", "soft", "long shots", "longer shots", "relaxed")
_PACE_NORMAL = ("flow", "groove", "melody", "back to normal", "normal", "regular")
_HOLD = ("still shot", "one shot", "single shot", "hold", "held shot", "panning shot", "pan shot", "pose", "posing",
         "one long shot", "stay on")
_SYNC = ("coinciding", "coincide", "on the hits", "on each hit", "every hit", "each hit", "with the hits", "on the drums",
         "with the drums", "on the kicks", "on the snare", "hit sync", "line up with", "match the hits", "with those")
_ACTION = ("punch", "punches", "punching", "fight", "fighting", "fights", "kick", "kicking", "attack", "attacks",
           "explosion", "explosions", "blast", "action", "combo", "beatdown", "battle")
_EFFECT_ACTIONS = (("freeze", "freeze"), ("flash", "flash"), ("zoom", "zoom"), ("shake", "shake"), ("glitch", "rgb"),
                   ("reverse", "reverse"), ("strobe", "invert"), ("slow mo", "slowmo"), ("slow-mo", "slowmo"),
                   ("slowmo", "slowmo"), ("slow motion", "slowmo"), ("fade to white", "fade_white"),
                   ("fade to black", "fade_black"), ("fade out", "fade_black"))
_TIME_ANY = re.compile(r"\b(\d{1,2}):(\d{2})(?:\.(\d))?\b")


def _cue_of(text: str) -> str | None:
    for kind, pattern in CUES:
        if re.search(pattern, text):
            return kind
    return None


def _chunks(prompt: str) -> list[str]:
    """Split a prompt into instructions: at sentence ends, at every time, and where the
    musical cue changes ("...a still shot during the cymbals, and as soon as the bass drops...")."""
    out = []
    for sentence in re.split(r"[.!?;\n]+", prompt.lower()):
        if not sentence.strip():
            continue
        starts = [m.start() for m in _TIME_ANY.finditer(sentence)]
        pieces = []
        if starts:
            # split before each time's lead-in ("..., freeze at 0:12", "... and then around 3:09")
            bounds = [0]
            for st in starts[1:]:
                lead = max(sentence.rfind(",", 0, st), sentence.rfind(" and ", 0, st), sentence.rfind(" then ", 0, st))
                bounds.append(lead if lead > bounds[-1] and lead > starts[len(bounds) - 1] else st)
            bounds.append(len(sentence))
            pieces = [sentence[a:b] for a, b in zip(bounds[:-1], bounds[1:])]
        else:
            pieces = [sentence]
        for piece in pieces:
            # split time-free pieces where a different cue starts
            tm = _TIME_ANY.search(piece)
            if tm:
                # Clauses before the time's own phrase are whole-edit instructions
                # ("slow intro, vertical, freeze at 0:12" -> "freeze at 0:12").
                lead = piece.rfind(",", 0, tm.start())
                out.append(piece[lead + 1:] if lead > 0 else piece)
                continue
            marks = []
            for kind, pattern in CUES:
                for m in re.finditer(pattern, piece):
                    marks.append((m.start(), kind))
            marks.sort()
            cut_at, last_kind = [], None
            for pos, kind in marks:
                if last_kind is not None and kind != last_kind:
                    lead = max(piece.rfind(" and ", 0, pos), piece.rfind(" as soon as", 0, pos),
                               piece.rfind(" when ", 0, pos), piece.rfind(" then ", 0, pos), piece.rfind(",", 0, pos))
                    cut_at.append(lead if lead > (cut_at[-1] if cut_at else 0) else pos)
                last_kind = kind
            bounds = [0] + cut_at + [len(piece)]
            out += [piece[a:b] for a, b in zip(bounds[:-1], bounds[1:]) if piece[a:b].strip()]
    return [c.strip(" ,") for c in out if c.strip(" ,")]


def read_timeline(prompt: str, d: Direction, notes: list[str], window: tuple[float, float] | None = None) -> None:
    """Turn timed / cue-based instructions into sections, moments and drop positions.

    `window` (start, end) applies the whole prompt to one selected region of the song.
    """
    for chunk in _chunks(prompt):
        times = [int(m.group(1)) * 60 + int(m.group(2)) + (int(m.group(3)) / 10 if m.group(3) else 0)
                 for m in _TIME_ANY.finditer(chunk)]
        cue = _cue_of(chunk)
        quick, slow = _has(chunk, _PACE_QUICK), _has(chunk, _PACE_SLOW)
        normal = _has(chunk, _PACE_NORMAL)
        hold = any(w in chunk for w in _HOLD)
        sync = any(w in chunk for w in _SYNC) or cue == "hits"
        action = _has(chunk, _ACTION)
        effects = [a for w, a in _EFFECT_ACTIONS if w in chunk and not _negated(chunk, w)]
        if window is None and not times and cue is None:
            continue  # a whole-edit instruction, handled by the keyword reader
        start = float(times[0]) if times else (window[0] if window and cue is None else -1.0)

        # "the drop is at 0:51": a drop position (and nothing else unless they ask for more)
        if cue == "drop" and times and not (quick or slow or hold or action or normal):
            d.drops = sorted(set((d.drops or []) + [float(times[0])]))
            notes.append(f"⏱ drop at {_mmss(times[0])}")
            continue
        # "freeze at 1:05", "flash at 0:42": single moments
        if times and effects and not (quick or slow or hold or sync or action or normal or cue):
            for a in effects:
                d.moments.append(Moment(time=float(times[0]), action=a,
                                        duration=1.0 if a in ("freeze", "slowmo", "shake") else 0.0))
            notes.append(f"⏱ {_mmss(times[0])}: " + ", ".join(effects))
            continue
        if not (quick or slow or normal or hold or sync or action or effects or cue):
            continue

        sd = SectionDirection(start=start, end=float(times[1]) if len(times) > 1 else -1.0, cue=cue,
                              note=chunk.strip())
        if re.search(r"\bpan(ning|s)?\b", chunk):
            sd.camera = "pan"
        elif re.search(r"zoom(ing)? in(to)?\b|zoom(ing)? shot|push(ing)? in", chunk):
            sd.camera = "zoom"
        elif re.search(r"\bshaky\b|handheld|camera shake", chunk):
            sd.camera = "shaky"
        if window:
            sd.start, sd.end, sd.fixed = window[0], window[1], True
        if hold:
            sd.hold, sd.cut_every = True, 8.0
            sd.still = "still" in chunk or (cue == "intro" and "pan" not in chunk)
            sd.energy = 0.0 if sd.still else 0.4
        elif sync:
            sd.sync_hits, sd.cut_every, sd.energy = True, 1.0, 1.0 if action or cue == "hits" else 0.8
        elif quick:
            sd.cut_every, sd.energy = (0.5 if _has(chunk, ("frantic", "crazy", "rapid", "rapidly", "flurry")) else 1.0), 1.0
        elif slow:
            sd.cut_every, sd.energy = 4.0, 0.2
        elif normal:
            sd.cut_every, sd.energy = 2.0, None   # a steady, medium groove
        elif action or cue == "drop":
            sd.cut_every, sd.energy = 1.0, 1.0
        if action and not hold:
            sd.energy = 1.0
        fx: dict[str, float] = {}
        if (quick or action or sync) and not hold:
            fx = {"flash": 0.6, "zoom": 0.5, "shake": 0.6 if action else 0.3}
        if slow:
            fx = {"slowmo": 0.5}
        if hold or normal:
            fx = {}
        for a in effects:
            if a in EFFECTS:
                fx[a] = 0.9
            elif a == "fade_black" and hold:
                d.moments.append(Moment(time=-1.0, action="fade_black", duration=1.5))  # end of the song
        if any(w in chunk for w in ("no effects", "no flash", "clean")):
            fx = {}
        sd.effects = fx
        d.sections.append(sd)
        what = ("hold one still shot" if sd.hold and sd.still else "hold one shot" if sd.hold else
                "cuts on each hit" if sd.sync_hits else
                "quick cuts" if quick else "slower cuts" if slow else "back to the flow" if normal else "action")
        where = (f"{_mmss(window[0])}–{_mmss(window[1])}" if window else
                 (_mmss(start) if start >= 0 else "") + (f" ({_CUE_NAMES.get(cue, cue)})" if cue else ""))
        notes.append(f"⏱ {where.strip()}: {what}" + (", action shots" if action and not sd.hold else ""))


_CUE_NAMES = {"hits": "hard hits", "solo": "solo", "drums_faster": "drums get faster", "ringout": "last note rings",
              "drop": "bass drops", "stop": "stop", "fill": "drum fill", "intro": "intro"}


def region_direction(text: str, start: float, end: float) -> tuple[SectionDirection | None, list[str]]:
    """A refinement for one selected region of the song ("quicker cuts with punches")."""
    probe = Direction(style=copy.deepcopy(STYLES[DEFAULT_STYLE]), look=Look())
    notes: list[str] = []
    read_timeline(text, probe, notes, window=(start, end))
    if not probe.sections:
        return None, notes
    sd = probe.sections[0]
    sd.note = text.strip()
    return sd, notes
