"""Step 4: turn the instructions into a Direction (whole edit first, then parts of the song, then timed
sections, so the more specific instruction always wins)."""

from __future__ import annotations

import copy
import re

from ..direction import (
    CUT_CHOICES,
    DEFAULT_STYLE,
    EFFECTS,
    SECTION_LABELS,
    STYLES,
    Direction,
    Moment,
    SectionDirection,
    _fx,
    _mmss,
    _rate_label,
)
from ..plan import Look
from .instruction import Instruction, read_instruction
from .normalise import TIME_RE, T, _secs, normalise
from .splitting import _split
from .vocabulary import CUE_NAMES, FILLER, GLOBAL_HINTS, LOOKS


def _step(value: float, steps: int) -> float:
    i = min(range(len(CUT_CHOICES)), key=lambda k: abs(CUT_CHOICES[k] - value))
    return CUT_CHOICES[max(0, min(len(CUT_CHOICES) - 1, i + steps))]


def _shift_pace(cut_every: dict, labels, pace: float) -> None:
    """Faster (+) or slower (-) cutting. 0.5 = a touch, 1 = clearly, 2 = a lot."""
    steps = -1 if pace > 0 else 1
    if abs(pace) >= 1.5:
        steps *= 2
    for lab in labels:
        cur = cut_every[lab]
        if abs(pace) < 1.0:  # a touch: only move the parts that aren't already there
            if (pace > 0 and cur <= 1.0) or (pace < 0 and cur >= 4.0):
                continue
        cut_every[lab] = _step(cur, steps) if not (pace > 0 and cur <= 0.5 and steps == -2) else 0.5


def _apply_amount(fx: dict, eff: str, amount: str) -> None:
    cur = fx.get(eff, 0.0)
    fx[eff] = float(min(1.0, max(0.0, {"none": 0.0, "less": cur * 0.4, "light": min(max(cur, 0.15), 0.25),
                                       "more": max(cur + 0.4, 0.75), "add": max(cur + 0.3, 0.5)}[amount])))


EFFECT_LABEL = {"flash": "flashes", "zoom": "zoom punches", "shake": "shake", "rgb": "glitch / RGB split",
                "slowmo": "slow motion", "reverse": "reversed clips", "invert": "negative strobes",
                "stutter": "stutter cuts", "flicker": "black flickers"}
AMOUNT_WORD = {"none": "no", "less": "fewer", "light": "a few", "more": "more", "add": ""}
DRUM_WORD = {"snare": "snare", "kick": "kick", "hits": "drum hit", "big": "big hit", "bar": "bar", "beat": "beat"}


TRANSITION_NAMES = {"crossfade": "crossfades", "dissolve": "dissolves", "whip": "whip pans", "zoom": "zoom-throughs",
                    "white": "dips to white", "black": "dips to black", "circle": "circle wipes", "wipe": "wipes",
                    "pixel": "pixel glitches", "quick": "quick blends", "blackcut": "cuts through black"}
MOMENT_WORD = {"fade_black": "fade to black", "fade_white": "fade to white", "freeze": "freeze frame",
               "fade_in": "fade in from black"}


def _effects_note(effects: dict[str, str]) -> list[str]:
    out = []
    for eff, amt in effects.items():
        name = EFFECT_LABEL.get(eff, eff)
        out.append((AMOUNT_WORD[amt] + " " + name).strip())
    return out


def _pace_note(pace: float) -> str:
    if pace == 0:
        return "back to the normal flow"
    word = "faster" if pace > 0 else "slower"
    return {0.5: f"a little {word}", 1.0: f"{word} cuts", 2.0: f"much {word} cuts"}.get(min(abs(pace), 2.0) if abs(pace) in (0.5, 1.0, 2.0) else 1.0, f"{word} cuts")


def read_prompt(prompt: str, base: Direction) -> Direction:
    """Adjust a style-based Direction from a plain-English prompt."""
    d = copy.deepcopy(base)
    d.unclear = []
    if not prompt.strip():
        return d
    prompt, texts = extract_texts(prompt)
    for t in texts:
        d.texts.append(t)
        where = {-2.0: "at the start", -1.0: "at the end"}.get(t["time"], f"at {_mmss(t['time'])}")
        notes_early = f"text “{t['text']}” {where} ({t['style']})"
        d.notes.append(notes_early)
    text = normalise(prompt)
    notes: list[str] = list(d.notes[-len(texts):]) if texts else []
    every = list(SECTION_LABELS)

    # Whole-edit flavour words first (genre / era / look / shape).
    for pattern, hint, note in GLOBAL_HINTS:
        if re.search(pattern, text):
            _apply_hint(d, hint)
            notes.append(note)
    for pattern, grade, note in LOOKS:
        if re.search(pattern, text):
            d.look.grade = grade
            notes.append(note)
            break
    if re.search(r"\bglow(?:y|ing)?\b|\bbloom\b", text) and not re.search(r"\bglowing (?:shots|aura)", text):
        d.look.glow = max(d.look.glow, 0.5)
        notes.append("glow")
    if re.search(r"\btrails?\b|\bghost(?:ing)?\b|\becho\b|\bafterimages?\b|\bmotion blur\b", text):
        d.look.trails = max(d.look.trails, 0.5)
        notes.append("ghost trails")
    if re.search(r"\bletterbox(?:ed)?\b|\bblack bars\b|\bcinematic bars\b|\bwidescreen\b", text):
        d.look.letterbox = True
        notes.append("letterbox bars")
    if re.search(r"\bvertical\b|\btiktok\b|\bshorts\b|\breels?\b|\bportrait\b|\bphone\b|\b9:16\b", text):
        d.aspect = "9:16"
        notes.append("vertical 9:16")
    elif re.search(r"\bsquare\b|\binstagram post\b|\b1:1\b", text):
        d.aspect = "1:1"
        notes.append("square 1:1")
    text = re.sub(r"\bre[- ](use|using|used|peat|peats|peating)\b", r"re\1", text)  # "re use scenes"
    if re.search(r"\b(?:no|don't|never|without|avoid|stop|zero)\b[\w\s']{0,20}\b(?:repeat|repeats|repeated|repeating|reuse|reused|"
                 r"reusing|duplicate|duplicates|same (?:clip|scene|shot)s?)\b|\bunique (?:clips|scenes|shots)\b|"
                 r"\b(?:each|every) (?:clip|scene|shot) (?:only )?once\b|\b(?:clips|scenes|shots) only once\b",
                 text):
        d.repeats = "never"
        notes.append("no repeated scenes")
    elif re.search(r"\b(?:repeat|reuse|callback)s?\b", text):
        d.repeats = "allowed"
        notes.append("repeats allowed")
    m = re.search(rf"\b(?:the )?(?:beat |bass )?drops? (?:is |hits |happens |comes |comes in |kicks in )?(?:at|around|about) ({T})(?![~\d])",
                  text)
    if not m:
        m = re.search(rf"\b(?:at|around) ({T}),? (?:is )?(?:where )?(?:the|when the) (?:bass |beat )?drop(?:s)?\b(?! [a-z]+ (?:quick|fast|crazy))",
                      text)
    if m:
        t = _secs(m.group(1))
        d.drops = sorted(set((d.drops or []) + [t]))
        notes.append(f"⏱ drop at {_mmss(t)}")

    instructions = [read_instruction(c) for c in _split(text)]
    # 1. whole edit (pace words across the prompt count once: "not too fast, keep it chill")
    paces = [i.pace for i in instructions if i.scope == "all" and i.pace]
    if paces:
        up = max([p for p in paces if p > 0], default=0.0)
        down = min([p for p in paces if p < 0], default=0.0)
        overall = up + (0.5 * down if up and down > -1 else down)  # "kinda fast but not too crazy": still faster
        first = True
        for ins in instructions:
            if ins.scope == "all" and ins.pace:
                ins.pace = (overall or None) if first else None
                first = False
    for ins in instructions:
        if ins.scope != "all":
            continue
        _apply_scoped(d, ins, every, notes, "")
    # 2. parts of the song ("calm intro, crazy chorus")
    for ins in instructions:
        if ins.scope != "label":
            continue
        _apply_scoped(d, ins, sorted(ins.labels), notes, "/".join(sorted(ins.labels)) + ": ")
    # 3. moments and timed sections
    for ins in instructions:
        if ins.scope != "section":
            continue
        _add_section(d, ins, notes, window=None)
    for ins in instructions:
        if not ins.understood and _meaningful(ins.text):
            d.unclear.append(ins.text.strip())
    seen = set()
    notes = [n for n in notes if n and not (n in seen or seen.add(n))]
    d.notes = notes or ["(using the style as it is)"]
    return d


QUOTE_RX = re.compile(r'["\u201c\u201d]([^"\u201c\u201d]{1,80})["\u201c\u201d]')


def extract_texts(prompt: str) -> tuple[str, list[dict]]:
    """Quoted words are text to show: 'title "VEGETA - AWAKE"', 'credits "edit by Keeya" at the end',
    'at 1:20 text "Final Flash"'. Returns the prompt without them, and the text cards."""
    cards = []
    out = prompt
    for m in list(QUOTE_RX.finditer(prompt)):
        before = prompt[max(0, m.start() - 70):m.start()].lower()
        after = prompt[m.end():m.end() + 50].lower()
        around = before + " " + after
        style = ("credits" if re.search(r"credit|edited by|edit by|made by|by me|outro|thanks for watching", around)
                 else "lower" if re.search(r"name ?tag|lower third|name (?:on|at) the bottom|label", around)
                 else "caption" if re.search(r"caption|subtitle|lyric|small text|at the bottom", around)
                 else "title")
        after_t = re.sub(r"\bfor (?:about )?\d+(?:\.\d+)? ?(?:s|sec|secs|seconds)\b", " ", after)
        tm = TIME_RE.search(normalise(after_t)[:30]) or None
        tb = list(TIME_RE.finditer(normalise(before)))
        if tb and len(normalise(before)) - tb[-1].end() < 40:
            at = _secs(tb[-1].group(1))
        elif tm:
            at = _secs(tm.group(1))
        elif re.search(r"\b(?:end|ending|outro|last|finish|credits)\b", around):
            at = -1.0
        else:
            at = -2.0  # the start
        dm = re.search(r"\bfor (\d+(?:\.\d+)?) ?(?:s|sec|secs|seconds)\b", after)
        dur = float(dm.group(1)) if dm else {"title": 3.0, "credits": 4.0, "caption": 2.5, "lower": 3.0}[style]
        cards.append({"text": m.group(1).strip(), "time": at, "duration": dur, "style": style})
        # take the quote and the words that introduced it out of the prompt
        lead = re.search(r"(?:(?:add|put|show|with|and|then|a|an|the|big|small)\s+)*(?:title(?: card)?|text|caption|"
                         r"subtitle|credits?|name ?tag|words?|lower third|card)?\s*(?:saying|that says|reading|of|:|=)?\s*$",
                         prompt[:m.start()], re.I)
        a = lead.start() if lead else m.start()
        tail = re.match(r"(?:\s*,?\s*(?:(?:at|in|on|during)\s+(?:the\s+)?(?:very\s+)?(?:end|ending|start|beginning|outro|"
                        r"intro|opening)(?: of the (?:song|edit|video))?|at\s+\d{1,2}:\d{2}(?:\.\d)?|for (?:about )?"
                        r"\d+(?:\.\d+)? ?(?:s|sec|secs|seconds)))+", prompt[m.end():], re.I)
        b = m.end() + (tail.end() if tail else 0)
        lead_time = re.search(r"\b(?:at|around)\s+\d{1,2}:\d{2}(?:\.\d)?\s*,?\s*$", prompt[:a], re.I)
        if lead_time:
            a = lead_time.start()
        out = out.replace(prompt[a:b], " ", 1)
    return out, cards


def _meaningful(text: str) -> bool:
    rest = FILLER.sub(" ", re.sub(r"[^a-z' ]", " ", text))
    words = [w for w in rest.split() if len(w) > 2]
    if not words:
        return False
    hinted = any(re.search(p, text) for p, _, _ in GLOBAL_HINTS) or any(re.search(p, text) for p, _, _ in LOOKS)
    if re.search(r"\b(?:it'?s|its|it is|that'?s|this is|is) (?:hard|difficult|impossible|confusing|annoying|boring|"
                 r"too much)\b|\bcan'?t (?:follow|see|tell|keep up)\b", text):
        return False  # a complaint about the edit, read with the rest of the sentence
    shape = re.search(r"vertical|tiktok|shorts|reels|square|portrait|repeat|reuse|letterbox|glow|trail|ghost|drop (?:is|at)|"
                      r"\bonce\b|"
                      r"length of the song|whole song|full song|entire song|all of the song", text)
    return not (hinted or shape)


def _apply_hint(d: Direction, hint: str) -> None:
    st, every = d.style, list(SECTION_LABELS)
    if hint == "phonk":
        for lab in ("build", "drop"):
            _apply_amount(st.effects[lab], "shake", "more")
            _apply_amount(st.effects[lab], "zoom", "more")
        d.drum_fx.append({"effect": "zoom", "drum": "kick", "label": "drop"})
        d.sync.setdefault("drop", "kick")
        st.energy_bias = min(0.5, st.energy_bias + 0.15)
    elif hint == "amv":
        st.white_into_drop = True
        st.blur_into_sections = True
        st.flash_top = max(st.flash_top, 0.1)
        d.look.glow = max(d.look.glow, 0.4)
        d.look.trails = max(d.look.trails, 0.25)
    elif hint == "dark_mv":  # a dark rap/horror music video: hard cuts that snap through black, flickering light
        d.look.grade, d.look.letterbox, d.look.glow, d.look.trails = "dark", True, 0.0, 0.0
        d.transitions.clear()
        d.transitions["all"] = "blackcut"
        st.white_into_drop = st.blur_into_sections = False
        st.flash_top *= 0.3
        st.transitions = {}
        for lab in every:
            st.effects[lab]["flicker"] = 0.8 if lab == "intro" else 0.35
            st.effects[lab]["flash"] *= 0.3
    elif hint == "trailer":
        d.look.letterbox = True
        st.freeze_before_drop = True
    elif hint == "simple":
        for lab in every:
            st.effects[lab] = {k: v * 0.4 for k, v in st.effects[lab].items()}
        st.flash_top *= 0.5
        d.look.glow *= 0.5
        d.look.trails = 0.0
    elif hint == "smooth":
        st.blur_into_sections = True
        st.flash_top *= 0.5
        for lab in every:
            st.effects[lab]["shake"] = st.effects[lab].get("shake", 0) * 0.5
    elif hint == "velocity":
        for lab in ("build", "drop"):
            if lab not in d.velocity:
                d.velocity.append(lab)
    elif hint == "transitions":
        st.blur_into_sections = True
        st.white_into_drop = True
        d.transitions.setdefault("@sections", "mix")
    elif hint == "hard_cuts":
        st.blur_into_sections = False
        st.white_into_drop = False
        d.transitions.clear()
    elif hint == "epic":
        st.freeze_before_drop = True
        st.white_into_drop = True
    elif hint == "opening":
        st.cut_on_fills = True
        st.freeze_before_drop = True
        st.flash_top = max(st.flash_top, 0.08)
    elif hint == "freeze_drop":
        st.freeze_before_drop = True
    elif hint == "white_drop":
        st.white_into_drop = True


def _apply_scoped(d: Direction, ins: Instruction, labels: list[str], notes: list[str], prefix: str) -> None:
    """A whole-edit or a song-part instruction: changes the style itself."""
    st = d.style
    said = []
    if ins.cut_rate is not None:
        for lab in labels:
            st.cut_every[lab] = ins.cut_rate
            if lab not in d.rate_locked:
                d.rate_locked.append(lab)
        said.append(f"a cut every {_rate_label(ins.cut_rate)}")
    elif ins.pace is not None:
        if ins.pace == 0:
            base = STYLES.get(st.id, STYLES[DEFAULT_STYLE])
            for lab in labels:
                st.cut_every[lab] = base.cut_every[lab]
        else:
            _shift_pace(st.cut_every, labels, ins.pace)
            if prefix == "":
                st.energy_bias = float(max(-0.5, min(0.5, st.energy_bias + 0.15 * ins.pace)))
            for lab in labels:
                if ins.pace > 0:
                    for e in ("zoom", "shake", "flash"):
                        st.effects[lab][e] = min(1.0, st.effects[lab].get(e, 0) + (0.15 if prefix == "" else 0.3) * min(ins.pace, 1))
                else:
                    st.effects[lab]["slowmo"] = min(1.0, st.effects[lab].get("slowmo", 0) + 0.3 * min(-ins.pace, 1))
                    for e in ("shake", "zoom"):
                        st.effects[lab][e] = max(0.0, st.effects[lab].get(e, 0) - 0.3 * min(-ins.pace, 1))
        said.append(_pace_note(ins.pace))
    if ins.all_effects:
        if ins.all_effects == "none":
            for lab in labels:
                st.effects[lab] = _fx()
            if prefix == "":
                st.flash_top = 0.0
                st.white_into_drop = st.blur_into_sections = st.freeze_before_drop = False
                d.look = Look()
        else:
            k = 0.45 if ins.all_effects == "less" else 1.5
            for lab in labels:
                st.effects[lab] = {e: min(1.0, v * k) for e, v in st.effects[lab].items()}
            if prefix == "":
                st.flash_top = min(0.3, st.flash_top * k)
                d.look.glow = min(1.0, d.look.glow * (k if k < 1 else 1.2))
        said.append({"none": "no effects", "less": "toned-down effects", "more": "more effects"}[ins.all_effects])
    for eff, amt in ins.effects.items():
        for lab in labels:
            _apply_amount(st.effects[lab], eff, amt)
        if eff == "flash" and prefix == "":
            st.flash_top = {"none": 0.0, "less": st.flash_top * 0.4, "light": min(st.flash_top, 0.03) or 0.03,
                            "more": min(0.3, st.flash_top * 2 + 0.04), "add": max(st.flash_top, 0.06)}[amt]
            if amt == "none":
                st.white_into_drop = False
    said += _effects_note(ins.effects)
    if ins.prefer:
        for lab in (labels if prefix else ["all"]):
            d.prefer.setdefault(lab, {}).update(ins.prefer)
        names = list(ins.prefer_names)
        if ins.prefer.get("_query") and (names or ins.understood):
            names.append("shots of " + " / ".join(ins.prefer["_query"]))
        said.append(", ".join(n for n in names if n))
    if ins.sync:
        for lab in (labels if prefix else ["all"]):
            d.sync[lab] = ins.sync
        said.append(f"cut on the {DRUM_WORD[ins.sync]}s")
    for eff, drum in ins.drum_fx.items():
        for lab in (labels if prefix else [None]):
            d.drum_fx.append({"effect": eff, "drum": drum, "label": lab})
        said.append(f"{EFFECT_LABEL.get(eff, eff)} on every {DRUM_WORD.get(drum, drum)}")
    if ins.ramp:
        for lab in (labels if prefix else ["build"]):
            if lab not in d.ramp:
                d.ramp.append(lab)
        said.append("cuts speed up toward the drop" if not prefix else "cuts speed up")
    if ins.transition == "none":
        if prefix:
            for lab in labels:
                d.transitions.pop(lab, None)
        else:
            d.transitions.clear()
        said.append("no transitions (hard cuts)")
    elif ins.transition:
        for lab in (labels if prefix else ["all"]):
            d.transitions[lab] = ins.transition
        said.append(f"{TRANSITION_NAMES[ins.transition]} between cuts")
    if ins.velocity:
        for lab in (labels if prefix else ["build", "drop"]):
            if lab not in d.velocity:
                d.velocity.append(lab)
        said.append("velocity edit")
    if ins.camera and prefix:
        for lab in labels:
            d.prefer.setdefault(lab, {})["camera_" + ins.camera] = 1.0
        said.append(f"{ins.camera} shots")
    if ins.moments and not prefix:
        # "end it on a freeze frame" without a place ("fade in" = at the start)
        for a in ins.moments:
            d.moments.append(Moment(time=-2.0 if a == "fade_in" else -1.0, action=a,
                                    duration=1.5 if a == "fade_black" else 1.0))
        said.append("ending: " + ", ".join(MOMENT_WORD.get(a, a) for a in ins.moments))
    if said:
        notes.append(prefix + ", ".join(s for s in said if s))


def _add_section(d: Direction, ins: Instruction, notes: list[str], window: tuple[float, float] | None) -> None:
    times = ins.times
    # "the drop is at 0:51" was read as a drop position already
    if ins.cue == "drop" and times and not (ins.understood and (ins.pace is not None or ins.hold or ins.prefer
                                                                  or ins.sync or ins.effects or ins.all_effects)):
        if not ins.moments:
            return
    # single moments: "freeze at 1:05", "flash at 0:42", "fade to black at the end"
    point_fx = {a: 0.0 for a in ins.moments}
    if not window and not ins.span and (times or ins.cue == "end") and len(times) <= 1 and (ins.moments or ins.effects) \
            and not (ins.shapes_cuts or (ins.cue not in (None, "end", "ringout") and not times)):
        acts = list(point_fx) + [e for e, amt in ins.effects.items() if amt != "none"]
        t = times[0] if times else -1.0
        for a in acts:
            d.moments.append(Moment(time=t, action=a,
                                    duration=1.0 if a in ("freeze", "slowmo", "shake") else (1.5 if a == "fade_black" else 0.0)))
        notes.append(f"⏱ {_mmss(t) if t >= 0 else 'the end'}: " + ", ".join(MOMENT_WORD.get(a, a) for a in acts))
        return
    if ins.span and ins.moments and not (ins.shapes_cuts or ins.effects):
        kind, _, n = ins.span.partition(":")
        if kind == "tail":
            for a in ins.moments:
                d.moments.append(Moment(time=-1.0, action=a, duration=min(float(n), 8.0) if a == "fade_black" else float(n)))
            notes.append(f"⏱ last {n} s: " + ", ".join(MOMENT_WORD.get(a, a) for a in ins.moments))
            return
    if "fade_in" in ins.moments and not times:  # "start with a fade in": the start of the edit
        d.moments.append(Moment(time=-2.0, action="fade_in", duration=1.0))
        notes.append("⏱ the start: fade in from black")
        ins.moments = [a for a in ins.moments if a != "fade_in"]
        if not (ins.moments or ins.shapes_cuts or ins.effects):
            return
    if ins.moments and ins.cue and not times and not (ins.shapes_cuts or ins.effects):
        # "freeze every time the music stops", "flash when the solo starts": moments at a heard moment
        sd = SectionDirection(start=-1.0, end=-1.0, cue=ins.cue, occurrence=ins.occurrence, note=ins.text.strip(),
                              moments=list(ins.moments), only_moments=True)
        d.sections.append(sd)
        notes.append(f"⏱ {_where(ins, None)}: " + ", ".join(MOMENT_WORD.get(a, a) for a in ins.moments))
        return
    if ins.drop_at is not None:
        d.drops = sorted(set((d.drops or []) + [ins.drop_at]))
        notes.append(f"⏱ drop at {_mmss(ins.drop_at)}")
    if not ins.understood:
        return
    sd = SectionDirection(start=times[0] if times else -1.0, end=times[1] if len(times) > 1 else -1.0,
                          cue=None if ins.cue == "end" else ins.cue, note=ins.text.strip(), occurrence=ins.occurrence,
                          span=ins.span, camera=ins.camera)
    if ins.cue == "end":
        sd.cue = "ringout"
    if window:
        sd.start, sd.end, sd.fixed, sd.cue, sd.span = window[0], window[1], True, None, None
    pace = ins.pace
    if ins.hold:
        sd.hold, sd.cut_every = True, 8.0
        sd.still = ins.still
        sd.play_out = ins.play_out
        sd.energy = 0.0 if sd.still else 0.4
    elif ins.sync:
        sd.sync_hits, sd.sync_on, sd.cut_every = True, ins.sync, 1.0
        sd.energy = 1.0 if ins.prefer.get("motion", 0) > 0 or ins.cue == "hits" else 0.8
    elif ins.cut_rate is not None:
        sd.cut_every = ins.cut_rate
        sd.energy = 1.0 if ins.cut_rate <= 1 else (0.3 if ins.cut_rate >= 4 else None)
    elif pace is not None and pace > 0:
        sd.cut_every, sd.energy = (0.5 if pace >= 1.5 or ins.frantic else 1.0), 1.0
    elif pace is not None and pace < 0:
        sd.cut_every, sd.energy = (8.0 if pace <= -1.5 else 2.0 if pace > -1 else 4.0), 0.2
    elif pace == 0:
        sd.cut_every, sd.energy = 2.0, None
    elif ins.prefer.get("motion", 0) > 0:
        sd.cut_every, sd.energy = 1.0, 1.0
    if ins.ramp:
        sd.ramp = True
        sd.cut_every = sd.cut_every or 2.0
    sd.velocity = ins.velocity
    sd.transition = ins.transition if ins.transition != "none" else None
    if ins.prefer.get("motion", 0) > 0 and not sd.hold:
        sd.energy = 1.0
    elif ins.prefer.get("motion", 0) < 0:
        sd.energy = 0.1
    # (a scene left to play out keeps "fight scenes": it picks which part of the footage plays)
    sd.prefer = {k: v for k, v in ins.prefer.items() if k != "motion" or sd.play_out}
    if sd.play_out and ins.prefer.get("motion", 0) > 0:
        sd.energy = 0.9
    if ins.camera:
        sd.prefer["camera_" + ins.camera] = 1.0
    fx: dict[str, float] = {}
    quick = (pace or 0) > 0 or ins.sync or ins.prefer.get("motion", 0) > 0 or ins.ramp
    if quick and not sd.hold:
        fx = {"flash": 0.6, "zoom": 0.5, "shake": 0.6 if ins.prefer.get("motion", 0) > 0 else 0.3}
    if (pace or 0) < 0:
        fx = {"slowmo": 0.5}
    if sd.hold or pace == 0:
        fx = {}
    if ins.all_effects == "none":
        fx = {e: 0.0 for e in EFFECTS}
    elif ins.all_effects == "less":
        fx = {e: v * 0.4 for e, v in fx.items()}
    for eff, amt in ins.effects.items():
        if amt == "none":
            fx[eff] = 0.0
        else:
            _apply_amount(fx, eff, "more" if amt in ("add", "more") else amt)
            fx[eff] = max(fx[eff], 0.9) if amt in ("add", "more") else fx[eff]
    for a in ins.moments:
        if a == "fade_black":
            d.moments.append(Moment(time=-1.0 if sd.hold or ins.cue in ("ringout", "end") else (times[0] if times else -1.0),
                                    action="fade_black", duration=1.5))
        elif a == "freeze" and times:
            d.moments.append(Moment(time=times[0], action="freeze", duration=1.0))
    sd.effects = fx
    sd.drum_fx = dict(ins.drum_fx)
    if not window and not (pace is not None or ins.hold or ins.sync or ins.cut_rate is not None or ins.ramp
                           or ins.velocity or ins.moments):
        # only says what to show / which effects ("feather wings", "flashes"): a layer on top, so it never
        # cuts short a held shot or another part's pacing
        sd.keep_cuts = True
        sd.prefer = dict(ins.prefer)
        sd.energy = None
    if sd.play_out and not ins.effects and not ins.all_effects:
        sd.effects = dict.fromkeys(EFFECTS, 0.0)  # let it play as it is, unless effects were asked for
    d.sections.append(sd)

    what = ("hold one still shot" if sd.hold and sd.still else
            "let one scene play out (no cuts of mine)" if sd.hold and sd.play_out else "hold one shot" if sd.hold else
            f"cut on each {DRUM_WORD.get(sd.sync_on, 'hit')}" if sd.sync_hits else
            "cuts speed up" if sd.ramp else
            f"a cut every {_rate_label(sd.cut_every)}" if ins.cut_rate is not None else
            _pace_note(pace) if pace is not None else "action" if sd.energy == 1.0 else
            {"none": "no effects", "less": "toned-down effects", "more": "more effects"}.get(ins.all_effects or "", ""))
    extras = []
    if ins.prefer_names:
        extras.append(", ".join(ins.prefer_names))
    if ins.prefer.get("_query"):
        extras.append("shots of " + " / ".join(ins.prefer["_query"]))
    extras += [x for x in _effects_note(ins.effects) if x]
    extras += [f"{EFFECT_LABEL.get(e, e)} on every {DRUM_WORD.get(k, k)}" for e, k in ins.drum_fx.items()]
    if sd.velocity:
        extras.append("velocity edit")
    if sd.transition:
        extras.append(TRANSITION_NAMES[sd.transition])
    elif ins.transition == "none":
        extras.append("hard cuts")
    if what == "action" and "action shots" in extras:
        what = ""
    sd.what = ", ".join(x for x in [what] + extras if x)
    notes.append(f"⏱ {_where(ins, window)}: {sd.what}")


def _where(ins: Instruction, window) -> str:
    if window:
        return f"{_mmss(window[0])}–{_mmss(window[1])}"
    parts = []
    if ins.times:
        parts.append(_mmss(ins.times[0]) + (f"–{_mmss(ins.times[1])}" if len(ins.times) > 1 else ""))
    if ins.span:
        kind, _, n = ins.span.partition(":")
        parts.append({"tail": f"last {n} s", "head": f"first {n} s", "half": "second half",
                      "firsthalf": "first half", "middle": "the middle"}[kind])
    if ins.cue:
        if ins.cue.startswith("sec:"):
            lab = ins.cue[4:]
            name = {"drop": "chorus / drop", "breakdown": "bridge / breakdown"}.get(lab, lab)
        else:
            name = CUE_NAMES.get(ins.cue, ins.cue) if ins.cue != "end" else "the end"
        if ins.occurrence == 0:
            name = ("every " + ("drop" if ins.cue == "drop" else name) if ins.cue == "drop" or ins.cue.startswith("sec:")
                    else f"{name} (every time)")
        elif ins.occurrence:
            name = "drop" if ins.cue == "drop" else name
            name = {1: "first ", 2: "second ", 3: "third ", 4: "fourth ", -1: "last "}[ins.occurrence] + name
        parts.append(f"({name})" if ins.times else name)
    return " ".join(parts)


def read_timeline(prompt: str, d: Direction, notes: list[str], window: tuple[float, float] | None = None) -> None:
    """Timed / cue-based instructions only (kept for callers of the old API)."""
    for chunk in _split(normalise(prompt)):
        ins = read_instruction(chunk)
        if window is not None:
            ins.scope = "section"
        if ins.scope == "section":
            _add_section(d, ins, notes, window)


def region_direction(text: str, start: float, end: float) -> tuple[SectionDirection | None, list[str]]:
    """A refinement for one selected region of the song ("quicker cuts with punches")."""
    probe = Direction(style=copy.deepcopy(STYLES[DEFAULT_STYLE]), look=Look())
    notes: list[str] = []
    ins = read_instruction(normalise(text))
    if not ins.understood and re.search(r"\bbuild(?:s|ing)?(?: it)?(?: up)?\b|\bbuild-?up\b|\bbuild the tension\b", text.lower()):
        ins.ramp, ins.understood = True, True  # "this part should build up"
    if not ins.understood:
        return None, notes
    ins.times, ins.cue, ins.span, ins.occurrence = [], None, None, None
    moments, ins.moments = list(ins.moments), []  # freeze / fades happen at the edges of the selection
    others = (ins.pace is not None or ins.cut_rate is not None or ins.effects or ins.all_effects or ins.hold or ins.sync
              or ins.drum_fx or ins.prefer or ins.ramp or ins.velocity or ins.camera or ins.transition)
    if others:
        _add_section(probe, ins, notes, window=(start, end))
    if not probe.sections:
        if not moments:
            return None, notes
        probe.sections.append(SectionDirection(start=start, end=end, fixed=True, only_moments=True))
        notes.append(f"⏱ {_mmss(start)}–{_mmss(end)}: " + ", ".join(MOMENT_WORD.get(a, a) for a in moments))
    sd = probe.sections[0]
    sd.moments = moments
    sd.note = text.strip()
    # Only says what to show (or which effects): keep the cuts that are there, including your taps.
    sd.keep_cuts = not (ins.pace is not None or ins.cut_rate is not None or ins.hold or ins.sync or ins.ramp
                        or ins.velocity or sd.only_moments)
    if ins.pace and not (ins.cut_rate is not None or ins.hold or ins.sync or ins.ramp):
        # "quicker cuts" on a selected part: quicker than it is there now, not a fixed rate
        sd.pace_shift, sd.cut_every = (2.0 if ins.pace >= 1.5 or ins.frantic else 1.0) if ins.pace > 0 else \
            (-2.0 if ins.pace <= -1.5 else -1.0), None
    if sd.keep_cuts:
        sd.cut_every, sd.energy = None, None
        sd.prefer = dict(ins.prefer)  # including motion ("fight scenes"), which picks the shots here
        if ins.camera:
            sd.prefer["camera_" + ins.camera] = 1.0
        if ins.on_taps:
            sd.what = (sd.what + " " if sd.what else "") + "on your cuts here"
    if moments and sd.what:
        sd.what += ", " + ", ".join(MOMENT_WORD.get(a, a) for a in moments)
    elif moments:
        sd.what = ", ".join(MOMENT_WORD.get(a, a) for a in moments)
    return sd, notes
