"""lib/qbit.py — qBittorrent WebUI password rotation + *arr cascade.

Rotating qBit's password breaks every *arr's download-client config because
each *arr stores qBit credentials in its own DownloadClients table. This
module rotates the qBit pw AND walks every *arr in the manifest, updating
the password field in each QBittorrent download-client entry.

Public API:
  rotate_password(manifest, new_password, *, old_password=None,
                  secrets_dir=None) -> RotateResult

Reads creds from `~/secrets/`:
  - qbittorrent.user      (qBit WebUI username)
  - qbittorrent.password  (current pw — used as old_password if not passed)
  - qbittorrent.port      (qBit WebUI port on host loopback)
  - <arr>.key             (per-*arr API key)
  - <arr>.urlbase         (per-*arr URL prefix)
  - <arr>.port            (per-*arr loopback port)

Read-only: never mutates anything if the qBit login fails or any cascade
test returns non-2xx. After all rotations succeed, writes the new password
back to `secrets/qbittorrent.password`.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from lib.manifest import Manifest


# *arrs that store qBit creds in their DownloadClients tables. Readarr
# (api/v1) is included; Bazarr/Prowlarr do not have download clients.
_ARR_KINDS = {
    "sonarr": "v3",
    "sonarr2": "v3",
    "radarr": "v3",
    "radarr2": "v3",
    "readarr": "v1",
}


@dataclass
class RotateResult:
    qbit_login_old_ok: bool = False
    qbit_set_pw_ok: bool = False
    qbit_login_new_ok: bool = False
    arrs_rewritten: dict[str, int] = field(default_factory=dict)
    arrs_failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.qbit_login_old_ok
            and self.qbit_set_pw_ok
            and self.qbit_login_new_ok
            and not self.arrs_failed
        )


def _secrets_dir(override: Optional[Path]) -> Path:
    if override:
        return override
    env = os.environ.get("MANITOBA_SECRETS")
    if env:
        return Path(env).expanduser()
    return Path.home() / "secrets"


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""


# --- qBittorrent WebUI client ---------------------------------------------

def _qbit_login(host: str, user: str, password: str) -> tuple[str, str]:
    """POST /api/v2/auth/login. Returns (body, SID-cookie)."""
    data = urllib.parse.urlencode({"username": user, "password": password}).encode()
    req = urllib.request.Request(
        f"{host}/api/v2/auth/login",
        data=data,
        headers={"Referer": host},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}", ""
    body = resp.read().decode().strip()
    sid = ""
    cookie = resp.headers.get("Set-Cookie", "") or ""
    for part in cookie.split(";"):
        if part.strip().startswith("SID="):
            sid = part.strip().split("=", 1)[1]
            break
    return body, sid


def _qbit_set_password(host: str, sid: str, new_password: str) -> tuple[int, str]:
    body = urllib.parse.urlencode({
        "json": json.dumps({"web_ui_password": new_password})
    }).encode()
    req = urllib.request.Request(
        f"{host}/api/v2/app/setPreferences",
        data=body,
        headers={"Cookie": f"SID={sid}", "Referer": host},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="ignore")[:200]


# --- *arr download-client cascade -----------------------------------------

def _arr_url(secrets: Path, slug: str, version: str, path: str) -> str:
    port = _read(secrets / f"{slug}.port")
    base = _read(secrets / f"{slug}.urlbase") or slug
    return f"http://127.0.0.1:{port}/{base}/api/{version}{path}"


def _arr_req(secrets: Path, slug: str, version: str, path: str, *,
             method: str = "GET", body: dict | None = None) -> tuple[int, str]:
    api_key = _read(secrets / f"{slug}.key")
    url = _arr_url(secrets, slug, version, path)
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "X-Api-Key": api_key,
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode(errors="ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="ignore")[:300]


def _cascade_arr(secrets: Path, slug: str, version: str, new_password: str,
                 *, dry_run: bool) -> tuple[bool, int]:
    """Walk slug's downloadclient list; rewrite the password field in any
    QBittorrent entry. Returns (ok, rewrote_count). On any test/PUT failure
    returns (False, partial_count) — caller should treat as fatal."""
    code, body = _arr_req(secrets, slug, version, "/downloadclient")
    if code != 200:
        return False, 0
    clients = json.loads(body or "[]")
    rewrote = 0
    for c in clients:
        if c.get("implementation") != "QBittorrent":
            continue
        pw_field = next(
            (f for f in (c.get("fields") or []) if f.get("name") == "password"),
            None,
        )
        if pw_field is None:
            continue
        if pw_field.get("value") == new_password:
            continue  # already rotated; idempotent
        pw_field["value"] = new_password

        if dry_run:
            rewrote += 1
            continue

        # Test first — never persist a config we can't validate.
        tcode, _ = _arr_req(secrets, slug, version, "/downloadclient/test",
                             method="POST", body=c)
        if tcode not in (200, 201):
            return False, rewrote
        # Persist.
        pcode, _ = _arr_req(secrets, slug, version, f"/downloadclient/{c['id']}",
                             method="PUT", body=c)
        if pcode not in (200, 202):
            return False, rewrote
        rewrote += 1
    return True, rewrote


# --- public entry ---------------------------------------------------------

def rotate_password(
    manifest: Manifest,
    new_password: str,
    *,
    old_password: Optional[str] = None,
    secrets_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> RotateResult:
    """Rotate qBittorrent WebUI password, cascade to every *arr in the manifest.

    If `old_password` is None, reads it from `secrets/qbittorrent.password`.
    On success, writes `new_password` back to that file.
    """
    secrets = _secrets_dir(secrets_dir)
    res = RotateResult()

    qbit_user = _read(secrets / "qbittorrent.user") or "quadstronaut"
    qbit_port = _read(secrets / "qbittorrent.port")
    if not qbit_port:
        return res
    host = f"http://127.0.0.1:{qbit_port}"

    if old_password is None:
        old_password = _read(secrets / "qbittorrent.password")
    if not old_password:
        return res

    # 1) Login with old pw.
    if dry_run:
        res.qbit_login_old_ok = True  # asserted; not actually attempted
    else:
        body, sid = _qbit_login(host, qbit_user, old_password)
        if body != "Ok." or not sid:
            return res
        res.qbit_login_old_ok = True

        # 2) Set new pw.
        code, _ = _qbit_set_password(host, sid, new_password)
        if code != 200:
            return res
        res.qbit_set_pw_ok = True

        # 3) Verify login with new pw.
        body2, _sid2 = _qbit_login(host, qbit_user, new_password)
        if body2 != "Ok.":
            return res
        res.qbit_login_new_ok = True

    # 4) Cascade to every *arr in the manifest.
    for slug, version in _ARR_KINDS.items():
        try:
            manifest.app(slug)
        except KeyError:
            continue  # not in manifest = not installed
        ok, n = _cascade_arr(secrets, slug, version, new_password,
                              dry_run=dry_run)
        res.arrs_rewritten[slug] = n
        if not ok:
            res.arrs_failed.append(slug)

    # 5) Persist new password — only if everything succeeded.
    if not dry_run and res.ok:
        pw_file = secrets / "qbittorrent.password"
        pw_file.write_text(new_password)
        try:
            os.chmod(pw_file, 0o600)
        except OSError:
            pass

    return res
