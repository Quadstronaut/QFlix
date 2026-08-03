"""Tests for scripts/canaries/prowlarr-app-sync.sh.

There is no shell-lint gate anywhere in this repo (.github/workflows/tests.yml
runs pytest and nothing else), so a pytest test that actually runs
`bash <script>` is the only real gate on a canary's correctness. Everything
below therefore drives the shipped artifact end to end: real bash, real python3,
real HTTP over loopback, real secret files on disk.

The fixture is the LIVE 2026-08-03 topology, read-only from the box and
sanitised (no host, no port, no key): 4 applications (Radarr/Sonarr on tag
`general`, Radarr2/Sonarr2 on tag `anime`), 12 enabled torrent indexers + 1
usenet + 1 disabled, the real syncCategories, and the real per-*arr indexer
lists. On that fixture exactly one thing is wrong -- LimeTorrents is absent from
Radarr -- which is the fault that ran for ~10 weeks with every monitor green.

Three jobs:

  1. MUTATION VERIFICATION. Every failing fixture is constructed so that
     `_health_probe_verdict()` -- what prowlarr-indexer-health.sh Probe 2
     actually does, read Prowlarr /api/v1/health and count indexer-ish warnings
     -- returns GREEN while this canary returns RED. That is the un-fixed
     behaviour, shown passing, so "strictly stronger" is proven rather than
     asserted.

  2. THE COUNTED-SKIP CONTRACT (rule 4). Six indexers are legitimately not
     synced to some app. They must appear in a named bucket, never vanish.

  3. THE EXIT-CODE TRICHOTOMY (rule 5). 0 clean / 1 finding / 2 broken, with a
     negative control for each broken leg -- including the one that matters
     most, zero applications, where "nothing is missing" is trivially true and
     completely wrong.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "canaries" / "prowlarr-app-sync.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="prowlarr-app-sync.sh needs bash + python3 on PATH",
)

PROW_KEY = "prowlarr-test-key"
ARR_KEYS = {"radarr": "rk", "radarr2": "r2k", "sonarr": "sk", "sonarr2": "s2k"}


# ---------------------------------------------------------------------------
# The un-fixed behaviour, implemented so it can be shown to pass
# ---------------------------------------------------------------------------


def _health_probe_verdict(health_items):
    """prowlarr-indexer-health.sh Probe 2, restated (script lines ~110-135):
    count warning/error health items mentioning "indexer", fire at >= 2.
    GREEN means "the canary that already exists would have said this is fine"."""
    flagged = [h for h in health_items or []
               if (h.get("type") or "").lower() in ("warning", "error")
               and ("indexer" in (h.get("message") or "").lower()
                    or "indexer" in (h.get("source") or "").lower())]
    return len(flagged) < 2


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def cat(cid, name="c", subs=None):
    return {"id": cid, "name": name, "subCategories": subs or []}


def indexer(iid, name, tags, cats, *, enable=True, protocol="torrent",
            magnet=False):
    fields = [{"name": "baseUrl", "value": "https://example.invalid/"}]
    if protocol == "torrent":
        fields.append({"name": "torrentBaseSettings.preferMagnetUrl",
                       "value": magnet})
    return {"id": iid, "name": name, "enable": enable, "protocol": protocol,
            "tags": tags, "fields": fields,
            "capabilities": {"categories": cats}}


def application(aid, name, impl, tags, slug, sync_cats, anime_cats=None, *,
                base="", sync_level="fullSync"):
    fields = [
        {"name": "prowlarrUrl", "value": "http://prowlarr.invalid"},
        {"name": "baseUrl", "value": "%s/%s" % (base, slug)},
        # Prowlarr MASKS secret fields on read -- measured live, the apiKey it
        # hands back is an 8-char placeholder. The canary must ignore it and
        # read ~/secrets/<slug>.key instead, so the fixture lies the same way.
        {"name": "apiKey", "value": "********"},
        {"name": "syncCategories", "value": sync_cats},
    ]
    if anime_cats is not None:
        fields.append({"name": "animeSyncCategories", "value": anime_cats})
    return {"id": aid, "name": name, "implementation": impl,
            "syncLevel": sync_level, "tags": tags, "fields": fields}


def arr_entry(name, prowlarr_id, *, shape="id"):
    """An *arr indexer record as Prowlarr writes it. `shape` picks how the
    Prowlarr link is expressed so the id-vs-name matcher can be exercised."""
    if shape == "id":
        base = "http://prowlarr.invalid/prowlarr/%d/" % prowlarr_id
    else:                       # the URL shape changed underneath us
        base = "http://prowlarr.invalid/prowlarr/api"
    return {"id": 100 + prowlarr_id, "name": "%s (Prowlarr)" % name,
            "implementation": "Torznab",
            "fields": [{"name": "baseUrl", "value": base}]}


def native_entry(name):
    """An indexer added DIRECTLY to the *arr, bypassing Prowlarr (NZBgeek).
    It must never be counted as a Prowlarr-synced indexer in either direction."""
    return {"id": 999, "name": name, "implementation": "Newznab",
            "fields": [{"name": "baseUrl", "value": "https://api.example.invalid"}]}


MOVIE_CATS = [2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080, 2090]
TV_CATS = [5000, 5010, 5020, 5030, 5040, 5045, 5050, 5090]


def live_indexers(*, knaben_magnet=False, tokyo_magnet=False):
    """The 14 enabled + 1 disabled indexers as they exist on the box."""
    return [
        indexer(21, "Knaben", [3], [cat(2000), cat(5000)],
                magnet=knaben_magnet),
        indexer(2, "LimeTorrents", [3],
                [cat(2000), cat(5000, subs=[cat(5070)]), cat(8000)]),
        indexer(4, "The Pirate Bay", [3], [cat(2000), cat(5000)]),
        indexer(30, "YTS", [3], [cat(2000)]),
        indexer(33, "MagnetDownload", [3], [cat(8000)]),
        indexer(32, "TorrentsCSV", [3], [cat(8000)]),
        indexer(18, "Bangumi Moe", [1], [cat(2000), cat(5000)]),
        indexer(27, "nekoBT", [1], [cat(2000), cat(5000)]),
        indexer(8, "Nyaa.si", [1], [cat(2000), cat(5000)]),
        indexer(11, "subsplease", [1], [cat(2000), cat(5000)]),
        indexer(37, "Shana Project", [1], [cat(5000)]),
        indexer(36, "Nipponsei", [1], [cat(3000)]),
        indexer(38, "Tokyo Toshokan", [1],
                [cat(3000), cat(5000), cat(6000), cat(7000), cat(8000)],
                magnet=tokyo_magnet),
        indexer(39, "NZBgeek", [], [cat(2000), cat(5000)], protocol="usenet"),
        indexer(5, "TorrentDownload", [3], [cat(2000), cat(5000)],
                enable=False),
    ]


def live_applications(base):
    return [
        application(2, "Radarr", "Radarr", [3], "radarr", MOVIE_CATS,
                    base=base),
        application(1, "Sonarr", "Sonarr", [3], "sonarr", TV_CATS, [5070],
                    base=base),
        application(4, "Radarr2 (Anime)", "Radarr", [1], "radarr2", MOVIE_CATS,
                    base=base),
        application(3, "Sonarr2 (Anime)", "Sonarr", [1], "sonarr2", TV_CATS,
                    [5070], base=base),
    ]


def live_arr_lists(*, limetorrents_in_radarr=False):
    """Each *arr's own indexer list, verified live. Radarr is missing
    LimeTorrents; the four Torrent[CORE]/Uindex/kickass/TorrentDownload
    leftovers are the real disabled-legacy orphans."""
    radarr = [arr_entry("Knaben", 21), arr_entry("The Pirate Bay", 4),
              arr_entry("YTS", 30), native_entry("NZBgeek"),
              arr_entry("TorrentDownload", 5), arr_entry("Uindex", 25)]
    if limetorrents_in_radarr:
        radarr.append(arr_entry("LimeTorrents", 2))
    return {
        "radarr": radarr,
        "sonarr": [arr_entry("Knaben", 21), arr_entry("LimeTorrents", 2),
                   arr_entry("The Pirate Bay", 4), native_entry("NZBgeek"),
                   arr_entry("TorrentDownload", 5), arr_entry("Uindex", 25)],
        "radarr2": [arr_entry("Bangumi Moe", 18), arr_entry("nekoBT", 27),
                    arr_entry("Nyaa.si", 8), arr_entry("subsplease", 11),
                    native_entry("NZBgeek")],
        "sonarr2": [arr_entry("Bangumi Moe", 18), arr_entry("nekoBT", 27),
                    arr_entry("Nyaa.si", 8), arr_entry("subsplease", 11),
                    arr_entry("Shana Project", 37),
                    arr_entry("Tokyo Toshokan", 38), native_entry("NZBgeek")],
    }


# ---------------------------------------------------------------------------
# Fake Prowlarr + *arr stack
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self):                                   # noqa: N802 - stdlib
        fake = self.server.fake
        path = self.path.split("?", 1)[0]
        got_key = self.headers.get("X-Api-Key")
        fake.hits.append(path)
        route = fake.routes.get(path)
        if route is None:
            return self._send(404, {"error": "no route"})
        want_key, status, payload = route
        if want_key is not None and got_key != want_key:
            return self._send(401, {"error": "unauthorized"})
        self._send(status, payload)

    def _send(self, status, payload):
        body = (payload if isinstance(payload, bytes)
                else json.dumps(payload).encode("utf-8"))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, *args):
        pass


class FakeStack:
    def __init__(self):
        self.routes = {}
        self.hits = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.fake = self
        self.port = self.httpd.socket.getsockname()[1]
        self.base = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def route(self, path, payload, *, key=None, status=200):
        self.routes[path] = (key, status, payload)

    def stop(self):
        for fn in (self.httpd.shutdown, self.httpd.server_close):
            try:
                fn()
            except Exception:
                pass


@pytest.fixture
def stack():
    made = []

    def _make():
        s = FakeStack()
        made.append(s)
        return s

    yield _make
    for s in made:
        s.stop()


def _secrets(tmp_path, port, *, arr_keys=None, prowlarr_key=PROW_KEY,
             urlbase="prowlarr", omit=()):
    d = tmp_path / "secrets"
    d.mkdir(exist_ok=True)
    files = {"prowlarr.port": str(port), "prowlarr.key": prowlarr_key,
             "prowlarr.urlbase": urlbase}
    for slug, k in (ARR_KEYS if arr_keys is None else arr_keys).items():
        files[slug + ".key"] = k
    for name, value in files.items():
        if name in omit:
            continue
        (d / name).write_text(value, encoding="utf-8")
    return d


def _wire(stack_, *, indexers=None, apps=None, arr_lists=None, health=None,
          arr_keys=None, arr_status=200):
    indexers = live_indexers() if indexers is None else indexers
    apps = live_applications(stack_.base) if apps is None else apps
    arr_lists = live_arr_lists() if arr_lists is None else arr_lists
    stack_.route("/prowlarr/api/v1/indexer", indexers, key=PROW_KEY)
    stack_.route("/prowlarr/api/v1/applications", apps, key=PROW_KEY)
    stack_.route("/prowlarr/api/v1/health", health if health is not None else [],
                 key=PROW_KEY)
    keys = ARR_KEYS if arr_keys is None else arr_keys
    for slug, entries in arr_lists.items():
        stack_.route("/%s/api/v3/indexer" % slug, entries,
                     key=keys.get(slug), status=arr_status)
    return stack_


def _run(secrets_dir, args=(), **env_extra):
    env = dict(os.environ)
    env["MANITOBA_SECRETS"] = str(secrets_dir)
    env["QFLIX_CANARY_APPSYNC_TIMEOUT_S"] = "10"
    for k, v in env_extra.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = str(v)
    return subprocess.run(["bash", str(SCRIPT), *args], env=env,
                          capture_output=True, text=True, timeout=120)


def _stage(result):
    m = re.search(r"STAGE=([a-z0-9-]+)", result.stderr or "")
    return m.group(1) if m else None


def _json(result):
    return json.loads(result.stdout)


# ===========================================================================
# 1. THE INCIDENT, and the mutation proof
# ===========================================================================


def test_reproduces_the_limetorrents_radarr_gap(tmp_path, stack):
    """Prowlarr intends to sync LimeTorrents to Radarr (enabled, shares tag
    `general`, declares cat 2000) and Radarr does not have it. Ten weeks, every
    6 hours, nothing red."""
    s = _wire(stack())
    r = _run(_secrets(tmp_path, s.port))
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert _stage(r) == "prowlarr-appsync-missing", r.stderr
    assert "Radarr:LimeTorrents" in r.stderr, r.stderr


def test_the_health_endpoint_probe_is_green_on_the_very_same_fixture(
        tmp_path, stack):
    """MUTATION PROOF. Prowlarr only raises an indexer health item after an
    indexer has been DISABLED for failures >6h. LimeTorrents never fails -- it
    answers 200 and returns nothing in cat 2000 -- so /api/v1/health is [] and
    Probe 2's numerator is zero, not small. Lowering its threshold from 2 to 1
    would change nothing."""
    assert _health_probe_verdict([]) is True, (
        "fixture must be indistinguishable from healthy to the existing probe")
    s = _wire(stack())
    r = _run(_secrets(tmp_path, s.port))
    assert r.returncode == 1, "the new canary must see what /health cannot"


def test_a_clean_stack_passes_with_the_real_counts(tmp_path, stack):
    s = _wire(stack(),
              indexers=live_indexers(knaben_magnet=True, tokyo_magnet=True),
              arr_lists=live_arr_lists(limetorrents_in_radarr=True))
    r = _run(_secrets(tmp_path, s.port))
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert r.stdout.startswith("PASS: prowlarr-app-sync"), r.stdout
    assert "apps=4" in r.stdout
    assert "idx=14/15" in r.stdout, r.stdout
    assert "missing" not in r.stderr
    # 4 (Radarr) + 3 (Sonarr) + 4 (Radarr2) + 6 (Sonarr2), computed by hand
    # from the live tag/category matrix.
    assert "intended=17" in r.stdout, r.stdout
    assert "magnet_ok=2" in r.stdout, r.stdout


def test_a_pass_is_one_stdout_line_and_a_silent_stderr(tmp_path, stack):
    s = _wire(stack(),
              indexers=live_indexers(knaben_magnet=True, tokyo_magnet=True),
              arr_lists=live_arr_lists(limetorrents_in_radarr=True))
    r = _run(_secrets(tmp_path, s.port))
    assert r.returncode == 0
    assert len([ln for ln in r.stdout.splitlines() if ln.strip()]) == 1
    assert r.stderr.strip() == "", r.stderr


def test_failure_output_is_exactly_one_line_of_two_tokens(tmp_path, stack):
    """cli.py takes stderr verbatim as the Kuma msg= and truncates at 200 chars
    (scripts/maint/lib/cli.py:605-610), so the contract is one line of exactly
    two whitespace-separated tokens -- which also means no name may carry a
    space into the message."""
    s = _wire(stack())
    r = _run(_secrets(tmp_path, s.port))
    lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1, lines
    tokens = lines[0].split()
    assert len(tokens) == 2, tokens
    assert tokens[0].startswith("STAGE=")
    assert tokens[1].startswith("msg=")
    # The docstring above named the 200-char truncation and then asserted
    # nothing about it, so an unbounded join sailed through. Now pinned.
    assert len(lines[0]) <= 200, (len(lines[0]), lines[0])
    assert r.stdout.strip() == "", r.stdout


def test_an_indexer_name_with_a_space_is_slugged_into_the_message(
        tmp_path, stack):
    """"Tokyo Toshokan" reaching msg= unmodified would split the Kuma message
    into three tokens and truncate the verdict."""
    s = _wire(stack(), arr_lists=live_arr_lists(limetorrents_in_radarr=True))
    r = _run(_secrets(tmp_path, s.port))
    assert r.returncode == 1
    assert _stage(r) == "prowlarr-appsync-magnet-pref-off"
    assert "Tokyo_Toshokan" in r.stderr, r.stderr


# ===========================================================================
# 2. RULE 4 -- every skip is counted and named
# ===========================================================================


def test_by_design_category_skips_are_counted_not_silent(tmp_path, stack):
    """MagnetDownload/TorrentsCSV publish only 8000, YTS only 2000, and
    Nipponsei/Shana Project/Tokyo Toshokan miss on the mirrored anime side.
    Prowlarr logs those at Debug. If they simply vanished from this canary's
    arithmetic, an indexer that STOPPED publishing a wanted category would be
    indistinguishable from one that never did."""
    s = _wire(stack(),
              indexers=live_indexers(knaben_magnet=True, tokyo_magnet=True),
              arr_lists=live_arr_lists(limetorrents_in_radarr=True))
    r = _run(_secrets(tmp_path, s.port), ["--json"])
    assert r.returncode == 0, r.stderr
    d = _json(r)
    per_app = {a["app"]: a for a in d["apps"]}
    assert set(per_app["Radarr"]["skip_nocat"]) == {"MagnetDownload",
                                                    "TorrentsCSV"}
    assert set(per_app["Sonarr"]["skip_nocat"]) == {"MagnetDownload",
                                                    "TorrentsCSV", "YTS"}
    assert set(per_app["Sonarr2 (Anime)"]["skip_nocat"]) == {"Nipponsei"}
    assert d["totals"]["skip_nocat"] == 9, d["totals"]


def test_tag_mismatches_are_counted_and_named(tmp_path, stack):
    s = _wire(stack(),
              indexers=live_indexers(knaben_magnet=True, tokyo_magnet=True),
              arr_lists=live_arr_lists(limetorrents_in_radarr=True))
    r = _run(_secrets(tmp_path, s.port), ["--json"])
    d = _json(r)
    per_app = {a["app"]: a for a in d["apps"]}
    # NZBgeek carries no tag at all: an untagged indexer does NOT sync to a
    # tagged app, which is why it is added directly to each *arr instead.
    assert "NZBgeek" in per_app["Radarr"]["skip_notag"]
    assert "Nyaa.si" in per_app["Radarr"]["skip_notag"]
    assert d["totals"]["skip_notag"] == 30, d["totals"]


def test_a_disabled_indexer_is_counted_and_never_reported_missing(
        tmp_path, stack):
    s = _wire(stack(),
              indexers=live_indexers(knaben_magnet=True, tokyo_magnet=True),
              arr_lists=live_arr_lists(limetorrents_in_radarr=True))
    r = _run(_secrets(tmp_path, s.port), ["--json"])
    d = _json(r)
    assert d["skip_disabled"] == 1
    assert not d["missing"]


def test_orphans_are_counted_and_reported_but_do_not_fail(tmp_path, stack):
    """Uindex and TorrentDownload are disabled in Prowlarr yet still sit in
    Radarr/Sonarr. Untidy, not broken. Paging on it would park a permanent red
    for a condition no operator is going to act on today."""
    s = _wire(stack(),
              indexers=live_indexers(knaben_magnet=True, tokyo_magnet=True),
              arr_lists=live_arr_lists(limetorrents_in_radarr=True))
    r = _run(_secrets(tmp_path, s.port), ["--json"])
    assert r.returncode == 0, r.stderr
    d = _json(r)
    per_app = {a["app"]: a for a in d["apps"]}
    assert set(per_app["Radarr"]["orphan"]) == {"TorrentDownload", "id-25"}
    assert d["totals"]["orphan"] == 4, d["totals"]


def test_a_natively_added_indexer_is_neither_synced_nor_an_orphan(
        tmp_path, stack):
    """NZBgeek is added straight to each *arr and correctly bypasses Prowlarr.
    Counting it as an orphan would manufacture four findings out of a correct
    configuration."""
    s = _wire(stack(),
              indexers=live_indexers(knaben_magnet=True, tokyo_magnet=True),
              arr_lists=live_arr_lists(limetorrents_in_radarr=True))
    r = _run(_secrets(tmp_path, s.port), ["--json"])
    d = _json(r)
    for a in d["apps"]:
        assert "NZBgeek" not in a["orphan"], a


# ===========================================================================
# 3. P1 SEMANTICS
# ===========================================================================


def test_subcategory_flattening_is_load_bearing(tmp_path, stack):
    """Sonarr's animeSyncCategories is literally [5070], a SUBcategory of 5000.
    A flatten that stopped at the parent would mark the indexer skip_nocat and
    then never report it missing -- a silent false green."""
    s = stack()
    idx = [indexer(7, "AnimeOnly", [1], [cat(5000, subs=[cat(5070)])])]
    apps = [application(3, "Sonarr2 (Anime)", "Sonarr", [1], "sonarr2", [],
                        [5070], base=s.base)]
    _wire(s, indexers=idx, apps=apps, arr_lists={"sonarr2": []})
    r = _run(_secrets(tmp_path, s.port), ["--json"])
    assert r.returncode == 1, (r.stdout, r.stderr)
    d = _json(r)
    assert [m["indexer"] for m in d["missing"]] == ["AnimeOnly"]
    assert d["apps"][0]["skip_nocat"] == []


def test_an_app_with_no_tags_syncs_every_enabled_indexer(tmp_path, stack):
    """Prowlarr's rule: an app with NO tags takes every indexer; an app WITH
    tags takes only indexers sharing one. Getting this backwards would report
    every indexer as missing on an untagged app."""
    s = stack()
    idx = [indexer(1, "Tagged", [9], [cat(2000)]),
           indexer(2, "Untagged", [], [cat(2000)])]
    apps = [application(2, "Radarr", "Radarr", [], "radarr", MOVIE_CATS,
                        base=s.base)]
    _wire(s, indexers=idx, apps=apps,
          arr_lists={"radarr": [arr_entry("Tagged", 1)]})
    r = _run(_secrets(tmp_path, s.port), ["--json"])
    assert r.returncode == 1
    d = _json(r)
    assert [m["indexer"] for m in d["missing"]] == ["Untagged"]
    assert d["apps"][0]["skip_notag"] == []


def test_an_application_with_synclevel_disabled_is_skipped_and_counted(
        tmp_path, stack):
    s = stack()
    idx = [indexer(1, "Only", [3], [cat(2000)])]
    apps = [application(2, "Radarr", "Radarr", [3], "radarr", MOVIE_CATS,
                        base=s.base),
            application(9, "Retired", "Radarr", [3], "radarr2", MOVIE_CATS,
                        base=s.base, sync_level="disabled")]
    _wire(s, indexers=idx, apps=apps,
          arr_lists={"radarr": [arr_entry("Only", 1)], "radarr2": []})
    r = _run(_secrets(tmp_path, s.port), ["--json"])
    assert r.returncode == 0, r.stderr
    d = _json(r)
    assert d["totals"]["apps_sync_disabled"] == 1
    assert d["totals"]["apps_checked"] == 1
    assert {"app": "Retired", "state": "sync-disabled"} in d["apps"]


def test_matching_survives_a_changed_prowlarr_url_shape_and_says_so(
        tmp_path, stack):
    """If Prowlarr ever stops embedding the indexer id in the *arr's baseUrl,
    an id-only matcher reports EVERY indexer missing -- a fabricated outage far
    louder than the real one. Degrade to name matching, and disclose it."""
    s = stack()
    idx = [indexer(21, "Knaben", [3], [cat(2000)], magnet=True),
           indexer(4, "The Pirate Bay", [3], [cat(2000)])]
    apps = [application(2, "Radarr", "Radarr", [3], "radarr", MOVIE_CATS,
                        base=s.base)]
    _wire(s, indexers=idx, apps=apps, arr_lists={"radarr": [
        arr_entry("Knaben", 21, shape="broken"),
        arr_entry("The Pirate Bay", 4, shape="broken")]})
    r = _run(_secrets(tmp_path, s.port), ["--json"])
    assert r.returncode == 0, (r.stdout, r.stderr)
    d = _json(r)
    assert d["apps"][0]["match"] == "name", d["apps"][0]
    assert d["missing"] == []


def test_id_matching_is_immune_to_a_renamed_indexer(tmp_path, stack):
    """A name-only matcher would call a renamed indexer both missing AND an
    orphan -- two findings for zero faults."""
    s = stack()
    idx = [indexer(21, "Knaben (EU mirror)", [3], [cat(2000)], magnet=True)]
    apps = [application(2, "Radarr", "Radarr", [3], "radarr", MOVIE_CATS,
                        base=s.base)]
    _wire(s, indexers=idx, apps=apps,
          arr_lists={"radarr": [arr_entry("Knaben", 21)]})
    r = _run(_secrets(tmp_path, s.port), ["--json"])
    assert r.returncode == 0, (r.stdout, r.stderr)
    d = _json(r)
    assert d["apps"][0]["match"] == "id"
    assert d["missing"] == [] and d["apps"][0]["orphan"] == []


# ===========================================================================
# 4. P2 -- the grab-path regression lock
# ===========================================================================


def test_magnet_pref_off_fires_when_sync_is_otherwise_clean(tmp_path, stack):
    """The Knaben/Tokyo Toshokan class: nothing is missing, every app syncs,
    and grabs still fail because the *arr prefers a downloadUrl that 403s."""
    s = _wire(stack(), arr_lists=live_arr_lists(limetorrents_in_radarr=True))
    r = _run(_secrets(tmp_path, s.port))
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert _stage(r) == "prowlarr-appsync-magnet-pref-off", r.stderr
    assert "Knaben" in r.stderr and "Tokyo_Toshokan" in r.stderr


def test_ticking_prefer_magnet_url_is_what_clears_it(tmp_path, stack):
    """The runbook's step 1, executed against the artifact: exactly the one
    field flips and the canary goes green. This is the acceptance test for
    docs/prowlarr-indexer-remediation-2026-08-03.md."""
    s = _wire(stack(),
              indexers=live_indexers(knaben_magnet=True, tokyo_magnet=True),
              arr_lists=live_arr_lists(limetorrents_in_radarr=True))
    r = _run(_secrets(tmp_path, s.port))
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "magnet_ok=2" in r.stdout


def test_a_policy_entry_for_a_removed_indexer_is_counted_not_a_red(
        tmp_path, stack):
    """If the operator deletes Knaben, a policy list that still names it must
    not park a permanent red -- but it must not go silent either, or the list
    rots into decoration."""
    s = _wire(stack(),
              indexers=live_indexers(knaben_magnet=True, tokyo_magnet=True),
              arr_lists=live_arr_lists(limetorrents_in_radarr=True))
    r = _run(_secrets(tmp_path, s.port), ["--json"],
             QFLIX_CANARY_APPSYNC_MAGNET_REQUIRED="Knaben,Ghost Tracker")
    assert r.returncode == 0, (r.stdout, r.stderr)
    d = _json(r)
    assert d["magnet"]["absent"] == ["Ghost Tracker"]
    assert d["magnet"]["ok"] == ["Knaben"]


def test_the_policy_can_be_switched_off_explicitly_and_only_explicitly(
        tmp_path, stack):
    """Removing an entry is an explicit, reviewable decision. Nothing else
    disables P2 -- there is no threshold to quietly tune to zero."""
    s = _wire(stack(), arr_lists=live_arr_lists(limetorrents_in_radarr=True))
    off = _run(_secrets(tmp_path, s.port),
               QFLIX_CANARY_APPSYNC_MAGNET_REQUIRED="")
    assert off.returncode == 0, (off.stdout, off.stderr)
    on = _run(_secrets(tmp_path, s.port))
    assert on.returncode == 1, "the shipped default must still hold both names"


def test_a_usenet_indexer_named_in_the_policy_is_not_a_violation(
        tmp_path, stack):
    """preferMagnetUrl does not exist for Newznab. Reporting its absence as a
    violation would be a permanent false red."""
    s = _wire(stack(),
              indexers=live_indexers(knaben_magnet=True, tokyo_magnet=True),
              arr_lists=live_arr_lists(limetorrents_in_radarr=True))
    r = _run(_secrets(tmp_path, s.port), ["--json"],
             QFLIX_CANARY_APPSYNC_MAGNET_REQUIRED="NZBgeek")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert _json(r)["magnet"]["not_torrent"] == ["NZBgeek"]


def test_a_sync_gap_outranks_a_magnet_violation_in_the_message(tmp_path, stack):
    """Both can be true at once (they are, right now). One line reaches Kuma,
    so the missing-indexer finding leads and the magnet finding rides along
    rather than being dropped."""
    s = _wire(stack())
    r = _run(_secrets(tmp_path, s.port))
    assert _stage(r) == "prowlarr-appsync-missing"
    assert "magnet_off=Knaben,Tokyo_Toshokan" in r.stderr, r.stderr


# ===========================================================================
# 5. RULE 5 -- empty-because-clean vs empty-because-broken
# ===========================================================================


def test_zero_applications_is_broken_not_clean(tmp_path, stack):
    """The sharpest leg. With no applications the intended set is empty and
    "nothing is missing" is trivially true -- a wiped Applications page would
    otherwise read as a perfect green."""
    s = _wire(stack(), apps=[])
    r = _run(_secrets(tmp_path, s.port))
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert _stage(r) == "prowlarr-appsync-no-apps", r.stderr


def test_every_application_sync_disabled_is_also_broken_not_clean(
        tmp_path, stack):
    s = stack()
    apps = [application(2, "Radarr", "Radarr", [3], "radarr", MOVIE_CATS,
                        base=s.base, sync_level="disabled")]
    _wire(s, apps=apps, arr_lists={"radarr": []})
    r = _run(_secrets(tmp_path, s.port))
    assert r.returncode == 2
    assert _stage(r) == "prowlarr-appsync-no-apps", r.stderr


def test_zero_enabled_indexers_is_broken_not_clean(tmp_path, stack):
    s = _wire(stack(), indexers=[indexer(1, "Off", [3], [cat(2000)],
                                         enable=False)])
    r = _run(_secrets(tmp_path, s.port))
    assert r.returncode == 2
    assert _stage(r) == "prowlarr-appsync-no-indexers", r.stderr


def test_prowlarr_unreachable_is_broken(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    r = _run(_secrets(tmp_path, port), QFLIX_CANARY_APPSYNC_TIMEOUT_S="2")
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert _stage(r) == "prowlarr-appsync-prowlarr-down", r.stderr


def test_a_wrong_prowlarr_key_is_broken_not_clean(tmp_path, stack):
    s = _wire(stack())
    r = _run(_secrets(tmp_path, s.port, prowlarr_key="wrong"))
    assert r.returncode == 2
    assert _stage(r) == "prowlarr-appsync-prowlarr-down", r.stderr
    assert "http-401" in r.stderr, r.stderr


def test_missing_prowlarr_secrets_is_broken(tmp_path, stack):
    s = _wire(stack())
    r = _run(_secrets(tmp_path, s.port, omit=("prowlarr.key",)))
    assert r.returncode == 2
    assert _stage(r) == "prowlarr-appsync-config-missing", r.stderr


def test_an_unreachable_arr_is_broken_and_named(tmp_path, stack):
    """A 500 from ONE *arr must not be reported as "that *arr is missing every
    indexer". Distinguishing them is the whole point of the third exit code."""
    s = _wire(stack(), arr_status=500)
    r = _run(_secrets(tmp_path, s.port))
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert _stage(r) == "prowlarr-appsync-arr-down", r.stderr
    assert "http-500" in r.stderr, r.stderr


def test_a_missing_local_arr_key_is_broken_and_names_the_slug(tmp_path, stack):
    """Prowlarr masks the apiKey it hands back, so the *arr key has to come
    from ~/secrets/<slug>.key. If that file is gone the canary must say so, not
    silently report the *arr as empty."""
    s = _wire(stack())
    keys = dict(ARR_KEYS)
    keys.pop("radarr")
    r = _run(_secrets(tmp_path, s.port, arr_keys=keys))
    assert r.returncode == 2
    assert _stage(r) == "prowlarr-appsync-arr-down", r.stderr
    assert "radarr" in r.stderr


def test_an_unknown_application_implementation_is_broken_never_guessed(
        tmp_path, stack):
    """Guessing /api/v3 at an app that speaks /api/v1 would 404 and read as
    "every indexer missing" -- a fabricated outage. Refuse instead."""
    s = stack()
    apps = [application(2, "Mystery", "Sonarr4", [3], "radarr", MOVIE_CATS,
                        base=s.base)]
    _wire(s, apps=apps, arr_lists={"radarr": []})
    r = _run(_secrets(tmp_path, s.port))
    assert r.returncode == 2
    assert _stage(r) == "prowlarr-appsync-unknown-app", r.stderr


def test_a_non_json_prowlarr_response_is_broken(tmp_path, stack):
    """An nginx error page or a login redirect is a 200 with HTML. Parsed
    loosely it becomes an empty list, i.e. a green board."""
    s = stack()
    s.route("/prowlarr/api/v1/indexer", b"<html>login</html>", key=PROW_KEY)
    r = _run(_secrets(tmp_path, s.port))
    assert r.returncode == 2
    assert _stage(r) == "prowlarr-appsync-prowlarr-down", r.stderr
    assert "non-json-body" in r.stderr, r.stderr


# ===========================================================================
# 6. The artifact matches what the runbook and the manifest claim
# ===========================================================================


def test_the_script_names_its_runbook(tmp_path):
    """A detector whose remediation lives nowhere is a red light with no
    switch. The runbook has to be findable from the script that fires."""
    src = SCRIPT.read_text(encoding="utf-8")
    doc = "docs/prowlarr-indexer-remediation-2026-08-03.md"
    assert doc in src, "the canary must point at its runbook"
    assert (REPO_ROOT / doc).is_file()


def test_every_stage_label_the_script_can_emit_is_documented_in_its_header():
    """The header table is what an operator reads at 3am off a Kuma message.
    A label that exists only in code is a message with no explanation."""
    src = SCRIPT.read_text(encoding="utf-8")
    emitted = set(re.findall(r'STAGE=([a-z0-9-]+)', src))
    header = src.split("set -uo pipefail", 1)[0]
    undocumented = sorted(lbl for lbl in emitted
                          if ("#   %s" % lbl) not in header)
    assert not undocumented, (
        "STAGE labels emitted but absent from the header table: %s"
        % undocumented)


def test_the_shipped_magnet_policy_is_exactly_the_two_diagnosed_indexers():
    """Pins the deliberate scope decision. All 12 enabled torrent indexers have
    preferMagnetUrl=false; only these two have a diagnosed broken proxy
    download path. Widening the default silently would demand ten unjustified
    config changes and make the canary born-red for the wrong reason."""
    src = SCRIPT.read_text(encoding="utf-8")
    assert '_mag = "Knaben,Tokyo Toshokan"' in src


# ===========================================================================
# The EMPTY-INTENT condition, guarded rather than just its two known causes
# (arbiter fix 2026-08-03)
# ===========================================================================


def test_a_topology_with_no_indexer_categories_is_broken_not_clean(
        tmp_path, stack):
    """Zero apps and zero enabled indexers were the two ANTICIPATED ways to get
    an empty intended set. The thing that makes the verdict vacuous is the
    empty set itself, and category data can vanish on either side.

    Here every indexer comes back with `capabilities.categories == []` -- the
    shape a Prowlarr definition/schema change produces. Every indexer falls into
    skip_nocat, `intended` is empty, `missing` is trivially empty, and every
    present entry is reclassified as an orphan (which by design never fails).
    Before the guard this exited 0 with intended=0, orphan=16, skip_nocat=10."""
    s = stack()
    blind = [dict(i, capabilities={"categories": []}) for i in live_indexers()]
    _wire(s, indexers=blind, arr_lists=live_arr_lists())
    r = _run(_secrets(tmp_path, s.port))
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert _stage(r) == "prowlarr-appsync-no-intent", r.stderr
    assert "ZERO-intended-syncs" in r.stderr, r.stderr


def test_wiped_sync_categories_on_every_app_is_broken_not_clean(
        tmp_path, stack):
    """The mirror image: the indexers are fine, the APPLICATIONS lost their
    syncCategories. Same vacuous verdict, same exit 2."""
    s = stack()
    apps = [application(2, "Radarr", "Radarr", [3], "radarr", [], base=s.base),
            application(1, "Sonarr", "Sonarr", [3], "sonarr", [], base=s.base)]
    _wire(s, apps=apps, arr_lists=live_arr_lists())
    r = _run(_secrets(tmp_path, s.port))
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert _stage(r) == "prowlarr-appsync-no-intent", r.stderr


def test_the_empty_intent_guard_discriminates(tmp_path, stack):
    """MUTATION PROOF. The SAME fixture with categories restored must NOT trip
    the new guard, so exit 2 above is caused by the blindness rather than by the
    guard firing on everything."""
    s = _wire(stack(), arr_lists=live_arr_lists(limetorrents_in_radarr=True))
    r = _run(_secrets(tmp_path, s.port))
    assert _stage(r) != "prowlarr-appsync-no-intent", r.stderr


# ===========================================================================
# The alert line must survive the widest finding, counts first
# ===========================================================================


def test_a_total_outage_keeps_the_counts_and_declares_what_it_dropped(
        tmp_path, stack):
    """THE FAILURE MODE WAS INVERTED: the wider the outage, the less the
    operator was told. `where` joined every app:indexer pair unbounded and the
    rule-4 tally was appended LAST, so cli.py's 200-char cut dropped the tally
    entirely and severed the final name mid-token with no marker. Measured on
    this exact fixture before the fix: 1327 bytes of stderr, 9 of 56 entries
    survived, zero counts."""
    s = stack()
    _wire(s, arr_lists={"radarr": [], "sonarr": [], "radarr2": [],
                        "sonarr2": []})
    r = _run(_secrets(tmp_path, s.port))
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    line = [ln for ln in r.stderr.splitlines() if ln.strip()][0]
    assert len(line) <= 200, (len(line), line)
    # The counts are the rule-4 audit trail. They must never be the thing that
    # gets dropped.
    for token in ("intended=", "present=", "orphan=", "skip_notag=",
                  "skip_nocat=", "skip_disabled="):
        assert token in line, (token, line)
    # And a drop must announce itself rather than looking like a short finding.
    assert "more" in line, line
    # Full detail is never lost -- it is on stdout under --json.
    detail = _json(_run(_secrets(tmp_path, s.port), args=("--json",)))
    assert len(detail["missing"]) == 17, len(detail["missing"])


def test_a_magnet_only_finding_also_stays_inside_the_kuma_budget(
        tmp_path, stack):
    s = stack()
    _wire(s, arr_lists=live_arr_lists(limetorrents_in_radarr=True))
    r = _run(_secrets(tmp_path, s.port),
             QFLIX_CANARY_APPSYNC_MAGNET_REQUIRED=",".join(
                 [i["name"] for i in live_indexers()
                  if i["enable"] and i["protocol"] == "torrent"]))
    assert r.returncode == 1
    line = [ln for ln in r.stderr.splitlines() if ln.strip()][0]
    assert len(line) <= 200, (len(line), line)
    assert "intended=" in line, line
