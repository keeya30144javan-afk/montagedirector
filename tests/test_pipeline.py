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


def test_project_saves_and_reopens_to_the_same_edit(media, tmp_path):
    from montage_director.project import Project, Settings

    proj = Project(footage_paths=[str(media)], song_path=str(media / "song.mp3"),
                   settings=Settings(style="hype", prompt="flash at 0:12", seed=21, api_key="sk-secret"))
    proj.analyse()
    proj.direct()
    proj.refine_region("hold one shot", 10.0, 15.0)
    proj.taps = [(4.0, "cut"), (5.0, "flash")]
    proj.apply_taps()
    before = proj.make_plan()
    saved = proj.save(tmp_path / "my edit")
    assert saved.suffix == ".mdproj" and "sk-secret" not in saved.read_text()

    again = Project.load(saved)
    again.analyse()
    after = again.make_plan()
    assert [(c.f0, c.f1, c.shot) for c in after.cuts] == [(c.f0, c.f1, c.shot) for c in before.cuts]
    assert again.taps == proj.taps and len(again.direction.regions) == 1


def test_undo_and_redo_restore_the_edit(analysed):
    from montage_director.project import Project, Settings

    song, footage = analysed
    proj = Project(settings=Settings(style="hype", seed=5))
    proj.song, proj.footage = song, footage
    proj.direct()
    first = [(c.f0, c.shot) for c in proj.make_plan().cuts]
    proj.checkpoint()
    proj.refine_region("hold one shot", 8.0, 14.0)
    proj.checkpoint()
    proj.reroll()
    changed = [(c.f0, c.shot) for c in proj.make_plan().cuts]
    assert proj.undo() and proj.undo() and not proj.undo()
    assert [(c.f0, c.shot) for c in proj.make_plan().cuts] == first
    assert proj.redo() and proj.redo() and not proj.redo()
    assert [(c.f0, c.shot) for c in proj.make_plan().cuts] == changed


def test_export_presets_set_size_and_file_limit(media, tmp_path, monkeypatch):
    from montage_director import project as project_mod
    from montage_director.project import Project, Settings

    proj = Project(footage_paths=[str(media)], song_path=str(media / "song.mp3"),
                   settings=Settings(style="clean", seed=3, song_end=6.0))
    proj.analyse()
    proj.direct()
    proj.make_plan()
    out = proj.export(tmp_path / "tiktok.mp4", "tiktok")
    info = probe(out)
    assert (info.width, info.height) == (1080, 1920)

    monkeypatch.setitem(project_mod.EXPORT_PRESETS, "discord",
                        ("small", project_mod.EXPORT, None, None, 0.5))  # 0.5 MB for a 6 s clip
    out = proj.export(tmp_path / "small.mp4", "discord")
    assert out.stat().st_size < 0.5 * 1024 * 1024


def _write_song(path, bpm, seconds, drums=True):
    import numpy as np
    import imageio_ffmpeg

    sr = 22050
    t = np.arange(int(sr * seconds)) / sr
    y = 0.15 * np.sin(2 * np.pi * 220 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * t / 8))
    beat = 60 / bpm
    if drums:
        rng = np.random.default_rng(0)
        for i in range(int(seconds / beat)):
            s = int(i * beat * sr)
            n = min(int(0.12 * sr), len(t) - s)
            tt = np.arange(n) / sr
            if i % 2 == 0:
                y[s:s + n] += 0.9 * np.sin(2 * np.pi * (50 + 90 * np.exp(-25 * tt)) * tt) * np.exp(-20 * tt)
            else:
                y[s:s + n] += 0.5 * rng.normal(0, 1, n) * np.exp(-30 * tt)
    y = (y / np.abs(y).max()).astype(np.float32)
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "f32le", "-ar", str(sr),
                    "-ac", "1", "-i", "-", str(path)], input=y.tobytes(), check=True)
    return path


def test_pacing_follows_the_feel_of_the_song(media, tmp_path):
    import copy

    footage = analyze_footage([media])
    calm = analyze_song(_write_song(tmp_path / "pad.wav", 90, 30, drums=False))
    assert not calm.feel["steady"], calm.feel
    base = analyze_song(media / "song.mp3")
    fast = copy.deepcopy(base)
    fast.feel = dict(base.feel, rate_scale=2.0, pace="fast")  # as for a song heard at 150+ BPM

    def avg(song, prompt=""):
        plan = make_plan(song, footage, read_prompt(prompt, style_direction("hype")), OutputSettings(fps=30, seed=1))
        inner = [c.frames / 30 for c in plan.cuts[1:-1]]
        return sum(inner) / len(inner)

    assert avg(fast) > 1.4 * avg(base), "fast songs: longer cuts in beats"
    assert avg(calm) >= 0.85, "no drum beat: never faster than about a cut a second"
    # what the user asks for exactly is kept exactly
    assert abs(avg(fast, "cut on every beat") - avg(base, "cut on every beat")) < 0.05


def test_ai_can_cut_on_drums_and_put_effects_on_them(analysed):
    song, footage = analysed
    raw = {"summary": "s", "style": "hype", "look": {"grade": "none", "glow": 0, "trails": 0, "letterbox": False, "grain": 0},
           "sections": [{"start": 0, "end": 8, "cut_every_beats": 2, "intensity": 0.3, "shots": [], "effects": {},
                         "cut_on": "beat", "speed_up": True, "velocity": False, "effects_on_drums": [], "note": ""},
                        {"start": 8, "end": 20, "cut_every_beats": 1, "intensity": 1, "shots": [], "effects": {},
                         "cut_on": "kick", "speed_up": False, "velocity": False,
                         "effects_on_drums": [{"effect": "flash", "drum": "kick"}, {"effect": "explode", "drum": "kick"}],
                         "note": ""}],
           "moments": [], "avoid_shots": [], "repeats": "rarely", "drops": [8.0]}
    d = to_direction(raw, footage, 0, song.duration)
    a, b = d.sections
    assert a.ramp and not a.sync_hits and b.sync_hits and b.sync_on == "kick" and b.drum_fx == {"flash": "kick"}
    plan = make_plan(song, footage, d, OutputSettings(fps=24, seed=3))
    starts = [c.f0 / 24 for c in plan.cuts if 8.5 <= c.f0 / 24 < 19]
    kicks = [k for k in song.kicks if 8.5 <= k < 19]
    assert kicks and sum(1 for t in starts if min(abs(t - k) for k in kicks) <= 1 / 24 + 0.01) >= 0.8 * len(starts)
    flashes = [(c.f0 + e.frame) / 24 for c in plan.cuts for e in c.effects if e.kind == "flash" and c.f0 / 24 >= 8.5]
    assert len(flashes) >= 0.6 * len(kicks)


def test_freeze_in_a_selected_part(analysed):
    from montage_director.project import Project, Settings

    song, footage = analysed
    proj = Project(settings=Settings(style="hype", seed=4))
    proj.song, proj.footage = song, footage
    proj.direct()
    before = proj.make_plan()
    proj.refine_region("freeze", 12.0, 15.0)
    plan = proj.make_plan()
    assert any(c.freeze and abs(c.f0 / plan.fps - 12.0) < 0.3 for c in plan.cuts)
    early = lambda p: [(c.f0, c.shot) for c in p.cuts if c.f1 / p.fps <= 11.0]
    assert early(before) == early(plan), "a freeze-only edit must not re-cut the rest"


def test_pick_a_shot_for_the_final_note(analysed):
    from montage_director.project import Project, Settings

    song, footage = analysed
    proj = Project(settings=Settings(style="hype", seed=4))
    proj.song, proj.footage = song, footage
    proj.direct()
    shot = footage.usable_shots()[-1].id
    proj.pick_region_shot(14.07, 20.0, shot, freeze=True)  # starts just off the beat grid
    plan = proj.make_plan()
    last = [c for c in plan.cuts if c.f0 / plan.fps >= 14.0]
    assert len(last) == 1 and last[0].shot == shot and last[0].freeze
    assert abs(last[0].f0 / plan.fps - 14.07) <= 1 / plan.fps + 1e-6, "the part starts exactly where it was selected"


def test_shots_for_my_taps_in_a_selected_part(band_song, media):
    from montage_director.project import Project, Settings

    song, footage = analyze_song(band_song), analyze_footage([media])
    proj = Project(settings=Settings(style="hype", seed=13))
    proj.song, proj.footage = song, footage
    proj.direct()
    proj.taps = [(56.07, "cut"), (56.55, "cut"), (56.8, "cut"), (57.04, "cut"), (57.56, "cut")]
    proj.apply_taps()
    # "I like these 4 but not the one in the middle"
    assert proj.remove_taps([56.8]) == 1 and len(proj.taps) == 4
    tapped = proj.make_plan()
    fps = tapped.fps
    starts = lambda p: [c.f0 for c in p.cuts]
    notes = proj.refine_region("put a scene where a character is powering up/yelling for every manual marker/cut "
                               "in this highlighted section", 55.5, 58.5)
    assert notes and proj.direction.regions[-1].keep_cuts
    plan = proj.make_plan()
    assert starts(plan) == starts(tapped), "changing what's shown must keep every cut where it was"
    # several shots picked by hand: used in order on those cuts
    picks = [s.id for s in footage.usable_shots()[:3]]
    proj.pick_region_shot(55.9, 58.0, picks)
    plan = proj.make_plan()
    assert starts(plan) == starts(tapped)
    inside = [c.shot for c in plan.cuts if 55.9 <= c.f0 / fps < 58.0]
    assert inside[:3] == picks, inside


def test_shot_descriptions_marks_scene_keys_transitions_and_text(analysed):
    from montage_director.project import Project, Settings

    song, footage = analysed
    proj = Project(settings=Settings(style="hype", seed=6))
    proj.song, proj.footage = song, footage
    usable = [s.id for s in footage.usable_shots()]
    group, nappa, banned, fav = usable[1], usable[2], usable[3], usable[4]
    # as if "Describe my shots" had run
    proj.shot_tags = {i: {"desc": "a fighter alone", "kind": "action", "people": 1} for i in usable}
    proj.shot_tags[group] = {"desc": "the whole team standing together", "kind": "character", "people": 6}
    proj.shot_tags[nappa] = {"desc": "Nappa laughing", "kind": "character", "people": 1}
    proj.direct()
    proj.refine_region("a shot with as many characters as possible", 16.0, 20.0)
    proj.mark_shot(banned, "banned")
    proj.mark_shot(fav, "favorite")
    proj.taps = [(9.0, "scene1"), (10.0, "cut")]
    proj.exact_taps = [10.0]
    proj.apply_taps()
    proj.texts = [{"text": "Saiyan Pride", "time": 2.0, "duration": 2.0, "style": "title"}]
    proj.direction.transitions["all"] = "crossfade"
    plan = proj.make_plan()
    fps = plan.fps
    end = [c for c in plan.cuts if c.f0 / fps >= 16.0]
    assert end and end[0].shot == group, "the ending holds the group shot"
    assert banned not in {c.shot for c in plan.cuts}
    assert fav in {c.shot for c in plan.cuts}
    assert any(c.f0 == round(10.0 * fps) for c in plan.cuts), "an exact tap is not snapped"
    texts = [(c.f0 + e.frame) / fps for c in plan.cuts for e in c.effects if e.kind == "text"]
    assert texts and abs(min(texts) - 2.0) < 0.05
    assert any(c.transition for c in plan.cuts)
    # names in the descriptions
    d = read_prompt("show nappa in the intro", style_direction("hype"))
    proj.direction = d
    plan = proj.make_plan()
    intro = [c.shot for c in plan.cuts if c.f1 / fps <= 8.0]
    assert nappa in intro
    proj.render_preview()


def test_a_highlighted_part_really_changes(analysed):
    from montage_director.project import Project, Settings

    song, footage = analysed
    A, B = 10.0, 18.0

    def run(text):
        proj = Project(settings=Settings(style="hype", seed=4))
        proj.song, proj.footage = song, footage
        proj.direct()
        if text:
            proj.refine_region(text, A, B)
        plan = proj.make_plan()
        cuts = [c for c in plan.cuts if A <= c.f0 / plan.fps < B - 0.1]
        flashes = sum(1 for c in cuts for e in c.effects if e.kind == "flash")
        return sum(c.frames for c in cuts) / len(cuts) / plan.fps, flashes

    base, base_fl = run("")
    quick, _ = run("quicker cuts")
    slow, _ = run("slower cuts")
    _, more_fl = run("more flashes")
    assert quick < 0.75 * base, (quick, base)
    assert slow > 1.5 * base, (slow, base)
    assert more_fl >= base_fl + 2, (more_fl, base_fl)


def test_look_alike_shots_are_kept_apart(analysed):
    """Two shots that look almost the same (colours, layout) aren't put back to back."""
    import copy

    import numpy as np

    from montage_director.planner import _Planner

    song, footage = analysed
    footage = copy.deepcopy(footage)
    a, b = footage.shots[0], footage.shots[1]
    b.sig = list(a.sig)  # make shot 1 look exactly like shot 0 (different scenes, same picture)
    p = _Planner(song, footage, style_direction("hype"), OutputSettings(fps=24, seed=3))
    row = p.look_alike(a.id)
    assert row[b.id] == 1.0 and row[a.id] == 1.0
    assert min(row) < 0.5, "different-looking shots don't count"

    def side_by_side():
        ids = [c.shot for c in make_plan(song, footage, style_direction("hype"), OutputSettings(fps=24, seed=3)).cuts]
        return sum({x, y} == {a.id, b.id} for x, y in zip(ids, ids[1:]))

    now = side_by_side()
    real = _Planner.look_alike
    try:
        _Planner.look_alike = lambda self, i: np.zeros(len(self.footage.shots))
        without = side_by_side()
    finally:
        _Planner.look_alike = real
    assert now <= 1 and now < without, (now, without)


def test_preview_of_one_part(analysed, tmp_path, monkeypatch):
    """Preview part: only the cuts in the selection, frame-exact, with the music from that point."""
    from montage_director.project import Project, Settings

    monkeypatch.setenv("MONTAGE_DATA_DIR", str(tmp_path))
    song, footage = analysed
    proj = Project(settings=Settings(style="hype", seed=14))
    proj.song, proj.footage = song, footage
    proj.direct()
    plan = proj.make_plan()
    part = proj.part_plan(plan.song_start + 6.0, plan.song_start + 9.0)
    part.validate()
    first = next(c for c in plan.cuts if c.f1 > 6.0 * plan.fps)
    assert part.cuts[0].shot == first.shot and part.cuts[0].f0 == 0
    assert abs(part.song_start - (plan.song_start + first.f0 / plan.fps)) < 0.01
    assert 3.0 <= part.duration <= 3.0 + 2 * max(c.frames for c in plan.cuts) / plan.fps
    path, offset = proj.render_part(plan.song_start + 6.0, plan.song_start + 9.0)
    assert abs(probe(path).duration - part.duration) < 0.1 and abs(offset - first.f0 / plan.fps) < 0.01
