"""Behind-the-scenes section.

Two sources, override-then-fallback:

  1. A Claude-authored blurb committed to the `newsletter-digest` branch as
     digest/latest.json — preferred when present and fresh for the send week.
  2. A deterministic recap built straight from the week's public GitHub
     commits — grouped feat / fix lists. Always works.

Every fetch is fail-safe: any network/parse error degrades to the next source
or to "hide the section", so the newsletter always sends.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

import requests

logger = logging.getLogger(__name__)

DEFAULT_REPO = "Quadstronaut/QFlix"
DEFAULT_BRANCH = "newsletter-digest"
DEFAULT_WINDOW_DAYS = 7
DEFAULT_TIMEOUT_S = 15
MAX_BULLETS = 6  # cap visible items per group; full counts still reported

# Conventional-commit subject:  type(scope)!: description
_CC_RE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?:\s*(?P<desc>.+)$")
# Optional friendly override the operator can drop in a commit body:
#   Newsletter: Improved streaming stability
_TRAILER_RE = re.compile(
    r"^\s*(?:Newsletter|Digest):\s*(?P<text>.+?)\s*$", re.IGNORECASE | re.MULTILINE
)

FEATURE_TYPES = {"feat"}
FIX_TYPES = {"fix", "perf"}


@dataclass
class Commit:
    sha: str
    type: str  # conventional type, or "" when the subject isn't conventional
    scope: Optional[str]
    summary: str  # description with the scope stripped (whole subject if unparseable)
    friendly: Optional[str]  # from a Newsletter:/Digest: trailer, if present
    date: Optional[_dt.datetime] = None

    @property
    def display(self) -> str:
        """What the email shows: the friendly override if given, else the subject."""
        return self.friendly or self.summary


@dataclass
class BehindScenes:
    """What the template renders — either a human blurb OR grouped commit lists."""

    blurb_html: Optional[str] = None  # Claude-authored override (preferred)
    week_of: Optional[str] = None
    features: list[Commit] = field(default_factory=list)  # capped to MAX_BULLETS
    fixes: list[Commit] = field(default_factory=list)  # capped to MAX_BULLETS
    other_count: int = 0  # docs/chore/refactor/etc. — counted, not shown
    feature_count: int = 0  # full count (may exceed len(features))
    fix_count: int = 0
    generated_label: Optional[str] = None  # e.g. "Jun 27" for the date line

    @property
    def has_blurb(self) -> bool:
        return bool(self.blurb_html)

    @property
    def has_items(self) -> bool:
        return bool(self.blurb_html or self.features or self.fixes)

    @property
    def feature_overflow(self) -> int:
        return max(0, self.feature_count - len(self.features))

    @property
    def fix_overflow(self) -> int:
        return max(0, self.fix_count - len(self.fixes))


def parse_commit(sha: str, message: str, date: Optional[_dt.datetime] = None) -> Commit:
    """Parse one commit message into a Commit (subject + optional friendly trailer)."""
    lines = (message or "").splitlines()
    subject = lines[0].strip() if lines else ""
    body = "\n".join(lines[1:])

    friendly = None
    tm = _TRAILER_RE.search(body)
    if tm:
        friendly = tm.group("text").strip()

    m = _CC_RE.match(subject)
    if m:
        return Commit(
            sha=sha,
            type=m.group("type").lower(),
            scope=m.group("scope"),
            summary=m.group("desc").strip(),
            friendly=friendly,
            date=date,
        )
    return Commit(sha=sha, type="", scope=None, summary=subject, friendly=friendly, date=date)


def build_behind_scenes(
    commits: Sequence[Commit], *, max_bullets: int = MAX_BULLETS
) -> BehindScenes:
    """Group commits into feature/fix buckets; everything else is a tail count."""
    features: list[Commit] = []
    fixes: list[Commit] = []
    other = 0
    for c in commits:
        if c.type in FEATURE_TYPES:
            features.append(c)
        elif c.type in FIX_TYPES:
            fixes.append(c)
        else:
            other += 1
    return BehindScenes(
        features=features[:max_bullets],
        fixes=fixes[:max_bullets],
        other_count=other,
        feature_count=len(features),
        fix_count=len(fixes),
    )


def fetch_commits(
    repo: str,
    *,
    since_days: int = DEFAULT_WINDOW_DAYS,
    now: Optional[_dt.datetime] = None,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> list[Commit]:
    """Pull the last `since_days` of commits from the public GitHub API.

    Unauthenticated (public repo, 60 req/hr — one weekly call is plenty). Merge
    commits are dropped. Raises on transport/HTTP error — the caller decides the
    fallback.
    """
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    since = (now - _dt.timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get(
        f"https://api.github.com/repos/{repo}/commits",
        params={"since": since, "per_page": 100},
        headers={"Accept": "application/vnd.github+json"},
        timeout=timeout,
    )
    r.raise_for_status()
    out: list[Commit] = []
    for row in r.json():
        commit = row.get("commit") or {}
        message = commit.get("message") or ""
        subject = message.splitlines()[0].strip() if message else ""
        if subject.startswith("Merge "):
            continue
        date = _parse_iso((commit.get("author") or {}).get("date"))
        out.append(parse_commit(row.get("sha", ""), message, date))
    return out


def fetch_override(
    repo: str,
    *,
    branch: str = DEFAULT_BRANCH,
    now: Optional[_dt.datetime] = None,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> Optional[BehindScenes]:
    """Fetch the Claude-authored blurb from the digest branch.

    Returns None if absent (404), unreachable, malformed, or stale (its `week_of`
    is not within the current send week). Never raises.
    """
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/digest/latest.json"
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # network, JSON, anything — degrade quietly
        logger.info("digest override fetch failed (%s); using deterministic recap", exc)
        return None

    html = (data.get("html") or "").strip()
    week_of = (data.get("week_of") or "").strip()
    if not html:
        return None
    if not _is_fresh(week_of, now):
        logger.info("digest override stale (week_of=%r); using deterministic recap", week_of)
        return None
    return BehindScenes(blurb_html=html, week_of=week_of)


def fetch_behind_scenes(
    repo: str = DEFAULT_REPO,
    *,
    branch: str = DEFAULT_BRANCH,
    since_days: int = DEFAULT_WINDOW_DAYS,
    now: Optional[_dt.datetime] = None,
) -> Optional[BehindScenes]:
    """Override blurb if available + fresh, else deterministic commit recap.

    Returns None only when both are empty (no blurb and no commits), in which
    case the template hides the section.
    """
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)

    override = fetch_override(repo, branch=branch, now=now)
    if override and override.has_blurb:
        return override

    try:
        commits = fetch_commits(repo, since_days=since_days, now=now)
    except Exception as exc:
        logger.warning("changelog fetch failed: %s; Behind-the-scenes hidden", exc)
        return None

    bs = build_behind_scenes(commits)
    bs.generated_label = now.strftime("%b %d")
    return bs if bs.has_items else None


def _parse_iso(raw: Optional[str]) -> Optional[_dt.datetime]:
    if not raw:
        return None
    try:
        return _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_fresh(week_of: str, now: _dt.datetime) -> bool:
    """True if `week_of` (YYYY-MM-DD) lands in the current send week.

    The blurb is generated an hour before the send, same calendar day, so a
    fresh value is hours old. Allow a day of negative skew and up to 4 days
    forward; a week-old blurb (a missed routine run) is rejected so we never
    show last week's copy.
    """
    try:
        d = _dt.datetime.strptime(week_of, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc)
    except (ValueError, TypeError):
        return False
    delta_days = (now - d).total_seconds() / 86400.0
    return -1.0 <= delta_days <= 4.0
