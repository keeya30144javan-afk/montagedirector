"""The Free Director's prompt reader.

Reads a plain-English idea the way an editor would:

1. normalise it ("1 minute 30" -> 1:30, "0:30-0:45" -> one range, "last 20 seconds"),
2. split it into instructions (sentences, commas, "then", "and" before a new part of the song),
3. for each instruction find WHERE it applies (whole edit, a part of the song like "the
   chorus", a moment the app hears like "when the solo starts", "the second drop", a time
   or range, "the last 20 seconds") and WHAT it asks for (pace, effects with amounts,
   negation, the kind of shots, cutting on the kick or snare, effects on each drum hit,
   holds, speed ramps, moments like "freeze at 1:05"),
4. apply it: whole-edit first, then parts of the song, then timed sections, so the more
   specific instruction always wins.

Anything it couldn't read is reported back so the user knows to rephrase it.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

from .direction import (CUT_CHOICES, EFFECTS, SECTION_LABELS, Direction, Moment, SectionDirection, STYLES,
                        DEFAULT_STYLE, _cut_rate, _fx, _mmss, _rate_label)
from .plan import Look

# --------------------------------------------------------------------------- #
# 1. normalise
# --------------------------------------------------------------------------- #
T = r"\d{1,2}:\d{2}(?:\.\d)?"
TIME_RE = re.compile(rf"(?<![\d:])({T})(?:~({T}))?(?![\d:])")


def _secs(tok: str) -> float:
    m, rest = tok.split(":")
    return int(m) * 60 + float(rest)


def _tok(seconds: float) -> str:
    seconds = max(0.0, seconds)
    whole = int(seconds)
    frac = round(seconds - whole, 1)
    return f"{whole // 60}:{whole % 60:02d}" + (f".{int(frac * 10)}" if frac else "")


def normalise(prompt: str) -> str:
    t = " " + prompt.lower() + " "
    t = (t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
         .replace("–", "-").replace("—", " - "))
    for a, b in (("dont", "don't"), ("doesnt", "doesn't"), ("cant", "can't"), ("wont", "won't"),
                 ("isnt", "isn't"), ("shouldnt", "shouldn't"), ("didnt", "didn't"), ("aint", "ain't")):
        t = re.sub(rf"\b{a}\b", b, t)
    t = re.sub(r"\bdo not\b", "don't", t)
    t = re.sub(r"\b(?:id|i'd) (?:like|love|want)\b", "i want", t)
    t = re.sub(r"\b(slo-?mo|slow-mo|slowmo|slow mo|slomo)\b", "slow motion", t)
    t = re.sub(r"\bsped up\b", "speed up", t)
    t = re.sub(r"\b(asf|af|as hell|as fuck)\b", " super ", t)
    t = re.sub(r"\b(drop|chorus|verse|hook|breakdown|bridge|build)\s*(\d)\b",
               lambda m: f"{['', 'first', 'second', 'third', 'fourth'][min(int(m.group(2)), 4)]} {m.group(1)}", t)
    # relative spans
    num = r"(\d+(?:\.\d+)?)"
    sec = r"(?:seconds?|secs?|s)\b"
    mins = r"(?:minutes?|mins?)\b"
    t = re.sub(rf"\b(?:the\s+)?(?:last|final|ending)\s+{num}\s*{sec}", lambda m: f" @tail{m.group(1)} ", t)
    t = re.sub(rf"\b(?:the\s+)?(?:last|final)\s+{num}\s*{mins}", lambda m: f" @tail{float(m.group(1)) * 60:g} ", t)
    t = re.sub(rf"\b(?:the\s+)?(?:first|opening)\s+{num}\s*{sec}", lambda m: f" @head{m.group(1)} ", t)
    t = re.sub(rf"\b(?:the\s+)?(?:first|opening)\s+{num}\s*{mins}", lambda m: f" @head{float(m.group(1)) * 60:g} ", t)
    t = re.sub(r"\b(?:the\s+)?first half(?: of the (?:song|edit|video))?\b", " @firsthalf ", t)
    t = re.sub(r"\bin the middle\b(?! of the (?:song|edit|video))", " @middle ", t)
    t = re.sub(r"\b(?:the\s+)?(?:second|last|back) half(?: of the (?:song|edit|video))?\b|\bhalf ?way(?: through| point| mark)?\b"
               r"|\bmidway\b|\b(?:in )?the middle of the (?:song|edit|video)\b", " @half ", t)
    # absolute times written out
    t = re.sub(rf"\b(\d{{1,2}})\s*(?:minutes?|mins?|m)\s*(?:and\s+)?(\d{{1,2}})\s*(?:seconds?|secs?|s)?\b",
               lambda m: f"{int(m.group(1))}:{int(m.group(2)):02d}", t)
    t = re.sub(rf"\b(\d{{1,2}})\s*{mins}(?:\s+mark)?", lambda m: f"{int(m.group(1))}:00", t)
    t = re.sub(rf"(?<![\d:@a-z])(\d{{1,4}}(?:\.\d)?)\s*{sec}(?:\s+mark)?(?:\s+in)?",
               lambda m: _tok(float(m.group(1))), t)
    # ranges become one token: 0:30~0:45
    t = re.sub(rf"\bbetween\s+({T})\s+and\s+({T})", r"\1~\2", t)
    t = re.sub(rf"(?:\bfrom\s+)?({T})\s*(?:-|to|till|til|until|through|thru)\s*({T})", r"\1~\2", t)
    return re.sub(r"[ \t]+", " ", t)


# --------------------------------------------------------------------------- #
# vocabulary
# --------------------------------------------------------------------------- #
SECTION_WORDS = {
    "intro": "intro", "beginning": "intro", "opening": "intro", "verse": "verse", "verses": "verse",
    "chorus": "drop", "choruses": "drop", "hook": "drop", "drop": "drop", "drops": "drop",
    "build": "build", "buildup": "build", "build-up": "build", "build up": "build", "pre-chorus": "build",
    "prechorus": "build", "breakdown": "breakdown", "bridge": "breakdown", "outro": "outro", "ending": "outro",
}
ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
            "last": -1, "final": -1}
# Musical moments the app hears (most specific first). Section nouns are handled separately.
CUES = [
    ("hits", r"hard (?:drums|hits|stabs)|\b1\s*,?\s*2\s*,?\s*3\s*,?\s*4\b|\bstabs\b|\baccents\b|big hits|four hits"
             r"|the hits\b(?! in)"),
    ("solo", r"guitar solo|\bsolo\b|\blead (?:guitar|melody)\b"),
    ("drums_faster", r"drums?\b.{0,30}\b(?:faster|frantic|speed|speeding|busier|intense|crazy)"
                     r"(?: and (?:more |even more )?(?:frantic|intense|crazy|busier|faster|wild))*|\bfrantic drums\b"
                     r"|double time drums"),
    ("ringout", r"note (?:ends|rings|fades)|rings? out|\bringing\b|\brings\b|last note|end of the song|\bthe end\b"
                r"|\bat the end\b|\bend it\b|\bend on\b|\bend with\b|\bthe very end\b"),
    ("predrop", r"\bbefore (?:the|each|every|a) (?:bass |beat |big )?drops?|\bleading (?:up )?(?:in)?to the drops?"),
    ("drop", r"bass (?:drops|comes in|hits|kicks in)|beat (?:drops|comes in|kicks in|hits)|\bkicks in\b"
             r"|\bthe drop hits\b|\bdrop comes in\b"),
    ("vocals", r"\b(?:vocals?|singing|singer|lyrics|voice|rapping|rap) (?:come|comes|start|starts|kick|kicks|begin|begins)"
               r"|\bwhen (?:he|she|they) (?:starts? )?(?:sings?|raps?)"),
    ("stop", r"\b(?:music )?(?:stops|goes quiet|cuts out|goes silent)\b|\bthe silence\b|\bthe pause\b"),
    ("fill", r"drum fills?|\bfills?\b"),
    ("intro", r"\bat the (?:start|beginning)\b|\bstart of the song\b|\bto start\b|\bstart off\b|cymbals|"
              r"\b(?:start|begin)(?:s|ing)? (?:it |the edit |off |out )?(?=slow|with|calm|chill|soft|quiet|by|on)"),
]
CUE_NAMES = {"hits": "hard hits", "solo": "solo", "drums_faster": "drums get faster", "ringout": "last note rings",
             "drop": "bass drops", "stop": "music stops", "fill": "drum fill", "intro": "intro",
             "predrop": "lead-in to the drop", "vocals": "vocals come in"}

STRONG = r"(?:super|really|very|so|extremely|insanely|ultra|mega|way|hella|absolutely|totally|max|maximum|too)"
MILD = r"(?:a little|a bit|a little bit|slightly|kinda|kind of|somewhat|a tad|bit|mildly|little)"
NONE = r"(?:no|not|without|don't|never|zero|hate|avoid|stop|skip|remove|get rid of|lose the|nothing|none|isn't|aren't|shouldn't|won't|can't)"
LESS = r"(?:less|fewer|not as many|not so many|not as much|tone down|toned down|lower|reduce|cut down on|not too many|dial back|dial down|calm down the)"
LIGHT = r"(?:a few|some|light|subtle|little|small|minimal|occasional|slight|soft|a couple|couple of|just a few|only a few|sprinkle of|bit of|a bit of|hint of)"
MORE = r"(?:more|lots of|lot of|a lot of|tons of|ton of|heavy|heavier|extra|plenty of|loads of|max|crazy|big|bigger|strong|stronger|many|frequent|constant|all the|insane|nonstop|so many|alot of)"

EFFECT_WORDS = [  # (pattern, effect)
    (r"white flash(?:es)?|flash(?:es|ing|y)?", "flash"),
    (r"zoom(?:s|ing)?(?: punch(?:es)?| ins?)?|punch[- ]?ins?", "zoom"),
    (r"(?:camera |screen )?shak(?:e|es|y|ing)|wobble", "shake"),
    (r"glitch(?:es|y|ing)?|rgb(?: split)?|chromatic(?: aberration)?|datamosh(?:ing)?", "rgb"),
    (r"slow motion", "slowmo"),
    (r"revers(?:e|ed|ing)|rewind(?:s|ing)?|backwards", "reverse"),
    (r"invert(?:ed|s)?|negative(?: flash(?:es)?)?|strobe(?:s|ing)?", "invert"),
]
FAST = (r"fast(?:er)?|quick(?:er|ly)?|rapid(?:ly)?|hype(?:d)?|aggressive|crazy|insane|intense|chaotic|wild|"
        r"go(?:es)? off|go hard|goes hard|speed (?:it )?up|speeds up|pick(?:s)? (?:it |back )?up|picks back up|energetic|"
        r"high energy|frantic|flurry|barrage|hectic|nonstop|hard[- ]hitting|lit|snappy|choppy|rapid fire|"
        r"go (?:all )?out|turn (?:it )?up|amp(?:ed)? (?:it )?up|go nuts|go ham|explode|hit hard|hits hard|hit harder|hits harder|harder|"
        r"more cuts|cut more|more often|shorter cuts")
SLOW = (r"slow(?:er)?(?! motion)|slow (?:it )?down|slows down|calm(?:er)?|chill(?:er)?|mellow|relax(?:ed|ing)?|gentle|"
        r"peaceful|soft(?:er)?|emotional|sad|melanchol(?:y|ic)|laid back|take it easy|long(?:er)? (?:shots|cuts|clips)|"
        r"let it breathe|breathe|tone it down|calm it down|ease up|low energy|quiet|mysterious|eerie|creepy|spooky|"
        r"ominous|haunting|less cuts|fewer cuts|cut less|less often|longer cuts")
NORMAL = (r"back to (?:normal|the flow|the groove|regular|how it was|the melody|the (?:main )?(?:beat|vibe|rhythm|pace))|"
          r"normal pace|regular pace|the flow|groove|steady|medium pace|go back|goes back|melody")
HOLD = (r"still shot|one shot|single shot|\bhold(?:s|ing)?\b|held shot|panning shot|pan shot|one long shot|"
        r"stay on (?:one|a|the)|linger|let (?:it|the shot) play|don't cut|no cuts|long take|"
        r"\ba still\b|\bstill (?:of|frame|image|picture)\b|\b(?:final|last|closing|ending|end) shot\b|"
        r"\bend (?:it )?(?:on|with) (?:a|one) (?:shot|still|image|picture)\b|group shot|wide shot")
RAMP = (r"faster and faster|quicker and quicker|more and more cuts|speed(?:s|ing)? up (?:gradually|more and more)|"
        r"gradually (?:faster|quicker|speed(?:s|ing)? up)|build(?:s|ing)? (?:up )?(?:the )?tension|accelerat\w+|"
        r"getting (?:faster|quicker)|ramp(?:s|ing)? up|cuts? speed(?:s|ing)? up|build(?:s|ing)? up (?:the )?(?:speed|pace|cuts)")
VELOCITY = r"velocity|speed ramps?|speed-ramp(?:s|ing)?|speed changes|time ?remap"
TRANSITION_WORDS = [  # (pattern, kind), most specific first
    (r"\b(?:cross ?fades?|cross-fades?|crossfading|cross[- ]dissolves?|fades? between|blend(?:s|ing)? (?:between|into)|"
     r"fade (?:from one|between) (?:shot|scene|clip)s?)\b", "crossfade"),
    (r"\bdissolves?\b", "dissolve"),
    (r"\bwhip(?:s| pans?| transitions?)?\b|\bswipes?\b|\bslide transitions?\b", "whip"),
    (r"\bzoom(?:-| )?(?:transitions?|through|thru)\b|\bzoom between\b", "zoom"),
    (r"\b(?:dips? to white|white dips?|fade through white|white transitions?|flash transitions?)\b", "white"),
    (r"\b(?:dips? to black|black dips?|fade through black|black transitions?)\b", "black"),
    (r"\bcircle (?:wipes?|transitions?|opens?)\b", "circle"),
    (r"\bwipes?\b|\bwipe transitions?\b", "wipe"),
]
DRUMS = [  # what "on the ..." can mean (pattern, drum)
    (r"snares?|claps?|backbeat", "snare"),
    (r"kicks?(?: drums?)?|bass drums?|808s?|bass hits?|the bass\b|bass", "kick"),
    (r"(?:big |hard |loud |strong )?hits?|accents|drum hits|drums", "hits"),
    (r"downbeats?|bars?|every bar", "bar"),
    (r"beats?", "beat"),
    (r"drops?", "drop"),
]
CONTENT = [  # (pattern, preferences, what to call it)
    (r"punch(?:es|ing)?|fight(?:s|ing)?|kicking|attack(?:s|ing)?|combat|battle(?:s)?|beat ?downs?|combos?|"
     r"brawl(?:s|ing)?|clash(?:es|ing)?|sword ?fights?|gun ?fights?|shoot ?outs?|chases?|action(?: scenes| shots)?",
     {"motion": 1.0}, "action shots"),
    (r"explosions?|explod\w+|blasts?|beams?|energy (?:blasts?|attacks?)|kamehameha|final flash|galick gun|lasers?",
     {"motion": 0.6, "bright": 1.0}, "bright blasts and explosions"),
    (r"fire|flames?|burning|lava|sunset", {"bright": 0.7, "warm": 1.0}, "fiery, warm shots"),
    (r"auras?|power(?:ing)? ?ups?|powers up|transform(?:ation|ations|ing|s)?|super saiyan|charging|glowing", {"bright": 1.0, "sat": 0.6},
     "glowing power-up shots"),
    (r"lightning|electric(?:ity)?|thunder", {"bright": 0.8, "hue": 220.0}, "electric blue shots"),
    (r"yell(?:s|ing)?|scream(?:s|ing)?|shout(?:s|ing)?|roar(?:s|ing)?|rage|raging|angry|furious|battle cry",
     {"bright": 0.5, "sat": 0.6}, "intense shots (yelling, rage)"),
    (r"close[- ]?ups?|faces?|face shots|talking|dialogue|conversations?|staring|eyes|looking|reaction shots?",
     {"motion": -1.0}, "close-ups and faces"),
    (r"quiet scenes|calm scenes|emotional scenes|sad scenes|crying|tears|scenery|landscapes?|establishing shots|"
     r"the sky|clouds|peaceful scenes", {"motion": -1.0}, "calm scenes"),
    (r"bright(?:er)? (?:scenes|shots|parts|moments)|daylight|sunny", {"bright": 1.0}, "bright scenes"),
    (r"dark(?:er)? (?:scenes|shots|parts|moments)?|night(?:time)?|shadows?|moody|gloomy|grim|mysterious|eerie|creepy|spooky",
     {"bright": -1.0}, "dark scenes"),
    (r"no movement|not moving|still shots|static shots|calm shots|nothing moving", {"motion": -1.0}, "still, calm shots"),
    (r"colou?rful|vibrant|saturated|neon", {"sat": 1.0}, "colourful shots"),
    (r"red (?:scenes|shots|parts)?|bloody|blood", {"hue": 0.0}, "red shots"),
    (r"blue (?:scenes|shots|parts)?|ocean|water|underwater|ice|icy", {"hue": 220.0}, "blue shots"),
    (r"green (?:scenes|shots|parts)?|forest|grass|nature", {"hue": 120.0}, "green shots"),
    (r"purple (?:scenes|shots|parts)?|pink (?:scenes|shots|parts)?", {"hue": 290.0}, "purple shots"),
    (r"gold(?:en)? (?:scenes|shots|parts)?|yellow (?:scenes|shots|parts)?", {"hue": 50.0, "bright": 0.4}, "golden shots"),
]
QUERY_SKIP = set("""about above after again against also another around away back because been before being below
between both but can could did does doing down during each even ever every few first from further give going gonna got
had has have having her here hers him his how into its just last later least less let lets like made make many more
most much must near need next not now off once one only other our out over own part parts put really right same
see seem seems shall she should show some something song still such take than that the their them then there these
they thing things this those though through till too under until upon very want was way well were what when where
which while who whom why will with within without would yet you your yours start end ending middle beginning intro
outro verse chorus drop drops bridge build section sections second seconds minute minutes time times bit lot lots
character characters people person guy guys scene scenes shot shots clip clips footage video edit edits cut cuts
moment moments possible get getting look looks looking happen happens happening put putting use using try
final screen bunch frame frames image picture whole entire every each other way long short too much fast slow
quick quicker slower faster cool sick nice good great best better bad kind sort feel""".split())
FILLER = re.compile(r"\b(i want|i'd like|i would like|i need|please|make it|make the|make this|the edit|this edit|edit|it|"
                    r"should|would|be|have|has|to|of|the|a|an|and|with|for|in|on|is|are|that|this|my|me|i|you|"
                    r"thanks|thank you|ok|okay|like|just|really|very|so|some|lot|lots|also|too|then|there|"
                    r"gonna|wanna|vibe|vibes|feel|feeling|kind|type|style|song|music|video|clip|clips|shots|"
                    r"scenes|parts|part|all|whole|time|way|look|looks|show|use|put|get|go|do|make|can|maybe|"
                    r"good|nice|cool|awesome|sick|dope|fire|lit|bro|dude|yeah|yes|right|well|keep|let|we|our|its|"
                    r"it's|i'm|im|want|wants|need|needs|cut|cuts|edits|editing)\b")


def _rx(p: str) -> re.Pattern:
    return re.compile(r"(?<![\w-])(?:" + p + r")(?![\w])")


EFFECT_RX = [(_rx(p), e) for p, e in EFFECT_WORDS]
FAST_RX, SLOW_RX, NORMAL_RX = _rx(FAST), _rx(SLOW), _rx(NORMAL)
HOLD_RX, RAMP_RX, VELOCITY_RX = _rx(HOLD), _rx(RAMP), _rx(VELOCITY)
CONTENT_RX = [(_rx(p), pref, name) for p, pref, name in CONTENT]
DRUM_ALT = "|".join(f"(?:{p})" for p, _ in DRUMS)
SECTION_RX = _rx("|".join(sorted((re.escape(w) for w in SECTION_WORDS), key=len, reverse=True)))
ORDINAL_RX = re.compile(r"(?<![\w-])(first|1st|second|2nd|third|3rd|fourth|4th|last|final)\s+(?:big\s+|main\s+)?("
                        + "|".join(sorted((re.escape(w) for w in SECTION_WORDS), key=len, reverse=True)) + r")\b")
REST_RX = _rx(r"the rest|everything else|rest of (?:it|the (?:song|edit|video))|the whole (?:time|thing|song|edit|video)|"
              r"throughout|all the way through|the entire (?:song|edit|video)|overall|in general")


def _drum_of(word: str) -> str:
    for p, d in DRUMS:
        if re.fullmatch(p, word.strip()):
            return d
    return "hits"


# --------------------------------------------------------------------------- #
# 2. split into instructions
# --------------------------------------------------------------------------- #
def _has_scope(text: str) -> bool:
    return bool(TIME_RE.search(text) or "@" in text or SECTION_RX.search(text) or REST_RX.search(text)
                or any(re.search(p, text) for _, p in CUES))


def _split(text: str) -> list[str]:
    out: list[str] = []
    for sentence in re.split(r"(?<!\d)[.!?;\n]+|[.!?;\n]+(?!\d)", text):
        if not sentence.strip():
            continue
        # commas (not inside "1,2,3,4"), "then", "but", "after that", "while"
        parts = re.split(r"(?<!\d),(?!\d)|,(?=\s)|\bthen\b|\bbut\b|\bafter that\b|\bfollowed by\b|\bnext\b", sentence)
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
        if merged and not TIME_RE.search(p) and not _has_scope(p) and (
                _only_scope(merged[-1]) or (asks and (TIME_RE.search(merged[-1]) or _cue_only(merged[-1])))):
            merged[-1] = merged[-1] + " " + p
        elif merged and _only_scope(merged[-1]) and not TIME_RE.search(p):
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
    return [p for p in out if p.strip()]


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
                             r"line up|match the|with (?:the|those) (?:hits|drums)|with those|on (?:each|every) (?:hit|drum)",
                             text))


def _cue_only(text: str) -> bool:
    return any(re.search(p, text) for _, p in CUES) and not re.search(r"\bi want|\bshould|\bmake|\bgive", text)


def _only_scope(text: str) -> bool:
    return _has_scope(text) and not _intent_words(text)


# --------------------------------------------------------------------------- #
# 3. read one instruction
# --------------------------------------------------------------------------- #
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
    understood: bool = False


def _amount(before: str) -> str:
    """How much of an effect, from the words just before it."""
    tail = before[-40:]
    if re.search(rf"\b{NONE}\b(?:\s+\w+){{0,3}}\s*$", tail):
        if re.search(rf"\b(?:not|don't) (?:too|so|that) (?:many|much)\b(?:\s+\w+){{0,2}}\s*$", tail):
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
    if re.search(rf"\b{STRONG}\s*$", before) or re.search(rf"^\s*(?:super|as hell|than ever|and then some|as possible)\b", after):
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
    m = re.search(r"\b(?:no|without|zero|skip the|no extra) (?:effects|fx|filters)\b|\bcuts? only\b|\bjust (?:the )?cuts\b", work)
    if m:
        ins.all_effects = "none"
        take(m)
    m = re.search(rf"\b(?:{MORE}|{LESS}|{LIGHT})\s+(?:effects|fx)\b", work)
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
    fast = [m for m in FAST_RX.finditer(work)]
    slow = [m for m in SLOW_RX.finditer(work)]
    normal = NORMAL_RX.search(work)
    up, down = 0.0, 0.0
    for m in fast:
        if _negated(work, m.start()):
            down = max(down, 0.5)   # "not too fast" -> a touch calmer
        else:
            up = max(up, _strength(work, m.start(), m.end()))
    for m in slow:
        if not _negated(work, m.start()):
            down = max(down, _strength(work, m.start(), m.end()))
    if fast or slow:
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
    if not ins.hold and not ins.on_taps and re.search(
            r"\b(?:a|one|single) (?:shot|scene|still|picture|frame)(?: or (?:a |one )?(?:shot|scene|still))? "
            r"(?:with|of|where|that|showing)\b", text):
        ins.hold = True  # "a shot with as many characters as possible for the last part"
        ins.still = "still" in text
    # Names and things to look for in the shot descriptions ("vegeta", "nappa", "spaceship")
    left = [w for w in re.findall(r"[a-z][a-z'-]{2,}", FILLER.sub(" ", work)) if w not in QUERY_SKIP]
    if left:
        ins.prefer["_query"] = sorted(set(left))[:6]
    ins.people = bool(re.search(r"\b(?:characters?|everyone|everybody|the (?:whole )?(?:crew|team|squad|gang|cast|group)|"
                                r"group shot|together|all of them|both of them|him|her|them)\b", text))
    ins.frantic = bool(re.search(r"frantic|rapid|flurry|barrage|crazy|insane|nonstop", work))

    query = ins.prefer.pop("_query", None)
    if query and re.search(r"\b(?:show|shows|showing|use|using|put|have|with|featuring|feature|of|starring|where)\b", text):
        ins.prefer["_query"] = query  # "show nappa in the chorus": looked up in the shot descriptions
    ins.understood = bool(ins.on_taps or ins.transition or "crowd" in ins.prefer or ins.pace is not None or ins.cut_rate or ins.effects or ins.all_effects or ins.hold
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


# --------------------------------------------------------------------------- #
# 4. apply
# --------------------------------------------------------------------------- #
GLOBAL_HINTS = [
    # (pattern, what it does, note)
    (r"\b(?:sync|synced|match|follow|flow with|move with|vibe with|cut to|cuts? on|feel)\b[\w\s]{0,25}\b(?:the )?"
     r"(?:music|song|beat|rhythm|energy|groove)\b|\bon beat\b|\bbeat ?sync(?:ed)?\b", "follow", "following the music (always on)"),
    (r"\b(?:lots of|more|many|smooth|cool|sick) transitions\b|\btransitions? between\b", "transitions",
     "transitions: soft blurs into each part, white fades into the drops"),
    (r"\bjump cuts\b|\bhard cuts\b|\bno transitions\b|\bstraight cuts\b", "hard_cuts", "hard cuts, no transitions"),
    (r"\bepic\b|\blegendary\b|\bheroic\b|\bhype trailer\b", "epic", "epic: a freeze before the drop, white flash into it"),
    (r"\banime (?:opening|op|intro)\b|\bopening sequence\b|\b(?:op|ed) style\b", "opening",
     "anime opening feel: punchy cuts on the drum fills, a freeze before the drop"),
    (r"\bunder control\b|\bdon't overdo it\b|\bnot too much\b|\btasteful\b|\bnot over the top\b", "simple",
     "toned down: lighter effects"),
    (r"\bphonk\b", "phonk", "phonk edit: heavy shake and zooms on the bass, cuts on the kick in the drop"),
    (r"\bamvs?\b|\bgmvs?\b|\b20(?:0\d|1[0-3])\b|\bold (?:school )?(?:youtube|amv|edit)s?\b|\bnostalgi\w+\b|\bclassic edit\b",
     "amv", "2000s AMV feel: glow, ghost trails, white flashes into the drop"),
    (r"\btrailer\b|\bmovie\b|\bfilm(?:ic)?\b", "trailer", "trailer feel: letterbox, film grade, a freeze before the drop"),
    (r"\b(?:keep it )?simple\b|\bminimal(?:ist)?\b|\bnothing (?:too )?fancy\b|\bbasic\b|\bsubtle\b|\bclean\b", "simple",
     "simple: lighter effects"),
    (r"\bsmooth(?:er)?(?: transitions)?\b|\bsoft transitions\b|\bfluid\b|\bseamless\b", "smooth",
     "smooth: soft blur transitions, gentler flashes"),
    (r"\bfreeze(?: frame)? (?:right )?(?:before|on|at|into) the drop\b", "freeze_drop", "freeze frame before the drop"),
    (r"\bwhite (?:flash|fade) (?:in)?to the drop\b|\bfade (?:in )?from white\b", "white_drop", "white fade into the drop"),
]
LOOKS = [
    (r"\bvhs\b|\bretro\b|\bgrain(?:y)?\b|\bcamcorder\b|\bold school\b", "vhs", "VHS look"),
    (r"\bblack (?:and|&|n) white\b|\bb&w\b|\bmonochrome\b|\bgr[ae]yscale\b|\bno colou?r\b", "mono", "black and white"),
    (r"\by2k\b|\b2000s\b|\b2010s\b|\bold youtube\b|\bnostalgi\w*\b", "y2k", "Y2K colour"),
    (r"\bteal\b|\borange and teal\b|\bmovie\b|\bfilm(?:ic)?\b|\bcinematic\b|\btrailer\b", "cinematic", "film grade"),
    (r"\bsad\b|\bmelanchol\w*\b|\bdepressing\b|\brain(?:y)?\b|\bgloomy\b", "sad", "sad grade"),
    (r"\bdream(?:y)?\b|\bethereal\b|\bheavenly\b", "dream", "dreamy grade"),
]


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
                "slowmo": "slow motion", "reverse": "reversed clips", "invert": "negative strobes"}
AMOUNT_WORD = {"none": "no", "less": "fewer", "light": "a few", "more": "more", "add": ""}
DRUM_WORD = {"snare": "snare", "kick": "kick", "hits": "drum hit", "big": "big hit", "bar": "bar", "beat": "beat"}


TRANSITION_NAMES = {"crossfade": "crossfades", "dissolve": "dissolves", "whip": "whip pans", "zoom": "zoom-throughs",
                    "white": "dips to white", "black": "dips to black", "circle": "circle wipes", "wipe": "wipes"}
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
    st = d.style
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
    if re.search(r"\b(?:no|don't|never|without|avoid|stop|zero)\b[\w\s']{0,20}\b(?:repeat|repeats|repeating|reuse|reusing|"
                 r"duplicate|duplicates|same (?:clip|scene|shot)s?)\b|\bunique (?:clips|scenes|shots)\b|\beach (?:clip|scene|shot) once\b",
                 text):
        d.repeats = "never"
        notes.append("no repeated scenes")
    elif re.search(r"\b(?:repeat|reuse|callback)s?\b", text):
        d.repeats = "allowed"
        notes.append("repeats allowed")
    m = re.search(rf"\b(?:the )?drop (?:is|hits|happens|comes)? ?(?:at|around|about) ({T})(?![~\d])", text)
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
        overall = max([p for p in paces if p > 0], default=0.0) + min([p for p in paces if p < 0], default=0.0)
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
    shape = re.search(r"vertical|tiktok|shorts|reels|square|portrait|repeat|reuse|letterbox|glow|trail|ghost|drop (?:is|at)|"
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
            and not (ins.pace is not None or ins.hold or ins.sync or ins.prefer or ins.cut_rate or ins.ramp
                     or ins.velocity or (ins.cue not in (None, "end", "ringout"))):
        acts = list(point_fx) + [e for e, amt in ins.effects.items() if amt != "none"]
        t = times[0] if times else -1.0
        for a in acts:
            d.moments.append(Moment(time=t, action=a,
                                    duration=1.0 if a in ("freeze", "slowmo", "shake") else (1.5 if a == "fade_black" else 0.0)))
        notes.append(f"⏱ {_mmss(t) if t >= 0 else 'the end'}: " + ", ".join(MOMENT_WORD.get(a, a) for a in acts))
        return
    if ins.span and ins.moments and not (ins.pace is not None or ins.hold or ins.sync or ins.prefer or ins.cut_rate
                                         or ins.effects or ins.ramp or ins.velocity):
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
        if not (ins.moments or ins.pace is not None or ins.hold or ins.sync or ins.prefer or ins.cut_rate
                or ins.effects or ins.ramp or ins.velocity):
            return
    if ins.moments and ins.cue and not times and not (ins.pace is not None or ins.hold or ins.sync or ins.prefer
                                                       or ins.cut_rate or ins.effects or ins.ramp or ins.velocity):
        # "freeze every time the music stops", "flash when the solo starts": moments at a heard moment
        sd = SectionDirection(start=-1.0, end=-1.0, cue=ins.cue, occurrence=ins.occurrence, note=ins.text.strip(),
                              moments=list(ins.moments), only_moments=True)
        d.sections.append(sd)
        notes.append(f"⏱ {_where(ins, None)}: " + ", ".join(MOMENT_WORD.get(a, a) for a in ins.moments))
        return
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
    sd.prefer = {k: v for k, v in ins.prefer.items() if k != "motion"}
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
    d.sections.append(sd)

    what = ("hold one still shot" if sd.hold and sd.still else "hold one shot" if sd.hold else
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
