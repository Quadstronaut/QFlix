# Audit residual-risk register

> **Read this first.** The Convergent Audit Regime does **not** deliver "no new
> findings ever again". That outcome is not achievable and any claim to the
> contrary is wrong. What it delivers is **run-to-run determinism**: the same
> commit produces the same `report_digest`, so a change is always attributable
> to an input change or a newly enrolled class. Everything below is what is
> **still** capable of producing genuinely new findings, forever.

Every row here is machine-checked. `manifest/audit-scope.yaml:residuals` and
every `status: residual` class in `manifest/defect-classes.yaml` must have a row
whose first cell is its id, and every row's `last_reviewed` must be inside its
cadence. A missing row or a lapsed review is a **REGIME INTEGRITY** failure
(`qflix-audit` exit **2**), not a finding — see `docs/audit-regime.md`.

---

## The six permanent residuals

| id | residual | why it cannot be closed | owner | cadence (days) | last reviewed |
|---|---|---|---|---|---|
| R1 | Upstream / platform change — Tdarr, the *arr suite, Plex, SABnzbd, the Ultra.cc platform. | Unbounded and out of our control. "Never again" would be a claim about third parties' future behaviour. In-tree evidence: the entire tdarr-WASM-OOM saga, and the UCC kernel re-IP that broke Tautulli. | operator | 90 | 2026-07-29 |
| R2 | Live box drift between audit runs (classes L-01..L-06). | Deployed state mutates out-of-band; offline detectors cannot see it. Bounded only by how often `qflix-audit-live.py` runs. | operator | 30 | 2026-07-29 |
| R3 | Semantic defects with no syntactic signature — a policy contradiction between two config surfaces that share no token, a wrong threshold, wrong business logic. | Undecidable in general (Rice). The regime trades semantic completeness for syntactic exhaustiveness, deliberately. **This is what an LLM audit is genuinely good at**; the regime does not replace it, it shrinks the LLM's job from "search everything" to "search the residual". | operator | 90 | 2026-07-29 |
| R4 | S2 untracked file bodies — `scripts/local-llm/qflix-rea.ps1`, `scripts/manitoba-tunnel.ps1`. | Only their extracted **policy** is guarded (`manifest/rea-noise-classes.yaml`, C-07). A body edit that does not touch policy is invisible to CI, and that part cannot be closed while the files stay out of a public repo. **What changed 2026-08-03: it is no longer also _unrecoverable_.** `scripts/local-llm/backup-untracked.ps1` reads the S2 member list, copies each file hourly into the same `B:\BAKS\Documents` mirror `Backup-Documents` uses, keeps a content-addressed version history *outside* the `/MIR` purge scope, SHA256-verifies all three, and exits non-zero + pages Discord on any divergence. Measured the day it landed: the mirror was **22 hours and two edits behind** `qflix-rea.ps1` while every surface reported success. | operator | 90 | 2026-08-03 |
| R5 | **CI itself is unwatched.** If GitHub Actions silently stops running, everything above stays green and the digest stops changing for the most boring possible reason. | The mitigation (assert the last successful `master` run is < 7 days old via the GitHub API) needs network and a token; every detector in this regime is offline by construction. **Not implemented.** This is the last turtle and it is named so it is never a surprise. | operator | 30 | 2026-07-29 |
| R6 | A defect class nobody has thought of yet. | The regime does not prevent this. It guarantees that once a class is thought of **once**, it is enumerated **exhaustively** and never re-sampled. | operator | 180 | 2026-07-29 |

## Surface residuals

| id | residual | why it cannot be closed | owner | cadence (days) | last reviewed |
|---|---|---|---|---|---|
| R-WORKSTATION-SCHEDULED-TASKS | The Windows scheduled tasks behind the workstation half: `\QFlix-LLM\QFlix Random Error Audit` and `\Archangel\Backups\QFlix-Untracked-Backup`. | Existence, schedule and enabled-state are live workstation state. A disabled task looks exactly like a quiet week. (The path was written `\Archangel\QFlix-LLM\` here until 2026-08-03; `Get-ScheduledTask` says it is at the root, `\QFlix-LLM\`.) Partial cover: REA is watched from the **box** by `scripts/canaries/rea-liveness.sh`; the backup task has no external watcher, but `-VerifyOnly` reds on a receipt older than 48h so anything that asks can see it. | operator | 90 | 2026-08-03 |
| R-WORKSTATION-OLLAMA-SERVICE | The local ollama service and its model set. | Live workstation state. The model store was relocated to `B:\AIModels` on 2026-07-24 after a deletion killed `serve` on every boot. | operator | 90 | 2026-07-29 |
| R-DASH-CI | `apps/qflix-dash` has a `svelte-check` gate that this repo's CI does not run. | 16 errors -> 0 was a manual milestone (`8a694da`), not an enforced one. Enrolling it means a Node toolchain in CI. | operator | 90 | 2026-07-29 |

## Live defect classes (L-01..L-06)

These are **named** classes with **no offline detector**, by construction. They
belong to `scripts/maint/qflix-audit-live.py` (its own module, its own timer,
its own Kuma monitor — per the compartmentalisation design law) and they are the
classes that will keep producing new findings forever.

| id | live class | why it cannot be decided offline |
|---|---|---|
| L-01 | Repo unit files vs **deployed** unit files. | Drop-ins that exist only on the box are **by design** out of repo — the reaper is armed with `--max-pct 100` and the torrent-janitor with `--execute` via on-box drop-ins precisely so a repo clone cannot arm a mass delete. Repo text cannot tell you what systemd will actually run. |
| L-02 | Deployed timers/services absent from the repo entirely. | A unit that exists only on the box is invisible to a glob over the repo. Only `systemctl --user list-unit-files` knows. |
| L-03 | Live Kuma monitor set vs manifest. | Needs `kuma.db`, which is box-local. `/metrics` is not a substitute: it omits daily self-pushers for ~22h/day between beats, so a `/metrics`-based check false-flags them as drift. |
| L-04 | Every declared secret key exists on the box with the correct mode. | Secrets are never in git (public repo). Existence and file mode are properties of the box's filesystem only. |
| L-05 | Every self-pusher has a **persisted** push token. | The born-mute class: a push token is a live Kuma artifact in `~/secrets/kuma-push-tokens.json`, and a missing one makes the job exit 0 silently. Nothing in the repo can observe it. |
| L-06 | Quota / thread ceiling / disk. | Inherently live numbers on a shared seedbox whose limits Ultra.cc can change out-of-band (128 cores visible, `ulimit -u` 2000, `ulimit -v` 10GB). |

## Known, unclosed dead-man gaps

Adjudicated in `manifest/jobs.yaml` with `open_gap: true`. Each is reported as an
**advisory finding on every run** — `open_gap` moves a gap from *unknown* to
*known, dated and owned*; it does not make it disappear.

| timer | gap |
|---|---|
| `manitoba-maint-arr-audit` | Weekly *arr audit notifies on findings but pushes no heartbeat, so a permanently-failing unit is indistinguishable from a clean week. |
| `manitoba-maint-window` | Monday window orchestrator. If both it and its watchdog fail to start, nothing pages. |
| `manitoba-maint-window-watchdog` | The watcher with no watcher. An instance of the turtle problem; the chain currently ends here. |
| `manitoba-maint-flaresolverr-canary` | **Found by the detector, not by a hand-list.** Not in `manifest/apps.yaml:canaries`, so it has no monitor and no dead-man; if its timer dies the only signal is the absence of Discord messages nobody expected. |

## What changing this file costs

Nothing may be removed from here without either (a) landing the detector that
makes it enumerable, or (b) writing down why the risk no longer exists. A row
deleted without one of those breaks the ledger-pair check and exits 2.
