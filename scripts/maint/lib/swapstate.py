#!/usr/bin/env python3
"""lib/swapstate.py - per-app swap state + recorded listen set (QFLX-20, spec 5.8).

WHY THIS EXISTS: the UCC divorce swaps one container app at a time for a native
systemd unit. To prove afterwards that the native app listens exactly where the
container did (and that nothing else came back), the swap procedure CAPTURES the
app's listen set before the swap and the audit-live leg diffs against it later.
The same directory carries the swap bookkeeping the soak gate and rollback need.

Layout (default ~/.opt/maint/swap, override $QFLIX_SWAP_DIR; resolved LAZILY so
tests use monkeypatch.setenv):
  <slug>/listen-set.before   one "addr:port" per line, sorted
  <slug>/state.json          ucc_version, swap_date, soak_until,
                             rollback_window (open|closed), exceptions[],
                             port, captured_at
  <slug>/.lock               flock target

Every read-modify-write holds an exclusive flock on <slug>/.lock and writes with
mkstemp + os.replace. Lock INFRASTRUCTURE missing (no fcntl, cannot open the
lock file) fails open; a lock CONTENDED past LOCK_TIMEOUT_S raises (failing open
there would allow a lost update).

Deliberately stdlib + PyYAML only, no `lib.*` imports: the shell wrapper
scripts/ops/qflix-listen-set.sh runs this file directly (lib/ is a merged
namespace package; never add an __init__.py).

CLI:
  swapstate.py capture SLUG [--port N | --manifest PATH] [--ss-file F|-] [--ucc-version V]
  swapstate.py set SLUG key=value ...   (swap_date, soak_until, rollback_window, ucc_version)
  swapstate.py add-exception SLUG ADDR:PORT... --reason TEXT
                                 record operator-approved listen-set exceptions
                                 (spec 5.4 / D-4; e.g. a dropped public-IP listener)
  swapstate.py show SLUG
  swapstate.py diff SLUG [--ss-file F|-]   exit 0 same, 1 differs, 2 error
  swapstate.py soak-check SLUG   exit 1 refused (inside soak), 0 ok
  swapstate.py close-window SLUG first native upgrade closes rollback-to-UCC
  swapstate.py ucc-slugs [--manifest PATH]  active ucc slugs, one per line
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # non-POSIX workstation: lock degrades to no-op (fail open)
    fcntl = None  # type: ignore[assignment]

LOCK_TIMEOUT_S = 10.0
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ROLLBACK_WINDOWS = ("open", "closed")
SETTABLE = ("swap_date", "soak_until", "rollback_window", "ucc_version")


class SwapStateError(RuntimeError):
    """Bad input or a contended lock."""


def swap_dir() -> Path:
    env = os.environ.get("QFLIX_SWAP_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".opt" / "maint" / "swap"


def _slug_dir(slug: str) -> Path:
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise SwapStateError(f"unsafe slug: {slug!r}")
    return swap_dir() / slug


# ---------------------------------------------------------------------------
# ss parsing
# ---------------------------------------------------------------------------

def parse_listen(ss_text: str, port: int) -> list[str]:
    """Sorted, de-duplicated local "addr:port" of every LISTEN row on *port*.

    Accepts `ss -tln[H]` output with or without the State column."""
    out: set[str] = set()
    for line in ss_text.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        if cols[0] == "LISTEN":
            local = cols[3]
        elif cols[0].isdigit() and cols[1].isdigit():
            local = cols[2]          # rows without the State column
        else:
            continue
        _addr, sep, p = local.rpartition(":")
        if sep and p == str(port):
            out.add(local)
    return sorted(out)


# ---------------------------------------------------------------------------
# lock + atomic write
# ---------------------------------------------------------------------------

def _open_lock(d: Path):
    try:
        d.mkdir(parents=True, exist_ok=True)
        return open(d / ".lock", "a+")
    except OSError:
        return None


def _acquire(handle) -> None:
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise SwapStateError("swap state lock contended past timeout") from None
            time.sleep(0.05)


class _Locked:
    def __init__(self, d: Path):
        self.d = d
        self.handle = None

    def __enter__(self):
        self.d.mkdir(parents=True, exist_ok=True)
        if fcntl is not None:
            self.handle = _open_lock(self.d)
            if self.handle is not None:
                _acquire(self.handle)
        return self

    def __exit__(self, *exc):
        if self.handle is not None:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self.handle.close()
        return False


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# state API
# ---------------------------------------------------------------------------

def load_state(slug: str) -> dict:
    """The slug's state.json, or {} if absent/corrupt (readers must tolerate)."""
    p = _slug_dir(slug) / "state.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def recorded_listen(slug: str) -> list[str] | None:
    """Recorded listen set, or None when nothing was captured."""
    p = _slug_dir(slug) / "listen-set.before"
    try:
        return sorted(l.strip() for l in p.read_text(encoding="utf-8").splitlines()
                      if l.strip())
    except OSError:
        return None


def _now_iso(now=None) -> str:
    import datetime as _dt
    n = now or _dt.datetime.now(_dt.timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%SZ")


def _merge_state(d: Path, updates: dict) -> dict:
    """Read-modify-write of state.json. CALLER holds the lock."""
    sj = d / "state.json"
    try:
        cur = json.loads(sj.read_text(encoding="utf-8"))
    except FileNotFoundError:
        cur = {}
    except (OSError, ValueError) as exc:
        # Writes are atomic, so this is external corruption. Merging onto {}
        # would erase swap_date/soak_until and make soak_gate read "not swapped".
        raise SwapStateError(f"{sj} unreadable ({exc}); refusing to overwrite") from exc
    if not isinstance(cur, dict):
        raise SwapStateError(f"{sj} is not a JSON object; refusing to overwrite")
    cur.setdefault("rollback_window", "open")
    cur.setdefault("swap_date", None)
    cur.setdefault("soak_until", None)
    cur.setdefault("exceptions", [])
    for k, v in updates.items():
        if v is not None:
            cur[k] = v
    _write_atomic(d / "state.json", json.dumps(cur, indent=2, sort_keys=True) + "\n")
    return cur


def capture(slug: str, ss_text: str, port: int, *, ucc_version: str | None = None,
            now=None) -> list[str]:
    """Record the listen set + ucc version. Re-running re-records the listen set
    but never resets swap_date / soak_until / rollback_window."""
    d = _slug_dir(slug)
    listen = parse_listen(ss_text, port)
    with _Locked(d):
        _write_atomic(d / "listen-set.before", "".join(a + "\n" for a in listen))
        _merge_state(d, {"ucc_version": ucc_version, "captured_at": _now_iso(now),
                         "port": port})
    return listen


def update_state(slug: str, **fields) -> dict:
    """Set swap_date / soak_until / rollback_window / ucc_version under flock."""
    bad = set(fields) - set(SETTABLE)
    if bad:
        raise SwapStateError(f"unsettable field(s): {sorted(bad)}")
    rw = fields.get("rollback_window")
    if rw is not None and rw not in ROLLBACK_WINDOWS:
        raise SwapStateError(f"rollback_window must be one of {ROLLBACK_WINDOWS}")
    d = _slug_dir(slug)
    with _Locked(d):
        return _merge_state(d, fields)


_ADDR_RE = re.compile(r"^(\[[0-9A-Fa-f:.]+\]|[0-9A-Za-z.*-]+):[0-9]{1,5}$")


def add_exceptions(slug: str, addrs: list[str], reason: str) -> dict:
    """Record operator-approved listen-set exceptions (I-7, D-4) under flock.

    Each address is excluded from diff_listen afterwards. The reason is kept
    beside it (exception_reasons) so the audit trail says WHY an address may be
    missing. Idempotent: an address already recorded is not duplicated."""
    if not addrs:
        raise SwapStateError("add-exception needs at least one ADDR:PORT")
    bad = [a for a in addrs if not isinstance(a, str) or not _ADDR_RE.match(a)]
    if bad:
        raise SwapStateError(f"not ADDR:PORT: {bad}")
    if not isinstance(reason, str) or not reason.strip():
        raise SwapStateError("an exception needs a reason (operator approval)")
    d = _slug_dir(slug)
    with _Locked(d):
        cur = _merge_state(d, {})
        exc = [str(x) for x in (cur.get("exceptions") or [])]
        reasons = dict(cur.get("exception_reasons") or {})
        for a in addrs:
            if a not in exc:
                exc.append(a)
            reasons[a] = reason.strip()
        return _merge_state(d, {"exceptions": sorted(exc), "exception_reasons": reasons})


def diff_listen(slug: str, ss_text: str) -> dict:
    """Current listen set vs recorded, minus recorded exceptions.

    Raises SwapStateError when nothing was recorded or no port is known (the
    caller must report "could not check", never "clean")."""
    rec = recorded_listen(slug)
    st = load_state(slug)
    if rec is None or not isinstance(st.get("port"), int):
        raise SwapStateError(f"{slug}: no recorded listen set / port")
    cur = parse_listen(ss_text, st["port"])
    exc = {str(x) for x in (st.get("exceptions") or [])}
    return {"added": sorted((set(cur) - set(rec)) - exc),
            "removed": sorted((set(rec) - set(cur)) - exc)}


def _parse_when(v):
    """ISO date or datetime (Z or offset) -> aware UTC datetime; None if bad."""
    import datetime as _dt
    if not isinstance(v, str) or not v.strip():
        return None
    t = v.strip().replace("Z", "+00:00")
    try:
        d = _dt.datetime.fromisoformat(t)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)


def soak_gate(slug: str, now=None) -> dict:
    """QFLX-21 soak gate (spec 5.7): {"refused": bool, "reason": str}.

    Only a slug that actually swapped (swap_date set) is gated. A swapped slug
    with a missing/unparseable soak_until FAILS CLOSED: a safety gate that
    cannot read its own deadline must not wave an upgrade through."""
    import datetime as _dt
    st = load_state(slug)
    if not st.get("swap_date"):
        return {"refused": False, "reason": "not swapped"}
    until = _parse_when(st.get("soak_until"))
    if until is None:
        return {"refused": True,
                "reason": f"soak_until missing/unparseable ({st.get('soak_until')!r})"}
    n = now or _dt.datetime.now(_dt.timezone.utc)
    if n < until:
        return {"refused": True, "reason": f"in soak until {st['soak_until']}"}
    return {"refused": False, "reason": "soak elapsed"}


def close_rollback_window(slug: str) -> bool:
    """The first native upgrade closes rollback-to-UCC. True if it changed.

    No-op (False, nothing created) for a slug that never swapped."""
    d = _slug_dir(slug)
    if not load_state(slug).get("swap_date"):
        return False
    with _Locked(d):
        if load_state(slug).get("rollback_window") == "closed":
            return False
        _merge_state(d, {"rollback_window": "closed"})
    return True


def swapped_slugs() -> list[str]:
    """Slugs whose state carries a swap_date (a swap actually happened)."""
    base = swap_dir()
    try:
        names = sorted(p.name for p in base.iterdir() if p.is_dir())
    except OSError:
        return []
    return [n for n in names
            if _SLUG_RE.match(n) and load_state(n).get("swap_date")]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_manifest() -> Path:
    env = os.environ.get("MANITOBA_MANIFEST")
    return Path(env).expanduser() if env else Path.home() / ".opt" / "maint" / "apps.yaml"


def _secrets_dir() -> Path:
    env = os.environ.get("MANITOBA_SECRETS_DIR")
    return Path(env).expanduser() if env else Path.home() / "secrets"


def _manifest_apps(path) -> dict:
    import yaml
    with open(path, encoding="utf-8") as fh:
        apps = (yaml.safe_load(fh) or {}).get("apps")
    if not isinstance(apps, dict):
        raise SwapStateError(f"manifest {path} has no apps mapping")
    return apps


def _port_for(slug: str, manifest) -> int | None:
    apps = _manifest_apps(manifest)
    name = slug if slug in apps else next(
        (k for k, v in apps.items() if isinstance(v, dict) and v.get("ucc_slug") == slug),
        None)
    if name is None:
        raise SwapStateError(f"{slug}: not in manifest")
    sec = ((apps[name] or {}).get("health") or {}).get("port_secret")
    if not sec:
        return None
    try:
        return int((_secrets_dir() / sec).read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        raise SwapStateError(f"{slug}: port secret {sec} unreadable: {exc}") from exc


def _read_ss(arg: str | None) -> str:
    if arg in (None, "-"):
        return sys.stdin.read()
    return Path(arg).read_text(encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("slug")
    c.add_argument("--port", type=int)
    c.add_argument("--manifest")
    c.add_argument("--ss-file", default="-")
    c.add_argument("--ucc-version")
    s = sub.add_parser("set")
    s.add_argument("slug")
    s.add_argument("pairs", nargs="+")
    sh = sub.add_parser("show")
    sh.add_argument("slug")
    df = sub.add_parser("diff")
    df.add_argument("slug")
    df.add_argument("--ss-file", default="-")
    ae = sub.add_parser("add-exception")
    ae.add_argument("slug")
    ae.add_argument("addrs", nargs="+")
    ae.add_argument("--reason", required=True)
    sk = sub.add_parser("soak-check")      # exit 1 = refused (in soak), 0 = ok
    sk.add_argument("slug")
    cw = sub.add_parser("close-window")
    cw.add_argument("slug")
    u = sub.add_parser("ucc-slugs")
    u.add_argument("--manifest")
    args = ap.parse_args(list(argv) if argv is not None else None)
    try:
        if args.cmd == "capture":
            port = args.port
            if port is None:
                port = _port_for(args.slug, args.manifest or _default_manifest())
            if port is None:
                print(f"swapstate: SKIP {args.slug}: no port secret in manifest",
                      file=sys.stderr)
                return 0
            listen = capture(args.slug, _read_ss(args.ss_file), port,
                             ucc_version=args.ucc_version)
            print(f"{args.slug}: port={port} listen={','.join(listen) or '(none)'}")
            return 0
        if args.cmd == "set":
            kv = {}
            for pair in args.pairs:
                k, sep, v = pair.partition("=")
                if not sep:
                    raise SwapStateError(f"expected key=value, got {pair!r}")
                kv[k] = v
            print(json.dumps(update_state(args.slug, **kv), indent=2, sort_keys=True))
            return 0
        if args.cmd == "add-exception":
            st = add_exceptions(args.slug, args.addrs, args.reason)
            print(json.dumps(st.get("exceptions"), sort_keys=True))
            return 0
        if args.cmd == "show":
            print(json.dumps({"state": load_state(args.slug),
                              "listen": recorded_listen(args.slug)},
                             indent=2, sort_keys=True))
            return 0
        if args.cmd == "diff":
            d = diff_listen(args.slug, _read_ss(args.ss_file))
            print(json.dumps(d, sort_keys=True))
            return 1 if (d["added"] or d["removed"]) else 0
        if args.cmd == "soak-check":
            g = soak_gate(args.slug)
            print(json.dumps(g, sort_keys=True))
            return 1 if g["refused"] else 0
        if args.cmd == "close-window":
            print("closed" if close_rollback_window(args.slug) else "unchanged")
            return 0
        if args.cmd == "ucc-slugs":
            apps = _manifest_apps(args.manifest or _default_manifest())
            for k in sorted(apps):
                v = apps[k] or {}
                if v.get("class") == "ucc" and v.get("ucc_dormant") in (None, False):
                    print(v.get("ucc_slug") or k)
            return 0
    except (SwapStateError, OSError, ImportError) as exc:
        print(f"swapstate: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
