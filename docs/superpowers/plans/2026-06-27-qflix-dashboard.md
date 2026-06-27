# QFlix Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the mobile-hostile Homarr board with a self-hosted SvelteKit dashboard — six tiles, live status dots, a Plex-gated Support→Discord form, and a silent "welcome back" greeting — then cut over and decommission Homarr.

**Architecture:** SvelteKit (Svelte 5) + `@sveltejs/adapter-node`, built on the workstation, run on the seedbox as a systemd `--user` service behind user-nginx at root. Prerendered landing page; dynamic `/api/*` endpoints for status, Plex auth, support, and the greeting. Membership verification reuses the box's existing python-plexapi; status reuses `manitoba-maint status --all --json`.

**Tech Stack:** SvelteKit 2 / Svelte 5 (runes), TypeScript, Vite, Vitest, Playwright; Node 20 (nvm, $HOME) at runtime; Python 3 (existing python-plexapi venv) for the membership helper; systemd user units; user-nginx (`app-nginx`).

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-27-qflix-dashboard-design.md` is the source of truth; this plan implements it.
- **Project location:** `apps/qflix-dash/` (new top-level dir).
- **No root, ever** — everything in `$HOME`; Node via nvm (user-space).
- **Port:** must come from `app-ports free` (never an arbitrary/default port — FUP risk); stored in `secrets/qflix-dash.port`.
- **nginx:** fragments live at `~/.apps/nginx/proxy.d/<app>.conf`; reload with `app-nginx restart`.
- **Persistence:** systemd `--user` unit at `~/.config/systemd/user/`, `WantedBy=default.target`.
- **Origin:** never hardcode adapter-node `ORIGIN`; use `PROTOCOL_HEADER=x-forwarded-proto`, `HOST_HEADER=x-forwarded-host`, `XFF_DEPTH=2`.
- **Secrets:** one-line files in `~/secrets/`; never committed; the install script materializes them into the service env file (mode 600).
- **Identity is server-trusted:** the Support submitter's identity comes only from the signed session, never client input.
- **Hosts in committed files:** sanitized to `<fqdn>` / `<user>` per repo convention (README note). Real values from `secrets/seedbox.host`.
- **Palette:** base `#0a1628`/`#05101f`/`#0e1d33`; accents `#ff8c42` (orange) / `#7dd3fc` (cyan) / `#d4af37` (gold); text `#f8fafc`/`#cbd5e1`.
- **Webhook NOT rotated** (operator decision) — use the value as supplied, stored only in `secrets/qflix-dash.discord_webhook`.
- **Commit cadence:** small, labelled commits per task; push to `master` after each phase.

---

## File Structure

```
apps/qflix-dash/
  svelte.config.js              # adapter-node
  vite.config.ts
  package.json / package-lock.json
  tsconfig.json
  playwright.config.ts
  static/
    icons/{plex,seerr,kuma,github,faq,support}.svg   # bundled locally
    q.svg | q.png                # hero mark (from repo Q.png)
    grain.png                    # film-grain texture (tiny, tiled)
  src/
    app.html                     # letterbox shell, meta
    app.css                      # palette tokens + cinema effects + reduced-motion
    lib/
      tiles.ts                   # the 6 tile definitions (label/icon/href/statusKey)
      components/
        Tile.svelte
        StatusPuck.svelte
        Board.svelte
        Hero.svelte              # Q mark + spotlight + greeting slot
        SupportModal.svelte
      server/
        plex.ts                  # PIN flow + identity (plex.tv)
        membership.ts            # Plex-shared-users primary + Seerr fallback
        session.ts               # HMAC sign/verify; auth + greeting cookies
        status.ts                # manitoba-maint json + reachability, cached
        ratelimit.ts             # per-account in-memory limiter
        env.ts                   # typed env access
    routes/
      +layout.svelte
      +page.svelte               # the board (prerendered)
      +page.ts                   # export const prerender = true
      api/
        status/+server.ts
        me/+server.ts
        support/+server.ts
        auth/plex/start/+server.ts
        auth/plex/callback/+server.ts
      healthz/+server.ts

scripts/qflix-dash/
  plex_members.py                # uses existing python-plexapi venv; prints JSON
  qflix-dash.service.tmpl        # systemd unit template
  qflix-dash.nginx.conf.tmpl     # nginx root fragment template

scripts/configure/
  90-qflix-dash-install.sh       # idempotent installer (node/nvm, ship, env, unit, nginx, kuma)

manifest/apps.yaml               # +qflix-dash, -homarr
tests/unit/test_dashboard_manifest.py
```

---

## Phase 0 — Scaffold

### Task 0.1: Workstation Node + SvelteKit scaffold

**Files:**
- Create: `apps/qflix-dash/**` (scaffold)

- [ ] **Step 1: Verify workstation Node ≥ 20**

Run: `node -v && npm -v`
Expected: `v20.x`+ (if absent, install Node 20 LTS first). This is the *build* host; the box gets its own Node in Phase 6.

- [ ] **Step 2: Scaffold the SvelteKit project**

Run (from repo root):
```bash
npm create svelte@latest apps/qflix-dash   # Skeleton project, TypeScript, add Prettier; Svelte 5
cd apps/qflix-dash && npm install
npm install -D @sveltejs/adapter-node @playwright/test
```

- [ ] **Step 3: Switch to adapter-node**

`apps/qflix-dash/svelte.config.js`:
```js
import adapter from '@sveltejs/adapter-node';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

export default {
  preprocess: vitePreprocess(),
  kit: { adapter: adapter() }
};
```

- [ ] **Step 4: Verify dev server boots**

Run: `npm run dev -- --port 5180`
Expected: serves the skeleton at `http://localhost:5180`. Ctrl-C.

- [ ] **Step 5: Add a root `.gitignore` entry for build artifacts**

Append to `apps/qflix-dash/.gitignore` (created by scaffold): ensure `node_modules`, `/build`, `/.svelte-kit`, `test-results/` are ignored.

- [ ] **Step 6: Commit**

```bash
git add apps/qflix-dash
git commit -m "feat(dash): scaffold SvelteKit (Svelte 5, adapter-node) at apps/qflix-dash"
```

---

## Phase 1 — Theme & design system

### Task 1.1: Palette tokens + cinema effects

**Files:**
- Create: `apps/qflix-dash/src/app.css`
- Modify: `apps/qflix-dash/src/app.html`

**Interfaces:**
- Produces: CSS custom properties `--bg-0/-1/-2`, `--accent-orange/-cyan/-gold`, `--text/-dim`, `--ok/-warn/-down/-unknown`; utility classes `.letterbox`, `.grain`, `.scanlines`, `.spotlight`.

- [ ] **Step 1: Write the token + effects stylesheet**

`src/app.css` (load-bearing — full content):
```css
:root {
  --bg-0:#05101f; --bg-1:#0a1628; --bg-2:#0e1d33;
  --accent-orange:#ff8c42; --accent-cyan:#7dd3fc; --accent-gold:#d4af37;
  --text:#f8fafc; --text-dim:#cbd5e1;
  --ok:#3ad17a; --warn:#d4af37; --down:#ff5a52; --unknown:#5b6b82;
  --tile-border:rgba(125,211,252,.18);
  --radius:2px;            /* "sharp" */
  --maxw:1100px;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg-0);color:var(--text);
  font-family:"Segoe UI",Roboto,system-ui,sans-serif;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}

/* widescreen letterbox frame */
.letterbox::before,.letterbox::after{content:"";position:fixed;left:0;right:0;height:34px;z-index:5;
  background:linear-gradient(var(--bg-0),rgba(5,16,31,0));pointer-events:none}
.letterbox::before{top:0}
.letterbox::after{bottom:0;transform:scaleY(-1)}

/* spotlight glow behind the hero mark */
.spotlight{position:relative}
.spotlight::before{content:"";position:absolute;inset:-40% -10% auto;height:340px;z-index:0;
  background:radial-gradient(60% 60% at 50% 0,rgba(125,211,252,.16),rgba(125,211,252,0) 70%);
  pointer-events:none}

/* film grain (GPU-cheap, fixed, decorative) */
.grain::after{content:"";position:fixed;inset:0;z-index:30;opacity:.05;pointer-events:none;
  background:url("/grain.png");background-size:160px}

/* faint scanlines */
.scanlines::before{content:"";position:fixed;inset:0;z-index:29;pointer-events:none;opacity:.05;
  background:repeating-linear-gradient(0deg,#000 0 1px,transparent 1px 3px)}

@media (prefers-reduced-motion: reduce){
  *{animation:none!important;transition:none!important}
}
```

- [ ] **Step 2: Wire the shell**

`src/app.html` — add `class="letterbox grain scanlines"` to `<body>`, set `<meta name="theme-color" content="#05101f">`, `<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">`, and `data-qflix-dash` on `<body>` (the canary/smoke content marker). Import `app.css` in `+layout.svelte`.

- [ ] **Step 3: Commit**

```bash
git add apps/qflix-dash/src
git commit -m "feat(dash): QFlix palette tokens + cinema effects (letterbox/grain/scanlines/spotlight)"
```

---

## Phase 2 — Board UI (prerendered)

### Task 2.1: Tile data + StatusPuck + Tile

**Files:**
- Create: `src/lib/tiles.ts`, `src/lib/components/StatusPuck.svelte`, `src/lib/components/Tile.svelte`
- Create: `src/lib/components/StatusPuck.test.ts`
- Add icons: `static/icons/*.svg`, `static/q.svg`, `static/grain.png`

**Interfaces:**
- Produces:
  - `type TileState = 'ok'|'warn'|'down'|'unknown'`
  - `interface TileDef { key:string; label:string; icon:string; href?:string; action?:'support'; statusKey:string }`
  - `export const TILES: TileDef[]`
  - `StatusPuck` prop `state:TileState`; `Tile` props `tile:TileDef`, `state:TileState`.

- [ ] **Step 1: Define tiles**

`src/lib/tiles.ts`:
```ts
export type TileState = 'ok' | 'warn' | 'down' | 'unknown';
export interface TileDef {
  key: string; label: string; icon: string;
  href?: string; action?: 'support'; statusKey: string;
}
// hrefs are built at render from the live host (see Board); paths only here.
export const TILES: TileDef[] = [
  { key:'seerr',  label:'Requests',     icon:'/icons/seerr.svg',  href:'/seerr/',   statusKey:'seerr'  },
  { key:'plex',   label:'Watch',        icon:'/icons/plex.svg',   href:'/web/',     statusKey:'plex'   },
  { key:'status', label:'Status',       icon:'/icons/kuma.svg',   href:'/status/manitoba', statusKey:'status' },
  { key:'github', label:'Source',       icon:'/icons/github.svg', href:'https://github.com/Quadstronaut/QFlix', statusKey:'github' },
  { key:'faq',    label:'FAQ',          icon:'/icons/faq.svg',    href:'/faq/',     statusKey:'faq'    },
  { key:'support',label:'Support',      icon:'/icons/support.svg', action:'support', statusKey:'support' },
];
```

- [ ] **Step 2: Write the StatusPuck failing test**

`src/lib/components/StatusPuck.test.ts`:
```ts
import { render } from '@testing-library/svelte';
import { expect, test } from 'vitest';
import StatusPuck from './StatusPuck.svelte';

test('puck reflects state via aria-label + data-state', () => {
  const { getByRole } = render(StatusPuck, { props: { state: 'down' } });
  const el = getByRole('img');
  expect(el.getAttribute('data-state')).toBe('down');
  expect(el.getAttribute('aria-label')?.toLowerCase()).toContain('down');
});
```

- [ ] **Step 3: Run it — expect FAIL** (`npm run test -- StatusPuck` → component missing). Add `@testing-library/svelte`, `jsdom` to devDeps and a vitest config if scaffold lacks them.

- [ ] **Step 4: Implement StatusPuck**

`src/lib/components/StatusPuck.svelte`:
```svelte
<script lang="ts">
  import type { TileState } from '$lib/tiles';
  let { state = 'unknown' as TileState } = $props();
  const label: Record<TileState,string> = {
    ok:'Online', warn:'Degraded', down:'Down', unknown:'Status unknown'
  };
</script>
<span class="puck" role="img" data-state={state} aria-label={label[state]}></span>
<style>
  .puck{width:10px;height:10px;border-radius:50%;display:inline-block}
  [data-state="ok"]{background:var(--ok);box-shadow:0 0 8px var(--ok)}
  [data-state="warn"]{background:var(--warn);box-shadow:0 0 8px var(--warn)}
  [data-state="down"]{background:var(--down);box-shadow:0 0 8px var(--down)}
  [data-state="unknown"]{background:var(--unknown)}
</style>
```

- [ ] **Step 5: Run test — expect PASS.**

- [ ] **Step 6: Implement Tile**

`src/lib/components/Tile.svelte` — renders an `<a>` (link tiles) or `<button>` (support action), icon, non-truncating label, and `StatusPuck`. Key rules: `min-height:120px`, label `white-space:normal; overflow-wrap:anywhere; font-size:clamp(.95rem,3.6vw,1.15rem)`; hover = lift + accent glow; sharp corners; emits `support` event when `tile.action==='support'`.
```svelte
<script lang="ts">
  import type { TileDef, TileState } from '$lib/tiles';
  import StatusPuck from './StatusPuck.svelte';
  let { tile, state = 'unknown' as TileState, onsupport } = $props<{
    tile: TileDef; state?: TileState; onsupport?: () => void;
  }>();
  const ext = tile.href?.startsWith('http');
</script>
{#if tile.action === 'support'}
  <button class="tile" onclick={() => onsupport?.()}>
    <img class="ic" src={tile.icon} alt="" /><span class="lbl">{tile.label}</span>
    <StatusPuck {state} />
  </button>
{:else}
  <a class="tile" href={tile.href} target={ext ? '_blank' : null} rel={ext ? 'noreferrer' : null}>
    <img class="ic" src={tile.icon} alt="" /><span class="lbl">{tile.label}</span>
    <StatusPuck {state} />
  </a>
{/if}
<style>
  .tile{display:flex;flex-direction:column;gap:.6rem;align-items:flex-start;justify-content:space-between;
    min-height:120px;padding:1rem;background:linear-gradient(180deg,var(--bg-2),var(--bg-1));
    border:1px solid var(--tile-border);border-radius:var(--radius);color:var(--text);
    width:100%;text-align:left;cursor:pointer;transition:transform .12s,border-color .12s,box-shadow .12s}
  .tile:hover,.tile:focus-visible{transform:translateY(-3px);border-color:var(--accent-cyan);
    box-shadow:0 6px 22px rgba(125,211,252,.18);outline:none}
  .ic{width:34px;height:34px}
  .lbl{font-weight:700;letter-spacing:.02em;white-space:normal;overflow-wrap:anywhere;
    font-size:clamp(.95rem,3.6vw,1.15rem)}
</style>
```

- [ ] **Step 7: Commit**

```bash
git add apps/qflix-dash/src apps/qflix-dash/static
git commit -m "feat(dash): Tile + StatusPuck components and tile definitions"
```

### Task 2.2: Hero + Board + prerendered page

**Files:**
- Create: `src/lib/components/Hero.svelte`, `src/lib/components/Board.svelte`
- Create: `src/routes/+layout.svelte`, `src/routes/+page.svelte`, `src/routes/+page.ts`
- Create: `tests/e2e/board.spec.ts`, `playwright.config.ts`

**Interfaces:**
- Consumes: `TILES`, `Tile`, `Hero`.
- Produces: prerendered `/` with `data-qflix-dash` marker; a `#greeting` slot Hero exposes for Phase 4.

- [ ] **Step 1: Hero** — Q mark in a `.spotlight` wrapper, wordmark "QFlix", and an empty `#greeting` element (reserved height to avoid layout shift). Cyan/gold accent rule under the wordmark.

- [ ] **Step 2: Board** — responsive grid:
```svelte
<script lang="ts">
  import { TILES } from '$lib/tiles';
  import Tile from './Tile.svelte';
  import type { TileState } from '$lib/tiles';
  let { states = {} as Record<string,TileState>, onsupport } = $props();
</script>
<section class="grid">
  {#each TILES as t (t.key)}
    <Tile tile={t} state={states[t.statusKey] ?? 'unknown'} {onsupport} />
  {/each}
</section>
<style>
  .grid{display:grid;gap:.9rem;max-width:var(--maxw);margin:1.2rem auto;padding:0 1rem;
    grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
  @media (max-width:520px){ .grid{grid-template-columns:1fr} }  /* single column on phones */
</style>
```

- [ ] **Step 3: Page + prerender**

`src/routes/+page.ts`: `export const prerender = true;`
`src/routes/+page.svelte`: mounts `Hero` + `Board`; `onMount` → fetch `/api/status` (Phase 3) and `/api/me` (Phase 4); opens `SupportModal` on the support event. (Stub the fetches as no-ops until those phases land — states stay `unknown`.)

- [ ] **Step 4: Playwright mobile no-truncation test**

`tests/e2e/board.spec.ts`:
```ts
import { test, expect, devices } from '@playwright/test';
test.use({ ...devices['Pixel 5'] });
test('labels are never clipped on mobile', async ({ page }) => {
  await page.goto('/');
  for (const lbl of page.locator('.lbl')) { /* see config note */ }
  const labels = page.locator('.lbl');
  const n = await labels.count();
  for (let i=0;i<n;i++){
    const el = labels.nth(i);
    const clipped = await el.evaluate((e:HTMLElement)=> e.scrollWidth > e.clientWidth + 1);
    expect(clipped, await el.innerText()).toBe(false);
  }
  await expect(page.locator('body[data-qflix-dash]')).toBeVisible();
});
```
`playwright.config.ts`: `webServer: { command:'npm run build && node build', port: 3000 }`, `use:{ baseURL:'http://localhost:3000' }`.

- [ ] **Step 5: Run** `npm run build && npx playwright test` → expect PASS (labels wrap, no clipping). Fix CSS if any label clips.

- [ ] **Step 6: Commit**

```bash
git add apps/qflix-dash
git commit -m "feat(dash): hero, responsive board grid, prerendered landing + mobile no-clip test"
```

---

## Phase 3 — Status API + live dots

### Task 3.1: status.ts (manitoba-maint + reachability, cached)

**Files:**
- Create: `src/lib/server/status.ts`, `src/lib/server/env.ts`
- Create: `src/lib/server/status.test.ts`
- Create: `src/routes/api/status/+server.ts`, `src/routes/healthz/+server.ts`

**Interfaces:**
- Produces:
  - `getStatus(): Promise<Record<string,TileState>>` — keys `plex,seerr,status,github,faq,support`, 30 s cache.
  - `env` typed accessor (reads `process.env`).

- [ ] **Step 1: env accessor**

`src/lib/server/env.ts`:
```ts
import { env } from '$env/dynamic/private';
export const cfg = {
  maintBin: env.MANITOBA_MAINT_BIN || 'manitoba-maint',
  plexToken: env.PLEX_TOKEN || '',
  plexClientId: env.PLEX_CLIENT_ID || '',
  plexMembersPy: env.PLEX_MEMBERS_PY || '',     // "<venv-python> <script>"
  seerrUrl: env.SEERR_URL || '',                 // e.g. http://127.0.0.1:42011
  seerrKey: env.SEERR_API_KEY || '',
  discordWebhook: env.DISCORD_WEBHOOK || '',
  sessionSecret: env.SESSION_SECRET || '',
  qAvatar: env.Q_AVATAR_URL || 'https://raw.githubusercontent.com/Quadstronaut/QFlix/master/Q.png',
  faqUrl: env.FAQ_PROBE_URL || ''                // same-origin /faq/ absolute, set in env
};
```

- [ ] **Step 2: Write failing test for status mapping**

`src/lib/server/status.test.ts`:
```ts
import { expect, test, vi } from 'vitest';
test('maps maint json to tile states; aggregate down -> status warn/down', async () => {
  vi.mock('node:child_process', () => ({ execFile: (_c:any,_a:any,cb:any)=>
    cb(null, JSON.stringify({ summary:{total:3,up:2,down:1},
      apps:[{app:'plex',ok:true},{app:'seerr',ok:false},{app:'sonarr',ok:true}] }), '') }));
  const { mapMaint } = await import('./status');
  const s = mapMaint({ summary:{total:3,up:2,down:1},
    apps:[{app:'plex',ok:true},{app:'seerr',ok:false}] } as any);
  expect(s.plex).toBe('ok'); expect(s.seerr).toBe('down');
  expect(['warn','down']).toContain(s.status);
});
```

- [ ] **Step 3: Run — expect FAIL** (no `mapMaint`).

- [ ] **Step 4: Implement status.ts**

`mapMaint(json)` derives per-app `ok→'ok'`/`false→'down'`; `status` = `down` if a *core* app (plex/seerr) is down, else `warn` if `summary.down>0`, else `ok`. `getStatus()` = run `execFile(cfg.maintBin,['status','--all','--json'])` (reject→all `unknown`), plus `github` (HEAD `https://api.github.com/` with 3 s timeout) and `faq` (HEAD `cfg.faqUrl`), `support`='ok'. Cache the whole object 30 s (module `let cache={t,val}` using `Date.now()`).

- [ ] **Step 5: Run test — expect PASS.**

- [ ] **Step 6: Endpoints**

`src/routes/api/status/+server.ts`:
```ts
import { json } from '@sveltejs/kit';
import { getStatus } from '$lib/server/status';
export const GET = async () => json(await getStatus());
```
`src/routes/healthz/+server.ts`:
```ts
export const GET = () => new Response('ok', { status: 200 });
```

- [ ] **Step 7: Wire client** — in `+page.svelte` `onMount`, `fetch('/api/status')` → update `states`. Pucks go live.

- [ ] **Step 8: Commit**

```bash
git add apps/qflix-dash/src
git commit -m "feat(dash): /api/status from manitoba-maint json + reachability, cached; /healthz"
```

---

## Phase 4 — Plex auth, session, greeting

### Task 4.1: session.ts (HMAC cookies)

**Files:**
- Create: `src/lib/server/session.ts`, `src/lib/server/session.test.ts`

**Interfaces:**
- Produces:
  - `signAuth(p:{u:string;e:string}, ttlSec=1800): string` / `verifyAuth(token:string): {u:string;e:string}|null`
  - `signGreeting(name:string, ttlSec=2592000): string` / `verifyGreeting(token:string): {n:string}|null`
  - cookie names `AUTH='qd_s'`, `GREET='qd_g'`.

- [ ] **Step 1: Failing test** — sign→verify round-trips; tampered payload → null; expired → null; wrong secret → null.
```ts
import { expect, test, beforeAll } from 'vitest';
beforeAll(()=>{ process.env.SESSION_SECRET='test-secret-please-change'; });
test('auth cookie round-trips and rejects tampering', async () => {
  const { signAuth, verifyAuth } = await import('./session');
  const tok = signAuth({u:'kyle',e:'k@x.com'});
  expect(verifyAuth(tok)).toMatchObject({u:'kyle',e:'k@x.com'});
  expect(verifyAuth(tok.slice(0,-2)+'xx')).toBeNull();
});
```

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement** with `node:crypto` HMAC-SHA256 over `base64url(JSON{...,exp})`, constant-time compare (`crypto.timingSafeEqual`), exp check. Greeting variant identical with `{n}` payload + 30 d ttl.

- [ ] **Step 4: Run — PASS. Commit**
```bash
git commit -am "feat(dash): HMAC-signed auth + greeting cookies (tamper/expiry safe)"
```

### Task 4.2: plex.ts (PIN flow + identity)

**Files:**
- Create: `src/lib/server/plex.ts`, `src/lib/server/plex.test.ts`

**Interfaces:**
- Produces:
  - `createPin(): Promise<{id:number;code:string}>`
  - `authUrl(code:string, forwardUrl:string): string`
  - `pollPin(id:number): Promise<string|null>` (authToken or null)
  - `whoami(authToken:string): Promise<{id:number;username:string;email:string}>`

- [ ] **Step 1: Failing test** (mock `fetch`): `createPin` POSTs `https://plex.tv/api/v2/pins?strong=true` with `X-Plex-Client-Identifier` + `X-Plex-Product`; `authUrl` contains `clientID`, `code`, encoded `forwardUrl`; `whoami` parses `{id,username,email}`.

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement** — all calls send headers `accept: application/json`, `X-Plex-Product: 'QFlix'`, `X-Plex-Client-Identifier: cfg.plexClientId`; `whoami` sends `X-Plex-Token`. `authUrl` = `https://app.plex.tv/auth#?clientID=${cid}&code=${code}&context%5Bdevice%5D%5Bproduct%5D=QFlix&forwardUrl=${encodeURIComponent(forwardUrl)}`.

- [ ] **Step 4: Run — PASS. Commit.**

### Task 4.3: plex_members.py + membership.ts (primary + fallback)

**Files:**
- Create: `scripts/qflix-dash/plex_members.py`
- Create: `src/lib/server/membership.ts`, `src/lib/server/membership.test.ts`

> **Execution-time verifications (do first, record findings in the commit body):**
> - Locate the existing python-plexapi venv (grep `scripts/mcp/plex.py` + `inventory.md`; e.g. `~/.apps/python-plexapi/.venv/bin/python`).
> - Confirm `secrets/plex.token` is a **plex.tv account token** (works with `MyPlexAccount`). If it's a server token, switch the helper to `PlexServer(...).myPlexAccount().users()`.
> - Confirm the box reaches Seerr at `127.0.0.1:<jellyseerr.port>` vs `172.17.0.1:<port>` (docker bridge). Set `SEERR_URL` accordingly.

**Interfaces:**
- `plex_members.py` prints JSON `[{"id":int,"email":str,"username":str}, ...]` (owner + shared users) to stdout; non-zero exit + stderr on failure.
- `membership.ts` produces `isMember(who:{id:number;email:string}): Promise<'plex'|'seerr'|null>` (which source matched, or null). 10-min cache on the Plex member set.

- [ ] **Step 1: Write plex_members.py**
```python
#!/usr/bin/env python3
"""Print QFlix Plex authorized accounts (owner + shared users) as JSON.
Reuses the existing python-plexapi venv. Token from $PLEX_TOKEN (account token)."""
import json, os, sys
from plexapi.myplex import MyPlexAccount
def main():
    tok = os.environ.get("PLEX_TOKEN") or sys.exit("PLEX_TOKEN unset")
    acct = MyPlexAccount(token=tok)
    out = [{"id": acct.id, "email": (acct.email or "").lower(), "username": acct.username}]
    for u in acct.users():
        out.append({"id": u.id, "email": (u.email or "").lower(), "username": u.title})
    json.dump(out, sys.stdout)
if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Failing test for membership.ts** — given a mocked member set `[{id:1,email:'a@x'}]` and a mocked Seerr `/api/v1/user` returning `[{plexId:9,email:'b@x'}]`: `isMember({id:1,email:'a@x'})→'plex'`; `isMember({id:9,email:'b@x'})→'seerr'`; `isMember({id:5,email:'c@x'})→null`.

- [ ] **Step 3: Run — FAIL.**

- [ ] **Step 4: Implement membership.ts** — primary: `execFile` the python helper (`cfg.plexMembersPy` split into python+script), parse JSON, cache 10 min, match by `id` or lowercased `email`. Fallback: `GET ${cfg.seerrUrl}/api/v1/user?take=200` header `X-Api-Key`, match by `plexId`/`email`. Returns `'plex'|'seerr'|null`. Any error in a source = that source misses (fail-closed overall: only grant on an explicit match).

- [ ] **Step 5: Run — PASS. Commit** (record the venv path + token type + seerr URL findings in the message).

### Task 4.4: auth routes + /api/me + greeting render

**Files:**
- Create: `src/routes/api/auth/plex/start/+server.ts`, `.../callback/+server.ts`, `src/routes/api/me/+server.ts`
- Modify: `src/lib/components/Hero.svelte`, `src/routes/+page.svelte`

**Interfaces:**
- `GET /api/auth/plex/start` → 302 to Plex auth URL; stashes `{pinId}` in a short signed cookie; `forwardUrl` built from `x-forwarded-host`/`-proto` (fallback `url.origin`).
- `GET /api/auth/plex/callback` → polls pin, `whoami`, `isMember`; on member sets AUTH + GREET cookies and 302 → `/?support=1`; on non-member 302 → `/?support=denied`.
- `GET /api/me` → `{name}` from GREET cookie or `200 {}`; never errors to client; never triggers login.

- [ ] **Step 1: Implement the three routes** (start/callback/me) using `plex.ts`, `membership.ts`, `session.ts`. Callback polls `pollPin` up to ~25× @ 1 s.

- [ ] **Step 2: Silent greeting in Hero** — `+page.svelte` `onMount`: `fetch('/api/me')` → if `{name}`, set `#greeting` text "Now showing for {name}" (fade-in). Empty/error → render nothing. **Silent check/fail/empty.**

- [ ] **Step 3: Test** — `/api/me` with no cookie → `{}`; with a valid GREET cookie → `{name}`; with tampered cookie → `{}`. (Vitest, calling the handler with a mocked `cookies`.)

- [ ] **Step 4: Commit**
```bash
git commit -am "feat(dash): Plex PIN auth, membership gate, /api/me silent greeting"
```

---

## Phase 5 — Support form + webhook

### Task 5.1: ratelimit.ts + /api/support

**Files:**
- Create: `src/lib/server/ratelimit.ts`, `src/lib/components/SupportModal.svelte`
- Create: `src/routes/api/support/+server.ts`, `src/lib/server/support.test.ts`
- Modify: `src/routes/+page.svelte`

**Interfaces:**
- `allow(key:string, max=3, windowMs=3600_000): boolean` (in-memory sliding window).
- `POST /api/support` body `{message:string}`; requires AUTH cookie; 401 if absent, 429 if rate-limited, 400 if empty/oversize, 204 on success.

- [ ] **Step 1: Failing test** — POST without AUTH → 401; with AUTH + empty message → 400; valid → 204 and the mocked webhook `fetch` is called once with `username:'QFlix'`, `avatar_url:cfg.qAvatar`, and an embed containing the **session** email (not a client-supplied one); 4th call within the window → 429.

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement /api/support** — `verifyAuth(cookies.get(AUTH))`→401; honeypot field non-empty→204 silently; `allow('sup:'+who.e)`→429; validate `message` 1..2000 chars→400; POST to `cfg.discordWebhook`:
```ts
await fetch(cfg.discordWebhook, { method:'POST', headers:{'content-type':'application/json'},
  body: JSON.stringify({ username:'QFlix', avatar_url: cfg.qAvatar, embeds:[{
    title:'Support request', description: message.slice(0,2000),
    fields:[{name:'From',value:`${who.u} (${who.e})`}], timestamp: new Date().toISOString(),
    color: 0xff8c42 }] }) });
```
Return `204`.

- [ ] **Step 4: Run — PASS.**

- [ ] **Step 5: SupportModal UI** — three states: (a) not signed in → "Sign in with Plex" button → `/api/auth/plex/start`; (b) member (AUTH present, detected via `/api/me` presence or a `?support=1` redirect) → textarea + submit → `POST /api/support` → success toast; (c) `?support=denied` → "This is for QFlix members — sign in with the Plex account you watch with." Honeypot input visually hidden. Opened from the Support tile event.

- [ ] **Step 6: Commit**
```bash
git commit -am "feat(dash): Plex-gated Support form -> Discord webhook (Q avatar), rate-limited"
```

---

## Phase 6 — Build & seedbox provisioning (NO cutover yet)

> Goal: the dashboard runs on the box on its own port and is verified via the SSH tunnel, while root STILL serves Homarr. Cutover is Phase 7.

### Task 6.1: Templates + installer

**Files:**
- Create: `scripts/qflix-dash/qflix-dash.service.tmpl`, `scripts/qflix-dash/qflix-dash.nginx.conf.tmpl`
- Create: `scripts/configure/90-qflix-dash-install.sh`

- [ ] **Step 1: systemd unit template** (`@@PORT@@`, `@@NODE@@`, `@@APPDIR@@` substituted by the installer):
```ini
[Unit]
Description=QFlix Dashboard (SvelteKit adapter-node)
After=network-online.target

[Service]
Type=exec
WorkingDirectory=@@APPDIR@@
EnvironmentFile=%h/.config/qflix-dash/qflix-dash.env
ExecStart=@@NODE@@ @@APPDIR@@/build/index.js
Restart=on-failure
RestartSec=3
StandardOutput=append:%h/.apps/qflix-dash/logs/app.log
StandardError=append:%h/.apps/qflix-dash/logs/app.log

[Install]
WantedBy=default.target
```

- [ ] **Step 2: nginx root fragment template** — note this OWNS `location /`; it is staged in Phase 6 but only included at cutover (Phase 7) to avoid a duplicate-`location /` clash with the live default site:
```nginx
# manitoba-qflix-dash-root  (replaces the homarr root redirect at cutover)
location / {
    auth_basic              off;
    proxy_pass              http://127.0.0.1:@@PORT@@;
    proxy_http_version      1.1;
    proxy_set_header        Host                 $host;
    proxy_set_header        X-Forwarded-Host     $http_host;
    proxy_set_header        X-Forwarded-Proto    $scheme;
    proxy_set_header        Upgrade              $http_upgrade;
    proxy_set_header        Connection           "upgrade";
}
```

- [ ] **Step 3: Installer** `scripts/configure/90-qflix-dash-install.sh` (idempotent), runs from workstation over `sshm`/`scpm`:
  1. Ensure nvm + Node 20 on the box: `ssh … 'export NVM_DIR=$HOME/.nvm; [ -s $NVM_DIR/nvm.sh ] || (curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash); . $NVM_DIR/nvm.sh; nvm install 20; nvm alias default 20; node -v'` and capture the absolute node path (`nvm which 20`).
  2. Allocate port: `ssh … 'app-ports free'` → pick one (or reuse Homarr's freed port post-decom) → write `secrets/qflix-dash.port`.
  3. Mint secrets if absent: `qflix-dash.session_secret` (`openssl rand -hex 32`), `qflix-dash.plex_client_id` (`uuidgen`), `qflix-dash.discord_webhook` (operator-supplied).
  4. Build on workstation: `(cd apps/qflix-dash && npm ci && npm run build)`.
  5. Ship: `rsync`/`scp` `build/`, `package.json`, `package-lock.json` → `~/.apps/qflix-dash/`; `ssh … 'cd ~/.apps/qflix-dash && <node/npm> ci --omit=dev'`.
  6. Render env file → `~/.config/qflix-dash/qflix-dash.env` (mode 600) from secrets: `PORT`, `HOST=127.0.0.1`, `PROTOCOL_HEADER=x-forwarded-proto`, `HOST_HEADER=x-forwarded-host`, `XFF_DEPTH=2`, `PLEX_TOKEN`, `PLEX_CLIENT_ID`, `PLEX_MEMBERS_PY="<venv-python> ~/.apps/qflix-dash/plex_members.py"`, `SEERR_URL`, `SEERR_API_KEY`, `DISCORD_WEBHOOK`, `SESSION_SECRET`, `Q_AVATAR_URL`, `FAQ_PROBE_URL`, `MANITOBA_MAINT_BIN=<abs path>`.
  7. Ship `scripts/qflix-dash/plex_members.py` → `~/.apps/qflix-dash/plex_members.py`.
  8. Render + install the unit (substitute `@@NODE@@/@@PORT@@/@@APPDIR@@`), `mkdir -p ~/.apps/qflix-dash/logs`, `systemctl --user daemon-reload && systemctl --user enable --now qflix-dash.service`.
  9. Seed Kuma monitor "QFlix Dashboard" (via the repo's `bootstrap-kuma-monitors.py` after the manifest entry lands — Task 6.2).

- [ ] **Step 4: Run installer; verify over tunnel**
```bash
ssh -L 5199:127.0.0.1:<qflix-dash.port> quadstronaut@<ssh-fqdn> -N &
curl -s localhost:5199/healthz   # -> ok
curl -s localhost:5199/api/status | jq .
```
Expected: `healthz=ok`, status JSON with real pucks. Root still serves Homarr (unchanged).

- [ ] **Step 5: Commit**
```bash
git add scripts/qflix-dash scripts/configure/90-qflix-dash-install.sh
git commit -m "feat(dash): seedbox installer (nvm Node 20, systemd unit, env, plex helper) + nginx root template"
```

### Task 6.2: Manifest entry + Kuma + pytest

**Files:**
- Modify: `manifest/apps.yaml` (add `qflix-dash`)
- Create: `tests/unit/test_dashboard_manifest.py`

- [ ] **Step 1: Failing pytest**
```python
import yaml, pathlib
def test_qflix_dash_in_manifest():
    apps = yaml.safe_load(pathlib.Path("manifest/apps.yaml").read_text())["apps"]
    assert "qflix-dash" in apps
    d = apps["qflix-dash"]
    assert d["class"] == "systemd"
    assert d["kuma_monitor"] == "QFlix Dashboard"
    assert d["health"]["kind"] == "http_root"
```

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Add the manifest block** (per spec §11), `port_secret: qflix-dash.port`, `path_template:"/healthz"`. Run `bootstrap-kuma-monitors.py` to create the monitor.

- [ ] **Step 4: Run — PASS. Commit.**
```bash
git commit -am "feat(dash): manifest entry + Kuma monitor for qflix-dash"
```

---

## Phase 7 — Cutover & Homarr decommission

> Reversible; follows the Maintainerr/Jellyfin decom pattern. Do during a quiet window; keep the tunnel-verified service from Phase 6 as the rollback.

### Task 7.1: Switch root to the dashboard

**Files:**
- Modify (on box): `~/.apps/nginx/sites-available/default` — remove the `# manitoba-homarr-root-redirect` block and repoint `location /` to the dashboard (install the staged fragment / edit in place).
- Create: `scripts/configure/91-nginx-root-to-dash.sh` (idempotent; mirrors `34-nginx-root-to-homarr.sh`, backs up the conf first).

- [ ] **Step 1:** Implement `91-nginx-root-to-dash.sh`: back up `default`, delete the homarr `location = /` block, replace the `location / {…}` body with the proxy fragment, `app-nginx restart`, then assert `curl -sk https://<fqdn>/ -o /dev/null -w '%{http_code}'` is `200` and the body contains `data-qflix-dash`.
- [ ] **Step 2:** Run it. Verify the live root on a phone + via `qflix.quadstronix.dev` (Porkbun forward already lands here — no DNS change).
- [ ] **Step 3:** Commit `scripts/configure/91-nginx-root-to-dash.sh`.

### Task 7.2: Repoint mobile-ux canary

**Files:**
- Modify: `scripts/canaries/mobile-ux.sh`

- [ ] **Step 1:** Change the assertions from "root 302 + homarr board 200" to "root **200** + body contains `data-qflix-dash` + HTML < 512 KB". Drop the `homarr-upstream` host computation.
- [ ] **Step 2:** Run the canary locally (`scripts/canaries/mobile-ux.sh`) → PASS.
- [ ] **Step 3:** Commit.

### Task 7.3: Remove Homarr

**Files:**
- Modify: `manifest/apps.yaml` (remove `homarr`), `inventory.md`, `README.md`, `docs/transition-log.md`
- Modify: `tests/unit/test_dashboard_manifest.py` (assert `homarr` absent)
- Retire: `scripts/configure/34-nginx-root-to-homarr.sh`, `35-homarr-seed-boards.py`, `46-homarr-add-comms.py`, `61-homarr-qflix-theme.py` (delete; history retains them)

- [ ] **Step 1:** `ssh … 'app-homarr uninstall'` (or `stop` if keeping the optional old-URL shim — default uninstall). Remove the two Homarr Kuma monitors via `bootstrap-kuma-monitors.py`.
- [ ] **Step 2:** Update manifest (drop `homarr`), `inventory.md` (homarr row, counts), `README.md` ("reachable without tunnel" table: Homarr → QFlix Dashboard; app counts), `docs/transition-log.md` (reversible entry).
- [ ] **Step 3:** Extend `tests/unit/test_dashboard_manifest.py` with `assert "homarr" not in apps`; run pytest → PASS.
- [ ] **Step 4:** Update `scripts/smoke-test.sh` root assertion (200 + marker, not 302). Run smoke → green.
- [ ] **Step 5:** Commit.
```bash
git commit -am "feat(dash)!: cut over root to QFlix Dashboard; decommission Homarr"
git push origin master
```

---

## Self-Review (spec coverage)

| Spec section | Task(s) |
|---|---|
| §3 architecture / origin / runtime Node | 0.1, 6.1 |
| §4 tiles (incl. GitHub reachability dot) | 2.1, 3.1 |
| §5 routes (status, me, support, auth, healthz) | 3.1, 4.4, 5.1 |
| §6 status from maint json | 3.1 |
| §7 Plex PIN + membership (primary+fallback) | 4.2, 4.3 |
| §7a greeting cookie | 4.1, 4.4 |
| §8 support → Discord (Q avatar, rate-limit, server identity) | 5.1 |
| §9 secrets | 6.1 |
| §10 URL/DNS (no change; Porkbun already forwards) | 7.1 (verify) |
| §11 ops (manifest/systemd/nginx/kuma/canary) | 6.1, 6.2, 7.1, 7.2 |
| §12 decom | 7.3 |
| §13 visual (palette/cinema/mobile/greeting) | 1.1, 2.1, 2.2, 4.4 |
| §14 error handling (fail-closed, grey pucks) | 3.1, 4.3, 5.1 |
| §15 testing (vitest/playwright/pytest/smoke) | every phase + 6.2, 7.2, 7.3 |
| §16 mint plex_client_id | 6.1 |
| §17 out of scope (audiobooks/comics/admin) | n/a (excluded) |

**Open execution-time verifications** (flagged inline, not guesses): python-plexapi venv path; `plex.token` type; Seerr reachable address; `manitoba-maint` absolute path; nvm node absolute path; chosen free port.
