"""Tests for scripts/canaries/vlogs-time-integrity.sh (QFLX-44).

Drives the shipped script (real bash + python3) against a fake VictoriaLogs on
loopback. The fake answers by looking at the LogsQL text, so a regression in
the query shape shows up as a wrong answer here, not only on the box.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "canaries" / "vlogs-time-integrity.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="needs bash + python3 on PATH",
)


class Fake:
    def __init__(self):
        self.future = []        # rows for the FUTURE stats query
        self.top_time = None    # answer to the max-ahead query
        self.dups = []          # rows for the DUPLICATE query
        self.latest = []        # rows for the SKEW query
        self.fail = set()       # {"future","dup","top","skew"} -> 500
        self.health = 200
        self.queries = []
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_GET(self):                                   # noqa: N802
                u = urlparse(self.path)
                if u.path == "/health":
                    return self._send(fake.health, b"ok")
                qs = {k: v[0] for k, v in parse_qs(u.query).items()}
                q = qs.get("query", "")
                fake.queries.append((q, qs))
                if "_msg" in q and "filter n:>1" in q:
                    kind, rows = "dup", fake.dups
                elif "max(_time)" in q:
                    kind, rows = "skew", fake.latest
                elif "sort by (_time desc)" in q:
                    kind = "top"
                    rows = [{"_time": fake.top_time}] if fake.top_time else []
                else:
                    kind, rows = "future", fake.future
                if kind in fake.fail:
                    return self._send(500, b"boom")
                self._send(200, "\n".join(json.dumps(r) for r in rows).encode())

            def _send(self, code, body):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.stop()


def run(fake, tmp_path, **env):
    import os
    e = {**os.environ,
         "QFLIX_CANARY_VLTI_URL": fake.url,
         "QFLIX_CANARY_VLTI_LOG": str(tmp_path / "vlti.log"),
         "MANITOBA_SECRETS": str(tmp_path)}
    e.update(env)
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                          env=e, timeout=120)


def test_clean_index_is_green(fake, tmp_path):
    r = run(fake, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "ok future=0 dup_groups=0 skewed=0" in r.stdout


def test_future_dated_events_are_red_and_name_the_app(fake, tmp_path):
    from datetime import datetime, timedelta, timezone
    ahead = (datetime.now(timezone.utc) + timedelta(minutes=118)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    fake.future = [{"app": "listmonk", "n": "7"}, {"app": "sonarr", "n": 3}]
    fake.top_time = ahead
    r = run(fake, tmp_path)
    assert r.returncode == 1
    assert "STAGE=vlti-future-time" in r.stderr
    assert "listmonk:7" in r.stderr and "sonarr:3" in r.stderr
    assert "max-ahead=11" in r.stderr           # ~117-118 minutes ahead
    assert "listmonk:7" in (tmp_path / "vlti.log").read_text()


def test_future_query_window_starts_after_tolerance(fake, tmp_path):
    from datetime import datetime, timedelta, timezone
    run(fake, tmp_path)
    q, qs = fake.queries[0]
    start = datetime.strptime(qs["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert timedelta(minutes=1) < start - datetime.now(timezone.utc) < timedelta(minutes=3)


def test_duplicate_rows_are_red_and_name_the_app(fake, tmp_path):
    fake.dups = [{"app": "qbittorrent", "groups": "4", "rows": "9"}]
    r = run(fake, tmp_path)
    assert r.returncode == 1
    assert "STAGE=vlti-duplicates" in r.stderr
    assert "qbittorrent:4groups/9rows" in r.stderr


def test_dup_tolerance_override(fake, tmp_path):
    fake.dups = [{"app": "qbittorrent", "groups": "2", "rows": "4"}]
    assert run(fake, tmp_path, QFLIX_CANARY_VLTI_DUP_MAX="2").returncode == 0
    assert run(fake, tmp_path, QFLIX_CANARY_VLTI_DUP_MAX="1").returncode == 1


def test_both_findings_reported_together(fake, tmp_path):
    fake.future = [{"app": "radarr", "n": 1}]
    fake.dups = [{"app": "plex", "groups": 1, "rows": 2}]
    r = run(fake, tmp_path)
    assert r.returncode == 1
    assert "vlti-future-time" in r.stderr and "vlti-duplicates" in r.stderr


def test_output_never_contains_log_text(fake, tmp_path):
    fake.future = [{"app": "plex", "n": 1, "_msg": "SECRET-MEMBER-NAME"}]
    fake.dups = [{"app": "plex", "groups": 1, "rows": 2, "_msg": "SECRET-MEMBER-NAME"}]
    r = run(fake, tmp_path)
    assert "SECRET-MEMBER-NAME" not in r.stdout + r.stderr


def test_unanswerable_query_is_red_not_clean(fake, tmp_path):
    fake.fail = {"future"}
    r = run(fake, tmp_path)
    assert r.returncode == 1 and "STAGE=vlti-query-fail" in r.stderr
    fake.fail = {"dup"}
    r = run(fake, tmp_path)
    assert r.returncode == 1 and "STAGE=vlti-query-fail" in r.stderr


def test_vlogs_down_is_red(fake, tmp_path):
    fake.health = 503
    r = run(fake, tmp_path)
    assert r.returncode == 1 and "STAGE=vlti-down" in r.stderr


def test_missing_port_secret_is_red(tmp_path):
    import os
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "MANITOBA_SECRETS": str(tmp_path),
                            "QFLIX_CANARY_VLTI_URL": "",
                            "QFLIX_CANARY_VLTI_LOG": str(tmp_path / "l")})
    assert r.returncode == 1 and "STAGE=vlti-config-missing" in r.stderr


def _file_with_mtime_age(tmp_path, minutes_ago):
    import os
    import time
    f = tmp_path / "app.log"
    f.write_text("x" + chr(10))
    t = time.time() - minutes_ago * 60
    os.utime(f, (t, t))
    return f


def _iso_ago(minutes):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_skew_behind_file_mtime_is_red(fake, tmp_path):
    """Tautulli/plex/seerr on 2026-10-10: file written 20 min ago, newest
    stored _time two hours older."""
    f = _file_with_mtime_age(tmp_path, 20)
    fake.latest = [{"app": "tautulli", "source_file": str(f), "latest": _iso_ago(140)}]
    r = run(fake, tmp_path)
    assert r.returncode == 1
    assert "STAGE=vlti-time-skew" in r.stderr and "tautulli:12" in r.stderr   # ~120m


def test_skew_within_tolerance_or_idle_file_is_green(fake, tmp_path):
    f = _file_with_mtime_age(tmp_path, 20)
    fake.latest = [{"app": "a", "source_file": str(f), "latest": _iso_ago(35)}]   # 15m lag
    assert run(fake, tmp_path).returncode == 0
    idle = _file_with_mtime_age(tmp_path, 300)                                    # idle 5h
    fake.latest = [{"app": "a", "source_file": str(idle), "latest": _iso_ago(900)}]
    assert run(fake, tmp_path).returncode == 0


def test_skew_ignores_journald_and_missing_files(fake, tmp_path):
    fake.latest = [{"app": "maint-pusher", "source_file": "journalctl:x.service", "latest": _iso_ago(900)},
                   {"app": "gone", "source_file": str(tmp_path / "nope.log"), "latest": _iso_ago(900)}]
    assert run(fake, tmp_path).returncode == 0


def test_skew_query_failure_is_red(fake, tmp_path):
    fake.fail = {"skew"}
    r = run(fake, tmp_path)
    assert r.returncode == 1 and "STAGE=vlti-query-fail" in r.stderr
