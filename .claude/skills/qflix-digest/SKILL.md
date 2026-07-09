---
name: qflix-digest
description: Use to generate the QFlix newsletter's weekly "Behind the scenes" blurb — a warm, non-technical, subscriber-facing summary of what improved this past week, derived from the repo's commits and published to the newsletter-digest branch. Invoked manually or by the Monday 14:00 UTC scheduled cloud routine, one hour before the newsletter sends.
---

# QFlix weekly digest blurb

Turn this week's commits into one short, friendly paragraph for **Plex members**
(non-technical) and publish it where the newsletter can read it. The newsletter
(`scripts/qflix-newsletter`) fires Monday **15:00 UTC**; this runs at **14:00 UTC**,
one hour ahead. If this never runs, the newsletter falls back to an auto-generated
commit list — so a good blurb here is an upgrade, never a hard dependency.

## Output contract

Write `digest/latest.json` on the **`newsletter-digest`** branch:

```json
{
  "week_of": "YYYY-MM-DD",
  "generated_at": "YYYY-MM-DDTHH:MM:SSZ",
  "since": "YYYY-MM-DDTHH:MM:SSZ",
  "html": "<friendly blurb as inline HTML>"
}
```

- `week_of` = today's date (UTC). The newsletter rejects a blurb whose `week_of`
  is not within the current send week, so this MUST be today's run date.
- `html` = the blurb as simple inline HTML (`<p>…</p>`, maybe one `<ul>`). No
  `<style>`, no scripts, no images. It is dropped verbatim into a dark card, so
  keep text light-on-dark friendly (don't set colors; the card handles that).

## Editorial rules — translate, don't transcribe

The audience are members who just want their movies and shows. They do not know
or care about `GOMAXPROCS`, `*arr`, or `vlogs`.

- **Lead with the benefit to them.** "cap GOMAXPROCS=4 to stop crash-loop" →
  "more reliable streaming". "add SABnzbd + NZBgeek Usenet path" → "a new
  high-quality download source, so new titles arrive faster and look better".
- **Include** user-facing improvements: new capabilities (`feat`), reliability
  and speed (`fix`, `perf`), anything a member would notice.
- **Skip** pure internals: docs, chores, refactors, CI, audits, decommissions,
  and changes to the newsletter/digest machinery itself. If a week is all
  internals, write a brief, honest "quiet week — kept everything humming"
  rather than inventing news.
- **Never** mention deleting members' content, version numbers, file names,
  branch names, or jargon. No marketing fluff. 2–4 sentences or ≤4 short bullets.
- Warm, a little playful, signed off lightly (a single emoji is fine).
- **Upkeep/tune-ups: keep it generic, never specific.** A brief "a few reliability
  tune-ups round things out" is fine and welcome. But do NOT name specific apps or
  versions — the newsletter auto-renders a concrete "⚙️ This week's tune-ups" line
  (Plex, the request system, +N behind-the-scenes apps) from the Monday window's
  `last-upgrade.json`, and ONLY on weeks with no blurb (when your blurb is present
  it's suppressed to avoid duplicating you). So you own the narrative; the
  deterministic line is just the fallback. Focus on what members will *notice*.

## Procedure

1. **Gather the week.** From the repo root on `master`:
   ```bash
   git log --since="7 days ago" --pretty=format:"%h %s%n%b%n---"
   ```
   Read subjects and bodies. Note any commit body line starting `Newsletter:` —
   that is the operator's hand-written phrasing; prefer it verbatim.

2. **Draft the blurb** per the editorial rules above. Build the JSON object.
   `generated_at`/`since` use real UTC timestamps (`date -u +%Y-%m-%dT%H:%M:%SZ`).

3. **Publish to the `newsletter-digest` branch** without disturbing `master`,
   via a throwaway worktree:
   ```bash
   WEEK=$(date -u +%Y-%m-%d)
   TMP=$(mktemp -d)
   git fetch origin newsletter-digest
   git worktree add "$TMP" newsletter-digest        # branch is pre-seeded; exists
   mkdir -p "$TMP/digest"
   # write the JSON to "$TMP/digest/latest.json"
   ( cd "$TMP" && git add digest/latest.json \
        && git commit -m "chore(digest): week of $WEEK" \
        && git push origin newsletter-digest )
   git worktree remove "$TMP" --force
   ```
   If `newsletter-digest` does not exist yet (first ever run), create it as an
   **orphan** branch containing only `digest/latest.json`, then push it.

4. **VERIFY-AFTER-PUSH (mandatory).** After the `git push origin
   newsletter-digest` above, re-fetch the raw URL and assert `week_of`
   equals today (UTC):
   ```bash
   TODAY=$(date -u +%Y-%m-%d)
   LIVE=$(curl -fsS "https://raw.githubusercontent.com/Quadstronaut/QFlix/newsletter-digest/digest/latest.json")
   LIVE_WEEK_OF=$(printf '%s' "$LIVE" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("week_of",""))')
   if [ "$LIVE_WEEK_OF" != "$TODAY" ]; then
     echo "VERIFY-AFTER-PUSH FAILED: live week_of='$LIVE_WEEK_OF' expected '$TODAY'" >&2
     exit 1   # see FAIL-LOUD below — this IS a run failure, not a soft warning
   fi
   ```
   A `curl` failure (network/CDN, branch didn't actually update, GitHub raw
   caching lag) is **equally** a VERIFY-AFTER-PUSH failure — treat any
   non-zero `curl` exit or a `week_of` mismatch as a RUN FAILURE, not
   something to shrug off. This closes the gap the pre-hardening version of
   this step only soft-confirmed ("Confirm `week_of` is today. Done.") —
   the 2026-07-06 miss (routine fired per `last_fired_at`, produced no
   commit) went undetected specifically because nothing asserted this step
   actually succeeded.

5. **FAIL-LOUD (mandatory).** On ANY failure in this run — session/model
   couldn't initialize, `git push` auth/permission error, or the
   VERIFY-AFTER-PUSH mismatch/failure above — send an operator alert via
   the routine's Gmail MCP:
   - Subject: `QFlix digest publish FAILED <YYYY-MM-DD>` (today, UTC)
   - Body: the captured error (git/curl output, exception text, or
     "session could not initialize" if the failure is that early)

   This exists because every prior failure mode here was **silent**: a
   cloud-session failure produces no commit and (before this hardening) no
   signal anywhere. The box-side canary
   (`scripts/canaries/newsletter-digest-stale.sh`) is the durable backstop
   that catches a miss regardless of whether this step itself ran — it
   independently re-checks freshness at Monday send time — but this
   Gmail alert gets the failure to the operator ~40min-1h earlier, while
   there's still time to manually re-run this skill before the 15:00 UTC
   send.

Done — the newsletter will pick up the fresh blurb at 15:00 UTC.

## Notes

- The repo is public; reading commits needs no token. **Pushing** the branch
  needs write access — the scheduled cloud routine must run with the repo
  connected for write. If push fails (or VERIFY-AFTER-PUSH fails), the
  newsletter still sends via its deterministic fallback — see FAIL-LOUD
  above for how the operator finds out this happened.
- Keep `digest/latest.json` to a single current week. History lives in the
  branch's git log; the newsletter only ever reads the latest.
- Cloud-vs-box: blurb generation stays HERE, in the cloud routine — the
  seedbox has no Claude access, and the box is designed to run with the
  operator's PC off. What moved to the box is OBSERVABILITY: the canary
  above watches the *outcome* (is the branch fresh at send time?)
  independent of whether this routine ran, fired-but-failed, or was never
  invoked at all — the two are complementary, not redundant: this
  step's Gmail alert is faster when it fires; the canary is the guarantee
  that catches every failure mode, including ones where this routine
  didn't run far enough to send its own alert.
