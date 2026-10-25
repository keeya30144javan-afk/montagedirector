"""The Free Director's prompt reader on the kind of things people actually type."""

import pytest

from montage_director.direction import STYLES, style_direction
from montage_director.reader import normalise, read_prompt


def read(prompt, style="hype"):
    return read_prompt(prompt, style_direction(style))


def test_times_written_out_and_ranges():
    assert "1:30" in normalise("around 1 minute 30 the drop hits")
    assert "1:34" in normalise("at 94 seconds freeze frame")
    assert "0:30~0:45" in normalise("from 0:30 to 0:45 slow motion")
    assert "0:30~0:45" in normalise("0:30-0:45 quick cuts")
    assert "@tail20" in normalise("last 20 seconds fade to black")
    d = read("from 0:30 to 0:45 quick cuts")
    assert [(s.start, s.end, s.cut_every) for s in d.sections] == [(30.0, 45.0, 1.0)]
    d = read("hold one shot until 0:11")
    assert [(s.start, s.end, s.hold) for s in d.sections] == [(0.0, 11.0, True)]


def test_negation_and_amounts():
    base = STYLES["hype"]
    d = read("not too fast, keep it chill")
    assert all(d.style.cut_every[k] >= base.cut_every[k] for k in base.cut_every)
    assert d.style.cut_every["drop"] > base.cut_every["drop"]
    d = read("don't go crazy with the effects, just a few flashes")
    assert d.style.cut_every == base.cut_every, "talking about effects must not change the pace"
    assert d.style.effects["drop"]["shake"] < base.effects["drop"]["shake"]
    assert 0 < d.style.flash_top < base.flash_top
    d = read("no shake please, I hate shaky edits")
    assert all(fx["shake"] == 0 for fx in d.style.effects.values())
    d = read("less flashes")
    assert 0 < d.style.flash_top < base.flash_top
    d = read("dont use slow motion")
    assert all(fx["slowmo"] == 0 for fx in d.style.effects.values())
    assert d.style.cut_every == base.cut_every


def test_intensity():
    base = STYLES["clean"].cut_every["verse"]
    assert read("a little faster", "sad").style.cut_every["verse"] == 2.0  # sad verse 4 -> 2
    assert read("super fast cuts", "sad").style.cut_every["verse"] == 1.0  # two steps
    assert read("make it hype af", "clean").style.cut_every["verse"] == 0.5 <= base


def test_parts_of_the_song_and_the_rest():
    d = read("intro should be slow and the rest should be fast")
    assert d.style.cut_every["intro"] > d.style.cut_every["verse"]
    d = read("every 4 beats in the verse and every beat in the chorus")
    assert d.style.cut_every["verse"] == 4.0 and d.style.cut_every["drop"] == 1.0
    d = read("first chorus calm, last chorus crazy")
    assert [(s.cue, s.occurrence) for s in d.sections] == [("sec:drop", 1), ("sec:drop", -1)]
    d = read("the second drop should go insane")
    assert [(s.cue, s.occurrence, s.cut_every) for s in d.sections] == [("drop", 2, 0.5)]
    d = read("halfway through switch to slow emotional shots")
    assert d.sections[0].span == "half" and d.sections[0].cut_every >= 4


def test_what_to_show():
    d = read("use dark scenes for the intro and bright explosions for the drop")
    assert d.prefer["intro"]["bright"] < 0 and d.prefer["drop"]["bright"] > 0
    d = read("show close ups of faces during the verse")
    assert d.prefer == {"verse": {"motion": -1.0}}
    d = read("at 2:00 i want explosions and at 2:20 switch to dark moody scenes")
    assert [s.start for s in d.sections] == [120.0, 140.0]
    assert d.sections[0].prefer["bright"] > 0 and d.sections[1].prefer["bright"] < 0


def test_drums():
    assert read("cut on every kick drum").sync == {"all": "kick"}
    assert read("keep the cuts on the snare").sync == {"all": "snare"}
    d = read("flash on every snare")
    assert d.drum_fx == [{"effect": "flash", "drum": "snare", "label": None}]
    d = read("slowmo on the big hits")
    assert d.drum_fx == [{"effect": "slowmo", "drum": "big", "label": None}] and not d.sections


def test_moments_and_endings():
    d = read("end it on a freeze frame")
    assert [(m.time, m.action) for m in d.moments] == [(-1.0, "freeze")]
    d = read("last 20 seconds fade to black")
    assert [(m.time, m.action) for m in d.moments] == [(-1.0, "fade_black")]
    d = read("at 94 seconds freeze frame")
    assert [(m.time, m.action) for m in d.moments] == [(94.0, "freeze")]


def test_build_ups_and_velocity():
    d = read("build up the tension before the drop with faster and faster cuts")
    assert [(s.cue, s.ramp) for s in d.sections] == [("predrop", True)]
    assert d.style.cut_every == STYLES["hype"].cut_every
    assert read("make it a velocity edit").velocity


def test_the_long_timeline_prompt():
    prompt = ("i want the length of the edit to be the length of the song, i want it to cut to different scenes and "
              "flow with the groove of the song, the beginning should have a still shot for the length of those "
              "cymbals and as soon as the bass drops the action should happen. the part at 1:36 when the drums start "
              "getting faster and more frantic, id like some more quicker cuts with some punches being thrown, and "
              "when 2:30 starts we can go back to the melody, around 2:50 when the guitar solo starts id like to see "
              "some slower cuts and then around 3:09 when the song picks back up we can go back to fast fight scenes, "
              "then at 3:29 we follow get into the flow again, then around 3:57 the music has some hard drums, "
              "hitting 1,2,3,4 i want some edits coinciding with those. and then at the end of the song when the note "
              "ends and rings a panning shot of vegeta hitting a pose")
    d = read(prompt)
    got = [(s.start, s.cue, s.hold, s.sync_hits, s.cut_every) for s in d.sections]
    assert got == [(-1.0, "intro", True, False, 8.0), (-1.0, "drop", False, False, 1.0),
                   (96.0, "drums_faster", False, False, 1.0), (150.0, None, False, False, 2.0),
                   (170.0, "solo", False, False, 4.0), (189.0, None, False, False, 1.0),
                   (209.0, None, False, False, 2.0), (237.0, "hits", False, True, 1.0),
                   (-1.0, "ringout", True, False, 8.0)], got
    assert d.sections[2].energy == 1.0, "punches being thrown = action shots"
    assert d.sections[-1].camera == "pan"
    assert not d.unclear, d.unclear


@pytest.mark.parametrize("prompt", ["the intro is way too long", "make it look like my cat made it"])
def test_unclear_parts_are_reported(prompt):
    assert read(prompt).unclear == [prompt]
