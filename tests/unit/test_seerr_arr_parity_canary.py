"""Tests for scripts/canaries/seerr-arr-parity.sh.

There is no shell-lint gate in this repo (.github/workflows/tests.yml runs
pytest and nothing else), so a test that actually runs `bash <script>` is the
only real gate on this canary's correctness. Everything below drives the
shipped artifact end to end: real bash, real python3, real HTTP over
loopback, real secret files on disk -- same approach as
test_prowlarr_app_sync.py.

Three jobs, matching the repo's established test shape for this class of
canary:

  1. THE TWO-RUN GATE (arr-plex-parity's noise-control mechanism, reused
     verbatim). A finding must ARM on its first sighting (exit 0) and only
     PAGE on the second consecutive sighting (exit 1). Every finding-shaped
     test below drives the script twice against the same state file.

  2. THE COUNTED-SKIP CONTRACT (rule 4). Every intentional exclusion --
     not-settled status, within-grace, an unparseable timestamp, a season
     Sonarr doesn't know about -- must appear in a named `skips=` bucket,
     never vanish silently.

  3. THE EXIT-CODE TRICHOTOMY (rule 5). 0 clean/armed (PASS-WARN for a
     confirmed underreported-only backlog) / 1 orphan-or-stranded confirmed /
     2 could-not-assert, with a negative control for each broken leg --
     including the two that matter most, an empty Seerr list and an empty
     *arr list, where "nothing is missing" would be trivially true and
     completely wrong.

Additionally: `reconcile_seerr()`'s real-world blind spot is `filter=
available` only, which can never see a DELETED (status 7) row. This suite's
STRANDED tests use status 7 specifically, so a clean pass here is a genuine
demonstration that this canary is STRICTLY STRONGER than the mechanism it
was built to catch drifting.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "canaries" / "seerr-arr-parity.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="seerr-arr-parity.sh needs bash + python3 on PATH",
)

SEERR_KEY = "seerr-test-key"
ARR_KEYS = {"sonarr": "sk", "sonarr2": "s2k", "radarr": "rk", "radarr2": "r2k"}

# Far enough in the past that it always clears the default 26h grace window,
# and fixed so tests never depend on wall-clock time.
NOW = 2_000_000_000
OLD_STAMP = "2020-01-01T00:00:00.000Z"
FRESH_STAMP = "2033-05-18T04:19:47.000Z"  # deliberately AFTER NOW (epoch ~2e9 = 2033)

NOT_SETTLED = {2: "PENDING", 3: "PROCESSING", 6: "BLOCKLISTED"}


# ---------------------------------------------------------------------------
# Fake Seerr + *arr stack -- one HTTP server, routed by path (with an
# optional prefix match for /api/v1/tv/<tmdbId> and a callable payload for
# paginated /api/v1/media).
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self):                                    # noqa: N802 - stdlib
        fake = self.server.fake
        raw = self.path
        path = raw.split("?", 1)[0]
        qs = {k: v[0] for k, v in parse_qs(raw.split("?", 1)[1]).items()} if "?" in raw else {}
        fake.hits.append(raw)
        got_key = self.headers.get("X-Api-Key")

        route = fake.routes.get(path)
        if route is None:
            for prefix, r in fake.prefix_routes.items():
                if path.startswith(prefix):
                    route = r
                    break
        if route is None:
            return self._send(404, {"error": "no route for %s" % path})

        want_key, status, payload = route
        if want_key is not None and got_key != want_key:
            return self._send(401, {"error": "unauthorized"})
        if callable(payload):
            result = payload(path, qs)
            status2, body = result if isinstance(result, tuple) else (status, result)
        else:
            status2, body = status, payload
        self._send(status2, body)

    def _send(self, status, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
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
        self.prefix_routes = {}
        self.hits = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.fake = self
        self.port = self.httpd.socket.getsockname()[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def route(self, path, payload, *, key=None, status=200, prefix=False):
        (self.prefix_routes if prefix else self.routes)[path] = (key, status, payload)

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


def _closed_port():
    """A port nothing is listening on -- connect() gets refused immediately."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def media_row(id_, mtype, tmdb, status, *, tvdb=None, updated=OLD_STAMP):
    row = {"id": id_, "mediaType": mtype, "tmdbId": tmdb, "status": status, "updatedAt": updated}
    if tvdb is not None:
        row["tvdbId"] = tvdb
    return row


def paginated(rows, take_hint=100):
    """A /api/v1/media route callable that honours take/skip like the real
    Seerr admin endpoint, so the pagination loop in the script is actually
    exercised rather than assumed."""
    def _handler(_path, qs):
        take = int(qs.get("take", take_hint))
        skip = int(qs.get("skip", 0))
        return {"results": rows[skip:skip + take]}
    return _handler


def tv_detail_map(details):
    def _handler(path, _qs):
        tmdb = int(path.rsplit("/", 1)[-1])
        return details.get(tmdb, {})
    return _handler


def series(tvdb, seasons):
    """seasons: {seasonNumber: episodeFileCount}"""
    return {"tvdbId": tvdb,
            "seasons": [{"seasonNumber": n, "statistics": {"episodeFileCount": fc}}
                        for n, fc in seasons.items()]}


def tv_detail(tvdb, seasons):
    """seasons: {seasonNumber: seerrStatus}"""
    return {"externalIds": {"tvdbId": tvdb},
            "mediaInfo": {"seasons": [{"seasonNumber": n, "status": st}
                                       for n, st in seasons.items()]}}


def _secrets(tmp_path, seerr_port, arr_ports, *, arr_keys=None, seerr_key=SEERR_KEY, omit=()):
    """arr_ports: {"sonarr": port, "sonarr2": port, "radarr": port, "radarr2": port}"""
    d = tmp_path / "secrets"
    d.mkdir(exist_ok=True)
    keys = ARR_KEYS if arr_keys is None else arr_keys
    files = {"seerr.port": str(seerr_port), "seerr.key": seerr_key}
    for slug, port in arr_ports.items():
        files[slug + ".port"] = str(port)
        files[slug + ".key"] = keys[slug]
        files[slug + ".urlbase"] = slug
    for name, value in files.items():
        if name in omit:
            continue
        (d / name).write_text(value, encoding="utf-8")
    return d


def _wire_default(stack_, *, media_rows, tv_details=None, sonarr=None, sonarr2=None,
                   radarr=None, radarr2=None):
    """A minimally-consistent four-*arr backend: every arr answers with at
    least one (non-matching, harmless) item unless the caller overrides it,
    so tests that are not exercising the arr-empty leg don't trip it by
    accident."""
    stack_.route("/api/v1/media", paginated(media_rows), key=SEERR_KEY)
    stack_.route("/api/v1/tv/", tv_detail_map(tv_details or {}), key=SEERR_KEY, prefix=True)
    stack_.route("/sonarr/api/v3/series", sonarr if sonarr is not None
                 else [series(90001, {1: 0})], key=ARR_KEYS["sonarr"])
    stack_.route("/sonarr2/api/v3/series", sonarr2 if sonarr2 is not None
                 else [series(90002, {1: 0})], key=ARR_KEYS["sonarr2"])
    stack_.route("/radarr/api/v3/movie", radarr if radarr is not None
                 else [{"tmdbId": -1}], key=ARR_KEYS["radarr"])
    stack_.route("/radarr2/api/v3/movie", radarr2 if radarr2 is not None
                 else [{"tmdbId": -2}], key=ARR_KEYS["radarr2"])
    return stack_


def _run(secrets_dir, *, state=None, trail=None, **env_extra):
    env = dict(os.environ)
    env["MANITOBA_SECRETS"] = str(secrets_dir)
    env["QFLIX_CANARY_SAP_NOW"] = str(NOW)
    env["QFLIX_CANARY_SAP_TIMEOUT_S"] = "10"
    if state is not None:
        env["QFLIX_CANARY_SAP_STATE"] = str(state)
    if trail is not None:
        env["QFLIX_CANARY_SAP_TRAIL"] = str(trail)
    for k, v in env_extra.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = str(v)
    return subprocess.run(["bash", str(SCRIPT)], env=env,
                          capture_output=True, text=True, timeout=120)


def _run_twice(secrets_dir, tmp_path, **env_extra):
    """The two-consecutive-run gate needs the SAME state file across both
    invocations; a fresh tmp_path per call (pytest's default) would silently
    make every finding look like a first sighting forever."""
    state = tmp_path / "state.json"
    trail = tmp_path / "trail.log"
    r1 = _run(secrets_dir, state=state, trail=trail, **env_extra)
    r2 = _run(secrets_dir, state=state, trail=trail, **env_extra)
    return r1, r2


# ---------------------------------------------------------------------------
# 1. Clean pass
# ---------------------------------------------------------------------------


def test_clean_pass_when_everything_reconciles(tmp_path, stack):
    """Movie present in radarr, TV season's Sonarr file count matches its
    Seerr AVAILABLE status: no finding, either run."""
    s = stack()
    rows = [
        media_row(1, "movie", 100, 5),
        media_row(2, "tv", 200, 5, tvdb=300),
    ]
    _wire_default(s, media_rows=rows,
                  tv_details={200: tv_detail(300, {1: 5})},
                  sonarr=[series(300, {1: 5})],
                  radarr=[{"tmdbId": 100}])
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1, r2 = _run_twice(secrets, tmp_path)
    assert r1.returncode == 0, (r1.returncode, r1.stdout, r1.stderr)
    assert r2.returncode == 0, (r2.returncode, r2.stdout, r2.stderr)
    assert r1.stdout.startswith("PASS:")
    assert "orphan=0 stranded=0 underreported=0" in r1.stdout
    assert "skips=" in r1.stdout


# ---------------------------------------------------------------------------
# 2. ORPHAN -- movie
# ---------------------------------------------------------------------------


def test_orphan_movie_arms_then_pages(tmp_path, stack):
    s = stack()
    rows = [media_row(1, "movie", 999, 5)]   # 999 is in NEITHER radarr
    _wire_default(s, media_rows=rows)
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1, r2 = _run_twice(secrets, tmp_path)
    assert r1.returncode == 0, (r1.returncode, r1.stdout, r1.stderr)
    assert "orphan=1" in r1.stdout
    assert r2.returncode == 1, (r2.returncode, r2.stdout, r2.stderr)
    assert "STAGE=seerr-arr-parity" in r2.stderr
    assert "orphan-movie" in r2.stderr
    assert "tmdb=999" in r2.stderr


def test_orphan_movie_absent_when_present_in_either_radarr_instance(tmp_path, stack):
    """A movie in radarr2 only (the anime split) must NOT be an orphan --
    orphan status is checked against the UNION of both instances."""
    s = stack()
    rows = [media_row(1, "movie", 555, 5)]
    _wire_default(s, media_rows=rows, radarr2=[{"tmdbId": 555}])
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1, _ = _run_twice(secrets, tmp_path)
    assert r1.returncode == 0
    assert "orphan=0" in r1.stdout


# ---------------------------------------------------------------------------
# 3. ORPHAN -- tv
# ---------------------------------------------------------------------------


def test_orphan_tv_arms_then_pages(tmp_path, stack):
    s = stack()
    rows = [media_row(1, "tv", 700, 5, tvdb=7700)]
    _wire_default(s, media_rows=rows, tv_details={700: tv_detail(7700, {1: 5})})
    # sonarr/sonarr2 fixtures (from _wire_default) know tvdb 90001/90002 only.
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1, r2 = _run_twice(secrets, tmp_path)
    assert r1.returncode == 0
    assert "orphan=1" in r1.stdout
    assert r2.returncode == 1
    assert "orphan-tv" in r2.stderr
    assert "tvdb=7700" in r2.stderr


# ---------------------------------------------------------------------------
# 4. STRANDED -- the Law & Order class (status 7/DELETED, 0 files)
# ---------------------------------------------------------------------------


def test_stranded_season_status_deleted_arms_then_pages(tmp_path, stack):
    """This is exactly the shape reconcile_seerr's `filter=available` can
    never see: status 7 is DELETED, not available, so a filter scoped to
    'available' rows would never visit this row at all. This canary must
    catch it anyway."""
    s = stack()
    rows = [media_row(1, "tv", 800, 7, tvdb=8800)]
    _wire_default(s, media_rows=rows,
                  tv_details={800: tv_detail(8800, {1: 7})},
                  sonarr=[series(8800, {1: 0})])
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1, r2 = _run_twice(secrets, tmp_path)
    assert r1.returncode == 0
    assert "stranded=1" in r1.stdout
    assert r2.returncode == 1
    assert "stranded" in r2.stderr
    assert "tvdb=8800" in r2.stderr
    assert "season=1" in r2.stderr


def test_stranded_season_status_available_with_zero_files(tmp_path, stack):
    """The second STRANDED shape: Seerr says AVAILABLE (5) but Sonarr holds
    zero files for that season -- equally un-watchable, equally a finding."""
    s = stack()
    rows = [media_row(1, "tv", 810, 5, tvdb=8810)]
    _wire_default(s, media_rows=rows,
                  tv_details={810: tv_detail(8810, {1: 5})},
                  sonarr=[series(8810, {1: 0})])
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1, r2 = _run_twice(secrets, tmp_path)
    assert r1.returncode == 0 and "stranded=1" in r1.stdout
    assert r2.returncode == 1 and "stranded" in r2.stderr


def test_season_with_files_is_not_stranded(tmp_path, stack):
    s = stack()
    rows = [media_row(1, "tv", 820, 7, tvdb=8820)]
    _wire_default(s, media_rows=rows,
                  tv_details={820: tv_detail(8820, {1: 7})},
                  sonarr=[series(8820, {1: 3})])   # files still present
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1, _ = _run_twice(secrets, tmp_path)
    assert r1.returncode == 0
    assert "stranded=0" in r1.stdout


# ---------------------------------------------------------------------------
# 5. UNDERREPORTED -- lower severity, reported but never pages alone
# ---------------------------------------------------------------------------


def test_underreported_never_pages_even_when_confirmed_twice(tmp_path, stack):
    """Status 1/absent while Sonarr shows files is real but lower severity:
    it must show up in the message (never silently dropped) and must NEVER
    by itself flip the exit code to 1, on the first OR the second run."""
    s = stack()
    rows = [media_row(1, "tv", 900, 1, tvdb=9900)]   # Seerr: status 1 (UNKNOWN)
    _wire_default(s, media_rows=rows,
                  tv_details={900: tv_detail(9900, {1: 1})},
                  sonarr=[series(9900, {1: 4})])     # Sonarr: 4 files sitting there
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1, r2 = _run_twice(secrets, tmp_path)
    assert r1.returncode == 0
    assert "underreported=1" in r1.stdout
    assert r2.returncode == 0, (r2.returncode, r2.stdout, r2.stderr)
    assert r2.stdout.startswith("PASS-WARN:")
    assert "confirmed_underreported=1" in r2.stdout


def test_underreported_season_absent_from_seerr_entirely(tmp_path, stack):
    """Sonarr has season 2 and Seerr's season list doesn't even mention it
    (not merely status 1) -- must be treated identically to status 1."""
    s = stack()
    rows = [media_row(1, "tv", 910, 5, tvdb=9910)]
    _wire_default(s, media_rows=rows,
                  tv_details={910: tv_detail(9910, {1: 5})},   # only season 1 known to Seerr
                  sonarr=[series(9910, {1: 5, 2: 3})])          # Sonarr also has season 2
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1, _ = _run_twice(secrets, tmp_path)
    assert r1.returncode == 0
    assert "underreported=1" in r1.stdout   # season 2 only
    assert "stranded=0" in r1.stdout


# ---------------------------------------------------------------------------
# 6. Counted-and-named exclusions (rule 4)
# ---------------------------------------------------------------------------


def test_not_settled_statuses_excluded_and_counted(tmp_path, stack):
    s = stack()
    rows = [media_row(i, "movie", 1000 + i, status)
            for i, status in enumerate(NOT_SETTLED, start=1)]
    _wire_default(s, media_rows=rows)   # none of these tmdbIds are in any radarr
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1, _ = _run_twice(secrets, tmp_path)
    assert r1.returncode == 0
    assert "orphan=0 stranded=0" in r1.stdout   # nothing evaluated -- all excluded
    for code in NOT_SETTLED:
        assert ("not-settled-status-%d" % code) in r1.stdout


def test_within_grace_window_excluded_and_never_confirms(tmp_path, stack):
    s = stack()
    rows = [media_row(1, "movie", 1100, 5, updated=FRESH_STAMP)]  # after NOW
    _wire_default(s, media_rows=rows)
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1, r2 = _run_twice(secrets, tmp_path)
    assert r1.returncode == 0 and "within-grace-window" in r1.stdout
    assert r2.returncode == 0 and "within-grace-window" in r2.stdout
    assert "orphan=0" in r1.stdout and "orphan=0" in r2.stdout


def test_unparseable_updated_at_withheld_and_counted(tmp_path, stack):
    """Fail closed: a row whose age cannot be determined is withheld, not
    guessed old or new -- spec section 5."""
    s = stack()
    rows = [media_row(1, "movie", 1200, 5, updated="not-a-timestamp")]
    _wire_default(s, media_rows=rows)
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1, _ = _run_twice(secrets, tmp_path)
    assert r1.returncode == 0
    assert "updatedat-unparseable" in r1.stdout
    assert "orphan=0" in r1.stdout


def test_season_unknown_to_sonarr_is_skipped_not_a_finding(tmp_path, stack):
    """Seerr lists a season number Sonarr has never heard of (not yet
    released, or a numbering mismatch) -- must be a named skip, not a
    STRANDED or UNDERREPORTED verdict fabricated from nothing."""
    s = stack()
    rows = [media_row(1, "tv", 1300, 5, tvdb=13300)]
    # Season 1 matches on both sides (no finding); season 9 is a Seerr season
    # Sonarr has never heard of -- that one alone must produce the skip.
    _wire_default(s, media_rows=rows,
                  tv_details={1300: tv_detail(13300, {1: 5, 9: 5})},
                  sonarr=[series(13300, {1: 5})])
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1, _ = _run_twice(secrets, tmp_path)
    assert r1.returncode == 0
    assert "season-unknown-to-sonarr" in r1.stdout
    assert "orphan=0 stranded=0 underreported=0" in r1.stdout


def test_tv_row_with_no_resolvable_tvdbid_is_withheld(tmp_path, stack):
    s = stack()
    rows = [media_row(1, "tv", 1400, 5)]   # no tvdbId on the row
    _wire_default(s, media_rows=rows, tv_details={1400: {}})  # detail has none either
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1, _ = _run_twice(secrets, tmp_path)
    assert r1.returncode == 0
    assert "tv-no-tvdbid-resolvable" in r1.stdout
    assert "orphan=0" in r1.stdout


def test_unknown_media_type_is_skipped(tmp_path, stack):
    s = stack()
    rows = [{"id": 1, "mediaType": "collection", "tmdbId": 1, "status": 5, "updatedAt": OLD_STAMP}]
    _wire_default(s, media_rows=rows)
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1, _ = _run_twice(secrets, tmp_path)
    assert r1.returncode == 0
    assert "unknown-media-type-collection" in r1.stdout


# ---------------------------------------------------------------------------
# 7. Pagination
# ---------------------------------------------------------------------------


def test_pagination_walks_every_page_until_a_short_one(tmp_path, stack):
    s = stack()
    # 3 movie rows, page size forced to 2 -> pages of [2, 1]. Both rows that
    # can never resolve stay orphans so the walk is visible in the count.
    rows = [media_row(i, "movie", 5000 + i, 5) for i in range(1, 4)]
    _wire_default(s, media_rows=rows)
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1 = _run(secrets, state=tmp_path / "s.json", trail=tmp_path / "t.log",
              QFLIX_CANARY_SAP_PAGE_SIZE="2")
    assert r1.returncode == 0, (r1.returncode, r1.stdout, r1.stderr)
    assert "orphan=3" in r1.stdout
    media_hits = [h for h in s.hits if h.startswith("/api/v1/media")]
    assert len(media_hits) == 2   # one full page (2 rows) + one short page (1 row)


# ---------------------------------------------------------------------------
# 8. Exit-code trichotomy -- CANNOT-ASSERT negative controls
# ---------------------------------------------------------------------------


def test_missing_secrets_is_broken(tmp_path, stack):
    s = stack()
    _wire_default(s, media_rows=[])
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port},
                       omit=("seerr.key",))
    r = _run(secrets, state=tmp_path / "s.json", trail=tmp_path / "t.log")
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "STAGE=seerr-arr-parity-config-missing" in r.stderr
    assert "seerr.key" in r.stderr


def test_seerr_unreachable_is_broken(tmp_path, stack):
    s = stack()  # up, but we point seerr.port at a closed port instead
    _wire_default(s, media_rows=[])
    secrets = _secrets(tmp_path, _closed_port(),
                       {"sonarr": s.port, "sonarr2": s.port, "radarr": s.port, "radarr2": s.port})
    r = _run(secrets, state=tmp_path / "s.json", trail=tmp_path / "t.log",
             QFLIX_CANARY_SAP_TIMEOUT_S="2")
    assert r.returncode == 2
    assert "STAGE=seerr-arr-parity-seerr-unreachable" in r.stderr


def test_seerr_empty_media_list_is_broken_never_a_vacuous_pass(tmp_path, stack):
    s = stack()
    _wire_default(s, media_rows=[])
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r = _run(secrets, state=tmp_path / "s.json", trail=tmp_path / "t.log")
    assert r.returncode == 2
    assert "STAGE=seerr-arr-parity-seerr-empty" in r.stderr


def test_one_arr_unreachable_is_broken(tmp_path, stack):
    s = stack()
    _wire_default(s, media_rows=[media_row(1, "movie", 1, 5)])
    secrets = _secrets(tmp_path, s.port, {
        "sonarr": s.port, "sonarr2": s.port, "radarr": s.port, "radarr2": _closed_port()})
    r = _run(secrets, state=tmp_path / "s.json", trail=tmp_path / "t.log",
             QFLIX_CANARY_SAP_TIMEOUT_S="2")
    assert r.returncode == 2
    assert "STAGE=seerr-arr-parity-arr-unreachable" in r.stderr
    assert "radarr2" in r.stderr


def test_one_arr_empty_is_a_counted_skip_not_a_page(tmp_path, stack):
    """Round-3 live run: sonarr2 (Anime) legitimately had zero series after
    its only show was reaped; one empty single-library instance must not
    red the canary every hour."""
    s = stack()
    _wire_default(s, media_rows=[media_row(1, "movie", 1, 5)], sonarr2=[])
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r = _run(secrets, state=tmp_path / "s.json", trail=tmp_path / "t.log")
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "arr-empty:sonarr2" in r.stdout


def test_all_arrs_empty_is_broken(tmp_path, stack):
    s = stack()
    _wire_default(s, media_rows=[media_row(1, "movie", 1, 5)],
                  sonarr=[], sonarr2=[], radarr=[], radarr2=[])
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r = _run(secrets, state=tmp_path / "s.json", trail=tmp_path / "t.log")
    assert r.returncode == 2
    assert "STAGE=seerr-arr-parity-arr-empty" in r.stderr


def test_tv_detail_fetch_failure_is_a_named_skip_not_a_broken_run(tmp_path, stack):
    """A single title's detail lookup 404ing (e.g. the tmdbId went stale)
    must not take down the whole run -- only that row is withheld."""
    s = stack()
    rows = [media_row(1, "tv", 1500, 5, tvdb=15500)]
    _wire_default(s, media_rows=rows, tv_details={})  # 1500 not in the detail map -> {}
    # Give it no tvdbId on the row either? row HAS tvdb=15500 already resolvable
    # from the media row itself even if the tv detail body is empty, so force a
    # real fetch failure via a 500 instead.
    s.route("/api/v1/tv/1500", {}, key=SEERR_KEY, status=500)
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1, _ = _run_twice(secrets, tmp_path)
    assert r1.returncode == 0, (r1.returncode, r1.stdout, r1.stderr)
    assert "tv-detail-fetch-failed" in r1.stdout


# ---------------------------------------------------------------------------
# 9. Bad numeric overrides -- cannot-assert, never a silently-disabled probe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env_name,bad_value", [
    ("QFLIX_CANARY_SAP_GRACE_H", "-1"),
    ("QFLIX_CANARY_SAP_GRACE_H", "0"),
    ("QFLIX_CANARY_SAP_GRACE_H", "0.5"),
    ("QFLIX_CANARY_SAP_GRACE_H", "abc"),
    ("QFLIX_CANARY_SAP_TIMEOUT_S", "0"),
    ("QFLIX_CANARY_SAP_PAGE_SIZE", "-5"),
    ("QFLIX_CANARY_SAP_MAX_PAGES", ""),
    ("QFLIX_CANARY_SAP_RETRIES", "-1"),
])
def test_bad_numeric_override_is_cannot_assert(tmp_path, stack, env_name, bad_value):
    """REQ-CLAMP-style exercise: -1, 0, 0.0-shaped and non-numeric values all
    have to be rejected, not just a single pinned case that happens to work."""
    s = stack()
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r = _run(secrets, state=tmp_path / "s.json", trail=tmp_path / "t.log",
             **{env_name: bad_value})
    assert r.returncode == 2, (env_name, bad_value, r.returncode, r.stdout, r.stderr)
    assert "STAGE=seerr-arr-parity-bad-config" in r.stderr
    assert env_name in r.stderr


def test_retries_zero_is_a_valid_non_negative_value(tmp_path, stack):
    s = stack()
    _wire_default(s, media_rows=[media_row(1, "movie", 1, 5)], radarr=[{"tmdbId": 1}])
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r = _run(secrets, state=tmp_path / "s.json", trail=tmp_path / "t.log",
             QFLIX_CANARY_SAP_RETRIES="0")
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)


# ---------------------------------------------------------------------------
# 10. Durable trail
# ---------------------------------------------------------------------------


def test_durable_trail_is_appended_every_run(tmp_path, stack):
    s = stack()
    _wire_default(s, media_rows=[media_row(1, "movie", 1, 5)], radarr=[{"tmdbId": 1}])
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    trail = tmp_path / "trail.log"
    _run(secrets, state=tmp_path / "s.json", trail=trail)
    _run(secrets, state=tmp_path / "s.json", trail=trail)
    assert trail.exists()
    lines = [ln for ln in trail.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2
    assert all(ln.startswith("20") for ln in lines)  # ISO timestamp prefix


# ---------------------------------------------------------------------------
# 11. tvdbId collision -- UNTRUSTED, never "keep the first instance"
# ---------------------------------------------------------------------------


def test_tvdbid_collision_marks_untrusted_and_withholds_the_row(tmp_path, stack):
    """2026-09-13 council finding (MAJOR, boundary): keeping the first-seen
    sonarr instance's (stale, zero-file) season map for a duplicated tvdbId
    and running the STRANDED predicate against it produced a false page for
    a season that unambiguously has files in the OTHER instance. The tvdbId
    must be marked UNTRUSTED and withheld entirely -- no orphan, no
    stranded, no underreported -- for as long as the collision persists,
    across both consecutive runs."""
    s = stack()
    rows = [media_row(1, "tv", 5550, 5, tvdb=555)]
    _wire_default(s, media_rows=rows,
                  tv_details={5550: tv_detail(555, {1: 5})},
                  sonarr=[series(555, {1: 0})],    # stale/wrong if ever trusted
                  sonarr2=[series(555, {1: 5})])   # the real, file-holding instance
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1, r2 = _run_twice(secrets, tmp_path)
    assert r1.returncode == 0, (r1.returncode, r1.stdout, r1.stderr)
    assert "tvdbid-collision-across-sonarr-instances" in r1.stdout
    assert "tvdbid-untrusted-row-excluded" in r1.stdout
    assert "orphan=0 stranded=0 underreported=0" in r1.stdout
    # Must stay withheld on the SECOND run too -- the collision persists, so
    # nothing about it should ever page, on any run.
    assert r2.returncode == 0, (r2.returncode, r2.stdout, r2.stderr)
    assert "orphan=0 stranded=0 underreported=0" in r2.stdout


def test_tvdbid_collision_does_not_orphan_the_untrusted_id_either(tmp_path, stack):
    """The untrusted-id exclusion must fire BEFORE the orphan check -- a
    duplicated tvdbId dropped from sonarr_tvdb must not fall through and
    read as 'absent from Sonarr entirely' (a false ORPHAN) either."""
    s = stack()
    rows = [media_row(1, "tv", 5560, 5, tvdb=556)]
    _wire_default(s, media_rows=rows,
                  tv_details={5560: tv_detail(556, {1: 5})},
                  sonarr=[series(556, {1: 5})],
                  sonarr2=[series(556, {1: 5})])
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1, _ = _run_twice(secrets, tmp_path)
    assert r1.returncode == 0
    assert "orphan=0" in r1.stdout
    assert "tvdbid-untrusted-row-excluded" in r1.stdout


# ---------------------------------------------------------------------------
# 12. State-file corruption -- a named, counted skip, never a silent reset
# ---------------------------------------------------------------------------


def test_state_file_corruption_is_a_named_skip_and_still_arms(tmp_path, stack):
    """2026-09-13 council finding (MAJOR, false-green): a state file that
    fails to parse as JSON used to be treated identically to 'no file yet'
    with zero trace anywhere, silently resetting the two-run confirmation
    gate. It must now be a NAMED, COUNTED skip with its own trail line, and
    the run must still ARM (not silently drop back to a false clean pass
    forever) -- proven by the SECOND run against the now-valid state file
    actually paging."""
    s = stack()
    rows = [media_row(1, "movie", 999, 5)]   # 999 is in no radarr -- an orphan
    _wire_default(s, media_rows=rows)
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    state = tmp_path / "state.json"
    trail = tmp_path / "trail.log"
    state.write_text("{not json", encoding="utf-8")

    r1 = _run(secrets, state=state, trail=trail)
    assert r1.returncode == 0, (r1.returncode, r1.stdout, r1.stderr)
    assert "state-file-corrupt-or-unreadable" in r1.stdout
    assert "orphan=1" in r1.stdout
    trail_text = trail.read_text(encoding="utf-8")
    assert "STATE-CORRUPT" in trail_text

    # The gate was NOT silently reset to a permanent clean pass: run 1 wrote
    # a valid state file, so run 2 against it must confirm-and-page.
    r2 = _run(secrets, state=state, trail=trail)
    assert r2.returncode == 1, (r2.returncode, r2.stdout, r2.stderr)


def test_missing_state_file_is_not_treated_as_corruption(tmp_path, stack):
    """The expected first-deploy shape (no state file yet) must NOT be
    counted as the corruption skip -- only a file that exists and fails to
    parse or open is corruption."""
    s = stack()
    _wire_default(s, media_rows=[media_row(1, "movie", 1, 5)], radarr=[{"tmdbId": 1}])
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    state = tmp_path / "does-not-exist" / "state.json"
    r1 = _run(secrets, state=state, trail=tmp_path / "t.log")
    assert r1.returncode == 0, (r1.returncode, r1.stdout, r1.stderr)
    assert "state-file-corrupt-or-unreadable" not in r1.stdout


# ---------------------------------------------------------------------------
# 13. Persistent per-title tv-detail failure -- escalates, never masks forever
# ---------------------------------------------------------------------------


def test_persistent_tv_detail_failure_escalates_to_cannot_assert(tmp_path, stack):
    """2026-09-13 council finding (MAJOR, false-green): a Seerr title whose
    detail endpoint returns 500 EVERY run forever used to mask a genuine
    STRANDED season behind an ever-incrementing skip counter that nothing
    ever pages on. Per operator ruling, 3 consecutive runs against the same
    tmdbId must escalate the whole run to CANNOT-ASSERT, naming the id."""
    s = stack()
    rows = [media_row(1, "tv", 1600, 7, tvdb=16600)]   # a genuine STRANDED shape
    _wire_default(s, media_rows=rows, tv_details={}, sonarr=[series(16600, {1: 0})])
    s.route("/api/v1/tv/1600", {}, key=SEERR_KEY, status=500)
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    state = tmp_path / "state.json"
    trail = tmp_path / "trail.log"

    r1 = _run(secrets, state=state, trail=trail)
    r2 = _run(secrets, state=state, trail=trail)
    r3 = _run(secrets, state=state, trail=trail)
    r4 = _run(secrets, state=state, trail=trail)

    assert r1.returncode == 0 and "tv-detail-fetch-failed" in r1.stdout
    assert r2.returncode == 0 and "tv-detail-fetch-failed" in r2.stdout
    assert r3.returncode == 2, (r3.returncode, r3.stdout, r3.stderr)
    assert "STAGE=seerr-arr-parity-tv-detail-persistent-failure" in r3.stderr
    assert "tmdb=1600" in r3.stderr
    assert "consecutive=3" in r3.stderr
    # Stays escalated on the 4th run too -- the id is still failing.
    assert r4.returncode == 2, (r4.returncode, r4.stdout, r4.stderr)


def test_persistent_tv_detail_failure_counter_resets_on_one_success(tmp_path, stack):
    """A single successful lookup must reset the consecutive-failure counter
    to zero -- two more failures right after must NOT immediately escalate."""
    s = stack()
    rows = [media_row(1, "tv", 1650, 5, tvdb=16650)]
    calls = {"fail": True}

    def _detail(_path, _qs):
        if calls["fail"]:
            return (500, {})
        return tv_detail(16650, {1: 5})

    _wire_default(s, media_rows=rows, sonarr=[series(16650, {1: 5})])
    s.route("/api/v1/tv/1650", _detail, key=SEERR_KEY)
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    state = tmp_path / "state.json"
    trail = tmp_path / "trail.log"

    r1 = _run(secrets, state=state, trail=trail)          # failure 1
    r2 = _run(secrets, state=state, trail=trail)          # failure 2
    calls["fail"] = False
    r3 = _run(secrets, state=state, trail=trail)          # success -- resets
    calls["fail"] = True
    r4 = _run(secrets, state=state, trail=trail)          # failure 1 (again)
    r5 = _run(secrets, state=state, trail=trail)          # failure 2 (again)

    for r in (r1, r2, r3, r4, r5):
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)


# ---------------------------------------------------------------------------
# 14. Pagination boundary -- an exact page*cap multiple is a clean stop
# ---------------------------------------------------------------------------


def test_pagination_exact_max_pages_boundary_is_a_clean_pass(tmp_path, stack):
    """2026-09-13 council finding (MINOR, boundary): exactly MAX_PAGES full
    pages with no short final page used to manufacture a false CANNOT-ASSERT
    even though Seerr answered every request correctly. One confirmation
    request after the cap must distinguish a genuinely empty trailing page
    (clean stop) from real overflow."""
    s = stack()
    rows = [media_row(i, "movie", 6000 + i, 5) for i in range(1, 4)]   # exactly 3
    _wire_default(s, media_rows=rows, radarr=[{"tmdbId": 6000 + i} for i in range(1, 4)])
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1 = _run(secrets, state=tmp_path / "s.json", trail=tmp_path / "t.log",
              QFLIX_CANARY_SAP_PAGE_SIZE="1", QFLIX_CANARY_SAP_MAX_PAGES="3")
    assert r1.returncode == 0, (r1.returncode, r1.stdout, r1.stderr)
    assert "orphan=0" in r1.stdout
    media_hits = [h for h in s.hits if h.startswith("/api/v1/media")]
    assert len(media_hits) == 4   # 3 full pages + 1 confirmation page that came back empty


def test_pagination_genuine_overflow_beyond_max_pages_is_labelled_correctly(tmp_path, stack):
    """A page AFTER the cap that comes back NON-empty is real overflow -- it
    must use its own STAGE (`-pagination-overflow`), not the generic
    `-seerr-unreachable`, because Seerr answered every request fine."""
    s = stack()
    rows = [media_row(i, "movie", 7000 + i, 5) for i in range(1, 5)]   # 4 -- one past the cap
    _wire_default(s, media_rows=rows)
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    r1 = _run(secrets, state=tmp_path / "s.json", trail=tmp_path / "t.log",
              QFLIX_CANARY_SAP_PAGE_SIZE="1", QFLIX_CANARY_SAP_MAX_PAGES="3")
    assert r1.returncode == 2, (r1.returncode, r1.stdout, r1.stderr)
    assert "STAGE=seerr-arr-parity-pagination-overflow" in r1.stderr
    assert "seerr-arr-parity-seerr-unreachable" not in r1.stderr


# ---------------------------------------------------------------------------
# 15. ROUND 2 (2026-09-13 council, MAJOR, finding #1) -- persistent state-file
# corruption escalates rather than being forgiven forever.
# ---------------------------------------------------------------------------


def test_state_file_corruption_persistent_3_runs_escalates_to_cannot_assert(tmp_path, stack):
    """'Arm this run' is correct for a ONE-OFF state-file corruption, but a
    file that fails EVERY run forever (permissions regression, disk gone
    read-only, a bad file dropped in place and never fixed) used to be
    forgiven identically every single time -- prev_findings is wiped on
    every corrupt read, so the two-run confirmation gate could never fire
    again. This test re-corrupts state.json before each invocation to
    simulate a persistent EXTERNAL cause: the script itself always tries to
    write a fresh, valid file at the end of a run, so only an external actor
    re-breaking it produces a genuine streak."""
    s = stack()
    rows = [media_row(1, "movie", 999, 5)]   # irrelevant to this escalation
    _wire_default(s, media_rows=rows)
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    state = tmp_path / "state.json"
    trail = tmp_path / "trail.log"

    state.write_text("{not json", encoding="utf-8")
    r1 = _run(secrets, state=state, trail=trail)
    assert r1.returncode == 0, (r1.returncode, r1.stdout, r1.stderr)

    state.write_text("{not json", encoding="utf-8")
    r2 = _run(secrets, state=state, trail=trail)
    assert r2.returncode == 0, (r2.returncode, r2.stdout, r2.stderr)

    state.write_text("{not json", encoding="utf-8")
    r3 = _run(secrets, state=state, trail=trail)
    assert r3.returncode == 2, (r3.returncode, r3.stdout, r3.stderr)
    assert "STAGE=seerr-arr-parity-state-corrupt-persistent" in r3.stderr
    assert "consecutive=3" in r3.stderr

    # Stays escalated on a 4th consecutive corrupt run too -- the cause is
    # still live.
    state.write_text("{not json", encoding="utf-8")
    r4 = _run(secrets, state=state, trail=trail)
    assert r4.returncode == 2, (r4.returncode, r4.stdout, r4.stderr)
    assert "consecutive=4" in r4.stderr


def test_state_corrupt_streak_resets_after_one_valid_read(tmp_path, stack):
    """A single successful state-file read must reset the corrupt-streak to
    zero -- two more corruptions right after must NOT immediately
    escalate."""
    s = stack()
    rows = [media_row(1, "movie", 999, 5)]
    _wire_default(s, media_rows=rows)
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    state = tmp_path / "state.json"
    trail = tmp_path / "trail.log"

    state.write_text("{not json", encoding="utf-8")
    r1 = _run(secrets, state=state, trail=trail)          # corrupt streak 1
    state.write_text("{not json", encoding="utf-8")
    r2 = _run(secrets, state=state, trail=trail)          # corrupt streak 2
    # No re-corruption before r3: the valid file the script itself wrote at
    # the end of r2 is read cleanly, resetting the streak.
    r3 = _run(secrets, state=state, trail=trail)          # valid read -- resets
    state.write_text("{not json", encoding="utf-8")
    r4 = _run(secrets, state=state, trail=trail)          # corrupt streak 1 (again)
    state.write_text("{not json", encoding="utf-8")
    r5 = _run(secrets, state=state, trail=trail)          # corrupt streak 2 (again)

    assert r1.returncode == 0, (r1.returncode, r1.stdout, r1.stderr)
    assert r2.returncode == 0, (r2.returncode, r2.stdout, r2.stderr)
    # The orphan is now confirmed against the valid state r2 left behind --
    # a real finding, not an escalation, and proof the streak did not carry
    # over to accidentally hit 3 on the very next (valid) read.
    assert r3.returncode == 1, (r3.returncode, r3.stdout, r3.stderr)
    assert r4.returncode == 0, (r4.returncode, r4.stdout, r4.stderr)
    assert r5.returncode == 0, (r5.returncode, r5.stdout, r5.stderr)


# ---------------------------------------------------------------------------
# 16. ROUND 2 (2026-09-13 council, MAJOR, finding #2) -- wrong-shaped state
# JSON must not bypass the corruption path.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_shape", [
    "[]",
    "null",
    '{"findings": [1, 2], "tv_detail_failures": {}}',
    '{"findings": {}, "tv_detail_failures": "not-a-dict"}',
    '{"findings": {}, "tv_detail_failures": {}, "tvdbid_collisions": [1]}',
])
def test_state_file_wrong_shape_is_treated_as_corrupt_not_bypassed(tmp_path, stack, bad_shape):
    """A state file that is valid JSON but the WRONG SHAPE -- a list,
    `null`, or a dict whose findings/tv_detail_failures/tvdbid_collisions
    are not themselves dicts -- used to parse clean through `json.load` and
    sail straight past the corruption guard into `.get()` calls downstream.
    Every one of these shapes must route through the identical
    skip('state-file-corrupt-or-unreadable') + STATE-CORRUPT trail path as a
    genuine parse failure, and the run must still arm rather than misbehave
    or crash."""
    s = stack()
    rows = [media_row(1, "movie", 999, 5)]   # 999 is in no radarr -- an orphan
    _wire_default(s, media_rows=rows)
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    state = tmp_path / "state.json"
    trail = tmp_path / "trail.log"
    state.write_text(bad_shape, encoding="utf-8")

    r1 = _run(secrets, state=state, trail=trail)
    assert r1.returncode == 0, (bad_shape, r1.returncode, r1.stdout, r1.stderr)
    assert "state-file-corrupt-or-unreadable" in r1.stdout
    assert "orphan=1" in r1.stdout
    trail_text = trail.read_text(encoding="utf-8")
    assert "STATE-CORRUPT" in trail_text

    # The gate was not silently reset to a permanent clean pass -- the same
    # orphan confirms on the next run against the now-VALID state the
    # script itself wrote at the end of r1.
    r2 = _run(secrets, state=state, trail=trail)
    assert r2.returncode == 1, (bad_shape, r2.returncode, r2.stdout, r2.stderr)


# ---------------------------------------------------------------------------
# 17. ROUND 2 (2026-09-13 council, MAJOR, finding #3) -- persistent tvdbId
# collision escalates rather than being withheld forever.
# ---------------------------------------------------------------------------


def test_tvdbid_collision_persistent_3_runs_escalates_to_cannot_assert(tmp_path, stack):
    """A tvdbId reported by BOTH sonarr instances is UNTRUSTED and withheld
    from every per-row assertion, but that withholding used to last exactly
    as long as the collision did with no escalation of its own -- a show
    genuinely living in both instances forever would be silently
    unassertable forever, which is operator drift that must be surfaced."""
    s = stack()
    rows = [media_row(1, "tv", 5550, 5, tvdb=555)]
    _wire_default(s, media_rows=rows,
                  tv_details={5550: tv_detail(555, {1: 5})},
                  sonarr=[series(555, {1: 0})],
                  sonarr2=[series(555, {1: 5})])
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    state = tmp_path / "state.json"
    trail = tmp_path / "trail.log"

    r1 = _run(secrets, state=state, trail=trail)
    r2 = _run(secrets, state=state, trail=trail)
    r3 = _run(secrets, state=state, trail=trail)
    r4 = _run(secrets, state=state, trail=trail)

    assert r1.returncode == 0 and "tvdbid-collision-across-sonarr-instances" in r1.stdout
    assert r2.returncode == 0 and "tvdbid-collision-across-sonarr-instances" in r2.stdout
    assert r3.returncode == 2, (r3.returncode, r3.stdout, r3.stderr)
    assert "STAGE=seerr-arr-parity-tvdbid-collision-persistent" in r3.stderr
    assert "tvdb=555" in r3.stderr
    assert "consecutive=3" in r3.stderr
    # Stays escalated on a 4th run too -- the collision is still live.
    assert r4.returncode == 2, (r4.returncode, r4.stdout, r4.stderr)


def test_tvdbid_collision_counter_resets_when_collision_stops(tmp_path, stack):
    """A run where the collision does not reproduce must reset the counter
    to zero -- two more collisions right after must NOT immediately
    escalate."""
    s = stack()
    rows = [media_row(1, "tv", 5560, 5, tvdb=556)]
    colliding = {"on": True}

    def _sonarr2(_path, _qs):
        return [series(556, {1: 5})] if colliding["on"] else [series(90099, {1: 1})]

    _wire_default(s, media_rows=rows, tv_details={5560: tv_detail(556, {1: 5})},
                  sonarr=[series(556, {1: 5})])
    s.route("/sonarr2/api/v3/series", _sonarr2, key=ARR_KEYS["sonarr2"])
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    state = tmp_path / "state.json"
    trail = tmp_path / "trail.log"

    r1 = _run(secrets, state=state, trail=trail)          # collision streak 1
    r2 = _run(secrets, state=state, trail=trail)          # collision streak 2
    colliding["on"] = False
    r3 = _run(secrets, state=state, trail=trail)          # no collision -- resets
    colliding["on"] = True
    r4 = _run(secrets, state=state, trail=trail)          # collision streak 1 (again)
    r5 = _run(secrets, state=state, trail=trail)          # collision streak 2 (again)

    for r in (r1, r2, r3, r4, r5):
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)


# ---------------------------------------------------------------------------
# 18. ROUND 2 (2026-09-13 council, MAJOR, finding #4) -- a confirmed
# orphan/stranded finding must never be masked by an escalation firing in
# the same run.
# ---------------------------------------------------------------------------


def test_tv_detail_persistent_failure_does_not_mask_a_confirmed_finding(tmp_path, stack):
    """Confirmed findings must be computed BEFORE any escalation gets to
    decide the exit code. An orphan movie confirmed across two runs,
    alongside an UNRELATED tmdbId whose tv-detail lookup has failed 3
    consecutive runs, must still exit 1 naming the orphan -- not 2, which
    would silently hide a finding already proven true behind a
    CANNOT-ASSERT. The escalation itself must still be logged (named skip +
    trail line), just outranked."""
    s = stack()
    rows = [
        media_row(1, "movie", 9001, 5),            # becomes a confirmed orphan
        media_row(2, "tv", 1600, 7, tvdb=16600),    # detail lookup always 500s
    ]
    _wire_default(s, media_rows=rows, tv_details={}, sonarr=[series(16600, {1: 0})])
    s.route("/api/v1/tv/1600", {}, key=SEERR_KEY, status=500)
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    state = tmp_path / "state.json"
    trail = tmp_path / "trail.log"

    r1 = _run(secrets, state=state, trail=trail)
    r2 = _run(secrets, state=state, trail=trail)
    r3 = _run(secrets, state=state, trail=trail)

    assert r1.returncode == 0 and "orphan=1" in r1.stdout
    assert r2.returncode == 1, (r2.returncode, r2.stdout, r2.stderr)

    # r3: the tv-detail failure has now hit its 3rd consecutive run --
    # escalation-worthy on its own -- but the orphan is STILL confirmed, so
    # the finding must win (exit 1, not 2).
    assert r3.returncode == 1, (r3.returncode, r3.stdout, r3.stderr)
    assert "STAGE=seerr-arr-parity msg=" in r3.stderr
    assert "tmdb=9001" in r3.stderr
    assert "tv-detail-persistent-failure-suppressed-by-confirmed-finding" in r3.stderr
    trail_text = trail.read_text(encoding="utf-8")
    assert "ESCALATION-SUPPRESSED-BY-FINDING" in trail_text
    assert "seerr-arr-parity-tv-detail-persistent-failure" in trail_text


def test_tvdbid_collision_persistent_does_not_mask_a_confirmed_finding(tmp_path, stack):
    """Same as above for the tvdbid-collision-persistent escalation: an
    unrelated confirmed orphan must still win the exit code over a
    colliding tvdbId reaching its 3rd consecutive run."""
    s = stack()
    rows = [media_row(1, "movie", 9002, 5)]   # confirmed orphan by run 2
    _wire_default(s, media_rows=rows,
                  sonarr=[series(557, {1: 5})],
                  sonarr2=[series(557, {1: 5})])   # colliding tvdbId, unrelated to the row
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    state = tmp_path / "state.json"
    trail = tmp_path / "trail.log"

    r1 = _run(secrets, state=state, trail=trail)
    r2 = _run(secrets, state=state, trail=trail)
    r3 = _run(secrets, state=state, trail=trail)

    assert r1.returncode == 0 and "orphan=1" in r1.stdout
    assert r2.returncode == 1, (r2.returncode, r2.stdout, r2.stderr)
    assert r3.returncode == 1, (r3.returncode, r3.stdout, r3.stderr)
    assert "tmdb=9002" in r3.stderr
    assert "tvdbid-collision-persistent-suppressed-by-confirmed-finding" in r3.stderr
    trail_text = trail.read_text(encoding="utf-8")
    assert "ESCALATION-SUPPRESSED-BY-FINDING" in trail_text
    assert "seerr-arr-parity-tvdbid-collision-persistent" in trail_text


def test_state_corrupt_with_untrackable_trail_escalates_immediately(tmp_path, stack):
    """ROUND 3 (false-green/BLOCKER): the corruption streak rides on the
    trail; with the trail unreadable/unwritable (parent is a plain file --
    the read-only-disk shape) every run used to re-derive streak=1 and the
    persistent-corruption escalation never fired. Now a corrupt state whose
    streak cannot be tracked is fail-closed on the FIRST run."""
    s = stack()
    _wire_default(s, media_rows=[media_row(1, "movie", 999, 5)])
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    state = tmp_path / "state.json"
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    trail = blocker / "trail.log"            # parent is a file: unreadable AND unwritable
    for _ in range(2):
        state.write_text("{not json", encoding="utf-8")
        r = _run(secrets, state=state, trail=trail)
        assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
        assert "STAGE=seerr-arr-parity-state-corrupt-persistent" in r.stderr
        assert "streak-untrackable" in r.stderr


def test_healthy_state_with_unwritable_trail_is_not_widened_into_a_page(tmp_path, stack):
    """The round-3 fail-closed leg is scoped to CORRUPT state: a healthy
    state file with a broken trail loses bookkeeping, not correctness."""
    s = stack()
    _wire_default(s, media_rows=[media_row(1, "movie", 999, 5)])
    secrets = _secrets(tmp_path, s.port, {"sonarr": s.port, "sonarr2": s.port,
                                          "radarr": s.port, "radarr2": s.port})
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    r = _run(secrets, state=tmp_path / "state.json", trail=blocker / "trail.log")
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
