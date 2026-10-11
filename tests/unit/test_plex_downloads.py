"""QFLX-49: every member may download (Plex allowSync), and the gate keeps it so.

The drift this pins: every Plex share is born with downloads OFF (Plex web
invite default, plexapi inviteFriend default), and the gate's library writes
never touch the flag, so a member invited the normal way silently could not
download, forever. The gate now corrects any accepted share at allowSync=0 on
every run, and `--downloads-check` lets the entitlement-service canary page
if one survives.

NOTHING IN THIS FILE MAY NAME A REAL MEMBER.
"""
import datetime as dt
import importlib.util
import sys
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "maint" / "lib"))

import plexshare as PS         # noqa: E402


def _load_gate():
    p = ROOT / "scripts" / "maint" / "qflix-entitlement.py"
    spec = importlib.util.spec_from_file_location("qflix_entitlement_dl", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


G = _load_gate()

NOW = dt.datetime(2026, 10, 10, tzinfo=dt.timezone.utc)

XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="4">
<SharedServer id="11" userID="101" email="on@example.com" username="u1"
  acceptedAt="1738445374" invitedAt="1738445300" allLibraries="0" allowSync="1">
  <Section id="132920523" key="4" title="QFlix - Movies" type="movie" shared="1"/>
</SharedServer>
<SharedServer id="12" userID="102" email="off@example.com" username="u2"
  acceptedAt="1738445374" invitedAt="1738445300" allLibraries="0" allowSync="0">
  <Section id="999" key="9" title="QFlix - Welcome" type="movie" shared="1"/>
</SharedServer>
<SharedServer id="13" userID="103" email="pending@example.com" username="u3"
  invitedAt="1738445300" allLibraries="0" allowSync="0"/>
<SharedServer id="14" userID="104" email="unreported@example.com" username="u4"
  acceptedAt="1738445374" invitedAt="1738445300" allLibraries="0"/>
</MediaContainer>"""


class Recorder:
    """Stands in for urllib's opener; records (method, path, query, body)."""

    def __init__(self, reply=""):
        self.calls = []
        self.reply = reply

    def __call__(self, req, timeout=None):
        u = urllib.parse.urlsplit(req.full_url)
        self.calls.append((req.get_method(), u.path,
                           dict(urllib.parse.parse_qsl(u.query)), req.data))
        reply = self.reply

        class R:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def read(self_):
                return reply.encode()
        return R()


def client(rec):
    return PS.PlexShareClient(token="tok", machine_id="mid", opener=rec)


def test_parse_reads_allow_sync_tristate():
    by_id = {s.user_id: s for s in PS.parse_shares(XML)}
    assert by_id[101].allow_sync is True
    assert by_id[102].allow_sync is False
    assert by_id[104].allow_sync is None, "absent attribute is unknown, not off"


def test_only_accepted_shares_explicitly_off_are_drift():
    off = PS.shares_without_downloads(PS.parse_shares(XML))
    assert [s.user_id for s in off] == [102], (
        "pending invites and unreported flags must not be written")


def test_set_allow_sync_wire_format():
    """plexapi 4.18.1 updateFriend: PUT /api/v2/sharings/<userID>?allowSync=1,
    keyed by plex.tv USER id, no body."""
    rec = Recorder()
    share = PS.parse_shares(XML)[1]
    client(rec).set_allow_sync(share, True)
    (method, path, query, body), = rec.calls
    assert method == "PUT"
    assert path == "/api/v2/sharings/102"
    assert query["allowSync"] == "1"
    assert body is None


def test_set_allow_sync_refuses_a_share_with_no_user_id():
    rec = Recorder()
    s = PS.Share(shared_server_id=5, user_id=0, email="x@example.com", username="")
    with pytest.raises(PS.PlexShareError):
        client(rec).set_allow_sync(s)
    assert rec.calls == []


def test_reconcile_corrects_the_member_with_downloads_off():
    rec = Recorder()
    out = G.reconcile_downloads(client(rec), PS.parse_shares(XML), execute=True)
    assert [(m, p, q.get("allowSync")) for m, p, q, _ in rec.calls] == [
        ("PUT", "/api/v2/sharings/102", "1")]
    assert len(out.applied) == 1 and not out.failed
    assert "off@example.com" not in out.applied[0], "addresses are masked"


def test_reconcile_is_idempotent_when_everyone_can_download():
    xml = XML.replace('allowSync="0"', 'allowSync="1"')
    rec = Recorder()
    out = G.reconcile_downloads(client(rec), PS.parse_shares(xml), execute=True)
    assert rec.calls == [] and not out.applied and not out.failed


def test_reconcile_writes_nothing_when_not_executing():
    rec = Recorder()
    out = G.reconcile_downloads(client(rec), PS.parse_shares(XML), execute=False)
    assert rec.calls == []
    assert len(out.deferred) == 1


def test_reconcile_failure_is_reported_not_raised():
    def boom(req, timeout=None):
        raise OSError("down")
    out = G.reconcile_downloads(client(boom), PS.parse_shares(XML), execute=True)
    assert len(out.failed) == 1 and not out.applied


def test_floor_share_gets_downloads_but_no_library_change():
    """The off share above holds only the Welcome floor. Enabling downloads
    must not touch its libraries: the only write is the sharings PUT."""
    rec = Recorder()
    G.reconcile_downloads(client(rec), PS.parse_shares(XML), execute=True)
    assert all("shared_servers" not in p for _, p, _, _ in rec.calls)


def test_downloads_check_counts_only(tmp_path, monkeypatch, capsys):
    (tmp_path / "plex.token").write_text("tok")
    monkeypatch.setattr(G, "_secrets_dir", lambda: tmp_path)
    rec = Recorder(reply=XML)
    real = PS.PlexShareClient

    def fake(**kw):
        return real(opener=rec, **kw)
    monkeypatch.setattr(G.PS, "PlexShareClient", fake)
    rc = G.main(["--downloads-check", "--machine-id", "mid"])
    out = capsys.readouterr().out
    assert rc == G.EXIT_ARM_CHECK_RED
    assert "DOWNLOADS_OFF=1" in out and "DOWNLOADS_ON=1" in out
    assert "@" not in out, "counts only, never an address"
    assert all(m == "GET" for m, _, _, _ in rec.calls), "check is read-only"


def test_migration_invites_with_downloads_on():
    src = (ROOT / "scripts" / "migrate" / "45-plex-invites.py").read_text(encoding="utf-8")
    assert "allowSync=True" in src
