#!/usr/bin/env python3
"""bazarr2-sync — keep bazarr2 pinned to the same upstream version as bazarr-1.

Reads bazarr-1's running version via its API, compares to bazarr2's running
version. If they differ, fetches the matching tag from morpheus65535/bazarr,
checks it out in ~/.apps/bazarr2/bin/, re-applies the threads=100 to 4 patch,
re-installs requirements, updates the BAZARR_VERSION env in bazarr2.service,
and restarts. On success, pushes UP to the configured Kuma monitor; on
failure, pushes DOWN with a reason.

Exit codes:
  0 = versions already match, or sync succeeded
  1 = sync attempted but failed (bazarr2 may be in a bad state; see logs)
  2 = could not determine versions (bazarr-1 unreachable, etc.) — soft skip
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path(os.path.expanduser("~"))

BAZARR1_URL = os.environ.get("BAZARR1_URL", "http://127.0.0.1:17031/bazarr")
BAZARR2_URL = os.environ.get("BAZARR2_URL", "http://127.0.0.1:17032/bazarr2")
BAZARR2_BIN = HOME / ".apps" / "bazarr2" / "bin"
BAZARR2_UNIT_PATH = HOME / ".config" / "systemd" / "user" / "bazarr2.service"
KUMA_BASE = os.environ.get("KUMA_BASE", "http://127.0.0.1:42005")


def _read_kuma_token():
    # The pusher writes per-app push tokens to ~/secrets/kuma-push-tokens.json
    # (bootstrapped by scripts/maint/bootstrap-kuma-monitors.py). The
    # bazarr2-sync token lives under the "bazarr2-sync" key. Env var wins
    # so operators can override without touching the file.
    env = os.environ.get("BAZARR2_SYNC_KUMA_TOKEN")
    if env:
        return env
    path = HOME / "secrets" / "kuma-push-tokens.json"
    try:
        return json.load(open(path)).get("bazarr2-sync", "")
    except Exception:
        return ""


KUMA_TOKEN = _read_kuma_token()

# Patches that need to be re-applied after every git checkout.
# (regex, replacement) — sed-like.
PATCHES = [
    # waitress thread=100 -> 4. The host kernel boundary refuses bursts of
    # 100 thread creates from a fresh-imported Python child; 4 (waitress
    # default) is plenty for subtitle-fetch traffic.
    (r"threads=100\)", "threads=4)"),
]


def log(msg):
    print("[bazarr2-sync] " + msg, flush=True)


def _read_apikey(config_path):
    if not config_path.exists():
        return ""
    in_auth = False
    for line in config_path.read_text().splitlines():
        if line.startswith("auth:"):
            in_auth = True
            continue
        if in_auth:
            if line and not line.startswith(" "):
                in_auth = False
                continue
            m = re.match(r"\s+apikey:\s*(\S+)", line)
            if m:
                return m.group(1)
    return ""


def _api_version(base, key):
    url = base.rstrip("/") + "/api/system/status"
    req = urllib.request.Request(url, headers={"X-API-KEY": key})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        ver = (data.get("data") or {}).get("bazarr_version") or ""
        ver = ver.lstrip("v")
        return ver or None
    except Exception as exc:
        log("WARN: could not read version from " + base + ": " + str(exc))
        return None


def _semver_eq(a, b):
    def core(s):
        s = s.lstrip("v").split("-")[0]
        try:
            return tuple(int(x) for x in s.split("."))
        except ValueError:
            return ()
    return core(a) == core(b) and core(a) != ()


def _push_kuma(status, msg):
    if not KUMA_TOKEN:
        return
    qs = urllib.parse.urlencode({"status": status, "msg": msg[:200]})
    url = KUMA_BASE + "/api/push/" + KUMA_TOKEN + "?" + qs
    try:
        urllib.request.urlopen(url, timeout=5).read()
    except Exception as exc:
        log("WARN: Kuma push failed: " + str(exc))


def _git_tag_exists(tag):
    cp = subprocess.run(
        ["git", "ls-remote", "--tags", "https://github.com/morpheus65535/bazarr.git",
         "refs/tags/" + tag],
        capture_output=True, text=True, timeout=30,
    )
    return tag in cp.stdout


def _ensure_frontend(version, force=False):
    """Install the PREBUILT web UI for `version` into bin/frontend/build/.

    WHY THIS EXISTS (2026-08-18 audit finding): this script pins bazarr2 by
    checking out a GIT TAG, but the web UI is a Vite app that upstream only
    ships PREBUILT in the release zip — a tag checkout leaves bin/frontend/
    as unbuilt source with no build/ directory, app/ui.py 404s its Jinja
    loader, and every UI request 500s with TemplateNotFound. That was the
    state since the Jul 6 install: API perfectly healthy, UI dead for six
    weeks, and nothing noticed because all automation is API-driven.

    Building the frontend on the box is a non-starter (no node toolchain on
    the slot, and a build would need re-running on every sync). Instead pull
    the release asset for the SAME tag the checkout pinned and extract only
    frontend/build/. Idempotent: skips when build/index.html already exists,
    unless force=True (version just changed, so the old build is stale).

    Returns True on success, False on failure — callers treat a missing UI
    as DEGRADED, not fatal: the API (the part the stack depends on) is
    untouched either way, so a GitHub hiccup must not abort a version sync.
    """
    import shutil
    import tempfile
    import zipfile

    build_dir = BAZARR2_BIN / "frontend" / "build"
    if not force and (build_dir / "index.html").exists():
        return True

    url = ("https://github.com/morpheus65535/bazarr/releases/download/v"
           + version + "/bazarr.zip")
    log("installing prebuilt frontend from " + url)
    tmp = None
    try:
        with urllib.request.urlopen(url, timeout=300) as resp:
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
                shutil.copyfileobj(resp, f)
                tmp = Path(f.name)
        with zipfile.ZipFile(tmp) as z:
            # The release zip's internal prefix has moved before; locate
            # frontend/build/ by content rather than assuming a root layout.
            members = [m for m in z.namelist() if "frontend/build/" in m
                       and not m.startswith("/") and ".." not in m]
            if not members:
                log("ERROR: release zip has no frontend/build/ members")
                return False
            if (build_dir / "index.html").exists():
                shutil.rmtree(build_dir)
            for m in members:
                rel = m[m.index("frontend/build/"):]
                dest = BAZARR2_BIN / rel
                if m.endswith("/"):
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(m) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
        if not (build_dir / "index.html").exists():
            log("ERROR: extraction finished but build/index.html is absent")
            return False
        log("frontend build installed (" + str(len(members)) + " files)")
        return True
    except Exception as exc:
        log("ERROR: frontend install failed: " + str(exc))
        return False
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def _apply_patches():
    server_py = BAZARR2_BIN / "bazarr" / "app" / "server.py"
    text = server_py.read_text()
    orig = text
    for pat, repl in PATCHES:
        text = re.sub(pat, repl, text)
    if text != orig:
        server_py.write_text(text)
        log("applied " + str(len(PATCHES)) + " patch(es) to " + str(server_py))


def _update_unit_version(new_version):
    unit = BAZARR2_UNIT_PATH.read_text()
    unit = re.sub(
        r'Environment="BAZARR_VERSION=[^"]*"',
        'Environment="BAZARR_VERSION=' + new_version + '"',
        unit,
    )
    BAZARR2_UNIT_PATH.write_text(unit)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)


def _systemctl(*args):
    return subprocess.run(["systemctl", "--user"] + list(args)).returncode


def _wait_for_bazarr2(key, timeout_s=60):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        v = _api_version(BAZARR2_URL, key)
        if v:
            return True
        time.sleep(2)
    return False


def sync_to(target_version, bazarr2_key):
    tag = "v" + target_version
    log("syncing bazarr2 to " + tag)

    if not _git_tag_exists(tag):
        msg = "upstream tag " + tag + " not found"
        log("ERROR: " + msg)
        _push_kuma("down", msg)
        return 1

    if _systemctl("stop", "bazarr2.service") != 0:
        log("WARN: stop returned non-zero; continuing")

    cp = subprocess.run(
        ["git", "-C", str(BAZARR2_BIN), "fetch", "--depth=1", "origin",
         "refs/tags/" + tag + ":refs/tags/" + tag],
        capture_output=True, text=True,
    )
    if cp.returncode != 0:
        msg = "git fetch failed: " + cp.stderr[:120]
        log("ERROR: " + msg)
        _systemctl("start", "bazarr2.service")
        _push_kuma("down", msg)
        return 1

    cp = subprocess.run(
        ["git", "-C", str(BAZARR2_BIN), "checkout", "-f", tag],
        capture_output=True, text=True,
    )
    if cp.returncode != 0:
        msg = "git checkout failed: " + cp.stderr[:120]
        log("ERROR: " + msg)
        _systemctl("start", "bazarr2.service")
        _push_kuma("down", msg)
        return 1

    _apply_patches()

    frontend_ok = _ensure_frontend(target_version, force=True)
    if not frontend_ok:
        # Degraded, not fatal: the version sync must still complete (the API
        # is the contract the stack depends on) - but the final Kuma push
        # carries the degradation so it is a signal, not a logfile WARN.
        log("WARN: continuing sync WITHOUT a web UI (frontend install failed)")

    venv_pip = HOME / ".apps" / "bazarr2" / "venv" / "bin" / "pip"
    cp = subprocess.run(
        [str(venv_pip), "install", "--quiet", "-r", str(BAZARR2_BIN / "requirements.txt")],
        capture_output=True, text=True, timeout=600,
    )
    if cp.returncode != 0:
        msg = "pip install failed: " + cp.stderr[:120]
        log("ERROR: " + msg)
        _push_kuma("down", msg)
        return 1

    _update_unit_version(target_version)

    if _systemctl("start", "bazarr2.service") != 0:
        msg = "bazarr2.service failed to start after sync"
        log("ERROR: " + msg)
        _push_kuma("down", msg)
        return 1

    if not _wait_for_bazarr2(bazarr2_key, timeout_s=60):
        msg = "bazarr2 API did not come back within 60s after sync"
        log("ERROR: " + msg)
        _push_kuma("down", msg)
        return 1

    final = _api_version(BAZARR2_URL, bazarr2_key)
    if not _semver_eq(final or "", target_version):
        msg = "post-sync version mismatch: got " + repr(final) + ", wanted " + target_version
        log("ERROR: " + msg)
        _push_kuma("down", msg)
        return 1

    log("SUCCESS: bazarr2 now at " + str(final))
    if not frontend_ok:
        _push_kuma("down", "synced to " + str(final) + " but UI missing (frontend install failed)")
        return 1
    _push_kuma("up", "synced to " + str(final))
    return 0


def main():
    b1_key = _read_apikey(HOME / ".apps" / "bazarr" / "config" / "config.yaml")
    b2_key = _read_apikey(HOME / ".apps" / "bazarr2" / "data" / "config" / "config.yaml")
    if not b1_key or not b2_key:
        log("ERROR: could not read both Bazarr API keys from their config.yaml files")
        return 2

    v1 = _api_version(BAZARR1_URL, b1_key)
    v2 = _api_version(BAZARR2_URL, b2_key)
    if not v1:
        log("bazarr-1 unreachable; skipping (will retry next tick)")
        return 2
    if not v2:
        log("bazarr-2 unreachable; pushing DOWN")
        _push_kuma("down", "bazarr2 API unreachable")
        return 2

    log("bazarr-1=" + repr(v1) + "  bazarr-2=" + repr(v2))
    if _semver_eq(v1, v2):
        # Versions agree, but a tag checkout ships no web UI (see
        # _ensure_frontend) - heal that here so a missing build is a
        # one-tick outage, not a permanent one. Restart only when the
        # build was actually (re)installed.
        if not (BAZARR2_BIN / "frontend" / "build" / "index.html").exists():
            if _ensure_frontend(v2):
                _systemctl("restart", "bazarr2.service")
                if not _wait_for_bazarr2(b2_key, timeout_s=60):
                    log("ERROR: bazarr2 did not come back after frontend heal")
                    _push_kuma("down", "bazarr2 down after frontend heal")
                    return 1
                log("frontend healed and bazarr2 restarted")
            else:
                # DOWN, not a log-file WARN (council 2026-08-18, gen-opus-1
                # F-03 / gen-opus-2 QF-01): pushing "up: in sync" over a
                # missing UI is the EXACT six-week silent-UI class the heal
                # was built to close, re-opened on its own failure branch.
                # Nothing else observes the UI (the app probe is API-only),
                # so this push is the one place the silence can become a
                # signal. Self-clearing: the next hourly tick re-heals and
                # pushes up.
                log("ERROR: web UI still missing (frontend install failed); API unaffected")
                _push_kuma("down", "UI missing and frontend install failed; API healthy")
                return 1
        log("versions match; no-op")
        _push_kuma("up", "in sync at " + v1)
        return 0

    log("DRIFT: bazarr-2 " + v2 + " != bazarr-1 " + v1)
    return sync_to(v1, b2_key)


if __name__ == "__main__":
    sys.exit(main())
