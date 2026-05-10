#!/usr/bin/env python3
"""cp_upgrade_clicker — headless Playwright that logs into cp.ultra.cc and
clicks "Upgrade & Repair" on every app that has the option available.

Background: many apps installed via Ultra.cc's control panel (e.g.
Maintainerr) have no `app-<name> update` CLI verb. The control-panel UI is
the only documented upgrade path. This script automates it so the Monday
04:00 maintenance window can roll updates without operator intervention.

Per Ultra.cc FAQ: only one upgrade at a time. We click sequentially.
Apps without an Upgrade & Repair option (e.g. some lifecycle-only apps)
are silently skipped — this is normal.

Designed to run inside the newsletterr venv on the seedbox, where
Playwright + Chromium are already installed.

Usage:
  python3 cp_upgrade_clicker.py            # do it
  python3 cp_upgrade_clicker.py --dry-run  # log what would happen, no clicks
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

CP_URL = "https://cp.ultra.cc"
SECRETS_DIR = Path(os.environ.get("MANITOBA_SECRETS", str(Path.home() / "secrets")))


def _read_username() -> str:
    """cp.ultra.cc login is email-based (the form is AngularJS type=email).
    Read from secrets/ultracc.user if present, else fall back to env, else
    error out — never silently use a wrong default."""
    f = SECRETS_DIR / "ultracc.user"
    if f.exists():
        return f.read_text().strip()
    env = os.environ.get("ULTRACC_USER")
    if env:
        return env
    print(
        f"FATAL: no username — set {f} or ULTRACC_USER env var\n"
        "(cp.ultra.cc expects an email, not a system username)",
        file=sys.stderr,
    )
    sys.exit(2)

# Per-app upgrade timeout — Ultra.cc upgrade can take a few minutes for
# heavier apps (Plex, Jellyfin). Cap at 8 minutes per app.
PER_APP_TIMEOUT_S = 8 * 60

# Maximum total runtime for the whole sweep — protects against runaway
# loops if a selector breaks. Maintenance window is 240 min; cap at 200.
TOTAL_BUDGET_S = 200 * 60


def _read_password() -> str:
    pw_file = SECRETS_DIR / "htpasswd.password"
    if not pw_file.exists():
        print(f"FATAL: {pw_file} not found", file=sys.stderr)
        sys.exit(2)
    return pw_file.read_text().strip()


def _notify(msg: str, level: str = "info") -> None:
    """Best-effort Notifiarr POST — don't fail the run if notifications fail."""
    try:
        import requests
        nf_key_file = SECRETS_DIR / "notifiarr.key"
        if not nf_key_file.exists():
            return
        key = nf_key_file.read_text().strip()
        body = {
            "notification": {
                "name": "cp-upgrade-clicker",
                "event": "upgrade_sweep",
            },
            "discord": {
                "color": "ff8800" if level == "warning" else "00cc66",
                "text": {"description": msg[:1900]},
            },
        }
        requests.post(
            f"https://notifiarr.com/api/v1/notification/passthrough/{key}",
            json=body,
            timeout=10,
        )
    except Exception as exc:
        print(f"notify failed (non-fatal): {exc}", file=sys.stderr)


def _login(page, username: str, password: str) -> None:
    """Navigate to cp.ultra.cc and complete the login form.

    The form is AngularJS — username is `type="email"` with ng-model
    validation. page.fill() fires input/change events that AngularJS
    listens to, but AngularJS's form-level $invalid only clears once
    the field's validator (RFC 5322 email check) passes. So pass an
    email-format string."""
    page.goto(CP_URL, wait_until="networkidle", timeout=30_000)

    # Specific to Ultra.cc's AngularJS login form.
    page.fill('input[name="username"]', username)
    page.fill('input[name="password"]', password)

    # Submit by pressing Enter inside the password field — works even if
    # the button's ng-disabled hasn't propagated yet (AngularJS digest cycle).
    page.locator('input[name="password"]').press("Enter")

    # AngularJS routes after submit; networkidle fires before the hash-route
    # change. Wait specifically for the URL fragment to leave /login.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if "#/login" not in page.url.lower():
            break
        page.wait_for_timeout(500)
    else:
        raise RuntimeError(f"still on login page after submit: {page.url}")
    page.wait_for_load_state("networkidle", timeout=15_000)


# Selectors specific to cp.ultra.cc's AngularJS Service Manager UI.
APP_ROW_SELECTOR = 'tr[ng-repeat-start*="vma.applications"]'


def _navigate_to_apps(page) -> None:
    """Land on the user's Service page → Apps tab.

    cp.ultra.cc top-nav doesn't have a global Apps link — it's per-service.
    """
    page.wait_for_timeout(2_000)
    href = page.locator('a[href*="userservice"]').first.get_attribute("href")
    if not href:
        raise RuntimeError("could not find userservice link post-login")
    target = f"{CP_URL}/{href}" if href.startswith("#") else f"{CP_URL}{href}"
    print(f"  navigate -> {target}")
    page.goto(target, timeout=30_000)
    page.wait_for_timeout(6_000)
    print(f"  on service page: {page.url}")
    apps_btn = page.locator('a:has-text("Apps"), button:has-text("Apps")').first
    if apps_btn.count() == 0:
        raise RuntimeError("Apps tab not found on service page")
    apps_btn.click()
    # AngularJS digest + ng-repeat for 12+ rows takes a beat.
    page.wait_for_timeout(6_000)
    rows = page.locator(APP_ROW_SELECTOR)
    print(f"  app rows visible: {rows.count()}")


def _list_installed_apps(page) -> list[str]:
    """Return the human-readable name of each installed UCC app.

    Each row is `tr[ng-repeat-start="app in vma.applications | …"]` with
    an h4 / strong / td-first-cell containing the app name. We grab the
    first non-empty text under 40 chars."""
    rows = page.locator(APP_ROW_SELECTOR)
    out: list[str] = []
    for i in range(rows.count()):
        row = rows.nth(i)
        name = ""
        for sel in ("h4", "strong", "td:first-child", "td"):
            cand = row.locator(sel).first
            if cand.count() > 0:
                t = (cand.inner_text() or "").strip().split("\n")[0].strip()
                if t and len(t) < 40:
                    name = t
                    break
        # Fall back to row index if no name extracted
        out.append(name or f"app#{i}")
    return out


def _click_upgrade_for_row(page, row_idx: int, *, dry_run: bool = False) -> str:
    """Open the Actions menu for the row at `row_idx` and click Upgrade & Repair.

    Returns one of:
      "upgraded"      — menu item clicked + completion banner observed
      "would_upgrade" — dry-run, menu item exists but not clicked
      "no_button"     — menu open but Upgrade & Repair missing (anomaly)
      "timeout"       — clicked but no completion banner within budget
      "error: <msg>"  — anything else
    """
    rows = page.locator(APP_ROW_SELECTOR)
    if row_idx >= rows.count():
        return f"error: row {row_idx} out of range ({rows.count()} rows)"

    row = rows.nth(row_idx)

    # Open the Actions menu
    toggle = row.locator("button.dropdown-toggle").first
    if toggle.count() == 0:
        return "error: dropdown-toggle not found in row"
    try:
        toggle.click(timeout=5_000)
    except Exception as exc:
        return f"error: dropdown click: {exc}"

    page.wait_for_timeout(500)

    # The menu is rendered in a popover / sibling ul; search globally for
    # the most-recently-opened one that contains Upgrade & Repair.
    upgrade_btn = page.locator(
        'ul.dropdown-menu:visible >> a:has-text("Upgrade & Repair")'
    ).first
    if upgrade_btn.count() == 0:
        # Some renderings nest items in li > a without role.
        upgrade_btn = page.locator(
            '.dropdown-menu a:has-text("Upgrade & Repair")'
        ).first

    if upgrade_btn.count() == 0:
        page.keyboard.press("Escape")
        return "no_button"

    if dry_run:
        page.keyboard.press("Escape")
        return "would_upgrade"

    upgrade_btn.click()

    # Confirmation modal — Ultra.cc typically asks "Are you sure?" with a
    # "Confirm" / "Yes" button. Click whichever is present.
    page.wait_for_timeout(500)
    for sel in (
        'button:has-text("Confirm")',
        'button:has-text("Yes")',
        'button:has-text("Upgrade")',
        '.modal-footer button.btn-primary',
    ):
        cand = page.locator(sel).first
        if cand.count() > 0 and cand.is_visible():
            try:
                cand.click(timeout=3_000)
                break
            except Exception:
                pass

    # Wait for completion. cp.ultra.cc shows a sticky toast or alert.
    deadline = time.monotonic() + PER_APP_TIMEOUT_S
    completion_patterns = ("Successfully", "successfully", "complete", "Complete")
    while time.monotonic() < deadline:
        body = page.locator("body").inner_text()
        if any(p in body for p in completion_patterns):
            return "upgraded"
        page.wait_for_timeout(2_000)

    return "timeout"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="don't actually click Upgrade & Repair, just enumerate")
    parser.add_argument("--only", default="",
                        help="comma-separated app name substrings to limit the sweep "
                             "(case-insensitive); useful for partial-live testing")
    args = parser.parse_args()
    only_filters = [s.strip().lower() for s in args.only.split(",") if s.strip()]

    user = _read_username()
    pw = _read_password()
    started = time.monotonic()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("FATAL: playwright not importable — install via the newsletterr venv", file=sys.stderr)
        return 2

    results: dict[str, str] = {}
    error: Optional[str] = None

    with sync_playwright() as pw_ctx:
        # Firefox, not Chromium — Ultra.cc's seccomp filter SIGTRAPs
        # Chromium when it tries to set up its remote-debugging IPC
        # (sandboxing, namespace ops). Firefox launches cleanly.
        browser = pw_ctx.firefox.launch(headless=True)
        # Use browser.new_page() directly (not new_context first) to mirror
        # the interactive test path that works. Some auth state isn't
        # carried into a fresh new_context() on Ultra.cc's AngularJS app.
        page = browser.new_page()

        try:
            _login(page, user, pw)
            _navigate_to_apps(page)
            names = _list_installed_apps(page)
            print(f"discovered {len(names)} installed app(s) in cp.ultra.cc Apps tab")

            if only_filters:
                # Keep (idx, name) pairs so click_upgrade_for_row uses the
                # correct row index even after filtering.
                pairs = [(i, n) for i, n in enumerate(names)
                         if any(f in n.lower() for f in only_filters)]
                print(f"--only filter applied: {len(pairs)} of {len(names)} match "
                      f"{only_filters}")
            else:
                pairs = list(enumerate(names))

            for idx, name in pairs:
                if time.monotonic() - started > TOTAL_BUDGET_S:
                    print(f"BUDGET exceeded after {name} — bailing", file=sys.stderr)
                    break
                try:
                    res = _click_upgrade_for_row(page, idx, dry_run=args.dry_run)
                except Exception as exc:
                    res = f"error: {exc}"
                results[name] = res
                tag = "DRY" if args.dry_run else "DO "
                print(f"  [{tag}] {name:35s} {res}")
                # Cooldown between apps — Ultra.cc says one at a time. Also
                # gives the dropdown a chance to fully close before next row.
                page.wait_for_timeout(2_000)

        except Exception as exc:
            error = f"clicker fatal: {exc}"
            print(error, file=sys.stderr)
        finally:
            browser.close()

    # Summarize + notify
    upgraded = sum(1 for v in results.values() if v == "upgraded")
    # mariadb is uninstalled — every remaining app should expose Upgrade &
    # Repair. Treat absence as a flag-worthy anomaly, not a silent skip.
    no_button = sum(1 for v in results.values() if v == "no_button")
    failed = sum(1 for v in results.values() if v.startswith("error") or v == "timeout")
    summary = (
        f"cp upgrade sweep ({'DRY-RUN' if args.dry_run else 'live'}): "
        f"upgraded={upgraded} no_button={no_button} failed={failed} "
        f"total={len(results)}"
    )
    print(summary)
    if error:
        _notify(f"{summary}\n\nFATAL: {error}", level="warning")
        return 3

    # Include all non-trivial outcomes (upgraded + failed + no_button) in the
    # Discord summary so anomalies surface.
    detail_lines = [f"{k}: {v}" for k, v in results.items()
                    if v != "would_upgrade"]
    if detail_lines:
        summary += "\n" + "\n".join(detail_lines[:30])
    _notify(summary, level="info" if (failed == 0 and no_button == 0) else "warning")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
