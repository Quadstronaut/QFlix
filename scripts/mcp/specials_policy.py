#!/usr/bin/env python3
"""scripts/mcp/specials_policy.py — Season-0 specials janitor.

Policy: **Season 0 (specials) is never monitored on QFlix.** Specials — promo
featurettes, recap "Omnibus" episodes, chibi shorts, awards segments,
behind-the-scenes — rarely have standalone releases, so a monitored S00 episode
sits perpetually-missing: it burns indexer queries daily and generates false-red /
alert noise (the 2026-07-18 Ted Lasso / Chainsaw Man digest that motivated this).

Stateless, convergent enforcement. Every run re-asserts the invariant across the
TV instances:
  1. unmonitor any monitored Season-0 episode (PUT /episode/monitor), AND
  2. clear the Season-0 season flag (PUT /series with seasons[S0].monitored=false).

Step 2 is what makes it durable: a series refresh re-monitors episodes to match
the season flag, so episode-only unmonitoring silently regresses.

Deliberately STANDALONE (own module, own timer, own Kuma check) rather than folded
into quality_fallback.py — so it stays compartmentalized and independently
swappable/tunable as QFlix migrates to larger servers.

Spec: docs/superpowers/specs/2026-07-18-tv-fallback-v2-design.md

Modes: --cron | --emit-json | --dry-run
       (--dry-run is read-only: no arr writes, no notifications, JSON to stdout)
Args:  --slug <name>   (limit to one TV instance)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "maint"))

from lib.arr_client import ArrClient  # noqa: E402

TV_ARRS = ["sonarr", "sonarr2"]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _season0(series: dict) -> Optional[dict]:
    return next((s for s in series.get("seasons", [])
                 if s.get("seasonNumber") == 0), None)


def _needs_scan(series: dict) -> bool:
    """Only spend an /episode fetch on series that actually have specials or a
    monitored Season 0 — everything else is a guaranteed no-op."""
    s0 = _season0(series)
    if not s0:
        return False
    stats = s0.get("statistics") or {}
    return bool(s0.get("monitored")) or (stats.get("totalEpisodeCount") or 0) > 0


def _monitored_s0_ids(episodes: list) -> list:
    return [e["id"] for e in episodes
            if e.get("seasonNumber") == 0 and e.get("monitored")]


def _with_season0_unmonitored(series: dict) -> dict:
    """Return a copy of the series with ONLY its Season-0 flag flipped off.
    Every other season dict is passed through verbatim (the same object) — never
    rebuilt — so a season that arrived without a 'monitored' key is preserved
    as-is rather than getting monitored=None injected."""
    out = dict(series)
    out["seasons"] = [
        (dict(s, monitored=False) if s.get("seasonNumber") == 0 else s)
        for s in series.get("seasons", [])
    ]
    return out


# ---------------------------------------------------------------------------
# Per-instance enforcement (the only code that talks to *arr)
# ---------------------------------------------------------------------------

def enforce_instance(client, dry_run: bool) -> dict:
    code, series = client.get("/series")
    if code != 200 or not isinstance(series, list):
        return {"status": "failed-series-list", "code": code}

    changes: list = []
    eps_unmonitored = 0
    fetch_failures: list = []
    for s in series:
        if not _needs_scan(s):
            continue
        code_e, eps = client.get("/episode", query=f"seriesId={s['id']}")
        if code_e != 200 or not isinstance(eps, list):
            # We cannot see this series' episodes, so we must NOT write anything
            # for it: clearing the season flag here would be decoupled from the
            # (unseen) episode unmonitors and could leave S0 episodes monitored
            # under a cleared flag. Skip and surface the failure — convergent
            # (next run retries), and a PERSISTENT failure stays red via a
            # non-ok status instead of silently exiting 0 / Kuma green.
            fetch_failures.append(s.get("id"))
            continue
        mon_ids = _monitored_s0_ids(eps)
        flag_on = bool((_season0(s) or {}).get("monitored"))
        if not mon_ids and not flag_on:
            continue                       # nothing to converge on this series
        changes.append({"series_id": s.get("id"), "title": s.get("title", "?"),
                        "episodes": mon_ids, "cleared_flag": flag_on})
        eps_unmonitored += len(mon_ids)
        if not dry_run:
            if mon_ids:
                client.put("/episode/monitor",
                           body={"episodeIds": mon_ids, "monitored": False})
            if flag_on:
                client.put(f"/series/{s['id']}",
                           body=_with_season0_unmonitored(s))

    status = "ok" if not fetch_failures else "partial-episode-fetch-failure"
    out = {"status": status, "series_changed": len(changes),
           "episodes_unmonitored": eps_unmonitored, "changes": changes}
    if fetch_failures:
        out["episode_fetch_failures"] = fetch_failures
    return out


def run(*, client_factory=None, dry_run: bool = False,
        slug: Optional[str] = None) -> dict:
    client_factory = client_factory or (lambda s: ArrClient(s, "v3"))
    out: dict = {"per_arr": {}}
    for s in TV_ARRS:
        if slug and s != slug:
            continue
        try:
            out["per_arr"][s] = enforce_instance(client_factory(s), dry_run)
        except Exception as e:
            # Per-instance isolation: a malformed payload or transport error on
            # one *arr must not sink the others. Non-ok status -> --cron exit 1.
            out["per_arr"][s] = {"status": "failed-exception",
                                 "error": str(e)[:300]}
    return out


# ---------------------------------------------------------------------------
# Notify + CLI
# ---------------------------------------------------------------------------

def _notify(message: str, level: str = "info") -> None:
    try:
        from lib.notify import notify  # type: ignore
        notify(message, level)
    except Exception as _exc:
        sys.stderr.write("specials_policy.py: notify import failed - alerts unavailable from this script: "
                         + repr(_exc) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--cron", action="store_true")
    g.add_argument("--emit-json", action="store_true")
    g.add_argument("--dry-run", action="store_true")
    ap.add_argument("--slug")
    args = ap.parse_args()

    res = run(dry_run=args.dry_run, slug=args.slug)
    failed = [s for s, r in res["per_arr"].items() if r.get("status") != "ok"]
    changed = [(s, r) for s, r in res["per_arr"].items()
               if r.get("status") == "ok" and r.get("series_changed", 0) > 0]

    if args.cron:
        if changed:
            lines = [f"[{s}] unmonitored {r['episodes_unmonitored']} Season-0 "
                     f"episode(s) across {r['series_changed']} series: "
                     + ", ".join(c["title"] for c in r["changes"])
                     for s, r in changed]
            _notify("specials_policy enforced:\n" + "\n".join(lines), "info")
        if failed:
            _notify(f"specials_policy: failures on {failed}", "error")

    if args.emit_json or args.dry_run:
        # JSON mode always exits 0 — the body carries failure detail (same
        # rationale as missing.py: a nonzero exit makes MCP discard the body).
        json.dump(res, sys.stdout, default=str)
        sys.stdout.write("\n")
        return 0
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
