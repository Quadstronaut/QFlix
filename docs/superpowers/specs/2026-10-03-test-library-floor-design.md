# QFLX-4 — Test library on the entitlement floor + new-household pre-seed

Status: design approved in session 2026-10-03, awaiting written-spec review.
Jira: QFLX-4. Branch: `feature/QFLX-4-test-library`.

## Intent (operator's words, paraphrased)

1. A new person is about to be invited. Do the **minimum** so the environment
   is ready *before* the Plex invite goes out.
2. Add a library of playback test videos so a **prospective member can check
   "does it work" on their device before subscribing**. The operator places the
   files by hand; nothing may process them.

## Decisions (each one asked, not assumed)

| # | Question | Answer |
|---|---|---|
| Q1 | What is the new person to the roster? | New paying household |
| Q2 | Is the Patreon email the same as the Plex email? | Unknown yet — seed with the Plex email, correct later |
| Q3 | Rail + amount | Patreon, $50 |
| Q4 | Household id / display | first-name id (box roster only, never git) |
| Q5 | Who sees the test library? | **The unentitled.** Owner sees everything as owner. Nobody else. |
| Q6 | What should the test teach? | Just "does it work" |
| Q7 | Tiers / processing | **Static library of operator-placed assets, processed by nothing** |
| Q8 | Title | `QFlix - Test` (folder `~/media/Test`) |
| Q9 | Plex's own per-library processing | Keep Plex defaults |
| Q10 | Frozen (`unknown-payer`) households | Leave frozen alone — no Test for them |

## Facts this design rests on (verified, with source)

- **Gate state today** (live report-only run, 2026-10-03 07:xx UTC): 13 plans —
  3 entitled, 4 exempt, 5 unknown-payer, 1 expired, 0 expired-and-mutating.
- **An unresolved household disarms the whole gate** —
  `scripts/maint/lib/members.py` `gate_is_armed()` returns False if *any*
  household lacks `amount_usd`/`rail`/`payer_ref`. The seed row must be fully
  resolved on first write.
- **A roster household with no Plex share is informational, never fatal** —
  `members.py` `reconcile_shares()` docstring. Pre-seeding is supported.
- All four existing Patreon households use `holder == payer_ref == accounts[0]`.
- **PENDING / UNKNOWN_PAYER plans make no Plex change**; only EXPIRED is reduced
  to the floor; ENTITLED is set to `full_access_ids` —
  `qflix-entitlement.py` `plan_for_share()`.
- **Tripwire counts EXPIRED-and-mutating as reductions**; denominator =
  entitled + pending + expired + no-answer + unknown-payer. Adding Test moves
  the 1 expired share → 1/9 = 11% < 34% default. Safe.
- `full_access_ids()` = every live section minus Welcome, recomputed every run
  (`lib/plexshare.py:298`). Without a change, a new library reaches every
  entitled member within 15 minutes.
- Live PMS **1.43.3.10896** `/system/agents` lists `tv.plex.agents.none`
  = "Plex Personal Media"; scanners include "Plex Video Files". This is
  the Other Videos pair. (A web-research pass returned the legacy
  `com.plexapp.agents.none`; the live server is the authority.)
- Plex share pickers allow choosing specific libraries at invite time
  (support.plex.tv/articles/201105738-creating-and-managing-server-shares/).
- Static guarantee — these consumers use **hardcoded** library/path lists and
  will never see `~/media/Test`: Tdarr (`50b-tdarr-config.py LIBRARIES`),
  reaper (`qflix-reaper.py LIBRARIES`), audio-disposition janitor
  (`DEFAULT_ROOTS`), anime janitor (`ANIME_PAIRS`), stats (`MOVIE_LIBS`/
  `SHOW_LIBS`), library-container-sanity (`LCS_ROOTS`).
- `kometa-libraries.sh` checks Kometa libs ⊂ Plex titles only — a new Plex
  library does **not** redden it.
- `qflix-poster-janitor.py` names any section not in `SECTION_NAMES` or
  `UTILITY_SECTIONS` in every Kuma message — Test must join `UTILITY_SECTIONS`.

## Part A — pre-seed the household (box only, no code)

Edit `~/secrets/members.yaml` on the box (never the repo — public, and
`test_no_pii_in_repo` guards it):

```yaml
  - id: <first-name>
    display: "<First name>"
    exempt: false
    billing:
      holder: <plex email>
      amount_usd: 50
      rail: patreon
      payer_ref: <plex email>
    accounts:
      - <plex email>
```

Procedure: backup to `members.yaml.pre-QFLX-4-<date>` → write candidate to a
temp file → load it with `lib.members` loader (must parse, and
`gate_is_armed()` must stay `(True, "armed")`) → atomic replace → report-only
gate run (`--json --no-notify --no-kuma`) shows the same 13 plans and no new
alert. If the Patreon address later differs, edit `holder` + `payer_ref` only.

Lifecycle after the invite: accepted → gate creates their Seerr row at perms 0 →
`unknown-payer` (frozen at whatever the invite granted: Welcome + Test) →
Patreon sees their address → `entitled` → full libraries, Welcome+Test removed,
Seerr perms restored. Listmonk picks them up at the 04:00 nightly sync.

## Part B — the test library

### B1. Gate: Welcome becomes a *floor set*

`lib/plexshare.py`:

- `minimum_access_ids(sections, welcome_title, extra_floor_titles=())` →
  Welcome id (REQUIRED — still raises when absent; this is the anti-eviction
  rail) plus the id of each extra floor title that exists. Missing extras are
  skipped, never raised on.
- `full_access_ids(sections, welcome_title, extra_floor_titles=())` → every
  section minus Welcome minus every extra floor title. Still raises on empty.
- New helper `missing_floor_titles(sections, extra_floor_titles)` for reporting.

`qflix-entitlement.py`:

- `DEFAULT_FLOOR_EXTRA = ("QFlix - Test",)`; repeatable `--floor-section`
  overrides it. Threaded into both call sites (`:1289`, `:1464`).
- When an extra floor title is missing, the run's Kuma message appends
  `floor missing: QFlix - Test`. Logged, not paged (steady state while the
  library does not exist; paging on it would be base-rate noise).
- No other branch changes. The grant rail already subtracts `minimum_ids`
  from `held_content`, so Test in minimum is handled by construction.

Rejected: a `members.yaml` key (box-only, untested policy surface; the dead
`defaults.paused_sections` knob is evidence of how that goes).

### B2. Library creation — extend 59b, don't fork it

`scripts/configure/59b-plex-welcome-library.py` gains `--agent` and
`--scanner` (defaults unchanged: `tv.plex.agents.movie` / `Plex Movie`).
Invocation:

```
~/.apps/python-plexapi/venv/bin/python ~/scripts/configure/59b-plex-welcome-library.py \
  --title "QFlix - Test" --path ~/media/Test \
  --agent tv.plex.agents.none --scanner "Plex Video Files"
```

Creation hands Test to every share still carrying `allLibraries="1"`
(59b docstring). Pre-flight counts those shares (counts only, no identities).

### B3. Exclusions

- `qflix-poster-janitor.py UTILITY_SECTIONS += ["QFlix - Test"]`, with an
  operator-dated reason comment matching Welcome's.
- Nothing else — every other consumer is already blind to it (see Facts).

### B4. Docs

- `docs/entitlement-gate-runbook.md`: onboarding step 2 → share
  **`QFlix - Welcome` + `QFlix - Test`**; three-states table floor column;
  step 6 "all five libraries" → "the content libraries".
- README / inventory library-count mentions that C-06 guards, if any change.

## Testing

- `tests/unit/test_welcome_section_is_exclusive.py` (extend):
  Test ∈ minimum; Test ∉ full; Test absent → minimum == [Welcome] and no raise;
  Welcome absent → raises; Welcome + Test the only sections → full raises.
- Gate plan tests: expired share holding [Welcome] → target [Welcome, Test];
  entitled share holding full+Test → target full, no short-catalogue alert;
  unknown-payer untouched.
- Poster-janitor: Test not named as unmanaged.
- Full suite **after `git add`** (audit boundary is the git index).

## Rollout (never inside Mon 11:00–15:00 UTC)

1. Part A (independent of code).
2. PR → green pytest/audit/pwsh → merge → deploy box to master (deploy-drift).
3. Gate report-only run on new code: `floor missing: QFlix - Test`, no plan
   changes.
4. Pre-flight `allLibraries=1` count → create library (B2) → read back.
5. Next gate run: 1 expired share → Welcome+Test; entitled unchanged; tripwire
   quiet; Kuma green.
6. Operator SFTPs clips into `~/media/Test`; confirm Plex auto-scan pref,
   fallback manual section refresh; confirm items visible.
7. Operator invites the new member ticking **Welcome + Test** only.
8. Session end: master == GitHub == box.

## Out of scope

Frozen households receiving Test (Q10). Transcoding/normalizing test clips
(Q7). Any 4K / codec matrix (Q6, Q7).
