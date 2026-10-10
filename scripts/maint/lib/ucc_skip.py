#!/usr/bin/env python3
"""lib/ucc_skip.py — which UCC slugs must never be woken (spec I-9, QFLX-17).

WHY THIS EXISTS: the UCC divorce converts apps one by one from a UCC container
(`app-<slug>`) to a native systemd unit. The old container stays installed and
STOPPED as the rollback target (I-8), so both `~/.apps/<slug>/` and the
`app-<slug>` wrapper keep existing after the swap. Anything that discovers apps
by directory or wrapper (the Monday sweep walks ~/.apps, F-7) or that names a
slug from a secret (the UCC gate probe runs `app-<probe_app> start` every 5
minutes, F-5) would start the dormant container next to the native unit: two
runtimes on one config dir and one port (I-6).

The rule, read from the DEPLOYED manifest (never restated by hand):
  * skip   = every app whose class is not `ucc`, or that carries ucc_dormant
  * active = every app whose class is `ucc` and is not dormant
Both the manifest key and its `ucc_slug` are covered, since ~/.apps dirs and
`app-*` wrappers are named by the ucc_slug. A slug claimed by both sides is
skipped: when in doubt, never wake.

`ucc_dormant` fails closed: any value other than absent/null/false counts as
dormant, so a typo like `ucc_dormant: "yes"` still protects the container.

Deliberately stdlib + PyYAML only, no `lib.*` imports: the sweep and the
configure scripts run this file directly as a script, and lib/ is a merged
namespace package (never add an __init__.py).

CLI (exit codes are the contract the shell callers rely on):
  ucc_skip.py [--manifest PATH] --list        skip slugs, one per line; 0 ok, 2 error
  ucc_skip.py [--manifest PATH] --check SLUG  0 = active ucc slug (safe to drive
                                              through app-<slug>), 3 = not, 2 = error
PATH defaults to $MANITOBA_MANIFEST, else ~/.opt/maint/apps.yaml (the copy 240
deploys). There is no repo fallback: the box has no checkout, and guessing a
manifest is exactly how a dormant app would get woken.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Iterable, Union

PathLike = Union[str, "os.PathLike[str]"]

# Names flow into bash comparisons and `app-<slug>` command names, so anything
# that is not a plain slug is a hard error rather than something to quote.
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SkipListError(Exception):
    """The manifest cannot be read or interpreted; callers must fail closed."""


def default_manifest_path() -> Path:
    """$MANITOBA_MANIFEST, else the deployed ~/.opt/maint/apps.yaml. Lazy: read
    at call time so tests (and the sweep's env) can redirect it."""
    env = os.environ.get("MANITOBA_MANIFEST")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".opt" / "maint" / "apps.yaml"


def _is_dormant(value: object) -> bool:
    return value is not None and value is not False


def _names(app_name: object, data: dict) -> set[str]:
    out = set()
    for n in (app_name, data.get("ucc_slug")):
        if n is None:
            continue
        if not isinstance(n, str) or not _SLUG_RE.match(n):
            raise SkipListError(f"unsafe app/ucc_slug name in manifest: {n!r}")
        out.add(n)
    return out


def _classify(path: PathLike | None) -> tuple[set[str], set[str]]:
    """Return (active_ucc, skip) for the manifest at *path*."""
    p = Path(path) if path is not None else default_manifest_path()
    try:
        import yaml  # local import: a missing PyYAML is a SkipListError too
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception as exc:  # missing file, permissions, YAML syntax, no yaml
        raise SkipListError(f"cannot read manifest {p}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("apps"), dict):
        raise SkipListError(f"manifest {p} has no 'apps' mapping")

    active: set[str] = set()
    skip: set[str] = set()
    for name, entry in data["apps"].items():
        if not isinstance(entry, dict):
            raise SkipListError(f"manifest {p}: app {name!r} is not a mapping")
        names = _names(name, entry)
        if entry.get("class") == "ucc" and not _is_dormant(entry.get("ucc_dormant")):
            active |= names
        else:
            skip |= names
    return active - skip, skip


def skip_slugs(path: PathLike | None = None) -> list[str]:
    """Sorted slugs the Monday sweep (and anything else) must never wake."""
    return sorted(_classify(path)[1])


def active_ucc_slugs(path: PathLike | None = None) -> set[str]:
    """Slugs that are live UCC apps, the only ones `app-<slug>` may drive."""
    return _classify(path)[0]


def probe_allowed(slug: str, path: PathLike | None = None) -> bool:
    """True only when *slug* is an active, non-dormant ucc app. Raises
    SkipListError when the manifest is unreadable (callers decide the
    fallback; lib/ucc.py allows only its pinned default then)."""
    return slug in active_ucc_slugs(path)


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--manifest", default=None)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true")
    g.add_argument("--check", metavar="SLUG")
    args = ap.parse_args(list(argv) if argv is not None else None)
    try:
        if args.list:
            for s in skip_slugs(args.manifest):
                print(s)
            return 0
        return 0 if probe_allowed(args.check, args.manifest) else 3
    except SkipListError as exc:
        print(f"ucc_skip: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
