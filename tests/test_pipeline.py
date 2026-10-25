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


def test_no_scene_repeats_until_the_footage_runs_out(analysed):
    song, footage = analysed
    plan = make_plan(song, footage, read_prompt("no repeats, fast", style_direction("hype")),
                     OutputSettings(fps=24, seed=5))
    # A freeze frame that just holds the end of the previous cut is a continuation, not a new pick.
    picks = [c for i, c in enumerate(plan.cuts) if not (c.freeze and i and plan.cuts[i - 1].shot == c.shot)]
    groups = [footage.shots[c.shot].group for c in picks]
    n_scenes = len({s.group for s in footage.usable_shots()})
    first_repeat = next((i for i, g in enumerate(groups) if g in groups[:i]), len(groups))
    assert first_repeat >= min(n_scenes, len(groups))
    assert all(a != b for a, b in zip(groups, groups[1:])), "a scene played back to back"
    if len(groups) > n_scenes:
        assert plan.warnings, "the user should be told the footage is too short"


def test_timed_instruction_starts_a_fast_section_on_the_beat(analysed):
    song, footage = analysed
    d = read_prompt("at 0:12 quick edits of fighting", style_direction("clean"))
    assert [(s.start, s.cut_every, s.energy) for s in d.sections] == [(12.0, 1.0, 1.0)]  # quick = every beat
    assert d.style.cut_every == STYLES["clean"].cut_every, "a timed sentence must not change the whole edit"
    plan = make_plan(song, footage, d, OutputSettings(fps=24, seed=6))
    starts = [c.f0 / 24 for c in plan.cuts]
    assert min(abs(t - 12.0) for t in starts) <= 60 / song.tempo * 0.6
    inside = [c for c in plan.cuts if 12.0 <= c.f0 / 24 < 18.0]
    outside = [c for c in plan.cuts if 2.0 <= c.f0 / 24 < 8.0]
    avg = lambda cs: sum(c.frames for c in cs) / max(1, len(cs))
    assert avg(inside) < avg(outside)


def test_user_drop_position_wins(analysed):
    song, footage = analysed
    d = read_prompt("the drop is at 0:14", style_direction("hype"))
    assert d.drops == [14.0]
    plan = make_plan(song, footage, d, OutputSettings(fps=24, seed=7))
    at = [c for c in plan.cuts if abs(c.f0 / 24 - 14.0) < 0.3]
    assert at and any(e.kind in ("flash", "fade_white") for c in at for e in c.effects)


def test_drops_land_on_the_right_beat(edm_song):
    song = analyze_song(edm_song)
    beat = 60 / 128
    assert len(song.drops) == 2, song.drops
    for found, true in zip(song.drops, [30.0, 90.0]):
        assert abs(found - true) < beat / 2, (found, true)
    assert song.section_at(30.5).label == "drop" and song.section_at(90.5).label == "drop"


def test_one_shot_section_is_held_without_cuts(analysed):
    from montage_director.direction import SectionDirection

    song, footage = analysed
    d = style_direction("hype")
    shot = footage.usable_shots()[0].id
    d.sections = [SectionDirection(start=0.0, end=9.0, cut_every=8.0, shots=[shot])]
    plan = make_plan(song, footage, d, OutputSettings(fps=24, seed=8))
    first = plan.cuts[0]
    assert first.shot == shot and first.f1 >= int(8.5 * 24), "the opening should be one held shot"
    assert first.speed < 1.0, "a 4 s shot held for ~9 s is slowed down instead of running into the next scene"


def test_clean_beat_sync_is_cuts_only_on_every_beat(analysed):
    song, footage = analysed
    beat = 60 / song.tempo
    for prompt, beats_per_cut in (("", 1), ("cut every 2 beats", 2)):
        plan = make_plan(song, footage, read_prompt(prompt, style_direction("clean")), OutputSettings(fps=24, seed=9))
        assert not any(c.effects for c in plan.cuts), "Clean must not add effects"
        assert all(c.speed == 1 and not c.reverse and not c.freeze for c in plan.cuts)
        assert plan.look.grade == "none" and plan.look.glow == 0
        first, last = song.beats[0], song.beats[-1]
        inner = [c for c in plan.cuts if first + 0.1 < c.f0 / 24 < last - 2 * beat]
        on_rate = [c for c in inner if abs(c.frames / 24 / beat - beats_per_cut) < 0.25]
        assert len(on_rate) >= 0.9 * len(inner), (prompt, len(on_rate), len(inner))


def test_ai_style_off_grid_times_land_on_the_beat(analysed):
    """The AI can't hear the song: its section starts, drops and moments are rough guesses.
    The planner must put all of them on the beat grid, and shots must not run into the next scene."""
    import numpy as np

    from montage_director.direction import EFFECTS, Moment, SectionDirection

    song, footage = analysed
    d = style_direction("hype")
    shots = [s.id for s in footage.usable_shots()]
    for a, b in ((0, 5.3), (5.3, 12.7), (12.7, song.duration)):
        d.sections.append(SectionDirection(start=a, end=b, cut_every=1.0, shots=list(shots),
                                           effects={k: 0.5 for k in EFFECTS}))
    d.drops = [8.31]                       # the real drop is at 8.0
    d.moments = [Moment(10.27, "flash"), Moment(14.6, "cut")]
    plan = make_plan(song, footage, d, OutputSettings(fps=24, seed=10))
    beats = np.array(song.beats)
    grid = np.concatenate([beats, (beats[:-1] + beats[1:]) / 2])
    for c in plan.cuts[1:]:
        t = c.f0 / 24
        if beats[0] < t < beats[-1]:
            assert np.min(np.abs(grid - t)) <= 1 / 24 + 0.01, f"cut at {t:.3f}s is off the beat"
    for c in plan.cuts:
        s = footage.shots[c.shot]
        if not c.freeze:
            assert c.src_start + c.frames / 24 * c.speed <= s.end + 0.01 or c.speed <= 0.25


def test_the_app_hears_the_band(band_song):
    song = analyze_song(band_song)
    beat = 60 / song.tempo
    found = {k: [e for e in song.events if e["kind"] == k] for k in
             ("intro", "fill", "drums_faster", "stop", "solo", "hits", "ringout")}
    assert found["intro"] and abs(found["intro"][0]["end"] - 8.0) < 1.0
    assert any(abs(e["time"] - 15.5) < 0.3 for e in found["fill"])
    assert any(abs(e["time"] - 24.0) < 2 * beat for e in found["drums_faster"])
    assert any(abs(e["time"] - 37.0) < 0.5 for e in found["stop"])
    assert any(abs(e["time"] - 40.0) < 2.5 and abs(e["end"] - 56.0) < 2.5 for e in found["solo"])
    hits = found["hits"][0]["times"]
    assert 4 <= len(hits) <= 5 and all(abs(a - b) < 0.1 for a, b in zip(hits, (56.0, 56.5, 57.0, 57.5)))
    assert any(abs(e["time"] - 68.0) < 0.5 for e in found["ringout"])


def test_prompt_timeline_lands_on_what_the_app_heard(band_song, media):
    song, footage = analyze_song(band_song), analyze_footage([media])
    prompt = ("the beginning should have a still shot for the length of those cymbals and as soon as the bass drops "
              "the action should happen. around 0:39 when the guitar solo starts slower cuts, then around 0:55 the "
              "hard drums hitting 1,2,3,4 i want edits coinciding with those. at the end when the note rings a held shot")
    plan = make_plan(song, footage, read_prompt(prompt, style_direction("hype")), OutputSettings(fps=24, seed=11))
    starts = [c.f0 / 24 for c in plan.cuts]
    assert plan.cuts[0].f1 / 24 > 7.0, "the cymbal intro is one held shot"
    for hit in (56.0, 56.5, 57.0, 57.5):
        assert min(abs(t - hit) for t in starts) < 0.1, f"no cut on the hard hit at {hit}"
    solo = [c for c in plan.cuts if 41 <= c.f0 / 24 < 55]
    drop = [c for c in plan.cuts if 9 <= c.f0 / 24 < 23]
    assert sum(c.frames for c in solo) / len(solo) > 1.5 * sum(c.frames for c in drop) / len(drop)
    assert plan.cuts[-1].f0 / 24 <= 68.5, "the ending is held from the moment the last note rings"
    assert any("solo" in n for n in plan.timeline)


def test_refining_one_region_changes_only_that_region(analysed):
    from montage_director.project import Project, Settings

    song, footage = analysed
    proj = Project(settings=Settings(style="hype", seed=12))
    proj.song, proj.footage = song, footage
    proj.direct()
    before = proj.make_plan()
    proj.refine_region("hold one shot", 10.0, 15.0)
    after = proj.make_plan()
    fps = after.fps
    inside = [c for c in after.cuts if 10.0 <= c.f0 / fps < 14.9]
    assert len(inside) == 1, "the selected part should be one held shot"
    outside = lambda plan: [(c.f0, c.shot) for c in plan.cuts if c.f1 / plan.fps <= 9.5]
    assert outside(before) == outside(after), "parts before the selection must not change"
    assert proj.remove_region(12.0) and not proj.direction.regions


def test_tapped_cuts_snap_to_hits_and_replace_auto_cuts(band_song, media):
    from montage_director.project import Project, Settings

    song, footage = analyze_song(band_song), analyze_footage([media])
    proj = Project(settings=Settings(style="hype", seed=13))
    proj.song, proj.footage = song, footage
    proj.direct()
    auto = proj.make_plan()
    # Someone tapping along by ear: a little late on each of the four hard hits, plus a flash.
    proj.taps = [(56.07, "cut"), (56.55, "cut"), (57.04, "cut"), (57.56, "cut"), (56.09, "flash")]
    proj.apply_taps()
    plan = proj.make_plan()
    fps = plan.fps
    starts = [c.f0 / fps for c in plan.cuts]
    for hit in (56.0, 56.5, 57.0, 57.5):  # snapped onto the real hits
        assert min(abs(t - hit) for t in starts) <= 1 / fps + 0.02, hit
    inside = [t for t in starts if 56.0 - 0.05 <= t <= 57.5 + 0.05]
    assert len(inside) == 4, f"only the tapped cuts inside the tapped stretch, got {inside}"
    flashes = [(c.f0 + e.frame) / fps for c in plan.cuts for e in c.effects if e.kind == "flash"]
    assert any(abs(t - 56.0) <= 1 / fps + 0.02 for t in flashes)
    early = lambda p: [(c.f0, c.shot) for c in p.cuts if c.f1 / fps <= 40.0]
    assert early(auto) == early(plan), "cuts far from the taps must not change"
    proj.clear_taps()
    assert proj.make_plan().cuts[0].f0 == 0 and not proj.direction.tap_cuts


def test_bass_drops_falls_back_to_the_beat_coming_in(band_song, media):
    song, footage = analyze_song(band_song), analyze_footage([media])
    song.drops = []  # a song where the beat comes in gradually: no dramatic drop
    d = read_prompt("a still shot during the intro and as soon as the bass drops the action should happen",
                    style_direction("hype"))
    plan = make_plan(song, footage, d, OutputSettings(fps=24, seed=14))
    assert not plan.warnings or "bass drop" not in plan.warnings[0]
    intro_end = song.events_of("intro")[0]["end"]
    assert any(abs(c.f0 / 24 - intro_end) < 0.1 for c in plan.cuts), "the action starts when the beat comes in"
