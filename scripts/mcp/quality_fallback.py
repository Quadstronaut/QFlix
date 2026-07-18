#!/usr/bin/env python3
"""scripts/mcp/quality_fallback.py — two-stage quality loosening for stuck movies.

A movie missing for PROMOTE_DAYS consecutive attempted days gets its quality
profile swapped to "QFlix Fallback HDTV"; DEEPEN_DAYS -> "QFlix Fallback SD";
PARK_DAYS -> restore original profile, unmonitor, alert. Grab at any fallback
stage -> restore original profile (file sits below cutoff; upgradinatorr/RSS
upgrade it later where the original profile allows upgrades).

TV (sonarr/sonarr2) is PARK-ONLY in v2 (no quality loosening — Sonarr profiles
are per-series, so loosening one stuck episode would drop the whole series;
and release-less specials grab nothing at any quality anyway). An aired+searched
real episode (Season 0 excluded — that's the standalone specials_policy janitor's
job) gets a day-5 Discord heads-up, then at PARK_DAYS is unmonitored + alerted.
The ONLY sonarr write is that unmonitor, blast-capped at MAX_TV_PARKS_PER_RUN.

Spec: docs/superpowers/specs/2026-07-18-tv-fallback-v2-design.md
      (v1 movie design: docs/superpowers/specs/2026-06-06-quality-fallback-design.md)
API ground truth: deployed Radarr 6.1.1.10360 / Sonarr 4.0.17.2952 (see the
implementation plan's "RTFM ground truth" section).

Modes: --cron | --emit-json | --dry-run | --bootstrap-profiles
       (--emit-json runs LIVE like --cron, JSON to stdout; --dry-run is the
        read-only mode: no arr writes, no state writes, no notifications)
Args:  --slug <name>  (limit to one instance)
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "maint"))

from lib.arr_client import ArrClient  # noqa: E402

MOVIE_ARRS = ["radarr", "radarr2"]
TV_ARRS = ["sonarr", "sonarr2"]

SOURCE_PROFILE_NAME = "HD Bluray + WEB"
FALLBACK_HDTV = "QFlix Fallback HDTV"
FALLBACK_SD = "QFlix Fallback SD"

# Quality names exactly as deployed Radarr 6.1.1 returns them in
# /qualityprofile/schema. "WEB 720p"/"WEB 480p" are GROUP names
# (WEBDL+WEBRip); leaf names match item["quality"]["name"].
STAGE1_ALLOW = {"HDTV-720p", "HDTV-1080p", "WEB 720p"}
STAGE2_ALLOW = STAGE1_ALLOW | {"SDTV", "DVD", "WEB 480p", "Bluray-480p", "REGIONAL"}
# Operator order: nothing pre-retail, ever. Enforced at profile build AND
# re-checked on every bootstrap write.
BANNED = {"WORKPRINT", "CAM", "TELESYNC", "TELECINE", "DVDSCR"}

PROMOTE_DAYS = 5      # stage 0 -> 1 (Fallback HDTV) / TV day-5 heads-up
DEEPEN_DAYS = 10      # stage 1 -> 2 (Fallback SD)
PARK_DAYS = 15        # movies: restore + unmonitor + alert / TV: unmonitor + alert
MAX_IN_FALLBACK = 25  # per instance, stage >= 1, blast-radius cap (movies)
MAX_TV_PARKS_PER_RUN = 10  # per instance, TV unmonitors per run; overflow defers
SEARCH_FRESH_HOURS = 48  # a day only counts if the sweep actually searched

STATE_PATH = Path.home() / ".apps" / "qflix-fallback" / "state.json"


# ---------------------------------------------------------------------------
# State + time helpers
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    """Load state; tolerate missing/corrupt file (fresh start beats crash —
    worst case counters restart and promotions arrive a few days late)."""
    try:
        s = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(s, dict):
            s.setdefault("movies", {})
            s.setdefault("tv", {})
            return s
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"movies": {}, "tv": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX


def parse_arr_ts(ts: Optional[str]) -> Optional[datetime]:
    """*arr timestamps are ISO-8601 with a Z suffix; fromisoformat rejects Z
    before Python 3.11, so normalize first."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Profile bootstrap (pure builder; API wrapper lives in the API layer below)
# ---------------------------------------------------------------------------

def _item_name(item: dict) -> str:
    """Leaf items carry quality.name; group items carry their own name."""
    q = item.get("quality")
    return q["name"] if q else item.get("name", "")


def build_fallback_profile(source: dict, name: str, extra_allowed: set) -> dict:
    """Pure: clone a QualityProfileResource, widen the allowed set, enforce
    the pre-retail ban. Returns a POST-ready resource (no id)."""
    p = copy.deepcopy(source)
    p.pop("id", None)
    p["name"] = name
    for item in p["items"]:
        nm = _item_name(item)
        if nm in extra_allowed:
            item["allowed"] = True
            # group members mirror the group flag (Radarr UI does the same)
            for sub in item.get("items") or []:
                sub["allowed"] = True
        if nm in BANNED:
            item["allowed"] = False  # ban wins over everything, always
    return p


# ---------------------------------------------------------------------------
# Movie planner — PURE. No I/O, no clock reads; everything injected.
# ---------------------------------------------------------------------------

def _fresh_search(m: dict, now: datetime) -> bool:
    """True if the sweep actually searched recently — proof of an attempt."""
    ts = parse_arr_ts(m.get("lastSearchTime"))
    return ts is not None and (now - ts) <= timedelta(hours=SEARCH_FRESH_HOURS)


def _expected_fb(stage: int, fb_ids: dict) -> Optional[int]:
    return {1: fb_ids["hdtv"], 2: fb_ids["sd"]}.get(stage)


def plan_movies(slug: str, missing: list, movies_by_id: dict, fb_ids: dict,
                state: dict, today: str, now: datetime) -> list:
    """Compute actions and mutate `state` (this slug's keys only) in place.

    state keys: f"{slug}:{tmdbId}" -> {movie_id, days, stage,
        original_profile_id, last_counted, parked, title}
    (If a movie is deleted and re-added in radarr, the keyed tmdbId record
    keeps the old movie_id; Phase 1 misses the lookup and drops the record —
    benign, counters restart next cycle.)
    Returns actions: [{action, movie_id, key, title, to_profile}].
    action in {promote, deepen, park, restore_grabbed, restore_operator}.
    """
    actions: list = []
    prefix = f"{slug}:"
    # Keys dropped for operator override this run: Phase 2 must not
    # immediately re-create them (the movie is still in the missing list);
    # counting restarts on the NEXT run instead.
    overridden: set = set()

    # -- Phase 1: reconcile existing records against library reality --------
    for key in [k for k in state if k.startswith(prefix)]:
        rec = state[key]
        m = movies_by_id.get(rec["movie_id"])
        if m is None:                       # deleted from radarr
            del state[key]
            continue
        if rec["parked"]:
            if m["monitored"]:              # operator re-armed it
                rec.update(parked=False, stage=0, days=0,
                           original_profile_id=None, last_counted="")
            continue
        if rec["stage"] >= 1 and m["qualityProfileId"] != _expected_fb(rec["stage"], fb_ids):
            del state[key]                  # operator override: hands off
            overridden.add(key)
            continue
        if m.get("hasFile"):
            if rec["stage"] >= 1:
                actions.append({"action": "restore_grabbed", "movie_id": m["id"],
                                "key": key, "title": rec["title"],
                                "to_profile": rec["original_profile_id"]})
            del state[key]
            continue
        if not m["monitored"]:
            if rec["stage"] >= 1:
                actions.append({"action": "restore_operator", "movie_id": m["id"],
                                "key": key, "title": rec["title"],
                                "to_profile": rec["original_profile_id"]})
            del state[key]
            continue

    # -- Phase 2: accrue + transition for currently-missing eligibles -------
    # Phase 1 ran first, so a grab/override freeing a slot is reusable now.
    in_fallback = sum(1 for k, r in state.items()
                      if k.startswith(prefix) and r["stage"] >= 1 and not r["parked"])

    for m in missing:
        if not (m.get("monitored") and m.get("isAvailable") and _fresh_search(m, now)):
            continue
        key = f"{slug}:{m['tmdbId']}"
        if key in overridden:
            continue
        rec = state.setdefault(key, {
            "movie_id": m["id"], "days": 0, "stage": 0,
            "original_profile_id": None, "last_counted": "",
            "parked": False, "title": m.get("title", "?")})
        if rec["parked"]:
            continue                        # unmonitored anyway; defensive
        if rec["last_counted"] != today:
            rec["days"] += 1
            rec["last_counted"] = today

        if rec["stage"] == 0 and rec["days"] >= PROMOTE_DAYS:
            if in_fallback >= MAX_IN_FALLBACK:
                continue                    # keep counting; promote when a slot frees
            rec["original_profile_id"] = m["qualityProfileId"]
            rec["stage"] = 1
            in_fallback += 1
            actions.append({"action": "promote", "movie_id": m["id"], "key": key,
                            "title": rec["title"], "to_profile": fb_ids["hdtv"]})
        elif rec["stage"] == 1 and rec["days"] >= DEEPEN_DAYS:
            rec["stage"] = 2
            actions.append({"action": "deepen", "movie_id": m["id"], "key": key,
                            "title": rec["title"], "to_profile": fb_ids["sd"]})
        elif rec["stage"] == 2 and rec["days"] >= PARK_DAYS:
            rec["parked"] = True
            actions.append({"action": "park", "movie_id": m["id"], "key": key,
                            "title": rec["title"],
                            "to_profile": rec["original_profile_id"]})
    return actions


# ---------------------------------------------------------------------------
# TV planner — PURE, park-only (v2). The ONLY write it plans is an unmonitor.
# ---------------------------------------------------------------------------

def plan_tv(slug: str, missing: list, state: dict, today: str,
            now: datetime) -> dict:
    """Day-count aired+searched missing REAL episodes and mutate `state` (this
    slug's keys). Season 0 is excluded — the standalone specials_policy janitor
    keeps S00 unmonitored; excluding it here decouples the two so the park stays
    correct even if that janitor is lagging or disabled.

    Returns {"digest": [...], "parks": [...]}, both lists of
    {slug, series_id, episode_id, season, episode, title, days}:
      - digest: crossed PROMOTE_DAYS, once per episode (day-5 heads-up)
      - parks:  crossed PARK_DAYS, once per episode, capped MAX_TV_PARKS_PER_RUN
                (the caller unmonitors these episodes)
    """
    prefix = f"{slug}:"
    seen = set()
    digest: list = []
    parks: list = []
    parked_this_run = 0

    for e in missing:
        if e.get("seasonNumber") == 0:
            continue                        # specials are never counted/parked
        aired = parse_arr_ts(e.get("airDateUtc"))
        if not (e.get("monitored") and aired and aired <= now
                and _fresh_search(e, now)):
            continue
        key = f"{prefix}{e['id']}"
        seen.add(key)
        rec = state.setdefault(key, {"days": 0, "last_counted": "",
                                     "alerted": False, "parked": False})
        rec.setdefault("parked", False)     # migrate v1 records in place
        if rec["last_counted"] != today:
            rec["days"] += 1
            rec["last_counted"] = today
        entry = {"slug": slug, "series_id": e["seriesId"], "episode_id": e["id"],
                 "season": e["seasonNumber"], "episode": e["episodeNumber"],
                 "title": e.get("title", "?"), "days": rec["days"]}
        if rec["days"] >= PROMOTE_DAYS and not rec["alerted"]:
            rec["alerted"] = True
            digest.append(dict(entry))
        if (rec["days"] >= PARK_DAYS and not rec["parked"]
                and parked_this_run < MAX_TV_PARKS_PER_RUN):
            rec["parked"] = True
            parked_this_run += 1
            parks.append(dict(entry))

    # prune entries no longer missing (grabbed or unmonitored)
    for key in [k for k in state if k.startswith(prefix) and k not in seen]:
        del state[key]
    return {"digest": digest, "parks": parks}


# ---------------------------------------------------------------------------
# API layer — the ONLY code that talks to *arr or the clock
# ---------------------------------------------------------------------------

def _notify(message: str, level: str = "info") -> None:
    """Discord notify, best-effort (lib lives under scripts/maint)."""
    try:
        from lib.notify import notify  # type: ignore
        notify(message, level)
    except Exception:
        pass


def _fetch_paged(client, path: str, page_size: int = 1000) -> list:
    """Drain a paged wanted/* endpoint. One page covers today's library
    sizes; loop is future-proofing. NOTE: totalRecords reflects the
    monitored=true-filtered set, so the termination compare is sound."""
    records, page = [], 1
    while True:
        code, body = client.get(path, query=f"page={page}&pageSize={page_size}&monitored=true")
        if code != 200 or not isinstance(body, dict):
            return records
        batch = body.get("records") or []
        records.extend(batch)
        if len(records) >= int(body.get("totalRecords") or 0) or not batch:
            return records
        page += 1


def _resolve_fb_ids(client) -> Optional[dict]:
    code, profiles = client.get("/qualityprofile")
    if code != 200 or not isinstance(profiles, list):
        return None
    by_name = {p.get("name"): p.get("id") for p in profiles}
    if FALLBACK_HDTV in by_name and FALLBACK_SD in by_name:
        return {"hdtv": by_name[FALLBACK_HDTV], "sd": by_name[FALLBACK_SD]}
    return None


def bootstrap_profiles(slug: str, client) -> dict:
    """Create or update both fallback profiles on one instance. Returns
    {profile_name: id}. Raises RuntimeError on any failure — bootstrap is
    operator-invoked, fail loud."""
    code, listing = client.get("/qualityprofile")
    if code != 200:
        raise RuntimeError(f"{slug}: GET /qualityprofile -> {code}")
    by_name = {p["name"]: p["id"] for p in listing}
    if SOURCE_PROFILE_NAME not in by_name:
        raise RuntimeError(f"{slug}: source profile {SOURCE_PROFILE_NAME!r} not found")
    code, source = client.get(f"/qualityprofile/{by_name[SOURCE_PROFILE_NAME]}")
    if code != 200:
        raise RuntimeError(f"{slug}: GET source profile -> {code}")

    out = {}
    for name, allow in ((FALLBACK_HDTV, STAGE1_ALLOW), (FALLBACK_SD, STAGE2_ALLOW)):
        desired = build_fallback_profile(source, name, allow)
        if name in by_name:
            desired["id"] = by_name[name]
            code, body = client.put(f"/qualityprofile/{by_name[name]}", body=desired)
        else:
            code, body = client.post("/qualityprofile", body=desired)
        if code not in (200, 201, 202) or not isinstance(body, dict):
            raise RuntimeError(f"{slug}: write {name!r} -> {code}: {str(body)[:200]}")
        out[name] = body.get("id") or by_name.get(name)
    return out


_ACTION_NOTIFY = {
    "promote": ("info", "fallback stage 1 (HDTV): {title} — day {days}, searching"),
    "deepen": ("info", "fallback stage 2 (SD retail): {title} — day {days}, searching"),
    "park": ("warning", "UNFINDABLE after {days} days: {title} — profile restored, "
                        "unmonitored. Manual intervention needed."),
    "restore_grabbed": ("info", "fallback GRAB: {title} landed at reduced quality; "
                                "original profile restored (will auto-upgrade where "
                                "the profile allows)"),
    "restore_operator": ("info", "fallback released: operator unmonitored {title}; "
                                 "original profile restored"),
}


def _apply_movie_action(client, act: dict) -> bool:
    """Execute one planned action. Returns True on success. Only ever writes
    qualityProfileId and (on park) monitored — nothing else, by design."""
    mid = act["movie_id"]
    if act["action"] == "park":
        # one editor call: restore profile AND unmonitor (controller
        # null-skips everything else — verified at deployed tag)
        code, _ = client.put("/movie/editor", body={
            "movieIds": [mid], "qualityProfileId": act["to_profile"],
            "monitored": False, "moveFiles": False})
        return code in (200, 202)
    code, _ = client.put("/movie/editor", body={
        "movieIds": [mid], "qualityProfileId": act["to_profile"],
        "moveFiles": False})
    if code not in (200, 202):
        return False
    if act["action"] in ("promote", "deepen"):
        code, _ = client.post("/command",
                              body={"name": "MoviesSearch", "movieIds": [mid]})
        return code in (200, 201)
    return True


def _apply_tv_park(client, episode_id: int) -> bool:
    """The ONLY TV write: unmonitor a genuinely-unfindable episode. Never
    touches quality profiles or anything else."""
    code, _ = client.put("/episode/monitor",
                         body={"episodeIds": [episode_id], "monitored": False})
    return code in (200, 202)


def run(*, client_factory=None, state_path: Path = STATE_PATH,
        now: Optional[datetime] = None, dry_run: bool = False,
        slug: Optional[str] = None) -> dict:
    client_factory = client_factory or (lambda s: ArrClient(s, "v3"))
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    state = load_state(state_path)
    out: dict = {"per_arr": {}, "tv_digest": []}

    for s in MOVIE_ARRS:
        if slug and s != slug:
            continue
        client = client_factory(s)
        fb_ids = _resolve_fb_ids(client)
        if fb_ids is None:
            out["per_arr"][s] = {"status": "skipped-no-fallback-profiles"}
            if not dry_run:
                _notify(f"quality_fallback: fallback profiles missing on {s}; "
                        f"run --bootstrap-profiles", "error")
            continue
        missing = _fetch_paged(client, "/wanted/missing")
        # full library needed: a grabbed movie has LEFT wanted/missing, so
        # reconcile must read it from /movie. O(library) daily, fine today.
        code, all_movies = client.get("/movie")
        if code != 200 or not isinstance(all_movies, list):
            out["per_arr"][s] = {"status": "failed-movie-list", "code": code}
            continue
        movies_by_id = {m["id"]: m for m in all_movies}

        if dry_run:
            scratch = copy.deepcopy(state["movies"])
            actions = plan_movies(s, missing, movies_by_id, fb_ids, scratch, today, now)
            out["per_arr"][s] = {"status": "dry-run", "actions": actions}
            continue

        actions = plan_movies(s, missing, movies_by_id, fb_ids,
                              state["movies"], today, now)
        applied = []
        for act in actions:
            ok = _apply_movie_action(client, act)
            days = state["movies"].get(act["key"], {}).get("days", "?")
            level, tmpl = _ACTION_NOTIFY[act["action"]]
            if ok:
                _notify(f"[{s}] " + tmpl.format(title=act["title"], days=days), level)
            else:
                _notify(f"[{s}] quality_fallback FAILED to apply "
                        f"{act['action']} for {act['title']}", "error")
            applied.append({**act, "ok": ok})
        out["per_arr"][s] = {"status": "ok", "actions": applied,
                             "in_fallback": sum(
                                 1 for k, r in state["movies"].items()
                                 if k.startswith(f"{s}:") and r["stage"] >= 1
                                 and not r["parked"])}

    # ---- TV: day-5 digest + day-15 park (unmonitor) ----------------------
    digest_all: list = []
    parks_all: list = []
    for s in TV_ARRS:
        if slug and s != slug:
            continue
        client = client_factory(s)
        missing = _fetch_paged(client, "/wanted/missing")
        if dry_run:
            scratch = copy.deepcopy(state["tv"])
            plan = plan_tv(s, missing, scratch, today, now)
            digest_all.extend(plan["digest"])
            parks_all.extend(plan["parks"])
            continue
        plan = plan_tv(s, missing, state["tv"], today, now)
        digest_all.extend(plan["digest"])
        for p in plan["parks"]:
            ok = _apply_tv_park(client, p["episode_id"])
            p["ok"] = ok
            if not ok:
                # The unmonitor did not land — roll back the optimistic parked
                # flag plan_tv set in state, so the NEXT run retries instead of
                # recording a park that never happened (the episode would
                # otherwise stay monitored/unfindable, permanently, unretried).
                rec = state["tv"].get(f"{p['slug']}:{p['episode_id']}")
                if rec is not None:
                    rec["parked"] = False
            parks_all.append(p)
    out["tv_digest"] = digest_all
    out["tv_parks"] = parks_all

    if (digest_all or parks_all) and not dry_run:
        # map (slug, series_id) -> series title, once per slug touched
        titles: dict = {}
        for s in {d["slug"] for d in digest_all} | {p["slug"] for p in parks_all}:
            code, series = client_factory(s).get("/series")
            if code == 200 and isinstance(series, list):
                titles.update({(s, x["id"]): x.get("title", "?") for x in series})

        def _label(d: dict) -> str:
            return (f"{titles.get((d['slug'], d['series_id']), d['slug'])} "
                    f"S{d['season']:02d}E{d['episode']:02d} {d['title']!r}")

        if digest_all:
            lines = [f"- {_label(d)} — {d['days']}d missing" for d in digest_all]
            _notify("TV still missing >5d (auto-parks at day 15 if unfound):\n"
                    + "\n".join(lines), "info")
        ok_parks = [p for p in parks_all if p.get("ok")]
        bad_parks = [p for p in parks_all if not p.get("ok")]
        if ok_parks:
            lines = [f"- {_label(p)} — unfindable after {p['days']}d, unmonitored"
                     for p in ok_parks]
            _notify("TV parked (unfindable — unmonitored, manual intervention "
                    "needed):\n" + "\n".join(lines), "warning")
        if bad_parks:
            lines = [f"- {_label(p)}" for p in bad_parks]
            _notify("TV park FAILED to unmonitor (still monitored):\n"
                    + "\n".join(lines), "error")

    if not dry_run:
        save_state(state_path, state)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _run_had_failures(res: dict) -> bool:
    """True if any planned live write failed — a movie action OR a TV park.
    Drives the --cron exit code so a failed unmonitor turns the
    systemd_oneshot / Kuma monitor red instead of exiting 0 (green)."""
    movie_bad = any(
        r.get("status") not in ("ok",)
        or any(not a.get("ok", True) for a in r.get("actions", []))
        for r in res.get("per_arr", {}).values())
    tv_bad = any(not p.get("ok", True) for p in res.get("tv_parks", []))
    return movie_bad or tv_bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--cron", action="store_true")
    g.add_argument("--emit-json", action="store_true")
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--bootstrap-profiles", action="store_true")
    ap.add_argument("--slug")
    args = ap.parse_args()

    if args.bootstrap_profiles:
        targets = [s for s in MOVIE_ARRS if args.slug is None or s == args.slug]
        result = {}
        for s in targets:
            result[s] = bootstrap_profiles(s, ArrClient(s, "v3"))
        json.dump(result, sys.stdout, default=str)
        sys.stdout.write("\n")
        return 0

    res = run(dry_run=args.dry_run, slug=args.slug)
    if args.emit_json or args.dry_run:
        # JSON mode always exits 0 — the body carries failure detail (same
        # rationale as missing.py: a nonzero exit makes MCP discard the body).
        json.dump(res, sys.stdout, default=str)
        sys.stdout.write("\n")
        return 0
    if _run_had_failures(res):
        bad = [s for s, r in res["per_arr"].items()
               if r.get("status") not in ("ok",)
               or any(not a.get("ok", True) for a in r.get("actions", []))]
        tv_bad = [p["episode_id"] for p in res.get("tv_parks", [])
                  if not p.get("ok", True)]
        _notify(f"quality_fallback: failures — movies={bad} tv_park_eps={tv_bad}",
                "error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
