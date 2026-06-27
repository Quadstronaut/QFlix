# QFlix Dashboard — SvelteKit replacement for Homarr

**Date:** 2026-06-27
**Status:** Design (approved in brainstorm; pending spec review)
**Scope:** Replace the Homarr public landing board with a self-hosted SvelteKit
dashboard. Mobile-first, high-contrast "cinema" theme on the QFlix palette, six
tiles (seerr · plex · status · github · faq · support), live status dots, and a
**Plex-gated Support form** that posts to a Discord webhook. Decommission Homarr
in one clean cutover.

---

## 1. Why

The current Homarr board renders badly on mobile — clipped category labels,
mis-laid tiles, illegible on a phone (the medium most users actually open it on).
It's also a heavy Next.js app whose only job is to show six links and some status
dots. We replace it with a purpose-built SvelteKit app we fully control: gorgeous
on mobile, on-brand, and able to host the one piece of real logic we want — a
member-gated support request form.

### What Homarr does today (the thing we're replacing)

- `ucc`-class app, served by Ultra.cc at `homarr-upstream-<user>.<fqdn>`.
- User-nginx root (`location = /`) **302-redirects** to its `/boards/public`.
- Tiles SQLite-seeded by `scripts/configure/35-homarr-seed-boards.py`; theme by
  `61-homarr-qflix-theme.py`; comms widget by `46-homarr-add-comms.py`; root
  redirect by `34-nginx-root-to-homarr.sh`.
- Two Kuma monitors; the `mobile-ux` canary asserts root-302 + board-200 + HTML
  < 512 KB every 15 min.

The full handed-out URL chain today is three hops:

```
qflix.quadstronix.dev  (Porkbun URL-forward, CNAME → uixie.porkbun.com, 301)
  → https://<fqdn>/      (user-nginx root, 302)
    → https://homarr-upstream-<user>.<fqdn>/boards/public
```

That third host is the ugly string in the address bar. The new design collapses
this so the dashboard is served **directly at the seedbox root** (no 302 bounce).

---

## 2. Locked decisions

| Decision | Choice |
|---|---|
| Framework | SvelteKit (Svelte 5 / runes) + `@sveltejs/adapter-node` |
| Runtime | systemd user service on the seedbox, loopback port, behind user-nginx |
| Project location | new top-level `apps/qflix-dash/` |
| Tiles | seerr · plex · status · github · faq · **support** (audiobooks/comics dropped, see §13) |
| Status dots | reuse `manitoba-maint status --all --json`; GitHub tile gets a **reachability dot** |
| Support gate | Sign in with Plex (PIN flow) → membership: **Plex shared-users primary, Seerr fallback** |
| Support sink | Discord webhook, `username:"QFlix"`, avatar = the public Q.png |
| Scope | **Full build, then one clean cutover** (no half-state at root) |
| Visual | QFlix full palette (navy + orange/cyan/gold), cinema effects, blue Q |
| Greeting | silent Plex-detected "welcome back" on the landing page — check-only, silent fail, silent on empty |

---

## 3. Architecture

A static site can't hold the webhook secret, run the Plex PIN flow, or sign a
session — so the Support form forces a server. SvelteKit + `adapter-node` gives a
prerendered landing page (fast, cheap) **and** server `/api/*` endpoints in one
deploy. The runtime is de-risked: Homarr and Seerr are already Node apps on this
box, so Node-on-Ultra.cc is proven. Net long-running process count stays flat
(Homarr out, dashboard in), and the service slots into the manifest/Kuma/self-heal
model like every other systemd app.

```
Browser ── HTTPS ──▶ Ultra.cc outer nginx ──▶ user-nginx (:17040)
                                                 │  location ^~ /seerr  /faq …  (unchanged)
                                                 └─ location /  ──▶ 127.0.0.1:<qflix-dash.port>
                                                                      node build/index.js
                                                                      ├─ /            prerendered board
                                                                      ├─ /api/status  live dots
                                                                      ├─ /api/auth/*  Plex PIN flow
                                                                      ├─ /api/support webhook post
                                                                      └─ /healthz     liveness
```

### Origin handling (double-proxy)

The app is reachable behind two proxies (Ultra.cc outer nginx → user-nginx) and at
**two hostnames** (`qflix.quadstronix.dev` and `<fqdn>`).
Do **not** hardcode adapter-node `ORIGIN`. Instead set `PROTOCOL_HEADER=x-forwarded-proto`
and `HOST_HEADER=x-forwarded-host` so SvelteKit derives its origin per-request
(both hostnames pass CSRF origin checks; the Plex `forwardUrl` is built from the
live forwarded host so the user returns to whichever host they started on). The
user-nginx fragment must forward those headers; set `XFF_DEPTH=2` for correct
client IP in rate-limiting.

### Runtime Node (we install it)

The seedbox has **no user-accessible Node** — verified over SSH: the login shell
has no `node`/`npm`, no nvm; Homarr and Seerr run Node *inside Docker*, not in the
user environment. So the install script provisions **Node 20 in `$HOME` via nvm**,
and the systemd unit invokes the **absolute** nvm node path (not login-shell PATH).
Node's libuv threadpool is tiny (default 4) — no concern against the box's
`ulimit -u`. The SvelteKit bundle is **built on the workstation**; only the
prebuilt `build/` + production `node_modules` ship to the box (`npm ci --omit=dev`).

**Runtime flags (verified on the box, 2026-06-27 — undici WASM gotcha):** the slot
caps `ulimit -v` at ~10 GB (hard) while reporting ~515 GB RAM, so Node auto-sizes a
huge heap and undici's WASM HTTP parser can't reserve its ~8 GB trap guard region →
`RangeError: Cannot allocate Wasm memory` crash on the first outbound `fetch()`.
Fixed with `NODE_OPTIONS=--disable-wasm-trap-handler --max-old-space-size=512` (+
`UV_THREADPOOL_SIZE=2`) in the service env file. Verified reliable across restarts;
the full status path (manitoba-maint JSON + GitHub/FAQ reachability via fetch) works.
Runs as `qflix-dash.service` on loopback `:42020` with `~/.nvm/versions/node/v20.20.2/bin/node`.

---

## 4. Tiles

Six tiles, each = local SVG icon + non-truncating label + status puck + tap action.

| Tile | Action | Status puck source |
|---|---|---|
| Plex | → `https://<fqdn>/web/` | `plex` app `ok` from status JSON |
| Seerr | → `https://<fqdn>/seerr/` | `seerr` app `ok` from status JSON |
| Status | → Kuma `…/status/manitoba` | aggregate: green if `summary.down==0`, amber if non-core down, red if a core app down |
| GitHub | → `https://github.com/Quadstronaut/QFlix` | reachability HEAD to GitHub (cached ~5 min) |
| FAQ | → `https://<fqdn>/faq/` | same-origin HEAD (cached) |
| Support | open Support flow (modal/route) | dashboard self-health (green if rendering) |

Icons are **bundled locally** (`apps/qflix-dash/static/icons/`) rather than pulled
from the walkxcode CDN at runtime — same self-contained ethos as the FAQ page, and
removes a third-party runtime dependency.

---

## 5. Routes / components

- **`/`** — prerendered board. Cinema layout, responsive grid, hydrates to fetch
  `/api/status` for live dots. No secrets, no server work on the hot path.
- **`GET /api/status`** — server spawns `manitoba-maint status --all --json`
  (same box, same user, on PATH), maps the relevant apps → tile pucks, plus the
  GitHub/FAQ reachability checks. **Cache 30–60 s** (module-level, shared) so the
  endpoint can't hammer the maint daemon or GitHub. Returns
  `{ plex, seerr, status, github, faq, support }` each `{state, latency_ms?}`.
- **`GET /api/auth/plex/start`** — create a Plex PIN, return the `app.plex.tv/auth`
  URL (with a `forwardUrl` back to `/api/auth/plex/callback` on the live host).
- **`GET /api/auth/plex/callback`** — poll the PIN for `authToken`, fetch the
  signed-in identity, run the **membership check** (§7), and on success mint a
  signed httpOnly session cookie. Redirect back to the Support form.
- **`POST /api/support`** — requires a valid session; validates + rate-limits;
  posts to the Discord webhook (§8). Returns success/failure for a toast.
- **`GET /api/me`** — **silent** identity read for the landing-page greeting
  (§7a). Validates the cosmetic greeting cookie; returns `{name}` on success or
  an empty `200 {}` when absent/invalid. Never triggers a login, never errors to
  the client.
- **`GET /healthz`** — 200 plain liveness for the Kuma probe + smoke test.

---

## 6. Status dots — data flow

`manitoba-maint status --all --json` already exists on the box and emits the
schema_version-1 contract (`docs/superpowers/specs/2026-05-24-quadstronot-status-json-contract.md`):
`{schema_version, captured_at, summary:{total,up,down}, apps:[{app,display,ok,…}]}`.

`/api/status` runs it via `child_process`, reads `summary` + the `plex` / `seerr`
rows, and derives the aggregate **Status** puck. GitHub + FAQ reachability are
independent fetches with their own longer caches. This reuses the repo's single
source of truth instead of re-implementing health probing in the dashboard.

> If shelling to `manitoba-maint` proves awkward from Node, the fallback source is
> the public Kuma status page JSON (`…/status/manitoba`), which carries the same
> up/down truth. Decided at plan time; the contract is the preferred source.

---

## 7. Support gate — Plex sign-in + membership

### Sign in with Plex (PIN OAuth, server-side)

1. `POST https://plex.tv/api/v2/pins?strong=true` with headers
   `X-Plex-Product: QFlix`, `X-Plex-Client-Identifier: <qflix-dash.plex_client_id>`
   (a fixed UUID from secrets so PINs are consistent) → `{id, code}`.
2. Redirect the user to
   `https://app.plex.tv/auth#?clientID=<id>&code=<code>&forwardUrl=<callback>&context[device][product]=QFlix`.
3. On callback, poll `GET https://plex.tv/api/v2/pins/<id>` until `authToken`.
4. `GET https://plex.tv/api/v2/user` with the user's `authToken` → their Plex
   account id / username / email. **The user's token is used only to prove
   identity, then discarded — never stored.**

### Membership check (primary → fallback)

- **Primary — Plex shared-users.** Using the operator server token
  (`secrets/plex.token`), resolve the set of accounts authorized on the QFlix Plex
  server (owner + shared/Home users) and match the signed-in account's id/email.
  Preferred implementation: a small server-side helper using the existing
  **python-plexapi** venv (already a managed QFlix dependency) — Plex's sharing API
  is finicky and python-plexapi already gets it right — invoked by the Node server
  and cached (~10 min). Direct TS fetch is an acceptable alternative if it proves
  reliable.
- **Fallback — Seerr.** If not matched in Plex, query Seerr's local API
  `GET /seerr/api/v1/user` (`X-Api-Key: secrets/jellyseerr.key`) and match by
  Plex account id/email. Catches the edge case where someone's on Plex but not yet
  synced to Seerr, or vice-versa.
- **Neither →** show a friendly "this is for QFlix members — sign in with the Plex
  account you watch with" message; no form.

### Session

On a successful match, mint a **short-lived signed httpOnly+Secure+SameSite=Lax
cookie** (HMAC with `secrets/qflix-dash.session_secret`), carrying the verified
Plex username/email + an expiry (e.g. 30 min). The Support form reads this; the
submit endpoint re-verifies the signature server-side. No server-side session
store needed.

### 7a. Greeting cookie (cosmetic, long-lived)

Sign-in **also** sets a separate **long-lived (e.g. 30-day) signed cookie carrying
only the display name** — purely cosmetic, for the landing-page greeting (§13). It
is **never** trusted for the Support gate: `POST /api/support` always relies on the
short auth session (and re-checks membership), so a stale greeting cookie can't
authorize a submission. `GET /api/me` reads this cookie. Splitting the two keeps
the security-sensitive gate short-lived while letting "welcome back" persist across
visits.

---

## 8. Support submission → Discord

On `POST /api/support` with a valid session:

- Validate: non-empty message within a length cap; basic anti-abuse (honeypot
  field + per-account **rate-limit**, e.g. 3/hour keyed on the verified Plex id).
- Post to the Discord webhook (`secrets/qflix-dash.discord_webhook`) with:
  - `username: "QFlix"`
  - `avatar_url:` **`https://raw.githubusercontent.com/Quadstronaut/QFlix/master/Q.png`**
    (repo is public, so the GitHub raw URL is a stable public avatar — no extra hosting)
  - an embed: the message, the **verified** Plex username + email, and a UTC
    timestamp. (Identity comes from the server-side session, never from client
    input, so it can't be spoofed.)
- The webhook URL lives only in `secrets/` and is never sent to the client.

> **Webhook rotation — operator declined (2026-06-27).** The current webhook URL
> stays as-is. For the record: Discord webhooks are unauthenticated (URL = post
> access) and this one appeared in a chat transcript, but the operator has accepted
> that and is not rotating it. The value still lives only in
> `secrets/qflix-dash.discord_webhook` and is never shipped to the client.

---

## 9. Secrets

New (one-line files in `~/secrets/`, gitignored; add to `docs/secrets-convention.md`):

| Secret | Purpose |
|---|---|
| `qflix-dash.port` | loopback port the Node service binds |
| `qflix-dash.discord_webhook` | Support → Discord (regenerated, §8) |
| `qflix-dash.session_secret` | HMAC key for the signed session cookie |
| `qflix-dash.plex_client_id` | stable `X-Plex-Client-Identifier` UUID |

Reused: `plex.token`, `plex.host`, `jellyseerr.key`, `jellyseerr.port`,
`seedbox.host`. The install script materializes these into the service's
environment (`~/.config/qflix-dash/qflix-dash.env`, mode 600) — the secrets stay
the source of truth.

---

## 10. URL & DNS continuity

### The new homepage URL (DNS — already wired, no change needed)

Post-cutover, the dashboard is served at the **seedbox root** `https://<fqdn>/`.

`qflix.quadstronix.dev` is a **Porkbun URL-forward** (CNAME → `uixie.porkbun.com`)
that **already 301s to `https://<fqdn>/`** (verified 2026-06-27). Since the seedbox
root is exactly where the new dashboard will serve, **no DNS or Porkbun change is
required** — the same forward simply lands on the new board instead of the old
homarr 302. The address bar settles on `<fqdn>` (as it does today), now showing a
clean dashboard instead of the ugly `homarr-upstream-…` host.

Ultra.cc custom domains are **not an option** — the UCP supports no third-party
integrations of any kind, and you can't bring your own cert on a shared slot. The
*only* way to make `qflix.quadstronix.dev` itself stay in the address bar would be
to front it with a CDN that terminates TLS for the domain (e.g. Cloudflare proxy,
DNS-side — nothing to do with UCP) and proxies to the seedbox origin. Left as an
optional future polish; **not in scope** for this build.

### Forwarding the old Homarr URL

The old URL is `https://homarr-upstream-<user>.<fqdn>/boards/public`.
That subdomain is an **Ultra.cc-owned outer-nginx vhost** (with an Ultra.cc-managed
cert) — we can't add a redirect to it, and it's **torn down when Homarr is
uninstalled**. So:

- **The only widely-handed-out URL is `qflix.quadstronix.dev`**, which we repoint
  in §10. Nobody bookmarks the `homarr-upstream-…` host on purpose — users only
  ever saw it transiently mid-redirect. So the practical forwarding need is small.
- **Best-effort old-subdomain forward (optional).** If we `app-homarr stop`
  instead of `uninstall`, the Ultra.cc subdomain vhost survives (app still
  "installed"), and we can bind a featherweight redirect on the freed Homarr port
  returning `301 → https://qflix.quadstronix.dev/`. This is **fragile** (Ultra.cc
  app management or upgrades can clobber it) and keeps the slot occupied. Treat as
  optional belt-and-suspenders, not the primary plan.
- **Default:** uninstall Homarr cleanly, repoint `qflix.quadstronix.dev`, and
  re-broadcast the pretty domain as canonical. Document the dead old-subdomain in
  the transition log.

---

## 11. Ops integration

- **`manifest/apps.yaml`** — add:

  ```yaml
  qflix-dash:
    class: systemd
    systemd_unit: qflix-dash.service
    kuma_monitor: "QFlix Dashboard"
    parked: false
    health:
      kind: http_root
      path_template: "/healthz"
      port_secret: qflix-dash.port
      expect_status: 200
  ```

  and **remove** the `homarr` entry.
- **systemd unit** `qflix-dash.service` — `ExecStart=node build/index.js`,
  `EnvironmentFile=%h/.config/qflix-dash/qflix-dash.env`, `Restart=always`,
  loopback bind. Model on the existing `listmonk` / `victorialogs` user units.
  Keep it lean (Node libuv threadpool is small; mind the box's `ulimit -u`
  per the seedbox thread-cap note, but Node won't approach it).
- **nginx** `~/.apps/nginx/proxy.d/qflix-dash.conf` — make `location /`
  `proxy_pass http://127.0.0.1:<qflix-dash.port>;` with `auth_basic off;`
  (the homepage is public, resolving the prior script-34 ambiguity), WebSocket
  upgrade headers, and `X-Forwarded-Host`/`X-Forwarded-Proto` pass-through. The
  existing `^~ /seerr`, `/faq`, `/tautulli`, … prefix locations keep precedence.
  Remove the `34-nginx-root-to-homarr.sh` injected 302 block.
- **Kuma** — add the "QFlix Dashboard" monitor; remove Homarr's two monitors via
  `bootstrap-kuma-monitors.py`.
- **`mobile-ux` canary** — repoint from the Homarr board to the new dashboard:
  assert root `200` (not `302`), a content marker (e.g. a `data-qflix-dash`
  attribute or the wordmark), and the existing HTML-size budget.
- **Install script** — `scripts/configure/NN-qflix-dash-install.sh` (numbered,
  idempotent): checkout/build (`npm ci && npm run build`), write the env file from
  secrets, install the unit + nginx fragment, enable+start, seed the Kuma monitor.
  Mirrors the existing `scripts/configure/*` pattern.

---

## 12. Homarr decommission (the cutover)

Follow the Maintainerr/Jellyfin decom pattern (transition-log entry, reversible).

1. Build + deploy the dashboard; service up on its port; nginx fragment staged
   (root still → Homarr until the switch). Smoke the dashboard via its loopback
   port + tunnel.
2. **Switch:** swap user-nginx root to proxy the dashboard, reload nginx. Root now
   serves the dashboard.
3. Repoint `qflix.quadstronix.dev` (§10). Verify end-to-end on a phone.
4. `app-homarr uninstall` (or `stop` if doing the optional old-URL shim).
5. Manifest: remove `homarr`, add `qflix-dash`. Kuma: drop Homarr monitors, add
   dashboard. Retire the homarr-specific configure scripts (34/35/46/61) — mark
   superseded; history keeps them.
6. Update `inventory.md`, `README.md` (the "reachable without tunnel" table +
   counts), and `docs/transition-log.md`.

---

## 13. Visual design

**Palette (QFlix full):** base navy `#0a1628` / `#05101f` / `#0e1d33`; accents
orange `#ff8c42`, cyan `#7dd3fc`, gold `#d4af37`; text `#f8fafc` / `#cbd5e1`. Blue
Q logo as the hero mark.

**Cinema effects (sharp, high-contrast):**
- Letterbox bars top/bottom (thin, subtle gradient) for the widescreen frame.
- Spotlight: radial-gradient glow behind the Q hero mark.
- Film grain: fixed low-opacity noise overlay, `pointer-events:none`, GPU-cheap.
- Optional scanlines: faint `repeating-linear-gradient` overlay.
- Sharp edges (minimal/zero border-radius per the "sharp" ask), 1px cyan/gold
  accent borders, glow on focus/hover.
- Tile hover = "now playing" lift + accent glow + slight scale.

**Mobile-first (the whole point):** single-column stack on phones, full-width
tiles, large tap targets, **no clipped labels** (responsive type, wrapping,
min-heights). Desktop: CSS-grid `auto-fit minmax(...)`. The Homarr screenshot's
truncated "REQUESTS…/AUDIOBOO…/COMICS…" is the explicit failure we're fixing.

**Silent greeting:** after hydration the board calls `GET /api/me`. If a valid
greeting cookie is present, a cinema-flavored welcome fades into the hero (e.g.
"Now showing for &lt;name&gt;") near the Q mark — with space reserved so there's no
layout shift. **Silent check** (never triggers a login popup), **silent fail** (any
error renders nothing), **silent on empty** (not-signed-in just shows the normal
board). Only ever greets someone already authenticated via the Support flow.

**Accessibility & perf:** WCAG-AA contrast (high contrast is requested anyway),
visible focus states, `prefers-reduced-motion` disables grain/scanline animation,
semantic HTML, aria-labelled status pucks. Prerender the landing page, inline
critical CSS, bundle icons locally; stay well under the canary's 512 KB HTML
budget.

---

## 14. Error handling

- **Status unavailable** (`manitoba-maint` errors / times out): pucks render in a
  neutral "unknown" grey, never block the board; cache serves the last good value.
- **GitHub/FAQ reachability fail:** that one puck goes grey; others unaffected.
- **Plex PIN never authorized / times out:** the flow times out gracefully with a
  retry prompt; no session minted.
- **Membership check source down:** if Plex is unreachable, fall to Seerr; if both
  are down, deny with a "try again shortly" message (fail closed — never grant on
  error).
- **Webhook post fails:** surface a "couldn't send, try again" toast; do not lose
  the user's typed message (keep it in the form).
- **Rate-limit hit:** friendly "you've sent a few recently — give it a bit."

---

## 15. Testing

- **pytest** (repo convention): manifest asserts `qflix-dash` present with the
  expected probe, `homarr` absent.
- **Vitest / Playwright** (`apps/qflix-dash/`): tile render + no-truncation at
  mobile widths; `/api/status` mapping (mocked `manitoba-maint` JSON); Plex PIN
  flow (mocked plex.tv); membership primary + Seerr fallback (mocked); deny path;
  support post (mocked webhook) incl. identity-from-session-not-client; rate-limit;
  session signature tamper rejection; `prefers-reduced-motion`.
- **smoke-test.sh**: extend so root serves the dashboard (`200` + content marker)
  instead of `302` → homarr; `/healthz` 200.
- Playwright is already available in this repo (MCP + `playwright-best-practices`
  skill) for the mobile-layout assertions.

---

## 16. Operator-deferred / open items

Resolved during spec review (2026-06-27):

- **UCP custom domain — N/A.** Ultra.cc UCP supports no third-party integrations
  (no custom domains, no BYO cert). Plan uses the existing Porkbun forward, which
  already targets the seedbox root — **no DNS change needed** (§10).
- **Node on the box — none; we install it.** Verified over SSH: no user `node`/
  `npm`, no nvm (Homarr/Seerr run Node inside Docker). The install script
  provisions **Node 20 in `$HOME` via nvm**; the systemd unit pins the absolute
  node path (§3, §11).
- **Webhook rotation — declined.** Operator keeps the current webhook (§8).

Still genuinely deferred:

- **Plex client id:** generate the stable UUID for `qflix-dash.plex_client_id`
  (the install script can mint it on first run).

---

## 17. Out of scope / future

- **Audiobooks + Comics tiles** are intentionally dropped for v1 (Komga / Kavita /
  Calibre-Web / Audiobookshelf). Re-add as a second tile group once the core six
  are shipped — the grid + tile component are built to take more.
- **Admin board** (the *arr stack tiles Homarr's admin board carried) is not
  reproduced here; admin access stays on the SSH tunnel per
  `docs/internal-app-tunnels.md`.
- No analytics, no per-user board customization — this is a fixed, fast landing
  page, by design.
