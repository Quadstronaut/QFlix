#!/usr/bin/env python3
"""pg_native.py -- the pure helpers behind 312-native-postgres-install.sh (QFLX-37).

The installer is the orchestration (stop, dump, restore, start); everything that
is a DECISION or a TEXT TRANSFORM lives here so it is unit-tested without a
postgres: parsing listmonk's config.toml, the pgpass file, the recorded listen
set -> listen_addresses, the globals filter, the per-table row-count snapshot
and its comparison, the proof-copy sanitizer SQL and the newsletter gate.

Rules:
  * The listmonk DB password NEVER reaches argv, stdout or a log. It is only
    ever written into a 0600 file (pgpass / the scratch listmonk config).
  * Fail closed: anything unparseable is exit 2 with a reason on stderr.
  * Stdlib only (python3.11+ for tomllib; the box runs 3.13). Run by path like
    native_sanitize.py; never imported through lib/ (no __init__.py there).

CLI (exit 0 ok | 1 "no" answer (differs / gate closed) | 2 refused / bad input):
  lmconf CONFIG                     host|port|user|database of [db] (no password)
  lmaddr CONFIG                     [app] address (host:port)
  pgpass CONFIG OUT                 OUT (0600) = "*:*:*:<user>:<password>"
  scratch-toml CONFIG OUT PGPORT APPADDR   listmonk config for the proof (0600)
  repoint CONFIG HOST               rewrite [db] host in place; prints the old one
  listen-addrs LISTENFILE PORT      "a,b,c" for listen_addresses (no wildcard)
  in-listen LISTENFILE HOST PORT    exit 0 when HOST:PORT is a recorded listener
  globals-filter USER IN OUT        drop the bootstrap "CREATE ROLE <USER>;"
  conf LISTEN PORT SOCKDIR          the postgresql.conf block we append
  hba                               pg_hba.conf for the native cluster
  sql NAME                          one of SQL (dbs dbowners counts seqs running campaigns
                                    sanitize sanitize-check)
  collect DIR                       <db>.counts / <db>.seqs (psql -tA -F'|') -> JSON
  compare A.json B.json             exit 0 equal, 1 differs (one line per difference)
  newsletter-gate [--now EPOCH]     exit 1 within 24h of the Monday newsletter
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - python < 3.11
    tomllib = None  # type: ignore[assignment]


class Refused(Exception):
    """Bad input: the caller must stop."""


# ------------------------------------------------------------------ config.toml
def _toml(path: str | Path) -> dict:
    if tomllib is None:
        raise Refused("python has no tomllib (needs 3.11+)")
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, ValueError) as exc:
        raise Refused(f"cannot read {path}: {exc.__class__.__name__}") from None


def lm_db(path: str | Path) -> dict:
    db = _toml(path).get("db")
    if not isinstance(db, dict):
        raise Refused("no [db] table in the listmonk config")
    out = {
        "host": str(db.get("host") or ""),
        "port": db.get("port"),
        "user": str(db.get("user") or ""),
        "database": str(db.get("database") or ""),
        "password": db.get("password"),
    }
    if not out["host"] or not out["user"] or not out["database"]:
        raise Refused("[db] host/user/database missing")
    if not isinstance(out["port"], int) or not 1 <= out["port"] <= 65535:
        raise Refused("[db] port missing or not a port")
    if not isinstance(out["password"], str) or not out["password"]:
        raise Refused("[db] password missing")
    for k in ("user", "database"):
        if not re.fullmatch(r"[A-Za-z0-9_]{1,63}", out[k]):
            raise Refused(f"[db] {k} is not a plain identifier")
    return out


def lm_addr(path: str | Path) -> str:
    app = _toml(path).get("app") or {}
    addr = str(app.get("address") or "")
    if not re.fullmatch(r"[0-9.]+:[0-9]{1,5}", addr):
        raise Refused("[app] address is not host:port")
    return addr


def _write_0600(path: str | Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def pgpass_line(user: str, password: str) -> str:
    """libpq pgpass: ':' and '\\' in a field are backslash-escaped."""
    esc = password.replace("\\", "\\\\").replace(":", "\\:")
    return f"*:*:*:{user}:{esc}\n"


def scratch_toml(cfg: dict, pg_port: int, app_addr: str) -> str:
    """A listmonk config for the PROOF copy: loopback app port, proof DB port.
    Everything that can send lives in the DB settings, which the proof
    sanitizes; this file only says where to listen and which DB to open."""
    pw = json.dumps(cfg["password"])  # TOML basic strings share JSON escaping
    return (
        "[app]\n"
        f'address = "{app_addr}"\n\n'
        "[db]\n"
        'host = "127.0.0.1"\n'
        f"port = {int(pg_port)}\n"
        f'user = "{cfg["user"]}"\n'
        f"password = {pw}\n"
        f'database = "{cfg["database"]}"\n'
        'ssl_mode = "disable"\n'
        "max_open = 5\nmax_idle = 2\n"
        'max_lifetime = "300s"\n'
    )


def repoint(path: str | Path, host: str) -> str:
    """Rewrite the [db] host line in place (text-level: keeps the operator's
    formatting and every other key). Returns the old host."""
    ipaddress.ip_address(host)  # raises ValueError -> refused by the CLI
    p = Path(path)
    lines = p.read_text(encoding="utf-8").split("\n")
    in_db, hit, old = False, None, ""
    for i, line in enumerate(lines):
        s = line.strip()
        if re.fullmatch(r"\[[^\]]+\]\s*(#.*)?", s):
            in_db = bool(re.fullmatch(r"\[db\]\s*(#.*)?", s))
            continue
        m = in_db and re.match(r'^(\s*host\s*=\s*)"([^"]*)"(.*)$', line)
        if m:
            hit, old = i, m.group(2)
            lines[i] = f'{m.group(1)}"{host}"{m.group(3)}'
            break
    if hit is None:
        raise Refused("no [db] host line to rewrite")
    st = os.stat(p)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(lines))
    os.chmod(tmp, st.st_mode & 0o7777)
    os.replace(tmp, p)
    return old


# ------------------------------------------------------------------ listen set
_WILDCARDS = {"0.0.0.0", "*", "::", "[::]"}


def _split(entry: str) -> tuple[str, int]:
    entry = entry.strip()
    m = re.fullmatch(r"\[([0-9a-fA-F:.%a-z]+)\]:(\d+)|([^:\s]+):(\d+)", entry)
    if not m:
        raise Refused(f"unparseable listener {entry!r}")
    host = m.group(1) or m.group(3)
    return host, int(m.group(2) or m.group(4))


def listen_addrs(listen_file: str | Path, port: int) -> list[str]:
    try:
        rows = [l.strip() for l in Path(listen_file).read_text(encoding="utf-8").splitlines()]
    except OSError:
        raise Refused("listen set not recorded") from None
    rows = [r for r in rows if r]
    if not rows:
        raise Refused("listen set is empty")
    out: list[str] = []
    for r in rows:
        host, p = _split(r)
        if host in _WILDCARDS:
            raise Refused(f"wildcard listener {r!r} in the recorded set (0.0.0.0 is never an option)")
        if p != port:
            raise Refused(f"listener {r!r} is not on port {port}")
        try:
            ipaddress.ip_address(host.split("%")[0])
        except ValueError:
            raise Refused(f"listener {r!r} is not an IP address") from None
        if host not in out:
            out.append(host)
    return out


def in_listen(listen_file: str | Path, host: str, port: int) -> bool:
    host = "127.0.0.1" if host == "localhost" else host
    return host in listen_addrs(listen_file, port)


# ------------------------------------------------------------------ globals
def globals_filter(user: str, text: str) -> str:
    """pg_dumpall --globals-only into a cluster whose BOOTSTRAP superuser is
    already <user>: its CREATE ROLE would fail under ON_ERROR_STOP, the ALTER
    ROLE that follows carries the attributes and the SCRAM verifier."""
    if not re.fullmatch(r"[A-Za-z0-9_]{1,63}", user):
        raise Refused("bootstrap user is not a plain identifier")
    create = {f"CREATE ROLE {user};", f'CREATE ROLE "{user}";'}
    alter = re.compile(r'^ALTER ROLE "?%s"? WITH .*PASSWORD ' % re.escape(user))
    lines = text.split("\n")
    if not any(alter.match(l) for l in lines):
        raise Refused(f"globals carry no ALTER ROLE {user} ... PASSWORD (role or password lost)")
    return "\n".join(l for l in lines if l.strip() not in create)


# ------------------------------------------------------------------ cluster text
def conf_block(listen: str, port: int, sockdir: str) -> str:
    if listen and listen != "":
        for h in listen.split(","):
            if h in _WILDCARDS:
                raise Refused("wildcard listen_addresses refused")
            ipaddress.ip_address(h)
    # Absolute: /... on the box; a drive path only on the Windows test runner.
    if "'" in sockdir or not re.match(r"^(/|[A-Za-z]:/)", sockdir):
        raise Refused("socket dir must be an absolute path without quotes")
    return (
        "\n# --- QFlix native postgres (QFLX-37; 312-native-postgres-install.sh) ---\n"
        f"listen_addresses = '{listen}'\n"
        f"port = {int(port)}\n"
        f"unix_socket_directories = '{sockdir}'\n"
        "unix_socket_permissions = 0700\n"
        "password_encryption = 'scram-sha-256'\n"
        "max_connections = 100\n"
        "shared_buffers = 128MB\n"
        "timezone = 'Etc/UTC'\n"
        "log_timezone = 'Etc/UTC'\n"
        "datestyle = 'iso, mdy'\n"
        "logging_collector = on\n"
        "log_directory = 'log'\n"
        "log_filename = 'postgresql-%a.log'\n"
        "log_truncate_on_rotation = on\n"
        "log_rotation_age = 1d\n"
    )


HBA = (
    "# QFlix native postgres (QFLX-37). The socket lives in a 0700 dir, so\n"
    "# `local` reaches only this uid. EVERY TCP client (loopback included: on a\n"
    "# shared slot 127.0.0.1 is shared with other tenants) needs the password.\n"
    "local   all   all                  trust\n"
    "host    all   all   127.0.0.1/32   scram-sha-256\n"
    "host    all   all   ::1/128        scram-sha-256\n"
    "host    all   all   all            scram-sha-256\n"
)


# ------------------------------------------------------------------ SQL
# Each statement leads with a /*qflix:<name>*/ tag: harmless to postgres, and
# it lets the test fakes answer by name.
SQL = {
    "dbs": "/*qflix:dbs*/ select datname from pg_database "
           "where datallowconn and not datistemplate order by 1",
    "dbowners": "/*qflix:dbowners*/ select datname, pg_get_userbyid(datdba) from pg_database "
                "where datallowconn and not datistemplate order by 1",
    "counts": "/*qflix:counts*/ select n.nspname || '.' || c.relname, "
              "(xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', "
              "n.nspname, c.relname), false, true, '')))[1]::text "
              "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
              "where c.relkind in ('r', 'p') "
              "and n.nspname not in ('pg_catalog', 'information_schema') "
              "and n.nspname not like 'pg\\_toast%' order by 1",
    "seqs": "/*qflix:seqs*/ select schemaname || '.' || sequencename, "
            "coalesce(last_value::text, 'null') from pg_sequences order by 1",
    "running": "/*qflix:running*/ select count(*) from campaigns where status = 'running'",
    "campaigns": "/*qflix:campaigns*/ select count(*) from campaigns",
    # PROOF COPY ONLY (listmonk db in ~/.apps/.prove). Everything that can send
    # or fetch: SMTP + messengers + bounce mailboxes off, bounce intake off,
    # update checks and opt-in mails off, and nothing left running/scheduled.
    "sanitize": "/*qflix:sanitize*/ "
                "update settings set value = (select coalesce(jsonb_agg(e || '{\"enabled\": false}'::jsonb), '[]'::jsonb) "
                "from jsonb_array_elements(value) e) "
                "where key in ('smtp', 'messengers', 'bounce.mailboxes') and jsonb_typeof(value) = 'array'; "
                "update settings set value = value || '{\"enabled\": false}'::jsonb "
                "where key in ('bounce.postmark', 'bounce.forwardemail', 'bounce.lettermint') "
                "and jsonb_typeof(value) = 'object'; "
                "update settings set value = 'false'::jsonb where key in ('bounce.enabled', "
                "'bounce.webhooks_enabled', 'bounce.ses_enabled', 'bounce.sendgrid_enabled', "
                "'app.check_updates', 'app.send_optin_confirmation'); "
                "update campaigns set status = 'paused' where status in ('running', 'scheduled');",
    "sanitize-check": "/*qflix:sanitize-check*/ select "
                      "(select count(*) from settings, jsonb_array_elements(value) e "
                      "where key in ('smtp', 'messengers', 'bounce.mailboxes') and jsonb_typeof(value) = 'array' "
                      "and coalesce(e->>'enabled', 'false') <> 'false') + "
                      "(select count(*) from settings where key in ('bounce.postmark', 'bounce.forwardemail', "
                      "'bounce.lettermint') and jsonb_typeof(value) = 'object' "
                      "and coalesce(value->>'enabled', 'false') <> 'false') + "
                      "(select count(*) from settings where key in ('bounce.enabled', 'bounce.webhooks_enabled', "
                      "'bounce.ses_enabled', 'bounce.sendgrid_enabled', 'app.check_updates', "
                      "'app.send_optin_confirmation') and value = 'true'::jsonb) + "
                      "(select count(*) from campaigns where status in ('running', 'scheduled'))",
}


# ------------------------------------------------------------------ counts
def _parse_rows(text: str, what: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        line = line.rstrip("\r")
        if not line.strip():
            continue
        name, sep, val = line.rpartition("|")
        if not sep or not name:
            raise Refused(f"unparseable {what} row {line!r}")
        if what == "counts":
            if not re.fullmatch(r"\d+", val):
                raise Refused(f"non-numeric count in {line!r}")
            out[name] = int(val)
        else:
            if not re.fullmatch(r"-?\d+|null", val):
                raise Refused(f"bad sequence value in {line!r}")
            out[name] = val
    return out


def collect(d: str | Path) -> dict:
    """{db: {"tables": {schema.table: n}, "seqs": {schema.seq: last}}}"""
    d = Path(d)
    dbs = sorted(p.stem for p in d.glob("*.counts"))
    if not dbs:
        raise Refused(f"no *.counts under {d}")
    snap: dict = {}
    for db in dbs:
        seqf = d / f"{db}.seqs"
        if not seqf.is_file():
            raise Refused(f"{db}.seqs missing")
        snap[db] = {
            "tables": _parse_rows((d / f"{db}.counts").read_text(encoding="utf-8"), "counts"),
            "seqs": _parse_rows(seqf.read_text(encoding="utf-8"), "seqs"),
        }
    return snap


def compare(a: dict, b: dict) -> list[str]:
    diffs: list[str] = []
    for db in sorted(set(a) | set(b)):
        if db not in a or db not in b:
            diffs.append(f"database {db}: only in {'source' if db in a else 'target'}")
            continue
        for kind in ("tables", "seqs"):
            x, y = a[db].get(kind, {}), b[db].get(kind, {})
            for k in sorted(set(x) | set(y)):
                if x.get(k) != y.get(k):
                    diffs.append(f"{db} {kind[:-1]} {k}: {x.get(k)} != {y.get(k)}")
    return diffs


# ------------------------------------------------------------------ newsletter
NEWSLETTER_WEEKDAY = 0         # Monday
NEWSLETTER_UTC_HOUR = 15       # qflix-newsletter.timer: Mon 08:00 America/Phoenix (UTC-7, no DST)
GATE = timedelta(hours=24)


def newsletter_near(now: datetime) -> bool:
    """True when `now` is within 24h of a Monday 15:00 UTC send (spec 6 row 13:
    never within 24h of the newsletter)."""
    now = now.astimezone(timezone.utc)
    base = now.replace(hour=NEWSLETTER_UTC_HOUR, minute=0, second=0, microsecond=0)
    base -= timedelta(days=(now.weekday() - NEWSLETTER_WEEKDAY) % 7)
    for k in (-1, 0, 1):
        if abs(now - (base + timedelta(days=7 * k))) < GATE:
            return True
    return False


# ------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    if not a:
        sys.stderr.write(__doc__ or "")
        return 2
    cmd, rest = a[0], a[1:]
    try:
        if cmd == "lmconf" and len(rest) == 1:
            c = lm_db(rest[0])
            print(f"{c['host']}|{c['port']}|{c['user']}|{c['database']}")
        elif cmd == "lmaddr" and len(rest) == 1:
            print(lm_addr(rest[0]))
        elif cmd == "pgpass" and len(rest) == 2:
            c = lm_db(rest[0])
            _write_0600(rest[1], pgpass_line(c["user"], c["password"]))
        elif cmd == "scratch-toml" and len(rest) == 4:
            c = lm_db(rest[0])
            if not re.fullmatch(r"127\.0\.0\.1:\d{1,5}", rest[3]):
                raise Refused("scratch listmonk must listen on 127.0.0.1")
            _write_0600(rest[1], scratch_toml(c, int(rest[2]), rest[3]))
        elif cmd == "repoint" and len(rest) == 2:
            print(repoint(rest[0], rest[1]))
        elif cmd == "listen-addrs" and len(rest) == 2:
            print(",".join(listen_addrs(rest[0], int(rest[1]))))
        elif cmd == "in-listen" and len(rest) == 3:
            return 0 if in_listen(rest[0], rest[1], int(rest[2])) else 1
        elif cmd == "globals-filter" and len(rest) == 3:
            text = Path(rest[1]).read_text(encoding="utf-8")
            _write_0600(rest[2], globals_filter(rest[0], text))
        elif cmd == "conf" and len(rest) == 3:
            sys.stdout.write(conf_block(rest[0], int(rest[1]), rest[2]))
        elif cmd == "hba" and not rest:
            sys.stdout.write(HBA)
        elif cmd == "sql" and len(rest) == 1 and rest[0] in SQL:
            print(SQL[rest[0]])
        elif cmd == "collect" and len(rest) == 1:
            print(json.dumps(collect(rest[0]), sort_keys=True))
        elif cmd == "compare" and len(rest) == 2:
            x = json.loads(Path(rest[0]).read_text(encoding="utf-8"))
            y = json.loads(Path(rest[1]).read_text(encoding="utf-8"))
            diffs = compare(x, y)
            for d in diffs:
                print(d)
            return 1 if diffs else 0
        elif cmd == "newsletter-gate" and len(rest) in (0, 2):
            now = datetime.now(timezone.utc)
            if rest:
                if rest[0] != "--now":
                    raise Refused("usage: newsletter-gate [--now EPOCH]")
                now = datetime.fromtimestamp(int(rest[1]), timezone.utc)
            if newsletter_near(now):
                print("within 24h of the Monday 15:00 UTC newsletter")
                return 1
            return 0
        else:
            sys.stderr.write(f"pg_native.py: bad usage: {' '.join(a[:1])}\n")
            return 2
    except (Refused, ValueError, OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"pg_native.py {cmd}: refused: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
