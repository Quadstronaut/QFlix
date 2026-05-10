"""Gemini-backed 'AI Picks' — small section at the bottom of the newsletter."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional, Sequence

logger = logging.getLogger(__name__)


@dataclass
class AiPick:
    if_you_liked: str
    try_this: str
    blurb: str


def build_prompt(library_titles: Sequence[str], recent_movies: Sequence[str]) -> str:
    library_sample = ", ".join(library_titles[:25]) or "(no data)"
    recents = ", ".join(recent_movies[:10]) or "(no data)"
    return (
        "You are recommending movies to a friend whose Plex library contains: "
        f"{library_sample}. They recently added: {recents}. "
        "Suggest 3 'if you liked X, try Y' picks where X is in their library/recents "
        "and Y is something they likely don't have. "
        "Output STRICT JSON: a list of 3 objects each with keys "
        "'if_you_liked', 'try_this', 'blurb' (one short sentence). "
        "No markdown fencing, no commentary, JSON only."
    )


def fetch_ai_picks(
    api_key: Optional[str],
    library_titles: Sequence[str],
    recent_titles: Sequence[str],
    *,
    model: str = "gemini-2.0-flash",
) -> list[AiPick]:
    """Fetch 3 AI picks. Returns [] on any failure — newsletter must still send."""
    if not api_key:
        return []
    try:
        import google.generativeai as genai  # type: ignore
    except ImportError:
        logger.warning("google-generativeai not installed; skipping AI picks")
        return []

    try:
        genai.configure(api_key=api_key)
        m = genai.GenerativeModel(model)
        response = m.generate_content(build_prompt(library_titles, recent_titles))
        return _parse_picks(response.text)
    except Exception as exc:
        logger.warning("Gemini call failed: %s", exc)
        return []


def _parse_picks(raw: str) -> list[AiPick]:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        # Strip optional ```json fences
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Gemini returned non-JSON: %.120s...", raw)
        return []
    if not isinstance(data, list):
        return []
    out: list[AiPick] = []
    for row in data[:3]:
        if not isinstance(row, dict):
            continue
        try:
            out.append(
                AiPick(
                    if_you_liked=str(row["if_you_liked"]),
                    try_this=str(row["try_this"]),
                    blurb=str(row["blurb"]),
                )
            )
        except KeyError:
            continue
    return out
