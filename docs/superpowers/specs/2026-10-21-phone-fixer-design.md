# Phone fixer: fix QFlix from the phone with the workstation off (QFLX-70)

## Why
Fixing QFlix meant: Discord alert → walk to the workstation → terminal → paste.
All knowledge, keys and memory lived on that one machine. The operator wants it
to work with the workstation **powered off** (e.g. mid-move, before home
internet exists), from the phone over cellular.

## Decided (operator, 2026-10-21)
- **Executor = Claude Code cloud session** (claude.ai/code). Same subscription
  usage as the CLI; Claude Code on Android/Termux is unsupported, so running
  the CLI on the phone saves nothing and is fragile.
- **Phone = QFlix Admin app, new "Fix" page**: prompt box → `POST` routine
  `/fire` → returns `claude.ai/code/...` session URL → open + steer it in the
  Claude app. History of the last 20 sessions for check-ins.
- **Steering baked in**: the routine's saved prompt makes every session read
  `qflix-brain/_steering/STEERING.md` first: concise caveman voice, hard guards,
  memory use, ship path.
- **Memory = private repo `qflix-brain`** (the workstation's QFlix memory dir is
  now a clone of it). Cloud sessions read and write it; one brain.
- **Full autonomy**: branch → PR → green `pytest`/`audit`/`pwsh` → REST merge →
  deploy → verify `deploy-drift`. Guards kept: Monday 11:00–15:00 UTC window,
  never commit master, member privacy. Stop and ask on data loss, member-facing
  or money/entitlement changes, or CI red after 3 tries.
- Secrets for QFlix may be stored with Anthropic/GitHub.

## Box access (forced by the spike)
Spike 2026-10-21 from a cloud session on Full network: TCP :22 times out; the
egress proxy answers `CONNECT` for any port but only carries TLS, which it
intercepts; no ssh client is installable; and the session's safeguards flagged
further SSH-tunnel attempts as a containment escape. So the sandbox never SSHes.

Instead the hand is **GitHub Actions in private `qflix-brain`**:
`bin/boxrun '<bash>'` dispatches the `box` workflow over the GitHub REST API
(the only egress a cloud session needs), the runner SSHes the box with a
dedicated `qflix-cloud-session` ed25519 key (Actions secret, revocable alone,
host key pinned), and output returns in the run log. `--runner` mode runs inside
a QFlix checkout with `secrets/` pulled from the box mirror, so workstation-run
installers work unchanged. Private repo → box output never reaches a public log.
The workflow itself refuses during the Monday window and serializes runs (no
ssh fan-out → no fail2ban).

## Parts
| Part | Where |
|---|---|
| Fix page (Compose), `RoutineFire` wire contract, `FixViewModel` + history | `apps/heartbeat-android/app/src/main/java/com/qflix/heartbeat/{net,ui}/` |
| Phone credential push | `apps/heartbeat-android/provision-fixer.ps1` (token stays in `~/.qflix-cloud-key/routine.json`, never the repo) |
| Routine "QFlix Fix (phone)", API trigger, repos QFlix + qflix-brain, env `qflix-fixer` (Full network) | claude.ai/code/routines |
| Steering, memory, `box` workflow, `bin/boxrun` | private `qflix-brain` |

## Limits / follow-ups
- Routine `/fire` is research preview (beta header pinned in `RoutineFire.BETA`); 30 fires/h/routine.
- Mobile app can't pick Bypass permissions; routine runs are autonomous anyway.
- Workstation ↔ brain sync is git in the memory dir; keep it pulled before sessions.
