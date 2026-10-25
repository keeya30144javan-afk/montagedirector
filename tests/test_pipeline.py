import json
import re
import subprocess
from types import SimpleNamespace

import pytest

from montage_director.direction import STYLES, read_prompt, style_direction
from montage_director.director_ai import EDIT_SCHEMA, to_direction
from montage_director.ffmpeg_tools import ffmpeg_exe, probe
from montage_director.footage import analyze_footage
from montage_director.music import analyze_song
from montage_director.planner import OutputSettings, make_plan
from montage_director.render import PREVIEW, Renderer


@pytest.fixture
def analysed(media):
    return analyze_song(media / "song.mp3"), analyze_footage([media])


def test_analysis_finds_shots_beats_and_the_drop(analysed):
    song, footage = analysed
    assert 110 <= song.tempo <= 130 or 55 <= song.tempo <= 65 or 230 <= song.tempo <= 250
    assert len(footage.sources) == 2 and len(footage.shots) >= 6
    assert any(abs(d - 8.0) < 1.5 for d in song.drops), song.drops
    assert any(not s.usable for s in footage.shots), "the black scene should be flagged"


@pytest.mark.parametrize("style", list(STYLES))
def test_every_style_makes_a_frame_exact_plan(analysed, style):
    song, footage = analysed
    plan = make_plan(song, footage, style_direction(style), OutputSettings(fps=24, seed=1))
    plan.validate()
    assert plan.frames == round(song.duration * 24)
    assert all(footage.shots[c.shot].usable for c in plan.cuts)


def test_prompt_keywords(analysed):
    d = read_prompt("slow intro, then go crazy on the drop, vertical, freeze at 0:12, black and white",
                    style_direction("hype"))
    assert d.aspect == "9:16" and d.look.grade == "mono"
    assert any(m.action == "freeze" and m.time == 12 for m in d.moments)
    assert d.style.cut_every["intro"] > STYLES["hype"].cut_every["intro"]


def test_ai_plan_conversion_ignores_bad_ids(analysed):
    song, footage = analysed
    raw = {"summary": "s", "style": "glitch", "look": {"grade": "vhs", "glow": 2, "trails": 0, "letterbox": True, "grain": 0.3},
           "sections": [{"start": 0, "end": 8, "cut_every_beats": 3.1, "intensity": 0.2, "shots": [0, 999],
                         "effects": {k: 0.5 for k in ("flash", "zoom", "shake", "rgb", "slowmo", "reverse", "invert")}, "note": "n"}],
           "moments": [{"time": 9, "action": "flash", "duration": 0}, {"time": 1, "action": "explode", "duration": 0}],
           "avoid_shots": [1, 12345]}
    d = to_direction(raw, footage, 0, song.duration)
    assert d.sections[0].shots == [0] and d.sections[0].cut_every == 4.0
    assert d.look.glow == 1.0 and d.avoid_shots == [1] and len(d.moments) == 1
    make_plan(song, footage, d, OutputSettings(fps=24, seed=2)).validate()


def test_schema_is_strict_compatible():
    def check(s):
        if s.get("type") == "object":
            assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
            for v in s["properties"].values():
                check(v)
        if s.get("type") == "array":
            check(s["items"])
    check(EDIT_SCHEMA)


def test_render_is_frame_exact_with_music(analysed, tmp_path):
    song, footage = analysed
    plan = make_plan(song, footage, read_prompt("lots of flashes, shake, glitch", style_direction("flash2000s")),
                     OutputSettings(width=640, height=360, fps=24, seed=3, song_start=4, song_end=16))
    out = Renderer().render(plan, tmp_path / "edit.mp4", PREVIEW)
    info = probe(out)
    assert info.has_audio and abs(info.duration - 12.0) < 0.1
    log = subprocess.run([ffmpeg_exe(), "-i", str(out), "-map", "0:v", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    assert int(re.findall(r"frame=\s*(\d+)", log)[-1]) == plan.frames
