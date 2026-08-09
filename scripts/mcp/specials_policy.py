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

BLAST RADIUS. This is a convergent mutator: it re-asserts the invariant every
day at 06:00 UTC, so it does not act once — it argues with anything that
disagrees, forever. Selection is broad ("Season 0" is not a synonym for
"unwanted": it unmonitors S0 episodes that already have files, which blocks
upgrades and re-acquisition after a loss), and a single upstream reclassification
can move a whole 24-episode season into Specials on Sonarr's nightly refresh.
So it carries the same three rails qflix-reaper.py does, minus the --execute gate
(a timer-driven convergent janitor has no useful dry-run default):

  * MAX_UNMONITORS_PER_RUN — overflow is DEFERRED to the next run, never
    dropped, and the deferral is counted and named. Convergence still reaches
    the same end state, just across more runs.
  * an EXCLUDE FILE — an operator who deliberately monitors a special needs a
    lever that survives convergence. Without one the only lever was disabling
    the timer for every series.
  * NOTIFICATION ESCALATION — past LOUD_UNMONITORS a run stops reading as
    routine and notifies at warning level.

Spec: docs/superpowers/specs/2026-07-18-tv-fallback-v2-design.md

Modes: --cron | --emit-json | --dry-run
       --cron is the ONLY live path. --emit-json and --dry-run are both
       read-only (no arr writes, no notifications), matching what --emit-json
       means in every other scripts/mcp/ module.
Args:  --slug <name>          limit to one TV instance
       --exclude-file <path>  default scripts/mcp/specials_policy.exclude
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

# Blast cap, per instance, per run. 50 mirrors qflix-reaper's DEFAULT_MAX_ITEMS.
# Sized against the live census (2026-08-03: 264 S0 episodes in scope on sonarr,
# 20 on sonarr2, all already converged) so a normal run is nowhere near it and
# only an ANOMALY — an upstream reclassification dumping a whole season into
# Specials — trips it. Overflow DEFERS, matching the reaper's defer-oldest-N
# change of 2026-07-14; aborting would stall convergence entirely.
MAX_UNMONITORS_PER_RUN = 50

# Past this, a run is no longer routine and the Discord level escalates from
# info to warning. A mass unmonitor must not read like a Tuesday.
LOUD_UNMONITORS = 25

EXCLUDE_PATH = HERE / "specials_policy.exclude"


# ---------------------------------------------------------------------------
# Exclusions — an operator exception has to survive a CONVERGENT janitor
# ---------------------------------------------------------------------------

def load_exclusions(path: Optional[Path] = None) -> set:
    """Read the exclude file into a set of tokens.

    One entry per line; `#` starts a comment; blanks ignored. An entry matches a
    series by tvdbId (preferred — stable across renames) or by exact title.
    A MISSING FILE IS NORMAL and means no exclusions; an UNREADABLE one is not
    silently treated as empty, it raises, because "the operator's exception list
    could not be read" must never quietly become "there are no exceptions".
    """
    path = path or EXCLUDE_PATH
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def _is_excluded(series: dict, tokens: set) -> bool:
    if not tokens:
        return False
    return (str(series.get("tvdbId")) in tokens
            or (series.get("title") or "") in tokens)


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

def enforce_instance(client, dry_run: bool, exclusions: Optional[set] = None,
                     max_unmonitors: int = MAX_UNMONITORS_PER_RUN) -> dict:
    code, series = client.get("/series")
    if code != 200 or not isinstance(series, list):
        return {"status": "failed-series-list", "code": code}

    exclusions = exclusions or set()
    changes: list = []
    eps_unmonitored = 0
    fetch_failures: list = []
    excluded: list = []
    deferred: list = []
    for s in series:
        if not _needs_scan(s):
            continue
        if _is_excluded(s, exclusions):
            # Rule 4: a skip is COUNTED AND NAMED. An exclusion that vanished
            # from the output would rot into a permanent silent carve-out.
            excluded.append(s.get("title", "?"))
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
        # BLAST CAP, checked once the real count is known so the total cannot
        # overshoot. `changes and` guarantees forward progress: the first series
        # of a run always proceeds, so a single series holding more than the cap
        # still converges instead of deferring itself forever.
        if changes and eps_unmonitored + len(mon_ids) > max_unmonitors:
            # DEFER, never abort — this janitor is convergent, so the next run
            # picks them up and the end state is identical. Only the per-run
            # blast radius is bounded.
            deferred.append(s.get("title", "?"))
            continue
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
           "episodes_unmonitored": eps_unmonitored, "changes": changes,
           "excluded": excluded, "excluded_count": len(excluded),
           "deferred": deferred, "deferred_count": len(deferred),
           "max_unmonitors_per_run": max_unmonitors}
    if fetch_failures:
        out["episode_fetch_failures"] = fetch_failures
    return out


def run(*, client_factory=None, dry_run: bool = False,
        slug: Optional[str] = None,
        exclude_file: Optional[Path] = None) -> dict:
    client_factory = client_factory or (lambda s: ArrClient(s, "v3"))
    exclusions = load_exclusions(exclude_file)
    out: dict = {"per_arr": {}, "exclusions_loaded": sorted(exclusions)}
    for s in TV_ARRS:
        if slug and s != slug:
            continue
        try:
            out["per_arr"][s] = enforce_instance(client_factory(s), dry_run,
                                                 exclusions)
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
        sys.stderr.write("specials_policy.py: notify failed - alerts unavailable from this script: "
                         + repr(_exc) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--cron", action="store_true")
    g.add_argument("--emit-json", action="store_true")
    g.add_argument("--dry-run", action="store_true")
    ap.add_argument("--slug")
    ap.add_argument("--exclude-file", type=Path, default=None,
                    help="series to leave alone (tvdbId or exact title, one "
                         "per line); default scripts/mcp/specials_policy.exclude")
    args = ap.parse_args()

    # --emit-json is READ-ONLY, like every other scripts/mcp/ module. It used to
    # run the full live mutation path and always exit 0 — the same flag name
    # that means "read" in logs.py / collect.py / missing.py issued *arr writes
    # here. --cron is the only live path.
    res = run(dry_run=args.dry_run or args.emit_json, slug=args.slug,
              exclude_file=args.exclude_file)
    failed = [s for s, r in res["per_arr"].items() if r.get("status") != "ok"]
    changed = [(s, r) for s, r in res["per_arr"].items()
               if r.get("status") == "ok" and r.get("series_changed", 0) > 0]
    deferred = [(s, r) for s, r in res["per_arr"].items()
                if r.get("deferred_count", 0) > 0]

    if args.cron:
        if changed:
            # Level escalates with blast radius: a run that unmonitors a whole
            # reclassified season must not read like routine convergence.
            loud = any(r["episodes_unmonitored"] >= LOUD_UNMONITORS
                       for _s, r in changed)
            lines = [f"[{s}] unmonitored {r['episodes_unmonitored']} Season-0 "
                     f"episode(s) across {r['series_changed']} series: "
                     + ", ".join(c["title"] for c in r["changes"])
                     + (f" (excluded {r['excluded_count']}, "
                        f"deferred {r['deferred_count']})"
                        if r["excluded_count"] or r["deferred_count"] else "")
                     for s, r in changed]
            _notify("specials_policy enforced:\n" + "\n".join(lines),
                    "warning" if loud else "info")
        if deferred:
            # Rule 4: the cap is never silent. If it trips repeatedly the
            # operator should be looking at WHY, not at the cap.
            _notify("specials_policy hit its per-run cap ("
                    f"{MAX_UNMONITORS_PER_RUN}); deferred to the next run: "
                    + "; ".join(f"[{s}] " + ", ".join(r["deferred"])
                                for s, r in deferred), "warning")
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
