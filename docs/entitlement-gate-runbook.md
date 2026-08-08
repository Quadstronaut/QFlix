# Entitlement gate runbook

Status as of 2026-08-07: **shipped, disarmed.** `armed: false` in
`secrets/members.yaml` and no `--execute` drop-in on the box. This document
is how an operator reads the payer oracle, arms the gate when it is safe to,
and rolls back if anything looks wrong.

It assumes the architecture decision in the Stage-0 spec: **QFlix never holds
a Patreon credential on the money path.** Everything below reads Patreon
exclusively through `entitlements.starhold.app`.

## 1. The money path, in one line

```
Patreon pledge -> entitlements.starhold.app (Starhold-owned sync) -> QFlix
entitlement gate (qflix-entitlement.py, lib/entitlement.py) -> Plex share
sections + Seerr permissions
```

The gate itself has had a dead-man since before this change ("QFlix
Entitlement Gate" Kuma monitor). What this change adds is a way to tell
whether the *service* the gate reads has ever actually carried a real
success through that whole pipe — a frozen, always-green gate looks
identical to a working one from the gate's own heartbeat.

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

## 4. The arming ritual — exactly two steps, in order

Arm **only** after the oracle reads `PROVEN` or `PROVEN_UPSTREAM` (rows 2 or
5). Arming on `SETTLING`, `DORMANT`, or any red verdict is arming on hope.

**Step 1 — the roster switch.** In `secrets/members.yaml` (gitignored, on
the box):

```yaml
armed: true
```

This alone does nothing: `MEM.gate_is_armed()` also requires zero
`unresolved()` households (no `provisional: true` rows, every non-exempt
household has a resolved `billing` block). The gate logs the specific
blocker if this is not yet true.

**Step 2 — the on-box drop-in.** Never edit the repo's
`manitoba-maint-entitlement.service` to add `--execute` — arming lives only
on the box, so a repo clone can never accidentally arm a mass mutation. The
**`ExecStart=` reset is mandatory**: systemd appends `ExecStart=` lines by
default, so omitting the blank reset line runs the flagless command *and*
the armed one, doubling every mutation in one run.

```
systemctl --user edit manitoba-maint-entitlement.service
```

Contents of the override:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/python3 %h/scripts/maint/qflix-entitlement.py --execute
```

Then:

```
systemctl --user daemon-reload
systemctl --user restart manitoba-maint-entitlement.service
```

The maintenance-window guard and the `--max-mutations` / `--max-reduce-pct`
tripwires stay in force regardless of arming — arming only removes the
"report only" interlock, not the blast-radius rails.

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
