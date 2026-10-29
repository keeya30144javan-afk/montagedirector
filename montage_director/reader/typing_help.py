"""The speech-bubble tips shown while someone types a prompt."""

from __future__ import annotations

import re

from ..direction import DEFAULT_STYLE, style_direction
from .apply import read_prompt, region_direction
from .normalise import TIME_RE, normalise
from .spelling import did_you_mean
from .vocabulary import CUES

WHO_RX = re.compile(r"\b(?:characters?|people|person|everyone|everybody|crowd|group|team|crew|squad|him|her|"
                    r"one (?!shot|scene|clip|still|cut|long|static)\w+|a single (?!shot|scene|clip)\w+|only one|just one)\b")


def hold_without_place(text: str) -> bool:
    """"hold one shot" with no where: nothing to hold."""
    from .instruction import read_instruction
    from .splitting import _split

    return any(i.hold and i.scope == "all" and not i.play_out and not i.prefer for i in map(read_instruction, _split(normalise(text))))


def typing_help(text: str, mode: str = "idea", has_tags: bool = False, use_ai: bool = False,
                style: str = DEFAULT_STYLE) -> tuple[list[str], list[str]]:
    """(what the free director will do, tips) for what the user is typing.
    mode: "idea" (the whole edit), "part" (a selected part / an inserted instruction), "refine" (whole edit)."""
    text = text.strip()
    if not text:
        return [], []
    if use_ai and mode != "part":
        return [], ["The AI Director understands any wording. Be specific: say WHEN (a time like 0:17, or "
                    "\"the chorus\"), WHAT should be on screen, and HOW it should cut (quick cuts, hold one shot)."]
    tips: list[str] = []
    fixes = did_you_mean(text, style_direction(style))
    if fixes:
        tips.append("Did you mean " + ", ".join(f"“{good}” (not “{bad}”)" for bad, good in fixes) + "?")
    if mode == "part":
        sd, notes = region_direction(text, 0.0, 10.0)
        reading = [n.split(": ", 1)[-1] for n in notes] if sd is not None or notes else []
        unclear = [] if (sd is not None or notes) else [text]
        prefs = sd.prefer if sd is not None else {}
    else:
        d = read_prompt(text, style_direction(style))
        reading = [n.replace("⏱ ", "") for n in d.notes if not n.startswith("(")]
        unclear = d.unclear
        prefs = {}
        for p in list(d.prefer.values()) + [s.prefer for s in d.sections]:
            prefs.update(p)
    describes = re.compile(r"\b(?:the |this |my )?(?:song|music|track|beat)(?:'s| is| has| feels| sounds)\b")
    unclear = [u for u in unclear if not describes.search(normalise(u))]  # describing the song is fine
    if unclear:
        quoted = "; ".join(f"“{u[:60]}”" for u in unclear[:2])
        tips.append(f"I won't understand {quoted}. Try saying how to cut (quick cuts, hold one shot, cut on the "
                    "snare), what to show (fight scenes, explosions, close-ups) or when (at 1:20, the chorus).")
    low = normalise(text)
    if not has_tags and ("crowd" in prefs or "solo" in prefs or prefs.get("_query") or WHO_RX.search(low)):
        tips.append("Who or what is in a shot (a name, \"one gundam\", \"many characters\", \"fighting in space\") "
                    "needs Describe my shots once (Footage section, about a cent), or choose the shot yourself with "
                    "Pick shot…. Without it, \"space\" just means dark-looking shots.")
    has_time = bool(TIME_RE.search(low))
    if hold_without_place(text):
        tips.insert(0, "Say where to hold it: \"hold one shot in the intro\", \"from 0:10 to 0:20\" or \"at the end\".")
    if not has_time and any(re.search(p, low) for k, p in CUES if k in ("drop", "solo", "drums_faster", "hits",
                                                                       "vocals", "stop")):
        tips.append("If it lands in the wrong place, add a time: \"until 0:17 when the beat drops\", "
                    "\"at 1:36 when the drums get faster\".")
    if describes.search(low) and not reading:
        tips.append("Describing the song is fine, but also say how to cut it, e.g. \"quick cuts on the chorus\".")
    if not reading and not unclear and not tips:
        tips.append("Be specific: WHEN (0:17, the chorus, when the beat drops), WHAT to show (fight scenes, one "
                    "gundam) and HOW to cut (quick cuts, hold one shot, flash on every snare).")
    return reading[:6], tips[:3]
