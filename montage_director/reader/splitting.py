"""Step 2: split a prompt into separate instructions (sentences, commas, "then", "and" before a new part)."""

from __future__ import annotations

import re

from ..direction import _cut_rate
from .normalise import TIME_RE
from .vocabulary import (
    CONTENT_RX,
    CUES,
    EFFECT_RX,
    FAST_RX,
    HOLD_RX,
    NORMAL_RX,
    RAMP_RX,
    REST_RX,
    SECTION_RX,
    SECTION_WORDS,
    SLOW_RX,
    TRANSITION_WORDS,
    VELOCITY_RX,
)


def _has_scope(text: str) -> bool:
    return bool(TIME_RE.search(text) or "@" in text or SECTION_RX.search(text) or REST_RX.search(text)
                or any(re.search(p, text) for _, p in CUES))


def _split(text: str) -> list[str]:
    out: list[str] = []
    for sentence in re.split(r"(?<!\d)[.!?;\n]+|[.!?;\n]+(?!\d)", text):
        if not sentence.strip():
            continue
        # commas (not inside "1,2,3,4"), "then", "but", "after that", "while"
        parts = re.split(r"(?<!\d),(?!\d)|,(?=\s)|\bthen\b|\bbut\b|\bafter that\b|\bfollowed by\b", sentence)
        pieces: list[str] = []
        for part in parts:
            # a new time inside the piece starts a new instruction ("slow down at 1:20 and speed up at 1:45")
            times = list(TIME_RE.finditer(part))
            bounds = [0]
            for k, m in enumerate(times[1:], 1):
                prev_end = times[k - 1].end()
                joiner = max(part.rfind(" and ", prev_end, m.start()), part.rfind(" then ", prev_end, m.start()))
                lead = joiner if joiner >= 0 else max(part.rfind(w, prev_end, m.start()) for w in
                                                      (" at ", " around ", " by ", " from ", " about "))
                bounds.append(lead if lead >= prev_end else m.start())
            bounds.append(len(part))
            for a, b in zip(bounds[:-1], bounds[1:]):
                pieces += _split_and(part[a:b])
        out += pieces
    # A piece that only says WHERE ("around 0:55,") joins the next one, which says what.
    merged: list[str] = []
    for p in (p.strip(" ,") for p in out):
        if not p or re.fullmatch(r"(?:and|then|so|also|ok|okay|and then)\W*", p):
            continue
        # "the part at 1:36 when the drums get faster, i want quicker cuts": the request
        # belongs to the moment just described
        asks = re.match(r"(?:and )?(?:i want|i wanna|we can|we could|let's|lets|give me|show|have|put|use|make|"
                        r"there should|it should|i need)\b", p)
        # "a scene of the gundam wing, with the feather wings, until 0:17": more about the same shot
        if merged and re.match(r"(?:with|wearing|holding|where|who|that has|which has) (?:the|his|her|their|its|a|an)\b", p) \
                and not FAST_RX.search(p) and not SLOW_RX.search(p):
            merged[-1] = merged[-1] + " " + p
            continue
        # "at 2:10 let the fight play, no cuts": a bare "no cuts" belongs to the instruction before it
        if merged and re.fullmatch(r"(?:and |just |with )?(?:no cuts(?: at all)?|don'?t cut(?: it| away)?|uncut|hold it|"
                                   r"let it (?:play|run)(?: out)?|no cutting|one take)", p):
            merged[-1] = merged[-1] + " " + p
            continue
        # ", until about 0:17 when the beat drops" ends the instruction before it
        if merged and re.match(r"(?:and |then )?(?:until|till|til|up to|up until|all the way to|through|thru|right up to)\b", p) \
                and _only_scope(p):
            merged[-1] = merged[-1] + " " + p
            continue
        if merged and not TIME_RE.search(p) and not _has_scope(p) and (
                _only_scope(merged[-1]) or (asks and (TIME_RE.search(merged[-1]) or _cue_only(merged[-1])))) or merged and _only_scope(merged[-1]) and not TIME_RE.search(p):
            merged[-1] = merged[-1] + " " + p
        else:
            merged.append(p)
    return merged


def _split_and(piece: str) -> list[str]:
    """Split at 'and' / 'as soon as' / 'when' where a new part of the song starts with its own instruction."""
    out = [piece]
    for joiner in (r"\band\b", r"\bas soon as\b", r"\bonce\b", r"\bwhen\b", r"\bwhile\b"):
        nxt = []
        for p in out:
            keep = [(r.start(), r.end()) for r in RAMP_RX.finditer(p)]  # "faster and faster" is one phrase
            ms = [m for m in re.finditer(joiner, p) if not any(a <= m.start() < b for a, b in keep)]
            last = 0
            for m in ms:
                left, right = p[last:m.start()], p[m.start():]
                right_scope = right[m.end() - m.start():]
                # only where BOTH sides are full instructions ("slow intro and crazy chorus"),
                # not "fighting when the beat drops" (the when-part is where the left part goes)
                if _has_scope(left) and _has_scope(right_scope) and _intent_words(left) and _intent_words(right_scope) \
                        and not _only_scope(left) and _scope_differs(left, right_scope):
                    nxt.append(left)
                    last = m.start() + (len(m.group(0)) if joiner == r"\band\b" else 0)
            nxt.append(p[last:])
        out = nxt
    # no punctuation at all: "slow intro fast chorus" -> "slow intro" | "fast chorus"
    final: list[str] = []
    for p in out:
        cut = None
        for m in SECTION_RX.finditer(p):
            left, right = p[:m.end()], p[m.end():]
            if SECTION_RX.search(right) and _intent_words(left) and _intent_words(right) \
                    and not re.match(r"\s*(?:and|then|,)", right):
                cut = m.end()
                break
        if cut:
            final += [p[:cut], p[cut:]]
        else:
            final.append(p)
    return [p for p in final if p.strip()]


def _scope_key(text: str) -> tuple:
    t = TIME_RE.search(text)
    sec = sorted({SECTION_WORDS[m.group(0)] for m in SECTION_RX.finditer(text)})
    cues = sorted({k for k, p in CUES if re.search(p, text)})
    return (t.group(0) if t else None, tuple(sec), tuple(cues), "@" in text, bool(REST_RX.search(text)))


def _scope_differs(a: str, b: str) -> bool:
    ka, kb = _scope_key(a), _scope_key(b)
    return kb != ka and any(kb)


def _intent_words(text: str) -> bool:
    text = TIME_RE.sub(" ", text)
    for _, p in CUES:  # "when the drums get faster" says where, not what
        text = re.sub(p, " ", text)
    return bool(FAST_RX.search(text) or SLOW_RX.search(text) or NORMAL_RX.search(text) or HOLD_RX.search(text)
                or RAMP_RX.search(text) or VELOCITY_RX.search(text) or _cut_rate(text) is not None
                or any(re.search(p, text) for p, _ in TRANSITION_WORDS)
                or any(rx.search(text) for rx, _ in EFFECT_RX) or any(rx.search(text) for rx, _, _ in CONTENT_RX)
                or re.search(r"\bfreeze|\bfade|\bsync|\bon the (?:snare|kick|beat|hits?)|\bcut (?:on|with|to)\b|coincid|"
                             r"line up|match the|with (?:the|those) (?:hits|drums)|with those|on (?:each|every) (?:hit|drum)|"
                             r"\bfocus(?:es|ed|ing)? on\b|\b(?:one|a single) (?:shot|scene)\b|"
                             r"\b(?:show|shows|showing|use|using|put|feature|featuring)\s+(?:the |a |an |some |his |her |their )?"
                             r"[a-z]{3,}",
                             text))


def _cue_only(text: str) -> bool:
    return any(re.search(p, text) for _, p in CUES) and not re.search(r"\bi want|\bshould|\bmake|\bgive", text)


def _only_scope(text: str) -> bool:
    return _has_scope(text) and not _intent_words(text)
