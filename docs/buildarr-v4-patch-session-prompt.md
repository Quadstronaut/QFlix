# Autonomous-patching session prompt — buildarr v4/v5 compatibility

Paste the fenced block below as the **first message** of a fresh Claude Code session opened at `<repo-root>`. Make sure every tool is enabled (Bash, Read/Edit/Write, Glob/Grep, WebFetch/WebSearch, TaskCreate/Update, Agent).

See [`buildarr-v4-patch-session-runbook.md`](buildarr-v4-patch-session-runbook.md) for: pre-flight checks, how to interrupt mid-session, how to roll back, and what to watch in real time.

---

```
# Buildarr v4/v5 compatibility — autonomous patching session

You are working on QFlix at `<repo-root>` (Windows, PowerShell + bash via the Bash tool). Live seedbox is `quadstronaut@seedbox.example.com`, SSH key already configured (use `ssh quadstronaut@seedbox.example.com '<cmd>'`).

The endpoint of this session is: **buildarr.service runs to clean Result=success against a populated config managing all 4 *arr instances (sonarr/sonarr2/radarr/radarr2), with our patches captured as committed artifacts so a future `pip install -U buildarr-sonarr buildarr-radarr` cleanly retires them when upstream catches up.** Buildarr is a Python application (https://buildarr.github.io/installation/python/) — full source visibility, monkey-patches are tractable, no Docker rebuild needed.

## Background — what's already done (DO NOT redo)

The 2026-05-11 audit established these as ground truth. Read `inventory.md` (top + the "2026-05-11 — audit sweep findings + fixes" section at bottom) and `manifest/apps.yaml` lines ~320 (the `buildarr:` entry) before doing anything else.

- **Plugin landscape**: venv at `~/.apps/buildarr/.venv/`. Installed: `buildarr 0.7.1`, `buildarr-prowlarr 0.5.3`, `buildarr-radarr 0.2.6`, `buildarr-sonarr 0.6.4` (all at latest PyPI; `buildarr-jellyseerr` was removed as misleading — Seerr is config-managed by `scripts/configure/30-seerr-arrs.py` and there's no upstream `buildarr-seerr` package).
- **Running *arr versions**: Sonarr v4.0.17.2952, Radarr v5. The plugins were written for Sonarr v3 / Radarr v3 schemas. **Every section's `from_remote` fetch hits some API drift.**
- **Live patches already applied in the venv** (with `# QFlix patch 2026-05-11` markers; backups exist as `*.bak`):
  1. `buildarr_sonarr/config/profiles/release.py` — `preferred` remote-map entry has `"optional": True` (Sonarr v4 dropped the preferred field, replaced by Custom Formats).
  2. Same file — `include_preferred_when_renaming` remote-map entry has `"optional": True` (also dropped in v4).
  3. `radarr/models/colon_replacement_format.py` — added `SMART = 'smart'` to the `ColonReplacementFormat` enum (Radarr v5 added it).
- **Known next bug** (was not yet patched when the prior session paused): `buildarr_sonarr/config/import_lists.py:1225` — `KeyError: 'languageProfileId'`. Sonarr v4 removed language profiles entirely (merged into quality). There are 16+ references to `languageProfileId` / `language_profile_id` across that file. Will need a broader patch — likely "make the whole language-profile fetch + map optional / no-op when remote lacks it".
- **Current safe state**: `~/.apps/buildarr/buildarr.yml` is the **original commented-out template** (no instance blocks declared, so buildarr.service exits with `RunNoPluginsDefinedError` after a fresh start). `systemctl --user reset-failed` was run; ActiveState=inactive, is-failed=false. The manifest entry at `manifest/apps.yaml` is on the **legacy `systemd_only` probe against `buildarr.timer`** (preserves silent-failure behavior — the timer is enabled so the probe stays green regardless of what the .service does). **This was verified empirically before launching you**: a deliberate buildarr.service failure left the Kuma "Buildarr" monitor at status=1/msg=active through a full pusher cycle. **You can iterate without triggering Kuma/Discord auto-heal notifications.** Verify continuously: `sqlite3 ~/.apps/uptimekuma/kuma.db "SELECT name,status,msg FROM heartbeat h JOIN monitor m ON h.monitor_id=m.id WHERE h.id=(SELECT MAX(id) FROM heartbeat WHERE monitor_id=m.id) AND name='Buildarr';"` should show status=1 throughout your work. If it goes to 0, **stop immediately and run `~/scripts/maint/rollback-buildarr-patches.sh`**.
- **Secrets**: API keys + ports + urlbases live in `~/secrets/{sonarr,sonarr2,radarr,radarr2}.{key,port,urlbase}` on the seedbox. All four return HTTP 200 from their `/api/v3/system/status` endpoints.
- **Pre-flight snapshot**: `~/.purged-2026-05-11/buildarr-pre-autonomous-fix.tar.gz` (9.1 KB) holds `buildarr.yml` + the 2 venv-patched files + their `.bak` originals + the deployed manifest, all as of 06:51 CEST. `~/.purged-2026-05-11/buildarr-pip-freeze-pre-autonomous.txt` captures the exact dependency set. **The rollback script `scripts/maint/rollback-buildarr-patches.sh` (also synced to `~/scripts/maint/`) restores all of it idempotently.**
- **Tests**: 243 unit tests in `tests/unit/`; run with `tests/.venv/Scripts/python.exe -m pytest tests/unit/ -q` from repo root. Must still pass at the end.

## Iteration loop (the core work)

1. **Populate `~/.apps/buildarr/buildarr.yml`** with all 4 instances using real keys from `~/secrets/`. Heredoc with `${SONARR_KEY}` etc. expanded. `chmod 600`. The 4 instances:
   - sonarr (main): hostname=127.0.0.1, port=17026, url_base=sonarr
   - sonarr (anime): hostname=127.0.0.1, port=17003, url_base=sonarr2
   - radarr (main): hostname=127.0.0.1, port=17027, url_base=radarr
   - radarr (anime): hostname=127.0.0.1, port=17008, url_base=radarr2
2. **Trigger a run**: `systemctl --user reset-failed buildarr.service && systemctl --user start --wait buildarr.service && echo OK || echo FAIL`. The `--wait` blocks until the run terminates, so `systemctl --user show buildarr.service -p Result -p ExecMainStatus` immediately reflects the outcome.
3. **If `Result=success`**: skip to "Wrap-up" below. DONE.
4. **If failed**: tail `~/.apps/buildarr/logs/buildarr.err` and `journalctl --user -u buildarr.service --since '5 minutes ago' --no-pager`. Identify the failing site (file + line + traceback).
5. **Diagnose**: read the actual *arr API response that's causing the mismatch. Sonarr/Radarr v4/v5 endpoints to probe directly with curl + the API key:
   - `GET /api/v3/releaseprofile`
   - `GET /api/v3/importlist`
   - `GET /api/v3/qualityprofile`
   - `GET /api/v3/customformat`
   - `GET /api/v3/indexer`
   - `GET /api/v3/downloadclient`
   - `GET /api/v3/config/naming`, `/api/v3/config/mediamanagement`, `/api/v3/config/host`, `/api/v3/config/ui`
   - `GET /api/v3/notification`
   - `GET /api/v3/rootfolder`
   - `GET /api/v3/tag`
   - Plus `GET /api/v3/system/status` for the appName/version.
   Compare to what the buildarr plugin source expects (`grep -n` the offending key in `~/.apps/buildarr/.venv/lib/python3.11/site-packages/buildarr_sonarr/` or `buildarr_radarr/`).
6. **Patch minimally**. Prefer marking remote-map entries `"optional": True` (buildarr core supports this — see `~/.apps/buildarr/.venv/lib/python3.11/site-packages/buildarr/config/base.py` around line 234) over editing pydantic models. For removed-entire-feature cases (like language profiles), make the `from_remote` `if any(... for ... in ...)` guards tolerant — e.g. wrap the `importlist["languageProfileId"]` access in `importlist.get("languageProfileId", 0)`. Mark every edit with a `# QFlix patch 2026-05-11` comment so we can grep them out later. Always copy the file to `*.bak` (timestamped, idempotent — skip if .bak already exists) before mutating.
7. **Bust bytecode cache**: `find ~/.apps/buildarr/.venv/lib/python3.11/site-packages/buildarr_sonarr ~/.apps/buildarr/.venv/lib/python3.11/site-packages/buildarr_radarr ~/.apps/buildarr/.venv/lib/python3.11/site-packages/radarr -name '*.pyc' -delete`.
8. **Update your TaskCreate punch list** with the patch you just landed (filename + one-line description). **Loop back to step 2.**

Expected bug surface (heuristic — actual list determined by iteration): preferred (✓ done), includePreferredWhenRenaming (✓ done), colonReplacementFormat=smart (✓ done), languageProfileId (next), root folders structure, custom-format vs preferred-words, indexer fields (some renamed), notification fields, maybe rootFolderPath shape. Budget ~5-10 more patches.

## Hard stops

**Abort the iteration loop and report — do NOT try to push through — if any of these triggers fire:**

1. Distinct API drifts patched in `buildarr-sonarr` exceeds **15**. (Same for `buildarr-radarr`.)
2. Total loop iterations exceeds **25**. You're going in circles.
3. Kuma "Buildarr" monitor status drops from 1 to 0 at any sqlite check.
4. Anything outside `~/.apps/buildarr/`, `manifest/apps.yaml`, `scripts/patches/`, `scripts/configure/`, `inventory.md`, `MEMORY.md`-tracked files, or `tests/unit/` gets modified.
5. A patch you applied has to be unwound twice. (Means the diagnosis was wrong; reset and re-diagnose from scratch.)
6. Anyone (the operator) sends a message saying "stop" / "abort" / "pause". Immediately stop, restore the most recent safe state, report.

If a hard stop fires: invoke `~/scripts/maint/rollback-buildarr-patches.sh` on the seedbox, report what was learned, and exit cleanly.

## Safety rails (continuous, not just on hard stops)

- **Do not modify the buildarr Kuma monitor or the manifest's `buildarr:` entry during iteration.** It's on the legacy `systemd_only` probe against `buildarr.timer` precisely so failures stay silent.
- **Do not stop or restart `manitoba-maint-pusher.service`** during iteration. It's not in your way.
- **Never use `--force` or `--no-verify` on git commands. Never commit unless the operator explicitly asks.**
- **Never embed API keys into committed files.** The buildarr.yml on the seedbox is never committed; only a redacted template (with `${SONARR_KEY}` placeholders) goes into repo artifacts.
- **Never touch files outside the allowed list in hard-stop #4.**
- The operator hates being paged for things that aren't real fires. Iteration is silent. Keep it that way until the very last "verify end-to-end" step.

## Wrap-up (once Result=success)

Order matters here.

1. **Capture patches as committed artifacts.** For each patched file, generate a unified diff from the `.bak`: `diff -u "$file.bak" "$file" > scripts/patches/buildarr-<file-slug>.patch`. SCP the patches off the seedbox into the repo at `scripts/patches/`. Sanity-check each patch with `patch --dry-run`.
2. **Write `scripts/configure/60-buildarr-patches.sh`** — idempotent: detects the venv's site-packages dir, checks if patches are already applied (grep for `# QFlix patch 2026-05-11`), applies them if not. Match the style of existing `scripts/configure/*.sh` (e.g. `30-seerr-arrs.py`, `50-tautulli-pms-url-fix.sh`). Add a comment block at the top explaining: "Remove this file when buildarr-sonarr >= X.Y.Z and buildarr-radarr >= X.Y.Z land Sonarr v4 / Radarr v5 native support — at that point `pip install -U buildarr-sonarr buildarr-radarr` retires the patches cleanly."
3. **Restore the populated `buildarr.yml` on the seedbox** (same heredoc pattern as iteration step 1) and run buildarr.service one more time end-to-end. Confirm Result=success in `systemctl --user show`.
4. **Flip the manifest entry** at `manifest/apps.yaml` (the `buildarr:` block ~line 320): change `unit: buildarr.timer` → `unit: buildarr.service`, `kind: systemd_only` → `kind: systemd_oneshot`, drop the `expect: active` line. Replace the "PARKED PENDING OPERATOR DECISION" comment block with a forward-looking note that points at `scripts/configure/60-buildarr-patches.sh` and the upgrade-path-deletion plan.
5. **Deploy the updated manifest**: `scp manifest/apps.yaml quadstronaut@seedbox.example.com:~/.opt/maint/apps.yaml` then `ssh ... 'systemctl --user restart manitoba-maint-pusher.service && sleep 12'`. Verify the Kuma "Buildarr" monitor pushes `msg=success` after restart (same sqlite query as in the Background section).
6. **Update `inventory.md`** — find the "2026-05-11 — audit sweep findings + fixes" section, move the buildarr bullet from "Not resolved" into the resolved list, document the patch count and where the patches live, and note the upgrade-path deletion plan.
7. **Update memory** at `C:\Users\Quadstronaut\.claude\projects\P--Documents-GIT-QFlix\memory\project_buildarr-upstream-broken.md` — change the description + body to reflect "patched, working, awaits upstream catchup". Update `MEMORY.md` index hook line. (Don't delete the memory; keep it as historical context for when the patches are eventually retired.)
8. **Run the full unit test suite**: `tests/.venv/Scripts/python.exe -m pytest tests/unit/ -q`. Must show ≥243 passing. If anything regressed, fix before reporting done.
9. **Stage and report** — git status + git diff summary. Do NOT commit unless the operator explicitly asks in a follow-up. Report a punch-list of files changed, patches applied (count + brief description of each), and what to verify next Monday (the next real timer fire at 04:30 CEST).

## Operating discipline

- Use `TaskCreate` / `TaskUpdate` to maintain a live punch list of bugs hit and patches applied. Mark in-progress when starting, completed immediately when done. Keep the list visible at all times.
- Batch independent tool calls in single messages — never two sequential calls with no dependency.
- For long-running buildarr.service invocations, prefer foreground Bash with appropriate timeout (the run takes ~14-30s normally; bound at 120s).
- When patching, ALWAYS show the exact `old_string` you're replacing and the `new_string` going in. No regex-magic without verification.
- One-sentence updates while working. End-of-turn summary should be tight: what's left, what's next.
- If you discover a patch you applied was wrong (overly broad, breaks something else), restore from the `.bak` first, then re-patch correctly. Never layer patches on top of broken patches.

## Done means

A future operator can run `app-buildarr update` (or equivalent reinstall) without breaking anything, because `scripts/configure/60-buildarr-patches.sh` re-applies the patches. They can also `pip install -U buildarr-sonarr buildarr-radarr` to test upstream catchup, run buildarr against current *arrs, and if it works without patches, delete the patches directory + configure script + this whole compatibility layer in one PR.
```
