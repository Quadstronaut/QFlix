"""scripts/maint/pg_native.py -- the tested decisions behind 312-native-postgres
(QFLX-37, UCC divorce A13).

Pure functions + the CLI the installer calls. The fixture listmonk password is
NOT a credential; every test that handles it asserts it never reaches stdout.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "maint" / "pg_native.py"

_spec = importlib.util.spec_from_file_location("pg_native", SCRIPT)
pg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pg)

PW = 'Fx7p:Q2\\wLm"9Zt'   # colon, backslash and quote: every escape path
CONFIG = f"""[app]
address = "127.0.0.1:42014"

[db]
host = "127.0.0.1"
port = 42009
user = "quadstronaut"
password = {json.dumps(PW)}
database = "listmonk"
ssl_mode = "disable"

[privacy]
x = 1
"""


@pytest.fixture()
def cfg(tmp_path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(CONFIG, encoding="utf-8", newline="\n")
    return p


def cli(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True, timeout=60)


# --- listmonk config -----------------------------------------------------------

def test_lmconf_prints_target_never_the_password(cfg):
    r = cli("lmconf", cfg)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "127.0.0.1|42009|quadstronaut|listmonk"
    assert PW not in r.stdout + r.stderr


def test_lmaddr(cfg):
    assert cli("lmaddr", cfg).stdout.strip() == "127.0.0.1:42014"


@pytest.mark.parametrize("bad", [
    CONFIG.replace("port = 42009", 'port = "x"'),
    CONFIG.replace('database = "listmonk"', 'database = "list; drop"'),
    CONFIG.replace(f"password = {json.dumps(PW)}", ""),
    "not toml [[[",
])
def test_lmconf_fails_closed(tmp_path, bad):
    p = tmp_path / "c.toml"
    p.write_text(bad, encoding="utf-8")
    r = cli("lmconf", p)
    assert r.returncode == 2
    assert PW not in r.stdout + r.stderr


def test_pgpass_is_0600_and_escaped(cfg, tmp_path):
    out = tmp_path / "sw" / ".pgpass"
    r = cli("pgpass", cfg, out)
    assert r.returncode == 0, r.stderr
    assert out.read_text() == "*:*:*:quadstronaut:Fx7p\\:Q2\\\\wLm\"9Zt\n"
    if os.name == "posix":
        assert (out.stat().st_mode & 0o777) == 0o600
    assert PW not in r.stdout + r.stderr


def test_scratch_toml_points_listmonk_at_the_proof_only(cfg, tmp_path):
    out = tmp_path / "lm.toml"
    r = cli("scratch-toml", cfg, out, 55432, "127.0.0.1:55433")
    assert r.returncode == 0, r.stderr
    import tomllib
    t = tomllib.loads(out.read_text())
    assert t["app"]["address"] == "127.0.0.1:55433"
    assert t["db"]["port"] == 55432 and t["db"]["host"] == "127.0.0.1"
    assert t["db"]["password"] == PW and t["db"]["database"] == "listmonk"
    assert cli("scratch-toml", cfg, out, 55432, "0.0.0.0:1").returncode == 2


def test_repoint_rewrites_only_the_db_host(cfg):
    r = cli("repoint", cfg, "127.0.0.1")
    assert r.returncode == 0 and r.stdout.strip() == "127.0.0.1"
    text = CONFIG.replace('[db]\nhost = "127.0.0.1"', '[db]\nhost = "172.17.0.1"')
    cfg.write_text(text, encoding="utf-8", newline="\n")
    r = cli("repoint", cfg, "127.0.0.1")
    assert r.stdout.strip() == "172.17.0.1"
    after = cfg.read_text()
    assert after == CONFIG
    assert cli("repoint", cfg, "not-an-ip").returncode == 2


# --- listen set ------------------------------------------------------------------

def _ls(tmp_path, *rows) -> Path:
    p = tmp_path / "listen-set.before"
    p.write_text("".join(r + "\n" for r in rows))
    return p


def test_listen_addrs_keeps_every_recorded_bind(tmp_path):
    # Documentation IPs only (RFC 5737): never a real slot address in a test.
    f = _ls(tmp_path, "127.0.0.1:42009", "172.17.0.1:42009", "203.0.113.7:42009")
    r = cli("listen-addrs", f, 42009)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "127.0.0.1,172.17.0.1,203.0.113.7"


@pytest.mark.parametrize("rows", [
    (), ("0.0.0.0:42009",), ("*:42009",), ("[::]:42009",), ("127.0.0.1:5432",),
    ("example.invalid:42009",),
])
def test_listen_addrs_refuses_empty_wildcard_or_foreign(tmp_path, rows):
    assert cli("listen-addrs", _ls(tmp_path, *rows), 42009).returncode == 2


def test_in_listen(tmp_path):
    f = _ls(tmp_path, "127.0.0.1:42009", "172.17.0.1:42009")
    assert cli("in-listen", f, "localhost", 42009).returncode == 0
    assert cli("in-listen", f, "172.17.0.1", 42009).returncode == 0
    assert cli("in-listen", f, "198.51.100.1", 42009).returncode == 1


# --- globals ---------------------------------------------------------------------

GLOBALS = """--
-- PostgreSQL database cluster dump
--
\\restrict abc
SET default_transaction_read_only = off;
CREATE ROLE quadstronaut;
ALTER ROLE quadstronaut WITH SUPERUSER INHERIT CREATEROLE CREATEDB LOGIN REPLICATION BYPASSRLS PASSWORD 'SCRAM-SHA-256$4096:x$y:z';
CREATE ROLE reader;
ALTER ROLE reader WITH LOGIN;
\\unrestrict abc
"""


def test_globals_filter_drops_only_the_bootstrap_create(tmp_path):
    src, out = tmp_path / "g.sql", tmp_path / "f.sql"
    src.write_text(GLOBALS)
    r = cli("globals-filter", "quadstronaut", src, out)
    assert r.returncode == 0, r.stderr
    t = out.read_text()
    assert "CREATE ROLE quadstronaut;" not in t
    assert "ALTER ROLE quadstronaut WITH SUPERUSER" in t and "PASSWORD 'SCRAM-SHA-256" in t
    assert "CREATE ROLE reader;" in t and "\\restrict abc" in t


def test_globals_without_the_role_password_is_refused(tmp_path):
    src, out = tmp_path / "g.sql", tmp_path / "f.sql"
    src.write_text(GLOBALS.replace(" PASSWORD 'SCRAM-SHA-256$4096:x$y:z'", ""))
    assert cli("globals-filter", "quadstronaut", src, out).returncode == 2
    assert not out.exists()


# --- cluster text ----------------------------------------------------------------

def test_conf_block_and_hba():
    c = pg.conf_block("127.0.0.1,172.17.0.1", 42009, "/h/.apps/pg-native/run")
    assert "listen_addresses = '127.0.0.1,172.17.0.1'\n" in c
    assert "port = 42009\n" in c and "unix_socket_permissions = 0700\n" in c
    assert pg.conf_block("", 42009, "/h/run").count("listen_addresses = ''") == 1
    with pytest.raises(pg.Refused):
        pg.conf_block("0.0.0.0", 42009, "/h/run")
    with pytest.raises(pg.Refused):
        pg.conf_block("127.0.0.1", 42009, "relative/run")
    # every TCP line needs the password (loopback is shared on a slot)
    tcp = [l for l in pg.HBA.splitlines() if l.startswith("host")]
    assert tcp and all(l.split()[-1] == "scram-sha-256" for l in tcp)
    assert "trust" not in "".join(tcp)


def test_every_sql_is_tagged_and_sanitize_targets_senders():
    for name, sql in pg.SQL.items():
        assert sql.startswith(f"/*qflix:{name}*/"), name
    s = pg.SQL["sanitize"]
    for key in ("'smtp'", "'messengers'", "'bounce.mailboxes'", "'bounce.enabled'",
                "'app.check_updates'", "status = 'paused'"):
        assert key in s, key
    assert "delete" not in s.lower()          # flags flipped, rows kept (count parity)


# --- counts ------------------------------------------------------------------------

def test_collect_and_compare(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "listmonk.counts").write_text("public.campaigns|73\npublic.subscribers|12\n")
    (d / "listmonk.seqs").write_text("public.campaigns_id_seq|80\npublic.x_seq|null\n")
    (d / "postgres.counts").write_text("")
    (d / "postgres.seqs").write_text("")
    r = cli("collect", d)
    assert r.returncode == 0, r.stderr
    a = json.loads(r.stdout)
    assert a["listmonk"]["tables"] == {"public.campaigns": 73, "public.subscribers": 12}
    assert a["listmonk"]["seqs"]["public.x_seq"] == "null"
    b = json.loads(r.stdout)
    b["listmonk"]["tables"]["public.subscribers"] = 11
    b["listmonk"]["seqs"]["public.campaigns_id_seq"] = "81"
    pa, pb = tmp_path / "a.json", tmp_path / "b.json"
    pa.write_text(json.dumps(a))
    pb.write_text(json.dumps(b))
    assert cli("compare", pa, pa).returncode == 0
    r = cli("compare", pa, pb)
    assert r.returncode == 1
    assert "public.subscribers: 12 != 11" in r.stdout
    assert "public.campaigns_id_seq: 80 != 81" in r.stdout
    del b["listmonk"]
    pb.write_text(json.dumps(b))
    assert "only in source" in cli("compare", pa, pb).stdout


def test_collect_refuses_garbage(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "x.counts").write_text("public.t|NaN\n")
    (d / "x.seqs").write_text("")
    assert cli("collect", d).returncode == 2
    assert cli("collect", tmp_path / "empty").returncode == 2


# --- newsletter gate -----------------------------------------------------------------

def _epoch(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


@pytest.mark.parametrize("when,closed", [
    ("2026-10-10T10:00:00", False),   # Saturday: open
    ("2026-10-11T15:30:00", True),    # Sunday 15:30: < 24h before Mon 15:00
    ("2026-10-12T15:00:00", True),    # the send itself
    ("2026-10-13T14:59:00", True),    # Tuesday, < 24h after
    ("2026-10-13T15:01:00", False),
    ("2026-10-11T14:59:00", False),
    ("2026-10-08T12:00:00", False),   # Thursday
])
def test_newsletter_gate(when, closed):
    r = cli("newsletter-gate", "--now", _epoch(when))
    assert r.returncode == (1 if closed else 0), (when, r.stdout, r.stderr)


def test_newsletter_hour_matches_the_timer():
    """qflix-newsletter.timer fires Mon 08:00 America/Phoenix = 15:00 UTC."""
    timer = (REPO / "scripts" / "maint" / "systemd" / "qflix-newsletter.timer").read_text()
    assert "OnCalendar=Mon 08:00:00 America/Phoenix" in timer
    assert pg.NEWSLETTER_UTC_HOUR == 15 and pg.NEWSLETTER_WEEKDAY == 0


def test_bad_usage_is_2():
    assert cli("frobnicate").returncode == 2
    assert cli("sql", "nope").returncode == 2
