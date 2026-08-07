#!/usr/bin/env python3
"""e2e-entitlement-live.py -- drive the real gate against real Plex and real Seerr.

RUNS ON THE BOX. Mutates exactly ONE account -- the operator's crash-test
account -- through the entire lifecycle and restores it, then diffs against the
captured before-state and fails on any residue.

    capture -> EXPIRE (shrink to Welcome + Seerr 0) -> verify
            -> ENTITLE (expand to all + Seerr restored) -> verify
            -> restore -> diff

WHY THIS EXISTS
---------------
Every other test in this suite is pure. They prove the decision table is right
and that no injected fault can revoke. None of them prove that the PUT body
this code sends is one plex.tv accepts, that `library_section_ids` really wants
Section@id, that a Seerr permission write really sticks, or that the restore
puts back exactly what was there. Those are all wire-format claims, and a
wire-format claim that has only ever been checked against a mock is a guess.

HOW THE TWO HALVES ARE FORCED
-----------------------------
The Patreon campaign has no members, so the live API says entitled:false for
everyone. That gives the EXPIRE half for free.

For the ENTITLE half, a local stub is stood up on 127.0.0.1 and
$QFLIX_ENTITLEMENT_URL is pointed at it. The gate's own code path is otherwise
completely unchanged -- same client, same parsing, same three-valued grading,
same real writes to real Plex and real Seerr. Only the upstream answer is
substituted, which is the one thing that cannot be arranged for real.

THE BLAST-RADIUS RAILS
----------------------
This script writes a THROWAWAY roster in which every household except the
crash-test one is marked `exempt`, and exempt is terminal -- no lookup, no
clock, no provisioning, no mutation. Combined with --max-mutations 2 and a
scratch state directory, the reachable blast radius is one account. The real
secrets/members.yaml and the real state file are never opened for writing.
"""
from __future__ import annotations

import copy
import datetime as dt
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

HOME = Path.home()
SECRETS = HOME / "secrets"
GATE = HOME / "scripts" / "maint" / "qflix-entitlement.py"
MACHINE_ID = "53a83e840bd624da3a105b10ef265f2e676165ee"
WELCOME_TITLE = "QFlix - Welcome"

# The crash-test account's address is MEMBER DATA and this repo is public, so it
# is not written down here. It comes from the environment or from gitignored
# secrets/, exactly like every other value that must not be committed.
#
# There is no default. A default would be either a real address (the thing this
# is avoiding) or a placeholder that silently matches nobody, in which case the
# harness would find no subject, mutate nothing, and print a wall of passes --
# a green run that tested precisely zero of the write paths.
SUBJECT = (os.environ.get("QFLIX_E2E_SUBJECT") or "").strip().lower()
if not SUBJECT:
    _f = Path.home() / "secrets" / "e2e-subject"
    try:
        SUBJECT = _f.read_text(encoding="utf-8").strip().lower()
    except OSError:
        raise SystemExit(
            "no crash-test subject. Set $QFLIX_E2E_SUBJECT to the Plex email of "
            "the throwaway account, or write it to %s. It is deliberately not "
            "hardcoded: member addresses never enter this repo." % _f)

FAILURES: list = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print("  %s %s%s" % ("PASS" if ok else "FAIL", label,
                         (" -- " + detail) if detail else ""), flush=True)
    if not ok:
        FAILURES.append(label + (" -- " + detail if detail else ""))
    return ok


def read_secret(name: str) -> str:
    return (SECRETS / name).read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# Live readers (independent of the gate's own code, on purpose: a shared bug
# would otherwise verify itself)
# ---------------------------------------------------------------------------

def plex_state():
    token = read_secret("plex.token")
    url = ("https://plex.tv/api/servers/%s/shared_servers?X-Plex-Token=%s"
           % (MACHINE_ID, token))
    with urllib.request.urlopen(url, timeout=30) as r:
        root = ET.fromstring(r.read().decode("utf-8"))
    for ss in root.iter("SharedServer"):
        if (ss.get("email") or "").lower() == SUBJECT:
            return {
                "shared_server_id": ss.get("id"),
                "user_id": ss.get("userID"),
                "all_libraries": ss.get("allLibraries"),
                "sections": sorted(int(s.get("id")) for s in ss.findall("Section")
                                   if s.get("shared") == "1"),
            }
    return None


def plex_catalogue():
    token = read_secret("plex.token")
    with urllib.request.urlopen(
            "https://plex.tv/api/servers/%s?X-Plex-Token=%s" % (MACHINE_ID, token),
            timeout=30) as r:
        root = ET.fromstring(r.read().decode("utf-8"))
    return {int(s.get("id")): s.get("title") for s in root.iter("Section")}


def seerr_state():
    port, key = read_secret("seerr.port"), read_secret("seerr.key")
    req = urllib.request.Request(
        "http://127.0.0.1:%s/api/v1/user?take=200" % port,
        headers={"X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    for u in data.get("results", []):
        if (u.get("email") or "").lower() == SUBJECT:
            return {"id": u["id"], "permissions": u.get("permissions"),
                    "userType": u.get("userType")}
    return None


# ---------------------------------------------------------------------------
# Entitlement stub
# ---------------------------------------------------------------------------

class _Stub(http.server.BaseHTTPRequestHandler):
    entitled = True

    def do_GET(self):
        if self.path.startswith("/healthz"):
            body = {"status": "ok"}
        elif _Stub.entitled:
            body = {"entitled": True, "email": SUBJECT, "status": "active_patron",
                    "tiers": ["e2e"], "amount_cents": 500, "stale": False,
                    "synced_at": "2026-08-06T00:00:00+00:00"}
        else:
            body = {"entitled": False, "email": SUBJECT,
                    "status": "former_patron", "stale": False}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def start_stub():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


# ---------------------------------------------------------------------------
# Throwaway roster: only the subject is governed; everybody else is exempt.
# ---------------------------------------------------------------------------

def build_roster(dest: Path, amnesty: str) -> None:
    real = yaml.safe_load((SECRETS / "members.yaml").read_text(encoding="utf-8"))
    out = copy.deepcopy(real)
    out["armed"] = True
    out["defaults"] = dict(out.get("defaults") or {})
    out["defaults"]["amnesty_until"] = amnesty
    out["defaults"]["grace_days"] = 7
    out["defaults"]["new_arrival_days"] = 30

    kept = 0
    for h in out["households"]:
        accounts = [a.lower() for a in (h.get("accounts") or [])]
        if SUBJECT in accounts:
            h["exempt"] = False
            h.setdefault("billing", {})
            h["billing"]["holder"] = SUBJECT
            h["billing"]["amount_usd"] = 5.0
            h["billing"]["rail"] = "patreon"
            h["billing"]["payer_ref"] = "e2e-crash-test"
            h.pop("provisional", None)
            kept += 1
        else:
            # Exempt is terminal. Every other real member is unreachable from
            # this run: no lookup, no clock, no provisioning, no mutation.
            h["exempt"] = True
            h.pop("billing", None)
            h.pop("provisional", None)
            h["reason"] = "e2e harness: out of scope for this test run"
    if kept != 1:
        raise SystemExit("expected exactly 1 governed household, got %d" % kept)
    dest.write_text(yaml.safe_dump(out, sort_keys=False), encoding="utf-8")


def run_gate(roster: Path, state_dir: Path, env_extra=None, execute=True):
    env = dict(os.environ)
    env["QFLIX_MEMBERS"] = str(roster)
    env.update(env_extra or {})
    cmd = [sys.executable, str(GATE), "--members", str(roster),
           "--state-dir", str(state_dir), "--max-mutations", "2",
           "--no-kuma", "--no-notify", "--ignore-window"]
    if execute:
        cmd.append("--execute")
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
    for line in (p.stdout or "").splitlines():
        if SUBJECT.split("@")[0][:2] in line or "done:" in line or "APPLIED" in line:
            print("    | " + line, flush=True)
    if p.returncode not in (0, 1):
        print("    | STDERR: " + (p.stderr or "")[:800], flush=True)
    return p


# ---------------------------------------------------------------------------
def main() -> int:
    print("=" * 72)
    print("QFlix entitlement gate -- LIVE end-to-end on the crash-test account")
    print("=" * 72)

    catalogue = plex_catalogue()
    welcome_id = next((i for i, t in catalogue.items()
                       if (t or "").strip().lower() == WELCOME_TITLE.lower()), None)
    all_ids = sorted(catalogue)
    if welcome_id is None:
        print("FATAL: no %r section" % WELCOME_TITLE)
        return 2
    print("catalogue: %d sections, Welcome id=%d" % (len(all_ids), welcome_id))

    before_plex, before_seerr = plex_state(), seerr_state()
    if before_plex is None or before_seerr is None:
        print("FATAL: subject not found (plex=%s seerr=%s)"
              % (bool(before_plex), bool(before_seerr)))
        return 2
    print("BEFORE  plex sections=%s  seerr id=%s perms=%s"
          % (before_plex["sections"], before_seerr["id"], before_seerr["permissions"]))

    work = Path(tempfile.mkdtemp(prefix="qflix-e2e-"))
    state_dir = work / "state"
    state_dir.mkdir()
    roster = work / "roster.yaml"
    srv, stub_url = start_stub()
    rc = 1

    try:
        # ---- 0. ESTABLISH -- entitled once, so a later lapse is a LAPSE ---
        #
        # A member who has never been entitled is "pending", not "lapsing", and
        # gets no lapse clock. Establishing entitlement first is what makes the
        # rest of this a test of the real revocation path rather than of the
        # amnesty path.
        print("\n[0] ESTABLISH -- upstream says entitled, seeds the cohort")
        # A PAST amnesty, so the lapse clock is the binding deadline rather than
        # the launch amnesty. With a future amnesty the max() correctly protects
        # the account and no revocation path is exercised at all -- which is the
        # right behaviour, and the reason this date has to be in the past for
        # the test to reach the code it is testing.
        build_roster(roster, amnesty="2026-06-01")
        _Stub.entitled = True
        run_gate(roster, state_dir, {"QFLIX_ENTITLEMENT_URL": stub_url})

        # ---- 1. LAPSE -- grace must be GRANTED, not skipped ---------------
        #
        # This assertion exists because of a confirmed review finding: an
        # earlier version recorded the lapse AFTER planning, so the run that
        # first observed it computed a deadline with no lapse clock and reduced
        # the member on the spot -- zero of the week they were promised.
        print("\n[1] LAPSE -- first observation must grant the full grace, not reduce")
        _Stub.entitled = False
        run_gate(roster, state_dir, {"QFLIX_ENTITLEMENT_URL": stub_url})
        mid = plex_state()
        check("lapse does NOT reduce on the run that first sees it",
              mid["sections"] == before_plex["sections"],
              "got %s" % mid["sections"])
        st = json.loads((state_dir / "state.json").read_text())
        check("the 7-day lapse clock was started",
              st["accounts"][SUBJECT].get("went_false_at") is not None)

        # ---- 1b. TIME PASSES ----------------------------------------------
        #
        # Backdating the state file is the only way to test a week-long clock in
        # a run that takes seconds. Everything else stays real -- the gate reads
        # this state exactly as it would after a genuine week.
        print("\n[1b] EXPIRE -- eight days later, grace exhausted")
        sp = state_dir / "state.json"
        st = json.loads(sp.read_text())
        st["first_run_at"] = "2026-01-01T00:00:00Z"          # floor long past
        st["accounts"][SUBJECT]["went_false_at"] = (
            (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8))
            .strftime("%Y-%m-%dT%H:%M:%SZ"))
        sp.write_text(json.dumps(st, indent=2), encoding="utf-8")
        run_gate(roster, state_dir, {"QFLIX_ENTITLEMENT_URL": stub_url})

        after = plex_state()
        after_s = seerr_state()
        check("plex reduced to Welcome only",
              after["sections"] == [welcome_id], "got %s" % after["sections"])
        check("plex share still EXISTS (not evicted)", after is not None)
        check("seerr permissions set to 0",
              after_s["permissions"] == 0, "got %s" % after_s["permissions"])
        check("seerr account still exists", after_s["id"] == before_seerr["id"])

        saved = json.loads((state_dir / "state.json").read_text())
        prior = saved["accounts"].get(SUBJECT, {}).get("seerr_perms_prior")
        check("prior permissions were saved BEFORE zeroing",
              prior == before_seerr["permissions"],
              "saved %s, was %s" % (prior, before_seerr["permissions"]))

        # ---- 2. ENTITLE --------------------------------------------------
        print("\n[2] ENTITLE -- upstream now says entitled")
        _Stub.entitled = True
        run_gate(roster, state_dir, {"QFLIX_ENTITLEMENT_URL": stub_url})

        after = plex_state()
        after_s = seerr_state()
        check("plex expanded to every section",
              after["sections"] == all_ids, "got %s want %s" % (after["sections"], all_ids))
        check("seerr permissions restored to the ORIGINAL value, not the default",
              after_s["permissions"] == before_seerr["permissions"],
              "got %s want %s" % (after_s["permissions"], before_seerr["permissions"]))

        # ---- 3. IDEMPOTENCE ----------------------------------------------
        print("\n[3] IDEMPOTENCE -- a second identical run must change nothing")
        p = run_gate(roster, state_dir, {"QFLIX_ENTITLEMENT_URL": stub_url})
        applied = [l for l in (p.stdout or "").splitlines() if "APPLIED" in l]
        check("second run applied nothing", applied == [],
              "applied %d" % len(applied))

        # ---- 4. OUTAGE ---------------------------------------------------
        print("\n[4] OUTAGE -- entitlement API unreachable must move NOTHING")
        srv.shutdown()
        before_outage = plex_state()["sections"]
        p = run_gate(roster, state_dir,
                     {"QFLIX_ENTITLEMENT_URL": "http://127.0.0.1:1"})
        applied = [l for l in (p.stdout or "").splitlines() if "APPLIED" in l]
        check("outage applied nothing", applied == [], "applied %d" % len(applied))
        check("plex unchanged during outage",
              plex_state()["sections"] == before_outage)
        check("exit code says UNAVAILABLE (3), not OK",
              p.returncode == 3, "got %d" % p.returncode)

        rc = 0
    finally:
        # ---- restore -----------------------------------------------------
        print("\n[5] RESTORE to the captured before-state")
        try:
            sys.path.insert(0, str(HOME / "scripts" / "maint" / "lib"))
            import plexshare as PS
            import seerrusers as SU
            c = PS.PlexShareClient(token=read_secret("plex.token"),
                                   machine_id=MACHINE_ID)
            share = next(s for s in c.shares() if s.email.lower() == SUBJECT)
            c.set_sections(share, before_plex["sections"])
            sc = SU.client_from_secrets()
            u = next(u for u in sc.users() if u.email.lower() == SUBJECT)
            sc.set_permissions(u, before_seerr["permissions"])
        except Exception as e:
            FAILURES.append("restore failed: %s" % e)
            print("  RESTORE FAILED: %s" % e)

        final_p, final_s = plex_state(), seerr_state()
        check("final plex sections match before-state",
              final_p["sections"] == before_plex["sections"],
              "%s vs %s" % (final_p["sections"], before_plex["sections"]))
        check("final seerr permissions match before-state",
              final_s["permissions"] == before_seerr["permissions"],
              "%s vs %s" % (final_s["permissions"], before_seerr["permissions"]))
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + "=" * 72)
    if FAILURES:
        print("FAILED (%d):" % len(FAILURES))
        for f in FAILURES:
            print("  - " + f)
        return 1
    print("ALL CHECKS PASSED -- account restored to its exact before-state")
    return rc


if __name__ == "__main__":
    sys.exit(main())
