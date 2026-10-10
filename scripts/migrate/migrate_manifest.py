#!/usr/bin/env python3
"""migrate_manifest.py -- every per-app table the migrate scripts use (QFLX-39).

WHY THIS FILE EXISTS
  The stale feature/migration scripts each carried a hand-pasted app list
  (ARR_SPECS, ARR_TABLE, a 35-row APPS heredoc in 40-validate-green.sh). Every
  one of them had drifted from manifest/apps.yaml by the time it was re-cut:
  four retired apps were still in the validation table and qflix-dash's
  installer was missing. Spec section 8 says: "Every per-app list in these
  scripts is generated from manifest/apps.yaml, never pasted." This module is
  that generator. The shell scripts call it; they never name an app.

WHAT IS POLICY HERE (and why it is not a pasted app list)
  The SET of apps always comes from the manifest. What lives here is a small
  map from app or app FAMILY to a data strategy ("how does this app's state
  move to box 2"). The family of an *arr-like app comes from
  scripts/maint/native_sanitize.py FAMILY, the repo's existing single source,
  loaded by file path. An app the rules cannot place raises NoStrategy: a new
  manifest app can never be silently skipped by the migration, it fails the
  run (and tests/unit/test_migrate_manifest.py) until someone decides.

ALSO HERE
  * in-window: the host-policy window answer for the OLD host's profile, so a
    live migrate run refuses inside the Ultra Monday window (spec 5.6). The
    window itself lives ONLY in scripts/maint/lib/hostpolicy_ultra.py; this
    file loads it, never restates it.
  * evaluate-green: turns green's `manitoba-maint status --all --json` plus a
    facts JSON into the 40-validate-green verdict. Pure, so it is unit-tested
    without SSH.

Stdlib + PyYAML only. Lives in scripts/migrate/ (NOT a lib/ dir: never add an
__init__.py anywhere under scripts/maint/lib or scripts/mcp/lib).

Exit codes (house style): 0 ok, 1 finding, 2 could-not-assert / bad input.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DEFAULT_MANIFEST = REPO / "manifest" / "apps.yaml"
DEFAULT_JOBS = REPO / "manifest" / "jobs.yaml"
MAINT = REPO / "scripts" / "maint"


class NoStrategy(Exception):
    """A manifest app has no migration data strategy. Fail closed."""


# ---------------------------------------------------------------------------
# Data strategies. Read the module doc: the app SET is the manifest's.
# ---------------------------------------------------------------------------
STRATEGIES = {
    "sqlite-tree": "VACUUM INTO every sqlite file under ~/.apps/<dir>, rsync the rest of the tree",
    "pg-dump": "pg_dumpall --globals-only + pg_dump -Fc from blue, restore into green's native postgres",
    "qbit-profile": "rsync the qBittorrent profile (config + BT_backup) during the freeze",
    "state-paths": "owned state is in migrate.conf STATE_TREES (copied by the state step)",
    "in-postgres": "all state is in postgres (covered by pg-dump); config rendered by its installer",
    "fresh-identity": "NOT copied: box 2 gets a new identity (Plex variant P1, spec section 7)",
    "rendered": "config rendered from secrets by its installer; no state",
    "stateless": "no state to move",
}

# Family -> strategy. Families come from native_sanitize.FAMILY.
FAMILY_STRATEGY = {
    "arr": "sqlite-tree",
    "prowlarr": "sqlite-tree",
    "bazarr": "sqlite-tree",
    "seerr": "sqlite-tree",
    "tautulli": "sqlite-tree",
    "sab": "sqlite-tree",
}

# Per-app exceptions for apps outside every family. Each line is a decision,
# not an inventory: the manifest decides which of these exist.
APP_STRATEGY = {
    "plex": "fresh-identity",          # D-1 default P1: new identity on box 2
    "qbittorrent": "qbit-profile",
    "flaresolverr": "stateless",
    "unpackerr": "rendered",           # [[general]] TOML rendered from secrets
    "postgres": "pg-dump",
    "listmonk": "in-postgres",
    "qflix-dash": "stateless",
    "tdarr-server": "state-paths",     # DB2 (incl. flows) + configs
    "tdarr-node": "state-paths",       # threadcap shim re-applied by 50-tdarr-install.sh
    "victorialogs": "state-paths",     # ~/.apps/vlogs/data
    "kometa": "state-paths",           # ~/.apps/kometa/config
}

# Classes whose apps own no runtime data of their own unless listed above.
STATELESS_CLASSES = ("cron", "library")


def _load_by_path(key: str, path: Path):
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load %s" % path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


def families() -> Dict[str, str]:
    """slug -> family, from the repo's one source (native_sanitize.FAMILY)."""
    return dict(_load_by_path("_qflix_mig_sanitize", MAINT / "native_sanitize.py").FAMILY)


def load_apps(manifest: Path = DEFAULT_MANIFEST) -> Dict[str, dict]:
    with open(manifest, encoding="utf-8") as fh:
        apps = (yaml.safe_load(fh) or {}).get("apps") or {}
    return {k: v for k, v in apps.items() if isinstance(v, dict)}


def slug_of(name: str, app: dict) -> str:
    return str(app.get("ucc_slug") or name)


def was_ucc(app: dict) -> bool:
    """Ever a UCC app on Ultra: still class ucc, or converted (keeps ucc_slug)."""
    return bool(app.get("ucc_slug"))


def strategy_of(name: str, app: dict, fam: Optional[Dict[str, str]] = None) -> str:
    fam = families() if fam is None else fam
    if name in APP_STRATEGY:
        return APP_STRATEGY[name]
    slug = slug_of(name, app)
    f = fam.get(slug) or fam.get(name)
    if f in FAMILY_STRATEGY:
        return FAMILY_STRATEGY[f]
    if str(app.get("class") or "") in STATELESS_CLASSES:
        return "stateless"
    raise NoStrategy("app %r (class %r) has no migration data strategy; add it to "
                     "APP_STRATEGY in scripts/migrate/migrate_manifest.py" % (name, app.get("class")))


def green_unit(name: str, app: dict) -> str:
    """The unit that runs the app on box 2. Native everywhere there (spec 1):
    a manifest `unit` wins; a UCC app gets the spec 5.1 name qflix-<slug>.service."""
    unit = app.get("unit")
    if unit:
        return str(unit)
    if was_ucc(app):
        return "qflix-%s.service" % slug_of(name, app)
    return ""


def app_rows(manifest: Path = DEFAULT_MANIFEST) -> List[dict]:
    fam = families()
    rows = []
    apps = load_apps(manifest)
    for name in sorted(apps):
        app = apps[name]
        health = app.get("health") or {}
        rows.append({
            "name": name,
            "class": str(app.get("class") or ""),
            "slug": slug_of(name, app),
            "was_ucc": was_ucc(app),
            "unit": green_unit(name, app),
            "strategy": strategy_of(name, app, fam),
            "data_dir": ".apps/%s" % slug_of(name, app),
            "port_secret": str(health.get("port_secret") or ""),
            "kuma_monitor": str(app.get("kuma_monitor") or ""),
        })
    return rows


def native_installers(configure_dir: Path, manifest: Path = DEFAULT_MANIFEST) -> List[dict]:
    """Every ever-UCC app needs a scripts/configure/3NN-native-<slug>-install.sh
    (spec 5.1). Missing ones are reported, never skipped."""
    out = []
    apps = load_apps(manifest)
    for name in sorted(apps):
        app = apps[name]
        if not was_ucc(app):
            continue
        slug = slug_of(name, app)
        hits = sorted(p for p in configure_dir.glob("3[0-9][0-9]-native-%s-install.sh" % slug))
        out.append({"name": name, "slug": slug,
                    "installer": hits[0].name if hits else ""})
    return out


def comms_jobs(keys: List[str], jobs_path: Path = DEFAULT_JOBS) -> List[dict]:
    """Resolve the I-1 member-facing comms jobs (migrate.conf COMMS_JOBS) from
    manifest/jobs.yaml into what the box actually runs: a timer unit name or a
    crontab script path. An unknown key is an error, never a silent no-op."""
    with open(jobs_path, encoding="utf-8") as fh:
        jobs = (yaml.safe_load(fh) or {}).get("jobs") or {}
    out = []
    for key in keys:
        job = jobs.get(key)
        if not isinstance(job, dict):
            raise KeyError("comms job %r not in %s" % (key, jobs_path.name))
        if job.get("timer"):
            out.append({"key": key, "kind": "timer", "target": Path(str(job["timer"])).name})
        elif job.get("unit"):
            out.append({"key": key, "kind": "timer", "target": str(job["unit"])})
        elif job.get("cron"):
            out.append({"key": key, "kind": "cron", "target": str(job["cron"])})
        else:
            raise KeyError("comms job %r has no timer/unit/cron" % key)
    return out


# ---------------------------------------------------------------------------
# Host policy (window + gateway), loaded from scripts/maint/lib by path.
# ---------------------------------------------------------------------------
def _policy(profile: str):
    lib = MAINT / "lib"
    if profile == "ultra":
        return _load_by_path("_qflix_hp_hostpolicy_ultra", lib / "hostpolicy_ultra.py").UltraPolicy()
    if profile == "generic":
        return _load_by_path("_qflix_hp_hostpolicy_generic", lib / "hostpolicy_generic.py").GenericPolicy()
    raise ValueError("unknown host profile %r" % profile)


def parse_now(text: Optional[str]) -> Optional[_dt.datetime]:
    if not text:
        return None
    now = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return now if now.tzinfo else now.replace(tzinfo=_dt.timezone.utc)


def in_window(profile: str, now: Optional[_dt.datetime]) -> bool:
    return _policy(profile).in_window(now)


def docker_gateway(profile: str) -> str:
    return _policy(profile).docker_gateway() or ""


# ---------------------------------------------------------------------------
# 40-validate-green verdict (pure).
# ---------------------------------------------------------------------------
def evaluate_green(apps: Dict[str, dict], status: dict, facts: dict, mode: str = "pre",
                   baseline_timers: Optional[int] = None, held_timers: int = 0) -> List[tuple]:
    """Rows of (verdict, check, detail); verdict in PASS/FAIL/SKIP.

    mode "pre"  = before cutover: green must be MUTED (I-1) and DISARMED (I-5).
    mode "post" = after the front-door flip: green must be LOUD and every app up.
    """
    rows: List[tuple] = []
    seen = {r.get("app"): r for r in status.get("apps") or [] if isinstance(r, dict)}
    for name in sorted(apps):
        r = seen.get(name)
        if r is None:
            rows.append(("FAIL", "app:" + name, "not in green's deployed manifest/status"))
        elif r.get("ok"):
            rows.append(("PASS", "app:" + name, "%s ok" % r.get("probe_kind")))
        else:
            rows.append(("FAIL", "app:" + name, "%s probe failed" % r.get("probe_kind")))
    for c in status.get("canaries") or []:
        nm = "canary:" + str(c.get("name"))
        if not c.get("ok"):
            rows.append(("FAIL", nm, "last run %s" % c.get("reason")))
        elif c.get("stale"):
            rows.append(("FAIL", nm, "stale (last_run %s)" % c.get("last_run")))
        else:
            rows.append(("PASS", nm, str(c.get("reason") or "ok")))

    # I-5: the gate is armed on at most one side; green ships disarmed.
    if facts.get("gate_dropin") is not False:
        rows.append(("FAIL", "gate-disarmed", "execute.conf drop-in present (or unknown) on green"))
    else:
        rows.append(("PASS", "gate-disarmed", "no execute.conf drop-in"))
    armed = facts.get("members_armed")
    if armed is False:
        rows.append(("PASS", "gate-armed-false", "members.yaml armed: false"))
    else:
        rows.append(("FAIL", "gate-armed-false", "members.yaml armed=%r (want false)" % (armed,)))

    viol = facts.get("parity_violations")
    if viol is None:
        rows.append(("FAIL", "runtime-parity", "could not read green state.json"))
    elif viol:
        rows.append(("FAIL", "runtime-parity", "; ".join(map(str, viol))[:300]))
    else:
        rows.append(("PASS", "runtime-parity", "no runtime-parity violations"))

    kuma = facts.get("kuma") or {}
    if not kuma.get("reachable"):
        rows.append(("FAIL", "kuma", "green Kuma unreachable/absent (box-2 Kuma is QFLX-41): %s"
                     % kuma.get("error", "")))
    elif mode == "pre":
        loud = int(kuma.get("monitors_with_human") or 0)
        rows.append(("PASS" if loud == 0 else "FAIL", "kuma-muted",
                     "%d monitor(s) still page a human (I-1)" % loud))
    else:
        quiet = int(kuma.get("monitors_without_human") or 0)
        rows.append(("PASS" if quiet == 0 else "FAIL", "kuma-loud",
                     "%d monitor(s) have no human channel" % quiet))
    hook = facts.get("webhook_parked")
    want_parked = mode == "pre"
    rows.append(("PASS" if hook is want_parked else "FAIL", "discord-webhook",
                 "parked=%r (want %r)" % (hook, want_parked)))

    rows.append(("PASS" if facts.get("ffmpeg_shim") else "FAIL", "tdarr-threadcap-shim",
                 "ffmpeg.real + shim present" if facts.get("ffmpeg_shim") else "shim missing"))
    media = int(facts.get("media_files") or 0)
    rows.append(("PASS" if media > 0 else "FAIL", "media-present", "%d video files sampled" % media))

    timers = facts.get("timer_count")
    if baseline_timers is None:
        rows.append(("SKIP", "timer-count", "no baseline (run 00-preflight.sh)"))
    elif isinstance(timers, int) and timers >= baseline_timers - held_timers:
        rows.append(("PASS", "timer-count", "green=%d blue=%d held=%d" % (timers, baseline_timers, held_timers)))
    else:
        rows.append(("FAIL", "timer-count", "green=%r blue=%d held=%d" % (timers, baseline_timers, held_timers)))
    return rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _tsv(rows: List[dict], cols: List[str]) -> str:
    return "".join("\t".join(str(r[c]).lower() if isinstance(r[c], bool) else str(r[c])
                             for c in cols) + "\n" for r in rows)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="migrate_manifest.py")
    ap.add_argument("--manifest", default=os.environ.get("QFLIX_MIGRATE_MANIFEST") or str(DEFAULT_MANIFEST))
    ap.add_argument("--jobs", default=os.environ.get("QFLIX_MIGRATE_JOBS") or str(DEFAULT_JOBS))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("apps", help="name class slug was_ucc unit strategy data_dir port_secret")
    p.add_argument("--strategy", default=None, help="only apps with this strategy")
    sub.add_parser("ucc-slugs")
    p = sub.add_parser("native-installers")
    p.add_argument("--configure-dir", default=str(REPO / "scripts" / "configure"))
    p = sub.add_parser("comms")
    p.add_argument("keys", nargs="+")
    p = sub.add_parser("in-window")
    p.add_argument("--profile", required=True)
    p.add_argument("--now", default=os.environ.get("QFLIX_NOW"))
    p = sub.add_parser("docker-gateway")
    p.add_argument("--profile", required=True)
    p = sub.add_parser("evaluate-green")
    p.add_argument("--status", required=True)
    p.add_argument("--facts", required=True)
    p.add_argument("--mode", choices=("pre", "post"), default="pre")
    p.add_argument("--baseline", default=None, help="migration-state.json from 00-preflight")
    p.add_argument("--held-timers", type=int, default=0)
    a = ap.parse_args(argv)
    manifest = Path(a.manifest)

    try:
        if a.cmd == "apps":
            rows = app_rows(manifest)
            if a.strategy:
                rows = [r for r in rows if r["strategy"] == a.strategy]
            sys.stdout.write(_tsv(rows, ["name", "class", "slug", "was_ucc", "unit", "strategy",
                                         "data_dir", "port_secret"]))
            return 0
        if a.cmd == "ucc-slugs":
            sys.stdout.write("".join(r["slug"] + "\n" for r in app_rows(manifest) if r["was_ucc"]))
            return 0
        if a.cmd == "native-installers":
            rows = native_installers(Path(a.configure_dir), manifest)
            sys.stdout.write("".join("%s\t%s\t%s\n" % (r["name"], r["slug"], r["installer"] or "MISSING")
                                     for r in rows))
            return 0
        if a.cmd == "comms":
            sys.stdout.write(_tsv(comms_jobs(a.keys, Path(a.jobs)), ["key", "kind", "target"]))
            return 0
        if a.cmd == "in-window":
            return 0 if in_window(a.profile, parse_now(a.now)) else 1
        if a.cmd == "docker-gateway":
            print(docker_gateway(a.profile))
            return 0
        if a.cmd == "evaluate-green":
            status = json.loads(Path(a.status).read_text(encoding="utf-8"))
            facts = json.loads(Path(a.facts).read_text(encoding="utf-8"))
            baseline = None
            if a.baseline and Path(a.baseline).is_file():
                n = (json.loads(Path(a.baseline).read_text(encoding="utf-8"))
                     .get("systemd_timers") or {}).get("count")
                baseline = n if isinstance(n, int) else None
            rows = evaluate_green(load_apps(manifest), status, facts, a.mode, baseline, a.held_timers)
            for verdict, check, detail in rows:
                print("  [%s] %-34s %s" % (verdict, check, detail))
            fails = [c for v, c, _ in rows if v == "FAIL"]
            print("== summary: %d pass, %d fail, %d skip ==" % (
                sum(1 for v, _, _ in rows if v == "PASS"), len(fails),
                sum(1 for v, _, _ in rows if v == "SKIP")))
            if fails:
                sys.stderr.write("STAGE=validate-fail msg=%d-checks-failed failed=%s\n"
                                 % (len(fails), ",".join(fails)[:400]))
                return 1
            return 0
    except (NoStrategy, KeyError, ValueError, OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        sys.stderr.write("STAGE=manifest-table msg=%s\n" % str(exc).replace(" ", "-")[:300])
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
