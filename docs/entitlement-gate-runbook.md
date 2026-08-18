# QFlix Entitlement Gate — operator runbook

The gate reconciles Plex library shares and Seerr permissions against
`entitlements.starhold.app` every 15 minutes. It provisions new members and
protects against lapsed ones. It never sends an invite — you do that by hand.

> **Current state: LIVE but INERT.** The timer runs and reports; it mutates
> nothing. Two switches are still off. See *Arming* below.

---

## The one-paragraph model

Access is an **AND**: a person must be **invited by you** (an accepted Plex
share *and* a `members.yaml` household) **and** **currently entitled**. Neither
alone is enough. That is why the gate reads the bare `entitled` boolean rather
than any pledge amount — the roster is already the allowlist, so a supporter you
never invited has no Plex share and therefore no access.

---

## Onboarding a new friend

| # | Who | What |
|---|-----|------|
| 1 | Them | Fills the form at `qflix.starhold.dev` → their email lands in Discord `1531809232259645480` |
| 2 | **You** | Invite that email in Plex, sharing **`QFlix - Welcome` only** |
| 3 | Gate | Sees the share flip to accepted → creates a Seerr account with **permissions 0** |
| 4 | **You** | Add them to `secrets/members.yaml` (see below). Until you do, the gate reports them as an *unnamed share* and never touches their Plex access |
| 5 | Them | Watches the welcome video, joins the community, becomes a supporter |
| 6 | Gate | Sees `entitled: true` → shares **all five libraries** + restores Seerr permissions |

Steps 2 and 4 are yours on purpose. The gate cannot invite anyone and cannot
decide who deserves access.

### The roster entry

```yaml
  - id: their-name
    display: "Their Name"
    exempt: false
    billing:
      holder: what-they-used-on-patreon@example.com   # the ENTITLEMENT email
      amount_usd: 5.0
      rail: patreon
      payer_ref: "how they appear on the receipt"
    accounts:
      - their-plex-email@example.com                  # the PLEX email
```

`holder` and `accounts` **do not have to match**. That is the whole reason the
roster exists: people pay from one address and watch from another, and forcing
them to be equal is how a paying member gets cut off. One lookup per household
on `holder`, applied to every address in `accounts`.

---

## The three states

| State | Plex | Seerr |
|---|---|---|
| accepted, not entitled | `QFlix - Welcome` **only** | permissions `0` |
| entitled | the content libraries, **without** `QFlix - Welcome` | permissions restored |
| revoked, past grace | `QFlix - Welcome` **only** | permissions `0` |

Revoked is deliberately identical to stage 1. The **share object is kept** — a
revoked member is put back in front of the pitch, not evicted. Restoring them
needs no new invite and no re-acceptance.

**The two levels are disjoint, not nested.** Welcome holds one video whose
entire content is "go to Patreon and activate your subscription", so an entitled
member must never be able to see it — they would be pitched something they
already pay for. `plexshare.full_access_ids()` subtracts Welcome by
construction, and a grant to an entitled member who still carries Welcome from a
previous lapse **removes it**. That removal is not a reduction and does not
alert; it is the disjointness rule being enforced. See *Safety properties* for
the short-catalogue rail it has to coexist with.

---

## The clocks

`deadline = max(applicable)`. Nobody is ever cut off with less than a fair
window.

| Clock | Applies to | Set in `defaults:` |
|---|---|---|
| Launch amnesty → **2026-09-01** | everyone already accepted at first run | `amnesty_until` |
| 30 days from acceptance | anyone who accepts after that | `new_arrival_days` |
| 7 days from going false | someone who *was* entitled and lapsed | `grace_days` |

**Delete `amnesty_until` once it passes.** It is migration scaffolding, not
policy.

---

## Arming

**Five** conditions. All five must hold before anything mutates.

1. `secrets/members.yaml` → `armed: true` *(currently `false`)*
2. **Zero unresolved households** *(currently **ten** are unresolved)*
3. `--execute` on the command line, via an on-box drop-in *(currently absent)*
4. not inside the Monday 11:00–15:00 UTC window
5. under `--max-mutations` (default 10; overflow **defers**, it does not abort)

> [!WARNING]
> ### `armed: true` on its own does nothing — and that is the dangerous part
>
> `gate_is_armed()` requires the switch **and** zero unresolved households. A
> household is unresolved until it has `amount_usd`, `rail`, and — for any rail
> that reports by email — `payer_ref`. **All ten** non-exempt households
> currently carry `rail: null` and `amount_usd: null`.
>
> So flipping `armed: true` today changes nothing, the log still says
> `armed=False`, and the natural next move is to fill in rail + amount for all
> ten in a single edit. **That arms all ten simultaneously**, on a shared
> deadline, with no rehearsal.
>
> **Resolve households ONE AT A TIME.** Watch a full run between each. The first
> one you resolve is your rehearsal — if it does something you did not expect,
> you have nine untouched households and a `git checkout` to fall back on.
>
> The blast-radius tripwire is the net under this, not a substitute for it: it
> refuses any run reducing more than a third of governed households. It converts
> the mistake from a mass eviction into a red monitor, but you still have to not
> make it.

Plus one rail that cannot be satisfied, only tripped:

> **Blast-radius tripwire (`--max-reduce-pct`, default 34).** If more than a
> third of governed households would be **reduced** in one run, the run reduces
> **nobody**, pages Discord, and turns the Kuma monitor **red**. Grants still
> apply. Real lapses are independent events — four at once out of ten is not a
> coincidence, it is a bug here or a billing outage upstream. Verified live: a
> deliberately broken roster produces `refused to reduce 10 of 10 governed`
> instead of twenty mutations.

```bash
# 1. review a report-only run first. Never arm blind.
python3 ~/scripts/maint/qflix-entitlement.py

# 2. the drop-in (NOT in the repo — same ritual as the reaper)
mkdir -p ~/.config/systemd/user/manitoba-maint-entitlement.service.d
cat > ~/.config/systemd/user/manitoba-maint-entitlement.service.d/armed.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/python3 %h/scripts/maint/qflix-entitlement.py --execute
EOF
systemctl --user daemon-reload

# 3. flip the roster switch
#    secrets/members.yaml -> armed: true
```

> **Before arming, check the households the service has no record of.** A
> non-exempt household whose `billing.holder` the entitlement service has never
> heard of shows `never_seen: true` on its plan and says so in `reason`. It is
> *usually* correct — the household pays on a rail the service cannot see, or is
> not a Patreon supporter at all — but it is also exactly what a typo looks
> like, and arming while a real supporter's address is misspelled cuts them off.
> Read them out of a report-only run:
>
> ```bash
> # --no-kuma: a manual inspection must not feed the gate's dead-man heartbeat
> # (--no-notify silences Discord only; the Kuma push is a separate flag)
> python3 ~/scripts/maint/qflix-entitlement.py --json --no-notify --no-kuma |
> jq -r '.plans[] | select(.never_seen) | [.state, .email, .household] | @tsv'
> ```
>
> **This does not page** (operator directive, 2026-08-17). It used to, and the
> alert was retired because the Patreon behind the entitlement service now
> carries non-QFlix members while QFlix carries households on rails the service
> cannot see — so `never_seen` became an ordinary steady state for a growing
> slice of the roster rather than an anomaly. Since `expired` is terminal, that
> meant a permanent daily Discord page, per household, on a fact that was not
> going to change. The rare, genuinely actionable form of the signal — an
> **ever-entitled** declared payer going never-seen, i.e. the sync projection
> died — still pages, from the payer oracle (verdict `DEAD`, row 3 below).

---

## Reading a run

```
shares=14 exempt=4 pending=10
  exempt    kh***@gmail.com   household is exempt (...); access is never gated
  pending   sa***@gmail.com   not entitled, 24.8 day(s) of grace remain -- the
                              entitlement service has no record of sa***@gmail.com
                              at all (unknown address, or a rail it cannot see)
  entitled  jo***@gmail.com   entitled; dropping the Welcome library (entitled
                              members are not shown the activate-your-subscription
                              video)
```

| State | Meaning | Your move |
|---|---|---|
| `exempt` | never gated, never provisioned | none |
| `entitled` | full access | none |
| `pending` | not entitled, clock running | fix a `never_seen` address, or wait |
| `expired` | reduced to Welcome | none — working as designed |
| `no-answer` | the API did not answer; **nothing moved** | check `entitlements.starhold.app` |
| `unnamed-share` | accepted share with no household | **add them to the roster** |
| `not-accepted` | invite sent, not taken | none |

Addresses are masked in logs, Discord, and Kuma. The audit manifest and durable
log live at `~/.opt/maint/entitlement/`.

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ran to completion |
| 1 | partial — a per-account step failed |
| **3** | **could not ask** the entitlement API — *not* "nobody is entitled" |
| 4 | Plex or Seerr unreachable |
| 5 | roster invalid, or `QFlix - Welcome` missing — refused to run |

3 is its own code deliberately. "The API says nobody is entitled" and "I could
not reach the API" must never share an exit status.

---

## Safety properties worth knowing

- **An entitlement API outage freezes the gate, it does not drain it.** Granting
  needs a clean `200 + entitled:true`; revoking needs a clean `200 +
  entitled:false`. Every other outcome — timeout, 429, 401, malformed body, a
  stale projection reporting not-entitled — moves nothing and does not advance
  the lapse clock.
- **An unnamed share is never shrunk.** Revoking because a *record* is missing is
  how one roster typo evicts a real person. You get a Discord page instead.
- **An empty Plex section list is refused unconditionally.** plex.tv reads `[]`
  as "unshare this server", which deletes the share and evicts the person.
- **A grant may never take a content section away.** `full_ids` is re-read from
  plex.tv every run and a short poll is accepted as truth, so an entitled member
  holding 5 could be planned down to 2 with the log calling it "raising to full
  access". While the answer *grants*, the target may never be a strict subset of
  the content the member already holds; reduction lives in the expiry branch,
  behind the clocks, alone. **Welcome is excluded from that comparison** — it is
  the floor, not content, and an entitled member carrying it must lose it. Before
  2026-08-17 it was counted, so a returning member held full+1, the rail read
  that as a truncated catalogue, and it cancelled the very write that would have
  dropped Welcome — self-sealing, and it re-alerted daily.
- **Prior Seerr permissions are saved before being zeroed**, so a restore is an
  exact replay rather than a guess at what the default used to be.
- **Most of the membership cannot be reduced in one run.** See the tripwire
  above. It guards the *shape* of the failure, not any particular cause — which
  is why it would have caught both mass-shrink bugs a review found, and is the
  only guard that can catch the next one.

---

## Things that were changed on the live stack

| What | Why |
|---|---|
| Seerr `defaultPermissions` `1153433760` → **0** | `newPlexLogin` is on, so a friend who signed in before the cron fired self-provisioned a **fully enabled** account. Polling faster does not fix a race; being born disabled does. Existing users unaffected. |
| New Plex section `QFlix - Welcome` (key 7, id 145397557) | the floor the whole design rests on. All 14 shares received it automatically via `allLibraries=1`. |
| `entitlement.key` on Starhold + box | QFlix-scoped, **lookup only** — bulk correctly 403s. |
| `grace_days` 3 → 7 | operator, 2026-08-06 |
| `never_seen` demoted from Discord alert to plan field | operator, 2026-08-17. Patreon now carries non-QFlix members and QFlix carries invisible rails, so never-seen is a steady state, not an anomaly. Still in `reason`, in `--json`, and still paging from the payer oracle when an *ever-entitled* payer goes never-seen. |
| Welcome excluded from the grant branch's short-catalogue comparison | operator, 2026-08-17. Entitled members who carried Welcome from a previous lapse were pinned there forever by the rail meant to protect them. |

---

## Verifying it still works

```bash
# report-only, safe any time
python3 ~/scripts/maint/qflix-entitlement.py

# full live cycle on the crash-test account, restores itself and diffs
python3 ~/e2e-entitlement-live.py
```

The e2e mutates exactly one account. Every other household is marked exempt in
a throwaway roster, and exempt is terminal.


---

# Part II — the payer oracle (money path)

> Grafted from the money-path council merge (wf_d43e030e-347, 2026-08-08).
> Where this part and Part I disagree on ARMING, Part I governs: arming is
> FIVE conditions resolved one household at a time, never a two-step ritual.

## 2. The payer-oracle verdict table

Computed by **one** pure module, `scripts/maint/lib/payer_oracle.py`, and
consumed by both the gate (`qflix-entitlement.py --oracle-check` /
`--arm-check`) and the canary (`scripts/canaries/entitlement-service.sh`,
leg 5). Inputs: `D` = declared payers (non-exempt, non-provisional household,
`billing.rail` set, `billing.amount_usd > 0`); `E` ⊆ `D` = those ever
entitled; `A` = each declared payer's current lookup answer; `B` = the bulk
cross-check (`GET /v1/entitlements`); `age` = time since the oldest
declaration; `settle_days` = 2.

| # | Condition (first match wins) | Verdict | Canary |
|---|---|---|---|
| 1 | `len(D) == 0` | `DORMANT` | green — nothing to prove |
| 2 | any `A` is YES | `PROVEN` | green |
| 3 | `E` non-empty and **any** member of `E` is now `never_seen` | `DEAD` | **red** |
| 4 | `B.supported` and `B.count > 0` and no declared holder appears in `B.entitled` | `MISMATCH` | **red** — a member may be paying under a different address |
| 5 | `B.supported` and `B.count > 0` | `PROVEN_UPSTREAM` | green |
| 6 | `age < settle_days` | `SETTLING` | green — prints hours remaining |
| 7 | `B` unavailable (no scope / unreachable / unparseable) and `E` empty | `UNPROVEN_BLIND` | **red** — names the exact fix |
| 8 | `B.supported` and `B.count == 0` and `E` empty | `UNPROVEN_EMPTY` | **red** |

**Today's live state is row 7, `UNPROVEN_BLIND`, and that is correct.** The
money path has never demonstrated a success (0 accounts ever entitled) and
the bulk cross-check answers `403 {"error": "this key lacks the 'bulk'
scope"}`. It clears the moment the operator grants the scope (row 5/2/6
follow) or the first real `YES` lands (row 2).

Rows 3 and 4 are deliberately strict: row 3 fires on **any** forgotten
ever-entitled account, not only when every one is lost at once, because with
a small number of payers one silent loss is still money lost in silence. Row
4 is the highest-value alert in the system once it can fire at all — it
means the service knows of a paying account that matches nobody's declared
billing address, i.e. someone is paying under an address the roster does not
have on file and is on a clock to be reduced while paying.

## 3. Reading the verdict

```
# read-only, no mutation, no Kuma push, no Plex/Seerr I/O at all
python3 scripts/maint/qflix-entitlement.py --oracle-check

# read-only, full preview including the exact (masked) set that would be
# reduced if this run executed with --execute
python3 scripts/maint/qflix-entitlement.py --arm-check
```

Both print `VERDICT=`, `RED=0|1` and `DETAIL=` (never an unmasked address or
household id). `--arm-check` additionally exits 2 (`EXIT_ARM_CHECK_RED`) on
**either** a red verdict **or** any account that would actually be reduced —
both are independent "do not arm" signals, and either one alone must hold
the gate.

The canary (`manitoba-maint canary push entitlement-service`, hourly)
carries the same verdict as its fifth leg, folded into a single Kuma
monitor: **Canary Entitlement Service**.

## 5. Disarm / rollback

Two independent switches; either one alone re-disarms:

```yaml
# secrets/members.yaml
armed: false
```

```
systemctl --user revert manitoba-maint-entitlement.service
systemctl --user daemon-reload
systemctl --user restart manitoba-maint-entitlement.service
```

`systemctl --user revert` removes the on-box drop-in and restores the
repo-shipped (flagless, report-only) unit file. After either step the next
run reports and mutates nothing; after both, the gate is back to its shipped
state. `armed:` flipping to `false` never *drains* an already-provisioned
member — see the YES/NO/UNKNOWN asymmetry below — it only stops **new**
mutations.

## 6. What arming does and does not change

- `grants` iff a clean `YES`; `revokes` iff a clean `NO`; both are `False`
  for `UNKNOWN`. An entitlement API outage freezes the gate — it never
  drains it. Arming does not change this; it is structural in
  `lib/entitlement.py`.
- Access stays binary. Tiers or pledge-amount thresholds are explicitly out
  of scope.
- No member email, username, household id, or viewing activity appears in
  this document, in the canary's Kuma message, or in Discord. See
  `lib/payer_oracle.py`'s masking law and the `never-publish-member-data`
  operator directive.

## 7. If the canary goes red after arming

Read `DETAIL=` from `--arm-check` first — it names the exact verdict and,
for `UNPROVEN_BLIND`, the exact operator action (grant the `bulk` scope).
For `DEAD` or `MISMATCH`, do **not** disarm reflexively: both are the
*money-losing* alerts this whole change exists to surface, and the correct
first action is to check the named (masked) example, not to silence the
monitor. Disarm (§5) only if the gate itself is behaving unexpectedly, not
merely because it started reporting something real.
