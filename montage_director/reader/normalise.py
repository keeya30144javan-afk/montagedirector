"""Step 1: tidy up what was typed (typos, times like "1 minute 30", ranges, "the last 20 seconds")."""

from __future__ import annotations

import re

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


TYPOS = {  # common misspellings in edit ideas
    "quik": "quick", "quck": "quick", "qucik": "quick", "fsat": "fast", "fasst": "fast",
    "flahses": "flashes", "flashs": "flashes", "flahs": "flash", "flsh": "flash", "falshes": "flashes",
    "falsh": "flash", "shakey": "shaky", "shakeing": "shaking", "zooom": "zoom", "zoomes": "zooms",
    "transistions": "transitions", "transitons": "transitions", "transtions": "transitions",
    "crossfades": "crossfades", "corssfade": "crossfade", "crosfade": "crossfade", "crossfad": "crossfade",
    "explosians": "explosions", "explostions": "explosions", "explotions": "explosions", "exlposions": "explosions",
    "chrous": "chorus", "choruse": "chorus", "corus": "chorus", "chourus": "chorus", "begining": "beginning",
    "beggining": "beginning", "begginning": "beginning", "agressive": "aggressive", "verticle": "vertical",
    "verical": "vertical", "intesne": "intense", "intens": "intense", "glich": "glitch", "glitchy": "glitchy",
    "efects": "effects", "effecs": "effects", "efffects": "effects", "scences": "scenes", "senes": "scenes",
    "sences": "scenes", "fight scences": "fight scenes", "fihgt": "fight", "figth": "fight", "fighitng": "fighting",
    "repeates": "repeats", "reapeat": "repeat", "dorp": "drop", "drpo": "drop", "slwo": "slow", "slwoer": "slower",
    "fater": "faster", "fasster": "faster", "freez": "freeze", "freezeframe": "freeze frame",
}


def normalise(prompt: str) -> str:
    t = " " + prompt.lower() + " "
    t = (t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
         .replace("–", "-").replace("—", " - "))
    for a, b in (("dont", "don't"), ("doesnt", "doesn't"), ("cant", "can't"), ("wont", "won't"),
                 ("isnt", "isn't"), ("shouldnt", "shouldn't"), ("didnt", "didn't"), ("aint", "ain't")):
        t = re.sub(rf"\b{a}\b", b, t)
    t = re.sub(r"\bdo not\b", "don't", t)
    t = re.sub(r"\b(?:id|i'd) (?:like|love|want)\b", "i want", t)
    for typo, word in TYPOS.items():
        t = re.sub(rf"\b{typo}\b", word, t)
    t = re.sub(r"\b(slo-?mo|slow-mo|slowmo|slow mo|slomo|slowmotion|slow-motion|slomotion)\b", "slow motion", t)
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
    t = re.sub(r"\b(\d{1,2})\s*(?:minutes?|mins?|m)\s*(?:and\s+)?(\d{1,2})(?:\s*(?:seconds?|secs?|s)\b)?",
               lambda m: f"{int(m.group(1))}:{int(m.group(2)):02d}", t)
    t = re.sub(rf"\b(\d{{1,2}})\s*{mins}(?:\s+mark)?", lambda m: f"{int(m.group(1))}:00", t)
    t = re.sub(rf"(?<![\d:@a-z])(\d{{1,4}}(?:\.\d)?)\s*{sec}(?:\s+mark)?(?:\s+in)?",
               lambda m: _tok(float(m.group(1))), t)
    # ranges become one token: 0:30~0:45
    t = re.sub(rf"\bbetween\s+({T})\s+and\s+({T})", r"\1~\2", t)
    t = re.sub(rf"(?:\bfrom\s+)?({T})\s*(?:-|to|till|til|until|through|thru)\s*({T})", r"\1~\2", t)
    return re.sub(r"[ \t]+", " ", t)
