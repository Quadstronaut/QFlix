# Entitlement gate — operator handoff

Everything the code could do, it has done. What is left is genuinely human:
a Starhold-side permission change, an out-of-band identity check, and one
decision about arming. Four items, in order.

## 1. Grant the QFlix entitlement key the `bulk` scope on Starhold

**Evidence this is still needed:**

```
GET /v1/entitlements  ->  403 {"error": "this key lacks the 'bulk' scope"}
```

This is the one-line change referenced in the Stage-0 spec (§2, A2). It is
Starhold-side and cannot be done from this repo or this box. Until it lands,
`payer_oracle.judge()` reads `UNPROVEN_BLIND` (row 7) — correctly, because
today the money path genuinely cannot be cross-checked at all.

## 2. Confirm the three declared payers' Patreon addresses match `billing.holder`

The entitlement service can only answer for the address it is asked about.
If any declared payer pledges under a different email than
`secrets/members.yaml`'s `billing.holder`, the gate will look up the wrong
address forever and — once armed — reduce a paying member as if they never
subscribed. This is a roster-accuracy check the operator has to make by
comparing the Patreon dashboard against `secrets/members.yaml` directly;
nothing in this repo can see both sides at once without holding a Patreon
credential on the money path, which the architecture decision (§2, A1)
forbids.

If a mismatch is found and local Patreon visibility would help confirm it,
see item 3.

## 3. OPTIONAL — register a SEPARATE Patreon OAuth client for local reporting

Only if local Patreon reporting (`patreon-report.py`) is wanted. It is a
workstation-only diagnostic, off the money path (§2, A3/A4) — it never runs
on the box and the box has no `secrets/patreon.json`.

Register a new client at
<https://www.patreon.com/portal/registration/register-clients>. **Never
reuse the entitlement service's client.** Patreon refresh tokens are
single-use; two holders of one client guarantee one gets randomly locked
out, and if the QFlix-side client is ever the *same* one Starhold uses,
rotating it here spends Starhold's token and breaks the sync the whole money
path depends on. `patreon-report.py` now refuses to rotate at all unless
`--allow-token-rotation` is explicitly passed, and names this exact hazard
at the point of refusal — but the safety flag only helps if the client
behind it is actually separate.

Verify what is on disk without ever touching the network:

```
python3 scripts/maint/patreon-report.py --verify
```

Prints which credential keys are present — never their values.

## 4. Arm — only after the oracle reads `PROVEN` or `PROVEN_UPSTREAM`

```
python3 scripts/maint/qflix-entitlement.py --arm-check
```

Read `VERDICT=`. If it is `PROVEN` or `PROVEN_UPSTREAM` (and `RED=0`,
`WOULD_REDUCE=0` unless a reduction is genuinely expected and already
understood), follow the two-step arming ritual in
`docs/entitlement-gate-runbook.md` §4: the `armed: true` roster switch, then
the on-box `systemctl --user edit` drop-in with the mandatory `ExecStart=`
reset.

Do not arm on `SETTLING`, `DORMANT`, `UNPROVEN_BLIND`, or `UNPROVEN_EMPTY` —
none of those has demonstrated the path works yet. If the oracle ever reads
`DEAD` or `MISMATCH` **after** arming, that is the alert working as
designed, not a reason to silence it — see the runbook §7.
