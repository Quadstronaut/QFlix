# Reporting

Operator reporting tools. Read-only. None of these grant or revoke access.

## Patreon support report

`scripts/maint/patreon-report.py` — reads Patreon v2 creator API, matches patrons
to roster households, prints reconciliation.

### Setup

Register client at `patreon.com/portal/registration/register-clients`.

| Field | Value |
|---|---|
| Client API Version | **2** (v1 is legacy, different member fields) |
| Redirect URI | `http://localhost:8000/callback` (required by form, unused) |
| Scopes | `identity`, `campaigns`, `campaigns.members`, `campaigns.members[email]` |

Write four values to `secrets/patreon.json` (gitignored):

```json
{
  "client_id": "...",
  "client_secret": "...",
  "access_token": "...",
  "refresh_token": "..."
}
```

Override path with `--creds` or `$QFLIX_PATREON_CREDS`.

### Usage

```
python scripts/maint/patreon-report.py
python scripts/maint/patreon-report.py --json
python scripts/maint/patreon-report.py --full          # unmask names/emails
python scripts/maint/patreon-report.py --out ~/rep.txt # refused inside tracked dirs
```

### Exit codes

Read this section before wrapping the tool in cron. The codes exist to keep two
very different situations apart, and conflating them is the failure this tool is
built to prevent.

| Code | Meaning |
|---|---|
| 0 | Real, successful read. Includes a genuinely empty campaign — reported in words as "ZERO patrons". |
| 3 | `AUTH_FAILED`. Token dead or refresh rejected. **Emits no member list at all.** |
| 4 | `API_FAILED`. Patreon returned non-200, or output path refused. |
| 5 | `CONFIG`. Credentials file missing or incomplete. |

Why it matters: Patreon creator access tokens expire (~1 month) and expire
*silently* — the API just starts answering 401. A client that swallows that
reports zero patrons, which is indistinguishable from every patron lapsing at
once. Exit 3 reports nothing rather than a false mass-lapse. **Never treat a
non-zero exit as "no patrons".**

### Roster matching

Matches on `(rail='patreon', payer_ref)` via `lib/members.Roster.by_payer_ref()`,
case-insensitive, against `full_name` then `email`.

Three output buckets:

- **matched** — patron has a roster household
- **unmatched_patrons** — paying, no household on file
- **roster_on_patreon_without_patron** — household claims `rail: patreon`, no patron record

Roster lives in `secrets/members.yaml`, never in the repo. Missing roster is not
fatal — reports the Patreon side alone and says so on stderr.

### What it does not do

No gate writes. No Plex, Seerr, or Listmonk calls. No roster mutation. No
`--execute` flag, because there is nothing to execute. `patron_status` here is
information for a human, not an entitlement decision.

Same separation `lib/members.py` already draws: roster is operator intent, rail
is external fact. Conflating them turns a payment outage into a mass revocation.

### Token rotation

On 401 the refresh token is spent for a new pair and `secrets/patreon.json` is
rewritten atomically (tmp + `os.replace`). Patreon rotates the *refresh* token
too — keeping the old one locks you out. A torn write costs a manual
re-registration, hence the atomic rewrite.

### PII

Names and emails are member data. Masked by default. `--out` refuses any path
under `manifest/`, `docs/`, `scripts/`, `tests/`, `apps/`. Nothing this tool
produces belongs in git.
