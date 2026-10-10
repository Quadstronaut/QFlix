'use strict';
// seerr-listen.cjs -- QFLX-36 (UCC divorce A12). Loaded by the native Seerr unit
// with `node --require <this> dist/index.js` (a CLI flag, never NODE_OPTIONS).
//
// Why: the UCC container is published on THREE host addresses (loopback, the
// docker gateway the arrs and the dash use, and the public IP the panel's
// seerr-<slot> vhost may route to; spec F-17). Upstream server/index.ts calls
// `server.listen(PORT[, HOST])` with at most ONE host, and with no HOST it binds
// the wildcard, which is never an option (I-7). This preload reproduces the
// recorded listen set exactly:
//
//   SEERR_LISTEN="127.0.0.1:42011 172.17.0.1:42011 203.0.113.9:42011"
//
// The first http.Server that listens on $PORT binds the first address; every
// further address gets its own http.Server whose 'request' / 'upgrade' events
// are re-emitted on the app's server, so express sees one server. An error on
// any of them (EADDRINUSE, EADDRNOTAVAIL) is re-emitted as the app server's
// 'error', which upstream turns into process.exit(1): the unit restarts, it
// never runs half-bound.
//
// Fails CLOSED: SEERR_LISTEN unset/empty, a wildcard, a malformed entry or a
// port that differs from $PORT throws before Seerr loads.
const http = require('http');

function parseListen(raw, port) {
  const out = [];
  for (const item of String(raw || '').trim().split(/\s+/).filter(Boolean)) {
    const m = /^(?:\[([0-9A-Fa-f:.]+)\]|([0-9.]+)):(\d{1,5})$/.exec(item);
    if (!m) throw new Error('SEERR_LISTEN: malformed entry ' + JSON.stringify(item));
    const host = m[1] || m[2];
    const p = Number(m[3]);
    if (host === '0.0.0.0' || host === '::' || host === '*') {
      throw new Error('SEERR_LISTEN: wildcard ' + item + ' refused (never 0.0.0.0)');
    }
    if (p !== port) {
      throw new Error('SEERR_LISTEN: ' + item + ' is not on PORT ' + port);
    }
    out.push({ host, port: p });
  }
  if (!out.length) throw new Error('SEERR_LISTEN is not set; refusing to start');
  return out;
}

const PORT = Number(process.env.PORT);
if (!Number.isInteger(PORT) || PORT < 1 || PORT > 65535) {
  throw new Error('PORT is not set; refusing to start');
}
const ADDRS = parseListen(process.env.SEERR_LISTEN, PORT);

const origListen = http.Server.prototype.listen;
let claimed = false;

http.Server.prototype.listen = function qflixListen(...args) {
  const first = args[0];
  const port = typeof first === 'object' && first !== null ? Number(first.port) : Number(first);
  if (claimed || port !== PORT) return origListen.apply(this, args);
  claimed = true;
  const cb = args.find((a) => typeof a === 'function');
  const main = this;
  for (const a of ADDRS.slice(1)) {
    const extra = http.createServer();
    extra.on('request', (req, res) => main.emit('request', req, res));
    extra.on('upgrade', (req, sock, head) => main.emit('upgrade', req, sock, head));
    extra.on('error', (err) => main.emit('error', err));
    origListen.call(extra, a.port, a.host);
  }
  const head = ADDRS[0];
  return cb ? origListen.call(this, head.port, head.host, cb)
            : origListen.call(this, head.port, head.host);
};

module.exports = { parseListen };
