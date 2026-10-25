"""AI Director: Claude watches the footage (as thumbnail sheets), reads the song
map and the user's prompt, and returns a structured edit Direction.

The user supplies their own Anthropic API key. Each request's cost is
computed from the response's token usage and shown in the app.
"""

from __future__ import annotations

import base64
import copy
import io
import json
import logging
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .direction import (CUT_CHOICES, EFFECTS, MOMENT_ACTIONS, STYLES, Direction, Moment,
                        SectionDirection)
from .footage import Footage, Shot
from .music import SongMap
from .plan import Look

log = logging.getLogger(__name__)

MODELS = {
    # id: (label, input $/MTok, output $/MTok, supports server-side fallbacks)
    "claude-opus-5-5": ("Claude Opus 5.5 (best)", 4.00, 20.00, True),
    "claude-sonnet-5-5": ("Claude Sonnet 5.5 (cheaper)", 2.00, 10.00, True),
    "claude-haiku-5-5": ("Claude Haiku 5.5 (cheapest)", 0.10, 0.50, False),
}
DEFAULT_MODEL = "claude-opus-5-5"
MAX_SHOTS = 160
TILE_W, TILE_H, COLS, ROWS = 192, 108, 8, 5
GRADES = ["none", "y2k", "sad", "cinematic", "vhs", "mono", "dream"]


class AIDirectorError(Exception):
    """Something the user can act on (bad key, no credit, refusal, network)."""


# --------------------------------------------------------------------------- #
# Output schema (structured outputs guarantee the reply matches it)
# --------------------------------------------------------------------------- #
def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


EDIT_SCHEMA = _obj({
    "summary": {"type": "string", "description": "Two or three sentences for the user: what this edit does and why."},
    "style": {"type": "string", "enum": list(STYLES)},
    "look": _obj({
        "grade": {"type": "string", "enum": GRADES},
        "glow": {"type": "number", "description": "0 to 1"},
        "trails": {"type": "number", "description": "0 to 1 ghosting"},
        "letterbox": {"type": "boolean"},
        "grain": {"type": "number", "description": "0 to 1"},
    }),
    "sections": {"type": "array", "items": _obj({
        "start": {"type": "number", "description": "song seconds"},
        "end": {"type": "number", "description": "song seconds"},
        "cut_every_beats": {"type": "number", "description": "one of 0.5, 1, 2, 4, 8"},
        "intensity": {"type": "number", "description": "0 calm to 1 wild: how much motion the chosen shots should have"},
        "shots": {"type": "array", "items": {"type": "integer"},
                  "description": "shot ids to use in this section, in playback order"},
        "effects": _obj({k: {"type": "number", "description": "0 to 1"} for k in EFFECTS}),
        "note": {"type": "string", "description": "one short line about the idea for this section"},
    })},
    "moments": {"type": "array", "items": _obj({
        "time": {"type": "number", "description": "song seconds"},
        "action": {"type": "string", "enum": list(MOMENT_ACTIONS)},
        "duration": {"type": "number", "description": "seconds; 0 for instant effects"},
    })},
    "avoid_shots": {"type": "array", "items": {"type": "integer"},
                    "description": "shots that should never appear (menus, loading screens, credits, off-topic)"},
})

SYSTEM_PROMPT = """You are the director of a music video edit: an AMV (anime music video), GMV (game music video) or montage in the style of classic 2000s and early-2010s YouTube edits.

You are given:
- the user's idea for the edit,
- a map of the song: tempo, sections with relative energy, drops and the biggest hits (all times in song seconds),
- contact sheets of the footage: every tile is one shot, labelled with its shot id,
- a table with each shot's length, motion (0 = still, 1 = most motion in this footage) and brightness.

Your job is to decide what the edit should feel like and which footage goes where, so the result matches what the user has in their head. Look closely at the contact sheets: pick shots by what they show (characters, fights, scenery, emotion, colour), not only by their numbers.

How your plan is used:
- A planner places every cut exactly on the beat for you. You choose how many beats each cut lasts per section (cut_every_beats: 0.5 = double-time, 1 = every beat, 2, 4, or 8 = long held shots).
- In each section, list the shots to use, in playback order. List more shots than the section has cuts if you can; they are used in order and the list repeats if it runs out. Prefer variety: avoid using the same shot back to back.
- "intensity" steers the planner when it needs extra shots: 0 picks calm footage, 1 picks the wildest.
- Effects (0 to 1) set how often each effect appears in that section: flash (white flash on hits), zoom (zoom punch on downbeats), shake (camera shake), rgb (RGB split glitch), slowmo (slow motion clips), reverse (reversed clips), invert (negative strobe). Use them with taste: big effects belong on drops and big hits.
- Moments are precise events at a song time: freeze (hold a frame), flash, zoom, shake, rgb, invert, slowmo, reverse, fade_white, fade_black, or cut (force a cut). Use them for the beats the user mentions and for signature moments such as the first drop.
- Sections must cover the whole requested range without gaps, in order. Align them with the song map's sections unless the user's idea calls for different splits.
- The style is a base recipe that your sections override; the look is applied to the whole video.

Write the summary for the user in plain words: what the edit does, and anything in their request you could not do (for example a scene that is not in the footage)."""


# --------------------------------------------------------------------------- #
# Request building
# --------------------------------------------------------------------------- #
def select_shots(footage: Footage, limit: int = MAX_SHOTS) -> list[Shot]:
    shots = footage.usable_shots()
    if len(shots) <= limit:
        return shots
    # Keep an even spread across the footage plus the most energetic moments.
    idx = set(np.linspace(0, len(shots) - 1, int(limit * 0.75)).round().astype(int).tolist())
    by_motion = sorted(range(len(shots)), key=lambda i: -shots[i].motion)
    for i in by_motion:
        if len(idx) >= limit:
            break
        idx.add(i)
    return [shots[i] for i in sorted(idx)]


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def contact_sheets(shots: list[Shot]) -> list[bytes]:
    """JPEG contact sheets, COLS x ROWS tiles, each labelled with its shot id."""
    per = COLS * ROWS
    font = _font(22)
    sheets = []
    for start in range(0, len(shots), per):
        chunk = shots[start:start + per]
        rows = (len(chunk) + COLS - 1) // COLS
        sheet = Image.new("RGB", (COLS * TILE_W, rows * TILE_H), (20, 20, 24))
        draw = ImageDraw.Draw(sheet)
        for i, s in enumerate(chunk):
            x, y = (i % COLS) * TILE_W, (i // COLS) * TILE_H
            try:
                tile = Image.open(s.thumb).convert("RGB").resize((TILE_W - 4, TILE_H - 4))
                sheet.paste(tile, (x + 2, y + 2))
            except OSError:
                pass
            label = f"#{s.id}"
            box = draw.textbbox((0, 0), label, font=font)
            draw.rectangle([x + 2, y + 2, x + 10 + box[2] - box[0], y + 8 + box[3] - box[1]], fill=(0, 0, 0))
            draw.text((x + 6, y + 2), label, fill=(255, 235, 60), font=font)
        buf = io.BytesIO()
        sheet.save(buf, "JPEG", quality=80)
        sheets.append(buf.getvalue())
    return sheets


def shot_table(footage: Footage, shots: list[Shot]) -> str:
    lines = ["id | video | at (s) | length (s) | motion | brightness | note"]
    for s in shots:
        note = "still" if s.static else ""
        lines.append(f"{s.id} | {s.source + 1} | {s.start:.1f} | {s.duration:.1f} | {s.motion:.2f} | "
                     f"{s.brightness:.2f} | {note}")
    return "\n".join(lines)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: float = 0.0

    def add(self, usage, model: str) -> float:
        _, price_in, price_out, _ = MODELS.get(model, MODELS[DEFAULT_MODEL])
        inp = getattr(usage, "input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cost = (inp * price_in + cw * price_in * 1.25 + cr * price_in * 0.1 + out * price_out) / 1e6
        self.input_tokens += inp
        self.output_tokens += out
        self.cache_read += cr
        self.cache_write += cw
        self.cost += cost
        return cost


@dataclass
class AIDirector:
    """One conversation with Claude about one edit. Call direct() once, then refine()."""

    api_key: str
    model: str = DEFAULT_MODEL
    effort: str = "high"
    client: object = None             # injectable for tests
    messages: list = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    last_raw: dict | None = None

    def _client(self):
        if self.client is None:
            import anthropic

            if not self.api_key:
                raise AIDirectorError("Add your Anthropic API key in Settings to use the AI Director.")
            self.client = anthropic.Anthropic(api_key=self.api_key, max_retries=3, timeout=600)
        return self.client

    def direct(self, prompt: str, song: SongMap, footage: Footage, song_start: float, song_end: float,
               aspect: str, base_style: str) -> Direction:
        shots = select_shots(footage)
        content: list[dict] = []
        sheets = contact_sheets(shots)
        for i, sheet in enumerate(sheets):
            content.append({"type": "text", "text": f"Contact sheet {i + 1} of {len(sheets)}:"})
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg",
                "data": base64.standard_b64encode(sheet).decode("ascii")}})
        videos = "\n".join(f"video {i + 1}: {src.path.replace(chr(92), '/').rsplit('/', 1)[-1]} "
                           f"({src.duration / 60:.1f} min)" for i, src in enumerate(footage.sources))
        content.append({"type": "text", "text": (
            f"FOOTAGE\n{videos}\n\nSHOTS\n{shot_table(footage, shots)}\n\n"
            f"SONG MAP\n{json.dumps(song.to_prompt_dict(song_start, song_end))}\n\n"
            f"OUTPUT\naspect {aspect}; edit runs from song second {song_start:.2f} to {song_end:.2f}\n"
            f"Base style the user selected: {base_style} ({STYLES[base_style].description})\n"
            "Available styles: " + "; ".join(f"{s.id}: {s.description}" for s in STYLES.values())),
            # Everything up to here is identical on refine requests: cache it.
            "cache_control": {"type": "ephemeral"}})
        content.append({"type": "text", "text": "THE USER'S IDEA\n" + (prompt.strip() or
                        "(no prompt: make the best edit you can for this song and footage)")})
        self.messages = [{"role": "user", "content": content}]
        self._song, self._footage, self._range = song, footage, (song_start, song_end)
        return self._ask()

    def refine(self, request: str) -> Direction:
        if not self.messages:
            raise AIDirectorError("Make an edit first, then refine it.")
        self.messages.append({"role": "user", "content": (
            "Change request from the user: " + request.strip() +
            "\nReturn the complete updated plan. Keep everything they did not ask to change.")})
        return self._ask()

    def _ask(self) -> Direction:
        import anthropic

        client = self._client()
        kwargs = dict(
            model=self.model, max_tokens=32000, system=SYSTEM_PROMPT, messages=self.messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": EDIT_SCHEMA}},
        )
        if MODELS.get(self.model, MODELS[DEFAULT_MODEL])[3]:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        try:
            try:
                with client.beta.messages.stream(**kwargs) as stream:
                    message = stream.get_final_message()
            except anthropic.BadRequestError as exc:
                if "fallback" not in str(exc).lower() or "fallbacks" not in kwargs:
                    raise
                kwargs.pop("fallbacks")
                kwargs.pop("betas")
                with client.beta.messages.stream(**kwargs) as stream:
                    message = stream.get_final_message()
        except anthropic.AuthenticationError:
            raise AIDirectorError("Your API key was rejected. Check it in Settings.") from None
        except anthropic.PermissionDeniedError:
            raise AIDirectorError("This API key can't use that model. Pick another model in Settings.") from None
        except anthropic.RateLimitError:
            raise AIDirectorError("Too many requests right now. Wait a minute and try again.") from None
        except anthropic.BadRequestError as exc:
            msg = getattr(exc, "message", str(exc))
            if "credit" in msg.lower() or "billing" in msg.lower():
                raise AIDirectorError("Your Anthropic account is out of credit. Add credit at "
                                      "console.anthropic.com, then try again.") from None
            raise AIDirectorError(f"The AI request was rejected: {msg}") from None
        except anthropic.APIStatusError as exc:
            raise AIDirectorError(f"The AI service had a problem ({exc.status_code}). Try again shortly.") from None
        except anthropic.APIConnectionError:
            raise AIDirectorError("Couldn't reach the AI service. Check your internet connection.") from None

        cost = self.usage.add(message.usage, self.model)
        log.info("AI Director: %s in / %s out tokens, $%.3f", message.usage.input_tokens,
                 message.usage.output_tokens, cost)
        if message.stop_reason == "refusal":
            self.messages.pop()
            raise AIDirectorError("The AI declined this request. Try rewording your idea.")
        if message.stop_reason == "max_tokens":
            self.messages.pop()
            raise AIDirectorError("The AI's plan was cut off. Try a shorter part of the song.")
        text = next((b.text for b in message.content if b.type == "text"), "")
        try:
            raw = json.loads(text)
        except ValueError:
            self.messages.pop()
            raise AIDirectorError("The AI returned an unreadable plan. Try again.") from None
        # Keep the full reply (thinking blocks included) so refine turns build on it.
        self.messages.append({"role": "assistant", "content": message.content})
        self.last_raw = raw
        return to_direction(raw, self._footage, *self._range)


def to_direction(raw: dict, footage: Footage, start: float, end: float) -> Direction:
    """Convert (and sanity-check) the AI's JSON into a Direction."""
    style = copy.deepcopy(STYLES.get(raw.get("style"), STYLES["hype"]))
    lk = raw.get("look") or {}
    look = Look(
        grade=lk.get("grade") if lk.get("grade") in GRADES else style.look.grade,
        glow=float(np.clip(lk.get("glow", 0), 0, 1)),
        trails=float(np.clip(lk.get("trails", 0), 0, 1)),
        letterbox=bool(lk.get("letterbox", False)),
        grain=float(np.clip(lk.get("grain", 0), 0, 1)),
    )
    valid = set(range(len(footage.shots)))
    sections = []
    notes = []
    for s in sorted(raw.get("sections") or [], key=lambda s: s.get("start", 0)):
        a, b = float(s.get("start", start)), float(s.get("end", end))
        if b <= a:
            continue
        every = min(CUT_CHOICES, key=lambda c: abs(c - float(s.get("cut_every_beats", 2))))
        fx = {k: float(np.clip((s.get("effects") or {}).get(k, 0), 0, 1)) for k in EFFECTS}
        sections.append(SectionDirection(
            start=a, end=b, cut_every=every,
            energy=float(np.clip(s.get("intensity", 0.5), 0, 1)),
            shots=[i for i in s.get("shots", []) if i in valid], effects=fx, note=s.get("note", "")))
        if s.get("note"):
            notes.append(f"{_mmss(a)}–{_mmss(b)}: {s['note']}")
    # Close gaps so the whole range is directed.
    if sections:
        sections[0].start = min(sections[0].start, start)
        sections[-1].end = max(sections[-1].end, end + 1)
        for p, q in zip(sections[:-1], sections[1:]):
            if q.start > p.end:
                p.end = q.start
    moments = [Moment(float(m["time"]), m["action"], float(max(0, m.get("duration", 0))))
               for m in raw.get("moments") or [] if m.get("action") in MOMENT_ACTIONS]
    return Direction(style=style, look=look, sections=sections, moments=moments,
                     avoid_shots=[i for i in raw.get("avoid_shots", []) if i in valid],
                     summary=raw.get("summary", ""), notes=notes)


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"
