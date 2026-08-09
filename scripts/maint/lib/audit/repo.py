"""lib/audit/repo.py — the ONLY module that touches the filesystem or git.

Concentrating I/O here is what makes the rest of the regime testable without a
checkout: every detector takes a Repo and every test can hand it a fake one.

Determinism rules enforced here, not left to callers:
  - `git ls-files` output is sorted and POSIX-slashed, so a Windows checkout
    and a Linux CI runner produce the same list in the same order.
  - text is read with CRLF normalised to LF, so a Windows autocrlf checkout
    cannot change a regex match or a digest.
  - undecodable bytes are replaced, never raised: a stray byte in one file must
    not take the whole audit down (that would be a failure-recovery defect in
    the auditor itself).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .model import RegimeError


def _translate_glob(pattern: str) -> "re.Pattern[str]":
    """Glob -> regex with the one rule everyone gets wrong made explicit:
    `*` does NOT cross a '/', `**` does.
    """
    out: List[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # `**/` collapses to "any number of leading dirs, or none".
                if i + 2 < n and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
            i += 1
            continue
        out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


_GLOB_CACHE: Dict[str, "re.Pattern[str]"] = {}


def glob_match(pattern: str, path: str) -> bool:
    rx = _GLOB_CACHE.get(pattern)
    if rx is None:
        rx = _translate_glob(pattern)
        _GLOB_CACHE[pattern] = rx
    return bool(rx.match(path))


class Repo:
    """A git checkout, read-only, with everything cached for determinism."""

    def __init__(self, root: Path, tracked: Optional[Sequence[str]] = None):
        self.root = Path(root)
        self._tracked: Optional[List[str]] = list(tracked) if tracked is not None else None
        self._text_cache: Dict[str, str] = {}

    # -- git ---------------------------------------------------------------
    @property
    def tracked(self) -> List[str]:
        """Every path in the git index, sorted, POSIX-slashed.

        The index (not the working tree) is the boundary on purpose: a file
        that has been `git add`ed is in scope even before it is committed, and
        a file that is only on disk is NOT in scope. That is the same rule CI
        sees after checkout, so local and CI runs agree.
        """
        if self._tracked is None:
            try:
                proc = subprocess.run(
                    ["git", "ls-files", "-z"],
                    cwd=str(self.root), capture_output=True, check=True,
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                # No git => we cannot know the boundary. Refusing is the only
                # honest answer; guessing with os.walk would silently change
                # what "in scope" means between environments.
                raise RegimeError(
                    "cannot enumerate tracked files: `git ls-files` failed (" + str(exc) + "). "
                    "The audit boundary is defined by the git index; it will not guess."
                ) from exc
            raw = proc.stdout.decode("utf-8", "replace")
            self._tracked = sorted(p.replace("\\", "/") for p in raw.split("\0") if p)
        return self._tracked

    def tracked_matching(self, patterns: Iterable[str]) -> List[str]:
        pats = list(patterns)
        return [p for p in self.tracked if any(glob_match(g, p) for g in pats)]

    def is_tracked(self, rel: str) -> bool:
        return rel.replace("\\", "/") in set(self.tracked)

    # -- files -------------------------------------------------------------
    def exists(self, rel: str) -> bool:
        return (self.root / rel).is_file()

    def read(self, rel: str) -> str:
        """Text of a repo-relative path, LF-normalised, cached."""
        key = rel.replace("\\", "/")
        cached = self._text_cache.get(key)
        if cached is not None:
            return cached
        try:
            data = (self.root / key).read_bytes()
        except OSError as exc:
            raise RegimeError("cannot read tracked file " + key + ": " + str(exc)) from exc
        text = data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
        self._text_cache[key] = text
        return text

    def read_optional(self, rel: str) -> Optional[str]:
        """Text, or None if absent. For S2 members that only exist on the
        operator's workstation — their absence must be a COUNTED skip, never a
        silent pass, so callers are forced to handle None explicitly."""
        if not (self.root / rel).is_file():
            return None
        return self.read(rel)


def find_repo_root(start: Optional[Path] = None) -> Path:
    """Walk up from this file to the checkout root (the dir holding manifest/).

    Not `git rev-parse --show-toplevel`: inside a linked worktree that is still
    correct, but this keeps the one filesystem assumption in one place and
    works in a tarball export where the detectors still run.
    """
    here = Path(start) if start else Path(__file__).resolve()
    for cand in [here] + list(here.parents):
        if (cand / "manifest" / "apps.yaml").is_file():
            return cand
    raise RegimeError("could not locate repo root above " + str(here))
