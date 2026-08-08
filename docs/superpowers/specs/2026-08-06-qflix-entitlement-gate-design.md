# QFlix Entitlement Gate — design

**Date:** 2026-08-06
**Branch:** `feat/entitlement-gate`
**Status:** approved (operator, 2026-08-06), not yet built
**Supersedes:** nothing. **Conflicts with:** master's planned payment-rail gate — see §10.

---

## 1. What this is

A cron-driven **provisioning AND protection** system that keeps three facts in
agreement for every QFlix member:

1. does the person hold an accepted Plex share on this server,
2. does `entitlements.starhold.app` say they are a current supporter,
3. what Plex libraries and Seerr permissions do they therefore get.

The operator invites people **by hand** — this system never sends an invite and
never decides who is worth inviting. It observes acceptance, provisions the
downstream account in a disabled state, and thereafter grants or withdraws
access to match the entitlement API.

It is a **human-in-the-loop** system on both ends: a human chooses who is
invited, and a human (via Patreon, upstream) causes entitlement to become true.
The automation only propagates decisions that a person already made.

### The AND gate

Access requires **both** of:

```
invited by the operator   (has an accepted Plex share, and a members.yaml household)
        AND
currently entitled        (entitlements.starhold.app -> entitled: true)
```

Neither alone is sufficient. This is why the system gates on the bare
`entitled` boolean and **not** on a pledge amount or a named reward: the roster
is the allowlist, so a $50 supporter the operator never invited still has no
Plex share and therefore no access. Encoding a price threshold into the
entitlement server would add a second, weaker allowlist that duplicates the
roster and can silently disagree with it.

**Decision (operator, 2026-08-06):** no `qflix` reward, no change to Starhold's
`config/patreon_rewards.yaml`. Tier policy stays a human judgement the operator
applies when granting entitlement upstream.

---

## 2. The three-stage member lifecycle

```
          form on qflix.starhold.dev
                    |
                    v          (operator reads Discord, invites by hand)
          Plex invite sent  -- NOT automated, deliberately
                    |
                    v
   [1] ACCEPTED, NOT ENTITLED
       plex  : QFlix - Welcome only   (the community-invite video)
       seerr : account exists, permissions = 0
       -> the person can see the pitch and nothing else
                    |
                    |  operator grants entitlement upstream
                    v
   [2] ENTITLED
       plex  : all five sections
       seerr : full member permissions
                    |
                    |  entitlement withdrawn (lapse or operator revoke)
                    v
   [3] REVOKED  (after grace, see §5)
       plex  : QFlix - Welcome only    <- share object KEPT
       seerr : permissions = 0         <- account KEPT
       -> reversible with no re-invite email, no re-accept
```

Stage 3 is deliberately identical to stage 1. A revoked member is returned to
the pitch, not evicted from the building. Deleting the Plex share would force a
fresh invite the person has to accept out of their email — that is an eviction,
and the roster's own comments already argue against it.

---

## 3. Components

Each is separately testable and separately swappable, per the operator's
compartmentalise-for-migration law.

| File | Responsibility | Talks to |
|---|---|---|
| `scripts/maint/lib/entitlement.py` | Ask the entitlement API about one email. Nothing else. | `entitlements.starhold.app` |
| `scripts/maint/lib/plexshare.py` | Read and write which sections a friend is shared. | `plex.tv` + local PMS |
| `scripts/maint/lib/seerrusers.py` | Read/create Seerr users, read/write permissions. | Seerr on `:42011` |
| `scripts/maint/lib/access_state.py` | Durable clocks, saved prior permissions, cohort seeding. | `~/.opt/maint/entitlement/` |
| `scripts/maint/qflix-entitlement.py` | Orchestrator + CLI. **The only file that mutates anything.** | all of the above |
| `scripts/configure/NN-plex-welcome-library.py` | Idempotently create the `QFlix - Welcome` section. | local PMS |

`lib/members.py` is **reused unchanged** — it already models exactly the
identity join this needs (`billing.holder` = the entitlement email,
`accounts:` = every Plex email in the household) and already refuses the
contradictions that matter.

### Why the identity join needs the roster

The entitlement API joins on the **Patreon** email. A member's **Plex** email
may differ, and forcing them to be equal is how a paying member gets cut off.
So:

```yaml
- id: newfriend
  exempt: false
  billing:
    holder: pays@example.com        # asked at entitlements.starhold.app
    rail: patreon
  accounts:
    - watches@example.net           # the Plex share the answer is applied to
    - kid@example.net               # dependents inherit the holder's state
```

One lookup per **household** (keyed on `billing.holder`), applied to every
account in it. Dependents are never looked up or billed independently.

---

## 4. State machine

Evaluated per household, every run.

```
EXEMPT ..................... never touched, ever. Terminal. No lookup is even made.

for each accounts[] email with an ACCEPTED Plex share:

  no Seerr user for it?
        -> CREATE Seerr user, permissions = 0
           (provisioning NEVER reduces Plex access. See 5.3)

  lookup(billing.holder):

    200 + entitled:true   -> ENTITLED
                             plex  sections := all five
                             seerr perms    := saved prior, else member default
                             all clocks cleared, lapse history discarded

    200 + entitled:false  -> deadline := MAX(applicable clocks, 5.1)
                             now <  deadline -> PENDING   (report only, no mutation)
                             now >= deadline -> EXPIRED
                                                plex  sections := [Welcome]
                                                seerr perms    := 0
                                                                  (prior saved first)

    anything else         -> NO-OP. Not a state. See 5.3.
```

### Unnamed shares

An accepted Plex share with no matching household is a **new arrival**, not an
error — a freshly accepted invite is by definition not yet in the roster.

```
UNKNOWN SHARE
  seerr   : create user if absent, permissions = 0
  plex    : if sections <= [Welcome]   -> leave alone
            if sections >  [Welcome]   -> REPORT LOUDLY, mutate nothing
  kuma    : WARN (not red)
  discord : "unnamed share <email> holds N libraries - add to roster"
```

**The system never shrinks an account it cannot name.** Revoking access because
a record is missing is how one roster typo evicts a real person. The
out-of-band-share hole is closed by paging a human, not by cutting.

This deliberately differs from `lib/members.py:reconcile_shares()`, which treats
an unlisted live share as fatal. That law is correct for a rail-reconciliation
gate that only ever subtracts; it is wrong here, because this system also
*provisions*, and a refuse-to-run on unlisted shares would jam on every new
signup — including the half that is supposed to welcome them.

---

## 5. Clocks

### 5.1 Three clocks, latest wins

| Clock | Applies to | Deadline |
|---|---|---|
| **Launch amnesty** | households whose share was already accepted at first run | `2026-09-01T00:00:00Z` |
| **New-arrival grace** | any share first seen accepted after first run | `first_seen_accepted + 30d` |
| **Lapse grace** | a household that *was* entitled and went false | `went_false + 7d` |

`deadline = max(applicable)`. Nobody is ever cut off with less than a fair
window: a person invited on 28 August gets until 27 September, not three days.

The launch amnesty exists because the campaign currently has **0 members**, so
arming without it would shrink all 13 existing households to Welcome on the
first run. It is a **one-time** migration allowance and is expressed as an
explicit date in the roster so the deadline is diffable and reviewable in the
same file that decides access — not implied by whenever a service first ran.

```yaml
defaults:
  grace_days: 7                  # lapse grace (was entitled, went false)
  new_arrival_days: 30           # first accepted -> must become entitled
  amnesty_until: 2026-09-01      # ONE-TIME launch amnesty. Delete after it passes.
```

`grace_days` moves from 3 to **7** (operator, 2026-08-06: "3 days is not
enough, let's make it a full week"). A declined card should not black out
someone's Friday night before they have seen the email about it.

### 5.2 Cohort seeding

On its **first run** — including in report-only mode — the system records every
currently-accepted share with `first_seen_accepted` and a `cohort: launch` tag.
Seeding is a write to state and happens regardless of arming, because a system
that only learns who existed once it is allowed to act cannot distinguish
"pre-existing" from "appeared while I was disarmed".

### 5.3 The asymmetric failure law

**This is the most important rule in the system.**

The integration guide says *fail closed, never grant on error*. For a system
that also **revokes**, that law inverted is a mass-eviction button. So the two
directions have different evidentiary standards:

| Direction | Requires | On error / timeout / 429 / 5xx / `stale:true` |
|---|---|---|
| **Grant** — expand libraries, enable Seerr | HTTP 200 **and** `entitled:true` | do nothing. Never grants. |
| **Revoke** — shrink libraries, zero Seerr | HTTP 200 **and** `entitled:false` | do nothing. **The clock does not advance.** |

An entitlement-API outage therefore **freezes** the system rather than draining
it. A `stale:true` response with `entitled:true` still grants (granting is
non-destructive and a stale yes was a real yes); `stale:true` with
`entitled:false` is treated as *no answer received*.

This is the same law `patreon-report.py` already encodes for its own auth
failures: empty-because-clean must never look like empty-because-broken. Here
the stakes are higher, because the empty answer does not merely print a wrong
report — it removes people's access.

**Testable consequence:** no fault injected into the API client — connection
refused, DNS failure, 401, 403, 429, 500, malformed JSON, truncated body,
timeout, or a body that omits `entitled` entirely — may produce a single
mutation. This is an enforced unit-test invariant, not a code comment.

---

## 6. Safety interlocks

All four must be satisfied before any mutation:

1. **`members.yaml: armed: true`** — currently `false`. Roster-level consent.
2. **`--execute`** — absent by default. The script ships inert and is armed on
   the box by a systemd drop-in, exactly as `qflix-reaper` and
   `qflix-torrent-janitor` are. The repo copy is never the armed copy.
3. **`suppression.in_pause_window` is false** — the Monday 11:00–15:00 UTC
   maintenance window suppresses all mutation. Reporting continues.
4. **Per-run mutation cap** (`--max-mutations`, default 10) — exceeding it
   **defers the remainder to the next run** rather than aborting the run, the
   same defer-oldest-N posture the reaper adopted after its abort behaviour
   proved to be a hard stop that hid work.

Additionally:

- an **audit manifest** is written *before* any mutation, naming every account
  and the exact section-set and permission transition intended;
- **prior Seerr permissions are persisted before being zeroed**, so restore is
  an exact replay rather than a guess at what "member default" was;
- a **run lock** prevents overlapping runs.

---

## 7. Durable state

`~/.opt/maint/entitlement/state.json` — the only thing that survives a run.

```json
{
  "schema": 1,
  "first_run_at": "2026-08-06T21:00:00Z",
  "accounts": {
    "<plex email>": {
      "first_seen_accepted": "2026-08-06T21:00:00Z",
      "cohort": "launch",
      "last_entitled_at": null,
      "went_false_at": null,
      "seerr_user_id": 17,
      "seerr_perms_prior": 1155539104,
      "last_action": null
    }
  }
}
```

Written atomically (tmp + `os.replace`). A half-written state file would lose
the record of what a paused account looked like before it was paused, which is
precisely the data that makes a restore exact.

Durable run logs at `~/.opt/maint/entitlement/entitlement-<date>.log`. Trust
the durable log over journald — a lesson already paid for twice in this repo.

---

## 8. Observability

**Kuma:** new push monitor `QFlix Entitlement Gate`, registered in
`lib/kuma.py:STANDALONE_SELF_PUSH_MONITORS` (the single source the audit and the
bootstrap both read) and in `manifest/jobs.yaml` against its timer.

A newly created Kuma monitor is born **both mute and tokenless** — the push
succeeds with exit 0 and the monitor sits DOWN forever. Bootstrap therefore
verifies the notification channel binding *and* the push token by read-back, and
**fails loudly** if either is absent.

**Discord** (`secrets/discord-webhook.url`):

| When | Message |
|---|---|
| weekly, T-30 … T-8 | "N households un-entitled, D days remain: …" |
| daily, T-7 … T-1 | "N households shrink in D days: …" |
| per mutation | "SHRUNK `<household>` → Welcome; Seerr perms 0" / "RESTORED …" |
| immediate | unnamed share holding more than Welcome |
| immediate | refuse-to-run, with the specific blocker named |

**Cadence:** every 15 minutes. ~30 lookups per run against a 120/min/key limit —
about 4% of budget. Fast enough that a new supporter gets access within a
quarter hour of the entitlement API seeing them.

---

## 9. Exit codes

Distinct on purpose, so a cron wrapper cannot conflate them.

| Code | Meaning |
|---|---|
| 0 | OK — ran to completion (including "nothing to do") |
| 1 | Partial — a per-account step failed; others succeeded |
| 3 | Entitlement API unreachable or unauthorised (**never** an empty result) |
| 4 | Plex or Seerr unreachable |
| 5 | Config/roster invalid — refused to run |

Code 3 is its own value for the same reason `patreon-report.py` has
`AUTH_FAILED`: "the API said nobody is entitled" and "I could not ask" must
never share an exit status.

---

## 10. Known conflict with master

Master is growing a payment-rail gate (`qflix-gate.py`) that reads the same
`members.yaml` and shrinks the same Plex shares from the rail-reconciliation
side. **Two writers on one Plex share is a real conflict**, and it is the single
most likely thing to cause an incident at merge.

**Proposal:** `qflix-entitlement.py` is the **sole writer** of Plex sections and
Seerr permissions. Master's rail gate stays report-only, or is scoped to
non-`patreon` rails and defers on any household this system governs. Resolved at
merge, deliberately not resolved unilaterally from this branch.

---

## 11. Test strategy

**Unit (no network).** State machine transitions; clock-max resolution across
all three clocks; cohort seeding; roster join; atomic state writes.

**Fault injection (no network).** The §5.3 invariant, mechanically: every
failure mode of the API client, asserted to produce zero mutations. This suite
is the acceptance gate for the whole design.

**Live end-to-end** on the operator's designated crash-test account. Its address
is not recorded here — the repo is public and member addresses live in
gitignored `secrets/` (`secrets/e2e-subject`, or `$QFLIX_E2E_SUBJECT`). The
harness refuses to run without one rather than defaulting to a placeholder that
would match nobody and print a wall of passes having tested no write path.

```
provision -> perms 0 -> entitle -> expand to 5 -> revoke -> grace -> shrink -> restore
```

verified against the real Plex share API and the real Seerr instance, then
reverted to its starting state. The account's before-state is captured and
diffed at the end; a non-empty diff fails the test.

**Adversarial review.** council-v2 on the destructive paths specifically, with
at least one reviewer lensed on "can this shrink someone it should not".

---

## 12. Out of scope, deliberately

- **Sending Plex invites.** Manual, permanently. The operator chooses who joins.
- **Reading Discord channel 1531809232259645480.** The form → Discord path is how
  the *operator* learns to invite someone. The automation derives every state it
  needs from Plex itself, so no bot token and no cross-fleet dependency is
  introduced.
- **Listmonk.** Untouched on revoke. The nightly `listmonk-sync.py` already
  reconciles subscribers from Plex friends; a revoked member keeps their share
  object and therefore keeps their subscription, which is correct — they should
  still hear about the thing they might come back to.
- **Surfacing viewing activity.** Nothing here reads or reports what anyone
  watched. Content presence yes, consumption never.

---

## 13. What live verification changed (appendix, 2026-08-06)

The design above was approved before the live APIs were read. Five things in it
turned out to be wrong or incomplete. Recorded here rather than silently edited
into the body, because the corrections are more instructive than the original.

**The arrival clock has a real anchor.** §7 assumed acceptance had to be
inferred from first observation. Plex's `shared_servers` reports a genuine
`acceptedAt` unix timestamp per share, so `seed()` anchors to when the person
actually accepted. Without it, a member who accepted in February and one who
accepted this morning would both anchor at "now" on the gate's first run, and
the second would silently inherit thirty days they had not earned.

**`library_section_ids` takes `Section@id`, not `Section@key`.** They are
different numbers for the same library (Movies is `id=132920523, key=4`).
Passing keys shares nothing at all, silently — plex.tv looks the value up in a
table where it does not appear.

**Writing an explicit section list destroys `allLibraries`.** Every existing
share carries `allLibraries="1"`, meaning "everything, including future
libraries". There is no documented way to set it back through this endpoint. The
mitigation is that "entitled" is recomputed from the live catalogue every run,
so a library created at 3pm reaches entitled members by 3:15. If that
recomputation is ever replaced with a hardcoded list, this paragraph becomes the
bug report.

**Seerr had an open self-provisioning race.** `newPlexLogin: true` plus
`defaultPermissions: 1153433760` meant any Plex friend who signed in before the
15-minute cron fired was auto-created with **full member permissions and no
entitlement**. The window was up to fifteen minutes wide and the person is by
construction someone who just got an invite and is curious. Polling faster does
not fix a race, so the fix is structural: `defaultPermissions` is now `0`, every
auto-created account is born disabled — which is exactly stage 1 — and the gate
grants permissions rather than racing to remove them. `check_default_permissions()`
reports the drift every run.

**Exempt was not terminal in the code.** §4 says exempt households are "never
touched, ever", but provisioning was written as independent of entitlement and
the exempt check happened after it. The first live run planned to create a Seerr
account for the operator's own second Plex account, which deliberately has none.
Exempt households are hand-managed by definition; exempt now means no lookup, no
clock, no provisioning, no mutation.

### Acceptance evidence

| Gate | Result |
|---|---|
| Fault injection (§5.3 invariant) | 18 faults, none produces `revokes` |
| Unit suite | 97 new tests, full repo suite green |
| Live report-only run | 14 shares, 4 exempt, 10 pending, **0 mutations** |
| Live end-to-end on the crash-test account | 13/13, restored to exact before-state |
| Outage behaviour, real refused socket | 0 mutations, exit 3 |
