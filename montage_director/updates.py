"""Ask GitHub whether a newer release exists. Never blocks or fails loudly."""

from __future__ import annotations

import json
import re
import urllib.request

from . import __version__

REPO = "keeya30144javan-afk/montagedirector"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"


def _parse(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def newer_release(timeout: float = 6.0) -> tuple[str, str] | None:
    """(version, page url) of a newer release, or None (also on any network problem)."""
    try:
        req = urllib.request.Request(API_URL, headers={"Accept": "application/vnd.github+json",
                                                       "User-Agent": f"MontageDirector/{__version__}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None  # offline, private repo, rate limited: just skip the check
    tag = str(data.get("tag_name") or "")
    if data.get("draft") or data.get("prerelease") or not tag:
        return None
    if _parse(tag) > _parse(__version__):
        return tag.lstrip("v"), str(data.get("html_url") or RELEASES_URL)
    return None
