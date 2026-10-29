"""Does the edit actually DO what was asked? Every instruction is made into an edit with every style, and the
result is measured: shot lengths, effects, transitions, holds, colour. (Reading the prompt right isn't enough:
"slower cuts" once read right but still came out at half a second a shot.)"""

import copy

import numpy as np
import pytest

from montage_director.direction import STYLES, style_direction
from montage_director.footage import analyze_footage
from montage_director.music import analyze_song
from montage_director.planner import OutputSettings, make_plan
from montage_director.reader import read_prompt


def _bigger(footage, n=40):
    """The test footage repeated as different scenes, so "no repeats" can be honoured."""
    footage = copy.deepcopy(footage)
    shots = []
    for k in range(n):
        for s in footage.shots:
            c = copy.copy(s)
            c.id, c.group, c.take = len(shots), k * 1000 + s.group, k * 1000 + s.take
            shots.append(c)
    footage.shots = shots
    return footage


def _retempo(song, bpm):
    s = copy.deepcopy(song)
    k = song.tempo / bpm
    s.tempo = bpm
    for name in ("beats", "downbeats", "kicks", "snares", "hats", "drops"):
        setattr(s, name, [t * k for t in getattr(s, name)])
    s.hits = [(t * k, x) for t, x in s.hits]
    s.duration *= k
    for sec in s.sections:
        sec.start, sec.end = sec.start * k, sec.end * k
    return s


def scene_lengths(p, a=0.0, b=1e9):
    """Seconds each scene is on screen (a stutter or a freeze tail is part of the same scene)."""
    out = []
    for i, c in enumerate(p.cuts):
        if i and p.cuts[i - 1].shot == c.shot:
            if out and a * p.fps <= p.cuts[i - 1].f0 < b * p.fps:
                out[-1] += c.frames / p.fps
        elif a * p.fps <= c.f0 < b * p.fps:
            out.append(c.frames / p.fps)
    return out


def avg_scene(p, a=0.0, b=None):
    b = p.duration if b is None else b
    return (b - a) / max(1, len(scene_lengths(p, a, b)))


def fx(p, kind, a=0.0, b=1e9):
    return sum(1 for c in p.cuts for e in c.effects if e.kind == kind and a * p.fps <= c.f0 + e.frame < b * p.fps)


def trans(p, kind=None):
    return sum(1 for c in p.cuts if c.transition and (kind is None or c.transition["kind"] == kind))


def slowmo(p, a=0.0, b=1e9):
    return sum(1 for c in p.cuts if c.speed < 0.99 and not c.freeze and a * p.fps <= c.f0 < b * p.fps)


def cuts_inside(p, a, b):
    return [c for i, c in enumerate(p.cuts)
            if a * p.fps + 1 < c.f0 < b * p.fps - 1 and not (i and p.cuts[i - 1].shot == c.shot)]


def span(song, label):
    sec = max((s for s in song.sections if s.label == label), key=lambda s: s.end - s.start)
    return sec.start, sec.end


def stutters(p, a, b):
    return sum(1 for x, y in zip(p.cuts, p.cuts[1:])
               if x.shot == y.shot and x.src_start == y.src_start and a * p.fps <= y.f0 < b * p.fps)


EFFECT_KINDS = ("flash", "zoom", "shake", "rgb", "invert", "blackout")
CHECKS = [  # (prompt, check(plan, plan without the prompt, song) -> ok)
    ("fast cuts", lambda p, b, s: avg_scene(p) < avg_scene(b) * 0.9 or avg_scene(p) <= 0.55),
    ("slower cuts", lambda p, b, s: avg_scene(p) > avg_scene(b) * 1.15),
    ("dont use so many cuts, have the scenes last longer", lambda p, b, s: avg_scene(p) >= 1.4),
    ("cut every 4 beats", lambda p, b, s: abs(np.median(scene_lengths(p)) - 4 * 60 / s.tempo) < 0.15),
    ("a cut on every beat", lambda p, b, s: abs(np.median(scene_lengths(p)) - 60 / s.tempo) < 0.1),
    ("no flashes", lambda p, b, s: fx(p, "flash") + fx(p, "fade_white") + trans(p, "white") == 0),
    ("lots of flashes", lambda p, b, s: fx(p, "flash") >= max(8, fx(b, "flash") + 1)),
    ("no effects", lambda p, b, s: sum(fx(p, k) for k in EFFECT_KINDS) == 0),
    ("no shake", lambda p, b, s: fx(p, "shake") == 0),
    ("no zooms", lambda p, b, s: fx(p, "zoom") == 0),
    ("shake on the drop", lambda p, b, s: fx(p, "shake", *span(s, "drop")) >= 3),
    ("no slow motion", lambda p, b, s: slowmo(p) == 0),
    ("slow motion in the outro", lambda p, b, s: slowmo(p, *span(s, "outro")) >= 1),
    ("heavy flicker in the intro", lambda p, b, s: fx(p, "blackout", *span(s, "intro")) >= 3),
    ("stutters on the drop", lambda p, b, s: stutters(p, *span(s, "drop")) >= 2),
    ("crossfades between shots", lambda p, b, s: trans(p, "crossfade") >= 3),
    ("dissolves", lambda p, b, s: trans(p, "dissolve") >= 3),
    ("hard cuts", lambda p, b, s: trans(p) == 0),
    ("cut through black", lambda p, b, s: fx(p, "blackout") >= 0.5 * len(scene_lengths(p))),
    ("black and white", lambda p, b, s: p.look.grade == "mono"),
    ("black and white but keep the red", lambda p, b, s: p.look.grade == "splash_red"),
    ("tv static on the drop", lambda p, b, s: fx(p, "static", *span(s, "drop")) >= 2),
    ("narrative amv", lambda p, b, s: sum(fx(p, k) for k in ("shake", "rgb", "invert", "blackout")) == 0
     and fx(p, "flash") <= max(3, fx(b, "flash") // 3) and p.look.grade == "none"),
    ("letterbox", lambda p, b, s: p.look.letterbox),
    ("hold one shot from 0:40 to 0:50", lambda p, b, s: not cuts_inside(p, 40 + 1.5 * 60 / s.tempo, 50 - 1.5 * 60 / s.tempo)),
    ("quick cuts from 1:00 to 1:10", lambda p, b, s: avg_scene(p, 60, 70) < avg_scene(b, 60, 70) * 0.9
     or avg_scene(p, 60, 70) <= 0.56),
    ("freeze frame at 0:20", lambda p, b, s: any(c.freeze and abs(c.f0 / p.fps - 20) < 0.6 for c in p.cuts)),
    ("end on a freeze frame", lambda p, b, s: p.cuts[-1].freeze),
    ("fade to black at the end", lambda p, b, s: fx(p, "fade_black", p.duration - 3) >= 1),
    ("no repeated scenes", lambda p, b, s: len({c.shot for c in p.cuts}) == len(scene_lengths(p))),
    ("from 0:35 to 0:45 play out a fight scene, no cuts",
     lambda p, b, s: not cuts_inside(p, 35 + 1.5 * 60 / s.tempo, 45 - 1.5 * 60 / s.tempo)),
    ("fast cuts on the drop, slow and calm in the outro",
     lambda p, b, s: avg_scene(p, *span(s, "drop")) < avg_scene(p, *span(s, "outro"))),
]


@pytest.fixture(scope="module")
def library(media, edm_song, tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("MONTAGE_DATA_DIR", str(tmp_path_factory.mktemp("obey-data")))
    try:
        song = analyze_song(edm_song)
        footage = _bigger(analyze_footage([media]))
    finally:
        mp.undo()
    return footage, {"128bpm": song, "160bpm": _retempo(song, 160.0)}


@pytest.mark.parametrize("tempo", ["128bpm", "160bpm"])
@pytest.mark.parametrize("style", list(STYLES))
def test_the_edit_does_what_was_asked(library, style, tempo):
    footage, songs = library
    song = songs[tempo]
    base = make_plan(song, footage, style_direction(style), OutputSettings(fps=24, seed=3))
    ignored = []
    for prompt, check in CHECKS:
        plan = make_plan(song, footage, read_prompt(prompt, style_direction(style)), OutputSettings(fps=24, seed=3))
        if not check(plan, base, song):
            ignored.append(prompt)
    assert not ignored, f"{style} at {tempo} ignored: {ignored}"
