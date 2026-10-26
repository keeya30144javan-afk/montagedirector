"""Step 3: read ONE instruction: where it applies (scope) and what it asks for."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..direction import _cut_rate
from .normalise import TIME_RE, T, _secs
from .vocabulary import (
    CONTENT_RX,
    CUES,
    DRUM_ALT,
    EFFECT_RX,
    FAST_RX,
    FILLER,
    HOLD_RX,
    LESS,
    LIGHT,
    MILD,
    MORE,
    NONE,
    NORMAL_RX,
    ORDINAL_RX,
    ORDINALS,
    QUERY_SKIP,
    RAMP_RX,
    REST_RX,
    SECTION_RX,
    SECTION_WORDS,
    SLOW_RX,
    STRONG,
    TRANSITION_WORDS,
    VELOCITY_RX,
    _drum_of,
)


@dataclass
class Instruction:
    text: str
    scope: str = "all"              # all | label | section
    labels: set[str] = field(default_factory=set)
    times: list[float] = field(default_factory=list)
    span: str | None = None
    cue: str | None = None
    occurrence: int | None = None
    pace: float | None = None       # + faster, - slower, 0 back to normal; magnitude = how much
    cut_rate: float | None = None
    effects: dict[str, str] = field(default_factory=dict)   # effect -> more | light | less | none
    all_effects: str | None = None  # more | less | none
    hold: bool = False
    still: bool = False
    camera: str | None = None
    sync: str | None = None         # hits | kick | snare
    drum_fx: dict[str, str] = field(default_factory=dict)
    prefer: dict[str, float] = field(default_factory=dict)
    prefer_names: list[str] = field(default_factory=list)
    ramp: bool = False
    velocity: bool = False
    moments: list[str] = field(default_factory=list)        # freeze | flash | fade_black | ...
    frantic: bool = False           # asked for the quickest cutting (half beats)
    people: bool = False            # asked for particular characters / a group: the free director can't see who
    on_taps: bool = False           # "for every marker / tap / cut I made": keep the cuts, change the shots
    transition: str | None = None   # crossfade | dissolve | whip | zoom | white | black | circle | wipe
    drop_at: float | None = None    # "till 0:17 when the beat drops": the drop is where this ends
    understood: bool = False

    @property
    def shapes_cuts(self) -> bool:
        """Says something about the cuts themselves (pace, holds, sync, what to show, build-ups)."""
        return bool(self.pace is not None or self.hold or self.sync or self.prefer or self.cut_rate is not None
                    or self.ramp or self.velocity)


def _amount(before: str) -> str:
    """How much of an effect, from the words just before it."""
    tail = before[-40:]
    if re.search(rf"\b{NONE}\b(?:\s+\w+){{0,3}}\s*$", tail):
        if re.search(r"\b(?:not|don't) (?:too|so|that) (?:many|much)\b(?:\s+\w+){0,2}\s*$", tail):
            return "less"
        if re.search(r"\b(?:no|not|don't|never|without|zero|nothing|none|hate|avoid|skip|remove|get rid of|lose the|stop)"
                     r"\b(?:\s+(?:any|the|more|use|using|add|adding|put|putting|want|like|with|those|these|of))*\s*$", tail) \
                or re.search(rf"\b{NONE}\s+(?:\w+\s+){{0,2}}$", tail):
            return "none"
    if re.search(rf"\b{LESS}\b(?:\s+\w+){{0,2}}\s*$", tail):
        return "less"
    if re.search(rf"\b{LIGHT}\b(?:\s+\w+){{0,1}}\s*$", tail):
        return "light"
    if re.search(rf"\b{MORE}\b(?:\s+\w+){{0,2}}\s*$", tail) or re.search(rf"\b{STRONG}\b\s*$", tail):
        return "more"
    return "add"


def _strength(text: str, start: int, end: int) -> float:
    before, after = text[max(0, start - 30):start], text[end:end + 25]
    if re.search(rf"\b{MILD}\s*$", before) or re.search(r"^\s*(?:ish|-ish)\b", after):
        return 0.5
    if re.search(rf"\b{STRONG}\s*$", before) or re.search(r"^\s*(?:super|as hell|than ever|and then some|as possible)\b", after):
        return 2.0
    return 1.0


def _negated(text: str, start: int) -> bool:
    before = text[max(0, start - 30):start]
    return bool(re.search(r"\b(?:not|don't|never|no|isn't|shouldn't|won't|without|nothing)\b(?:\s+\w+){0,2}\s*(?:too\s+|so\s+|that\s+|very\s+)?$",
                          before))


def read_instruction(text: str) -> Instruction:
    ins = Instruction(text=text)
    work = text  # matched words are blanked out so they aren't read twice

    def take(m: re.Match) -> None:
        nonlocal work
        work = work[:m.start()] + " " * (m.end() - m.start()) + work[m.end():]

    # effects tied to a drum: "flash on every snare", "slow motion on the big hits", "shake on the bass"
    for rx, eff in EFFECT_RX:
        for m in list(re.finditer(rx.pattern + rf"(?:\s+\w+){{0,2}}?\s+(?:on|with|at|to|every)\s+(?:every|each|the|all the|all)?\s*"
                                  rf"(?:big |hard |loud |strong )?((?:{DRUM_ALT}))\b(?! (?:comes|kicks|drops|in\b))", work)):
            drum = _drum_of(m.group(1))
            if drum == "hits" and re.search(r"\b(?:big|hard|loud|strong|heavy)\s+(?:drum\s+)?hits?\b", m.group(0)):
                drum = "big"
            if drum == "drop":
                continue  # "flash on the drop" -> the drop is a place
            if _amount(work[:m.start()]) == "none":
                continue
            ins.drum_fx[eff] = drum
            take(m)

    m = re.search(rf"\b(?:every time|whenever|each time|when(?:ever)? the)\s+(?:the\s+)?((?:{DRUM_ALT}))\s+(?:hits?|lands?|plays?|kicks?)\b"
                  rf",?\s*(?:i want (?:a |some )?|add (?:a )?|put (?:a )?|do (?:a )?|give (?:it )?(?:a )?)?([a-z ]+)", work)
    if m:
        for rx, eff in EFFECT_RX:
            e = rx.search(m.group(2))
            if e:
                drum = _drum_of(m.group(1))
                ins.drum_fx[eff] = "kick" if drum == "drop" else drum
                take(m)
                break
    # --- where -------------------------------------------------------------
    m = re.search(rf"\b(?:until|till|til|up to|up until|before|by|ending at)\s+(?:about |around )?({T})(?![~\d])", work)
    if m and len(TIME_RE.findall(work)) == 1:
        work = work[:m.start()] + f" 0:00~{m.group(1)} " + work[m.end():]
    for m in TIME_RE.finditer(work):
        ins.times.append(_secs(m.group(1)))
        if m.group(2):
            ins.times.append(_secs(m.group(2)))
        take(m)
        break
    m = re.search(r"@(tail|head)(\d+(?:\.\d+)?)|@(firsthalf|half|middle)", work)
    if m:
        ins.span = f"{m.group(1)}:{m.group(2)}" if m.group(1) else m.group(3)
        take(m)
    for m in ORDINAL_RX.finditer(work):
        ins.occurrence = ORDINALS[m.group(1)]
        label = SECTION_WORDS[m.group(2)]
        ins.cue = "drop" if label == "drop" and m.group(2).startswith("drop") else f"sec:{label}"
        take(m)
        break
    if ins.cue is None:
        for kind, pattern in CUES:
            m = re.search(pattern, work)
            if m:
                ins.cue = kind
                take(m)
                break
    if ins.cue == "drop" and len(ins.times) == 1 and not re.search(r"\b(?:go|cut|show|hold|slow|fast|quick|crazy)", work):
        ins.drop_at, ins.cue = ins.times[0], None  # "the beat drops at 0:32": where the drop is
        ins.times = []
    if ins.cue == "drop" and len(ins.times) == 2:
        # "from the start till 0:17 when the beat drops": the drop marks the END of this instruction
        ins.drop_at, ins.cue = ins.times[1], None
    if ins.cue and ins.occurrence is None and re.search(
            r"\b(?:each|every|all(?: the)?|both)\s+(?:\w+\s+)?(?:drops?|chorus(?:es)?|verses?|solos?|fills?|stops?|build ?ups?)\b",
            text) or re.search(r"\b(?:every time|whenever|each time|any time)\b", text):
        ins.occurrence = 0  # every time it happens
    for m in SECTION_RX.finditer(work):
        # "the build up" is a place; "build up the tension" is an instruction (handled by RAMP)
        if m.group(0) in ("build up", "build") and re.match(r"\s+(?:up\s+)?(?:the\s+)?(?:tension|speed|pace|hype)", work[m.end():]):
            continue
        ins.labels.add(SECTION_WORDS[m.group(0)])
    if ins.labels:
        work = SECTION_RX.sub(lambda m: " " * len(m.group(0)), work)
    if REST_RX.search(work):
        work = REST_RX.sub(lambda m: " " * len(m.group(0)), work)
    # the very end / start of the song as places
    if ins.cue == "ringout" and re.search(r"\b(freeze|fade|flash)", work) and not ins.times:
        ins.cue = "end"

    # --- what --------------------------------------------------------------
    # whole-effect amounts: "no effects", "don't go crazy with the effects", "subtle effects"
    m = re.search(r"(?:don't|not|never) go (?:too )?(?:crazy|overboard|nuts|wild|ham) (?:with|on) (?:the )?(?:effects|fx|edits)"
                  r"|(?:too many|too much) (?:effects|fx)|(?:effects|fx) (?:are|is) too much", work)
    if m:
        ins.all_effects = "less"
        take(m)
    m = re.search(r"\b(?:go (?:crazy|nuts|wild|ham|all out) (?:with|on) (?:the )?(?:effects|fx))\b", work)
    if m:
        ins.all_effects = "more"
        take(m)
    m = re.search(r"\b(?:no|without|zero|skip the|no extra) (?:effects|fx|filters)\b|\bcuts? only\b|\bjust (?:the )?cuts\b|"
                  r"\bonly (?:the )?cuts\b|\bnothing but cuts\b|\bplain cuts\b", work)
    if m:
        ins.all_effects = "none"
        take(m)
    m = re.search(rf"\b(?:{MORE}|{LESS}|{LIGHT})\s+(?:the |all the |all of the )?(?:effects|fx)\b", work)
    if m and not ins.all_effects:
        a = _amount(work[:m.start() + len(m.group(0)) - len(m.group(0).split()[-1])])
        ins.all_effects = {"light": "less", "add": "more"}.get(a, a)
        take(m)

    # cutting on the drums: "cut on every kick", "edits on the snare", "cuts that hit the 808s"
    m = re.search(rf"\b(?:cut|cuts|cutting|edit|edits|switch(?:es)?|change(?:s)?|transitions?|clips?)\b(?:\s+\w+){{0,3}}?\s+"
                  rf"(?:on|with|to|hit|hitting|match(?:ing)?|line up with)\s+(?:every|each|the|all the|all)?\s*"
                  rf"(?:big |hard |loud |strong )?((?:{DRUM_ALT}))\b", work)
    if m and _drum_of(m.group(1)) in ("snare", "kick", "hits"):
        ins.sync = _drum_of(m.group(1))
        take(m)
    m = re.search(r"coincid\w+|on the hits|on each hit|every hit|each hit|with the hits|on the drums|with the drums|"
                  r"hit sync|line up with (?:the )?(?:drums|hits)|match(?:es|ing)? the (?:hits|drums|drumming)|with those|"
                  r"follow(?:s|ing)? the drums|cut on them|on those hits|\bin sync\b|\bsync(?:ed|s)?(?: it| up)?"
                  r"(?: (?:with|to) (?:the )?\w+)?", work)
    if m and not ins.sync:
        ins.sync = "hits"
        take(m)
    if ins.cue == "hits" and not ins.sync and not ins.drum_fx:
        ins.sync = "hits"

    if re.search(r"\b(?:cut|cuts|edit|edits)?\s*(?:to|on|with) the beat\b", work) and _cut_rate(work) is None:
        work = re.sub(r"(?:to|on|with) the beat\b", "on every beat", work)
    rate = _cut_rate(work)
    if rate is not None:
        ins.cut_rate = rate
        work = re.sub(r"\b(?:every|each|on (?:every|each|the))\s+(?:\d+|one|two|three|four|eight|other|single)?\s*"
                      r"(?:beat|beats|bar|bars|measure|measures)\b|\b(?:twice (?:a|per|every) beat|half[- ]beats?|double[- ]time)\b",
                      lambda m: " " * len(m.group(0)), work)

    for word, kind in TRANSITION_WORDS:
        m = re.search(word, work)
        if m:
            ins.transition = "none" if _negated(work, m.start()) else kind
            take(m)
            break
    for m in list(RAMP_RX.finditer(work)):
        ins.ramp = True
        take(m)
    if ins.ramp:
        if ins.cue is None and not ins.times and not ins.labels and re.search(r"\bdrop\b", text):
            ins.cue = "predrop"
        work = re.sub(r"\b(?:faster|quicker)\b", lambda m: " " * len(m.group(0)), work)
    m = VELOCITY_RX.search(work)
    if m:
        ins.velocity = True
        take(m)

    complaint = re.search(r"\btoo (?:fast|quick|choppy|hectic|busy|frantic|crazy|chaotic|much cutting|many cuts)\b|"
                          r"\b(?:less|not as|not so|stop being|way too) (?:choppy|fast|hectic|busy|quick|frantic|chaotic)\b|"
                          r"\b(?:don't|do not|stop|not) cut(?:ting)? (?:it )?(?:up )?(?:so |too |as )?(?:much|fast|often|quick(?:ly)?)\b|"
                          r"\bcut (?:less|slower)\b|\bhold (?:the |each |every )?(?:shots?|clips?|scenes?|them|it)? ?"
                          r"(?:a bit |a little )?longer\b|\bgive (?:each|every|the) (?:shot|clip|scene)s? more time\b", work)
    if complaint and _negated(work, complaint.start()) and complaint.group(0).startswith("too"):
        complaint = None  # "not too crazy" is a mild ask, not a complaint
    if complaint:  # "it's too choppy", "stop cutting so fast", "hold the shots longer": slower
        take(complaint)
    m = HOLD_RX.search(work)
    if m:
        ins.hold = True
        ins.still = "still" in text or (ins.cue == "intro" and "pan" not in text)
        take(m)
    if re.search(r"\bpan(?:ning|s)?\b", text):
        ins.camera = "pan"
    elif re.search(r"zoom(?:ing)? in(?:to)? (?:shots?|on)|zoom(?:ing)? shots?|push(?:ing)? in", text):
        ins.camera = "zoom"
    elif re.search(r"\bshaky (?:shots|cam|camera|footage)\b|handheld", text):
        ins.camera = "shaky"

    # moments: "freeze frame", "fade to black", "flash" at a time or at the end
    for word, action in (("freeze", "freeze"), ("fade to white", "fade_white"), ("fade (?:to black|out)", "fade_black"),
                         ("fade[ -]?in|fades? (?:in|from black)", "fade_in"), ("fade", "fade_black")):
        m = re.search(rf"\b{word}\w*(?: frames?)?", work)
        if m and not _negated(work, m.start()):
            ins.moments.append(action)
            take(m)
            break

    # effects with amounts
    for rx, eff in EFFECT_RX:
        for m in rx.finditer(work):
            if eff == "shake" and ins.camera == "shaky":
                continue
            amt = _amount(work[:m.start()])
            if re.match(r"\s*(?:is|are) too much", work[m.end():]):
                amt = "less"
            if ins.effects.get(eff) not in (None, "add"):
                continue
            ins.effects[eff] = amt
            take(m)

    # shot content
    for rx, pref, name in CONTENT_RX:
        ms = list(rx.finditer(work))
        if not ms:
            continue
        m = ms[0]
        for extra in ms[1:]:
            take(extra)
        if _negated(work, m.start()):
            pref = {k: (-v if k != "hue" else v) for k, v in pref.items() if k != "hue"}
            name = "no " + name
            if not pref:
                take(m)
                continue
        for k, v in pref.items():
            ins.prefer.setdefault(k, v)
        ins.prefer_names.append(name)
        take(m)

    # pace (after effects and content so "slow motion" / "fire" aren't read as pace)
    fast = list(FAST_RX.finditer(work))
    slow = list(SLOW_RX.finditer(work))
    normal = NORMAL_RX.search(work)
    if re.search(r"\b(?:the |this |my )?(?:song|music|track|beat|instrumental)(?:'s| is| has| feels| sounds| got)\b|"
                 r"\bit'?s an? [\w\s-]{0,25}(?:song|track|beat)\b", text):
        fast, slow = [], []  # "the song is a chill hip hop vibe": describes the music, not how to cut it
    up, down = 0.0, 0.0
    for m in fast:
        if _negated(work, m.start()):
            down = max(down, 0.5)   # "not too fast" -> a touch calmer
        else:
            up = max(up, _strength(work, m.start(), m.end()))
    for m in slow:
        if not _negated(work, m.start()):
            gentle = re.match(r"drawn|play out|let ", m.group(0))  # "drawn out fights": a touch longer, not slow
            down = max(down, 0.5 if gentle else _strength(work, m.start(), m.end()))
    if complaint:
        down = max(down, 1.0)
    if fast or slow or complaint:
        ins.pace = up - down if (up or down) else None
        if ins.pace == 0.0 and not normal:
            ins.pace = None
    if normal and ins.pace is None:
        ins.pace = 0.0
    ins.on_taps = bool(re.search(r"\b(?:every|each|all|my|the|those|these)\s+(?:of my\s+)?(?:manual\s+|tapped\s+)?"
                                 r"(?:markers?|marks?|taps?|cuts?(?= i (?:made|tapped|put|did))|tapped cuts?)\b", text))
    m = re.search(r"\bas many (?:characters|people|guys|fighters|heroes|faces)\b|\b(?:lots|lot|bunch|ton|tons|load|loads) of "
                  r"(?:characters|people|guys|fighters|heroes)\b|\bmany (?:characters|people)\b|\bgroup (?:shot|scene|photo)s?\b|"
                  r"\bwhole (?:crew|team|squad|cast|gang|group)\b|\beveryone\b|\beverybody\b|\ball (?:of )?the (?:characters|"
                  r"heroes|fighters|cast)\b|\ball of them\b|\bmultiple characters\b|\bcrowd(?:ed)?\b|\bthe most characters\b", work)
    if m:
        ins.prefer["crowd"] = 1.0
        ins.prefer_names.append("as many characters as possible")
        take(m)
    m = re.search(r"\b(?:focus(?:es|ed|ing)? on (?:just )?(?:one|a single|1)|just one|only one|a single|one single|"
                  r"solo (?:shot|scene)|on (?:its|his|her|their) own|by (?:itself|himself|herself)|alone)\b", work)
    if m:
        ins.prefer["solo"] = 1.0
        ins.prefer_names.append("one subject in focus")
        take(m)
    if not ins.hold and not ins.on_taps and re.search(
            r"\b(?:a|one|single) (?:shot|scene|still|picture|frame)(?: or (?:a |one )?(?:shot|scene|still))? "
            r"(?:with|of|where|that|showing)\b", text):
        ins.hold = True  # "a shot with as many characters as possible for the last part"
        ins.still = "still" in text
    # Names and things to look for in the shot descriptions ("vegeta", "nappa", "spaceship")
    left = [w for w in re.findall(r"[a-z][a-z'-]{2,}", FILLER.sub(" ", work))
            if w not in QUERY_SKIP and "'" not in w and not w.endswith("n't")]  # not "don't", "it's"
    if left:
        ins.prefer["_query"] = sorted(set(left))[:6]
    ins.people = bool(re.search(r"\b(?:characters?|everyone|everybody|the (?:whole )?(?:crew|team|squad|gang|cast|group)|"
                                r"group shot|together|all of them|both of them|him|her|them)\b", text))
    ins.frantic = bool(re.search(r"frantic|rapid|flurry|barrage|crazy|insane|nonstop", work))

    query = ins.prefer.pop("_query", None)
    if query and re.search(r"\b(?:show|shows|showing|use|using|put|have|with|featuring|feature|of|starring|where)\b", text):
        ins.prefer["_query"] = query  # "show nappa in the chorus": looked up in the shot descriptions
    ins.understood = bool(ins.on_taps or ins.transition or "crowd" in ins.prefer or "solo" in ins.prefer
                          or ins.drop_at is not None or ins.pace is not None or ins.cut_rate or ins.effects or ins.all_effects or ins.hold
                          or ins.sync or ins.drum_fx or ins.prefer or ins.ramp or ins.velocity or ins.moments
                          or ins.camera)
    # Where it applies
    if ins.times or ins.span or ins.cue:
        ins.scope = "section"
    elif ins.labels and not REST_RX.search(text):
        ins.scope = "label"
        if ins.hold or ins.moments:  # a held shot needs a real place in the song
            lab = sorted(ins.labels)[0]
            ins.scope, ins.cue = "section", {"intro": "intro", "drop": "drop", "outro": "ringout"}.get(lab, f"sec:{lab}")
    return ins
