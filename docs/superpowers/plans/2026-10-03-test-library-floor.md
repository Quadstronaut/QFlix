# QFLX-4 Test Library on the Entitlement Floor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pre-seed one new Patreon household in the box roster, and add a static `QFlix - Test` Plex library that only unentitled members (plus the owner) can see.

**Architecture:** `lib/plexshare.py` generalizes the access "floor" from Welcome alone to Welcome (required) + optional extra floor titles; `qflix-entitlement.py` passes `("QFlix - Test",)` by default and annotates its Kuma summary when an extra floor library is absent. The library is created by the existing `59b` script with new `--agent/--scanner` flags (Plex Personal Media). Every other consumer is already blind to it except the poster janitor, which gets one exclusion.

**Tech Stack:** Python 3 stdlib, pytest, bash (`scripts/lib/ssh.sh` `sshm`/`scpm_to`/`scpm_from`), python-plexapi on the box venv.

**Spec:** `docs/superpowers/specs/2026-10-03-test-library-floor-design.md`

## Global Constraints

- Repo is PUBLIC: no member email, name, or household id in any tracked file, commit message, or PR. Use `$NEW_EMAIL` / `$NEW_ID` shell variables set at execution time from the operator conversation.
- Never commit to master. Branch `feature/QFLX-4-test-library` (worktree `../QFlix-testlib`), PR → green `pytest`/`audit`/`pwsh` → merge.
- Every commit subject starts with `QFLX-4 `.
- No box operations Monday 11:00–15:00 UTC (`lib/window.py`).
- Run pytest from the **Bash tool only** (PowerShell→WSL bash produces phantom failures): `bash tests/run.sh -q` (CI's command) or `python -m pytest <file> -q`.
- Run the full suite and `python scripts/maint/qflix-audit.py` **after `git add`** (audit boundary is the git index).
- Floor titles: Welcome = `"QFlix - Welcome"` (required), Test = `"QFlix - Test"` (optional). Folder `~/media/Test`. Agent `tv.plex.agents.none`, scanner `Plex Video Files` (verified live on PMS 1.43.3.10896, `/system/agents`, 2026-10-03).
- Gate code must be deployed to the box BEFORE the library is created.
- Session end: merged master == GitHub == box `~/scripts` (deploy-drift canary).

## Review Focus

1. **Welcome missing while Test exists** → must still raise (eviction rail); a Test-only floor must never be accepted as "minimum". Pinned in Task 2.
2. **Test renamed/absent** → floor silently shrinks to Welcome; operator must still see it in Kuma (`floor missing: QFlix - Test`). Pinned in Task 3.
3. **Entitled member holding content + Welcome + Test** (lapsed then returned) → target = content only, no short-catalogue alert (the self-sealing bug class from 2026-08-17). Pinned in Task 3.
4. **Server whose only sections are Welcome + Test** → `full_access_ids` must raise, not return `[]`. Pinned in Task 2.
5. **Title whitespace/case drift on Test** (`"  qflix - TEST "`) → still subtracted from full access, same leniency as Welcome. Pinned in Task 2.

---

### Task 1: Part A — pre-seed the household in the box roster (box only, no code)

**Files:** box `~/secrets/members.yaml`; workstation mirror `secrets/members.yaml` (gitignored). Nothing tracked changes.

**Interfaces:** Consumes `lib.members.load(Path) -> Roster`, `lib.members.gate_is_armed(Roster) -> (bool, str)`, `Roster.by_email() -> dict`.

- [ ] **Step 1: Window check + set variables (Bash tool, main repo dir)**

```bash
date -u   # must NOT be Monday 11:00-15:00 UTC
export NEW_EMAIL='<operator-supplied plex email>'
export NEW_ID='<operator-supplied id>'; export NEW_DISPLAY='<operator-supplied display>'
cd /g/Documents/GIT/Ultra.cc/QFlix && source scripts/lib/ssh.sh
```

- [ ] **Step 2: Backup + inspect layout (no PII printed)**

```bash
sshm 'cp -p ~/secrets/members.yaml ~/secrets/members.yaml.pre-QFLX-4-$(date -u +%Y%m%d) && grep -n "^[a-z_]*:" ~/secrets/members.yaml && grep -c "^  - id:" ~/secrets/members.yaml'
```

Expected: `households:` is the LAST top-level key; household count 18; rows start `  - id:` (2-space). If `households:` is not last, STOP and insert the block before the next top-level key instead of appending.

- [ ] **Step 3: Build candidate, validate with the real loader, then replace atomically**

```bash
sshm "set -e; C=\$(mktemp ~/secrets/.members.cand.XXXXXX); cp ~/secrets/members.yaml \$C
cat >> \$C <<EOF
  - id: $NEW_ID
    display: \"$NEW_DISPLAY\"
    exempt: false
    billing:
      holder: $NEW_EMAIL
      amount_usd: 50
      rail: patreon
      payer_ref: $NEW_EMAIL
    accounts:
      - $NEW_EMAIL
EOF
cd ~/scripts/maint && QFLIX_MEMBERS=\$C python3 -c '
import os,sys; sys.path.insert(0,\"lib\"); import members as M
r=M.load(M.find_roster()); ok,why=M.gate_is_armed(r)
print(\"households\",len(r.households),\"armed\",ok,why,\"new_listed\",\"$NEW_EMAIL\".lower() in r.by_email())
sys.exit(0 if ok and len(r.households)==19 else 1)' && mv -f \$C ~/secrets/members.yaml && chmod 600 ~/secrets/members.yaml"
```

Expected: `households 19 armed True armed new_listed True`, exit 0. On non-zero exit the live roster is untouched (candidate left at `~/secrets/.members.cand.*` — delete it).

- [ ] **Step 4: Report-only gate run — no new alerts**

```bash
sshm 'python3 ~/scripts/maint/qflix-entitlement.py --no-notify --no-kuma 2>&1 | tail -25'
```

Expected: same plan counts as before (entitled=3 exempt=4 unknown-payer=5 expired=1), no `unnamed`, no tripwire, no `roster failed validation`.

- [ ] **Step 5: Refresh workstation mirror**

```bash
scpm_from '~/secrets/members.yaml' secrets/members.yaml && git status --short secrets/ | wc -l   # expect 0 (gitignored)
```

---

### Task 2: plexshare floor set

**Files:**
- Modify: `scripts/maint/lib/plexshare.py:298-361` (`full_access_ids`, `minimum_access_ids`; add `missing_floor_titles`)
- Test: `tests/unit/test_welcome_section_is_exclusive.py` (append)

**Interfaces:**
- Produces:
  - `full_access_ids(sections: Sequence[Section], welcome_title: str, extra_floor_titles: Sequence[str] = ()) -> List[int]`
  - `minimum_access_ids(sections: Sequence[Section], welcome_title: str, extra_floor_titles: Sequence[str] = ()) -> List[int]` (sorted)
  - `missing_floor_titles(sections: Sequence[Section], extra_floor_titles: Sequence[str]) -> List[str]`

- [ ] **Step 1: Write the failing tests (append to `tests/unit/test_welcome_section_is_exclusive.py`)**

```python
# ---------------------------------------------------------------------------
# QFLX-4: the floor is a SET -- Welcome (required) + optional extras.
# `QFlix - Test` holds playback test clips for prospects; operator 2026-10-03:
# "for pre-subscription testing" -- unentitled see it, entitled never do.
# ---------------------------------------------------------------------------
TEST = "QFlix - Test"


def test_extra_floor_title_joins_the_minimum_set():
    secs = _sections("Movies", WELCOME, TEST)          # ids 100, 101, 102
    assert PS.minimum_access_ids(secs, WELCOME, (TEST,)) == [101, 102]


def test_extra_floor_title_is_absent_from_full_access():
    secs = _sections("Movies", "TV", WELCOME, TEST)
    assert PS.full_access_ids(secs, WELCOME, (TEST,)) == [100, 101]


def test_floor_set_and_full_access_stay_disjoint():
    secs = _sections("Movies", "TV", WELCOME, TEST, "Anime")
    full = set(PS.full_access_ids(secs, WELCOME, (TEST,)))
    floor = set(PS.minimum_access_ids(secs, WELCOME, (TEST,)))
    assert not (full & floor)


def test_missing_extra_floor_title_degrades_to_welcome_only():
    """Test is optional: absent means the floor is Welcome alone -- never a raise,
    because the anti-eviction rail is about Welcome, not about Test."""
    secs = _sections("Movies", WELCOME)
    assert PS.minimum_access_ids(secs, WELCOME, (TEST,)) == [101]
    assert PS.missing_floor_titles(secs, (TEST,)) == [TEST]


def test_missing_welcome_still_raises_even_when_test_exists():
    """A Test-only floor would hand prospects the clips but drop the
    go-subscribe video; worse, it hides that Welcome is gone. Refuse."""
    secs = _sections("Movies", TEST)
    with pytest.raises(PS.PlexShareError):
        PS.minimum_access_ids(secs, WELCOME, (TEST,))


def test_floor_only_server_raises_rather_than_evicting():
    secs = _sections(WELCOME, TEST)
    with pytest.raises(PS.PlexShareError):
        PS.full_access_ids(secs, WELCOME, (TEST,))


def test_extra_floor_match_is_case_and_whitespace_tolerant():
    secs = [PS.Section(id=100, key=1, title="Movies", type="movie"),
            PS.Section(id=101, key=2, title=WELCOME, type="movie"),
            PS.Section(id=102, key=3, title="  qflix - TEST ", type="movie")]
    assert PS.full_access_ids(secs, WELCOME, (TEST,)) == [100]
    assert PS.missing_floor_titles(secs, (TEST,)) == []


def test_default_extra_floor_is_empty_so_old_callers_are_unchanged():
    secs = _sections("Movies", WELCOME, TEST)
    assert PS.full_access_ids(secs, WELCOME) == [100, 102]
    assert PS.minimum_access_ids(secs, WELCOME) == [101]
```

- [ ] **Step 2: Run — expect failures**

Run: `python -m pytest tests/unit/test_welcome_section_is_exclusive.py -q`
Expected: new tests FAIL with `TypeError: ... takes 2 positional arguments but 3 were given` / `AttributeError: ... missing_floor_titles`.

- [ ] **Step 3: Implement in `scripts/maint/lib/plexshare.py`**

Replace the body of `full_access_ids` (keep its docstring, append the paragraph below to it) and `minimum_access_ids`; add `missing_floor_titles` after them:

```python
def full_access_ids(sections: Sequence[Section], welcome_title: str,
                    extra_floor_titles: Sequence[str] = ()) -> List[int]:
    # (existing docstring kept verbatim, plus:)
    # EXTRA FLOOR TITLES (QFLX-4, 2026-10-03)
    # `QFlix - Test` holds playback test clips for prospects. It belongs to the
    # not-entitled surface exactly like Welcome, so it is subtracted here for the
    # same reason: subtracting at the point FULL is computed means no caller can
    # re-add it by forgetting a filter.
    floor = {s.id for s in sections
             if find_section([s], welcome_title) is not None
             or any(find_section([s], t) is not None for t in extra_floor_titles)}
    ids = sorted(s.id for s in sections if s.id not in floor)
    if not ids:
        raise PlexShareError(
            "full access computes to an empty section list (sections=%d, "
            "welcome=%r, extra_floor=%r). An empty list unshares the server "
            "instead of granting it, so this is refused. Either the Plex server "
            "has no libraries besides the floor, or the section list failed to "
            "load." % (len(sections), welcome_title, list(extra_floor_titles)))
    return ids


def minimum_access_ids(sections: Sequence[Section], welcome_title: str,
                       extra_floor_titles: Sequence[str] = ()) -> List[int]:
    """The floor: Welcome (REQUIRED) plus any extra floor library that exists.

    Raises rather than returning `[]` when Welcome is absent. The empty list is
    the eviction bug; catching it here names the actual cause (a missing or
    renamed section) instead of letting set_sections() report the symptom.

    Extras are OPTIONAL by design: a missing `QFlix - Test` shrinks the floor to
    Welcome, it does not stop the gate. The anti-eviction guarantee rests on
    Welcome alone, and halting every grant and revoke because a convenience
    library was renamed would trade a cosmetic fault for a real one. The
    absence is surfaced by missing_floor_titles() in the run summary instead.
    """
    sec = find_section(sections, welcome_title)
    if sec is None:
        raise PlexShareError(
            "no Plex section titled %r, so 'minimum access' has no meaning and "
            "would compute to an empty share list -- which unshares the server "
            "instead of restricting it. Create the section (see "
            "scripts/configure/59b-plex-welcome-library.py) or correct the "
            "--welcome-section argument." % welcome_title)
    ids = {sec.id}
    for t in extra_floor_titles:
        extra = find_section(sections, t)
        if extra is not None:
            ids.add(extra.id)
    return sorted(ids)


def missing_floor_titles(sections: Sequence[Section],
                         extra_floor_titles: Sequence[str]) -> List[str]:
    """Extra floor titles with no matching section -- for the run summary."""
    return [t for t in extra_floor_titles if find_section(sections, t) is None]
```

Also update the module docstring's `THE EMPTY-LIST RULE` paragraph: after "if the Welcome section is missing or renamed," add "(the REQUIRED floor library; optional extras such as `QFlix - Test` only shrink the floor when missing)".

- [ ] **Step 4: Run — expect pass (old + new)**

Run: `python -m pytest tests/unit/test_welcome_section_is_exclusive.py tests/unit/test_entitlement_gate.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/maint/lib/plexshare.py tests/unit/test_welcome_section_is_exclusive.py
git commit -m "QFLX-4 feat(plexshare): access floor is a set - Welcome required, extras optional"
```

---

### Task 3: gate wiring + Kuma annotation + plan-level tests

**Files:**
- Modify: `scripts/maint/qflix-entitlement.py` — constants (~line 95), argparse (~line 1136), arm-check call site (~lines 1288-1293), main call site (~lines 1463-1470), summary (~line 1712)
- Test: `tests/unit/test_entitlement_gate.py` (append)

**Interfaces:**
- Consumes: Task 2 signatures.
- Produces: `DEFAULT_FLOOR_EXTRA: Tuple[str, ...] = ("QFlix - Test",)`; `args.floor_section: List[str]`; `floor_note(sections, extra_floor_titles) -> str` (returns `""` or `"; floor missing: <titles>"`).

- [ ] **Step 1: Write failing tests (append to `tests/unit/test_entitlement_gate.py`)**

```python
# ---------------------------------------------------------------------------
# QFLX-4: Test joins Welcome on the floor.
# ---------------------------------------------------------------------------
TEST_ID = 998
CONTENT = [132919827, 132920523, 143790062, 143790063]
FLOOR = [TEST_ID, WELCOME_ID]


def test_expired_at_welcome_only_is_raised_to_the_whole_floor(tmp_path):
    after = dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc)
    p = plan(answer=answer(ENT.NO), state=state_with(tmp_path),
             share=share(sections=[WELCOME_ID]), seerr_user=seerr_user(perms=0),
             full_ids=CONTENT, minimum_ids=FLOOR, now=after)
    assert p.state == G.S_EXPIRED
    assert p.plex_target == FLOOR


def test_entitled_holding_content_plus_floor_drops_the_floor_without_alert(tmp_path):
    """Lapsed-then-returned member: the grant must remove Welcome AND Test and
    must not trip the short-catalogue rail (self-sealing bug class, 2026-08-17)."""
    p = plan(answer=answer(ENT.YES), state=state_with(tmp_path),
             share=share(sections=CONTENT + FLOOR),
             full_ids=CONTENT, minimum_ids=FLOOR)
    assert p.state == G.S_ENTITLED
    assert p.plex_target == sorted(CONTENT)
    assert p.alert is None


def test_floor_note_names_a_missing_extra_floor_library():
    secs = [s for s in SECTIONS]                       # no Test section
    assert G.floor_note(secs, ("QFlix - Test",)) == "; floor missing: QFlix - Test"
    secs.append(PS.Section(id=TEST_ID, key=9, title="QFlix - Test", type="movie"))
    assert G.floor_note(secs, ("QFlix - Test",)) == ""


def test_default_floor_extra_is_the_test_library():
    assert G.DEFAULT_FLOOR_EXTRA == ("QFlix - Test",)
```

- [ ] **Step 2: Run — expect the two `floor_note`/`DEFAULT_FLOOR_EXTRA` tests to FAIL (AttributeError); the two plan tests may already pass (plan_for_share is set-generic) — that is the point, they pin it.**

Run: `python -m pytest tests/unit/test_entitlement_gate.py -q -k "floor"`

- [ ] **Step 3: Implement in `scripts/maint/qflix-entitlement.py`**

After `DEFAULT_WELCOME_SECTION = "QFlix - Welcome"`:

```python
# Extra libraries on the not-entitled floor beside Welcome (QFLX-4, operator
# 2026-10-03: test clips "for pre-subscription testing"). Optional: a missing
# one shrinks the floor and is named in the Kuma summary, it never stops a run.
DEFAULT_FLOOR_EXTRA = ("QFlix - Test",)
```

Near the other pure reporting helpers (above `digest_lines`):

```python
def floor_note(sections, extra_floor_titles) -> str:
    """Kuma-summary suffix naming absent extra floor libraries ('' if none).
    Visible, not paged: a missing Test library is a standing fact, and a page
    on a standing fact is how a channel gets muted."""
    gone = PS.missing_floor_titles(sections, extra_floor_titles)
    return ("; floor missing: %s" % ", ".join(gone)) if gone else ""
```

Argparse, after `--welcome-section`:

```python
    p.add_argument("--floor-section", action="append", default=None,
                   help="extra not-entitled floor library title (repeatable; "
                        "default: %s)" % ", ".join(DEFAULT_FLOOR_EXTRA))
```

and immediately after `args = p.parse_args(...)` in the same function (or at the top of `main` where `args` is first available):

```python
    if args.floor_section is None:
        args.floor_section = list(DEFAULT_FLOOR_EXTRA)
```

Both call sites — replace `(sections, args.welcome_section)` with `(sections, args.welcome_section, args.floor_section)` for `minimum_access_ids` and `full_access_ids` (arm-check ~1289/1293, main ~1464/1465).

Summary (~line 1712):

```python
    summary = "%d share(s); %s%s" % (
        len(plans), " ".join("%s=%d" % kv for kv in sorted(counts.items())),
        floor_note(sections, args.floor_section))
```

- [ ] **Step 4: Run gate + floor tests, then the whole suite after `git add`**

```bash
python -m pytest tests/unit/test_entitlement_gate.py tests/unit/test_welcome_section_is_exclusive.py -q
git add -A && bash tests/run.sh -q && python scripts/maint/qflix-audit.py
```
Expected: all PASS; audit 0 enforced findings.

- [ ] **Step 5: Commit**

```bash
git commit -m "QFLX-4 feat(entitlement): QFlix - Test rides the not-entitled floor; summary names a missing floor library"
```

---

### Task 4: poster-janitor exclusion

**Files:**
- Modify: `scripts/maint/qflix-poster-janitor.py:116`
- Test: `tests/unit/test_poster_janitor.py` (append)

- [ ] **Step 1: Failing test**

```python
def test_test_library_is_a_utility_section_not_unmanaged(monkeypatch):
    """QFLX-4: QFlix - Test is operator-placed static test clips on the
    not-entitled floor -- never janitored, never named as unmanaged."""
    xml = _sections_xml(
        [(n, str(i)) for i, n in enumerate(pj.SECTION_NAMES, 1)]
        + [("QFlix - Welcome", "7"), ("QFlix - Test", "8")])
    monkeypatch.setattr(pj, "_plex_req", lambda *a, **k: (200, xml))
    _, unmanaged = pj.resolve_sections("17025", "tok")
    assert unmanaged == []
```

- [ ] **Step 2: Run** `python -m pytest tests/unit/test_poster_janitor.py -q -k utility_section_not_unmanaged` → FAIL (`['QFlix - Test'] != []`).

- [ ] **Step 3: Implement** — replace line 116:

```python
# QFlix - Test added 2026-10-03 (QFLX-4): operator-placed playback test clips
# on the entitlement gate's not-entitled floor beside Welcome. Operator: a
# "static library of me-placed assets - I do not want it processed by
# anything". Same standing as Welcome: utility, not content.
UTILITY_SECTIONS = ["QFlix - Welcome", "QFlix - Test"]
```

- [ ] **Step 4: Run** `python -m pytest tests/unit/test_poster_janitor.py -q` → PASS.

- [ ] **Step 5: Commit** `git add -A && git commit -m "QFLX-4 feat(poster-janitor): QFlix - Test is a utility section"`

---

### Task 5: 59b `--agent/--scanner` flags

**Files:**
- Modify: `scripts/configure/59b-plex-welcome-library.py`
- Create: `tests/unit/test_plex_welcome_library_args.py`

**Interfaces:** Produces `build_parser() -> argparse.ArgumentParser` (flags `--title --path --agent --scanner --dry-run`); module constants `AGENT`, `SCANNER` keep their values as defaults.

- [ ] **Step 1: Failing test**

```python
"""59b creates Welcome by default and, with flags, the QFLX-4 Test library.
plexapi is NOT importable in CI, so only the argument surface is tested; the
script imports plexapi lazily inside main()."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "plex_welcome_59b", ROOT / "scripts" / "configure" / "59b-plex-welcome-library.py")
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)


def test_defaults_still_create_the_welcome_movie_library():
    a = M.build_parser().parse_args([])
    assert (a.title, a.agent, a.scanner) == ("QFlix - Welcome",
                                             "tv.plex.agents.movie", "Plex Movie")


def test_flags_select_personal_media_for_the_test_library():
    a = M.build_parser().parse_args([
        "--title", "QFlix - Test", "--path", "/x/Test",
        "--agent", "tv.plex.agents.none", "--scanner", "Plex Video Files"])
    assert (a.title, a.path, a.agent, a.scanner) == (
        "QFlix - Test", "/x/Test", "tv.plex.agents.none", "Plex Video Files")
```

- [ ] **Step 2: Run** `python -m pytest tests/unit/test_plex_welcome_library_args.py -q` → FAIL (`no attribute build_parser`).

- [ ] **Step 3: Implement** — extract the parser and use the flags:

```python
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default=DEFAULT_TITLE)
    ap.add_argument("--path", default=DEFAULT_DIR)
    # QFLX-4: the same idempotent create also makes `QFlix - Test` with the
    # Plex Personal Media pair (tv.plex.agents.none / Plex Video Files
    # Scanner, verified on the live PMS 1.43.3 /system/agents) so test clips
    # keep their filenames as titles instead of being matched to real films.
    ap.add_argument("--agent", default=AGENT)
    ap.add_argument("--scanner", default=SCANNER)
    ap.add_argument("--dry-run", action="store_true")
    return ap
```

In `main()`: `args = build_parser().parse_args()`; replace `AGENT`/`SCANNER` uses in the dry-run print and `plex.library.add(...)` with `args.agent`/`args.scanner`. Append to the module docstring's run line a second example:

```
    ~/.apps/python-plexapi/venv/bin/python ~/scripts/configure/59b-plex-welcome-library.py \
        --title "QFlix - Test" --path ~/media/Test \
        --agent tv.plex.agents.none --scanner "Plex Video Files"
```

- [ ] **Step 4: Run** the new test + `git add -A && bash tests/run.sh -q` → PASS.

- [ ] **Step 5: Commit** `git commit -m "QFLX-4 feat(59b): --agent/--scanner so the same script creates QFlix - Test"`

---

### Task 6: docs

**Files:** `docs/entitlement-gate-runbook.md`, `README.md`/`inventory.md` only if a guarded library count changes.

- [ ] **Step 1:** Runbook onboarding table: step 2 → "Invite that email in Plex, sharing **`QFlix - Welcome` + `QFlix - Test`** only"; step 6 → "shares **the content libraries**"; three-states table rows "accepted, not entitled" and "revoked, past grace" Plex column → "`QFlix - Welcome` + `QFlix - Test`"; entitled row → "the content libraries, **without** the floor (Welcome, Test)". Add one paragraph under *The three states*: Test is optional on the floor; if absent the Kuma summary ends `floor missing: QFlix - Test`.
- [ ] **Step 2:** `grep -rn -i "five librar\|5 librar" README.md inventory.md scripts/data/qflix-faq.html` — Tdarr's "all 5 libraries" stays TRUE (Test is deliberately not a Tdarr library); change nothing unless a line claims the Plex library count. Run `git add -A && python scripts/maint/qflix-audit.py` → 0 enforced (C-06 doc counts).
- [ ] **Step 3: Commit** `git commit -m "QFLX-4 docs(runbook): invite shares Welcome + Test; floor is a set"`
- [ ] **Step 4: Push + PR**

```bash
git push && gh pr create --base master --head feature/QFLX-4-test-library \
  --title "QFLX-4 Test library on the entitlement floor" \
  --body "Spec: docs/superpowers/specs/2026-10-03-test-library-floor-design.md
Plan: docs/superpowers/plans/2026-10-03-test-library-floor.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

Wait for green `pytest`/`audit`/`pwsh`; operator merges (or merge on operator go).

---

### Task 7: deploy + create library + verify (box; not Mon 11–15 UTC)

- [ ] **Step 1: Deploy changed files from merged master**

```bash
cd /g/Documents/GIT/Ultra.cc/QFlix && git checkout master && git pull -q && source scripts/lib/ssh.sh
for f in maint/lib/plexshare.py maint/qflix-entitlement.py maint/qflix-poster-janitor.py configure/59b-plex-welcome-library.py; do scpm_to "scripts/$f" "~/scripts/$f"; done
sshm 'cd ~/.opt/qflix-src && git pull -q && bash ~/scripts/canaries/deploy-drift.sh; echo rc=$?'
```
Expected: `rc=0` (no drift).

- [ ] **Step 2: Report-only on new code, library not yet created**

```bash
sshm 'python3 ~/scripts/maint/qflix-entitlement.py --no-notify --no-kuma 2>&1 | grep -E "done:|tripwire|floor"'
```
Expected: `done: ... floor missing: QFlix - Test`; no plan changes vs Task 1 Step 4.

- [ ] **Step 3: Pre-flight — shares still on allLibraries (counts only)**

```bash
sshm 'MID=$(curl -s "http://127.0.0.1:$(cat ~/secrets/plex.port)/identity" | grep -o "machineIdentifier=\"[^\"]*\"" | cut -d\" -f2); curl -s "https://plex.tv/api/servers/$MID/shared_servers?X-Plex-Token=$(cat ~/secrets/plex.token)" | grep -o "allLibraries=\"[01]\"" | sort | uniq -c'
```
Record the count: those shares get Test the instant it exists (entitled ones lose it again on the next gate run; frozen/exempt keep it — operator accepted frozen being left alone, Q10; report the number to the operator before Step 4).

- [ ] **Step 4: Create the library (dry run, then real)**

```bash
sshm 'mkdir -p ~/media/Test; V=~/.apps/python-plexapi/venv/bin/python; S=~/scripts/configure/59b-plex-welcome-library.py
$V $S --title "QFlix - Test" --path ~/media/Test --agent tv.plex.agents.none --scanner "Plex Video Files" --dry-run &&
$V $S --title "QFlix - Test" --path ~/media/Test --agent tv.plex.agents.none --scanner "Plex Video Files"'
```
Expected: `created section 'QFlix - Test' (key=..., type=movie, locations=[.../media/Test])`. Confirm agent via `/library/sections` shows `agent="tv.plex.agents.none"`.

- [ ] **Step 5: Gate run with the live floor**

```bash
sshm 'systemctl --user start manitoba-maint-entitlement.service; sleep 20; journalctl --user -u manitoba-maint-entitlement.service -n 30 --no-pager | grep -E "done:|expired|tripwire|floor"'
```
Expected: no `floor missing`; the 1 expired share planned/applied → Welcome+Test; entitled unchanged; no tripwire; Kuma green.

- [ ] **Step 6: Clips + scan (operator places files)**

Operator SFTPs clips into `~/media/Test`. Then:
```bash
sshm 'curl -s "http://127.0.0.1:$(cat ~/secrets/plex.port)/:/prefs?X-Plex-Token=$(cat ~/secrets/plex.token)" | grep -o "id=\"FSEventLibraryUpdatesEnabled\"[^>]*value=\"[^\"]*\""'
```
If value is `0` or items do not appear within 5 min: `GET /library/sections/<key>/refresh?X-Plex-Token=…`. Confirm item count via `/library/sections/<key>/all`.

- [ ] **Step 7: Close out** — operator invites the new member ticking **Welcome + Test** only; Jira QFLX-4 → Done with a comment of what shipped; confirm master == GitHub == box; remove worktree `git worktree remove ../QFlix-testlib`.
