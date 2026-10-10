#!/usr/bin/env python3
"""native_sanitize.py -- make a proof copy of an app's data dir INERT (I-11).

A proof copy (VACUUM INTO / cp of ~/.apps/<slug> into ~/.apps/.prove/<slug>)
must never grab, sync, notify or download once booted. This module drives
every enabled download client, indexer, apps-sync link, import list,
notification and self-updater in the copy to 0, then RE-READS the files from
disk and counts. Any non-zero count raises SanitizeError, and the caller must
not boot the copy.

Rules:
  * Works on COPIES only. A path that resolves (symlinks followed) into a live
    ~/.apps/<slug> is refused with LivePathRefused before any file is opened.
    ~/.apps/.prove/** is the one allowed place under ~/.apps.
  * Fail closed. A missing config/db, or an expected table that exists but
    lacks the column we must flip, is an error (we cannot prove inert). A
    table that does not exist at all is fine: nothing in it can be enabled.
  * Idempotent. Rows are kept (flags flipped) except notifications, which
    are deleted, so a count-parity check against the source still works.

Families: arr (sonarr/sonarr2/radarr/radarr2), prowlarr, bazarr (bazarr/
bazarr2), seerr, tautulli, sab (sab/sabnzbd). Stdlib + pyyaml only.

CLI:  native_sanitize.py <slug> <proof-dir>     exit 0 inert, 2 refused/failed
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Callable

import yaml


class SanitizeError(Exception):
    """Could not prove the copy inert. Do not boot it."""


class LivePathRefused(SanitizeError):
    """The path is (or resolves into) a live ~/.apps/<slug> dir."""


FAMILY = {
    "sonarr": "arr", "sonarr2": "arr", "radarr": "arr", "radarr2": "arr",
    "prowlarr": "prowlarr",
    "bazarr": "bazarr", "bazarr2": "bazarr",
    "seerr": "seerr",
    "tautulli": "tautulli",
    "sab": "sab", "sabnzbd": "sab",
}


# --------------------------------------------------------------- live guard
def _refuse_live(path: Path) -> Path:
    """Return the resolved path, or raise if it is inside live ~/.apps."""
    resolved = path.resolve()
    apps = (Path.home() / ".apps").resolve()
    try:
        rel = resolved.relative_to(apps)
    except ValueError:
        return resolved  # not under ~/.apps at all
    if rel.parts and rel.parts[0] == ".prove":
        return resolved
    raise LivePathRefused(f"{resolved} is under live {apps}; sanitize copies only")


# ------------------------------------------------------------- sqlite utils
def _open(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise SanitizeError(f"missing database {path}")
    return sqlite3.connect(path)


def _cols(con: sqlite3.Connection, table: str) -> list[str] | None:
    """Column names, or None when the table does not exist."""
    rows = con.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [r[1] for r in rows] if rows else None


def _flags_off(con, table: str, required: list[str], optional: list[str] = ()) -> int:
    """Set every listed flag column to 0, return rows still enabled.

    required columns must exist if the table does; of `optional`, at least
    the ones present are flipped. Table absent => 0.
    """
    cols = _cols(con, table)
    if cols is None:
        return 0
    missing = [c for c in required if c not in cols]
    if missing:
        raise SanitizeError(f"{table} lacks column(s) {missing}; cannot prove inert")
    use = list(required) + [c for c in optional if c in cols]
    if not use:
        raise SanitizeError(f"{table} has none of the expected flag columns")
    con.execute(f'UPDATE "{table}" SET ' + ", ".join(f'"{c}"=0' for c in use))
    where = " OR ".join(f'"{c}"!=0' for c in use)
    return con.execute(f'SELECT count(*) FROM "{table}" WHERE {where}').fetchone()[0]


def _delete_all(con, table: str) -> int:
    if _cols(con, table) is None:
        return 0
    con.execute(f'DELETE FROM "{table}"')
    return con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]


def _in_txn(db: Path, fn: Callable[[sqlite3.Connection], dict]) -> dict:
    con = _open(db)
    try:
        try:
            out = fn(con)
        except sqlite3.DatabaseError as e:
            con.rollback()
            raise SanitizeError(f"{db.name}: {e}") from e
        con.commit()
        return out
    finally:
        con.close()


# ----------------------------------------------------- arr / prowlarr (xml)
def _xml_update_off(config_xml: Path) -> int:
    """Force <UpdateAutomatically>False. Returns 1 if still not False."""
    if not config_xml.is_file():
        raise SanitizeError(f"missing {config_xml}")
    text = config_xml.read_text(encoding="utf-8")
    tag = "<UpdateAutomatically>False</UpdateAutomatically>"
    if re.search(r"<UpdateAutomatically>.*?</UpdateAutomatically>", text, re.S):
        text = re.sub(r"<UpdateAutomatically>.*?</UpdateAutomatically>", tag, text, flags=re.S)
    elif "</Config>" in text:
        text = text.replace("</Config>", f"  {tag}\n</Config>", 1)
    else:
        raise SanitizeError(f"{config_xml} has no </Config>")
    config_xml.write_text(text, encoding="utf-8")
    again = config_xml.read_text(encoding="utf-8")
    return 0 if tag in again else 1


def _arr(slug: str, d: Path) -> dict:
    base = "radarr" if slug.startswith("radarr") else "sonarr"

    def go(con):
        return {
            "download_clients": _flags_off(con, "DownloadClients", ["Enable"]),
            "indexers": _flags_off(con, "Indexers",
                                   ["EnableRss", "EnableAutomaticSearch", "EnableInteractiveSearch"]),
            # Sonarr: EnableAutomaticAdd. Radarr: Enabled + EnableAuto.
            "import_lists": _flags_off(con, "ImportLists", [],
                                       ["Enabled", "EnableAuto", "EnableAutomaticAdd"]),
            "notifications": _delete_all(con, "Notifications"),
        }

    out = _in_txn(d / f"{base}.db", go)
    out["auto_update"] = _xml_update_off(d / "config.xml")
    return out


def _prowlarr(slug: str, d: Path) -> dict:
    def go(con):
        # SyncLevel: 0 disabled, 1 addOnly, 2 fullSync. Indexers stay enabled:
        # the single manual proof search needs them (spec section 6).
        apps = 0
        cols = _cols(con, "Applications")
        if cols is not None:
            if "SyncLevel" not in cols:
                raise SanitizeError("Applications lacks SyncLevel; cannot prove inert")
            con.execute('UPDATE "Applications" SET "SyncLevel"=0')
            apps = con.execute('SELECT count(*) FROM "Applications" WHERE "SyncLevel"!=0').fetchone()[0]
        return {
            "apps_sync": apps,
            "download_clients": _flags_off(con, "DownloadClients", ["Enable"]),
            "notifications": _delete_all(con, "Notifications"),
        }

    out = _in_txn(d / "prowlarr.db", go)
    out["auto_update"] = _xml_update_off(d / "config.xml")
    return out


# ------------------------------------------------------------------- bazarr
def _bazarr(slug: str, d: Path) -> dict:
    cfg_path = d / "config" / "config.yaml"
    if not cfg_path.is_file():
        raise SanitizeError(f"missing {cfg_path}")
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    gen = cfg.setdefault("general", {})
    gen["enabled_providers"] = []
    gen["use_sonarr"] = False
    gen["use_radarr"] = False
    gen["auto_update"] = False
    if isinstance(cfg.get("analytics"), dict):
        cfg["analytics"]["enabled"] = False
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    def go(con):
        return {"notifications": _flags_off(con, "table_settings_notifier", ["enabled"])}

    out = _in_txn(d / "db" / "bazarr.db", go)
    back = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["general"]
    out["providers"] = len(back.get("enabled_providers") or [])
    out["arr_sync"] = int(bool(back.get("use_sonarr"))) + int(bool(back.get("use_radarr")))
    out["auto_update"] = int(bool(back.get("auto_update")))
    return out


# -------------------------------------------------------------------- seerr
def _seerr(slug: str, d: Path) -> dict:
    path = d / "settings.json"
    if not path.is_file():
        raise SanitizeError(f"missing {path}")
    s = json.loads(path.read_text(encoding="utf-8"))
    s["radarr"] = []
    s["sonarr"] = []
    for agent in ((s.get("notifications") or {}).get("agents") or {}).values():
        agent["enabled"] = False
    for lib in (s.get("plex") or {}).get("libraries") or []:
        lib["enabled"] = False
    path.write_text(json.dumps(s, indent=1), encoding="utf-8")
    b = json.loads(path.read_text(encoding="utf-8"))
    out = {
        "arr_servers": len(b.get("radarr") or []) + len(b.get("sonarr") or []),
        "notifications": sum(
            1 for a in ((b.get("notifications") or {}).get("agents") or {}).values() if a.get("enabled")),
        "plex_sync": sum(1 for lib in (b.get("plex") or {}).get("libraries") or [] if lib.get("enabled")),
    }
    # QFLX-36: the per-user Plex watchlist sync AUTO-REQUESTS (plex-watchlist-sync
    # job) from a booted copy. The db is optional: a copy without one boots on a
    # fresh, empty db, which has nothing to sync.
    db = d / "db" / "db.sqlite3"
    if db.is_file():
        out.update(_in_txn(db, lambda con: {"watchlist_sync": _flags_off(
            con, "user_settings", [], ["watchlistSyncMovies", "watchlistSyncTv"])}))
    else:
        out["watchlist_sync"] = 0
    return out


# ----------------------------------------------------------------- tautulli
def _ini_set_general(text: str, key: str, value: str) -> str:
    """Set `key = value` in [General]; insert if absent (absent == default ON)."""
    pat = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*).*$", re.M)
    if pat.search(text):
        return pat.sub(lambda m: f"{m.group(1)}{value}", text)
    if re.search(r"^\[General\]\s*$", text, re.M):
        return re.sub(r"^(\[General\]\s*)$", lambda m: f"{m.group(1)}\n{key} = {value}", text,
                      count=1, flags=re.M)
    return f"[General]\n{key} = {value}\n" + text


def _ini_value(text: str, key: str) -> str | None:
    m = re.search(rf"^\s*{re.escape(key)}\s*=\s*(\S*)", text, re.M)
    return m.group(1) if m else None


def _tautulli(slug: str, d: Path) -> dict:
    def go(con):
        return {
            "notifiers": _delete_all(con, "notifiers"),
            "newsletters": _flags_off(con, "newsletters", ["active"]),
        }

    out = _in_txn(d / "tautulli.db", go)
    ini = d / "config.ini"
    if not ini.is_file():
        raise SanitizeError(f"missing {ini}")
    text = ini.read_text(encoding="utf-8")
    for k in ("check_github", "check_github_on_startup"):
        text = _ini_set_general(text, k, "0")
    ini.write_text(text, encoding="utf-8")
    back = ini.read_text(encoding="utf-8")
    out["auto_update"] = sum(1 for k in ("check_github", "check_github_on_startup")
                             if _ini_value(back, k) != "0")
    return out


# ---------------------------------------------------------------------- sab
_SAB_SECTIONS = ("servers", "rss")


def _sab_rewrite(lines: list[str]) -> list[str]:
    """Set enable=0 in every [[sub]] under [servers]/[rss]; check_new_rel=0."""
    out: list[str] = []
    top = ""
    in_sub = False
    seen_enable = False
    seen_check = False

    def close_sub():
        if in_sub and top in _SAB_SECTIONS and not seen_enable:
            out.append("enable = 0")

    for line in lines:
        s = line.strip()
        if s.startswith("[[") and s.endswith("]]"):
            close_sub()
            in_sub, seen_enable = True, False
            out.append(line)
            continue
        if s.startswith("[") and s.endswith("]"):
            close_sub()
            in_sub, seen_enable = False, False
            top = s.strip("[]")
            out.append(line)
            continue
        if in_sub and top in _SAB_SECTIONS and re.match(r"enable\s*=", s):
            out.append("enable = 0")
            seen_enable = True
            continue
        if top == "misc" and not in_sub and re.match(r"check_new_rel\s*=", s):
            out.append("check_new_rel = 0")
            seen_check = True
            continue
        out.append(line)
    close_sub()
    if not seen_check:
        for i, line in enumerate(out):
            if line.strip() == "[misc]":
                out.insert(i + 1, "check_new_rel = 0")
                break
        else:
            raise SanitizeError("sabnzbd.ini has no [misc] section")
    return out


def _sab_count(lines: list[str]) -> dict:
    top, in_sub, servers, feeds, upd = "", False, 0, 0, 0
    for line in lines:
        s = line.strip()
        if s.startswith("[[") and s.endswith("]]"):
            in_sub = True
        elif s.startswith("[") and s.endswith("]"):
            top, in_sub = s.strip("[]"), False
        elif in_sub and re.match(r"enable\s*=\s*(\S+)", s):
            on = re.match(r"enable\s*=\s*(\S+)", s).group(1) != "0"
            servers += on and top == "servers"
            feeds += on and top == "rss"
        elif top == "misc" and not in_sub:
            m = re.match(r"check_new_rel\s*=\s*(\S+)", s)
            if m and m.group(1) != "0":
                upd += 1
    return {"servers": int(servers), "rss_feeds": int(feeds), "auto_update": upd}


def _sab(slug: str, d: Path) -> dict:
    ini = d / "sabnzbd.ini"
    if not ini.is_file():
        raise SanitizeError(f"missing {ini}")
    lines = ini.read_text(encoding="utf-8").splitlines()
    ini.write_text("\n".join(_sab_rewrite(lines)) + "\n", encoding="utf-8")
    return _sab_count(ini.read_text(encoding="utf-8").splitlines())


_DISPATCH = {"arr": _arr, "prowlarr": _prowlarr, "bazarr": _bazarr, "seerr": _seerr,
             "tautulli": _tautulli, "sab": _sab}


# ---------------------------------------------------------------- public API
def sanitize(slug: str, data_dir: str | Path) -> dict:
    """Sanitize a proof copy in place; return counts (all 0 on success).

    Raises LivePathRefused, or SanitizeError when any count stays non-zero.
    """
    fam = FAMILY.get(slug)
    if fam is None:
        raise SanitizeError(f"no sanitizer for slug {slug!r}")
    d = _refuse_live(Path(data_dir))
    if not d.is_dir():
        raise SanitizeError(f"{d} is not a directory")
    counts = _DISPATCH[fam](slug, d)
    bad = {k: v for k, v in counts.items() if v}
    if bad:
        raise SanitizeError(f"{slug} not inert after sanitize: {bad}")
    return counts


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: native_sanitize.py <slug> <proof-dir>", file=sys.stderr)
        return 2
    try:
        counts = sanitize(argv[0], argv[1])
    except SanitizeError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    print(json.dumps({"slug": argv[0], "inert": True, "counts": counts}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
