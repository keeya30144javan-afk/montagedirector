"""“Did you mean…?”: spot a misspelt word the reader would have understood if it were spelt right.

A word is only suggested when swapping it in actually changes the reading (something new is
understood), so names like “vegeta” or “gundam” are never “corrected”.
"""

from __future__ import annotations

import difflib
import re
from functools import lru_cache

from ..direction import STYLES
from . import vocabulary
from .normalise import TYPOS, normalise

# words people use all the time that the reader doesn't need to know
COMMON = set("""about after again also always another around back because been before being better between both
cant could didnt does doesnt doing done dont each even every feel feels first from going gonna good great have
here into just keep kind know last like little look looks lots made make many maybe more most much need only
other over part parts please really right same scene scenes shot shots show should some something song start
still such sure than that their them then there these they thing think this those through time times very
want wanna well were what when where which while will with without would your youre clip clips video edit
shorts reels youtube instagram tiktok story stories anime movie show series episode
""".split())


@lru_cache(maxsize=1)
def known_words() -> tuple[str, ...]:
    """Every word that appears in the reader's word lists (with optional endings spelt out)."""
    texts: list[str] = list(TYPOS.values()) + list(STYLES)
    for value in vars(vocabulary).values():
        items = value if isinstance(value, (list, tuple)) else [value]
        if isinstance(value, dict):
            items = list(value) + [v for v in value.values() if isinstance(v, str)]
        for item in items:
            for part in (item if isinstance(item, tuple) else (item,)):
                if isinstance(part, str):
                    texts.append(part)
                elif isinstance(part, re.Pattern):
                    texts.append(part.pattern)
    words: set[str] = set()
    for t in texts:
        # "fast(?:er)?" -> fast, faster; "flash(?:es)?" -> flash, flashes
        for base, ends in re.findall(r"([a-z]{3,})\(\?:([a-z|]+)\)\??", t):
            words.update(base + e for e in ends.split("|"))
        words.update(re.findall(r"(?<![\\?])\b[a-z]{4,}\b", t))
    return tuple(sorted(words - COMMON))


def _slips(a: str, b: str) -> int:
    """Typing slips between two words: a wrong, missing or extra letter, or two letters swapped."""
    d = [[i + j if not i * j else 0 for j in range(len(b) + 1)] for i in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


def _items(d) -> set[str]:
    """Each separate thing the reader understood ("drop: more flashes, shake" -> two items)."""
    out = set()
    for n in d.notes:
        prefix, _, rest = n.rpartition(": ")
        out.update(f"{prefix}: {item}" for item in rest.split(", ") if item)
    return out


def _gain(before, after) -> bool:
    """Did the swap make the reader understand something new?"""
    if len(after.unclear) < len(before.unclear):
        return True
    return any("shots of" not in n and not n.startswith("(") for n in _items(after) - _items(before))


def did_you_mean(prompt: str, base, limit: int = 3) -> list[tuple[str, str]]:
    """[(typed, suggestion)] for words that look misspelt. `base` is the style Direction to read against."""
    from .apply import read_prompt  # (apply imports nothing from here)

    from .apply import QUOTE_RX

    text = normalise(QUOTE_RX.sub(" ", prompt))  # titles in quotes are the user's own words
    known = known_words()
    known_set = set(known)
    typed = []
    for w in re.findall(r"[a-z]{4,}", text):
        if w not in known_set and w not in COMMON and w not in typed:
            typed.append(w)
    if not typed:
        return []
    before = read_prompt(text, base)
    out: list[tuple[str, str]] = []
    for w in typed[:8]:
        guesses = [g for g in difflib.get_close_matches(w, known, n=3, cutoff=0.78)
                   if _slips(w, g) <= (1 if len(w) <= 6 else 2)]
        guesses.sort(key=lambda g: sorted(g) != sorted(w))  # swapped letters first: "shaek" -> shake
        for guess in guesses:
            fixed = re.sub(rf"\b{w}\b", guess, text)
            after = read_prompt(fixed, base)
            if _gain(before, after):
                out.append((w, guess))
                text, before = fixed, after  # keep the fix so the next word is judged on its own
                break
        if len(out) >= limit:
            break
    return out
