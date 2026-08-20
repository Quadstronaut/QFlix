#!/usr/bin/env python3
"""Disable every Remux-* quality on every RADARR MAIN quality profile.
Idempotent - safe to re-run. Sibling of 57-no-4k-enforce.py and deliberately
built to the same shape (same secrets loading, same recursive items[] walk,
same cutoff-repair logic, no argparse, applies directly).

WHY THIS EXISTS - the concrete failure it prevents
--------------------------------------------------
One member's Plex client could not play a SINGLE movie from 2026-07-25
onward. TV worked fine on that same client the whole time. (Identity stays out
of this repo by operator directive - the affected account is recorded in the
box-side roster only.) Measured 2026-08-19 against the live stack:

  * Radarr main profile id 6 "HD 720p/1080p" holds 112 of 114 movies and had
    Remux-1080p (quality id 30) ALLOWED, at the TOP of its items[] list.
    upgradeAllowed=false, cutoff=6 (Bluray-720p).
  * 23 of the 46 movies-with-a-file were therefore Remux-1080p: 20-37 Mbps
    video, TrueHD / DTS-HD MA 6-8 channel audio, 572 GB across 23 files
    (GiB, the unit all the maint tooling reports in).
  * The client negotiates targetBitrate 1927 kbps with videoDecision=transcode
    and HardwareAcceleratedCodecs=0, on a shared seedbox with no GPU. A 30 Mbps
    TrueHD remux transcoded to ~1.9 Mbps in software never keeps up. Every
    movie stalled; every TV episode (WEBDL-1080p, ~4-8 Mbps) played.
  * All 46 files were added since 2026-07-01. That is not a coincidence:
    qflix-reaper's add-date retention (DEFAULT_THRESHOLD_DAYS, currently 45)
    churns the whole movie library, so EVERY re-grab lands on the highest
    ALLOWED quality - which was remux. The library was converging on 100%
    unplayable-on-that-client by construction. upgradeAllowed=false does not
    save you: it only stops UPGRADES, it does not stop the INITIAL grab from
    taking the best allowed release.

A prior audit agent blamed profile 7 "HD Bluray + WEB". That was WRONG and cost
a cycle: profile 7 does NOT allow Remux and holds exactly 1 movie. The mistake
comes from reading profile.items[] non-recursively. Radarr's items[] is a
NESTED tree - a group entry ("WEB 1080p", id 1002) carries its own allowed flag
AND an items[] list of real qualities. Walk it recursively or you will read the
wrong profile's allowed set. Every walker below is recursive for that reason.

SCOPE: RADARR MAIN ONLY. A deliberate decision, not an oversight.
----------------------------------------------------------------
sonarr / sonarr2 / radarr2 are NOT touched. Every one of them was ENUMERATED
2026-08-19, not assumed - here is what each actually holds and why it stays.

  * sonarr2 profile 7 "[Anime] Remux-1080p" allows Bluray-1080p Remux, and it
    IS recyclarr's TRaSH template 20e0fc959f1f1704bed501f23bdae76f (bound in
    56-recyclarr-install.sh under sonarr:anime). Capping it here would be
    silently reverted on the next recyclarr sync, so this script would be a lie
    that passes its own re-run check while prod drifts back. Two policy
    surfaces describing one intent, and the templated one wins. If the anime
    remux tier ever needs capping, do it in the recyclarr config, not here.

  * radarr2 profiles 1 "Any", 4 "HD-1080p" and 6 "HD - 720p/1080p" allow
    Remux-1080p. These are Radarr FACTORY defaults - recyclarr binds radarr2 to
    "HD Bluray + WEB" (d1d67249d3890e49bc12e275d989a7e9) only, so nothing would
    revert a change here. They stay out because of blast radius, not ownership:
    radarr2 is the anime/foreign MOVIE instance, 6 movies total, 3 with a file,
    exactly 1 of which is a remux. Different library, different audience, and
    one file is not a wave.

  * sonarr MAIN profile 6 "HD 720p/1080p" ALSO allows Bluray-1080p Remux, and
    ALL 36 series sit on it. TV is NOT structurally safe - it is a LATENT
    recurrence of this exact bug that has simply not drawn a remux release yet
    (every episode file measured today is WEBDL-class, ~4-8 Mbps, which is why
    TV kept playing while movies did not). It is out of scope for THIS change
    on purpose: one instance, one blast radius, one thing to roll back. Do not
    read its absence as "TV was checked and is fine". It was checked and it is
    ARMED. Cap it in a separate, separately-revertable change.

NOT A REVERT RISK ON RADARR MAIN: recyclarr binds radarr main to "HD Bluray +
WEB" (d1d67249..., live profile id 7). The profile this script changes is id 6
"HD 720p/1080p", an unmanaged Radarr factory default that holds 112 of the 114
movies. Nothing syncs over it. Note the standing oddity that implies: the TRaSH
scoring recyclarr installs lands on a profile holding ONE movie, while 112 sit
on an unmanaged default. That is a real finding, and it is not this script's
job to fix.

WHAT IT DOES, per radarr-main profile:
  1. recursively set allowed=false on every quality whose name contains
     "remux" (case-insensitive) - Remux-1080p, Remux-2160p, and any future
     "<x> Remux" naming;
  2. collapse a group to allowed=false once none of its children are allowed;
  3. REFUSE and leave the profile completely untouched if step 1 would strip
     the last allowed quality (a zero-quality profile is not a cap, it is a
     broken profile Radarr can never grab against again);
  4. repair profile.cutoff if it pointed at a now-disabled quality.

CUTOFF REPAIR DIFFERS FROM 57 ON PURPOSE. 57 picks max(allowed id). On Radarr's
real profile shape that picks a GROUP id, because group ids are 1000+ and always
numerically larger than any quality id - so a Bluray-1080p cutoff would be
silently rewritten to the "WEB 1080p" group, a downgrade. Radarr stores and
renders items[] worst-to-best, so the correct "highest allowed" is the LAST
allowed TOP-LEVEL entry, which is what highest_allowed_id() returns. The live
profile 6 has cutoff=6 (Bluray-720p, still allowed), so this path is a no-op
today; it exists so a remux-cutoff profile is not quietly downgraded to WEB.

Run on the seedbox (reads ~/secrets/radarr.{key,port,urlbase}). Or pipe via
SSH:  sshm "python3 -" < scripts/configure/58-remux-cap-enforce.py

Self-contained by design (no lib.* imports) precisely so that pipe works.

VERIFICATION SURFACE - this script is a WRITE, the gate is elsewhere
--------------------------------------------------------------------
Running this once proves nothing tomorrow. A UI click, a Radarr upgrade that
restores factory profile defaults, or a recyclarr binding change can re-allow
Remux-1080p with no signal at all - the only symptom is a member's client
failing every movie again, weeks later. So the policy has a live re-read gate:
scripts/smoke-test.sh check 13n "remux-cap-radarr" re-reads /qualityprofile and
FAILS if any radarr-main profile allows a remux tier. It walks items[]
RECURSIVELY; its sibling 13f (no-4k) does not, and measured on the box
2026-08-19 sonarr2 scores flat=0 / recursive=1, so a non-recursive gate would
have been blind by construction. If you widen ARRS here, widen 13n with it.

AFTER RUNNING THIS: existing remux files are NOT downgraded. Radarr never
downgrades on its own. Either wait for qflix-reaper to age them out, or run
scripts/maint/qflix-remux-regrab.py --execute to force the re-grab.

EXIT: 0 clean, 1 if any GET/PUT failed. (57 always returned 0; a silent write
failure on a policy enforcer is worth an exit code.)
"""
from __future__ import annotations

import copy
import json
import os
import sys
import urllib.error
import urllib.request


# Radarr MAIN only. Read the SCOPE block above before adding an entry here.
ARRS = {
    "radarr": "v3",
}

TIMEOUT = 15


def secret(name: str) -> str:
    with open(os.path.expanduser("~/secrets/" + name)) as f:
        return f.read().strip()


def secret_or(name: str, fallback: str) -> str:
    try:
        return secret(name)
    except FileNotFoundError:
        return fallback


# ---------------------------------------------------------------------------
# Pure tree walkers over Radarr's NESTED items[]. Everything below is offline
# and side-effect-free apart from the in-place mutation it advertises, so
# tests/unit/test_remux_cap.py can drive it with no box and no network.
# ---------------------------------------------------------------------------
def is_group(item: dict) -> bool:
    """A group has its own id+name and a nested `items` list (57 parity)."""
    return ("id" in item and "name" in item and isinstance(item.get("items"), list))


def is_remux_name(name) -> bool:
    """True for any quality whose name mentions remux, in any position/case.

    Substring, not equality: Radarr ships Remux-1080p / Remux-2160p, Sonarr
    ships "Bluray-1080p Remux", and TRaSH templates add "Anime Remux-1080p".
    A hardcoded name list would rot; the word is the policy.
    """
    return bool(name) and "remux" in str(name).lower()


def disable_remux(items: list) -> int:
    """Walk items recursively, set allowed=false on every allowed Remux
    quality. Collapses a group once none of its children remain allowed.
    Mutates in place. Returns the number of LEAF qualities toggled (a group
    collapse is bookkeeping, not a policy change, so it is not counted)."""
    toggled = 0
    for item in items or []:
        if is_group(item):
            toggled += disable_remux(item.get("items") or [])
            any_allowed = any(
                bool(s.get("allowed"))
                for s in (item.get("items") or [])
            )
            if not any_allowed and item.get("allowed"):
                item["allowed"] = False
        else:
            q = item.get("quality") or {}
            name = q.get("name") if isinstance(q, dict) else None
            if is_remux_name(name) and item.get("allowed"):
                item["allowed"] = False
                toggled += 1
    return toggled


def collect_allowed_quality_ids(items: list) -> set:
    """Ids of allowed LEAF qualities only.

    This is the emptiness oracle. A group flag with no allowed child under it
    is not a grabbable quality, so counting group ids here would let us
    "leave one allowed" while actually leaving the profile unable to grab
    anything at all.
    """
    out: set = set()
    for item in items or []:
        if is_group(item):
            out |= collect_allowed_quality_ids(item.get("items") or [])
        else:
            if item.get("allowed"):
                q = item.get("quality") or {}
                qid = q.get("id") if isinstance(q, dict) else None
                if qid is not None:
                    out.add(qid)
    return out


def collect_allowed_ids(items: list) -> set:
    """Ids a cutoff may legally reference: allowed leaf qualities, plus group
    ids for groups that are allowed AND still have an allowed child."""
    out: set = set()
    for item in items or []:
        if is_group(item):
            kids = collect_allowed_quality_ids(item.get("items") or [])
            if item.get("allowed") and kids:
                gid = item.get("id")
                if gid is not None:
                    out.add(gid)
            out |= kids
        else:
            if item.get("allowed"):
                q = item.get("quality") or {}
                qid = q.get("id") if isinstance(q, dict) else None
                if qid is not None:
                    out.add(qid)
    return out


def highest_allowed_id(items: list):
    """Id of the best still-allowed TOP-LEVEL entry, or None if none remain.

    Radarr stores items[] worst-to-best, so "best" is positional: the LAST
    allowed top-level entry wins. Quality ids are NOT ordered by quality
    (Bluray-1080p is 7, WEBRip-1080p is 15), which is exactly why max(ids) is
    the wrong answer. A group contributes its OWN id, which is what Radarr
    expects a cutoff to reference for grouped qualities.
    """
    best = None
    for item in items or []:
        if is_group(item):
            if item.get("allowed") and collect_allowed_quality_ids(item.get("items") or []):
                best = item.get("id")
        else:
            if item.get("allowed"):
                q = item.get("quality") or {}
                qid = q.get("id") if isinstance(q, dict) else None
                if qid is not None:
                    best = qid
    return best


def fix_cutoff(profile: dict) -> bool:
    """Repair profile.cutoff if it now points at a disabled quality.
    Returns True iff the cutoff was actually changed."""
    items = profile.get("items") or []
    cutoff = profile.get("cutoff")
    if cutoff in collect_allowed_ids(items):
        return False
    new_id = highest_allowed_id(items)
    if new_id is None or new_id == cutoff:
        return False
    profile["cutoff"] = new_id
    return True


def apply_remux_cap(profile: dict) -> dict:
    """Cap ONE profile in place. The only entry point that may mutate.

    Refuses (restoring items[] exactly) rather than emptying a profile.
    Returns a report dict; `changed` is the flag the caller PUTs on.
    """
    report = {
        "profile_id": profile.get("id"),
        "profile_name": profile.get("name"),
        "toggled": 0,
        "changed": False,
        "refused": False,
        "cutoff_before": profile.get("cutoff"),
        "cutoff_after": profile.get("cutoff"),
        "cutoff_repaired": False,
        "reason": "",
    }
    items = profile.get("items") or []
    before = copy.deepcopy(items)

    toggled = disable_remux(items)
    if toggled == 0:
        report["reason"] = "no allowed Remux entries (already capped, or never had one)"
        return report

    if not collect_allowed_quality_ids(items):
        # Refuse. Restore the pre-walk tree so that a caller which ignores
        # `refused` still cannot PUT a stripped profile.
        profile["items"] = before
        report["refused"] = True
        report["reason"] = ("disabling Remux would leave ZERO allowed qualities - "
                            "refused, profile left untouched")
        return report

    report["toggled"] = toggled
    report["cutoff_repaired"] = fix_cutoff(profile)
    report["cutoff_after"] = profile.get("cutoff")
    report["changed"] = True
    return report


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------
def main() -> int:
    grand_total = 0
    profiles_changed = 0
    refusals = 0
    failures = 0

    for arr, api_v in ARRS.items():
        try:
            key = secret(arr + ".key")
            port = secret(arr + ".port")
        except FileNotFoundError as exc:
            print("[" + arr + "] skipped: " + str(exc))
            continue
        # urlbase files carry NO leading slash on this box.
        base = secret_or(arr + ".urlbase", arr).strip("/")
        url_base = ("http://127.0.0.1:" + port + "/" + base
                    + "/api/" + api_v + "/qualityprofile")

        try:
            req = urllib.request.Request(url_base, headers={"X-Api-Key": key})
            profiles = json.loads(urllib.request.urlopen(req, timeout=TIMEOUT).read())
        except Exception as exc:
            print("[" + arr + "] GET qualityprofile failed: " + str(exc), file=sys.stderr)
            failures += 1
            continue

        for p in profiles:
            rep = apply_remux_cap(p)
            pid = rep["profile_id"]
            pname = rep["profile_name"] or "?"

            if rep["refused"]:
                refusals += 1
                print("[" + arr + "] REFUSED profile '" + str(pname) + "' (id="
                      + str(pid) + "): " + rep["reason"], file=sys.stderr)
                continue
            if not rep["changed"]:
                continue

            try:
                req = urllib.request.Request(
                    url_base + "/" + str(pid),
                    data=json.dumps(p).encode("utf-8"),
                    headers={
                        "X-Api-Key": key,
                        "Content-Type": "application/json",
                    },
                    method="PUT",
                )
                urllib.request.urlopen(req, timeout=TIMEOUT).read()
                msg = ("[" + arr + "] disabled " + str(rep["toggled"])
                       + " Remux entr" + ("y" if rep["toggled"] == 1 else "ies")
                       + " on profile '" + str(pname) + "' (id=" + str(pid) + ")")
                if rep["cutoff_repaired"]:
                    msg += ("; cutoff REPAIRED " + str(rep["cutoff_before"])
                            + " -> " + str(rep["cutoff_after"]))
                else:
                    msg += "; cutoff " + str(rep["cutoff_after"]) + " unchanged"
                print(msg)
                grand_total += rep["toggled"]
                profiles_changed += 1
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:200]
                print("[" + arr + "] PUT profile '" + str(pname) + "' failed: "
                      + str(exc.code) + " " + body, file=sys.stderr)
                failures += 1
            except Exception as exc:
                print("[" + arr + "] PUT profile '" + str(pname) + "' failed: "
                      + str(exc), file=sys.stderr)
                failures += 1

    print()
    print("Scope: " + ", ".join(sorted(ARRS)) + " (anime instances excluded - see header)")
    print("Profiles modified: " + str(profiles_changed))
    print("Total Remux entries disabled: " + str(grand_total))
    if refusals:
        print("Profiles REFUSED (would have been left with zero qualities): " + str(refusals))
    if failures:
        print("Failures: " + str(failures))
    # A re-run on an already-capped stack prints 0 / 0 and exits 0. That IS the
    # idempotence check - there is no separate verify step to forget.
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
