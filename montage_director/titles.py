"""Text cards: titles, captions and credits drawn as transparent pictures.

The bundled FFmpeg has no text filter, so each card is drawn once with Pillow
(in the classic AMV look: a bold font, white with a black outline and a soft glow)
and laid over the video by the renderer.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .paths import cache_dir

STYLES = ("title", "caption", "lower", "credits")
STYLE_NAMES = {"title": "Big title (middle)", "caption": "Caption (bottom)", "lower": "Name tag (bottom left)",
               "credits": "Credits (middle, smaller)"}
TEXT_VERSION = 1


def _font_files() -> list[str]:
    if sys.platform.startswith("win"):
        root = Path("C:/Windows/Fonts")
        names = ["impact.ttf", "arialbd.ttf", "segoeuib.ttf", "arial.ttf"]
        return [str(root / n) for n in names]
    if sys.platform == "darwin":
        return ["/System/Library/Fonts/Supplemental/Impact.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                "/Library/Fonts/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc"]
    return ["/usr/share/fonts/truetype/msttcorefonts/Impact.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"]


def _font(size: int):
    for f in _font_files():
        if Path(f).is_file():
            try:
                return ImageFont.truetype(f, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    lines = []
    for para in text.split("\n"):
        words, cur = para.split(), ""
        for w in words:
            trial = (cur + " " + w).strip()
            if draw.textlength(trial, font=font) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return [l for l in lines if l] or [""]


def text_card(text: str, style: str, width: int, height: int) -> Path:
    """Draw a card (cached by content) and return its path."""
    style = style if style in STYLES else "title"
    key = hashlib.sha1(f"{TEXT_VERSION}|{text}|{style}|{width}x{height}".encode()).hexdigest()[:16]
    out = cache_dir() / "texts" / f"t_{key}.png"
    if out.is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    short = min(width, height)
    size = {"title": 0.13, "credits": 0.06, "caption": 0.055, "lower": 0.05}[style] * short * (1.25 if width < height else 1)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    words = text.upper() if style == "title" else text
    max_w = int(width * (0.88 if style != "lower" else 0.6))
    font = _font(int(size))
    lines = _wrap(draw, words, font, max_w)
    while style == "title" and len(lines) > 3 and size > short * 0.05:  # long titles: smaller, not more lines
        size *= 0.85
        font = _font(int(size))
        lines = _wrap(draw, words, font, max_w)
    gap = int(size * 0.18)
    heights = [draw.textbbox((0, 0), l, font=font)[3] - draw.textbbox((0, 0), l, font=font)[1] for l in lines]
    block = sum(heights) + gap * (len(lines) - 1)
    if style in ("title", "credits"):
        y = (height - block) // 2
    elif style == "caption":
        y = int(height * 0.86) - block
    else:
        y = int(height * 0.80) - block
    stroke = max(2, int(size * (0.07 if style == "title" else 0.05)))
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    for line, h in zip(lines, heights):
        w = draw.textlength(line, font=font)
        x = int(width * 0.06) if style == "lower" else int((width - w) / 2)
        top = draw.textbbox((0, 0), line, font=font)[1]
        gdraw.text((x, y - top), line, font=font, fill=(255, 255, 255, 150), stroke_width=stroke * 2,
                   stroke_fill=(255, 255, 255, 90))
        draw.text((x, y - top), line, font=font, fill=(255, 255, 255, 255), stroke_width=stroke,
                  stroke_fill=(0, 0, 0, 255))
        y += h + gap
    if style == "title":  # soft glow behind the letters (2000s look)
        glow = glow.filter(ImageFilter.GaussianBlur(max(2, size * 0.12)))
        img = Image.alpha_composite(glow, img)
    img.save(out)
    return out
