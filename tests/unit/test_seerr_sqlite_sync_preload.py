"""scripts/data/seerr-sqlite-sync.cjs (QFLX-111): the --require preload that makes
every sqlite3 connection Seerr opens run `PRAGMA synchronous = NORMAL` before
anything else, so WAL commits (express-session's per-request `touch`) stop
paying an fsync each on the slot's shared disk.

Runs real Node when it is on PATH (CI's ubuntu runner has it). The real sqlite3
addon is not a test dependency, so a FAKE `sqlite3` package is written into a
node_modules tree and resolved by a plain require(), exactly as typeorm's
PlatformTools.load("sqlite3") does. The fake models what the hook relies on in
node-sqlite3 5.1.7 (src/database.cc): statements issued before the open
completes are QUEUED and drained in order after the open callback; a failed
open never drains the queue. The seerr-artifact workflow additionally boots the
real Seerr build (real typeorm + sqlite3) through the preload and requires the
`applied` line.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PRELOAD = REPO / "scripts" / "data" / "seerr-sqlite-sync.cjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="needs node")

APPLIED = "QFLX-111 applied: PRAGMA synchronous=NORMAL on db.sqlite3 (read back 1)"

# A stand-in for node-sqlite3's exports object (a native class + constants).
FAKE_SQLITE3 = r'''
const EventEmitter = require('events');
const log = [];
class Database extends EventEmitter {
  constructor(file, a, b) {
    super();
    const cb = typeof a === 'number' ? b : a;
    this.file = file; this.mode = typeof a === 'number' ? a : null;
    this.open = false; this.queue = []; this.sync = 2;
    log.push('new ' + file + (this.mode === null ? '' : ' mode=' + this.mode));
    setImmediate(() => {
      const fail = process.env.FAKE_OPEN_FAIL;
      if (fail) {                       // failed open: callback/error, queue never drains
        const e = Object.assign(new Error('SQLITE_CANTOPEN: unable to open database file'),
                                { code: 'SQLITE_CANTOPEN' });
        if (typeof cb === 'function') cb.call(this, e); else this.emit('error', e);
        return;
      }
      this.open = true;
      if (typeof cb === 'function') cb.call(this, null);
      this.emit('open');
      const q = this.queue; this.queue = [];
      q.forEach((f) => f());
    });
  }
  _sched(f) { if (this.open) setImmediate(f); else this.queue.push(f); }
  run(sql, cb) {
    this._sched(() => {
      log.push('run ' + sql);
      if (/synchronous\s*=\s*NORMAL/.test(sql)) {
        if (process.env.FAKE_PRAGMA_FAIL) return cb && cb(new Error('SQLITE_BUSY: fake'));
        if (!process.env.FAKE_PRAGMA_NOOP) this.sync = 1;
      }
      cb && cb.call({}, null);
    });
    return this;
  }
  get(sql, cb) {
    this._sched(() => {
      log.push('get ' + sql);
      cb(null, /PRAGMA synchronous$/.test(sql) ? { synchronous: this.sync } : {});
    });
    return this;
  }
}
Database.someStatic = 'kept';
class Statement {}
class Backup {}
const sqlite3 = { Database, Statement, Backup, OPEN_READONLY: 1, OPEN_READWRITE: 2,
                  OPEN_CREATE: 4, OPEN_URI: 64, __log: log, __copy: process.env.FAKE_COPY_TAG || 'a' };
sqlite3.cached = { Database: function (f, a, b) { return new Database(f, a, b); }, objects: {} };
sqlite3.verbose = function () { return sqlite3; };
module.exports = exports = sqlite3;
'''

# What typeorm 0.3.31 SqliteDriver does: require("sqlite3").verbose(), then
# new Database(file[, flags], cb), then its pragmas, then queries.
APP = r'''
const out = {};
const raw = require('sqlite3');
const OrigName = raw.Database.name;
const sqlite = raw.verbose();
out.same = sqlite === raw && require('sqlite3') === raw;
out.statics = [sqlite.Database.someStatic, typeof sqlite.cached.Database, sqlite.OPEN_URI,
               typeof sqlite.Statement, typeof sqlite.Backup, OrigName];
try { sqlite.Database('x'); out.noNew = 'no throw'; } catch (e) { out.noNew = e.constructor.name; }
const withMode = process.env.WITH_MODE === '1';
const done = (db) => {
  db.run('PRAGMA journal_mode = WAL', () => db.get('PRAGMA synchronous', (e, row) => {
    out.sync = row.synchronous;
    out.log = sqlite.__log.slice();
    if (process.env.SECOND_COPY) {                     // a copy resolved from another path
      const other = require(process.env.SECOND_COPY);
      out.otherCopy = other.__copy;
      const d2 = new other.Database('/x/other.sqlite3', (err) => {
        out.otherErr = err ? err.message : null;
        d2.get('PRAGMA synchronous', (e3, r3) => { out.otherSync = r3.synchronous;
          console.log(JSON.stringify(out)); });
      });
      return;
    }
    console.log(JSON.stringify(out));
  }));
};
const cb = function (err) {
  out.openErr = err ? err.code : null;
  out.cbThis = this === db;
  out.instanceofWrapper = db instanceof sqlite.Database;
  out.isEmitter = db instanceof require('events');
  out.logAtCallback = sqlite.__log.slice();
  if (err) { console.log(JSON.stringify(out)); return; }
  done(db);
};
let db;
if (process.env.NO_CALLBACK) {
  db = new sqlite.Database('/x/db/db.sqlite3');
  db.on('error', (e) => { out.errorEvent = e.code; console.log(JSON.stringify(out)); });
  db.on('open', () => { out.openEvent = true; });
  setTimeout(() => { if (!out.errorEvent) { out.sync = db.sync; console.log(JSON.stringify(out)); } }, 300);
} else {
  db = withMode ? new sqlite.Database('/x/db/db.sqlite3', 6, cb) : new sqlite.Database('/x/db/db.sqlite3', cb);
}
'''


def _tree(tmp: Path) -> Path:
    mod = tmp / "node_modules" / "sqlite3"
    mod.mkdir(parents=True)
    (mod / "package.json").write_text('{"name":"sqlite3","version":"5.1.7","main":"lib/sqlite3.js"}',
                                      newline="\n")
    (mod / "lib").mkdir()
    (mod / "lib" / "sqlite3.js").write_text(FAKE_SQLITE3, newline="\n")
    # A second, nested copy (another resolution path, another exports object).
    nested = tmp / "node_modules" / "other" / "node_modules" / "sqlite3"
    shutil.copytree(mod, nested)
    (tmp / "app.js").write_text(APP, newline="\n")
    return nested


def _run(tmp: Path, env=None, preload=True):
    nested = _tree(tmp)
    e = dict(os.environ, **(env or {}))
    if e.get("SECOND_COPY") == "1":
        e["SECOND_COPY"] = str(nested / "lib" / "sqlite3.js")
        e["FAKE_COPY_TAG"] = "a"
    args = [NODE] + (["--require", str(PRELOAD)] if preload else []) + [str(tmp / "app.js")]
    p = subprocess.run(args, cwd=tmp, env=e, capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stdout + p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1]), p.stderr


def test_without_the_preload_the_fake_stays_at_full(tmp_path):
    """Control: the fake models upstream's default (synchronous=FULL=2)."""
    out, err = _run(tmp_path, preload=False)
    assert out["sync"] == 2 and "qflix-sqlite-sync" not in err


@pytest.mark.parametrize("with_mode", ["0", "1"])
def test_pragma_runs_first_and_the_open_callback_waits_for_it(tmp_path, with_mode):
    out, err = _run(tmp_path, {"WITH_MODE": with_mode})
    assert out["sync"] == 1
    mode = " mode=6" if with_mode == "1" else ""
    # The callback only fires once the pragma is applied AND read back...
    assert out["logAtCallback"] == [f"new /x/db/db.sqlite3{mode}",
                                    "run PRAGMA synchronous = NORMAL", "get PRAGMA synchronous"]
    # ...so the caller's own pragmas run after it.
    assert out["log"].index("run PRAGMA synchronous = NORMAL") < out["log"].index("run PRAGMA journal_mode = WAL")
    assert out["openErr"] is None
    assert err.strip().splitlines() == ["[qflix-sqlite-sync] " + APPLIED]


def test_constructor_semantics_and_statics_are_kept(tmp_path):
    out, _ = _run(tmp_path)
    assert out["same"] is True                                   # verbose() returns the exports
    assert out["statics"] == ["kept", "function", 64, "function", "function", "Database"]
    assert out["noNew"] == "TypeError"
    assert out["cbThis"] is True and out["instanceofWrapper"] is True and out["isEmitter"] is True


def test_an_open_error_reaches_the_callback_and_nothing_is_claimed(tmp_path):
    out, err = _run(tmp_path, {"FAKE_OPEN_FAIL": "1"})
    assert out["openErr"] == "SQLITE_CANTOPEN"
    assert "applied" not in err and "ERROR" not in err


def test_an_open_error_without_a_callback_is_an_error_event(tmp_path):
    out, _ = _run(tmp_path, {"FAKE_OPEN_FAIL": "1", "NO_CALLBACK": "1"})
    assert out["errorEvent"] == "SQLITE_CANTOPEN"


def test_no_callback_still_gets_the_pragma(tmp_path):
    out, err = _run(tmp_path, {"NO_CALLBACK": "1"})
    assert out["sync"] == 1 and out["openEvent"] is True and APPLIED in err


@pytest.mark.parametrize("env,why", [({"FAKE_PRAGMA_FAIL": "1"}, "SQLITE_BUSY: fake"),
                                     ({"FAKE_PRAGMA_NOOP": "1"}, "read back synchronous=2")])
def test_a_failed_pragma_fails_open_and_loud(tmp_path, env, why):
    """Upstream's FULL is the safe direction: Seerr starts, stderr says so."""
    out, err = _run(tmp_path, env)
    assert out["openErr"] is None and out["sync"] == 2
    assert "QFLX-111 ERROR: PRAGMA synchronous=NORMAL NOT applied on db.sqlite3 (" + why in err
    assert "applied: " not in err


def test_a_copy_resolved_from_another_path_is_hooked_too(tmp_path):
    out, err = _run(tmp_path, {"SECOND_COPY": "1"})
    assert out["otherCopy"] == "a" and out["otherErr"] is None
    assert out["sync"] == 1 and out["otherSync"] == 1
    assert err.count("QFLX-111 applied") == 2                    # one line per connection


def test_preload_is_inert_for_non_sqlite_modules(tmp_path):
    app = tmp_path / "plain.js"
    app.write_text("const p = require('path'); console.log(typeof p.join, require('events').name);\n",
                   newline="\n")
    p = subprocess.run([NODE, "--require", str(PRELOAD), str(app)], capture_output=True, text=True,
                       timeout=30)
    assert p.returncode == 0 and p.stdout.split() == ["function", "EventEmitter"] and p.stderr == ""


@pytest.mark.parametrize("name", ["a", "sqlite3_partial"])
def test_a_circular_require_draws_no_node_warning(tmp_path, name):
    """QFLX-111 box boot printed 'Accessing non-existent property OPEN_READWRITE
    of module exports inside circular dependency': the shape check did property
    GETs on a still-loading module's exports (Node's warning proxy is its
    prototype). The check must never read through that proxy, even when the
    partial module's request names sqlite3."""
    (tmp_path / f"{name}.js").write_text(
        f"exports.x = 1;\nrequire('./b');\nconsole.log('circ ok');\n", newline="\n")
    (tmp_path / "b.js").write_text(
        f"const a = require('./{name}');\nmodule.exports = {{ y: a.x }};\n", newline="\n")
    ctl = subprocess.run([NODE, str(tmp_path / f"{name}.js")], capture_output=True, text=True,
                         timeout=30)
    p = subprocess.run([NODE, "--require", str(PRELOAD), str(tmp_path / f"{name}.js")],
                       capture_output=True, text=True, timeout=30)
    assert ctl.returncode == 0 and ctl.stderr == ""
    assert p.returncode == 0 and p.stdout.strip() == "circ ok"
    assert p.stderr == "", p.stderr


def test_lf_only_and_ascii():
    data = PRELOAD.read_bytes()
    assert b"\r" not in data
    data.decode("ascii")
