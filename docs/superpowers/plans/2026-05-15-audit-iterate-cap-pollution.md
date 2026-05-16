# Audit & Iterate — Cap-Counter Self-Trap + Vlogs Placeholder Pollution

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans
> to execute this plan inline. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Verify the 2026-05-15 audit-fix commit (`68f4bdf`) holds end-to-end,
then close the two residual issues this audit surfaced — (A) journalctl
"-- No entries --" placeholder polluting vlogs as level=unknown noise, and
(B) refused-cap-hit events counting toward the daily action cap so the
cap-trap self-reinforces — and drive each to 3 consecutive clean cycles.

**Architecture:** Two surgical fixes, both line-counted regression-safe:

1. `scripts/mcp/logs.py::_journalctl()` — drop the literal `-- No entries --`
   line from journalctl's stdout before returning. Single comprehension.
2. `scripts/mcp/unstick.py::_count_today()` — read each JSONL line, parse it,
   and only count entries whose `result` is in the set of statuses that
   actually consumed an *arr or qBit slot.
3. `scripts/local/qflix-collect.ps1::Count-TodaysActions` — apply the same
   filter on the workstation side (the workstation also has its own cap
   gate that uses a raw line count).

Both fixes preserve the audit-trail value of writing refusals to the events
file — the change is solely in how the cap-counter interprets those lines.

Validation = self-test offline + 3 consecutive E2E cycles per problem with
zero errors. Each cycle = (i) ingest timer fires on seedbox, (ii) MCP
queries verify no `-- No entries --` rows added since last check, (iii)
anime canary runs green, (iv) workstation collect runs cleanly.

**Tech Stack:** Python 3 (stdlib only), Bash, PowerShell 5.1, systemd-user
timers, VictoriaLogs (LogsQL), Uptime Kuma push monitors.

**Validated facts from audit:**
- `logs.py --self-test` passes 14/14 offline (commit 68f4bdf).
- `qflix_status` reports `kuma_red: []` — anime canary, vlogs-stall, all
  workstation monitors green right now.
- Workstation `last_collect` is `exit_code: 0` at 2026-05-16T02:00:01Z — the
  hourly task auto-resumed after the user's snapshot window.
- Live vlogs queries confirm bazarr (pipe-padded), maintainerr (DD/MM/YYYY),
  recyclarr (`[INF]`/`[WRN]`), prowlarr (.NET arr-pipe) now ship with
  `level` populated correctly.
- `~/scripts/mcp/events/2026-05-15.jsonl` is 32 lines (cap = 10), with 22+
  of those being `refused-cap-hit` retries of the same orphan hash —
  confirming the cap-counter self-trap empirically.

---

## File Structure

- Modify: `scripts/mcp/logs.py` (~5 lines added in `_journalctl`, plus 1 self-test case)
- Modify: `scripts/mcp/unstick.py` (~10 lines: extract `_EFFECTIVE_STATUSES`, rewrite `_count_today`)
- Modify: `scripts/local/qflix-collect.ps1` (~5 lines in `Count-TodaysActions`)
- Create: `tests/unit/test_logs_journalctl_filter.py` (pytest, no SSH)
- Create: `tests/unit/test_unstick_cap_counter.py` (pytest, no SSH)
- Touch: deploy via `scripts/configure/70-mcp-install.sh` (no edit needed)

---

### Task 1: Add failing test for journalctl placeholder filter

**Files:**
- Create: `tests/unit/test_logs_journalctl_filter.py`

- [ ] **Step 1: Write the failing test**

```python
"""Verify logs.py drops journalctl's '-- No entries --' placeholder."""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "mcp"))

import logs  # noqa: E402


def test_journalctl_drops_no_entries_placeholder():
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "-- No entries --\n"
    with patch.object(logs.subprocess, "run", return_value=fake):
        out = logs._journalctl("anything.service", "5m", 100)
    assert out == [], f"expected empty list, got {out!r}"


def test_journalctl_keeps_real_lines():
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = (
        "2026-05-15T10:11:12+0000 seedbox foo: started\n"
        "-- No entries --\n"
        "2026-05-15T10:11:13+0000 seedbox foo: working\n"
    )
    with patch.object(logs.subprocess, "run", return_value=fake):
        out = logs._journalctl("foo.service", "5m", 100)
    assert len(out) == 2
    assert "started" in out[0]
    assert "working" in out[1]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_logs_journalctl_filter.py -v`
Expected: `test_journalctl_drops_no_entries_placeholder` FAILS (list contains the placeholder).

- [ ] **Step 3: Implement the filter in logs.py**

In `scripts/mcp/logs.py`, modify `_journalctl()` (around line 186):

```python
def _journalctl(unit: str, since: str, n: int) -> list[str]:
    cmd = ["journalctl", "--user", "-u", unit, "--since", f"{since} ago",
           "-n", str(n), "--output", "short-iso", "--no-pager"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return []
    if proc.returncode != 0:
        return []
    # journalctl prints "-- No entries --" when the window is empty. That
    # line has no semantic value and pollutes the vlogs index with
    # level=unknown noise — drop it before any downstream parse.
    return [ln for ln in proc.stdout.splitlines() if ln.strip() != "-- No entries --"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_logs_journalctl_filter.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Run the full self-test to confirm no regression**

Run: `python scripts/mcp/logs.py --self-test`
Expected: `PASS: 14/14 cases`.

---

### Task 2: Add failing test for cap-counter ignoring refusals

**Files:**
- Create: `tests/unit/test_unstick_cap_counter.py`

- [ ] **Step 1: Write the failing test**

```python
"""Verify _count_today only counts effective actions, not refusals."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "mcp"))
sys.path.insert(0, str(ROOT / "scripts" / "maint"))

import unstick  # noqa: E402


def _write_events(events_dir: Path, lines: list[dict]) -> None:
    import datetime as dt
    f = events_dir / f"{dt.date.today().isoformat()}.jsonl"
    f.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def test_refusals_do_not_count(tmp_path, monkeypatch):
    monkeypatch.setattr(unstick, "EVENTS_DIR", tmp_path)
    _write_events(tmp_path, [
        {"result": "refused-cap-hit"},
        {"result": "refused-arr-red"},
        {"result": "refused-unknown-slug"},
    ])
    assert unstick._count_today() == 0


def test_effective_actions_count(tmp_path, monkeypatch):
    monkeypatch.setattr(unstick, "EVENTS_DIR", tmp_path)
    _write_events(tmp_path, [
        {"result": "deleted+blocklisted"},
        {"result": "qbit-orphan-removed"},
        {"result": "refused-cap-hit"},
        {"result": "deleted+blocklisted"},
    ])
    assert unstick._count_today() == 3


def test_malformed_lines_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(unstick, "EVENTS_DIR", tmp_path)
    f = tmp_path / "fake.jsonl"
    # Use today's filename
    import datetime as dt
    f = tmp_path / f"{dt.date.today().isoformat()}.jsonl"
    f.write_text("not json\n{\"result\":\"deleted+blocklisted\"}\n\n")
    assert unstick._count_today() == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_unstick_cap_counter.py -v`
Expected: `test_refusals_do_not_count` FAILS (returns 3); `test_effective_actions_count` FAILS (returns 4).

- [ ] **Step 3: Implement the fix in unstick.py**

In `scripts/mcp/unstick.py`, after the `ARR_VERSIONS` dict (around line 46), add:

```python
# Statuses that actually consumed an *arr/qBit action slot. The daily cap
# only counts these — refusals (cap-hit, arr-red, unknown-slug) are still
# recorded for audit but don't gate the next attempt. Otherwise a single
# refusal that fires every hour would self-trap the counter forever once
# the cap was hit, since each refusal append grows the events file.
_EFFECTIVE_STATUSES = frozenset({
    "deleted+blocklisted",
    "qbit-orphan-removed",
})
```

Replace `_count_today()` (around line 52):

```python
def _count_today() -> int:
    p = _today_events_path()
    if not p.exists():
        return 0
    count = 0
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("result") in _EFFECTIVE_STATUSES:
            count += 1
    return count
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_unstick_cap_counter.py -v`
Expected: all 3 tests PASS.

---

### Task 3: Apply the same filter to the workstation cap counter

**Files:**
- Modify: `scripts/local/qflix-collect.ps1:305-310`

- [ ] **Step 1: Replace `Count-TodaysActions`**

Change the function to filter by `result` field. Original counts raw lines:

```powershell
function Count-TodaysActions {
    $today = [DateTime]::UtcNow.ToString("yyyy-MM-dd")
    $f = Join-Path $DataRoot "events\$today.jsonl"
    if (-not (Test-Path $f)) { return 0 }
    return (Get-Content $f | Where-Object { $_.Trim() -ne "" }).Count
}
```

Replace with:

```powershell
function Count-TodaysActions {
    # Only effective actions consume a slot. Refusals are kept in the file
    # for the audit trail but must not gate the next attempt — otherwise
    # an orphan that fires hourly self-traps the cap (refusal → file grows
    # → still over cap → refusal again, forever).
    $today = [DateTime]::UtcNow.ToString("yyyy-MM-dd")
    $f = Join-Path $DataRoot "events\$today.jsonl"
    if (-not (Test-Path $f)) { return 0 }
    $effective = @('deleted+blocklisted', 'qbit-orphan-removed')
    $count = 0
    Get-Content $f | ForEach-Object {
        if ([string]::IsNullOrWhiteSpace($_)) { return }
        try {
            $ev = $_ | ConvertFrom-Json
            if ($effective -contains $ev.result) { $count++ }
        } catch { }
    }
    return $count
}
```

- [ ] **Step 2: Smoke-test the function locally**

Run (PowerShell):
```powershell
$today = [DateTime]::UtcNow.ToString("yyyy-MM-dd")
$f = "B:\QFlix\data\events\$today.jsonl"
if (Test-Path $f) { Get-Content $f | Where-Object { $_.Trim() -ne "" } | Measure-Object | Select-Object Count }
```
Sanity check the old vs new count differential.

---

### Task 4: Commit the code change on a branch and push

- [ ] **Step 1: Branch from master**

Run:
```bash
git checkout -b audit-cap-counter-and-vlogs-placeholder
```

- [ ] **Step 2: Stage and commit specifically**

```bash
git add scripts/mcp/logs.py scripts/mcp/unstick.py scripts/local/qflix-collect.ps1 \
        tests/unit/test_logs_journalctl_filter.py tests/unit/test_unstick_cap_counter.py
git status  # verify no unintended files
```

Then commit (HEREDOC for body):
```bash
git commit -m "$(cat <<'EOF'
fix(audit): cap-counter ignores refusals; drop journalctl placeholder

unstick.py + qflix-collect.ps1: only count results that actually consumed an
*arr/qBit slot (deleted+blocklisted, qbit-orphan-removed) toward the daily
cap. Refusals are still recorded for the audit trail but no longer gate the
next attempt. Verified empirically: ~/scripts/mcp/events/2026-05-15.jsonl
was 32 lines with cap=10, the surplus being refused-cap-hit retries of one
orphan hash that self-trapped the counter once the real cap was hit.

logs.py: drop journalctl's literal "-- No entries --" placeholder before
return. Six systemd-routed apps (maint-window, maint-pusher, qbittorrent,
tdarr-{node,server}, listmonk) were shipping one of these per ingest cycle
to vlogs as level=unknown noise.

Tests: pytest coverage for both — _journalctl filter (drop placeholder,
keep real lines) and _count_today (refusals don't count, effective do,
malformed lines ignored).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 3: Push the branch**

```bash
git push -u origin audit-cap-counter-and-vlogs-placeholder
```

---

### Task 5: Deploy fix to seedbox

- [ ] **Step 1: Run the MCP install script**

```bash
bash scripts/configure/70-mcp-install.sh
```

Expected: prints `OK: scripts/mcp/ deployed; qflix-missing-search.timer enabled.`

- [ ] **Step 2: Verify deployed files match local**

```bash
ssh -o BatchMode=yes "quadstronaut@$(cat secrets/seedbox.ssh-host)" \
  "md5sum ~/scripts/mcp/logs.py ~/scripts/mcp/unstick.py"
```

Compare to:
```bash
md5sum scripts/mcp/logs.py scripts/mcp/unstick.py
```

Expected: matching digests.

- [ ] **Step 3: Run logs.py --self-test on the seedbox**

```bash
ssh -o BatchMode=yes "quadstronaut@$(cat secrets/seedbox.ssh-host)" \
  "python3 ~/scripts/mcp/logs.py --self-test"
```

Expected: `PASS: 14/14 cases` (deployed code is functional in target Python).

---

### Task 6: 3-iteration E2E gauntlet — round 1

- [ ] **Step 1: Force one immediate ingest cycle on the seedbox**

```bash
ssh -o BatchMode=yes "quadstronaut@$(cat secrets/seedbox.ssh-host)" \
  "systemctl --user start qflix-vlogs-ingest.service && systemctl --user status qflix-vlogs-ingest.service --no-pager -l | head -20"
```

Expected: exit 0, "Deactivated successfully" — no failures in the summary line.

- [ ] **Step 2: Query for new "-- No entries --" rows in the last 10 minutes**

Use the MCP `qflix_query_logs` tool:
- Query: `_msg:"-- No entries --"`
- Start: `10m`
- Limit: `5`

Expected: `count: 0` (no new placeholder rows since the fix deployed).

- [ ] **Step 3: Run the anime canary against the seedbox**

```bash
ssh -o BatchMode=yes "quadstronaut@$(cat secrets/seedbox.ssh-host)" \
  "systemctl --user start manitoba-maint-canary-anime.service && \
   journalctl --user -u manitoba-maint-canary-anime.service -n 5 --no-pager"
```

Expected: last line contains `PASS: anime canary` (either the 202 soft-pass
or the normal push-verified path is acceptable).

- [ ] **Step 4: Check qflix_status reports kuma_red empty**

Use MCP `qflix_status`. Expected: `"kuma_red": []`.

- [ ] **Step 5: Record round 1 results**

If all four checks pass, increment the cycle counter to 1. If any fail,
diagnose, fix, and reset to 0.

---

### Task 7: 3-iteration E2E gauntlet — round 2

- [ ] **Step 1: Wait for or trigger the next ingest cycle**

```bash
ssh -o BatchMode=yes "quadstronaut@$(cat secrets/seedbox.ssh-host)" \
  "systemctl --user start qflix-vlogs-ingest.service"
```

- [ ] **Step 2: Re-query for placeholder pollution**

Same query as Task 6 Step 2. Expected: `count: 0`.

- [ ] **Step 3: Re-run the anime canary**

Same as Task 6 Step 3. Expected: PASS.

- [ ] **Step 4: Re-verify kuma_red empty**

Same as Task 6 Step 4. Expected: `[]`.

- [ ] **Step 5: Record round 2**

Counter → 2. If any fail, diagnose + fix + reset.

---

### Task 8: 3-iteration E2E gauntlet — round 3

- [ ] **Step 1: Third trigger**

Same as Task 7 Step 1.

- [ ] **Step 2: Third placeholder check**

Same as Task 6 Step 2. Expected: `count: 0`.

- [ ] **Step 3: Third anime canary**

Same as Task 6 Step 3. Expected: PASS.

- [ ] **Step 4: Third status check**

Same as Task 6 Step 4. Expected: `kuma_red: []`.

- [ ] **Step 5: Record round 3 — gauntlet complete**

If 3 of 3 rounds passed, the fix is validated. Move on to PR.

---

### Task 9: Open PR against master

- [ ] **Step 1: Push (if not already)**

```bash
git push -u origin audit-cap-counter-and-vlogs-placeholder
```

- [ ] **Step 2: Open PR**

```bash
gh pr create --title "fix(audit): cap-counter ignores refusals; drop journalctl placeholder" --body "$(cat <<'EOF'
## Summary
- `_count_today()` (both unstick.py and qflix-collect.ps1) now only counts results that actually consumed a slot — `deleted+blocklisted`, `qbit-orphan-removed`. Refusals still get recorded for audit but don't gate the next attempt.
- `logs.py::_journalctl()` drops journalctl's literal `-- No entries --` placeholder so it stops landing in vlogs as `level=unknown` noise.

## Why
Audit of the live system after merge of #11 (`68f4bdf`) found:
- `~/scripts/mcp/events/2026-05-15.jsonl` had 32 lines vs cap of 10 — surplus were refused-cap-hit retries of one orphan hash, self-trapping the counter.
- Six systemd-routed apps were shipping `{level:"unknown", _msg:"-- No entries --"}` to vlogs every 5 min.

## Test plan
- [x] `python -m pytest tests/unit/test_logs_journalctl_filter.py tests/unit/test_unstick_cap_counter.py -v` (5 tests pass)
- [x] `python scripts/mcp/logs.py --self-test` (14/14 pass — no regex regression)
- [x] Deploy via `scripts/configure/70-mcp-install.sh`, verify md5 match
- [x] 3 consecutive E2E rounds: ingest cycle + anime canary + status all green, no new `-- No entries --` rows
EOF
)"
```

- [ ] **Step 3: Capture PR URL and report to user**

---

## Acceptance

- All unit tests pass.
- `logs.py --self-test` reports 14/14.
- After deploy, three consecutive ingest cycles each produce zero new
  `_msg:"-- No entries --"` rows in vlogs.
- Anime canary green across all three rounds.
- `qflix_status` reports `kuma_red: []` across all three rounds.
- PR opened with passing checks.
