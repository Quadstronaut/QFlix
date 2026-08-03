"""Tests for scripts/canaries/plex-unmatched.sh.

WHAT THIS GUARDS
On 2026-08-03 an audit found 30 Plex episodes stuck on a `local://` guid -- no
synopsis, no agent artwork, no air date -- and `git grep -nE "local://|
unmatched|fix.?match|guid" -- scripts/ tests/ manifest/` returned ZERO probes.
Nothing in the repo had ever looked at Plex match state. This canary closes
that gap, and these tests pin the three properties that make it worth having:

  1. A COUNT, with the affected sections named.
  2. A justified threshold -- items under the grace window are SUPPRESSED but
     COUNTED and PRINTED, never silently dropped.
  3. Three distinguishable exit codes: 0 asserted-and-clean, 1 asserted-and-
     found, 2 could-not-assert. A section that declares totalSize > 0 and
     returns an empty Metadata array must land on 2, not 0 -- otherwise
     empty-because-broken renders identically to empty-because-clean.

HOW
These EXECUTE the canary's embedded python against a real local HTTP server
serving fixture MediaContainers, rather than grepping the shell for strings.
That exercises the actual urllib calls, the actual pagination arithmetic, the
actual grace clock and the actual exit codes -- the same approach
tests/unit/test_hardlink_vacuity_clock.py takes with that canary's heredoc.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pytest

REPO = Path(__file__).resolve().parents[2]
CANARY = REPO / "scripts" / "canaries" / "plex-unmatched.sh"
SYSTEMD = REPO / "scripts" / "maint" / "systemd"

# Fixed clock so ages are exact rather than "roughly".
NOW = 1_800_000_000
HOUR = 3600


# --------------------------------------------------------------------------
# lifting the implementation out of the shell wrapper
# --------------------------------------------------------------------------

def _embedded_python() -> str:
    """Pinned deliberately: if the heredoc delimiter or style changes this
    raises, instead of silently testing an empty string and passing."""
    src = CANARY.read_text(encoding="utf-8")
    marker = 'python3 <<"PYEND"'
    start = src.index(marker) + len(marker)
    end = src.index("\nPYEND", start)
    body = src[start:end]
    assert "plex-unmatched-stuck" in body, "extracted the wrong block"
    assert "_broken" in body, "extracted the wrong block"
    return body


@pytest.fixture(scope="module")
def code():
    return _embedded_python()


# --------------------------------------------------------------------------
# a fake Plex
# --------------------------------------------------------------------------

def _ep(idx, *, guid, series="Squid Game", season=1, added=NOW - 480 * HOUR,
        summary="", title=None):
    item = {
        "ratingKey": str(7000 + idx),
        "type": "episode",
        "guid": guid,
        "grandparentTitle": series,
        "parentIndex": season,
        "index": idx,
        "title": title or ("Episode %d" % idx),
        "summary": summary,
        "thumb": "media://1/Contents/Thumbnails/thumb1.jpg",
    }
    if added is not None:
        item["addedAt"] = added
    return item


def _plex_guid(idx):
    return "plex://episode/5d9c0%03d" % idx


def _local_guid(idx):
    return "local://%d" % (7600 + idx)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):        # keep pytest output clean
        pass

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                     # noqa: N802 - stdlib API
        plan = self.server.plan
        parsed = urlparse(self.path)
        if parsed.path == "/library/sections":
            self._json({"MediaContainer": {"size": len(plan["sections"]),
                                           "Directory": plan["sections"]}})
            return
        parts = parsed.path.strip("/").split("/")
        # library/sections/<key>/all
        if len(parts) == 4 and parts[0] == "library" and parts[3] == "all":
            key = parts[2]
            handler = plan["listings"].get(key)
            if handler is None:
                self._json({"error": "no such section"}, code=404)
                return
            start = int(self.headers.get("X-Plex-Container-Start", "0"))
            size = int(self.headers.get("X-Plex-Container-Size", "100"))
            qs = parse_qs(parsed.query)
            plan.setdefault("seen_types", []).append(qs.get("type", [None])[0])
            plan.setdefault("pages", []).append((key, start, size))
            self._json({"MediaContainer": handler(start, size)})
            return
        self._json({"error": "unhandled"}, code=404)


def _paged(items):
    """Well-behaved Plex: honours container start/size, reports totalSize."""
    def _serve(start, size):
        window = items[start:start + size]
        return {"size": len(window), "totalSize": len(items),
                "Metadata": window}
    return _serve


def _lying_empty(declared):
    """The empty-because-BROKEN case: says it has N, hands back nothing."""
    def _serve(_start, _size):
        return {"size": 0, "totalSize": declared, "Metadata": []}
    return _serve


class _Plex:
    def __init__(self, plan):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.plan = plan
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)

    @property
    def base(self):
        host, port = self.server.server_address[:2]
        return "http://%s:%d" % (host, port)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _run(code, tmp_path, *, base, env=None):
    script = tmp_path / "plex_unmatched_body.py"
    script.write_text(code, encoding="utf-8")
    full = dict(os.environ)
    for key in list(full):
        if key.startswith("PLEX_"):
            del full[key]
    full.update({
        "PLEX_BASE": base,
        "PLEX_TOKEN": "fixture-token",
        "PLEX_UNMATCHED_NOW": str(NOW),
        "PLEX_UNMATCHED_TIMEOUT": "10",
    })
    full.update(env or {})
    return subprocess.run([sys.executable, str(script)], env=full,
                          capture_output=True, text=True, timeout=60)


def _plan(sections, listings):
    return {"sections": sections, "listings": listings}


TV = {"key": "2", "type": "show", "title": "QFlix - TV"}
ANIME = {"key": "6", "type": "show", "title": "QFlix - Anime"}
MOVIES = {"key": "4", "type": "movie", "title": "QFlix - Movies"}
KIDS = {"key": "5", "type": "movie", "title": "QFlix - Kids Movies"}


# --------------------------------------------------------------------------
# exit 0 -- asserted, and clean
# --------------------------------------------------------------------------

def test_all_matched_exits_zero_and_says_what_it_checked(code, tmp_path):
    items = [_ep(i, guid=_plex_guid(i)) for i in range(1, 10)]
    with _Plex(_plan([TV, MOVIES], {"2": _paged(items)})) as plex:
        r = _run(code, tmp_path, base=plex.base)
    assert r.returncode == 0, r.stderr
    assert "plex-unmatched-clean local=0 aged=0 under_grace=0" in r.stdout
    # A clean pass must still evidence that it looked at something.
    assert "episodes=9" in r.stdout
    assert "sections=1" in r.stdout
    assert r.stderr == ""


def test_movie_sections_are_skipped_and_the_skip_is_counted(code, tmp_path):
    """Rule 4: a suppression or skip is COUNTED and LOGGED, never silent."""
    items = [_ep(1, guid=_plex_guid(1))]
    plan = _plan([TV, MOVIES, KIDS], {"2": _paged(items)})
    with _Plex(plan) as plex:
        r = _run(code, tmp_path, base=plex.base, env={"PLEX_UNMATCHED_JSON": "1"})
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout)
    assert report["sections_scanned"] == ["QFlix - TV"]
    assert sorted(report["sections_skipped"]) == [
        "QFlix - Kids Movies(movie)", "QFlix - Movies(movie)"]
    # and the movie sections were never listed -- only section 2 was walked
    assert {k for k, _s, _z in plan["pages"]} == {"2"}


def test_sections_are_discovered_not_hardcoded(code, tmp_path):
    """A TV library nobody told the canary about must still be scanned."""
    new_lib = {"key": "11", "type": "show", "title": "QFlix - Docs"}
    stuck = [_ep(1, guid=_local_guid(1), series="Planet Earth")]
    plan = _plan([MOVIES, new_lib], {"11": _paged(stuck)})
    with _Plex(plan) as plex:
        r = _run(code, tmp_path, base=plex.base)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "QFlix - Docs:1" in r.stderr


def test_episode_type_filter_is_sent(code, tmp_path):
    plan = _plan([TV], {"2": _paged([])})
    with _Plex(plan) as plex:
        _run(code, tmp_path, base=plex.base)
    assert plan["seen_types"] == ["4"], (
        "must request type=4 (episodes); anything else changes what is counted")


# --------------------------------------------------------------------------
# exit 1 -- asserted, and found something
# --------------------------------------------------------------------------

def test_aged_local_guids_exit_one_with_count_and_sections(code, tmp_path):
    tv = ([_ep(i, guid=_local_guid(i), series="Squid Game") for i in range(1, 10)]
          + [_ep(i, guid=_plex_guid(i), series="Squid Game", season=2)
             for i in range(1, 8)])
    anime = [_ep(13, guid=_local_guid(99), series="Mob Psycho 100")]
    with _Plex(_plan([TV, ANIME, MOVIES],
                     {"2": _paged(tv), "6": _paged(anime)})) as plex:
        r = _run(code, tmp_path, base=plex.base)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STAGE=plex-unmatched-stuck" in r.stderr
    # 9 TV + 1 Anime, over a 17-episode denominator (a bare hit count with no
    # denominator cannot be triaged)
    assert "aged=10/17" in r.stderr
    assert "suppressed=0" in r.stderr
    assert "QFlix - TV:9" in r.stderr
    assert "QFlix - Anime:1" in r.stderr
    assert "Squid Game x9" in r.stderr            # names the affected series
    assert "Mob Psycho 100 x1" in r.stderr
    # stdout carries the triage view, one row per affected series
    assert "QFlix - Anime | Mob Psycho 100 | 1 episode(s)" in r.stdout


def test_a_single_stuck_episode_pages_no_percentage_floor(code, tmp_path):
    """1 stuck item in a 63-episode library is 1.6% -- a ratio threshold hides
    it, which is why the threshold is an absolute count."""
    items = ([_ep(1, guid=_local_guid(1), series="Mob Psycho 100")]
             + [_ep(i, guid=_plex_guid(i)) for i in range(2, 64)])
    with _Plex(_plan([ANIME], {"6": _paged(items)})) as plex:
        r = _run(code, tmp_path, base=plex.base)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "aged=1/63" in r.stderr


def test_max_aged_threshold_is_tunable(code, tmp_path):
    items = [_ep(i, guid=_local_guid(i)) for i in range(1, 4)]
    plan = _plan([TV], {"2": _paged(items)})
    with _Plex(plan) as plex:
        under = _run(code, tmp_path, base=plex.base,
                     env={"PLEX_UNMATCHED_MAX_AGED": "3"})
        over = _run(code, tmp_path, base=plex.base,
                    env={"PLEX_UNMATCHED_MAX_AGED": "2"})
    assert under.returncode == 0, under.stderr
    assert over.returncode == 1, over.stdout


def test_alert_line_fits_the_kuma_msg_budget_and_truncates_whole_elements(code, tmp_path):
    """cli.py does `result.stderr.strip()[:200]` for the Kuma msg=. A wide
    fan-out must therefore drop WHOLE series with a `+N more` marker, not slice
    an age in half -- the first draft emitted `... S1E2 5` where the age was
    `527.0h`, which reads as a different number rather than as truncation."""
    items = []
    for n in range(1, 13):
        items.append(_ep(n, guid=_local_guid(n),
                         series="A Very Long Series Title Number %02d" % n,
                         added=NOW - (500 - n) * HOUR))
    with _Plex(_plan([TV], {"2": _paged(items)})) as plex:
        r = _run(code, tmp_path, base=plex.base)
    assert r.returncode == 1, r.stdout
    line = r.stderr.strip()
    assert len(line) <= 200, "would be cut off inside Kuma: %d chars" % len(line)
    assert "aged=12/12" in line
    assert "more]" in line, "dropped series must be announced, not just absent"
    # nothing is half-printed: every rendered series entry keeps its "xN <age>h"
    rendered = line.split("series=[", 1)[1].rstrip("]\n")
    for chunk in rendered.split(";"):
        if chunk.endswith("more"):
            continue
        assert chunk.endswith("h"), "truncated mid-token: %r" % chunk
    # stdout still carries ALL of them -- the cap is a display budget, not a
    # silent drop of findings
    assert r.stdout.count("episode(s)") == 12


# --------------------------------------------------------------------------
# the grace window -- suppressed, but never silent
# --------------------------------------------------------------------------

def test_freshly_imported_local_item_is_suppressed_but_counted(code, tmp_path):
    """A brand-new import is legitimately local:// for a few minutes. It must
    NOT page -- and it must NOT vanish either."""
    items = [_ep(1, guid=_local_guid(1), added=NOW - 1 * HOUR)]
    with _Plex(_plan([TV], {"2": _paged(items)})) as plex:
        r = _run(code, tmp_path, base=plex.base)
    assert r.returncode == 0, r.stderr
    assert "local=1" in r.stdout
    assert "aged=0" in r.stdout
    assert "under_grace=1" in r.stdout, (
        "an invisible suppression becomes permanent by accident: " + r.stdout)


def test_grace_boundary_is_the_configured_hours(code, tmp_path):
    just_under = [_ep(1, guid=_local_guid(1), added=NOW - 5 * HOUR)]
    just_over = [_ep(1, guid=_local_guid(1), added=NOW - 7 * HOUR)]
    with _Plex(_plan([TV], {"2": _paged(just_under)})) as plex:
        a = _run(code, tmp_path, base=plex.base,
                 env={"PLEX_UNMATCHED_GRACE_HOURS": "6"})
    with _Plex(_plan([TV], {"2": _paged(just_over)})) as plex:
        b = _run(code, tmp_path, base=plex.base,
                 env={"PLEX_UNMATCHED_GRACE_HOURS": "6"})
    assert a.returncode == 0, a.stderr
    assert b.returncode == 1, b.stdout


def test_mixed_aged_and_fresh_reports_both_numbers(code, tmp_path):
    items = [_ep(1, guid=_local_guid(1), added=NOW - 500 * HOUR),
             _ep(2, guid=_local_guid(2), added=NOW - 1 * HOUR),
             _ep(3, guid=_plex_guid(3))]
    with _Plex(_plan([TV], {"2": _paged(items)})) as plex:
        r = _run(code, tmp_path, base=plex.base)
    assert r.returncode == 1, r.stdout
    assert "aged=1/3-suppressed=1" in r.stderr


def test_item_with_no_added_timestamp_counts_as_aged(code, tmp_path):
    """A grace window that cannot age an item must not swallow it. Fail loud,
    and record ageHours=null so the operator can see why."""
    items = [_ep(1, guid=_local_guid(1), added=None)]
    with _Plex(_plan([TV], {"2": _paged(items)})) as plex:
        r = _run(code, tmp_path, base=plex.base, env={"PLEX_UNMATCHED_JSON": "1"})
    assert r.returncode == 1, r.stdout
    report = json.loads(r.stdout)
    assert report["aged"] == 1
    assert report["items"][0]["ageHours"] is None


# --------------------------------------------------------------------------
# exit 2 -- could not assert anything
# --------------------------------------------------------------------------

def test_plex_unreachable_exits_two_not_zero(code, tmp_path):
    with _Plex(_plan([TV], {"2": _paged([])})) as plex:
        dead = plex.base                       # port is released on __exit__
    r = _run(code, tmp_path, base=dead)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "STAGE=plex-sections-unreachable" in r.stderr
    assert dead not in r.stderr, "the base URL carries host:port -- keep it out of Kuma msg="


def test_missing_config_exits_two_never_zero(code, tmp_path):
    r = _run(code, tmp_path, base="", env={"PLEX_TOKEN": ""})
    assert r.returncode == 2, r.stdout + r.stderr
    assert "STAGE=plex-unmatched-config-missing" in r.stderr
    assert "base=EMPTY" in r.stderr and "token=EMPTY" in r.stderr


def test_zero_show_sections_is_broken_not_clean(code, tmp_path):
    """Plex answering with only movie libraries means the TV libraries are gone
    or the query changed shape. Exiting 0 there is a guard that stopped
    guarding while staying green."""
    with _Plex(_plan([MOVIES, KIDS], {})) as plex:
        r = _run(code, tmp_path, base=plex.base)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "STAGE=plex-no-show-sections" in r.stderr
    assert "directories=2" in r.stderr


def test_section_declaring_items_but_returning_none_is_broken(code, tmp_path):
    """THE headline exit-code case: totalSize=382, Metadata=[]. Zero local://
    hits, but for the wrong reason."""
    with _Plex(_plan([TV], {"2": _lying_empty(382)})) as plex:
        r = _run(code, tmp_path, base=plex.base)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "STAGE=plex-section-truncated" in r.stderr
    assert "declared=382-received=0" in r.stderr


def test_genuinely_empty_section_is_clean_not_broken(code, tmp_path):
    """The mirror image: totalSize=0 and Metadata=[] is an empty library, which
    is a content state, not a fault."""
    with _Plex(_plan([TV], {"2": _paged([])})) as plex:
        r = _run(code, tmp_path, base=plex.base)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "episodes=0" in r.stdout


def test_section_http_error_exits_two(code, tmp_path):
    with _Plex(_plan([TV], {})) as plex:       # no listing registered -> 404
        r = _run(code, tmp_path, base=plex.base)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "STAGE=plex-section-unreachable" in r.stderr
    assert "http=404" in r.stderr


# --------------------------------------------------------------------------
# pagination -- a silent page cap would make the canary read the first N
# episodes and call the rest clean
# --------------------------------------------------------------------------

def test_every_page_is_walked_and_a_late_stuck_item_is_found(code, tmp_path):
    items = [_ep(i, guid=_plex_guid(i)) for i in range(1, 250)]
    items.append(_ep(250, guid=_local_guid(250), series="Monster (2022)"))
    plan = _plan([TV], {"2": _paged(items)})
    with _Plex(plan) as plex:
        r = _run(code, tmp_path, base=plex.base,
                 env={"PLEX_UNMATCHED_PAGE_SIZE": "100"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "aged=1/250" in r.stderr
    assert "Monster (2022)" in r.stderr
    assert [p[1] for p in plan["pages"]] == [0, 100, 200], plan["pages"]


# --------------------------------------------------------------------------
# --json contract for the dashboard / newsletter
# --------------------------------------------------------------------------

def test_json_report_names_series_season_episode_and_age(code, tmp_path):
    items = [_ep(5, guid=_local_guid(5), series="Monster (2022)", season=1,
                 added=NOW - 479 * HOUR)]
    with _Plex(_plan([TV, MOVIES], {"2": _paged(items)})) as plex:
        r = _run(code, tmp_path, base=plex.base, env={"PLEX_UNMATCHED_JSON": "1"})
    assert r.returncode == 1, r.stderr
    report = json.loads(r.stdout)
    assert report["canary"] == "plex-unmatched"
    assert report["status"] == "stuck"
    assert report["aged"] == 1
    assert report["under_grace"] == 0
    assert report["affected_sections"] == ["QFlix - TV"]
    item = report["items"][0]
    assert item["series"] == "Monster (2022)"
    assert item["season"] == 1
    assert item["episode"] == 5
    assert item["ageHours"] == 479.0
    assert item["guid"].startswith("local://")
    assert item["hasSummary"] is False
    assert report["per_section"][0]["episodes"] == 1


def test_json_clean_run_still_reports_the_denominators(code, tmp_path):
    items = [_ep(i, guid=_plex_guid(i)) for i in range(1, 6)]
    with _Plex(_plan([TV], {"2": _paged(items)})) as plex:
        r = _run(code, tmp_path, base=plex.base, env={"PLEX_UNMATCHED_JSON": "1"})
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout)
    assert report["status"] == "clean"
    assert report["episodes"] == 5
    assert report["items"] == []
    assert report["grace_hours"] == 6.0


# --------------------------------------------------------------------------
# the shell wrapper and its units
# --------------------------------------------------------------------------

def test_wrapper_preserves_the_remote_exit_code():
    """`RES=$(sshm ...) || RC=$?` collapses 1 and 2 into 1. This canary's whole
    point is that those two mean different things, so the wrapper must capture
    $? unconditionally and exit with it."""
    src = CANARY.read_text(encoding="utf-8")
    assert "RES=$(sshm " in src
    assert "\nRC=$?\n" in src
    assert "\nexit $RC\n" in src


def test_missing_secret_never_exits_zero():
    """C-09 silent-exit-on-missing-prerequisite: the credential guard must exit
    2, not 0. A canary that cannot find its secret and exits clean shows Kuma a
    green push it never earned."""
    src = CANARY.read_text(encoding="utf-8")
    guard = src[src.index('if [ -z "$PLEX_HOST" ]'):]
    guard = guard[:guard.index("export PLEX_BASE")]
    assert "plex-unmatched-config-missing" in guard
    assert "exit 2" in guard
    assert "exit 0" not in guard


def test_header_documents_all_three_exit_codes():
    header = CANARY.read_text(encoding="utf-8")[:9000]
    assert "0 —" in header and "1 —" in header and "2 —" in header
    for stage in ("plex-unmatched-config-missing", "plex-sections-unreachable",
                  "plex-no-show-sections", "plex-section-unreachable",
                  "plex-section-truncated", "plex-unmatched-stuck"):
        assert stage in header, "undocumented stage label: " + stage


def test_no_hardcoded_plex_endpoint_or_section_ids():
    """Credentials come from ~/secrets/plex.{host,port,token} like every other
    Plex consumer, and the section list is discovered."""
    src = CANARY.read_text(encoding="utf-8")
    body = src[src.index("REMOTE=$(cat"):]
    for secret in ("plex.host", "plex.port", "plex.token"):
        assert secret in body
    assert "/library/sections" in body
    # no literal ip:port and no `sections/2` style hardcoding in the live path
    assert "172.17." not in body
    assert "32400" not in body
    assert "/library/sections/2/" not in body


def test_wrapper_is_valid_bash():
    if shutil.which("bash") is None:
        pytest.skip("bash not on PATH")
    r = subprocess.run(["bash", "-n", str(CANARY)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_wrapper_carries_the_json_flag_into_the_remote_body():
    """Env does not propagate over ssh, so --json has to travel as text. If
    this regresses, `--json` silently degrades to the one-line summary and the
    dashboard consumer gets an unparseable string."""
    src = CANARY.read_text(encoding="utf-8")
    assert "export PLEX_UNMATCHED_JSON=${EMIT_JSON}" in src
    assert "--json) EMIT_JSON=1" in src


# --- the shell half, executed for real ------------------------------------
# The wrapper itself goes through sshm and cannot run here, but the REMOTE body
# is the part that reads the secrets and launches python. Running it against a
# fixture Plex proves the credential plumbing works rather than asserting it
# from a grep.

def _remote_body() -> str:
    src = CANARY.read_text(encoding="utf-8")
    marker = "REMOTE=$(cat <<'REMOTE_EOF'\n"
    start = src.index(marker) + len(marker)
    end = src.index("\nREMOTE_EOF", start)
    body = src[start:end]
    assert "plex.token" in body, "extracted the wrong block"
    return body


def _run_remote(tmp_path, *, secrets, env=None):
    home = tmp_path / "home"
    sec = home / "secrets"
    sec.mkdir(parents=True)
    for name, value in secrets.items():
        (sec / name).write_text(value, encoding="utf-8")
    script = tmp_path / "remote_body.sh"
    script.write_text(_remote_body(), encoding="utf-8")
    full = dict(os.environ)
    for key in list(full):
        if key.startswith("PLEX_"):
            del full[key]
    full["HOME"] = str(home)
    full["USERPROFILE"] = str(home)
    full["PLEX_UNMATCHED_NOW"] = str(NOW)
    full["PLEX_UNMATCHED_TIMEOUT"] = "10"
    full.update(env or {})
    return subprocess.run(["bash", str(script)], env=full,
                          capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("python3") is None,
                    reason="the remote body needs bash + python3 on PATH")
def test_remote_body_reads_plex_secrets_and_finds_the_stuck_items(tmp_path):
    items = [_ep(1, guid=_local_guid(1), series="What We Do in the Shadows"),
             _ep(2, guid=_plex_guid(2))]
    with _Plex(_plan([TV, MOVIES], {"2": _paged(items)})) as plex:
        host, port = plex.server.server_address[:2]
        r = _run_remote(tmp_path, secrets={"plex.host": str(host),
                                           "plex.port": str(port),
                                           "plex.token": "fixture-token"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STAGE=plex-unmatched-stuck" in r.stderr
    assert "aged=1/2" in r.stderr
    assert "What We Do in the Shadows x1" in r.stderr


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("python3") is None,
                    reason="the remote body needs bash + python3 on PATH")
def test_remote_body_missing_secret_exits_two(tmp_path):
    with _Plex(_plan([TV], {"2": _paged([])})) as plex:
        host, port = plex.server.server_address[:2]
        r = _run_remote(tmp_path, secrets={"plex.host": str(host),
                                           "plex.port": str(port)})
    assert r.returncode == 2, r.stdout + r.stderr
    assert "STAGE=plex-unmatched-config-missing" in r.stderr
    assert "token=" in r.stderr


def test_systemd_unit_pair_exists_and_names_the_canary():
    service = SYSTEMD / "manitoba-maint-canary-plex-unmatched.service"
    timer = SYSTEMD / "manitoba-maint-canary-plex-unmatched.timer"
    assert service.is_file(), "canary with no .service is not scheduled"
    assert timer.is_file(), "canary with no .timer is not scheduled"
    body = service.read_text(encoding="utf-8")
    assert "canary push plex-unmatched" in body, (
        "ExecStart must name the canary by its MANIFEST key -- a unit that runs "
        "the wrong name pushes to the wrong Kuma monitor, or none")
    assert "Type=oneshot" in body
    timer_body = timer.read_text(encoding="utf-8")
    assert "OnCalendar=" in timer_body
    assert "WantedBy=timers.target" in timer_body
