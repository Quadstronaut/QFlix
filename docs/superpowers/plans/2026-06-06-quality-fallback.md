# QFlix Quality Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Movies stuck in the daily missing sweep for 5+ continuous attempted days get a two-stage quality loosening (HDTV tier → SD retail), restore-after-grab, and day-15 park + Discord alert; TV is alert-only.

**Architecture:** A pure-logic planner (`plan_movies` / `plan_tv`) computes actions from API snapshots + a JSON state file; a thin apply layer executes them via Radarr's null-skipping `PUT /movie/editor`. Profile bootstrap is a mode of the same script. Daily systemd user timer at 07:30 UTC, 30 min after the missing sweep.

**Tech Stack:** Python 3 stdlib (urllib via existing `ArrClient`), systemd user units, pytest with mocked `urllib.request.urlopen` (repo convention).

**Spec:** `docs/superpowers/specs/2026-06-06-quality-fallback-design.md`

---

## RTFM ground truth (verified 2026-06-06 — do NOT substitute trained knowledge)

All facts below were read from the **deployed** instances and the Radarr source at the **deployed tag v6.1.1.10360**. Cite this section when in doubt; re-verify against the live API if anything looks off.

**Deployed versions:** radarr + radarr2 = `6.1.1.10360` (API `/api/v3`); sonarr + sonarr2 = `4.0.17.2952` (API `/api/v3`).

**Radarr quality ladder (from `GET /qualityprofile/schema`)** — names/IDs as the API returns them. Singles: `Unknown`(0) `WORKPRINT`(24) `CAM`(25) `TELESYNC`(26) `TELECINE`(27) `REGIONAL`(29) `DVDSCR`(28) `SDTV`(1) `DVD`(2) `DVD-R`(23) `Bluray-480p`(20) `Bluray-576p`(21) `HDTV-720p`(4) `Bluray-720p`(6) `HDTV-1080p`(9) `Bluray-1080p`(7) `Remux-1080p`(30) `HDTV-2160p`(16) `Bluray-2160p`(19) `Remux-2160p`(31) `BR-DISK`(22) `Raw-HD`(10). Groups: `WEB 480p`(1000, contains WEBDL-480p+WEBRip-480p), `WEB 720p`(1001), `WEB 1080p`(1002), `WEB 2160p`(1003). Group items have `quality == None` and their own nested `items`.

**Existing radarr profiles:** id 6 `HD 720p/1080p` (cutoff=6, upgradeAllowed=**False**, already allows HDTV-720p/1080p, WEB 720p/1080p, Bluray-720p/1080p, Remux-1080p); id 7 `HD Bluray + WEB` (cutoff=7=Bluray-1080p, upgradeAllowed=True, cutoffFormatScore=10000, language=Original, 40 formatItems, allows ONLY: Bluray-720p, WEB 1080p, Bluray-1080p). radarr2 also has profile id 7 `HD Bluray + WEB` plus legacy profiles 1–6. **Movies are split across profiles 6 and 7** — per-movie `original_profile_id` is mandatory.

**`GET /wanted/missing?page=1&pageSize=N&monitored=true`** returns `{records: [MovieResource], totalRecords}`. Records DO include unreleased movies (`isAvailable: false`, e.g. Toy Story 5) — filter is load-bearing. Useful record fields (verified present): `id`, `tmdbId`, `title`, `monitored`, `isAvailable`, `qualityProfileId`, `lastSearchTime`, `hasFile`, `minimumAvailability`, `status`.

**`PUT /api/v3/movie/editor`** body = `MovieEditorResource`: `movieIds: List<int>`, `monitored: bool?`, `qualityProfileId: int?`, `minimumAvailability: MovieStatusType?`, `rootFolderPath: string`, `tags: List<int>`, `applyTags`, `moveFiles: bool`, `deleteFiles: bool`, `addImportExclusion: bool`. Controller (verified in `MovieEditorController.cs` at tag) **null-skips**: only fields present in JSON are applied (`if (resource.Monitored.HasValue) ...`). So `{"movieIds":[5],"qualityProfileId":9,"moveFiles":false}` changes profile only; `{"movieIds":[5],"monitored":false}` changes monitoring only. Returns 202 Accepted with the updated movie list.

**`POST /api/v3/qualityprofile`** body = `QualityProfileResource`: `id`, `name`, `upgradeAllowed`, `cutoff` (int — references an allowed item's quality id or group id), `items: [QualityProfileQualityItemResource]` (full ladder, every entry present, `allowed` flags toggled; group entries have `id`/`name`/`items`, leaf entries have `quality`), `minFormatScore`, `cutoffFormatScore`, `minUpgradeFormatScore`, `formatItems`, `language`. `PUT /qualityprofile/{id}` updates in place.

**Commands:** `{"name": "MoviesSearch", "movieIds": [..]}` (deployed-verified by `scripts/post-import/upgradinatorr.sh`); `{"name": "MissingMoviesSearch"}` (deployed-verified by `scripts/mcp/missing.py`). POST `/command` returns 201 with `id`.

**Sonarr `GET /wanted/missing?page=1&pageSize=N&monitored=true`** record fields (verified): `id`, `seriesId`, `title`, `airDateUtc`, `monitored`, `seasonNumber`, `episodeNumber`, `lastSearchTime`, `hasFile`. Records do NOT embed series — map titles via `GET /series` (fields: `id`, `title`, ...). v1 makes **zero writes** to sonarr.

**Timestamps** are ISO-8601 with `Z` suffix (`2026-06-06T09:00:06Z`). `datetime.fromisoformat` rejects `Z` before Python 3.11 — always `.replace("Z", "+00:00")` first.

**Seedbox wiring:** maint-pusher reads the manifest at `~/.opt/maint/apps.yaml`; pusher unit is `manitoba-maint-pusher.service`. **Hazard (memory):** restarting the pusher clears recovery's `permanently_failed` marks — acceptable, note in PR. Kuma monitor creation (`scripts/maint/bootstrap-kuma-monitors.py`) needs operator-held Kuma creds → operator-deferred step, NOT automated (kuma-automation-boundary).

**Existing missing-day evidence:** radarr currently has 29 missing (incl. unreleased); sweep runs daily 07:00 UTC and sets `lastSearchTime`.

---

## File structure

| File | Action | Responsibility |
|---|---|---|
| `scripts/mcp/lib/arr_client.py` | Modify | add `put()` (get/post/delete exist) |
| `scripts/mcp/quality_fallback.py` | Create | constants, state I/O, pure planners, profile bootstrap, apply layer, CLI |
| `scripts/mcp/systemd/qflix-quality-fallback.service` | Create | oneshot wrapping `quality_fallback.py --cron` |
| `scripts/mcp/systemd/qflix-quality-fallback.timer` | Create | daily 07:30 UTC |
| `manifest/apps.yaml` | Modify | cron-class entry → pusher pushes Kuma heartbeat |
| `scripts/configure/73-quality-fallback-install.sh` | Create | deploy + bootstrap profiles + enable timer + manifest sync |
| `docs/operator-deferred.md` | Modify | Kuma monitor creation note |
| `tests/unit/test_quality_fallback.py` | Create | pure-planner + bootstrap + CLI tests |
| `tests/unit/test_mcp_arr_client.py` | Modify | `put()` test |

State file (seedbox): `~/.apps/qflix-fallback/state.json`.

---

### Task 1: `ArrClient.put()`

**Files:**
- Modify: `scripts/mcp/lib/arr_client.py`
- Test: `tests/unit/test_mcp_arr_client.py`

- [ ] **Step 1: Write the failing test** — append to `tests/unit/test_mcp_arr_client.py` (follow the file's existing fixture style; it monkeypatches `MANITOBA_SECRETS` and mocks `urllib.request.urlopen`):

```python
@patch("urllib.request.urlopen")
def test_put_sends_put_method_and_body(mock_open, tmp_path, monkeypatch):
    secrets = tmp_path / "secrets"; secrets.mkdir()
    (secrets / "radarr.key").write_text("KEY")
    (secrets / "radarr.port").write_text("17000")
    (secrets / "radarr.urlbase").write_text("radarr")
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    m = MagicMock(); m.status = 202
    m.read.return_value = b"[]"
    m.__enter__.return_value = m
    mock_open.return_value = m
    c = ArrClient("radarr", "v3")
    code, body = c.put("/movie/editor", body={"movieIds": [5], "qualityProfileId": 9})
    assert code == 202
    req = mock_open.call_args[0][0]
    assert req.get_method() == "PUT"
    assert json.loads(req.data) == {"movieIds": [5], "qualityProfileId": 9}
```

(If the file lacks `json`/`MagicMock`/`patch`/`ArrClient` imports at top, add them matching `test_mcp_missing.py`'s import pattern.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_mcp_arr_client.py::test_put_sends_put_method_and_body -v`
Expected: FAIL — `AttributeError: 'ArrClient' object has no attribute 'put'`

- [ ] **Step 3: Implement** — in `scripts/mcp/lib/arr_client.py`, after `post()`:

```python
    def put(self, path: str, *, body: Optional[dict] = None,
            query: str = "", timeout: Optional[int] = None):
        return self._req("PUT", path, body=body, query=query,
                          timeout=timeout if timeout is not None
                          else (self._default_timeout if self._default_timeout is not None else 30))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_mcp_arr_client.py -v`
Expected: all PASS (existing tests too).

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp/lib/arr_client.py tests/unit/test_mcp_arr_client.py
git commit -m "feat(mcp): ArrClient.put for movie/editor + qualityprofile updates"
```

---

### Task 2: module skeleton — constants + state I/O

**Files:**
- Create: `scripts/mcp/quality_fallback.py`
- Create: `tests/unit/test_quality_fallback.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for scripts/mcp/quality_fallback.py."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE / "scripts" / "mcp"))

import quality_fallback as qf  # noqa: E402


def test_state_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    s = qf.load_state(p)
    assert s == {"movies": {}, "tv": {}}
    s["movies"]["radarr:100"] = {"days": 1}
    qf.save_state(p, s)
    assert qf.load_state(p) == s


def test_state_survives_corrupt_file(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json", encoding="utf-8")
    assert qf.load_state(p) == {"movies": {}, "tv": {}}


def test_parse_arr_ts_handles_z_suffix():
    dt = qf.parse_arr_ts("2026-06-06T09:00:06Z")
    assert dt == datetime(2026, 6, 6, 9, 0, 6, tzinfo=timezone.utc)
    assert qf.parse_arr_ts(None) is None
    assert qf.parse_arr_ts("") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_quality_fallback.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'quality_fallback'`

- [ ] **Step 3: Create `scripts/mcp/quality_fallback.py`**

```python
#!/usr/bin/env python3
"""scripts/mcp/quality_fallback.py — two-stage quality loosening for stuck movies.

A movie missing for PROMOTE_DAYS consecutive attempted days gets its quality
profile swapped to "QFlix Fallback HDTV"; DEEPEN_DAYS -> "QFlix Fallback SD";
PARK_DAYS -> restore original profile, unmonitor, alert. Grab at any fallback
stage -> restore original profile (file sits below cutoff; upgradinatorr/RSS
upgrade it later where the original profile allows upgrades).

TV (sonarr/sonarr2) is ALERT-ONLY in v1: a once-per-episode Discord digest
when an aired episode crosses PROMOTE_DAYS. Zero sonarr writes.

Spec: docs/superpowers/specs/2026-06-06-quality-fallback-design.md
API ground truth: deployed Radarr 6.1.1.10360 / Sonarr 4.0.17.2952 (see plan).

Modes: --cron | --emit-json | --dry-run | --bootstrap-profiles
Args:  --slug <name>  (limit to one instance)
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "maint"))

from lib.arr_client import ArrClient  # noqa: E402

MOVIE_ARRS = ["radarr", "radarr2"]
TV_ARRS = ["sonarr", "sonarr2"]

SOURCE_PROFILE_NAME = "HD Bluray + WEB"
FALLBACK_HDTV = "QFlix Fallback HDTV"
FALLBACK_SD = "QFlix Fallback SD"

# Quality names exactly as deployed Radarr 6.1.1 returns them in
# /qualityprofile/schema. "WEB 720p"/"WEB 480p" are GROUP names
# (WEBDL+WEBRip); leaf names match item["quality"]["name"].
STAGE1_ALLOW = {"HDTV-720p", "HDTV-1080p", "WEB 720p"}
STAGE2_ALLOW = STAGE1_ALLOW | {"SDTV", "DVD", "WEB 480p", "Bluray-480p", "REGIONAL"}
# Operator order: nothing pre-retail, ever. Enforced at profile build AND
# asserted after write.
BANNED = {"WORKPRINT", "CAM", "TELESYNC", "TELECINE", "DVDSCR"}

PROMOTE_DAYS = 5      # stage 0 -> 1 (Fallback HDTV)
DEEPEN_DAYS = 10      # stage 1 -> 2 (Fallback SD)
PARK_DAYS = 15        # stage 2 -> parked (restore + unmonitor + alert)
MAX_IN_FALLBACK = 25  # per instance, stage >= 1, blast-radius cap
SEARCH_FRESH_HOURS = 48  # a day only counts if the sweep actually searched

STATE_PATH = Path.home() / ".apps" / "qflix-fallback" / "state.json"


# ---------------------------------------------------------------------------
# State + time helpers
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    """Load state; tolerate missing/corrupt file (fresh start beats crash —
    worst case counters restart and promotions arrive a few days late)."""
    try:
        s = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(s, dict):
            s.setdefault("movies", {})
            s.setdefault("tv", {})
            return s
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"movies": {}, "tv": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX


def parse_arr_ts(ts: Optional[str]) -> Optional[datetime]:
    """*arr timestamps are ISO-8601 with a Z suffix; fromisoformat rejects Z
    before Python 3.11, so normalize first."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/test_quality_fallback.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp/quality_fallback.py tests/unit/test_quality_fallback.py
git commit -m "feat(fallback): module skeleton — constants, state I/O, ts parsing"
```

---

### Task 3: profile builder (pure) — `build_fallback_profile`

**Files:**
- Modify: `scripts/mcp/quality_fallback.py`
- Test: `tests/unit/test_quality_fallback.py`

- [ ] **Step 1: Write the failing tests** — append:

```python
def _ladder_item(qid, name, allowed=False):
    return {"quality": {"id": qid, "name": name}, "items": [], "allowed": allowed}


def _group_item(gid, name, members, allowed=False):
    return {"id": gid, "name": name, "quality": None, "allowed": allowed,
            "items": [_ladder_item(i, n, allowed) for i, n in members]}


def _source_profile():
    # Shape mirrors deployed GET /qualityprofile/7 (trimmed formatItems).
    return {
        "id": 7, "name": "HD Bluray + WEB", "upgradeAllowed": True, "cutoff": 7,
        "minFormatScore": 0, "cutoffFormatScore": 10000, "minUpgradeFormatScore": 1,
        "language": {"id": -2, "name": "Original"},
        "formatItems": [{"format": 1, "name": "SomeCF", "score": 100}],
        "items": [
            _ladder_item(24, "WORKPRINT"), _ladder_item(25, "CAM"),
            _ladder_item(26, "TELESYNC"), _ladder_item(27, "TELECINE"),
            _ladder_item(29, "REGIONAL"), _ladder_item(28, "DVDSCR"),
            _ladder_item(1, "SDTV"), _ladder_item(2, "DVD"),
            _group_item(1000, "WEB 480p", [(8, "WEBDL-480p"), (12, "WEBRip-480p")]),
            _ladder_item(20, "Bluray-480p"),
            _ladder_item(4, "HDTV-720p"),
            _group_item(1001, "WEB 720p", [(5, "WEBDL-720p"), (14, "WEBRip-720p")]),
            _ladder_item(6, "Bluray-720p", allowed=True),
            _ladder_item(9, "HDTV-1080p"),
            _group_item(1002, "WEB 1080p", [(3, "WEBDL-1080p"), (15, "WEBRip-1080p")],
                        allowed=True),
            _ladder_item(7, "Bluray-1080p", allowed=True),
        ],
    }


def _allowed_names(profile):
    return {(i["quality"]["name"] if i.get("quality") else i["name"])
            for i in profile["items"] if i["allowed"]}


def test_build_fallback_hdtv_menu():
    p = qf.build_fallback_profile(_source_profile(), qf.FALLBACK_HDTV, qf.STAGE1_ALLOW)
    assert "id" not in p
    assert p["name"] == "QFlix Fallback HDTV"
    assert _allowed_names(p) == {"Bluray-720p", "WEB 1080p", "Bluray-1080p",
                                 "HDTV-720p", "HDTV-1080p", "WEB 720p"}
    assert p["cutoff"] == 7                      # copied, still an allowed id
    assert p["cutoffFormatScore"] == 10000       # CF config copied verbatim
    assert p["language"] == {"id": -2, "name": "Original"}


def test_build_fallback_sd_menu_regional_in_no_preretail():
    p = qf.build_fallback_profile(_source_profile(), qf.FALLBACK_SD, qf.STAGE2_ALLOW)
    names = _allowed_names(p)
    assert {"SDTV", "DVD", "WEB 480p", "Bluray-480p", "REGIONAL"} <= names
    assert names & {"CAM", "TELESYNC", "TELECINE", "DVDSCR", "WORKPRINT"} == set()


def test_build_fallback_group_members_follow_group():
    p = qf.build_fallback_profile(_source_profile(), qf.FALLBACK_SD, qf.STAGE2_ALLOW)
    web480 = next(i for i in p["items"] if i.get("name") == "WEB 480p")
    assert web480["allowed"] is True
    assert all(sub["allowed"] for sub in web480["items"])


def test_build_fallback_never_unbans_even_if_source_corrupt():
    src = _source_profile()
    for item in src["items"]:
        if item.get("quality") and item["quality"]["name"] == "CAM":
            item["allowed"] = True  # simulate a corrupted/edited source
    p = qf.build_fallback_profile(src, qf.FALLBACK_HDTV, qf.STAGE1_ALLOW)
    assert "CAM" not in _allowed_names(p)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_quality_fallback.py -v -k build_fallback`
Expected: FAIL — `AttributeError: module 'quality_fallback' has no attribute 'build_fallback_profile'`

- [ ] **Step 3: Implement** — append to `quality_fallback.py`:

```python
# ---------------------------------------------------------------------------
# Profile bootstrap (pure builder + API wrapper)
# ---------------------------------------------------------------------------

def _item_name(item: dict) -> str:
    """Leaf items carry quality.name; group items carry their own name."""
    q = item.get("quality")
    return q["name"] if q else item.get("name", "")


def build_fallback_profile(source: dict, name: str, extra_allowed: set) -> dict:
    """Pure: clone a QualityProfileResource, widen the allowed set, enforce
    the pre-retail ban. Returns a POST-ready resource (no id)."""
    p = copy.deepcopy(source)
    p.pop("id", None)
    p["name"] = name
    for item in p["items"]:
        nm = _item_name(item)
        if nm in extra_allowed:
            item["allowed"] = True
            # group members mirror the group flag (Radarr UI does the same)
            for sub in item.get("items") or []:
                sub["allowed"] = True
        if nm in BANNED:
            item["allowed"] = False  # ban wins over everything, always
    return p
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/test_quality_fallback.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp/quality_fallback.py tests/unit/test_quality_fallback.py
git commit -m "feat(fallback): pure fallback-profile builder with pre-retail ban"
```

---

### Task 4: movie planner (pure) — accrual, transitions, reconcile, cap

**Files:**
- Modify: `scripts/mcp/quality_fallback.py`
- Test: `tests/unit/test_quality_fallback.py`

- [ ] **Step 1: Write the failing tests** — append:

```python
NOW = datetime(2026, 6, 6, 8, 0, 0, tzinfo=timezone.utc)
TODAY = "2026-06-06"
FB = {"hdtv": 90, "sd": 91}


def mk_movie(mid=1, tmdb=None, profile=7, monitored=True, available=True,
             has_file=False, searched_hours_ago=1, title="Movie"):
    ts = (NOW - timedelta(hours=searched_hours_ago)).isoformat().replace("+00:00", "Z")
    return {"id": mid, "tmdbId": tmdb or (1000 + mid), "title": title,
            "monitored": monitored, "isAvailable": available, "hasFile": has_file,
            "qualityProfileId": profile, "lastSearchTime": ts}


def _run_plan(missing, movies=None, state=None):
    movies = movies if movies is not None else {m["id"]: m for m in missing}
    state = state if state is not None else {}
    return qf.plan_movies("radarr", missing, movies, FB, state, TODAY, NOW)


def test_day_accrues_once_per_day():
    m = mk_movie()
    state = {}
    _run_plan([m], state=state)
    _run_plan([m], state=state)  # same-day rerun
    assert state["radarr:1001"]["days"] == 1


def test_unreleased_and_stale_search_accrue_nothing():
    state = {}
    _run_plan([mk_movie(mid=1, available=False),
               mk_movie(mid=2, searched_hours_ago=72)], state=state)
    assert state == {}


def test_promote_at_threshold():
    m = mk_movie()
    state = {"radarr:1001": {"movie_id": 1, "days": 4, "stage": 0,
                             "original_profile_id": None,
                             "last_counted": "2026-06-05", "parked": False,
                             "title": "Movie"}}
    acts = _run_plan([m], state=state)
    actions = [a for a in acts if a["action"] == "promote"]
    assert len(actions) == 1
    rec = state["radarr:1001"]
    assert rec["days"] == 5 and rec["stage"] == 1
    assert rec["original_profile_id"] == 7
    assert actions[0]["to_profile"] == FB["hdtv"] and actions[0]["movie_id"] == 1


def test_deepen_and_park():
    m = mk_movie(profile=FB["hdtv"])
    state = {"radarr:1001": {"movie_id": 1, "days": 9, "stage": 1,
                             "original_profile_id": 7,
                             "last_counted": "2026-06-05", "parked": False,
                             "title": "Movie"}}
    acts = _run_plan([m], state=state)
    assert [a["action"] for a in acts] == ["deepen"]
    assert acts[0]["to_profile"] == FB["sd"]

    m2 = mk_movie(profile=FB["sd"])
    state2 = {"radarr:1001": {"movie_id": 1, "days": 14, "stage": 2,
                              "original_profile_id": 7,
                              "last_counted": "2026-06-05", "parked": False,
                              "title": "Movie"}}
    acts2 = _run_plan([m2], state=state2)
    assert [a["action"] for a in acts2] == ["park"]
    assert acts2[0]["to_profile"] == 7      # restore original
    assert state2["radarr:1001"]["parked"] is True


def test_grab_at_fallback_restores_original():
    grabbed = mk_movie(has_file=True, profile=FB["hdtv"])
    state = {"radarr:1001": {"movie_id": 1, "days": 6, "stage": 1,
                             "original_profile_id": 7,
                             "last_counted": "2026-06-05", "parked": False,
                             "title": "Movie"}}
    acts = qf.plan_movies("radarr", [], {1: grabbed}, FB, state, TODAY, NOW)
    assert [a["action"] for a in acts] == ["restore_grabbed"]
    assert acts[0]["to_profile"] == 7
    assert "radarr:1001" not in state


def test_stage0_grab_drops_silently():
    grabbed = mk_movie(has_file=True)
    state = {"radarr:1001": {"movie_id": 1, "days": 3, "stage": 0,
                             "original_profile_id": None,
                             "last_counted": "2026-06-05", "parked": False,
                             "title": "Movie"}}
    acts = qf.plan_movies("radarr", [], {1: grabbed}, FB, state, TODAY, NOW)
    assert acts == []
    assert state == {}


def test_operator_profile_change_is_hands_off():
    moved = mk_movie(profile=42)  # operator picked some other profile
    state = {"radarr:1001": {"movie_id": 1, "days": 6, "stage": 1,
                             "original_profile_id": 7,
                             "last_counted": "2026-06-05", "parked": False,
                             "title": "Movie"}}
    acts = qf.plan_movies("radarr", [moved], {1: moved}, FB, state, TODAY, NOW)
    assert acts == []           # no restore — operator owns it now
    assert state == {}


def test_operator_unmonitor_mid_fallback_restores():
    um = mk_movie(monitored=False, profile=FB["hdtv"])
    state = {"radarr:1001": {"movie_id": 1, "days": 6, "stage": 1,
                             "original_profile_id": 7,
                             "last_counted": "2026-06-05", "parked": False,
                             "title": "Movie"}}
    acts = qf.plan_movies("radarr", [], {1: um}, FB, state, TODAY, NOW)
    assert [a["action"] for a in acts] == ["restore_operator"]
    assert "radarr:1001" not in state


def test_deleted_movie_drops():
    state = {"radarr:1001": {"movie_id": 1, "days": 6, "stage": 1,
                             "original_profile_id": 7,
                             "last_counted": "2026-06-05", "parked": False,
                             "title": "Movie"}}
    acts = qf.plan_movies("radarr", [], {}, FB, state, TODAY, NOW)
    assert acts == []
    assert state == {}


def test_parked_remonitored_restarts_cycle():
    back = mk_movie(profile=7)
    state = {"radarr:1001": {"movie_id": 1, "days": 15, "stage": 2,
                             "original_profile_id": 7,
                             "last_counted": "2026-06-01", "parked": True,
                             "title": "Movie"}}
    qf.plan_movies("radarr", [back], {1: back}, FB, state, TODAY, NOW)
    rec = state["radarr:1001"]
    assert rec["parked"] is False and rec["stage"] == 0 and rec["days"] == 1


def test_cap_blocks_26th_promotion():
    missing, state = [], {}
    for i in range(1, 26):  # 25 already in fallback
        m = mk_movie(mid=i, profile=FB["hdtv"])
        missing.append(m)
        state[f"radarr:{1000+i}"] = {"movie_id": i, "days": 6, "stage": 1,
                                     "original_profile_id": 7,
                                     "last_counted": "2026-06-05",
                                     "parked": False, "title": f"M{i}"}
    waiting = mk_movie(mid=26)
    missing.append(waiting)
    state["radarr:1026"] = {"movie_id": 26, "days": 4, "stage": 0,
                            "original_profile_id": None,
                            "last_counted": "2026-06-05", "parked": False,
                            "title": "M26"}
    movies = {m["id"]: m for m in missing}
    acts = qf.plan_movies("radarr", missing, movies, FB, state, TODAY, NOW)
    assert "promote" not in [a["action"] for a in acts]
    assert state["radarr:1026"]["days"] == 5      # still counts while waiting
    assert state["radarr:1026"]["stage"] == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_quality_fallback.py -v -k "promote or deepen or grab or operator or deleted or parked or cap or accrue"`
Expected: FAIL — `AttributeError: ... no attribute 'plan_movies'`

- [ ] **Step 3: Implement** — append to `quality_fallback.py`:

```python
# ---------------------------------------------------------------------------
# Movie planner — PURE. No I/O, no clock reads; everything injected.
# ---------------------------------------------------------------------------

def _fresh_search(m: dict, now: datetime) -> bool:
    """True if the sweep actually searched recently — proof of an attempt."""
    ts = parse_arr_ts(m.get("lastSearchTime"))
    return ts is not None and (now - ts) <= timedelta(hours=SEARCH_FRESH_HOURS)


def _expected_fb(stage: int, fb_ids: dict) -> Optional[int]:
    return {1: fb_ids["hdtv"], 2: fb_ids["sd"]}.get(stage)


def plan_movies(slug: str, missing: list, movies_by_id: dict, fb_ids: dict,
                state: dict, today: str, now: datetime) -> list:
    """Compute actions and mutate `state` (this slug's keys only) in place.

    state keys: f"{slug}:{tmdbId}" -> {movie_id, days, stage,
        original_profile_id, last_counted, parked, title}
    Returns actions: [{action, movie_id, key, title, to_profile|None}].
    action in {promote, deepen, park, restore_grabbed, restore_operator}.
    """
    actions: list = []
    prefix = f"{slug}:"
    # Keys dropped for operator override this run: Phase 2 must not
    # immediately re-create them (the movie is still in the missing list);
    # counting restarts on the NEXT run instead. (Found red during TDD —
    # without this, the override test fails because Phase 2 re-tracks the
    # movie in the same invocation.)
    overridden: set = set()

    # -- Phase 1: reconcile existing records against library reality --------
    for key in [k for k in state if k.startswith(prefix)]:
        rec = state[key]
        m = movies_by_id.get(rec["movie_id"])
        if m is None:                       # deleted from radarr
            del state[key]
            continue
        if rec["parked"]:
            if m["monitored"]:              # operator re-armed it
                rec.update(parked=False, stage=0, days=0,
                           original_profile_id=None, last_counted="")
            continue
        if rec["stage"] >= 1 and m["qualityProfileId"] != _expected_fb(rec["stage"], fb_ids):
            del state[key]                  # operator override: hands off
            overridden.add(key)
            continue
        if m.get("hasFile"):
            if rec["stage"] >= 1:
                actions.append({"action": "restore_grabbed", "movie_id": m["id"],
                                "key": key, "title": rec["title"],
                                "to_profile": rec["original_profile_id"]})
            del state[key]
            continue
        if not m["monitored"]:
            if rec["stage"] >= 1:
                actions.append({"action": "restore_operator", "movie_id": m["id"],
                                "key": key, "title": rec["title"],
                                "to_profile": rec["original_profile_id"]})
            del state[key]
            continue

    # -- Phase 2: accrue + transition for currently-missing eligibles -------
    in_fallback = sum(1 for k, r in state.items()
                      if k.startswith(prefix) and r["stage"] >= 1 and not r["parked"])

    for m in missing:
        if not (m.get("monitored") and m.get("isAvailable") and _fresh_search(m, now)):
            continue
        key = f"{slug}:{m['tmdbId']}"
        if key in overridden:
            continue
        rec = state.setdefault(key, {
            "movie_id": m["id"], "days": 0, "stage": 0,
            "original_profile_id": None, "last_counted": "",
            "parked": False, "title": m.get("title", "?")})
        if rec["parked"]:
            continue                        # unmonitored anyway; defensive
        if rec["last_counted"] != today:
            rec["days"] += 1
            rec["last_counted"] = today

        if rec["stage"] == 0 and rec["days"] >= PROMOTE_DAYS:
            if in_fallback >= MAX_IN_FALLBACK:
                continue                    # keep counting; promote when a slot frees
            rec["original_profile_id"] = m["qualityProfileId"]
            rec["stage"] = 1
            in_fallback += 1
            actions.append({"action": "promote", "movie_id": m["id"], "key": key,
                            "title": rec["title"], "to_profile": fb_ids["hdtv"]})
        elif rec["stage"] == 1 and rec["days"] >= DEEPEN_DAYS:
            rec["stage"] = 2
            actions.append({"action": "deepen", "movie_id": m["id"], "key": key,
                            "title": rec["title"], "to_profile": fb_ids["sd"]})
        elif rec["stage"] == 2 and rec["days"] >= PARK_DAYS:
            rec["parked"] = True
            actions.append({"action": "park", "movie_id": m["id"], "key": key,
                            "title": rec["title"],
                            "to_profile": rec["original_profile_id"]})
    return actions
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/test_quality_fallback.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp/quality_fallback.py tests/unit/test_quality_fallback.py
git commit -m "feat(fallback): pure movie planner — accrual, two-stage, park, cap, overrides"
```

---

### Task 5: TV planner (pure, alert-only)

**Files:**
- Modify: `scripts/mcp/quality_fallback.py`
- Test: `tests/unit/test_quality_fallback.py`

- [ ] **Step 1: Write the failing tests** — append:

```python
def mk_episode(eid=1, series_id=10, aired_days_ago=30, monitored=True,
               searched_hours_ago=1, season=1, ep=1, title="Ep"):
    aired = (NOW - timedelta(days=aired_days_ago)).isoformat().replace("+00:00", "Z")
    ts = (NOW - timedelta(hours=searched_hours_ago)).isoformat().replace("+00:00", "Z")
    return {"id": eid, "seriesId": series_id, "title": title,
            "seasonNumber": season, "episodeNumber": ep, "monitored": monitored,
            "airDateUtc": aired, "lastSearchTime": ts}


def test_tv_digest_fires_once_at_threshold():
    e = mk_episode()
    state = {"sonarr:1": {"days": 4, "last_counted": "2026-06-05", "alerted": False}}
    digest = qf.plan_tv("sonarr", [e], state, TODAY, NOW)
    assert len(digest) == 1
    assert digest[0]["series_id"] == 10 and digest[0]["episode_id"] == 1
    assert state["sonarr:1"]["alerted"] is True
    # next day: no repeat
    digest2 = qf.plan_tv("sonarr", [e], state, "2026-06-07", NOW + timedelta(days=1))
    assert digest2 == []


def test_tv_unaired_and_unmonitored_skipped():
    state = {}
    digest = qf.plan_tv("sonarr", [mk_episode(eid=1, aired_days_ago=-2),
                                   mk_episode(eid=2, monitored=False)],
                        state, TODAY, NOW)
    assert digest == [] and state == {}


def test_tv_grabbed_episode_pruned():
    state = {"sonarr:1": {"days": 6, "last_counted": "2026-06-05", "alerted": True}}
    qf.plan_tv("sonarr", [], state, TODAY, NOW)
    assert state == {}
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_quality_fallback.py -v -k tv`
Expected: FAIL — no attribute `plan_tv`

- [ ] **Step 3: Implement** — append:

```python
# ---------------------------------------------------------------------------
# TV planner — PURE, alert-only (v1 makes zero sonarr writes)
# ---------------------------------------------------------------------------

def plan_tv(slug: str, missing: list, state: dict, today: str,
            now: datetime) -> list:
    """Day-count aired+searched missing episodes; emit one digest entry per
    episode when it crosses PROMOTE_DAYS. Mutates `state` (this slug's keys).
    Returns [{slug, series_id, episode_id, season, episode, title, days}]."""
    prefix = f"{slug}:"
    seen = set()
    digest: list = []

    for e in missing:
        aired = parse_arr_ts(e.get("airDateUtc"))
        if not (e.get("monitored") and aired and aired <= now
                and _fresh_search(e, now)):
            continue
        key = f"{prefix}{e['id']}"
        seen.add(key)
        rec = state.setdefault(key, {"days": 0, "last_counted": "", "alerted": False})
        if rec["last_counted"] != today:
            rec["days"] += 1
            rec["last_counted"] = today
        if rec["days"] >= PROMOTE_DAYS and not rec["alerted"]:
            rec["alerted"] = True
            digest.append({"slug": slug, "series_id": e["seriesId"],
                           "episode_id": e["id"], "season": e["seasonNumber"],
                           "episode": e["episodeNumber"],
                           "title": e.get("title", "?"), "days": rec["days"]})

    # prune entries no longer missing (grabbed or unmonitored)
    for key in [k for k in state if k.startswith(prefix) and k not in seen]:
        del state[key]
    return digest
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/test_quality_fallback.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp/quality_fallback.py tests/unit/test_quality_fallback.py
git commit -m "feat(fallback): pure TV planner — once-per-episode digest, no writes"
```

---

### Task 6: API layer — fetch, bootstrap-profiles, apply, run(), CLI

**Files:**
- Modify: `scripts/mcp/quality_fallback.py`
- Test: `tests/unit/test_quality_fallback.py`

- [ ] **Step 1: Write the failing tests** — append. These exercise `run()` end-to-end with a fake client factory (no urllib):

```python
class FakeClient:
    """Minimal ArrClient stand-in: canned GET routes, records writes."""
    def __init__(self, routes):
        self.routes = routes          # {(method, path_prefix): (code, body)}
        self.writes = []              # [(method, path, body)]

    def _find(self, method, path):
        for (m, p), resp in self.routes.items():
            if m == method and path.startswith(p):
                return resp
        return (404, {"error": f"no route {method} {path}"})

    def get(self, path, **kw):
        return self._find("GET", path)

    def post(self, path, *, body=None, **kw):
        self.writes.append(("POST", path, body))
        return self._find("POST", path)

    def put(self, path, *, body=None, **kw):
        self.writes.append(("PUT", path, body))
        return self._find("PUT", path)


def _radarr_routes(missing, movies, profiles=None):
    profiles = profiles or [
        {"id": 7, "name": "HD Bluray + WEB"},
        {"id": 90, "name": qf.FALLBACK_HDTV},
        {"id": 91, "name": qf.FALLBACK_SD},
    ]
    return {
        ("GET", "/qualityprofile"): (200, profiles),
        ("GET", "/wanted/missing"): (200, {"records": missing,
                                           "totalRecords": len(missing)}),
        ("GET", "/movie"): (200, movies),
        ("PUT", "/movie/editor"): (202, []),
        ("POST", "/command"): (201, {"id": 555}),
    }


def test_run_promotes_and_searches(tmp_path, monkeypatch):
    m = mk_movie()
    clients = {"radarr": FakeClient(_radarr_routes([m], [m])),
               "radarr2": FakeClient(_radarr_routes([], [])),
               "sonarr": FakeClient({("GET", "/wanted/missing"):
                                     (200, {"records": [], "totalRecords": 0})}),
               "sonarr2": FakeClient({("GET", "/wanted/missing"):
                                      (200, {"records": [], "totalRecords": 0})})}
    state_path = tmp_path / "state.json"
    state = {"movies": {"radarr:1001": {"movie_id": 1, "days": 4, "stage": 0,
                                        "original_profile_id": None,
                                        "last_counted": "2026-06-05",
                                        "parked": False, "title": "Movie"}},
             "tv": {}}
    qf.save_state(state_path, state)
    res = qf.run(client_factory=lambda slug: clients[slug],
                 state_path=state_path, now=NOW, dry_run=False)
    r = clients["radarr"]
    assert ("PUT", "/movie/editor",
            {"movieIds": [1], "qualityProfileId": 90, "moveFiles": False}) in r.writes
    assert ("POST", "/command",
            {"name": "MoviesSearch", "movieIds": [1]}) in r.writes
    assert res["per_arr"]["radarr"]["actions"][0]["action"] == "promote"
    assert qf.load_state(state_path)["movies"]["radarr:1001"]["stage"] == 1


def test_run_dry_run_writes_nothing(tmp_path):
    m = mk_movie()
    clients = {"radarr": FakeClient(_radarr_routes([m], [m])),
               "radarr2": FakeClient(_radarr_routes([], [])),
               "sonarr": FakeClient({("GET", "/wanted/missing"):
                                     (200, {"records": [], "totalRecords": 0})}),
               "sonarr2": FakeClient({("GET", "/wanted/missing"):
                                      (200, {"records": [], "totalRecords": 0})})}
    state_path = tmp_path / "state.json"
    state = {"movies": {"radarr:1001": {"movie_id": 1, "days": 4, "stage": 0,
                                        "original_profile_id": None,
                                        "last_counted": "2026-06-05",
                                        "parked": False, "title": "Movie"}},
             "tv": {}}
    qf.save_state(state_path, state)
    qf.run(client_factory=lambda slug: clients[slug],
           state_path=state_path, now=NOW, dry_run=True)
    assert clients["radarr"].writes == []
    # state untouched on dry-run
    assert qf.load_state(state_path)["movies"]["radarr:1001"]["days"] == 4


def test_run_skips_instance_missing_fallback_profiles(tmp_path):
    m = mk_movie()
    routes = _radarr_routes([m], [m], profiles=[{"id": 7, "name": "HD Bluray + WEB"}])
    clients = {"radarr": FakeClient(routes),
               "radarr2": FakeClient(_radarr_routes([], [])),
               "sonarr": FakeClient({("GET", "/wanted/missing"):
                                     (200, {"records": [], "totalRecords": 0})}),
               "sonarr2": FakeClient({("GET", "/wanted/missing"):
                                      (200, {"records": [], "totalRecords": 0})})}
    state_path = tmp_path / "state.json"
    res = qf.run(client_factory=lambda slug: clients[slug],
                 state_path=state_path, now=NOW, dry_run=False)
    assert res["per_arr"]["radarr"]["status"] == "skipped-no-fallback-profiles"
    assert clients["radarr"].writes == []


def test_bootstrap_creates_and_updates(tmp_path):
    src = _source_profile()
    routes = {
        ("GET", "/qualityprofile/7"): (200, src),
        ("GET", "/qualityprofile"): (200, [
            {"id": 7, "name": "HD Bluray + WEB"},
            {"id": 90, "name": qf.FALLBACK_HDTV},   # exists -> PUT
        ]),
        ("POST", "/qualityprofile"): (201, {"id": 91}),
        ("PUT", "/qualityprofile/90"): (202, {"id": 90}),
    }
    client = FakeClient(routes)
    ids = qf.bootstrap_profiles("radarr", client)
    assert ids == {qf.FALLBACK_HDTV: 90, qf.FALLBACK_SD: 91}
    methods = [(m, p) for (m, p, _) in client.writes]
    assert ("PUT", "/qualityprofile/90") in methods
    assert ("POST", "/qualityprofile") in methods
    # every written profile keeps the ban
    for _, _, body in client.writes:
        for item in body["items"]:
            nm = item["quality"]["name"] if item.get("quality") else item["name"]
            if nm in qf.BANNED:
                assert item["allowed"] is False
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_quality_fallback.py -v -k "run or bootstrap_creates"`
Expected: FAIL — no attribute `run` / `bootstrap_profiles`

- [ ] **Step 3: Implement** — append:

```python
# ---------------------------------------------------------------------------
# API layer — the ONLY code that talks to *arr or the clock
# ---------------------------------------------------------------------------

def _notify(message: str, level: str = "info") -> None:
    """Discord notify, best-effort (lib lives under scripts/maint)."""
    try:
        from lib.notify import notify  # type: ignore
        notify(message, level)
    except Exception:
        pass


def _fetch_paged(client, path: str, page_size: int = 1000) -> list:
    """Drain a paged wanted/* endpoint. One page covers today's library
    sizes; loop is future-proofing."""
    records, page = [], 1
    while True:
        code, body = client.get(path, query=f"page={page}&pageSize={page_size}&monitored=true")
        if code != 200 or not isinstance(body, dict):
            return records
        batch = body.get("records") or []
        records.extend(batch)
        if len(records) >= int(body.get("totalRecords") or 0) or not batch:
            return records
        page += 1


def _resolve_fb_ids(client) -> Optional[dict]:
    code, profiles = client.get("/qualityprofile")
    if code != 200 or not isinstance(profiles, list):
        return None
    by_name = {p.get("name"): p.get("id") for p in profiles}
    if FALLBACK_HDTV in by_name and FALLBACK_SD in by_name:
        return {"hdtv": by_name[FALLBACK_HDTV], "sd": by_name[FALLBACK_SD]}
    return None


def bootstrap_profiles(slug: str, client) -> dict:
    """Create or update both fallback profiles on one instance. Returns
    {profile_name: id}. Raises RuntimeError on any failure — bootstrap is
    operator-invoked, fail loud."""
    code, listing = client.get("/qualityprofile")
    if code != 200:
        raise RuntimeError(f"{slug}: GET /qualityprofile -> {code}")
    by_name = {p["name"]: p["id"] for p in listing}
    if SOURCE_PROFILE_NAME not in by_name:
        raise RuntimeError(f"{slug}: source profile {SOURCE_PROFILE_NAME!r} not found")
    code, source = client.get(f"/qualityprofile/{by_name[SOURCE_PROFILE_NAME]}")
    if code != 200:
        raise RuntimeError(f"{slug}: GET source profile -> {code}")

    out = {}
    for name, allow in ((FALLBACK_HDTV, STAGE1_ALLOW), (FALLBACK_SD, STAGE2_ALLOW)):
        desired = build_fallback_profile(source, name, allow)
        if name in by_name:
            desired["id"] = by_name[name]
            code, body = client.put(f"/qualityprofile/{by_name[name]}", body=desired)
        else:
            code, body = client.post("/qualityprofile", body=desired)
        if code not in (200, 201, 202) or not isinstance(body, dict):
            raise RuntimeError(f"{slug}: write {name!r} -> {code}: {str(body)[:200]}")
        out[name] = body.get("id") or by_name.get(name)
    return out


_ACTION_NOTIFY = {
    "promote": ("info", "fallback stage 1 (HDTV): {title} — day {days}, searching"),
    "deepen": ("info", "fallback stage 2 (SD retail): {title} — day {days}, searching"),
    "park": ("warning", "UNFINDABLE after {days} days: {title} — profile restored, "
                        "unmonitored. Manual intervention needed."),
    "restore_grabbed": ("info", "fallback GRAB: {title} landed at reduced quality; "
                                "original profile restored (will auto-upgrade where "
                                "the profile allows)"),
    "restore_operator": ("info", "fallback released: operator unmonitored {title}; "
                                 "original profile restored"),
}


def _apply_movie_action(client, act: dict) -> bool:
    """Execute one planned action. Returns True on success."""
    mid = act["movie_id"]
    if act["action"] == "park":
        # one editor call: restore profile AND unmonitor (controller null-skips
        # everything else)
        code, _ = client.put("/movie/editor", body={
            "movieIds": [mid], "qualityProfileId": act["to_profile"],
            "monitored": False, "moveFiles": False})
        return code in (200, 202)
    code, _ = client.put("/movie/editor", body={
        "movieIds": [mid], "qualityProfileId": act["to_profile"],
        "moveFiles": False})
    if code not in (200, 202):
        return False
    if act["action"] in ("promote", "deepen"):
        code, _ = client.post("/command",
                              body={"name": "MoviesSearch", "movieIds": [mid]})
        return code in (200, 201)
    return True


def run(*, client_factory=None, state_path: Path = STATE_PATH,
        now: Optional[datetime] = None, dry_run: bool = False,
        slug: Optional[str] = None) -> dict:
    client_factory = client_factory or (lambda s: ArrClient(s, "v3"))
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    state = load_state(state_path)
    out: dict = {"per_arr": {}, "tv_digest": []}

    for s in MOVIE_ARRS:
        if slug and s != slug:
            continue
        client = client_factory(s)
        fb_ids = _resolve_fb_ids(client)
        if fb_ids is None:
            out["per_arr"][s] = {"status": "skipped-no-fallback-profiles"}
            if not dry_run:
                _notify(f"quality_fallback: fallback profiles missing on {s}; "
                        f"run --bootstrap-profiles", "error")
            continue
        missing = _fetch_paged(client, "/wanted/missing")
        code, all_movies = client.get("/movie")
        if code != 200 or not isinstance(all_movies, list):
            out["per_arr"][s] = {"status": "failed-movie-list", "code": code}
            continue
        movies_by_id = {m["id"]: m for m in all_movies}

        if dry_run:
            import copy as _copy
            scratch = _copy.deepcopy(state["movies"])
            actions = plan_movies(s, missing, movies_by_id, fb_ids, scratch, today, now)
            out["per_arr"][s] = {"status": "dry-run", "actions": actions}
            continue

        actions = plan_movies(s, missing, movies_by_id, fb_ids,
                              state["movies"], today, now)
        applied = []
        for act in actions:
            ok = _apply_movie_action(client, act)
            days = state["movies"].get(act["key"], {}).get("days", "?")
            level, tmpl = _ACTION_NOTIFY[act["action"]]
            if ok:
                _notify(f"[{s}] " + tmpl.format(title=act["title"], days=days), level)
            else:
                _notify(f"[{s}] quality_fallback FAILED to apply "
                        f"{act['action']} for {act['title']}", "error")
            applied.append({**act, "ok": ok})
        out["per_arr"][s] = {"status": "ok", "actions": applied,
                             "in_fallback": sum(
                                 1 for k, r in state["movies"].items()
                                 if k.startswith(f"{s}:") and r["stage"] >= 1
                                 and not r["parked"])}

    # ---- TV alert-only digest --------------------------------------------
    digest_all = []
    for s in TV_ARRS:
        if slug and s != slug:
            continue
        client = client_factory(s)
        missing = _fetch_paged(client, "/wanted/missing")
        if dry_run:
            import copy as _copy
            scratch = _copy.deepcopy(state["tv"])
            digest_all.extend(plan_tv(s, missing, scratch, today, now))
            continue
        digest_all.extend(plan_tv(s, missing, state["tv"], today, now))
    out["tv_digest"] = digest_all
    if digest_all and not dry_run:
        # map series ids -> titles, once per slug present in the digest
        titles = {}
        for s in {d["slug"] for d in digest_all}:
            code, series = client_factory(s).get("/series")
            if code == 200 and isinstance(series, list):
                titles.update({(s, x["id"]): x.get("title", "?") for x in series})
        lines = [f"- {titles.get((d['slug'], d['series_id']), d['slug'])} "
                 f"S{d['season']:02d}E{d['episode']:02d} {d['title']!r} "
                 f"— {d['days']}d missing" for d in digest_all]
        _notify("TV fallback candidates (alert-only, v2 decision data):\n"
                + "\n".join(lines), "warning")

    if not dry_run:
        save_state(state_path, state)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--cron", action="store_true")
    g.add_argument("--emit-json", action="store_true")
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--bootstrap-profiles", action="store_true")
    ap.add_argument("--slug")
    args = ap.parse_args()

    if args.bootstrap_profiles:
        targets = [s for s in MOVIE_ARRS if args.slug is None or s == args.slug]
        result = {}
        for s in targets:
            result[s] = bootstrap_profiles(s, ArrClient(s, "v3"))
        json.dump(result, sys.stdout, default=str)
        sys.stdout.write("\n")
        return 0

    res = run(dry_run=args.dry_run, slug=args.slug)
    if args.emit_json or args.dry_run:
        # JSON mode always exits 0 — the body carries failure detail (same
        # rationale as missing.py: a nonzero exit makes MCP discard the body).
        json.dump(res, sys.stdout, default=str)
        sys.stdout.write("\n")
        return 0
    bad = [s for s, r in res["per_arr"].items()
           if r.get("status") not in ("ok",)
           or any(not a.get("ok", True) for a in r.get("actions", []))]
    if bad:
        _notify(f"quality_fallback: failures on {bad}", "error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the full module test file**

Run: `python -m pytest tests/unit/test_quality_fallback.py -v`
Expected: all PASS

- [ ] **Step 5: Run the whole unit suite (regression)**

Run: `python -m pytest tests/unit -q`
Expected: all PASS, no regressions

- [ ] **Step 6: Commit**

```bash
git add scripts/mcp/quality_fallback.py tests/unit/test_quality_fallback.py
git commit -m "feat(fallback): API layer — bootstrap, apply, run(), CLI"
```

---

### Task 7: systemd units + manifest entry

**Files:**
- Create: `scripts/mcp/systemd/qflix-quality-fallback.service`
- Create: `scripts/mcp/systemd/qflix-quality-fallback.timer`
- Modify: `manifest/apps.yaml` (after the `qflix-missing-search` block, line ~417)

- [ ] **Step 1: Create `scripts/mcp/systemd/qflix-quality-fallback.service`**

```ini
[Unit]
Description=QFlix quality-fallback sweep — two-stage loosening for stuck movies
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 %h/scripts/mcp/quality_fallback.py --cron
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
```

- [ ] **Step 2: Create `scripts/mcp/systemd/qflix-quality-fallback.timer`**

```ini
[Unit]
Description=QFlix daily quality-fallback — 07:30 UTC (30 min after missing sweep)

[Timer]
OnCalendar=*-*-* 07:30:00 UTC
Persistent=true
Unit=qflix-quality-fallback.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Add manifest entry** — in `manifest/apps.yaml`, directly after the `qflix-missing-search` block:

```yaml
  qflix-quality-fallback:
    class: cron
    unit: qflix-quality-fallback.service
    kuma_monitor: "Qflix Quality Fallback"
    health:
      kind: systemd_oneshot
      unit: qflix-quality-fallback.service
```

- [ ] **Step 4: Validate manifest still parses**

Run: `python -m pytest tests/unit/test_manifest.py -q`
Expected: PASS (manifest loader validates apps.yaml; cron class is valid per `VALID_CLASSES`)

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp/systemd/qflix-quality-fallback.service scripts/mcp/systemd/qflix-quality-fallback.timer manifest/apps.yaml
git commit -m "feat(fallback): systemd units (07:30 UTC daily) + manifest cron entry"
```

---

### Task 8: install script + operator-deferred note

**Files:**
- Create: `scripts/configure/73-quality-fallback-install.sh`
- Modify: `docs/operator-deferred.md` (append)

- [ ] **Step 1: Create `scripts/configure/73-quality-fallback-install.sh`** (mirrors `70-mcp-install.sh` exactly — same helpers, same tar-over-ssh):

```bash
#!/usr/bin/env bash
# scripts/configure/73-quality-fallback-install.sh
# Deploy quality_fallback.py + units, bootstrap fallback profiles, sync
# manifest, restart pusher. Idempotent: re-runnable.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

# shellcheck disable=SC1091
source "$REPO/scripts/lib/ssh.sh"   # provides $SSHM_HOST + sshm/scpm_to helpers

echo "-> tar+ssh scripts/mcp/ to ${SSHM_HOST}:~/scripts/mcp/"
sshm "mkdir -p ~/scripts/mcp"
( cd "$REPO/scripts/mcp" && tar --exclude='__pycache__' --exclude='*.pyc' -cf - . ) \
  | sshm "tar -C scripts/mcp -xf -"

echo "-> bootstrap fallback profiles on radarr + radarr2 (fail-loud)"
sshm "python3 ~/scripts/mcp/quality_fallback.py --bootstrap-profiles"

echo "-> install systemd-user units"
sshm "mkdir -p ~/.config/systemd/user/"
scpm_to "$REPO/scripts/mcp/systemd/qflix-quality-fallback.service" \
        ".config/systemd/user/qflix-quality-fallback.service"
scpm_to "$REPO/scripts/mcp/systemd/qflix-quality-fallback.timer" \
        ".config/systemd/user/qflix-quality-fallback.timer"

echo "-> sync manifest (pusher reads ~/.opt/maint/apps.yaml)"
scpm_to "$REPO/manifest/apps.yaml" ".opt/maint/apps.yaml"

echo "-> enable + start timer"
sshm "systemctl --user daemon-reload && systemctl --user enable --now qflix-quality-fallback.timer"

# NOTE: pusher restart clears recovery's permanently_failed marks (known,
# accepted — see memory/push-suppression-and-resend-hazard.md)
echo "-> restart pusher to pick up new manifest entry"
sshm "systemctl --user restart manitoba-maint-pusher.service"

echo "-> verify"
sshm "systemctl --user list-timers qflix-quality-fallback.timer --all --no-pager"
sshm "python3 ~/scripts/mcp/quality_fallback.py --dry-run" | head -5

echo "OK: quality-fallback deployed; timer enabled; profiles bootstrapped."
echo "OPERATOR: create Kuma push monitor 'Qflix Quality Fallback' via"
echo "          scripts/maint/bootstrap-kuma-monitors.py (needs Kuma creds)."
```

- [ ] **Step 2: Append to `docs/operator-deferred.md`**

```markdown
## 2026-06-06 — quality-fallback Kuma monitor

`qflix-quality-fallback` (daily 07:30 UTC) is in the manifest but its Kuma
push monitor "Qflix Quality Fallback" requires operator-held Kuma creds:
run `scripts/maint/bootstrap-kuma-monitors.py` once. Until then the pusher
logs a missing-token WARN for this app (harmless).
```

- [ ] **Step 3: Commit**

```bash
git add scripts/configure/73-quality-fallback-install.sh docs/operator-deferred.md
git commit -m "feat(fallback): install script (deploy+bootstrap+timer) + operator-deferred Kuma note"
```

---

### Task 9: deploy, live verify, finalize

**Files:** none new — execution + verification.

- [ ] **Step 1: Full test suite**

Run: `python -m pytest tests/unit -q`
Expected: all PASS

- [ ] **Step 2: Deploy** (Git Bash from repo root; uses scripts/lib/ssh.sh conventions)

Run: `bash scripts/configure/73-quality-fallback-install.sh`
Expected output ends: `OK: quality-fallback deployed; timer enabled; profiles bootstrapped.`

- [ ] **Step 3: Verify profiles on the live instances** (read-only)

Run over SSH: `python3 -c` snippet listing `/qualityprofile` names for radarr+radarr2.
Expected: both instances list `QFlix Fallback HDTV` and `QFlix Fallback SD`; spot-check via API that CAM/TELESYNC/TELECINE/DVDSCR/WORKPRINT have `allowed: false` in both new profiles.

- [ ] **Step 4: Live dry-run sanity**

Run over SSH: `python3 ~/scripts/mcp/quality_fallback.py --dry-run`
Expected: JSON with `per_arr.radarr.status == "dry-run"`, zero actions (state file is fresh — day counters start today), `tv_digest: []` (fresh TV counters).

- [ ] **Step 5: Timer + Kuma wiring check**

Run over SSH: `systemctl --user list-timers qflix-quality-fallback.timer --no-pager` → NEXT shows tomorrow 07:30 UTC (09:30 CEST).
Run over SSH: `journalctl --user -u manitoba-maint-pusher.service --since '-5 min' --no-pager | tail -5` → pusher restarted cleanly (missing-token WARN for the new monitor is expected until the operator bootstraps it).

- [ ] **Step 6: Update spec status + CHANGELOG**

Spec: flip `**Status:**` line to `Implemented (PR #66)`. Append a CHANGELOG.md entry under a new heading following the file's existing format:

```markdown
## 2026-06-06
- feat(fallback): two-stage quality loosening for stuck missing movies —
  day 5 → HDTV tier, day 10 → SD retail, day 15 → park + alert; TV alert-only.
  Daily 07:30 UTC timer; profiles outside recyclarr's managed set. (PR #66)
```

- [ ] **Step 7: Push + finalize PR**

```bash
git add docs/superpowers/specs/2026-06-06-quality-fallback-design.md CHANGELOG.md
git commit -m "docs(fallback): spec status + changelog"
git push
gh pr ready 66
```

Then verify seedbox parity per session-end invariant (EOL-normalized SHA-256 of deployed `quality_fallback.py` vs `git show`).

---

## Plan self-review notes

- **Spec coverage:** profiles (Task 3+6+8), orchestrator + state machine (Tasks 2/4/6), TV digest (Task 5), safety rails — profile-existence guard (Task 6 `_resolve_fb_ids` + test), two-field-only writes (editor payloads), cap 25 (Task 4 + test), notify + Kuma (Tasks 6/7/8), `ArrClient.put` (Task 1), tests (every behavior in the spec's Testing section has a named test), rollout (Task 9 mirrors spec's 3 steps with dry-run gate). Day-accrual `lastSearchTime` rule: Task 4 (`_fresh_search` + stale test).
- **Known intentional deviations from spec draft:** bootstrap lives in `quality_fallback.py --bootstrap-profiles` + install script `73-…` (spec updated 2026-06-06 with recon corrections); stage-1 menu includes WEB 720p (spec updated; recon showed source profile lacks it).
- **Type consistency:** state record fields (`movie_id/days/stage/original_profile_id/last_counted/parked/title`) identical across Tasks 2/4/6 tests and code; action dicts (`action/movie_id/key/title/to_profile`) identical across Tasks 4/6.
