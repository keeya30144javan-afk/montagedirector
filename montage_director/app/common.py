"""Small helpers, constants and help texts shared by the window and its dialogs."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

ASSETS = Path(__file__).resolve().parent.parent / "assets"


SHAPES = [("auto", "Auto (from your idea)"), ("16:9", "16:9 widescreen"), ("9:16", "9:16 vertical (TikTok, Shorts)"),
          ("1:1", "1:1 square"), ("4:3", "4:3 classic")]


FITS = [("fill", "Crop to fill"), ("blur", "Blurred background"), ("fit", "Fit with black bars")]


FPS = [24, 30, 60]


PROMPT_HINT = ("Describe the edit you see in your head. For example:\n"
               "\"Slow emotional intro on the quiet scenes, then go crazy on the drop with the fight "
               "scenes and lots of flashes. Freeze frame at 1:05. Vertical for TikTok.\"")


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def _parse_time(text: str) -> float | None:
    text = text.strip()
    if not text:
        return None
    try:
        if ":" in text:
            m, s = text.split(":", 1)
            return int(m) * 60 + float(s)
        return float(text)
    except ValueError:
        raise ValueError(f"'{text}' isn't a time. Use minutes:seconds, like 1:05.") from None


def open_in_system(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - the user's own file
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def section_title(number: str, text: str) -> QLabel:
    label = QLabel(f"{number}   {text}")
    label.setObjectName("H2")
    return label


TAP_KEYS = {Qt.Key_Space: "cut", Qt.Key_F: "flash", Qt.Key_Z: "zoom", Qt.Key_X: "shake", Qt.Key_D: "drop",
            **{getattr(Qt, f"Key_{k}"): f"scene{k}" for k in "123456789"}}


TAP_HELP = ("Tapping: Space = cut · 1-9 = cut to a scene key · F = flash · Z = zoom · X = shake · D = drop · "
            "Backspace = undo")


IDEAS = [
    ("Hype fight edit", "Still shot for the intro, then when the bass drops go crazy with fight scenes and flashes "
                        "on every snare. Build up the tension with faster and faster cuts before each drop."),
    ("Sad / emotional", "Slow emotional edit with close-ups and faces, long shots, no flashes, slow motion on the "
                        "chorus, black and white. End on a freeze frame."),
    ("Phonk edit", "Phonk edit, cut on the kick, shake on every 808, dark scenes, vertical for TikTok."),
    ("2000s AMV", "Old 2008 YouTube AMV feel with glow and ghost trails, white flash into the drop, "
                  "zoom punches on the chorus."),
    ("Timeline (with times)", "At the start hold one shot. At 0:45 quick cuts of punches. At 1:20 slow down. "
                              "Around 2:00 when the solo starts slower cuts. The last 10 seconds fade to black."),
    ("Velocity edit", "Velocity edit on the drop, slow motion on the big hits, bright explosions in the chorus."),
]


PROMPT_HELP = """<b>The free director understands:</b><br><br>
<b>Pace:</b> fast, quick, crazy, hype, slow, calm, chill, "a little faster", "super slow", "not too fast",
"back to the flow", "cut every 2 beats / every bar", "faster and faster" (a build-up), velocity edit.<br><br>
<b>Effects, with amounts:</b> flashes, zoom punches, shake, glitch / RGB, slow motion, reverse, strobe:
"more", "a few", "less", "no". "Keep it simple", "smooth transitions", "no effects".<br><br>
<b>On the drums:</b> "cut on the kick", "cuts on the snare", "edits on the hits", "flash on every snare",
"shake on every 808", "slow motion on the big hits".<br><br>
<b>What to show:</b> fight scenes, punches, explosions, fire, power-ups, close-ups, faces, calm scenes,
dark / bright scenes, colours (blue, red, gold...), panning shots, "hold one shot".<br><br>
<b>Where:</b> the intro, verse, chorus, drop, bridge, outro; "the second drop", "the last chorus";
"at 1:20", "1 minute 30", "from 0:30 to 0:45", "until 0:11", "the last 20 seconds", "halfway through";
musical moments it hears: "when the bass drops", "when the drums get faster", "when the solo starts",
"the hard hits", "when the vocals come in", "before the drop", "when the last note rings".<br><br>
<b>Transitions:</b> crossfades, dissolves, whip pans, zoom transitions, dips to white / black, wipes;
"lots of transitions", "jump cuts" (none).<br><br>
<b>Text:</b> put words in quotes: title "VEGETA - AWAKE", credits "edit by me" at the end,
at 1:20 caption "Final Flash". Or Tools → Text cards.<br><br>
<b>Who is in the shot:</b> after <i>Describe my shots</i> (once per video), names like "Nappa" and
"a shot with as many characters as possible" work too.<br><br>
<b>Look and shape:</b> black and white, VHS, Y2K, film / trailer, sad, dreamy, glow, ghost trails, letterbox,
vertical / TikTok, square; "no repeats".<br><br>
The free director picks shots by how they look and move (action, calm, dark, bright, colour), not by who
is in them. To get a particular shot (a character, a group shot), select that part of the timeline and
press <b>Pick shot…</b>, or use the AI Director.<br><br>
Anything it doesn't understand is listed under the preview as <i>Didn't catch</i>. The AI Director
understands any wording."""
