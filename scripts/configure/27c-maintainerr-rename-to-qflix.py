#!/usr/bin/env python3
"""One-shot migration: rename Maintainerr 60-day rules to the canonical
'QFlix <short>-60d' scheme.

Pre-2026-05 the Plex libraries were named 'Pirate Movies' / 'Pirate TV
Shows' / 'Anime' / 'Anime Movies' and 27b-maintainerr-rules.py created
rules using `f"{title}-60d"` directly. The 2026-05 Plex rename added a
'QFlix - ' prefix to every library, but the maintainerr rule names
weren't migrated — they kept pointing at the right Plex library by
libraryId but carried stale titles.

Maintainerr exposes no PUT/PATCH on /api/rules/{id} (verified 2026-05-16,
both return 404), so this script DELETEs each rule whose current name
doesn't match what 27b would produce today, then re-invokes 27b's
idempotent create logic. The deletion drops the collection row
(handledMediaAmount counter resets to 0) but preserves nothing
load-bearing — the actual deletion logic targets items by
Plex.addDate, which is unaffected.

Idempotent: re-run after a successful run is a no-op (all names already
canonical → nothing to delete → 27b creates nothing new).
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


def _seedbox_host() -> str:
    env = os.environ.get("PUBLIC_HOST")
    if env:
        return env
    return Path("~/secrets/seedbox.host").expanduser().read_text(encoding="utf-8").strip()


_HOST = _seedbox_host()
_USERPART, _DOMAIN = _HOST.split(".", 1)
BASE = f"https://maintainerr-{_USERPART}.{_DOMAIN}"
HTPW = os.environ["HTPW"]
MTKEY = os.environ["MTKEY"]

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
BASIC = base64.b64encode(f"quadstronaut:{HTPW}".encode()).decode()


def req(path: str, method: str = "GET", body=None):
    headers = {"X-Api-Key": MTKEY, "Authorization": f"Basic {BASIC}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    request = urllib.request.Request(f"{BASE}{path}", data=data, method=method,
                                      headers=headers)
    try:
        with urllib.request.urlopen(request, context=ctx, timeout=30) as resp:
            txt = resp.read().decode()
            return resp.status, (json.loads(txt) if txt else None)
    except urllib.error.HTTPError as exc:
        body_txt = exc.read().decode()
        try:
            return exc.code, json.loads(body_txt)
        except Exception:
            return exc.code, body_txt


def canonical_short_name(plex_title: str) -> str:
    """Mirror of 27b-maintainerr-rules.py — strip the 'QFlix - ' prefix
    that the 2026-05 Plex rename added. Kept in sync by code review; if
    27b's rule changes, this MUST change with it."""
    prefix = "QFlix - "
    return plex_title[len(prefix):] if plex_title.startswith(prefix) else plex_title


def main() -> int:
    code, libs = req("/api/plex/libraries")
    if code != 200 or not isinstance(libs, list):
        print(f"FATAL: GET /api/plex/libraries returned {code}: {libs!r}",
              file=sys.stderr)
        return 2
    title_by_libid: dict[str, str] = {str(lib["key"]): lib["title"] for lib in libs}
    print(f"Plex libraries ({len(title_by_libid)}):")
    for lid, t in title_by_libid.items():
        print(f"  libId={lid}  title={t!r}")

    code, rules = req("/api/rules")
    if code != 200 or not isinstance(rules, list):
        print(f"FATAL: GET /api/rules returned {code}: {rules!r}", file=sys.stderr)
        return 2

    to_delete: list[tuple[int, str, str]] = []
    for rule in rules:
        cur_name = rule.get("name") or ""
        lib_id = str(rule.get("libraryId") or "")
        plex_title = title_by_libid.get(lib_id)
        if plex_title is None:
            print(f"  [skip] rule id={rule.get('id')} name={cur_name!r}: "
                  f"libraryId {lib_id} not in Plex (orphan? leave alone)")
            continue
        expected_name = f"QFlix {canonical_short_name(plex_title)}-60d"
        if cur_name == expected_name:
            print(f"  [ok] rule id={rule.get('id')} name={cur_name!r} "
                  f"already canonical")
            continue
        print(f"  [rename-needed] rule id={rule.get('id')} "
              f"{cur_name!r} → {expected_name!r}")
        to_delete.append((rule["id"], cur_name, expected_name))

    if not to_delete:
        print("\nAll rules already canonical — nothing to do.")
        return 0

    print(f"\nDeleting {len(to_delete)} stale-named rule(s):")
    for rid, old_name, new_name in to_delete:
        code, body = req(f"/api/rules/{rid}", method="DELETE")
        if code in (200, 201, 204):
            print(f"  [del]  id={rid} {old_name!r} (→ will be re-created as {new_name!r})")
        else:
            print(f"  [FAIL] id={rid} delete returned {code}: {body!r}",
                  file=sys.stderr)
            return 3

    # Re-invoke 27b — idempotent, will create the missing canonical names.
    print("\nRe-creating rules via 27b-maintainerr-rules.py (idempotent)…")
    rc = subprocess.call([sys.executable, str(HERE / "27b-maintainerr-rules.py")],
                         env=os.environ.copy())
    if rc != 0:
        print(f"FATAL: 27b-maintainerr-rules.py exited {rc}", file=sys.stderr)
        return rc

    # Verify final state.
    code, final = req("/api/rules")
    if code != 200:
        print(f"WARN: post-create GET /api/rules returned {code}", file=sys.stderr)
        return 0
    print(f"\nFinal rule names ({len(final or [])}):")
    for g in (final or []):
        print(f"  - {g.get('name')!r} (id={g.get('id')}, libId={g.get('libraryId')}, "
              f"active={g.get('isActive')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
