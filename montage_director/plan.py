"""The edit plan: a frame-exact list of cuts with their effects.

Directors produce a Direction (what the edit should feel like); the planner
turns it into an EditPlan; the renderer turns the EditPlan into a video.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

EFFECT_KINDS = (
    "flash",        # white (or coloured) frame that fades out
    "zoom",         # zoom punch: jumps in, eases back out
    "shake",        # camera shake
    "rgb",          # RGB split / chromatic glitch
    "invert",       # negative strobe
    "blur_in",      # blurred frames easing into focus
    "fade_white",   # fade from white at the start of the cut
    "fade_black",   # fade to black at the end of the cut
)


@dataclass
class Effect:
    kind: str
    frame: int              # first frame, relative to the cut
    frames: int             # length in frames
    intensity: float = 1.0  # 0..1
    color: str = "white"


@dataclass
class Cut:
    f0: int                 # first timeline frame (inclusive)
    f1: int                 # last timeline frame (exclusive)
    shot: int               # shot id in the footage library
    source: str             # path to the source video
    src_start: float        # seconds into the source
    speed: float = 1.0      # <1 slow motion, >1 fast
    reverse: bool = False
    freeze: bool = False    # hold the first frame for the whole cut
    effects: list[Effect] = field(default_factory=list)
    section: str = ""
    crop: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)  # remove baked-in black bars

    @property
    def frames(self) -> int:
        return self.f1 - self.f0


@dataclass
class Look:
    """Whole-video treatment."""
    grade: str = "none"     # none | y2k | sad | cinematic | vhs | mono | dream
    glow: float = 0.0       # 0..1 bloom
    trails: float = 0.0     # 0..1 ghosting (lagfun)
    letterbox: bool = False
    grain: float = 0.0      # 0..1


@dataclass
class EditPlan:
    fps: int
    width: int
    height: int
    song: str
    song_start: float       # where the edit starts in the song (seconds)
    frames: int             # total timeline frames
    cuts: list[Cut] = field(default_factory=list)
    look: Look = field(default_factory=Look)
    fit: str = "fill"       # fill (crop to fill) | fit (bars) | blur (blurred background)
    summary: str = ""
    warnings: list[str] = field(default_factory=list)
    timeline: list[str] = field(default_factory=list)   # how the director's instructions were placed

    @property
    def duration(self) -> float:
        return self.frames / self.fps

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1)

    @classmethod
    def from_json(cls, text: str) -> "EditPlan":
        d = json.loads(text)
        d["look"] = Look(**d["look"])
        d["cuts"] = [Cut(**{**c, "effects": [Effect(**e) for e in c["effects"]]}) for c in d["cuts"]]
        return cls(**d)

    def validate(self) -> None:
        """Cuts must tile the timeline exactly: no gaps, no overlaps."""
        expected = 0
        for c in self.cuts:
            if c.f0 != expected or c.f1 <= c.f0:
                raise ValueError(f"Cut at frame {c.f0} breaks the timeline (expected {expected}).")
            expected = c.f1
        if expected != self.frames:
            raise ValueError(f"Timeline covers {expected} frames, expected {self.frames}.")
