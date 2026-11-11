'use strict';
// seerr-sqlite-sync.cjs -- QFLX-111. Second preload of the native Seerr unit,
// loaded with `node --require <this>` beside seerr-listen.cjs (CLI flags, never
// NODE_OPTIONS).
//
// Why: Seerr's prod DataSource is `type: 'sqlite', enableWAL: true` and never
// sets PRAGMA synchronous, so every connection runs WAL at the compiled default
// synchronous=FULL: one fsync per commit. express-session (connect-typeorm
// store) `touch`es the session row on EVERY request that carries a cookie and
// holds the response until that commit lands. The slot's shared disk fsyncs at
// p50 232 ms / p90 613 ms, and the one sqlite connection serializes the ~40 API
// calls a page makes: 5-10 s page loads. In WAL mode synchronous=NORMAL is the
// documented pairing (sqlite.org/pragma.html#pragma_synchronous): no fsync per
// commit, the WAL is synced at checkpoint, and the database cannot corrupt; a
// power loss may roll back the last commits (session touches, at worst a
// request a member just made). That is the trade we take.
//
// Hook point (read, not guessed; typeorm 0.3.31 as Seerr 3.5.0 pins it):
//   SqliteDriver.loadDependencies: this.sqlite = PlatformTools.load("sqlite3").verbose()
//     PlatformTools.load("sqlite3") is a plain `require("sqlite3")`, and
//     sqlite3 5.1.7's verbose() returns the module exports object itself.
//   SqliteDriver.createDatabaseConnection: new this.sqlite.Database(db, cb)
//     then, once cb fires, PRAGMA journal_mode = WAL / foreign_keys = ON.
// So we wrap `Database` on the sqlite3 exports object the first time a
// require() whose request names sqlite3 returns it, from whatever path it was
// resolved (a nested or hoisted copy is caught too); see looksLikeSqlite3. Each new connection
// queues `PRAGMA synchronous = NORMAL` straight after construction; node-sqlite3
// queues statements issued before the open completes and drains that queue in
// order once it does (src/database.cc Schedule/Process), so ours runs FIRST.
// The caller's open callback is held until the pragma is applied AND read back,
// so TypeORM's own pragmas and every query after them see NORMAL. (The 'open'
// event still fires at the raw open; no caller in Seerr listens for it.)
//
// Semantics kept: `new` only, (file[, mode][, callback]) parsed exactly as the
// native constructor does, the callback's `this` is the db, an open error goes
// to the callback (or an 'error' event without one), instances are real native
// Databases (`instanceof` holds for both the wrapper and the original), statics
// inherit, and verbose() / cached / OPEN_* / Statement / Backup are untouched.
// sqlite3.cached.Database builds through its own closure and is NOT hooked;
// neither TypeORM nor Seerr uses it.
//
// Fails OPEN, loudly. Unlike seerr-listen.cjs (a wrong bind widens exposure, so
// it refuses to start), a failed pragma leaves upstream's own default, FULL:
// slower, never less durable. Failing closed would turn a performance knob into
// a crash loop (StartLimitBurst=5, then Seerr is down for every member). So a
// failure prints one `QFLX-111 ERROR` line and Seerr starts as upstream ships
// it. The deploy gate is where it fails closed: 311 --refresh-preload requires
// the `QFLX-111 applied` line in the unit's journal after its restart.
const Module = require('module');
const path = require('path');

const PRAGMA = 'PRAGMA synchronous = NORMAL';
const NORMAL = 1;                         // PRAGMA synchronous reads back 0..3
const TAG = '[qflix-sqlite-sync]';
const WRAPPED = Symbol.for('qflix.sqliteSync.wrapped');
const seen = new WeakSet();

function say(line) {
  try { process.stderr.write(TAG + ' ' + line + '\n'); } catch (_) { /* never fatal */ }
}

// The hook sees EVERY require() result, so the test must be side-effect free on
// foreign exports. A property GET is not: a module that is still loading
// (circular require) hands out an exports object whose prototype is Node's
// warning proxy, and reading a missing key on it prints "Accessing non-existent
// property 'OPEN_READWRITE' of module exports inside circular dependency" (seen
// twice on the box at boot, QFLX-111). So, in order:
//   1. the request string must name sqlite3 (typeorm asks for "sqlite3"; a
//      nested copy loaded by path ends in .../sqlite3/lib/sqlite3.js);
//   2. the four keys must be OWN properties: hasOwnProperty never consults the
//      prototype, so the proxy is never reached;
//   3. only then are the values read. A Proxy export that throws is not sqlite3.
// A partial sqlite3 (re-required while its own file runs) has no `verbose` yet,
// fails 2, stays out of `seen` and is matched on the complete return.
const has = Object.prototype.hasOwnProperty;
const KEYS = ['OPEN_READWRITE', 'verbose', 'Database', 'Statement'];
function namesSqlite3(request) {
  return typeof request === 'string' && request.indexOf('sqlite3') !== -1;
}
function looksLikeSqlite3(m) {
  if (m === null || (typeof m !== 'object' && typeof m !== 'function')) return false;
  try {
    for (const k of KEYS) if (!has.call(m, k)) return false;
    return typeof m.OPEN_READWRITE === 'number' && typeof m.verbose === 'function'
      && typeof m.Database === 'function' && typeof m.Statement === 'function';
  } catch (_) {
    return false;
  }
}

function wrapDatabase(Orig) {
  function Database(file, a, b) {
    if (!new.target) {
      throw new TypeError("Class constructor Database cannot be invoked without 'new'");
    }
    // The native parse: a number at [1] is the mode, the callback follows it;
    // otherwise [1] is the callback (src/database.cc Database::Database).
    const hasMode = typeof a === 'number';
    const cb = hasMode ? (typeof b === 'function' ? b : null) : (typeof a === 'function' ? a : null);
    const name = path.basename(String(file));
    let db = null;
    let done = false;
    const deliver = (err) => {
      if (done) return;
      done = true;
      if (cb) cb.call(db, err);
      else if (err) db.emit('error', err);
    };
    const degraded = (why) => {
      say('QFLX-111 ERROR: PRAGMA synchronous=NORMAL NOT applied on ' + name + ' (' + why +
          '); running at upstream synchronous=FULL (durable, one fsync per commit, slow)');
      deliver(null);
    };
    // Success waits for the pragma; an open error is final (a failed open never
    // drains the queue, so the pragma's callback would never come).
    const onOpen = (err) => { if (err) deliver(err); };
    const args = hasMode ? [file, a, onOpen] : [file, onOpen];
    db = new.target === Database ? new Orig(...args) : Reflect.construct(Orig, args, new.target);
    try {
      db.run(PRAGMA, (err) => {
        if (done) return;
        if (err) return degraded(err.message || String(err));
        db.get('PRAGMA synchronous', (err2, row) => {
          if (done) return;
          if (err2) return degraded('read-back: ' + (err2.message || String(err2)));
          const got = row ? Number(row.synchronous) : NaN;
          if (got !== NORMAL) return degraded('read back synchronous=' + got);
          say('QFLX-111 applied: PRAGMA synchronous=NORMAL on ' + name + ' (read back ' + got + ')');
          deliver(null);
        });
      });
    } catch (e) {
      degraded('queue: ' + (e && e.message ? e.message : String(e)));
    }
    return db;
  }
  // Instances stay native Databases: one shared prototype makes `instanceof`
  // true for both constructors; the static side inherits from the original.
  Database.prototype = Orig.prototype;
  Object.setPrototypeOf(Database, Orig);
  Object.defineProperty(Database, WRAPPED, { value: Orig });
  return Database;
}

function patch(exp) {
  seen.add(exp);
  if (exp.Database[WRAPPED]) return;
  const W = wrapDatabase(exp.Database);
  try { exp.Database = W; } catch (_) { /* checked below */ }
  if (exp.Database !== W) say('QFLX-111 ERROR: cannot replace sqlite3.Database; running at synchronous=FULL');
}

if (typeof Module._load !== 'function') {
  say('QFLX-111 ERROR: no Module._load to hook; running at synchronous=FULL');
} else {
  const origLoad = Module._load;
  Module._load = function qflixSqliteSyncLoad(request, parent, isMain) {
    const exp = origLoad.apply(this, arguments);
    if (namesSqlite3(request) && exp && !seen.has(exp) && looksLikeSqlite3(exp)) patch(exp);
    return exp;
  };
}

module.exports = { wrapDatabase, looksLikeSqlite3, namesSqlite3, PRAGMA };
