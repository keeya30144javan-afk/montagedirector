"""Turn a Direction + SongMap + Footage into a frame-exact EditPlan."""

from __future__ import annotations

import copy
import logging
import random
from dataclasses import dataclass

import numpy as np

from .direction import Direction, SectionDirection
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
        self.avoid = set(direction.avoid_shots)
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
        for sd in self.d.sections:
            sd.start, sd.end = self.snap(sd.start, bar=True), self.snap(sd.end, bar=True)
        for m in self.d.moments:
            if m.action != "fade_black":
                m.time = self.snap(m.time, hit=m.action in ("flash", "rgb", "invert", "fade_white"))
        if direction.drops is None:
            self.drops = list(song.drops)
        else:
            snapped = []
            for t in direction.drops:
                near = [d for d in song.drops if abs(d - t) <= 4 * self.beat]
                snapped.append(min(near, key=lambda d: abs(d - t)) if near else self.snap(t, bar=True))
            self.drops = sorted(set(snapped))

    # ----- helpers ----------------------------------------------------------- #
    def frame_of(self, song_t: float) -> int:
        return int(round((song_t - self.S) * self.fps))

    def song_t(self, frame: int) -> float:
        return self.S + frame / self.fps

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

    @staticmethod
    def usable_len(shot: Shot) -> float:
        """Shot boundaries are found 6 times a second, so keep clear of the edges:
        never show the last frames before the source cuts to another scene."""
        return max(0.0, shot.duration - 0.38)

    def hold_at(self, t: float) -> SectionDirection | None:
        """A section the director wants as ONE held shot (one shot listed, 8+ beats per cut)."""
        for sd in self.d.sections:
            if sd.start <= t < sd.end and len(sd.shots) == 1 and (sd.cut_every or 0) >= 8:
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
        return self.d.section_direction(t)

    def cut_every_at(self, t: float) -> float:
        sd = self.sd_at(t)
        if sd and sd.cut_every:
            return sd.cut_every
        return self.d.style.cut_every.get(self.label_at(t), 2.0)

    def effect_at(self, t: float, name: str) -> float:
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
        for i, b in enumerate(beats):
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
                if near_hit or bar_index % 4 == 3:
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
        # Sections the director asked for ("at 1:34 ...") start on the grid (snapped in __init__).
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

        # Too-short cuts read as glitches: keep a minimum length (priority points win).
        min_len = max(3, int(round(0.12 * self.fps)))
        ordered = sorted(p for p in points if 0 < p < self.total)
        kept = [0]
        for p in ordered:
            if p - kept[-1] >= min_len:
                kept.append(p)
            elif p in priority and kept[-1] not in priority and len(kept) > 1 and p - kept[-2] >= min_len:
                kept[-1] = p
        if self.total - kept[-1] < min_len and len(kept) > 1:
            kept.pop()
        kept.append(self.total)
        # Held shots ("a still shot until the bass drops"): no cuts inside them.
        holds = [(self.frame_of(sd.start), self.frame_of(sd.end)) for sd in self.d.sections
                 if len(sd.shots) == 1 and (sd.cut_every or 0) >= 8]
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
        return float(np.clip(0.55 * music + 0.45 * section + self.d.style.energy_bias, 0, 1))

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

    def pick_shot(self, t0: float, need: float, prev: Cut | None) -> Shot:
        sd = self.sd_at(t0)
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
            score -= 1.4 * self.uses.get(s.id, 0) + 2.0 * self.group_uses.get(s.group, 0)
            if prev is not None:
                if s.id == prev.shot:
                    score -= 4.0
                elif n_sources > 1 and s.source == self.footage.shots[prev.shot].source:
                    score -= 0.35
            if s.static and target > 0.4:
                score -= 0.8
            if s.duration < need * 0.9:
                score -= 0.6
            score += self.rng.gauss(0, 0.25)
            if score > best_score:
                best, best_score = s, score
        return best

    def in_point(self, shot: Shot, need: float, energetic: bool) -> float:
        src = self.footage.sources[shot.source]
        latest = max(0.0, src.duration - need - 0.05)
        lo = shot.start + 0.08
        hi = max(lo, shot.start + self.usable_len(shot) + 0.08 - need)
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
        cuts: list[Cut] = []
        prev = None
        for f0, f1 in zip(points[:-1], points[1:]):
            t0, t1 = self.song_t(f0), self.song_t(f1)
            dur = (f1 - f0) / self.fps
            label = self.label_at(t0)
            speed, reverse = 1.0, False
            if dur >= 0.4 and self.rng.random() < self.effect_at(t0, "slowmo") * 0.7:
                speed = 0.5
            elif dur >= 0.3 and self.rng.random() < self.effect_at(t0, "reverse") * 0.5:
                reverse = True
            need = dur * speed
            shot = self.pick_shot(t0, need, prev)
            if self.usable_len(shot) < need and not self.d.style.pure:
                # Shot shorter than the cut: stretch it with slow motion instead of
                # running past it into the next scene (which reads as an off-beat cut).
                speed = max(0.25, round(self.usable_len(shot) / dur, 3))
                need = dur * speed
            energetic = self.target_energy(t0, t1) > 0.5
            start = self.in_point(shot, need, energetic)
            self.uses[shot.id] = self.uses.get(shot.id, 0) + 1
            self.used_windows.setdefault(shot.id, []).append((start, start + need))
            self.group_uses[shot.group] = self.group_uses.get(shot.group, 0) + 1
            self.group_last[shot.group] = self.cut_i
            self.cut_i += 1
            src = self.footage.sources[shot.source]
            cut = Cut(f0=f0, f1=f1, shot=shot.id, source=src.path, src_start=round(start, 3),
                      speed=speed, reverse=reverse, section=label, crop=tuple(shot.crop))
            cuts.append(cut)
            prev = cut

        self.apply_effects(cuts)
        cuts = self.apply_moments(cuts)
        plan = EditPlan(fps=self.fps, width=self.out.width, height=self.out.height,
                        song=self.song.path, song_start=round(self.S, 3), frames=self.total,
                        cuts=cuts, look=self.d.look, fit=self.out.fit, summary=self.d.summary)
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
                if i >= 0 and cuts[i].frames >= 4:
                    cuts[i].freeze = cuts[i].frames <= beat_f
                    if not cuts[i].freeze:
                        self.split_freeze(cuts, i, min(beat_f, cuts[i].frames // 2))

        # Ending: fade to black on the last second (not for cuts-only styles).
        if not st.pure:
            last = cuts[-1]
            self.add(cuts, max(last.f0, self.total - self.fps), "fade_black", self.fps, 1.0, "black")

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
                   freeze=True, section=c.section, crop=c.crop)
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
            elif m.action in ("flash", "zoom", "shake", "rgb", "invert", "fade_white", "fade_black"):
                frames = {"flash": 4, "rgb": 4, "invert": 2}.get(m.action, dur_f)
                if m.action == "fade_black":
                    # end the cut in black: place the fade at the end of the moment
                    self.add(cuts, max(c.f0, f), m.action, min(dur_f, c.f1 - f), 1.0, "black")
                else:
                    self.add(cuts, f, m.action, frames, 1.0)
        return cuts
