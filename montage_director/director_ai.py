"""AI Director: Claude watches the footage, reads the song map and the user's
prompt, and returns a structured edit Direction.

Two steps:
1. Describe the footage (once per video, cached): Claude looks at a 3-frame
   strip of every shot (start, peak action, end) and writes a short
   description: who is on screen and what is happening.
2. Direct: with those descriptions, a contact sheet, the song map and the
   user's idea, Claude plans sections, shot order, effects and timed moments.

The user supplies their own Anthropic API key. Each request's cost is
computed from the response's token usage and shown in the app.
"""

from __future__ import annotations

import base64
import copy
import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .direction import (CUT_CHOICES, EFFECTS, MOMENT_ACTIONS, STYLES, Direction, Moment,
                        SectionDirection, asks_for_effects)
from .ffmpeg_tools import check_cancel
from .footage import Footage, Shot
from .music import SongMap
from .paths import cache_dir, file_key
from .plan import Look

log = logging.getLogger(__name__)

MODELS = {
    # id: (label, input $/MTok, output $/MTok, supports server-side fallbacks)
    "claude-opus-5-5": ("Claude Opus 5.5 (best)", 4.00, 20.00, True),
    "claude-sonnet-5-5": ("Claude Sonnet 5.5 (cheaper)", 2.00, 10.00, True),
    "claude-haiku-5-5": ("Claude Haiku 5.5 (cheapest)", 0.10, 0.50, False),
}
DEFAULT_MODEL = "claude-opus-5-5"
MAX_SHEET_SHOTS = 200      # shots shown as pictures in the directing request
MAX_TABLE_SHOTS = 900      # shots listed (with descriptions) in the directing request
TILE_W, TILE_H, COLS, ROWS = 192, 108, 8, 5
STRIP_W, STRIP_H, STRIP_COLS, STRIP_ROWS = 384, 72, 4, 10
GRADES = ["none", "y2k", "sad", "cinematic", "vhs", "mono", "dream"]
SHOT_KINDS = ["action", "character", "scenery", "dialogue", "text_or_ui", "other"]
TAG_VERSION = 2


class AIDirectorError(Exception):
    """Something the user can act on (bad key, no credit, refusal, network)."""


# --------------------------------------------------------------------------- #
# Output schemas (structured outputs guarantee the reply matches them)
# --------------------------------------------------------------------------- #
def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


TAG_SCHEMA = _obj({
    "shots": {"type": "array", "items": _obj({
        "id": {"type": "integer"},
        "desc": {"type": "string", "description": "at most 14 words: who is on screen, what they do, mood"},
        "kind": {"type": "string", "enum": SHOT_KINDS},
    })},
})

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
    "repeats": {"type": "string", "enum": ["never", "rarely", "allowed"],
                "description": "whether a scene may appear twice; 'rarely' unless the user says otherwise"},
    "drops": {"type": "array", "items": {"type": "number"},
              "description": "song seconds where the beat drops: copy the song map's drops unless the user corrects them"},
    "sections": {"type": "array", "items": _obj({
        "start": {"type": "number", "description": "song seconds"},
        "end": {"type": "number", "description": "song seconds"},
        "cut_every_beats": {"type": "number", "description": "one of 0.5, 1, 2, 4, 8"},
        "intensity": {"type": "number", "description": "0 calm to 1 wild: how much motion extra shots should have"},
        "shots": {"type": "array", "items": {"type": "integer"},
                  "description": "shot ids for this section, in playback order, each used once"},
        "effects": _obj({k: {"type": "number", "description": "0 to 1"} for k in EFFECTS}),
        "note": {"type": "string", "description": "one short line about the idea for this section"},
    })},
    "moments": {"type": "array", "items": _obj({
        "time": {"type": "number", "description": "song seconds"},
        "action": {"type": "string", "enum": list(MOMENT_ACTIONS)},
        "duration": {"type": "number", "description": "seconds; 0 for instant effects"},
    })},
    "avoid_shots": {"type": "array", "items": {"type": "integer"},
                    "description": "shots that should never appear (menus, loading screens, title cards, off-topic)"},
})

TAG_PROMPT = """You are logging footage for a video editor. Each labelled strip shows three frames from one shot: its start, its moment of most action, and its end.

For every shot id, write a short description an editor can search: who is on screen (use the character's name when you recognise them, otherwise describe them), what is happening (for example: throwing punches, kicking, firing an energy blast, powering up, flying, falling, explosion, crying, talking, walking, close-up stare, posing, landscape), the camera (still, pan, zoom in, shaking) when you can tell from the three frames, and the mood. Then give its kind:
- action: fighting, attacks, chases, explosions, big movement
- character: a character in focus without much action (stares, reactions, power-ups that are mostly posing)
- scenery: places, sky, establishing shots
- dialogue: talking scenes
- text_or_ui: title cards, captions, numbers, logos, narrator overlays, menus, subscribe screens, credits
- other

Describe every shot id you are given, exactly once."""

SYSTEM_PROMPT = """You are the director of a music video edit: an AMV (anime music video), GMV (game music video) or montage in the style of classic 2000s and early-2010s YouTube edits.

You are given:
- the user's idea for the edit,
- a map of the song: tempo, sections with relative energy, drops and the biggest hits (all times in song seconds),
- a table of the footage's shots with a description of each (who is on screen and what happens), its length, motion (0 = still, 1 = most motion in this footage) and its scene number,
- contact sheets showing a frame of many shots, labelled with shot ids.

Your job is to decide what the edit should feel like and which footage goes where, so the result matches what the user has in their head. Choose shots by what they show: use the descriptions to find specific characters and actions, and the contact sheets for look and colour.

Follow the user's instructions literally, especially timed ones. Times the user writes as m:ss (like 1:34) are song times: 1:34 means 94 seconds into the song. "At 1:34 quick edits of X" means: a section that starts at exactly 94.0, cuts fast (cut_every_beats 0.5 or 1) and lists only shots that show X, in an order that builds. If the user doesn't say how long, run it to the next natural change in the song (a section boundary, a drop, or about 8 seconds). If the footage has no shot matching what they asked for, use the closest match and say so in the summary.

How your plan is used:
- A planner places every cut exactly on the beat. You choose how many beats each cut lasts per section (cut_every_beats: 0.5 = double-time, 1 = every beat, 2, 4, or 8 = long held shots).
- To hold ONE shot for a whole section (a still opening shot, a final pose), give that section exactly one shot and cut_every_beats 8; a shot shorter than the section is slowed down to fit.
- Hits the user wants matched one by one (for example four hard drum hits) need cut_every_beats 1 for that bar, plus a flash or zoom moment on each hit.
- In each section, list the shots to use in playback order. Each listed shot is used at most once. When a section needs more cuts than you listed, the planner fills them with unused shots that match the section's intensity, so list enough shots for fast sections (roughly one per cut).
- Shots with the same scene number are the same moment (pieces of one take, or the same clip shown twice in a compilation). Using two of them reads as a repeat: pick one per scene unless the user wants repeats.
- repeats: "never" if the user doesn't want any scene twice, "allowed" only if they ask for repeats or callbacks, otherwise "rarely".
- drops: the song seconds where the beat drops. Copy the song map's drops unless the user tells you where a drop is.
- Effects (0 to 1) set how often each effect appears in a section: flash (white flash on hits), zoom (zoom punch on downbeats), shake (camera shake), rgb (RGB split glitch), slowmo (slow motion clips), reverse (reversed clips), invert (negative strobe). Big effects belong on drops and big hits.
- Moments are precise events at a song time: freeze (hold a frame), flash, zoom, shake, rgb, invert, slowmo, reverse, fade_white, fade_black, or cut (force a cut).
- Sections must cover the whole requested range without gaps, in order.
- avoid_shots: list every title card, caption-only, logo, menu or other text screen, plus anything off-topic for this edit.
- The style is a base recipe your sections override; the look applies to the whole video.

Write the summary for the user in plain words: what the edit does, and anything in their request you could not do."""


# --------------------------------------------------------------------------- #
# Pictures for the model
# --------------------------------------------------------------------------- #
def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _label(draw, x, y, text, font):
    box = draw.textbbox((0, 0), text, font=font)
    draw.rectangle([x, y, x + 8 + box[2] - box[0], y + 6 + box[3] - box[1]], fill=(0, 0, 0))
    draw.text((x + 4, y), text, fill=(255, 235, 60), font=font)


def _jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


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
                sheet.paste(Image.open(s.thumb).convert("RGB").resize((TILE_W - 4, TILE_H - 4)), (x + 2, y + 2))
            except OSError:
                pass
            _label(draw, x + 2, y + 2, f"#{s.id}", font)
        sheets.append(_jpeg(sheet))
    return sheets


def strip_sheets(shots: list[Shot]) -> list[bytes]:
    """Sheets of 3-frame strips (start, peak, end), STRIP_COLS x STRIP_ROWS, labelled with shot ids."""
    per = STRIP_COLS * STRIP_ROWS
    font = _font(18)
    sheets = []
    for start in range(0, len(shots), per):
        chunk = shots[start:start + per]
        rows = (len(chunk) + STRIP_COLS - 1) // STRIP_COLS
        cell_h = STRIP_H + 4
        sheet = Image.new("RGB", (STRIP_COLS * STRIP_W, rows * cell_h), (20, 20, 24))
        draw = ImageDraw.Draw(sheet)
        for i, s in enumerate(chunk):
            x, y = (i % STRIP_COLS) * STRIP_W, (i // STRIP_COLS) * cell_h
            src = s.strip or s.thumb
            try:
                sheet.paste(Image.open(src).convert("RGB").resize((STRIP_W - 4, STRIP_H)), (x + 2, y + 2))
            except OSError:
                pass
            _label(draw, x + 2, y + 2, f"#{s.id}", font)
        sheets.append(_jpeg(sheet))
    return sheets


def _image_block(data: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(data).decode("ascii")}}


def select_shots(footage: Footage, limit: int, ids: set[int] | None = None) -> list[Shot]:
    """Up to `limit` shots, one per scene first, spread across the footage."""
    shots = [s for s in footage.usable_shots() if ids is None or s.id in ids]
    if len(shots) <= limit:
        return shots
    best: dict[int, Shot] = {}
    for s in shots:  # the most active piece represents each scene
        if s.group not in best or s.motion > best[s.group].motion:
            best[s.group] = s
    picked = sorted(best.values(), key=lambda s: s.id)
    if len(picked) > limit:
        idx = np.linspace(0, len(picked) - 1, limit).round().astype(int)
        picked = [picked[i] for i in sorted(set(idx.tolist()))]
    else:
        rest = [s for s in shots if s not in picked]
        idx = np.linspace(0, len(rest) - 1, max(0, limit - len(picked))).round().astype(int) if rest else []
        picked = sorted(picked + [rest[i] for i in sorted(set(int(i) for i in idx))], key=lambda s: s.id)
    return picked


def shot_table(shots: list[Shot], tags: dict[int, dict]) -> str:
    lines = ["id | scene | video | at (s) | length (s) | motion | description"]
    for s in shots:
        tag = tags.get(s.id, {})
        desc = tag.get("desc", "")
        if tag.get("kind") == "text_or_ui":
            desc = "[TEXT/UI] " + desc
        if s.static and not desc:
            desc = "still image"
        lines.append(f"{s.id} | {s.group} | {s.source + 1} | {s.start:.1f} | {s.duration:.1f} | {s.motion:.2f} | {desc}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
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
    tags: dict[int, dict] = field(default_factory=dict)
    last_raw: dict | None = None

    def _client(self):
        if self.client is None:
            import anthropic

            if not self.api_key:
                raise AIDirectorError("Add your Anthropic API key in Settings to use the AI Director.")
            self.client = anthropic.Anthropic(api_key=self.api_key, max_retries=3, timeout=600)
        return self.client

    def _call(self, system: str, messages: list, schema: dict, effort: str, max_tokens: int):
        """One structured request. Returns (parsed JSON, message). Raises AIDirectorError."""
        import anthropic

        client = self._client()
        kwargs = dict(
            model=self.model, max_tokens=max_tokens, system=system, messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
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
        log.info("AI request: %s in / %s out tokens, $%.3f", message.usage.input_tokens,
                 message.usage.output_tokens, cost)
        if message.stop_reason == "refusal":
            raise AIDirectorError("The AI declined this request. Try rewording your idea.")
        if message.stop_reason == "max_tokens":
            raise AIDirectorError("The AI's answer was cut off. Try a shorter part of the song.")
        text = next((b.text for b in message.content if b.type == "text"), "")
        try:
            return json.loads(text), message
        except ValueError:
            raise AIDirectorError("The AI returned an unreadable answer. Try again.") from None

    # ----- step 1: describe the footage (cached per video) ------------------- #
    def describe_footage(self, footage: Footage, cancel=None, progress=None) -> dict[int, dict]:
        by_source: dict[int, list[Shot]] = {}
        for s in footage.shots:
            by_source.setdefault(s.source, []).append(s)
        tags: dict[int, dict] = {}
        todo: list[Shot] = []
        cache_files = {}
        for src_i, shots in by_source.items():
            path = Path(footage.sources[src_i].path)
            cf = cache_dir() / "tags" / f"{file_key(path, TAG_VERSION, len(shots))}.json"
            cache_files[src_i] = cf
            cached = {}
            if cf.is_file():
                try:
                    cached = {int(k): v for k, v in json.loads(cf.read_text(encoding="utf-8")).items()}
                except (OSError, ValueError):
                    cached = {}
            for local, s in enumerate(shots):
                if local in cached:
                    tags[s.id] = cached[local]
                elif s.usable:
                    todo.append(s)
        if not todo:
            self.tags = tags
            return tags

        per_request = STRIP_COLS * STRIP_ROWS * 4   # four sheets per request
        batches = [todo[i:i + per_request] for i in range(0, len(todo), per_request)]
        done = {"n": 0}

        def run(batch: list[Shot]) -> dict:
            check_cancel(cancel)
            content: list[dict] = []
            for sheet in strip_sheets(batch):
                content.append(_image_block(sheet))
            content.append({"type": "text", "text": "Shot ids to describe: " + ", ".join(str(s.id) for s in batch)})
            raw, _ = self._call(TAG_PROMPT, [{"role": "user", "content": content}], TAG_SCHEMA, "low", 32000)
            done["n"] += len(batch)
            if progress:
                progress(done["n"] / len(todo), f"AI is watching your footage ({done['n']}/{len(todo)} shots)")
            wanted = {s.id for s in batch}
            return {int(t["id"]): {"desc": str(t.get("desc", ""))[:160], "kind": t.get("kind", "other")}
                    for t in raw.get("shots", []) if int(t.get("id", -1)) in wanted}

        if progress:
            progress(0.0, f"AI is watching your footage (0/{len(todo)} shots, once per video)")
        with ThreadPoolExecutor(max_workers=3) as pool:
            for result in pool.map(run, batches):
                tags.update(result)

        # Save per video so the next edit with the same footage skips this step.
        for src_i, shots in by_source.items():
            data = {local: tags[s.id] for local, s in enumerate(shots) if s.id in tags}
            try:
                cache_files[src_i].parent.mkdir(parents=True, exist_ok=True)
                cache_files[src_i].write_text(json.dumps(data), encoding="utf-8")
            except OSError as exc:
                log.warning("Could not cache shot descriptions: %s", exc)
        self.tags = tags
        return tags

    # ----- step 2: direct ----------------------------------------------------- #
    def direct(self, prompt: str, song: SongMap, footage: Footage, song_start: float, song_end: float,
               aspect: str, base_style: str, cancel=None, progress=None) -> Direction:
        self.describe_footage(footage, cancel, (lambda f, m: progress(0.8 * f, m)) if progress else None)
        check_cancel(cancel)
        if progress:
            progress(0.85, "Directing the edit with Claude...")
        table_shots = select_shots(footage, MAX_TABLE_SHOTS)
        sheet_shots = select_shots(footage, MAX_SHEET_SHOTS,
                                   {s.id for s in table_shots
                                    if self.tags.get(s.id, {}).get("kind") != "text_or_ui"})
        content: list[dict] = []
        sheets = contact_sheets(sheet_shots)
        for i, sheet in enumerate(sheets):
            content.append({"type": "text", "text": f"Contact sheet {i + 1} of {len(sheets)}:"})
            content.append(_image_block(sheet))
        videos = "\n".join(f"video {i + 1}: {src.path.replace(chr(92), '/').rsplit('/', 1)[-1]} "
                           f"({src.duration / 60:.1f} min)" for i, src in enumerate(footage.sources))
        content.append({"type": "text", "text": (
            f"FOOTAGE\n{videos}\n\nSHOTS\n{shot_table(table_shots, self.tags)}\n\n"
            f"SONG MAP\n{json.dumps(song.to_prompt_dict(song_start, song_end))}\n\n"
            f"OUTPUT\naspect {aspect}; edit runs from song second {song_start:.2f} to {song_end:.2f} "
            f"({_mmss(song_start)} to {_mmss(song_end)})\n"
            f"Base style the user selected: {base_style} ({STYLES[base_style].description})\n"
            "Available styles: " + "; ".join(f"{s.id}: {s.description}" for s in STYLES.values())),
            # Everything up to here is identical on refine requests: cache it.
            "cache_control": {"type": "ephemeral"}})
        pure = STYLES[base_style].pure and not asks_for_effects(prompt)
        rules = ("\nThe user chose Clean Beat Sync: cuts only. Use style 'clean', grade 'none', all effects 0, "
                 "glow/trails/grain 0, no letterbox, and only 'cut' moments. Cut on every beat "
                 "(cut_every_beats 1) unless the idea asks for another rate." if pure else "")
        content.append({"type": "text", "text": "THE USER'S IDEA\n" + (prompt.strip() or
                        "(no prompt: make the best edit you can for this song and footage)") + rules})
        self._pure = pure
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
        try:
            raw, message = self._call(SYSTEM_PROMPT, self.messages, EDIT_SCHEMA, self.effort, 64000)
        except AIDirectorError:
            if self.messages and self.messages[-1]["role"] == "user" and len(self.messages) > 1:
                self.messages.pop()  # drop the failed change request so the next one starts clean
            raise
        # Keep the full reply (thinking blocks included) so refine turns build on it.
        self.messages.append({"role": "assistant", "content": message.content})
        self.last_raw = raw
        d = to_direction(raw, self._footage, *self._range)
        if getattr(self, "_pure", False):
            # Clean Beat Sync is a promise: whatever the model said, cuts only.
            d.style = copy.deepcopy(STYLES["clean"])
            d.look = Look()
            for sec in d.sections:
                sec.effects = {k: 0.0 for k in EFFECTS}
            d.moments = [m for m in d.moments if m.action == "cut"]
        ui = [sid for sid, t in self.tags.items() if t.get("kind") == "text_or_ui"]
        d.avoid_shots = sorted(set(d.avoid_shots) | set(ui))
        return d


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
    repeats = raw.get("repeats") if raw.get("repeats") in ("never", "rarely", "allowed") else "rarely"
    drops = raw.get("drops")
    drops = sorted({round(float(t), 3) for t in drops if 0 <= float(t)}) if isinstance(drops, list) else None
    return Direction(style=style, look=look, sections=sections, moments=moments,
                     avoid_shots=[i for i in raw.get("avoid_shots", []) if i in valid],
                     summary=raw.get("summary", ""), notes=notes, repeats=repeats, drops=drops)


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"
