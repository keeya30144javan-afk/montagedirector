"""Turn a Direction + SongMap + Footage into a frame-exact EditPlan."""

from __future__ import annotations

import bisect
import colorsys
import re
import copy
import logging
import random
from pathlib import Path
from dataclasses import dataclass

import numpy as np

from .direction import CUT_CHOICES, Direction, SectionDirection
from .footage import Footage, Shot
from .music import SongMap
from .plan import Cut, EditPlan, Effect

log = logging.getLogger(__name__)


@dataclass
class OutputSettings:
    width: int = 1920
    height: int = 1080
    fps: int = 30
    fit: str = "fill"          # fill | fit | blur
    song_start: float = 0.0
    song_end: float | None = None
    seed: int | None = None


def make_plan(song: SongMap, footage: Footage, direction: Direction, out: OutputSettings) -> EditPlan:
    return _Planner(song, footage, direction, out).build()


class _Planner:
    def __init__(self, song: SongMap, footage: Footage, direction: Direction, out: OutputSettings):
        self.song, self.footage, self.d, self.out = song, footage, direction, out
        self.rng = random.Random(out.seed)
        self.fps = out.fps
        self.S = max(0.0, min(out.song_start, song.duration - 1.0))
        end = song.duration if out.song_end is None else min(out.song_end, song.duration)
        self.E = max(self.S + 1.0, end)
        self.total = int(round((self.E - self.S) * self.fps))
        self.beat = 60.0 / song.tempo if song.tempo else 0.5
        self.avoid = set(direction.avoid_shots) | set(direction.banned)
        self.favs = set(direction.favorites) - self.avoid
        self.shots = [s for s in footage.usable_shots() if s.id not in self.avoid] or footage.usable_shots()
        self.uses: dict[int, int] = {}
        self.used_windows: dict[int, list[tuple[float, float]]] = {}
        self.order_pos: dict[int, int] = {}
        self.order_used: dict[int, set[int]] = {}
        # Scene groups: pieces of one take and near-duplicate moments count as the same scene.
        self.group_uses: dict[int, int] = {}
        self.group_last: dict[int, int] = {}
        self.cut_i = 0
        self.n_groups = len({s.group for s in self.shots}) or 1
        self.beats_a = np.asarray(song.beats, dtype=float)
        self.down_a = np.asarray(song.downbeats, dtype=float)
        # Everything a director times by hand is put on the song's real grid: the AI
        # can't hear the music, so its "1:36" may fall between two beats.
        self.d = direction = copy.deepcopy(direction)
        self.exact = list(self.d.tap_exact)
        self.tap_cuts = sorted({round(self.snap_tap(t), 3) for t in self.d.tap_cuts})
        self.scene_taps = {}
        for sc in self.d.tap_scenes:
            self.scene_taps[self.frame_of(self.snap_tap(sc["time"]))] = sc
        for m in self.d.moments:
            if m.tapped:
                m.time = self.snap_tap(m.time)
                continue
            if m.time <= -2:  # "start with a fade in"
                m.time = self.S
            elif m.time < 0:  # "...then fade to black" at the very end
                m.time = max(self.S, self.E - (m.duration or 1.5))
            elif m.action != "fade_black":
                m.time = self.snap(m.time, hit=m.action in ("flash", "rgb", "invert", "fade_white"))
        if direction.drops is None:
            self.drops = list(song.drops)
        else:
            snapped = []
            for t in direction.drops:
                near = [d for d in song.drops if abs(d - t) <= 4 * self.beat]
                snapped.append(min(near, key=lambda d: abs(d - t)) if near else self.snap(t, bar=True))
            self.drops = sorted(set(snapped))
        # Pieces of one unbroken take can play on into each other (no visible cut).
        self.take_end: dict[int, float] = {}
        by_take: dict[int, list[Shot]] = {}
        for s in footage.shots:
            by_take.setdefault(s.take, []).append(s)
        def same_scene(a: Shot, b: Shot) -> bool:
            # guards against a scene cut the analysis missed inside a "take"
            if not b.usable or abs(a.end - b.start) > 0.3:
                return False
            if len(a.sig) == len(b.sig) and a.sig:
                return float(np.abs(np.asarray(a.sig, float) - np.asarray(b.sig, float)).mean()) < 40.0
            return True

        for pieces in by_take.values():
            pieces.sort(key=lambda s: s.start)
            end = pieces[-1].end
            for k in range(len(pieces) - 1, -1, -1):
                s = pieces[k]
                if k + 1 < len(pieces) and not same_scene(s, pieces[k + 1]):
                    end = s.end
                self.take_end[s.id] = end
        self.timeline_notes: list[str] = []
        self.hit_aligned = 0
        self.warnings: list[str] = []
        strong = sorted(s for _, s in song.hits)
        big_cut = strong[int(len(strong) * 0.6)] if strong else 1.0
        self.drums = {
            "kick": sorted(song.kicks), "snare": sorted(song.snares), "hits": sorted(song.kicks + song.snares),
            "big": sorted(t for t, st in song.hits if st >= big_cut), "beat": list(song.beats),
            "bar": list(song.downbeats),
        }
        self.features = self._shot_features()
        e = np.asarray(song.energy[int(self.S * song.energy_rate):int(self.E * song.energy_rate)] or [0.5])
        self.energy_q = (float(np.percentile(e, 35)), float(np.percentile(e, 60)))
        self.resolve_sections()
        self.reserved = self._reserve_matches()

    # ----- helpers ----------------------------------------------------------- #
    def frame_of(self, song_t: float) -> int:
        return int(round((song_t - self.S) * self.fps))

    def song_t(self, frame: int) -> float:
        return self.S + frame / self.fps

    def snap_tap(self, t: float) -> float:
        """A tap by ear: lands on the drum hit you meant (within 100 ms), else on the
        nearest beat or half-beat (within 120 ms), else exactly where you tapped."""
        if any(abs(t - x) < 1e-3 for x in getattr(self, "exact", [])):
            return float(t)  # placed exactly by the user
        drums = self.song.kicks + self.song.snares
        if drums:
            d = min(drums, key=lambda x: abs(x - t))
            if abs(d - t) <= 0.10:
                return float(d)
        if len(self.beats_a):
            grid = np.concatenate([self.beats_a, (self.beats_a[:-1] + self.beats_a[1:]) / 2])
            g = float(grid[np.argmin(np.abs(grid - t))])
            if abs(g - t) <= 0.12:
                return g
        return float(t)

    def tap_runs(self) -> list[tuple[float, float]]:
        """Stretches where the user tapped cuts (taps less than 2 bars apart)."""
        runs: list[list[float]] = []
        for t in self.tap_cuts:
            if runs and t - runs[-1][-1] <= 8 * self.beat:
                runs[-1].append(t)
            else:
                runs.append([t])
        return [(r[0], r[-1]) for r in runs]

    def snap(self, t: float, bar: bool = False, hit: bool = False) -> float:
        """Nearest musical moment: a strong drum hit (hit=True, within 120 ms), the start
        of a bar (bar=True, within 1.5 beats), otherwise the nearest beat."""
        if hit:
            close = [h for h in self.hits_near(t) if abs(h[0] - t) <= 0.12 and h[1] >= 0.35]
            if close:
                return float(max(close, key=lambda h: h[1])[0])
        if bar and len(self.down_a):
            d = float(self.down_a[np.argmin(np.abs(self.down_a - t))])
            if abs(d - t) <= 1.5 * self.beat:
                return d
        if len(self.beats_a):
            b = float(self.beats_a[np.argmin(np.abs(self.beats_a - t))])
            if abs(b - t) <= self.beat:
                return b
        return float(t)

    def _shot_features(self) -> dict[int, dict[str, float]]:
        """Brightness, colourfulness, warmth (as ranks 0..1 within this footage) and hue, for
        prompts like "dark scenes", "fire and explosions", "blue shots"."""
        shots = self.shots
        if not shots:
            return {}
        def ranks(vals):
            order = np.argsort(np.argsort(vals))
            return order / max(1, len(vals) - 1)
        bright = ranks(np.array([s.brightness for s in shots]))
        sat = ranks(np.array([s.saturation for s in shots]))
        warm = ranks(np.array([(s.color[0] - s.color[2]) for s in shots], dtype=float))
        motion = ranks(np.array([s.motion for s in shots]))
        out = {}
        for i, s in enumerate(shots):
            r, g, b = (c / 255.0 for c in s.color)
            h, _, sv = colorsys.rgb_to_hsv(r, g, b)
            out[s.id] = {"bright": float(bright[i]), "sat": float(sat[i]), "warm": float(warm[i]), "motion": float(motion[i]),
                         "hue": h * 360.0, "hue_sat": float(sv)}
        return out

    def _reserve_matches(self) -> dict[int, set[str]]:
        """Shots that best match a part of the prompt ("bright explosions for the drop") are
        kept for that part instead of being used up earlier by chance."""
        prefs = [p for p in self.d.prefer.values() if p] + [sd.prefer for sd in self.d.sections if sd.prefer]
        out: dict[int, set[str]] = {}
        for p in prefs:
            key = repr(sorted(p.items()))
            scored = sorted(self.shots, key=lambda s: -self.content_score(s, p))
            for s in scored[:max(3, len(scored) // 4)]:
                if self.content_score(s, p) > 0.3:
                    out.setdefault(s.id, set()).add(key)
        return out

    def overlay_at(self, t: float) -> SectionDirection | None:
        t += 0.5 / self.fps
        for ov in reversed(getattr(self, "overlays", [])):
            if ov.start <= t < ov.end:
                return ov
        return None

    def prefer_at(self, t: float) -> dict[str, float]:
        lab = self.label_at(t)
        pref = dict(self.d.prefer.get("all", {}))
        pref.update(self.d.prefer.get(lab, {}))
        sd = self.sd_at(t)
        if sd and sd.prefer:
            pref.update(sd.prefer)
        ov = self.overlay_at(t)
        if ov and ov.prefer:
            pref.update(ov.prefer)
        return pref

    def content_score(self, s: Shot, pref: dict[str, float]) -> float:
        """How well a shot matches what the prompt asked to see (remembered per shot and request)."""
        if not pref:
            return 0.0
        if pref is not getattr(self, "_pref_obj", None):  # the same request is scored for many shots in a row
            self._pref_obj = pref
            self._pref_key = tuple(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in pref.items()))
        key = (s.id, self._pref_key)
        cache = self.__dict__.setdefault("_score_cache", {})
        if key not in cache:
            cache[key] = self._content_score(s, pref)
        return cache[key]

    def _content_score(self, s: Shot, pref: dict[str, float]) -> float:
        f = self.features.get(s.id)
        if f is None:
            return 0.0
        pref = dict(pref)
        score = 0.0
        for k in ("bright", "sat", "warm", "motion"):
            if k in pref:
                score += pref[k] * (f[k] - 0.5) * 2.0
        if "hue" in pref:
            diff = abs((f["hue"] - pref["hue"] + 180) % 360 - 180)
            score += (np.cos(np.radians(diff)) * min(1.0, f["hue_sat"] * 3) - 0.2) * 1.5
        for k, v in pref.items():
            if k.startswith("camera_") and s.camera == k[7:]:
                score += v
        tag = self.d.shot_tags.get(s.id) if self.d.shot_tags else None
        if tag:
            if "crowd" in pref and tag.get("people") is not None:  # "as many characters as possible"
                score += pref["crowd"] * (min(int(tag["people"]), 6) / 1.5 - 0.6)
            if "solo" in pref and tag.get("people") is not None:   # "focus on one gundam"
                score += pref["solo"] * {0: -0.5, 1: 1.5, 2: 0.3}.get(int(tag["people"]), -0.8)
            words = pref.get("_query")
            if words:  # names and things in the AI's shot descriptions
                desc = (tag.get("desc", "") + " " + tag.get("kind", "")).lower()
                hits = sum(1 for w in words if re.search(r"\b" + re.escape(w[:max(4, len(w) - 2)]), desc))
                score += 2.5 * hits / len(words) - 0.3
        return float(score)

    def drum_times(self, kind: str, a: float, b: float) -> list[float]:
        lst = self.drums.get(kind, [])
        i, j = bisect.bisect_left(lst, a), bisect.bisect_left(lst, b)
        return lst[i:j]

    def sync_at(self, t: float) -> str | None:
        """Which drum the cuts follow here ("kick", "snare", "hits"), or None for the beat grid."""
        sd = self.sd_at(t)
        kind = None
        if sd is not None and sd.sync_hits:
            kind = sd.sync_on
            if sd.hit_times:
                return kind
        elif sd is None or not (sd.hold or sd.ramp or sd.fixed or sd.shots):
            # a song-part instruction ("cut on the kick in the chorus") combines with a timed one
            kind = self.d.sync.get(self.label_at(t)) or self.d.sync.get("all")
        if not kind:
            return None
        # only where that drum is actually playing (else keep the beat grid)
        return kind if len(self.drum_times(kind, t - 2 * self.beat, t + 2 * self.beat)) >= 2 else None

    def ramp_at(self, t: float) -> tuple[float, float] | None:
        sd = self.sd_at(t)
        if sd is not None:
            return (sd.start, sd.end) if sd.ramp else None
        lab = self.label_at(t)
        if lab in self.d.ramp:
            sec = self.song.section_at(t)
            return (sec.start, sec.end)
        return None

    def velocity_at(self, t: float) -> bool:
        sd = self.sd_at(t)
        if sd is not None and (sd.velocity or sd.hold or sd.fixed):
            return sd.velocity
        return self.label_at(t) in self.d.velocity

    def usable_len(self, shot: Shot) -> float:
        """How long this shot can play before the source cuts to another scene. Pieces of
        one unbroken take continue into each other; keep clear of real scene cuts, which
        are only located to within a sixth of a second."""
        return max(0.0, self.take_end.get(shot.id, shot.end) - shot.start - 0.38)

    # ----- the director's timeline -------------------------------------------- #
    def section_runs(self, label: str) -> list[tuple[float, float, None]]:
        runs: list[list[float]] = []
        for sec in self.song.sections:
            if sec.label != label:
                continue
            if runs and abs(runs[-1][1] - sec.start) < 0.05:
                runs[-1][1] = sec.end
            else:
                runs.append([sec.start, sec.end])
        return [(a, b, None) for a, b in runs]

    def cue_events(self, kind: str) -> list[tuple[float, float | None, list | None]]:
        if kind.startswith("sec:"):
            label = kind[4:]
            runs = self.section_runs(label)
            if label == "drop" and not runs:
                runs = [(t, t + 32 * self.beat, None) for t in self.drops]
            return runs
        if kind == "predrop":
            out = []
            for t in self.drops:
                sec = self.song.section_at(max(0.0, t - 0.05))
                start = sec.start if sec.label == "build" and t - sec.start <= 32 * self.beat else t - 16 * self.beat
                out.append((max(0.0, start), t, None))
            return out or self.section_runs("build")
        if kind == "vocals":
            runs = self.section_runs("verse")
            if runs:
                return runs[:1]
            return [(e["end"], None, None) for e in self.song.events_of("intro") if e.get("end")]
        if kind == "ringout":
            evs = [(e["time"], e.get("end"), e.get("times")) for e in self.song.events_of("ringout")]
            return evs or [(max(self.S, self.E - 8 * self.beat), self.E, None)]
        if kind == "drop":
            if self.drops:
                return [(t, None, None) for t in self.drops]
            # No dramatic drop in this song: "when the bass drops" means the beat coming in.
            return [(e["end"], None, None) for e in self.song.events_of("intro") if e.get("end")]
        evs = [(e["time"], e.get("end"), e.get("times")) for e in self.song.events_of(kind)]
        if kind == "intro" and not evs and self.drops:
            evs = [(0.0, self.drops[0], None)]
        return evs

    def resolve_sections(self) -> None:
        """Place cue-based sections ("when the solo starts") on what the app heard, snap the
        rest to the grid, and give every section an end (the next section's start)."""
        from .listening import EVENT_LABELS

        self.overlays: list[SectionDirection] = []  # edits that change what's shown, not where the cuts are
        kept: list[SectionDirection] = []
        last_start = self.S - 1
        expanded = []
        for sd in self.d.sections:  # "before each drop": one section per time it happens
            if sd.cue and sd.occurrence == 0:
                n = len([e for e in self.cue_events(sd.cue) if self.S - 0.01 <= e[0] < self.E])
                for k in range(1, n + 1):
                    c = copy.deepcopy(sd)
                    c.occurrence = k
                    expanded.append(c)
            else:
                expanded.append(sd)
        self.d.sections = expanded
        for sd in self.d.sections:
            sd.cue_end, sd.hit_times = None, None
            if sd.fixed:
                kept.append(sd)
                continue
            if sd.span:
                kind, _, n = sd.span.partition(":")
                mid = self.S + (self.E - self.S) / 2
                third = (self.E - self.S) / 3
                a, b = {"tail": (self.E - float(n or 0), self.E), "head": (self.S, self.S + float(n or 0)),
                        "half": (mid, -1.0), "firsthalf": (self.S, mid),
                        "middle": (self.S + third, self.S + 2 * third)}[kind]
                sd.start = self.S if a <= self.S + 0.01 else self.snap(max(self.S, a), bar=True)
                sd.end = self.E if b < 0 or b >= self.E else self.snap(b, bar=True)
                if sd.end >= self.E:
                    sd.end = self.E
                sd.cue = None
                last_start = sd.start
                kept.append(sd)
                continue
            if sd.cue:
                evs = [e for e in self.cue_events(sd.cue) if self.S - 0.01 <= e[0] < self.E]
                if sd.occurrence:
                    k = sd.occurrence
                    pick = evs[k - 1] if 0 < k <= len(evs) else (evs[-1] if k < 0 and evs else None)
                    if pick is None:
                        what = sd.cue[4:] if sd.cue.startswith("sec:") else sd.cue
                        what = {"drop": "drop / chorus", "breakdown": "bridge"}.get(what, what)
                        nth = {1: "a first", 2: "a second", 3: "a third", 4: "a fourth"}.get(k, "that")
                        self.warnings.append(f"Couldn't find {nth} {what} in this part of the song (found {len(evs)}), "
                                             f"so that instruction was skipped. Add a time instead, like \"at 2:10 ...\".")
                        continue
                    evs = [pick]
                    sd.start = -1.0
                if sd.occurrence:
                    ev = evs[0]  # named exactly ("the second drop", "before each drop")
                elif sd.start >= 0:
                    near = [e for e in evs if abs(e[0] - sd.start) <= 6.0]
                    ev = min(near, key=lambda e: abs(e[0] - sd.start)) if near else None
                else:
                    later = [e for e in evs if e[0] >= last_start + 0.01]
                    ev = later[0] if later else None
                if ev is not None:
                    sd.start, sd.cue_end, sd.hit_times = ev[0], ev[1], ev[2]
                elif sd.start >= 0:
                    sd.start = self.snap(sd.start, bar=True)
                else:
                    name = {"drop": "bass drop", "predrop": "lead-in to a drop", "vocals": "vocals"}.get(
                        sd.cue, EVENT_LABELS.get(sd.cue, sd.cue[4:] if sd.cue.startswith("sec:") else sd.cue))
                    tip = (" or right-click the timeline and choose Mark the drop here" if sd.cue == "drop" else "")
                    self.warnings.append(f"Couldn't hear the {name} in this song, so that instruction was skipped. "
                                         f"Add a time, like \"at 0:23 ...\"{tip}.")
                    continue
            elif sd.start >= 0:
                sd.start = self.snap(sd.start, bar=True)
            else:
                continue
            last_start = sd.start
            kept.append(sd)
        # Parts of the prompt that only say what to show become layers over the timing.
        for sd in kept:
            if sd.keep_cuts:
                self.overlays.append(sd)
                self.timeline_notes.append(f"{_mmss(sd.start)}–{_mmss(min(sd.end, self.E))}: {sd.what or sd.note}")
        kept = [sd for sd in kept if not sd.keep_cuts]
        # Moments tied to a place in the music ("freeze every time the music stops").
        from .direction import Moment

        for sd in kept:
            for a in sd.moments:
                self.d.moments.append(Moment(time=sd.start, action=a, duration=1.0 if a in ("freeze", "slowmo") else 0.5))
        kept = [sd for sd in kept if not sd.only_moments]
        kept.sort(key=lambda s: s.start)

        bounds = sorted({s.start for s in self.song.sections} | set(self.drops))
        for i, sd in enumerate(kept):
            nxt = kept[i + 1].start if i + 1 < len(kept) else None
            if sd.fixed:
                pass
            elif sd.end >= 0:
                sd.end = self.snap(sd.end, bar=True)
            elif sd.cue_end and (sd.cue in ("hits", "fill", "stop", "solo", "drums_faster", "intro", "riser", "predrop")
                                 or (sd.cue or "").startswith("sec:")):
                # tied to a musical moment that has its own end: stop when it's over
                sd.end = min(sd.cue_end, nxt if nxt is not None else self.E)
            elif nxt is not None:
                sd.end = nxt
            elif sd.cue_end and sd.cue in ("solo", "drums_faster", "ringout"):
                sd.end = sd.cue_end
            elif sd.hold:
                sd.end = self.E
            else:
                later = [b for b in bounds if b >= sd.start + 4.0]
                sd.end = min(later[0] if later else self.E, sd.start + 16.0)
            if nxt is not None and sd.end > nxt:
                sd.end = nxt
            sd.end = min(sd.end, self.E + 1)
            if sd.cue or sd.hold or sd.sync_hits or not sd.shots:
                what = sd.what or ("hold one still shot" if sd.hold and sd.still else "hold one shot" if sd.hold else
                        f"cut on each {'hit' if sd.sync_on == 'hits' else sd.sync_on}" if sd.sync_hits else
                        "cuts speed up" if sd.ramp else
                        "quick cuts" if (sd.cut_every or 2) <= 1 and sd.energy and sd.energy >= 0.9 else
                        "slower cuts" if (sd.cut_every or 0) >= 4 else "normal flow")
                cue_name = (sd.cue[4:].replace("drop", "chorus/drop") if sd.cue and sd.cue.startswith("sec:") else
                            {"predrop": "lead-in to the drop", "vocals": "vocals come in"}.get(sd.cue or "",
                                                                                              EVENT_LABELS.get(sd.cue, sd.cue)))
                heard = f" ({cue_name})" if sd.cue else ""
                self.timeline_notes.append(f"{_mmss(sd.start)}–{_mmss(min(sd.end, self.E))}{heard}: {what}")
        # Region refinements ("from 1:36 to 2:30: quicker cuts") win over everything they overlap.
        for rg in self.d.regions:
            a, b = rg.start, rg.end
            for act in rg.moments:  # freeze at the start of the selection, fades at its end
                at = max(a, b - 1.5) if act == "fade_black" else a
                self.d.moments.append(Moment(time=at, action=act, duration=min(1.5, b - a) if act == "fade_black" else 1.0))
            if rg.only_moments or rg.keep_cuts:
                if rg.keep_cuts:
                    ov = copy.copy(rg)
                    ov.fixed = True
                    self.overlays.append(ov)
                self.timeline_notes.append(f"{_mmss(a)}–{_mmss(b)} (your section edit): {rg.what or rg.note}")
                continue
            carved = []
            for sd in kept:
                if sd.end <= a or sd.start >= b:
                    carved.append(sd)
                    continue
                if sd.start < a:
                    left = copy.copy(sd)
                    left.end = a
                    carved.append(left)
                if sd.end > b:
                    right = copy.copy(sd)
                    right.start = b
                    right.cue, right.hit_times = None, (sd.hit_times if sd.sync_hits else None)
                    carved.append(right)
            reg = copy.copy(rg)
            reg.fixed, reg.cue_end = True, None
            reg.hit_times = None
            carved.append(reg)
            kept = sorted(carved, key=lambda s: s.start)
            self.timeline_notes.append(f"{_mmss(a)}–{_mmss(b)} (your section edit): {rg.what or rg.note}")
        self.d.sections = [sd for sd in kept if sd.end > sd.start + 0.05]

    def hold_at(self, t: float) -> SectionDirection | None:
        """A section the director wants as ONE held shot (one shot listed, 8+ beats per cut)."""
        for sd in self.d.sections:
            if sd.start <= t < sd.end and (sd.hold or (len(sd.shots) == 1 and (sd.cut_every or 0) >= 8)):
                return sd
        return None

    def label_at(self, t: float) -> str:
        t += 0.5 / self.fps
        label = self.song.section_at(t).label
        # A drop the user (or AI) placed by hand drives the next 8 bars like a drop.
        if label not in ("drop", "build") and any(d <= t < d + 32 * self.beat for d in self.drops):
            return "drop"
        return label

    def hits_near(self, t: float):
        lo, hi = 0, len(self.song.hits)
        while lo < hi:  # hits are sorted by time
            mid = (lo + hi) // 2
            if self.song.hits[mid][0] < t - 0.1:
                lo = mid + 1
            else:
                hi = mid
        j = lo
        while j < len(self.song.hits) and self.song.hits[j][0] <= t + 0.1:
            yield self.song.hits[j]
            j += 1

    def sd_at(self, t: float) -> SectionDirection | None:
        return self.d.section_direction(t + 0.5 / self.fps)  # a cut starts on a frame: allow half a frame

    def cut_every_at(self, t: float) -> float:
        sd = self.sd_at(t)
        if sd and sd.cut_every:
            base = sd.cut_every
        elif sd and sd.pace_shift:
            # quicker / slower than this part of the song is cut by default
            label = self.label_at(t)
            here = self.d.style.cut_every.get(label, 2.0) * self.song.feel.get("rate_scale", 1.0)
            here = max(1.0, here)  # "0.5" = every beat plus the odd extra hit: count it as a beat
            i = min(range(len(CUT_CHOICES)), key=lambda k: abs(CUT_CHOICES[k] - here))
            steps = -1 if sd.pace_shift > 0 else 1
            steps *= 2 if abs(sd.pace_shift) >= 2 else 1
            return CUT_CHOICES[max(0, min(len(CUT_CHOICES) - 1, i + steps))]
        else:
            label = self.label_at(t)
            base = self.d.style.cut_every.get(label, 2.0)
            if not self.d.style.pure and label not in self.d.rate_locked:
                # styles are written for a ~120 BPM beat: keep cuts a watchable length in seconds
                scale = self.song.feel.get("rate_scale", 1.0)
                if scale > 1:  # "0.5" mostly means every beat plus the odd extra hit: count it as 1
                    base = max(base, 1.0)
                base = min(8.0, max(0.5, base * scale))
                if self.song.feel and not self.song.feel.get("steady", True):
                    # no drum beat to follow (piano, strings, ambient): the beat grid is a guess,
                    # so never cut faster than about once a second
                    base = max(base, min((c for c in CUT_CHOICES if c * self.beat >= 0.9), default=8.0))
                # ride the song: the calmer stretches inside a part cut a step slower
                if base <= 2.0 and not any(d - 0.1 <= t < d + 16 * self.beat for d in self.drops):
                    local = self.song.energy_at(t - self.beat, t + 7 * self.beat)
                    if local < self.energy_q[0]:
                        base = _slower(base)
                    elif local < self.energy_q[1] and base <= 0.5:
                        base = 1.0
        ramp = self.ramp_at(t)
        if ramp:  # quicker and quicker: full length, then half, then a quarter
            a, b = ramp
            p = (t - a) / max(0.1, b - a)
            base = max(base, 2.0)
            factor = 1.0 if p < 0.4 else (0.5 if p < 0.75 else 0.25)
            return min(CUT_CHOICES, key=lambda c: abs(c - base * factor))
        return base

    def effect_at(self, t: float, name: str) -> float:
        ov = self.overlay_at(t)
        if ov and name in ov.effects:
            return float(np.clip(ov.effects[name], 0, 1))
        sd = self.sd_at(t)
        if sd and name in sd.effects:
            return float(np.clip(sd.effects[name], 0, 1))
        return self.d.style.effects.get(self.label_at(t), {}).get(name, 0.0)

    # ----- 1. where the cuts go ---------------------------------------------- #
    def cut_points(self) -> list[int]:
        beats = [b for b in self.song.beats if self.S <= b < self.E]
        down = set(self.song.downbeats)
        points: set[int] = set()
        priority: set[int] = set()
        bar_index = 0
        sync = [sd for sd in self.d.sections if sd.sync_hits]
        for i, b in enumerate(beats):
            if self.sync_at(b):
                continue  # these cut on the drum hits instead (below)
            if b in down:
                bar_index += 1
            every = self.cut_every_at(b)
            is_down = b in down
            beat_in_bar = 0
            for j in range(i, -1, -1):  # position of this beat inside its bar
                if beats[j] in down:
                    break
                beat_in_bar += 1
            take = (every <= 1.0
                    or (every == 2.0 and beat_in_bar % 2 == 0)
                    or (every == 4.0 and is_down)
                    or (every >= 8.0 and is_down and bar_index % 2 == 1))
            if take:
                points.add(self.frame_of(b))
            if every <= 0.5 and i + 1 < len(beats):
                # Double-time only where the music earns it: a strong off-beat hit,
                # or a "roll" on every fourth bar. Constant half-beat cuts are unwatchable.
                mid = (b + beats[i + 1]) / 2
                near_hit = any(abs(t - mid) < 0.07 and s >= 0.45 for t, s in self.hits_near(mid))
                sd_b = self.sd_at(b)
                asked = sd_b is not None and (sd_b.pace_shift or 0) > 0  # "quicker cuts" here: every half beat
                if near_hit or bar_index % 4 == 3 or self.ramp_at(b) or asked:
                    points.add(self.frame_of(mid))
        for s in self.song.sections:
            if self.S < s.start < self.E:
                f = self.frame_of(s.start)
                points.add(f)
                priority.add(f)
        for t in self.drops:
            if self.S < t < self.E:
                f = self.frame_of(t)
                points.add(f)
                priority.add(f)
        # Cuts on the actual drum hits (the hard hits the app heard, or every kick / snare).
        gap = max(0.18, self.beat / 2)
        times = [t for sd in sync if sd.hit_times for t in sd.hit_times]
        for kind in ("hits", "kick", "snare", "big"):
            last = -9.0
            for t in self.drum_times(kind, self.S, self.E):
                sd = self.sd_at(t)
                if t - last >= gap and self.sync_at(t) == kind and not (sd and sd.hit_times):
                    times.append(t)
                    last = t
        for t in times:
            if self.S < t < self.E:
                f = self.frame_of(t)
                points.add(f)
                priority.add(f)
        for sd in sync:
            if self.S < sd.start < self.E:
                points.add(self.frame_of(sd.start))
        if self.d.style.cut_on_fills:
            for ev in self.song.events_of("fill"):
                t0 = ev["time"]
                if not (self.S < t0 < self.E) or self.hold_at(t0) or self.sync_at(t0):
                    continue
                if self.cut_every_at(t0) > 2 or self.label_at(t0) in ("intro", "outro", "breakdown"):
                    continue
                for t in (ev.get("times") or [t0])[:3]:
                    points.add(self.frame_of(t))

        # Sections the director asked for ("at 1:34 ...") start where they were resolved.
        for sd in self.d.sections:
            if self.S < sd.start < self.E:
                f = self.frame_of(sd.start)
                points.add(f)
                priority.add(f)
        for m in self.d.moments:
            if self.S < m.time < self.E and m.action in ("cut", "freeze"):
                f = self.frame_of(m.time)
                points.add(f)
                priority.add(f)

        # Tap to Direct: where the user tapped cuts, their cuts replace the automatic ones.
        if self.tap_cuts:
            runs = self.tap_runs()
            inside = lambda f: any(self.frame_of(a) - 1 <= f <= self.frame_of(b + self.beat) for a, b in runs)
            points = {p for p in points if not inside(p)}
            priority = {p for p in priority if not inside(p)}
            for t in self.tap_cuts:
                if self.S < t < self.E:
                    f = self.frame_of(t)
                    points.add(f)
                    priority.add(f)
            self.tapped_frames = {self.frame_of(t) for t in self.tap_cuts}
        else:
            self.tapped_frames = set()

        # Too-short cuts read as glitches: keep a minimum length (priority points win).
        min_len = max(3, int(round(0.12 * self.fps)))
        # Edges of a part the user selected are exact: nearby automatic cuts give way to them.
        fixed = {self.frame_of(x) for sd in self.d.sections if sd.fixed for x in (sd.start, sd.end)
                 if self.S < x < self.E}
        if fixed:
            points = {p for p in points if p in fixed or all(abs(p - f) >= min_len for f in fixed)}
            points |= fixed
            priority |= fixed
        tap_min = max(2, int(round(0.08 * self.fps)))  # tapped cuts may sit closer together
        ordered = sorted(p for p in points if 0 < p < self.total)
        kept = [0]
        for p in ordered:
            gap = tap_min if (p in self.tapped_frames and kept[-1] in self.tapped_frames) else min_len
            if p - kept[-1] >= gap:
                kept.append(p)
            elif p in priority and kept[-1] not in priority and len(kept) > 1 and p - kept[-2] >= min_len:
                kept[-1] = p
            elif p in self.tapped_frames and kept[-1] not in self.tapped_frames and len(kept) > 1 \
                    and p - kept[-2] >= min_len:
                kept[-1] = p  # a tapped cut beats an automatic one
        if self.total - kept[-1] < min_len and len(kept) > 1:
            kept.pop()
        kept.append(self.total)
        # Held shots ("a still shot until the bass drops"): no cuts inside them.
        holds = [(self.frame_of(sd.start), self.frame_of(sd.end)) for sd in self.d.sections
                 if sd.hold or (len(sd.shots) == 1 and (sd.cut_every or 0) >= 8)]
        if holds:
            kept = [p for p in kept if p in (0, self.total) or not any(a < p < b for a, b in holds)]

        # Very long cuts get split on beats (or evenly when there are none).
        final = [kept[0]]
        beat_frames = sorted(self.frame_of(b) for b in beats)
        for p in kept[1:]:
            label = self.label_at(self.song_t(final[-1]))
            max_len = int((6.0 if label in ("intro", "outro", "breakdown") else 4.0) * self.fps)
            if self.hold_at(self.song_t(final[-1]) + 0.01):
                max_len = int(30.0 * self.fps)  # one shot held on purpose, up to 30 s
            elif self.tap_cuts and any(self.frame_of(a) - 1 <= final[-1] <= self.frame_of(b) for a, b in self.tap_runs()):
                max_len = int(12.0 * self.fps)  # the user chose these cut lengths by ear
            while p - final[-1] > max_len:
                inside = [b for b in beat_frames if final[-1] + min_len <= b <= final[-1] + max_len and p - b >= min_len]
                final.append(inside[-1] if inside else final[-1] + max_len)
            final.append(p)
        return final

    # ----- 2. which shot goes in each cut ------------------------------------ #
    def target_energy(self, t0: float, t1: float) -> float:
        sd = self.sd_at(t0)
        if sd and sd.energy is not None:
            return float(np.clip(sd.energy, 0, 1))
        music = self.song.energy_at(t0, t1)
        section = self.song.section_at(t0).energy
        wants = self.prefer_at(t0).get("motion", 0.0)  # "fight scenes" / "close-ups" for a whole part
        return float(np.clip(0.55 * music + 0.45 * section + self.d.style.energy_bias + 0.4 * wants, 0, 1))

    def allowed(self, s: Shot) -> bool:
        """Repeat rules: never the same moment twice; a scene comes back only when the
        footage has run out of fresh ones, and then as far apart as possible."""
        if self.uses.get(s.id, 0) and len(self.uses) < len(self.shots):
            return False
        used = self.group_uses.get(s.group, 0)
        if not used:
            return True
        policy = self.d.repeats
        if policy == "allowed":
            return self.cut_i - self.group_last.get(s.group, -99) > 6
        fresh_left = len(self.group_uses) < self.n_groups
        if fresh_left:
            return False
        if policy == "never":
            # Out of scenes entirely: allow the least-used, longest-ago ones only.
            least = min(self.group_uses.values())
            return used == least and self.cut_i - self.group_last.get(s.group, 0) > max(8, self.n_groups * 3 // 4)
        return self.cut_i - self.group_last.get(s.group, -99) > max(8, self.n_groups // 3)

    def pick_shot(self, t0: float, need: float, prev: Cut | None, scene: dict | None = None) -> Shot:
        sd = self.sd_at(t0)
        pref = dict(scene["prefer"]) if scene and scene.get("prefer") else self.prefer_at(t0)
        ov = self.overlay_at(t0)
        if scene and scene.get("favorites") and self.favs:
            # scene key "my favourites": one of the ★ shots, least used first
            favs = [self.footage.shots[i] for i in self.favs if 0 <= i < len(self.footage.shots)]
            favs = [s for s in favs if prev is None or s.id != prev.shot] or favs
            return min(favs, key=lambda s: (self.uses.get(s.id, 0), self.group_last.get(s.group, -99), self.rng.random()))
        if ov and ov.picked and ov.shots:
            # shots the user picked for these cuts, in their order (round again if there are more cuts)
            ids = [i for i in ov.shots if 0 <= i < len(self.footage.shots)]
            if ids:
                k = self.order_pos.get(id(ov), 0)
                sid = ids[k % len(ids)]
                if prev is not None and sid == prev.shot and len(ids) > 1:
                    k += 1
                    sid = ids[k % len(ids)]
                self.order_pos[id(ov)] = k + 1
                return self.footage.shots[sid]
        if sd and sd.hold and not sd.shots:
            pool = [s for s in self.shots if self.allowed(s)] or list(self.shots)
            if pref and self.d.repeats != "never":
                # asked for something specific ("as many characters as possible"): the best match
                # may come back even if it was used earlier
                best = max((self.content_score(s, pref) for s in pool), default=-9.0)
                have = {x.id for x in pool}  # (ids: comparing whole shots field by field is slow)
                pool += [s for s in self.shots if s.id not in have and self.content_score(s, pref) > max(0.6, best)
                         and (prev is None or s.id != prev.shot)]
            def hold_score(s):
                fits = min(1.0, self.usable_len(s) / max(need, 0.1))
                calm = (1 - s.motion) if sd.still else 1 - abs(s.motion - 0.35)
                camera = 2.0 if sd.camera and s.camera == sd.camera else 0.0
                return (3.0 * fits + 1.5 * calm + camera + (0.5 if s.static and sd.still else 0)
                        - 3.0 * s.text + 1.5 * self.content_score(s, pref) + self.rng.gauss(0, 0.1))
            return max(pool, key=hold_score)
        if sd and sd.shots and sd.fixed and sd.hold and 0 <= sd.shots[0] < len(self.footage.shots):
            return self.footage.shots[sd.shots[0]]  # the user picked this shot by hand: always use it
        if sd and sd.shots:
            # The director's list for this section, in order, each scene once.
            key = id(sd)
            ordered = [i for i in sd.shots if 0 <= i < len(self.footage.shots) and i not in self.avoid]
            consumed = self.order_used.setdefault(key, set())
            waiting = [i for i in ordered if i not in consumed and self.allowed(self.footage.shots[i])]
            # In order, but skip shots too short for this cut (they're kept for a shorter one),
            # so a clip never runs on into the next scene in the source.
            fitting = [i for i in waiting if self.usable_len(self.footage.shots[i]) >= need]
            if fitting or waiting:
                sid = fitting[0] if fitting else max(waiting, key=lambda i: self.footage.shots[i].duration)
                consumed.add(sid)
                return self.footage.shots[sid]
            if ordered and self.d.repeats != "never":
                # A section the director filled on purpose (e.g. "punches at 1:34"): re-hit its
                # own shots, from a different moment, before reaching for unrelated footage.
                recent = {g for g, last in self.group_last.items() if self.cut_i - last <= 2}
                again = [self.footage.shots[i] for i in ordered if self.footage.shots[i].group not in recent]
                if again:
                    return min(again, key=lambda s: (self.uses.get(s.id, 0), self.group_last.get(s.group, -1)))
            # otherwise fill with fresh footage that fits the section

        target = self.target_energy(t0, t0 + need)
        n_sources = len(self.footage.sources)
        pool = [s for s in self.shots if self.allowed(s)]
        clean = [s for s in pool if s.text < 0.85]   # full-screen title cards only when nothing else is left
        if clean:
            pool = clean
        if pref and self.d.repeats != "never":
            # Nothing fresh looks like what was asked for here: bring back a good match
            # (spaced out like any repeat) rather than showing the wrong kind of shot.
            best = max((self.content_score(s, pref) for s in pool), default=-9.0)
            if best < 0.3:
                spacing = max(6, self.n_groups // 4)
                have = {x.id for x in pool}
                pool += [s for s in self.shots if s.id not in have and self.content_score(s, pref) > 0.6
                         and self.cut_i - self.group_last.get(s.group, -99) > spacing]
        if not pool:  # tiny footage: fall back to anything not shown in the last few cuts
            pool = [s for s in self.shots if self.cut_i - self.group_last.get(s.group, -99) > 2] or self.shots
        # Prefer shots long enough for the cut, so it never runs on into the next scene
        # (that would show up as an extra cut that isn't on the beat).
        long_enough = [s for s in pool if self.usable_len(s) >= need]
        if len(long_enough) >= max(3, len(pool) // 5):
            pool = long_enough
        best, best_score = None, -1e9
        for s in pool:
            score = -2.2 * abs(s.motion - target)
            match = self.content_score(s, pref) if pref else 0.0
            again = 0.35 if match > 0.6 else 1.0  # a shot that is exactly what was asked for may come back
            score -= again * (1.4 * self.uses.get(s.id, 0) + 2.0 * self.group_uses.get(s.group, 0))
            if prev is not None:
                if s.id == prev.shot:
                    score -= 4.0
                elif n_sources > 1 and s.source == self.footage.shots[prev.shot].source:
                    score -= 0.35
            if s.static and target > 0.4:
                score -= 0.8
            score -= 1.5 * s.text   # captions, logos, title cards
            if sd and sd.camera and s.camera == sd.camera:
                score += 1.0
            if pref:
                score += (4.0 if scene else 1.8) * match
            if s.id in self.favs:  # ★ shots: a bit more often, and above all at the big moments
                big = target > 0.7 or any(d - 0.1 <= t0 < d + 8 * self.beat for d in self.drops)
                score += 0.6 + (1.4 if big else 0.0)
            keys = self.reserved.get(s.id)
            if keys and (not pref or repr(sorted(pref.items())) not in keys):
                score -= 0.9  # saved for the part of the song that asked for it
            if s.duration < need * 0.9:
                score -= 0.6
            score += self.rng.gauss(0, 0.25)
            if score > best_score:
                best, best_score = s, score
        return best

    @staticmethod
    def focus_of(shot: Shot, a: float, b: float) -> tuple[float, float]:
        """Where the action is during [a, b] of the source, in the visible (de-barred) picture."""
        pts = [(x, y) for t, x, y in shot.centers if a - 0.1 <= t <= b + 0.1] or [(x, y) for _, x, y in shot.centers]
        if not pts:
            return (0.5, 0.5)
        x = float(np.mean([p[0] for p in pts]))
        y = float(np.mean([p[1] for p in pts]))
        cx, cy, cw, ch = shot.crop  # centres were measured on the full frame; bars are cropped first
        x = (x - cx) / cw if cw else x
        y = (y - cy) / ch if ch else y
        return (round(float(np.clip(x, 0, 1)), 3), round(float(np.clip(y, 0, 1)), 3))

    def drum_hits_in(self, t0: float, t1: float) -> list[float]:
        hits = sorted(self.song.kicks + self.song.snares)
        out = []
        for t in hits:
            if t0 - 0.06 <= t < t1 - 0.05 and (not out or t - out[-1] > 0.08):
                out.append(t)
        return out

    def in_point(self, shot: Shot, need: float, energetic: bool, t0: float = 0.0, dur: float = 0.0,
                 speed: float = 1.0) -> float:
        src = self.footage.sources[shot.source]
        latest = max(0.0, src.duration - need - 0.05)
        lo = shot.start + 0.08
        hi = max(lo, min(shot.end - 0.3, shot.start + self.usable_len(shot) + 0.08 - need))
        # Hit on the hit: start the clip so its punch / impact frame lands on a drum hit.
        if energetic and shot.impacts and dur > 0:
            best = None
            for h in self.drum_hits_in(t0, t0 + dur)[:3]:
                offset = max(0.0, h - t0) * speed          # seconds into the clip where the hit falls
                for p, strength in shot.impacts:
                    start = p - offset
                    if lo - 1e-6 <= start <= hi + 1e-6:
                        overlap = any(start < b and start + need > a for a, b in self.used_windows.get(shot.id, []))
                        score = strength - (0.5 if overlap else 0.0) - 0.15 * offset
                        if best is None or score > best[0]:
                            best = (score, start)
            if best is not None:
                self.hit_aligned += 1
                return float(np.clip(best[1], 0.0, latest))
        if energetic:
            start = shot.peak - need * 0.4
        else:
            start = self.rng.uniform(lo, hi) if hi > lo else lo
        start = float(np.clip(start, lo, hi))
        # Don't repeat the exact moment we already used from this shot.
        for a, b in self.used_windows.get(shot.id, []):
            if start < b and start + need > a:
                alt = b if b + need <= max(hi, b) + 1e-6 else max(lo, a - need)
                start = float(np.clip(alt, lo, hi))
                break
        return float(np.clip(start, 0.0, latest))

    # ----- 3. build ----------------------------------------------------------- #
    def build(self) -> EditPlan:
        if not self.shots:
            raise ValueError("There are no usable shots in the footage.")
        points = self.cut_points()
        self.down_frames = {self.frame_of(b) for b in self.song.downbeats if self.S <= b < self.E}
        cuts: list[Cut] = []
        prev = None
        for f0, f1 in zip(points[:-1], points[1:]):
            t0, t1 = self.song_t(f0), self.song_t(f1)
            dur = (f1 - f0) / self.fps
            label = self.label_at(t0)
            speed, reverse = 1.0, False
            if self.velocity_at(t0) and not self.d.style.pure:
                # velocity edit: clips race, then slow right down on the big beats
                on_big = f0 in self.down_frames or any(abs(t - t0) < 0.06 and st >= 0.6 for t, st in self.hits_near(t0))
                speed = 0.45 if on_big and dur >= 0.3 else (1.7 if dur >= 0.2 else 1.0)
            elif dur >= 0.4 and self.rng.random() < self.effect_at(t0, "slowmo") * 0.7:
                speed = 0.5
            elif dur >= 0.3 and self.rng.random() < self.effect_at(t0, "reverse") * 0.5:
                reverse = True
            need = dur * speed
            shot = self.pick_shot(t0, need, prev, self.scene_taps.get(f0))
            sd_here = self.sd_at(t0)
            frozen = bool(sd_here and sd_here.freeze_frame and sd_here.shots and shot.id in sd_here.shots)
            if frozen:
                speed = 1.0
            elif self.usable_len(shot) < need and not self.d.style.pure:
                # Shot shorter than the cut: stretch it with slow motion instead of
                # running past it into the next scene (which reads as an off-beat cut).
                speed = max(0.25, round(self.usable_len(shot) / dur, 3))
                need = dur * speed
            energetic = self.target_energy(t0, t1) > 0.5
            if frozen:  # the frame the user picked (the thumbnail is the middle of the shot)
                start = round((shot.start + shot.end) / 2, 3)
            else:
                start = self.in_point(shot, need, energetic and not reverse, t0, dur, speed)
            self.uses[shot.id] = self.uses.get(shot.id, 0) + 1
            self.used_windows.setdefault(shot.id, []).append((start, start + need))
            self.group_uses[shot.group] = self.group_uses.get(shot.group, 0) + 1
            self.group_last[shot.group] = self.cut_i
            self.cut_i += 1
            src = self.footage.sources[shot.source]
            cut = Cut(f0=f0, f1=f1, shot=shot.id, source=src.path, src_start=round(start, 3),
                      speed=speed, reverse=reverse, section=label, crop=tuple(shot.crop),
                      focus=self.focus_of(shot, start, start + need), freeze=frozen)
            cuts.append(cut)
            prev = cut

        self.apply_effects(cuts)
        cuts = self.apply_moments(cuts)
        self.apply_transitions(cuts)
        self.apply_texts(cuts)
        plan = EditPlan(fps=self.fps, width=self.out.width, height=self.out.height,
                        song=self.song.path, song_start=round(self.S, 3), frames=self.total,
                        cuts=cuts, look=self.d.look, fit=self.out.fit, summary=self.d.summary)
        plan.timeline = list(self.timeline_notes)
        plan.proxies = {s.path: s.proxy for s in self.footage.sources if s.proxy and Path(s.proxy).is_file()}
        plan.warnings.extend(self.warnings)
        wants = [p for p in [*self.d.prefer.values(), *(sd.prefer for sd in self.d.sections + self.overlays)]
                 if p and ("crowd" in p or p.get("_query"))]
        if wants and not self.d.shot_tags:
            plan.warnings.append(
                "To find shots by who is in them (names, or as many characters as possible), the app needs to look at "
                "your shots once: press Describe my shots (AI) in the Footage section (it costs about a cent). "
                "Or choose the shot yourself with Pick shot….")
        reused = self.cut_i - len(self.group_uses)
        if reused > 0:
            plan.warnings.append(
                f"Your footage has {len(self.group_uses)} different scenes but this edit needs {self.cut_i} cuts, "
                f"so {reused} cuts reuse a scene (never back to back). Add more footage, use slower cuts, "
                f"or edit a shorter part of the song to avoid repeats.")
        plan.validate()
        return plan

    # ----- effects ------------------------------------------------------------ #
    def cut_index_at(self, cuts: list[Cut], frame: int) -> int:
        lo, hi = 0, len(cuts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if cuts[mid].f0 <= frame:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def add(self, cuts: list[Cut], frame: int, kind: str, frames: int, intensity: float = 1.0,
            color: str = "white") -> None:
        if not 0 <= frame < self.total:
            return
        c = cuts[self.cut_index_at(cuts, frame)]
        local = frame - c.f0
        frames = max(1, min(frames, c.frames - local))
        if any(e.kind == kind and abs(e.frame - local) < 2 for e in c.effects):
            return
        c.effects.append(Effect(kind, local, frames, round(float(np.clip(intensity, 0.05, 1.0)), 2), color))

    def apply_effects(self, cuts: list[Cut]) -> None:
        st = self.d.style
        beat_f = max(2, int(round(self.beat * self.fps)))
        bar_f = beat_f * 4
        down_frames = {self.frame_of(b) for b in self.song.downbeats if self.S <= b < self.E}

        # Flashes on the strongest hits (share scaled by each section's flash amount).
        hits = [(t, s) for t, s in self.song.hits if self.S <= t < self.E - 0.1]
        if hits and st.flash_top > 0:
            strengths = np.array([s for _, s in hits])
            last = -10.0
            for t, s in sorted(hits):
                share = st.flash_top * (0.25 + 1.5 * self.effect_at(t, "flash"))
                if share <= 0:
                    continue
                if s >= np.quantile(strengths, max(0.0, 1.0 - min(share, 0.6))) and t - last >= 0.25:
                    self.add(cuts, self.frame_of(t), "flash", st.flash_frames, min(1.0, 0.5 + s / 2))
                    last = t

        for c in cuts:
            t = self.song_t(c.f0)
            if c.f0 in down_frames or c.f0 % beat_f < 2:
                if self.rng.random() < self.effect_at(t, "zoom") * 0.55:
                    self.add(cuts, c.f0, "zoom", min(c.frames, beat_f), 0.4 + 0.6 * self.effect_at(t, "zoom"))
                if self.rng.random() < self.effect_at(t, "shake") * 0.45:
                    self.add(cuts, c.f0, "shake", min(c.frames, bar_f), 0.3 + 0.7 * self.effect_at(t, "shake"))
        strong = [(t, s) for t, s in hits if s > 0.6]
        for t, s in strong:
            if self.rng.random() < self.effect_at(t, "rgb") * 0.6:
                self.add(cuts, self.frame_of(t), "rgb", 4, 0.4 + 0.6 * self.effect_at(t, "rgb"))
            if s > 0.85 and self.rng.random() < self.effect_at(t, "invert") * 0.5:
                self.add(cuts, self.frame_of(t), "invert", 2)

        self.apply_drum_fx(cuts)
        self.apply_asked_effects(cuts)

        # Section entrances.
        for sec in self.song.sections:
            if not (self.S < sec.start < self.E):
                continue
            f = self.frame_of(sec.start)
            if st.blur_into_sections and sec.label in ("verse", "drop", "build"):
                self.add(cuts, f, "blur_in", max(3, beat_f // 2), 0.8)

        # Drops: the signature moment.
        for t in self.drops:
            if not (self.S < t < self.E):
                continue
            f = self.frame_of(t)
            if st.white_into_drop:
                self.add(cuts, f, "fade_white", max(4, beat_f), 1.0)
            elif st.flash_top > 0 or self.effect_at(t, "flash") > 0:
                self.add(cuts, f, "flash", max(4, st.flash_frames + 1), 1.0)
            if self.effect_at(t, "zoom") > 0.1:
                self.add(cuts, f, "zoom", beat_f, 0.9)
            if self.effect_at(t, "shake") > 0.1:
                self.add(cuts, f, "shake", bar_f, 0.6 + 0.4 * self.effect_at(t, "shake"))
            if st.freeze_before_drop:
                i = self.cut_index_at(cuts, f) - 1
                if i >= 0 and cuts[i].frames >= 4 and not self.hold_at(self.song_t(cuts[i].f0) + 0.01):
                    cuts[i].freeze = cuts[i].frames <= beat_f
                    if not cuts[i].freeze:
                        self.split_freeze(cuts, i, min(beat_f, cuts[i].frames // 2))

        # Ending: fade to black on the last second (not for cuts-only styles).
        if not st.pure:
            last = cuts[-1]
            self.add(cuts, max(last.f0, self.total - self.fps), "fade_black", self.fps, 1.0, "black")

    def apply_asked_effects(self, cuts: list[Cut]) -> None:
        """Effects asked for in a part of the song ("more flashes", "zoom", "shake" there) really show up:
        on every bar and the strong hits, not just as a slightly higher chance."""
        beat_f = max(2, int(round(self.beat * self.fps)))
        for sd in self.d.sections + self.overlays:
            fx = {k: v for k, v in sd.effects.items() if v >= 0.75 and k in ("flash", "zoom", "shake", "rgb", "invert")}
            if not fx:
                continue
            a, b = max(sd.start, self.S), min(sd.end, self.E)
            downs = [t for t in self.song.downbeats if a <= t < b]
            strong = [t for t, st in self.song.hits if a <= t < b and st >= 0.5]
            for kind in fx:
                times = sorted(set(downs + (strong if kind in ("flash", "rgb", "invert") else [])))
                last = -9.0
                for t in times:
                    if t - last < (0.35 if kind == "flash" else 0.9):
                        continue
                    last = t
                    frames = {"flash": max(3, self.d.style.flash_frames), "zoom": beat_f, "shake": 2 * beat_f,
                              "rgb": 4, "invert": 2}[kind]
                    self.add(cuts, self.frame_of(t), kind, frames, 0.75)

    def apply_drum_fx(self, cuts: list[Cut]) -> None:
        """Effects the prompt tied to a drum: "flash on every snare", "slow motion on the big hits"."""
        jobs = [(e["effect"], e["drum"], e.get("label"), None) for e in self.d.drum_fx]
        jobs += [(eff, drum, None, sd) for sd in self.d.sections + self.overlays for eff, drum in sd.drum_fx.items()]
        beat_f = max(2, int(round(self.beat * self.fps)))
        for eff, drum, label, sd in jobs:
            a, b = (sd.start, sd.end) if sd else (self.S, self.E)
            gap = 0.22 if eff in ("flash", "rgb", "invert") else 0.4
            last = -9.0
            for t in self.drum_times(drum, max(a, self.S), min(b, self.E)):
                if t - last < gap or (label and self.label_at(t) != label):
                    continue
                if sd is None and self.sd_at(t) is not None and self.sd_at(t).hold and eff not in ("flash",):
                    continue
                f = self.frame_of(t)
                last = t
                if eff in ("slowmo", "reverse"):
                    c = cuts[self.cut_index_at(cuts, f)]
                    if abs(c.f0 - f) <= 2:
                        if eff == "slowmo":
                            c.speed = min(c.speed, 0.5)
                        else:
                            c.reverse = True
                    continue
                frames = {"flash": max(2, self.d.style.flash_frames), "rgb": 4, "invert": 2,
                          "zoom": beat_f, "shake": beat_f}.get(eff, 3)
                dense = len(self.drum_times(drum, t - 2 * self.beat, t + 2 * self.beat)) > 4
                strength = {"flash": 0.55 if dense else 0.85, "zoom": 0.55 if dense else 0.75, "shake": 0.6}.get(eff, 0.8)
                self.add(cuts, f, eff, 2 if eff == "flash" and dense else frames, strength)

    # ----- transitions and text cards ------------------------------------------ #
    def transition_at(self, t: float) -> str | None:
        sd = self.sd_at(t)
        if sd is not None and sd.transition:
            return sd.transition
        ov = self.overlay_at(t)
        if ov is not None and ov.transition:
            return ov.transition
        tr = self.d.transitions
        kind = tr.get(self.label_at(t)) or tr.get("all")
        if kind:
            return kind
        if "@sections" in tr:  # "lots of transitions": where a new part of the song starts
            starts = [s.start for s in self.song.sections if self.S < s.start < self.E]
            if any(abs(t - x) < 0.5 / self.fps + 1e-6 for x in starts):
                return "zoom" if any(abs(t - d) < 0.05 for d in self.drops) else tr["@sections"]
        return None

    def apply_transitions(self, cuts: list[Cut]) -> None:
        for i in range(1, len(cuts)):
            c, prev = cuts[i], cuts[i - 1]
            kind = self.transition_at(self.song_t(c.f0))
            if not kind or kind == "mix":
                kind = None if not kind else "crossfade"
            if not kind or prev.reverse or prev.shot == c.shot:
                continue
            secs = 0.4 if kind in ("crossfade", "dissolve") else 0.27
            n = min(int(round(secs * self.fps)), c.frames // 2, prev.frames)
            if n < 3 or c.frames < int(0.3 * self.fps):
                continue  # too quick to blend: stays a hard cut
            out_at = prev.src_start + prev.frames / self.fps * prev.speed
            shot = self.footage.shots[prev.shot]
            room = self.take_end.get(shot.id, shot.end) - out_at
            freeze = prev.freeze or room < n / self.fps * prev.speed + 0.05
            c.transition = {"kind": kind, "frames": n, "source": prev.source,
                            "src_start": round(prev.src_start if prev.freeze else
                                               (max(prev.src_start, out_at - 1.0 / self.fps) if freeze else out_at), 3),
                            "speed": prev.speed, "freeze": freeze, "crop": list(prev.crop), "focus": list(prev.focus)}

    def apply_texts(self, cuts: list[Cut]) -> None:
        from .titles import text_card

        for t in list(self.d.texts) + list(self.d.user_texts):
            text = str(t.get("text", "")).strip()
            if not text:
                continue
            dur = max(0.5, float(t.get("duration") or 3.0))
            at = float(t.get("time", -2.0))
            start = self.S if at <= -2 else (max(self.S, self.E - dur) if at < 0 else at)
            f0, f1 = max(0, self.frame_of(start)), min(self.total, self.frame_of(start + dur))
            if f1 - f0 < 2:
                continue
            name = text_card(text, t.get("style", "title"), self.out.width, self.out.height).name
            fade = min(0.4, dur / 4)
            for c in cuts:
                a, b = max(c.f0, f0), min(c.f1, f1)
                if b <= a:
                    continue
                edges = ("in" if a == f0 else "") + ("out" if b == f1 else "")
                tag = {"inout": "both", "in": "in", "out": "out", "": "none"}[edges]
                c.effects.append(Effect("text", a - c.f0, b - a, fade, f"{name}|{tag}"))

    def split_freeze(self, cuts: list[Cut], i: int, hold: int) -> None:
        """Freeze the last `hold` frames of cut i (the beat before a drop)."""
        c = cuts[i]
        if hold < 2 or c.frames - hold < 2:
            c.freeze = True
            return
        split = c.f1 - hold
        moving = (split - c.f0) / self.fps * c.speed
        tail = Cut(f0=split, f1=c.f1, shot=c.shot, source=c.source,
                   src_start=round(c.src_start + (0 if c.reverse else moving), 3),
                   freeze=True, section=c.section, crop=c.crop, focus=c.focus)
        tail.effects = [Effect(e.kind, e.frame - (split - c.f0), e.frames, e.intensity, e.color)
                        for e in c.effects if e.frame >= split - c.f0]
        c.effects = [e for e in c.effects if e.frame < split - c.f0]
        c.f1 = split
        cuts.insert(i + 1, tail)

    def apply_moments(self, cuts: list[Cut]) -> list[Cut]:
        for m in sorted(self.d.moments, key=lambda m: m.time):
            if not (self.S <= m.time < self.E):
                continue
            f = self.frame_of(m.time)
            i = self.cut_index_at(cuts, f)
            c = cuts[i]
            dur_f = max(2, int(round((m.duration or 0.5) * self.fps)))
            if m.action == "freeze":
                if c.f0 == f:
                    c.freeze = True
                else:
                    self.split_freeze(cuts, i, c.f1 - f)
            elif m.action == "slowmo":
                c.speed = 0.5
            elif m.action == "reverse":
                c.reverse = True
            elif m.action == "fade_in":
                self.add(cuts, f, "fade_in", max(4, int(round((m.duration or 1.0) * self.fps))), 1.0, "black")
            elif m.action in ("flash", "zoom", "shake", "rgb", "invert", "fade_white", "fade_black"):
                frames = {"flash": 4, "rgb": 4, "invert": 2}.get(m.action, dur_f)
                if m.action == "fade_black":
                    # end the cut in black: place the fade at the end of the moment
                    self.add(cuts, max(c.f0, f), m.action, min(dur_f, c.f1 - f), 1.0, "black")
                else:
                    self.add(cuts, f, m.action, frames, 1.0)
        return cuts


def _slower(every: float) -> float:
    i = CUT_CHOICES.index(every) if every in CUT_CHOICES else 1
    return CUT_CHOICES[min(len(CUT_CHOICES) - 1, i + 1)]


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}"
