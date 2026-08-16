# Operator-deferred manual steps

**Last reconciled against the live box: 2026-07-30.**

> **This file is a liability, not a backlog.** QFlix is live and is several
> people's primary source of entertainment. Anything sitting here is a piece of
> the shipped product that is not finished, and every entry must therefore be
> either **done**, **closed with a decision**, or **impossible without the
> operator** — with the reason stated. "Later session" is not a state.
>
> `tests/unit/test_operator_deferred.py` enforces that: every item under
> "Open" must carry an owner and a dated reason, and the file must not silently
> accumulate. Two of the entries that lived here for months were **already done**
> and nobody had noticed — the registry itself was the silent failure.

---

## Open — genuinely needs the operator

### Notifiarr CLIENT daemon — a redundant Plex push path (DECISION, not work)

Plex push events (play / pause / scrobble) can reach Notifiarr either through
its client daemon on the box plus a Plex webhook, or through Tautulli's Webhook
agent. **Tautulli's path has been wired and working since 2026-05-09**, so the
client daemon is a *duplicate* channel, not a missing one.

Nothing is broken and nothing is missing. This stays open only as a standing
choice: run a second, redundant path or not. Default is **no** — it is one more
daemon competing for the slot's thread budget for zero new signal.

**Owner:** operator · **Reason dated:** 2026-07-30 · **Blocks nothing.**

---

## Closed 2026-07-30

### Phase 16 uninstalls — ALREADY DONE, the entry was stale

This file claimed six apps were "ready to execute" for uninstall. Measured on
the box 2026-07-30: `jackett`, `medusa`, `pyload`, `deluge`, `transmission`,
`mariadb` and `ombi` all have **no `~/.apps` directory, no systemd units, no
port secret and no nginx fragment**. They were uninstalled long ago and the
registry was never updated, so the product read as unfinished when it was not.

### Tdarr worker cap — CLOSED AT 2/2, with data

`todo-after-claude.md` asked whether to raise the 2/2 CPU worker cap given 128
cores. **Decision: keep 2/2.** Measured live 2026-07-30 while clients were
streaming:

- **2 Plex transcoders active** — real playback in progress
- **946 / 2000 threads** used (47% of the slot's `RLIMIT_NPROC`)
- load average **26** on a **shared** box — those cores are not ours to spend
- thread exhaustion is a *proven* crash class here: it crash-looped VictoriaLogs
  (`pthread_create EAGAIN`), which is why `GOMAXPROCS` is capped in units and why
  the `thread-ceiling` canary exists

Tdarr is a background optimiser; playback is the product. Raising concurrency
trades guaranteed live-stream quality for faster background transcodes. If it is
ever raised, do it one worker at a time with the `thread-ceiling` canary watched,
never during peak viewing.

### Bounced system mail — spool cleared

A single 2,192-byte `MAILER-DAEMON` bounce from 2026-05-25 sat in
`/var/spool/mail/quadstronaut`. Root cause is host-side and **not ours to fix**:
the shared box aliases `root: usbx`, and `support@seedbox-provider.example.com` is
unrouteable. Confirmed **not customer-facing** — member mail goes out via the
Listmonk API and never touches the local MTA, and a repo-wide grep finds no
`sendmail` / `mail` / `smtplib` use anywhere in QFlix. Spool cleared to zero.
Non-recurring: one message in seven weeks. Nothing further to do without a
support ticket, which is not worth filing.

### quality-fallback Kuma monitor — ALREADY DONE, the entry was stale

Claimed the "Qflix Quality Fallback" push monitor still needed a one-off
`bootstrap-kuma-monitors.py` run. It exists, is UP, has both notification
channels, and beats within its cadence (verified 2026-07-30).

### UI smoke checklist — automated, no longer a manual walkthrough

Every item is now asserted by machine on every smoke run:

| Was a manual click | Now |
|---|---|
| Dashboard renders at root, tiles clickable | `mobile-ux` canary + `landing-page` smoke gate + `dash-asset-integrity` (asserts it can actually *hydrate*, which a human eyeball could not) |
| Seerr request flow end-to-end | `movie` and `anime` canaries post a real request and wait for Plex availability |
| Plex streaming a recent title | `plex-transcoder` canary probes the transcode endpoints |
| Calibre-Web admin login | ~~`calibre-web` app monitor (authenticated probe)~~ — moot: Calibre-Web purged 2026-08-16 with the rest of the books stack |
| Tautulli → Discord on Watched | `tautulli-plex-link` canary |

### Homarr `mediaReleases` widget — moot

Homarr was fully decommissioned 2026-07-13; the app no longer exists.

---

## Captured already (no operator action)

All credentials are in `secrets/` and verified by the smoke test: Plex
token/host/port, Seerr key, all five *arr keys, qBit credentials, Bazarr /
Bazarr 2 / Tautulli keys, htpasswd password, Listmonk API user + token,
Discord webhook + operator id, seedbox public FQDN and SSH host. (The Komga /
Kavita / Audiobookshelf / Calibre-Web keys listed here until 2026-08-16 were
purged with the books stack.)
