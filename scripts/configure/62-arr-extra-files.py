#!/usr/bin/env python3
"""Close the upstream door that puts release-group ARTWORK next to the video:
*arr extra-file import. Idempotent - safe to re-run. Sibling of
57-no-4k-enforce.py, 58-remux-cap-enforce.py and 59-brdisk-block.py, and
deliberately built to the same shape (same secrets loading, same urlbase
handling, same GET -> mutate -> PUT -> RE-READ, same print-what-changed,
applies directly, no argparse).

WHY THIS EXISTS - the concrete failure it prevents
--------------------------------------------------
Four movies in "QFlix - Movies" render in Plex with release-group / scene
branding instead of a real poster. Measured on the box 2026-08-24:

  * Each has an image sidecar named like the video file, e.g.
    "Evil Dead Burn (2026) WEBRip-1080p.jpg". Exactly FOUR such files exist
    under every media root (Movies, TV Shows, Anime, Anime Movies, Welcome) -
    1:1 with the four bad Plex items. No hidden backlog, and no coincidence.
  * The poster Plex selected IS that file, byte for byte. md5 of the selected
    poster fetched from Plex == md5 on disk for all four. Two of them
    (Evil Dead Burn, Young Washington) are the SAME 38690-byte image: one
    generic release-group graphic reused across releases.
  * radarr.db ExtraFiles says where they came from: `.jpg|4`, `.nfo|10`.
    Radarr imported them.
  * The Plex-side lever does NOT hold this door shut. useLocalAssets was
    already "0" on all four libraries in the dated Plex DB backups of
    2026-08-14/17/20/23, yet Sing (added 08-16) and A Minecraft Movie (added
    08-20) both still came up on their sidecar. Proven inert, not remembered.

So the file must not arrive. That is this script.

THE DOOR, live before this ran
------------------------------
  radarr   :17027/radarr    importExtraFiles=True   ext='srt,sub,subtitles,nfo,txt,jpg,jpeg'
  radarr2  :17008/radarr2   importExtraFiles=False  ext='srt'
  sonarr   :17026/sonarr    importExtraFiles=True   ext='srt,sub,nfo'
  sonarr2  :17003/sonarr2   importExtraFiles=False  ext='srt'

Only radarr main carries an image extension. sonarr's boolean is True but its
list has no image extension, so it is not the poster door.

THE POLICY, and why it is TWO levers and not one
------------------------------------------------
1. FORBIDDEN_EXTRA_EXTENSIONS is scrubbed from extraFileExtensions on ALL FOUR
   instances. The extension list is a denylist-by-omission: "there is no jpg in
   the list" is one UI click, one factory-default restore on a major *arr
   upgrade, or one well-meaning "let us also grab png artwork" away from being
   false again. Scrubbing everywhere means a re-run repairs whichever instance
   grew one, including the three that are clean today.

2. importExtraFiles is set False where extra-file import buys nothing. That is
   the invariant the list cannot express: a boolean cannot be widened by adding
   a value to it.

MEASURED VALUE OF RADARR'S EXTRA-FILE IMPORT: ZERO. Its entire lifetime output
is 4 poster jpgs (this defect), 10 .nfo (inert - the Plex agent is
tv.plex.agents.movie, which does not read nfo, and all five radarr metadata
consumers are enable=False), and exactly ONE plain .srt: "Young Washington
(2026) WEBRip-1080p.srt" at 1608 bytes. A real 1080p movie subtitle on this box
is 55-104 KB (the Bazarr-written `.en.srt` files). 1.6 KB is a promo stub.
Bazarr covers movies independently and has 0 wanted movies. Nothing a member
sees is lost.

SONARR MAIN IS DELIBERATELY LEFT ARMED (import_extra=None = assert only).
TV is the opposite case and the measurement says so: 11 plain
`Debris - S01Exx ... .srt` files at 28-44 KB each are REAL subtitles that
sonarr imported, and Bazarr still has 19 WANTED EPISODES - i.e. Bazarr is not
covering TV completely, so the release-subtitle fallback is load-bearing there.
Sonarr keeps importExtraFiles=True and only has image extensions scrubbed. Do
not "harmonise" it to False; that is a member-visible subtitle regression.

SUBTITLES CANNOT BE DROPPED BY THE SCRUB, BY CONSTRUCTION. scrub_extensions()
is a DENYLIST REMOVAL, not a whitelist rewrite: it copies every token through
untouched unless that token is in FORBIDDEN_EXTRA_EXTENSIONS, and no subtitle
extension is in that tuple. There is no per-instance "target list" string to
get wrong. tests/unit/test_arr_extra_files.py pins exactly that.

NOT A REVERT RISK: buildarr will not fight this, verified three ways.
  (a) ~/.apps/buildarr/buildarr.yml declares ONLY `buildarr:` plus sonarr and
      radarr `instances:` blocks. There is no `settings:` key anywhere, so
      media_management is unmanaged.
  (b) buildarr-radarr / buildarr-sonarr do not model extraFileExtensions at
      all - grep over both plugin trees in the venv returns zero lines.
  (c) Behavioural proof: buildarr-radarr's DEFAULT is
      import_extra_files=False and that attribute IS in its remote-map, while
      radarr main was live at True and buildarr's 04:30 run still logged
      "Remote configuration is up to date". buildarr-core gates every write on
      `attr_name in self.__fields_set__` - unset means untouched.
  If buildarr.yml ever grows a `settings:` block, re-verify all three.

WHAT THIS SCRIPT DOES NOT DO
----------------------------
  * It does not delete the four sidecars already on disk. Sidecars do not
    self-clean: "In the Mouth of Madness (1995) BR-DISK.en.srt" is still there
    under the OLD release name after the video was replaced with a
    Bluray-1080p.mkv. The operator deletes those four jpgs by explicit path
    (never a glob near a media tree), then re-runs the Plex poster flip.
  * It does not close the EMBEDDED-artwork door. A Minecraft Movie also
    carries mjpeg cover art in stream 0:34; importExtraFiles governs FILES, not
    streams. That class needs the Plex-side poster janitor, which is a separate
    concern with a separate cadence and a separate Kuma monitor (operator
    design law: compartmentalize for migration).

Run on the seedbox (reads ~/secrets/<arr>.{key,port,urlbase}). Or pipe via
SSH:  sshm "python3 -" < scripts/configure/62-arr-extra-files.py
Self-contained by design (no lib.* imports) precisely so that pipe works.

NOTE: the urlbase prefix is MANDATORY on this box. /api/v3/... returns 307;
/<urlbase>/api/v3/... returns 200.

EXIT: 0 clean, 1 if any GET/PUT failed, if any write did not survive its
re-read, or if EVERY instance was skipped (a run that touched nothing must
never read as an armed stack).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


# THE POLICY, stated once. Change THIS, not the *arr API by hand.
#
# Release groups ship a promo image named like the video file. *arr imports it
# as an "extra file", it lands beside the .mkv, and Plex selects it as the
# poster - useLocalAssets=false does not stop that (proven above). No image
# extension has ever had a legitimate job in an *arr extra-file import on this
# stack, so the whole class is denied rather than the two extensions that
# happened to bite. `tbn` is the classic Kodi thumbnail extension and belongs
# here for the same reason even though nothing writes one today.
#
# NOTHING SUBTITLE-SHAPED MAY EVER BE ADDED TO THIS TUPLE. Members use
# subtitles; sonarr's imported .srt files are the only ones some episodes have.
FORBIDDEN_EXTRA_EXTENSIONS = ("jpg", "jpeg", "png", "webp", "bmp", "gif", "tbn")

# Per-instance extra-file import switch.
#   False -> write importExtraFiles=False (extra-file import buys nothing here)
#   None  -> ASSERT ONLY, never write (sonarr main: its imported .srt are real
#            subtitles and Bazarr does not fully cover TV - see header)
# The extension scrub above applies to every instance regardless.
INSTANCES = {
    "radarr":  {"api": "v3", "import_extra": False},
    "radarr2": {"api": "v3", "import_extra": False},
    "sonarr":  {"api": "v3", "import_extra": None},
    "sonarr2": {"api": "v3", "import_extra": False},
}

TIMEOUT = 15


def discover_instances(secrets_dir=None) -> list:
    """Every *arr instance that actually EXISTS on this box, from the secrets dir.

    WHY THIS EXISTS. INSTANCES above is a hand-edited constant, and a constant
    that has to be remembered when the world changes is a latent gap with a
    countdown on it. Add a radarr3 tomorrow and it imports release artwork
    forever, silently, because nothing here would ever look at it -- the exact
    shape of the defect this whole file exists to close, one layer up.

    The secrets directory is the box's own answer to "which *arr instances are
    there": an instance is not reachable without <slug>.key AND <slug>.port, and
    every install script writes both. So discovery is a glob, not a guess.

    Returns sorted slugs. The caller compares this against INSTANCES and treats
    anything unknown as a NAMED FAILURE, never a silent skip.
    """
    import glob
    base = secrets_dir or os.path.expanduser("~/secrets")
    found = []
    for key_path in glob.glob(os.path.join(base, "*.key")):
        slug = os.path.basename(key_path)[:-4]
        if not (slug.startswith("radarr") or slug.startswith("sonarr")):
            continue
        if not os.path.exists(os.path.join(base, slug + ".port")):
            continue
        found.append(slug)
    return sorted(found)


def unknown_instances(secrets_dir=None) -> list:
    """Discovered *arr instances this file has no policy for. Must never be []
    by assumption -- see discover_instances."""
    return [s for s in discover_instances(secrets_dir) if s not in INSTANCES]


def secret(name: str) -> str:
    with open(os.path.expanduser("~/secrets/" + name)) as f:
        return f.read().strip()


def secret_or(name: str, fallback: str) -> str:
    try:
        return secret(name)
    except FileNotFoundError:
        return fallback


# ---------------------------------------------------------------------------
# Pure policy logic. Everything in this block is offline and side-effect-free
# apart from the in-place mutation it advertises, so
# tests/unit/test_arr_extra_files.py can drive it with no box and no network.
# ---------------------------------------------------------------------------
def normalise_ext(token: str) -> str:
    """Compare-form of one extension token: no dot, no whitespace, lowercase.

    *arr stores bare lowercase ("jpg"), but the UI accepts ".JPG" and a
    hand-edit can leave spaces. Compare on the normalised form so a
    hand-typed ".JPG" is still recognised as the forbidden thing.
    """
    return str(token).strip().lstrip(".").lower()


def parse_extensions(raw) -> list:
    """Split extraFileExtensions into tokens, dropping empties. Order and the
    original spelling of each kept token are preserved - this function must
    never be the thing that rewrites a list cosmetically."""
    if not raw:
        return []
    return [t.strip() for t in str(raw).split(",") if t.strip()]


def is_forbidden_ext(token: str) -> bool:
    return normalise_ext(token) in FORBIDDEN_EXTRA_EXTENSIONS


def scrub_extensions(raw) -> tuple:
    """DENYLIST REMOVAL. Returns (new_csv, removed_tokens).

    Every token that is not forbidden is copied through verbatim. That is what
    makes it structurally impossible for this script to drop a subtitle
    extension: there is no target list to mistype, only a set to subtract.
    """
    kept: list = []
    removed: list = []
    for tok in parse_extensions(raw):
        if is_forbidden_ext(tok):
            removed.append(tok)
        else:
            kept.append(tok)
    return (",".join(kept), removed)


def apply_extra_file_policy(cfg: dict, target_import_extra) -> dict:
    """Mutate a config/mediamanagement payload in place. Returns a report.

    `changed` False means the instance is ALREADY CORRECT and no PUT is owed -
    that is the whole idempotence contract, and a re-run proves it by printing
    NO-OP lines and nothing else.
    """
    before_ext = cfg.get("extraFileExtensions")
    before_imp = cfg.get("importExtraFiles")
    new_ext, removed = scrub_extensions(before_ext)

    report = {
        "before_ext": before_ext,
        "after_ext": before_ext,
        "removed": removed,
        "before_import": before_imp,
        "after_import": before_imp,
        "changed": False,
        "reasons": [],
    }

    if removed:
        cfg["extraFileExtensions"] = new_ext
        report["after_ext"] = new_ext
        report["changed"] = True
        report["reasons"].append("dropped image extension(s) "
                                 + ",".join(removed))

    if target_import_extra is None:
        report["reasons"].append("importExtraFiles left at "
                                 + str(before_imp) + " by policy")
    elif bool(before_imp) != bool(target_import_extra):
        cfg["importExtraFiles"] = bool(target_import_extra)
        report["after_import"] = bool(target_import_extra)
        report["changed"] = True
        report["reasons"].append("importExtraFiles " + str(before_imp)
                                 + " -> " + str(bool(target_import_extra)))

    if not report["changed"]:
        report["reasons"].insert(0, "already correct")
    return report


def verify_after(cfg: dict, target_import_extra) -> str:
    """Grade a RE-READ payload. Returns "" if the policy held, else why not."""
    _, still = scrub_extensions(cfg.get("extraFileExtensions"))
    if still:
        return ("extraFileExtensions still carries " + ",".join(still))
    if target_import_extra is not None:
        if bool(cfg.get("importExtraFiles")) != bool(target_import_extra):
            return ("importExtraFiles re-read as "
                    + str(cfg.get("importExtraFiles")) + ", wanted "
                    + str(bool(target_import_extra)))
    return ""


# ---------------------------------------------------------------------------
# Box-facing half.
# ---------------------------------------------------------------------------
def _req(url: str, key: str, data=None, method="GET"):
    headers = {"X-Api-Key": key}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    return urllib.request.urlopen(req, timeout=TIMEOUT).read()


def _get(url: str, key: str):
    return json.loads(_req(url, key))


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def enforce_instance(base_url: str, key: str, arr: str,
                     target_import_extra) -> int:
    """Returns a failure count (0 or 1)."""
    url = base_url + "/config/mediamanagement"
    try:
        cfg = _get(url, key)
    except Exception as exc:
        _err("[" + arr + "] GET config/mediamanagement failed: " + str(exc))
        return 1

    rep = apply_extra_file_policy(cfg, target_import_extra)

    if not rep["changed"]:
        print("[" + arr + "] NO-OP: importExtraFiles="
              + str(rep["before_import"]) + " extraFileExtensions="
              + repr(rep["before_ext"]) + " (" + "; ".join(rep["reasons"])
              + ")")
        return 0

    try:
        _req(url + "/" + str(cfg.get("id", 1)), key, data=cfg, method="PUT")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:200]
        _err("[" + arr + "] PUT config/mediamanagement failed: "
             + str(exc.code) + " " + body)
        return 1
    except Exception as exc:
        _err("[" + arr + "] PUT config/mediamanagement failed: " + str(exc))
        return 1

    # RE-READ. A 200 from an *arr is not evidence the value stuck.
    try:
        after = _get(url, key)
    except Exception as exc:
        _err("[" + arr + "] re-read config/mediamanagement failed: "
             + str(exc))
        return 1
    why = verify_after(after, target_import_extra)
    if why:
        _err("[" + arr + "] policy did NOT stick: " + why)
        return 1

    print("[" + arr + "] ARMED: " + "; ".join(rep["reasons"])
          + " | extraFileExtensions " + repr(rep["before_ext"]) + " -> "
          + repr(after.get("extraFileExtensions"))
          + " | importExtraFiles " + str(rep["before_import"]) + " -> "
          + str(after.get("importExtraFiles")))
    return 0


def main() -> int:
    failures = 0
    skipped = 0

    for arr, policy in INSTANCES.items():
        try:
            key = secret(arr + ".key")
            port = secret(arr + ".port")
        except FileNotFoundError as exc:
            print("[" + arr + "] skipped: " + str(exc))
            skipped += 1
            continue
        # urlbase files carry NO leading slash on this box, and the prefix is
        # mandatory - /api/v3/... answers 307, /<urlbase>/api/v3/... answers 200.
        base = secret_or(arr + ".urlbase", arr).strip("/")
        base_url = ("http://127.0.0.1:" + port + "/" + base
                    + "/api/" + policy["api"])
        failures += enforce_instance(base_url, key, arr,
                                     policy["import_extra"])

    print()
    print("Policy: no " + ",".join(FORBIDDEN_EXTRA_EXTENSIONS)
          + " in extraFileExtensions on any instance; importExtraFiles False "
            "except sonarr main (assert-only - see header)")
    print("Scope: " + ", ".join(sorted(INSTANCES)))
    if skipped:
        print("Instances skipped (no secrets): " + str(skipped))
    # A NEW *arr instance is a FAILURE here, not a shrug. It exists on the box,
    # it will import whatever its defaults say, and nothing else in the repo
    # would ever mention it. Failing loudly is the only way this constant stays
    # true; a silent skip is how the original defect got three months of runway.
    unknown = unknown_instances()
    if unknown:
        _err("*arr instance(s) present on this box with NO extra-file policy: "
             + ", ".join(unknown)
             + " -- add them to INSTANCES in this file (they are importing "
               "release artwork with vendor defaults until you do)")
        failures += len(unknown)
    if failures:
        print("Failures: " + str(failures))
    # A re-run on an already-armed stack prints only NO-OP lines and exits 0.
    # That IS the idempotence check - there is no separate verify step to
    # forget. A run that skipped EVERY instance touched nothing and must not be
    # mistaken for one.
    if skipped == len(INSTANCES):
        _err("every instance was skipped - nothing was enforced")
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
