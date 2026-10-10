"""lib/ports.py - claim a listen port for an app, once, under flock (QFLX-19).

Replaces the `app-ports free` filter that was copy-pasted into 240, 43-listmonk,
50-tdarr and 80-vlogs. Runs WORKSTATION-side (secrets/ lives there); the caller
(scripts/lib/ports.sh) fetches the two box-side inputs over ssh and hands them in:

  candidates : `app-ports free` output (Ultra policy; other hosts supply their own)
  bound      : `ss -tln` output - ports already listening on the box

claim(name) is idempotent: an existing, valid secrets/<name> wins untouched.
Otherwise the first candidate that is neither claimed by any secrets/*.port
(or *_port) file nor bound is written with mkstemp + os.replace, so a reader
never sees a partial file. The whole read-decide-write runs under an exclusive
flock on secrets/.ports.lock so concurrent claimers cannot pick the same port.

Failure policy:
  - lock INFRASTRUCTURE unavailable (no fcntl, cannot open lock file): fail open,
    proceed unlocked (a single operator run is the real-world case).
  - lock CONTENDED past LOCK_TIMEOUT_S: raise. Failing open here would risk the
    exact double-claim the lock exists to stop.
"""
from __future__ import annotations

import argparse
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
_LOCK_NAME = ".ports.lock"
_PORT_RE = re.compile(r"^\s*(\d{1,5})\s*$")


class PortClaimError(RuntimeError):
    """No port could be claimed (exhausted, or lock contended)."""


def parse_candidates(text: str) -> list[int]:
    """Numeric-only lines of `app-ports free`, order preserved, de-duplicated."""
    out: list[int] = []
    for line in text.splitlines():
        m = _PORT_RE.match(line)
        if m and int(m.group(1)) not in out:
            out.append(int(m.group(1)))
    return out


def parse_ss(text: str) -> set[int]:
    """Every local listening port in `ss -tln[H]` output (any bind address)."""
    found: set[int] = set()
    for line in text.splitlines():
        cols = line.split()
        # Local Address:Port is column 4 with the header row, 3 without; the
        # peer column is always `*:*`/`0.0.0.0:*`, so take the first :NNNN col
        # after the numeric Recv-Q/Send-Q pair.
        if len(cols) >= 4 and cols[0].upper() == "LISTEN":
            m = re.search(r":(\d{1,5})$", cols[3])
            if m:
                found.add(int(m.group(1)))
    return found


def _read_port(path: Path) -> int | None:
    try:
        m = _PORT_RE.match(path.read_text())
    except OSError:
        return None
    return int(m.group(1)) if m else None


def _claimed(secrets_dir: Path) -> set[int]:
    got: set[int] = set()
    for pat in ("*.port", "*_port"):
        for f in secrets_dir.glob(pat):
            p = _read_port(f)
            if p is not None:
                got.add(p)
    return got


def _open_lock(secrets_dir: Path):
    try:
        secrets_dir.mkdir(parents=True, exist_ok=True)
        return open(secrets_dir / _LOCK_NAME, "a+")
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
                raise PortClaimError("ports lock contended past timeout") from None
            time.sleep(0.05)


def _write_atomic(path: Path, port: int) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{port}\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def claim(name: str, secrets_dir, candidates, bound) -> int:
    """Return the port for secrets/<name>, claiming one if absent."""
    secrets_dir = Path(secrets_dir)
    target = secrets_dir / name
    handle = _open_lock(secrets_dir) if fcntl is not None else None
    try:
        if handle is not None:
            _acquire(handle)
        existing = _read_port(target)
        if existing is not None:
            return existing
        taken = _claimed(secrets_dir) | set(bound)
        for port in candidates:
            if port not in taken:
                _write_atomic(target, port)
                return port
        raise PortClaimError(f"no free port for {name} (candidates minus claimed minus bound)")
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            handle.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim")
    c.add_argument("name")
    c.add_argument("--secrets-dir", required=True)
    c.add_argument("--app-ports", default="", help="`app-ports free` output")
    c.add_argument("--ss", default="", help="`ss -tln` output")
    a = ap.parse_args(argv)
    try:
        port = claim(a.name, a.secrets_dir, parse_candidates(a.app_ports), parse_ss(a.ss))
    except PortClaimError as exc:
        print(f"ports: {exc}", file=sys.stderr)
        return 1
    print(port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
