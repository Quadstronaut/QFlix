# Runbook — autonomous buildarr v4/v5 patching session

Operator-facing companion to [`buildarr-v4-patch-session-prompt.md`](buildarr-v4-patch-session-prompt.md). Read this before launching the autonomous session — it's your safety net.

## What this session is

A fresh Claude Code instance, with all tools enabled, that iterates through Sonarr v4 / Radarr v5 API drifts in `buildarr-sonarr 0.6.4` + `buildarr-radarr 0.2.6`, patching each one in the live venv until `systemctl --user start --wait buildarr.service` exits with `Result=success`. Then it captures the patches as committed artifacts and flips the manifest probe back to `systemd_oneshot`.

**Estimated time**: 30 min – 2 hr depending on how many distinct API drifts surface. Hard cap at 25 loop iterations or 15 patches per plugin (whichever first).

## Pre-flight (already done — verify before launching)

The session-launching engineer ran these on 2026-05-11 06:51 CEST. If any fail today, **do not launch** — re-establish the preconditions first.

| Check | Command | Expected |
|---|---|---|
| Snapshot exists | `ssh quadstronaut@seedbox.example.com 'ls -la ~/.purged-2026-05-11/buildarr-pre-autonomous-fix.tar.gz'` | ~9 KB file |
| Pip freeze captured | `ssh ... 'wc -l ~/.purged-2026-05-11/buildarr-pip-freeze-pre-autonomous.txt'` | 31 lines |
| Rollback script deployed | `ssh ... 'ls -la ~/scripts/maint/rollback-buildarr-patches.sh'` | file present, executable |
| Kuma Buildarr monitor UP | `ssh ... 'sqlite3 ~/.apps/uptimekuma/kuma.db "SELECT status,msg FROM heartbeat h JOIN monitor m ON h.monitor_id=m.id WHERE h.id=(SELECT MAX(id) FROM heartbeat WHERE monitor_id=m.id) AND name=\"Buildarr\";"'` | `1\|active` |
| buildarr.service idle | `ssh ... 'systemctl --user is-failed buildarr.service'` | `inactive` (exit 1) |
| Silent iteration verified | The 2026-05-11 audit ran a deliberate buildarr failure and confirmed Kuma stayed green through one pusher cycle. | — |

Deploy the rollback script to the seedbox before launching:

```bash
scp /p/Documents/GIT/QFlix/scripts/maint/rollback-buildarr-patches.sh \
    quadstronaut@seedbox.example.com:~/scripts/maint/
ssh quadstronaut@seedbox.example.com 'chmod +x ~/scripts/maint/rollback-buildarr-patches.sh'
```

## Launching the session

1. Open a fresh Claude Code window at `P:\Documents\GIT\QFlix`.
2. Confirm permission mode. Two options:
   - **Default (per-tool prompts)**: safest. You'll approve each SSH command, each file write. Slow but everything is auditable in real time.
   - **`--dangerously-skip-permissions`**: fast. Recommended only after you've watched one normal-mode session and trust the prompt's safety rails. Skips approval prompts entirely.
3. Paste the fenced block from `buildarr-v4-patch-session-prompt.md` as the first message.
4. The agent will read inventory.md + the manifest, then start iterating. Expect heavy use of `TaskCreate`/`TaskUpdate` so you can watch progress.

## What to watch in real time

Pin a terminal with this command running in a loop and keep an eye on the right-hand column:

```bash
watch -n 30 'ssh quadstronaut@seedbox.example.com "
echo \"buildarr.service: \$(systemctl --user show buildarr.service -p Result -p ActiveState --value | tr \"\\n\" \" \")\"
sqlite3 ~/.apps/uptimekuma/kuma.db \"SELECT \"Kuma Buildarr: status=\" || status || \" msg=\" || substr(coalesce(msg,\"\"),1,30) FROM heartbeat h JOIN monitor m ON h.monitor_id=m.id WHERE h.id=(SELECT MAX(id) FROM heartbeat WHERE monitor_id=m.id) AND name=\"Buildarr\";\"
echo \"notify-fail tail:\"
tail -2 ~/.opt/maint/notify-fail.log 2>/dev/null
"'
```

**Green lights:**
- `Kuma Buildarr: status=1 msg=active` (stays this throughout iteration)
- No new lines in `notify-fail.log` with today's date
- TaskCreate/TaskUpdate list shows linear progress (new patches landing, no patches getting unwound twice)

**Red flags — abort immediately:**
- Kuma Buildarr status flips to 0
- A new `notify-fail.log` line with level=error appears
- Agent says "applying patch attempt 3 of preferred field" — means it's flailing on the same patch site
- Agent tries to commit anything
- Agent tries to touch files outside the allowed list (heartbeat scripts, nginx, secrets dir, recovery.py, etc.)
- TaskCreate count exceeds ~20 patches

## How to interrupt

The agent watches for the words **"stop"**, **"abort"**, or **"pause"** in any operator message. Type any of those and the agent should:
1. Stop iterating
2. Restore the most recent safe buildarr.yml state
3. Report what was learned
4. Exit cleanly

If the agent doesn't respond (rare — usually means it's mid-tool-call): hit Ctrl+C in the Claude Code window to cancel the in-flight tool, then send "stop" again.

If even that fails: close the Claude Code window. Then run the rollback (next section).

## How to roll back

The rollback restores the seedbox-side state to the 2026-05-11 06:51 CEST snapshot. It does NOT touch the repo-side files the agent created (`scripts/patches/*.patch`, `scripts/configure/60-buildarr-patches.sh`, etc.) — review those with `git status` and `git restore` whichever you want to discard.

```bash
ssh quadstronaut@seedbox.example.com '~/scripts/maint/rollback-buildarr-patches.sh'
```

Expected output:
- 6 numbered steps, all green
- Final verification block shows buildarr.service ActiveState=inactive and Kuma Buildarr status=1/msg=active

After rollback, optionally clean up repo-side agent artifacts:

```bash
cd P:\Documents\GIT\QFlix
git status   # see what the agent staged
git diff     # review changes
# To discard everything the agent created:
git restore --source=HEAD --staged --worktree \
    manifest/apps.yaml inventory.md \
    docs/buildarr-v4-patch-session-prompt.md docs/buildarr-v4-patch-session-runbook.md \
    scripts/maint/rollback-buildarr-patches.sh
rm -rf scripts/patches/ scripts/configure/60-buildarr-patches.sh
# Memory cleanup:
# Manually edit C:\Users\Quadstronaut\.claude\projects\P--Documents-GIT-QFlix\memory\project_buildarr-upstream-broken.md
# back to "still broken" if the agent updated it prematurely.
```

## How to know the session succeeded

The agent's final report should include:
- ✅ A list of patches landed in `scripts/patches/buildarr-*.patch` (expect 3 baseline + 5-10 new = 8-13 total)
- ✅ `scripts/configure/60-buildarr-patches.sh` exists and is idempotent
- ✅ The manifest `buildarr:` entry now reads `unit: buildarr.service` + `kind: systemd_oneshot`
- ✅ One successful end-to-end run: `systemctl --user show buildarr.service` shows `Result=success`
- ✅ `pytest tests/unit/ -q` shows ≥243 passing
- ✅ Kuma Buildarr monitor pushes `msg=success` after the pusher restart

After accepting the session result, you'll want to:

1. Truncate the cron mail spool (still has 583 messages of stale heartbeat-tdarr-node spam from before the XDG fix):
   ```bash
   ssh quadstronaut@seedbox.example.com '> /var/spool/mail/quadstronaut'
   ```
2. Review the agent's repo-side changes (`git status` + `git diff`) and commit when you're satisfied:
   ```bash
   cd P:\Documents\GIT\QFlix
   git add manifest/apps.yaml inventory.md scripts/patches/ scripts/configure/60-buildarr-patches.sh \
           docs/buildarr-v4-patch-session-prompt.md docs/buildarr-v4-patch-session-runbook.md \
           scripts/maint/rollback-buildarr-patches.sh
   git commit  # write your own message
   ```
3. Watch the next Monday 04:30 CEST timer fire — it should land Result=success in the live Kuma monitor.

## How to know upstream finally caught up (months from now)

Periodically:
1. Check `pip index versions buildarr-sonarr` and `pip index versions buildarr-radarr` for new releases.
2. When you see new versions, in a test venv: `pip install -U buildarr-sonarr buildarr-radarr`, then try the same config against the live *arrs.
3. If it works without applying `scripts/configure/60-buildarr-patches.sh`, you can retire the whole compatibility layer:
   ```bash
   ssh quadstronaut@seedbox.example.com 'rm ~/scripts/maint/rollback-buildarr-patches.sh'
   cd P:\Documents\GIT\QFlix
   rm -rf scripts/patches/ scripts/configure/60-buildarr-patches.sh scripts/maint/rollback-buildarr-patches.sh
   # Update inventory.md to remove the patch references
   # Update memory project_buildarr-upstream-broken.md to "retired YYYY-MM-DD"
   git commit -am "buildarr: retire local v4/v5 patches — upstream now native"
   ```
