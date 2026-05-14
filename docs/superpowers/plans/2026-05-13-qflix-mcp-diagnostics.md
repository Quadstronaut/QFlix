# QFlix MCP — Diagnostics, Log Fix, and Unstick Hang Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface diagnostic capabilities through the MCP server, fix the silent `qflix_get_logs` tool, find and patch the `unstick.py` SSH-timeout hang, and add a `meta-stuck` stale rule that auto-handles dead-magnet torrents.

**Architecture:** Two layers preserved. Local Windows host runs the FastMCP stdio server (`scripts/local/qflix-mcp/qflix_mcp.py`) which shells via `ssh_call` to remote scripts in `scripts/mcp/*.py` on the seedbox. Aggregator `scripts/local/qflix-collect.ps1` runs hourly on the workstation and updates `stale-state.json`. This plan modifies all three layers but adds no new layer.

**Tech Stack:** Python 3.x (FastMCP, urllib, subprocess), PowerShell 5.1 (workstation aggregator), pytest with `unittest.mock` for unit tests, SSH BatchMode for remote invocation.

---

## File Structure

**Modified files:**
- `scripts/local/qflix-mcp/lib/ssh.py` — add `TimeoutExpired` catch returning `returncode=124`.
- `scripts/local/qflix-mcp/qflix_mcp.py` — rewrite `qflix_get_logs`, add `qflix_list_log_apps`, add `qflix_diagnose_unstick`, add `timeout` parameter to `qflix_unstick_torrent`, surface `ssh-timeout` status across all three write tools, register the two new tools.
- `scripts/mcp/logs.py` — add `--list-apps` flag.
- `scripts/mcp/unstick.py` — split `run()` into `_preflight` / `_resolve_queue_item` / `_execute_delete` / `_record_event`; fix the slow `/queue` lookup query; add `--diagnose` flag; always emit an event row; pass `timeout=15` to `ArrClient`.
- `scripts/mcp/lib/arr_client.py` — add `timeout` kwarg to `ArrClient.__init__`.
- `scripts/local/qflix-collect.ps1` — add `meta-stuck` rule for `metaDL` torrents older than 24 hours.

**Modified tests:**
- `tests/unit/test_qflix_mcp_ssh.py` — add timeout case.
- `tests/unit/test_qflix_mcp_tools.py` — rewrite `test_get_logs_returns_lines`, add tests for new tools and timeout passthrough.
- `tests/unit/test_mcp_logs.py` — add `--list-apps` test.
- `tests/unit/test_mcp_unstick.py` — add tests for `_preflight`/`_resolve_queue_item`/`_execute_delete`/`--diagnose`/refusal events.
- `tests/unit/test_mcp_arr_client.py` — add `timeout` kwarg test.

**No new files.** Every change extends an existing module.

---

## Task 1: Harden `ssh_call` against `TimeoutExpired`

**Files:**
- Modify: `scripts/local/qflix-mcp/lib/ssh.py`
- Test: `tests/unit/test_qflix_mcp_ssh.py`

Today `subprocess.run(..., timeout=N)` raises `subprocess.TimeoutExpired` on timeout. The exception propagates through `ssh_call` and into every MCP tool, which surfaces as a bare error message. Catch it at the wrapper and return a synthetic `CompletedProcess` with `returncode=124` so callers can branch on it cleanly.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_qflix_mcp_ssh.py`:

```python
import subprocess


@patch("subprocess.run")
def test_ssh_call_returns_124_on_timeout(mock_run, tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "seedbox.ssh-host").write_text("h")
    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["ssh"], timeout=5)
    result = ssh_call("sleep 999", secrets_dir=secrets, timeout=5)
    assert result.returncode == 124
    assert "ssh-timeout" in result.stderr
    assert "5" in result.stderr
    assert result.stdout == ""
```

- [ ] **Step 2: Run the test and verify it fails**

```
pytest tests/unit/test_qflix_mcp_ssh.py::test_ssh_call_returns_124_on_timeout -v
```

Expected: FAIL with `TimeoutExpired` propagating out of `ssh_call`.

- [ ] **Step 3: Implement the catch**

Replace the final two lines of `scripts/local/qflix-mcp/lib/ssh.py` (the `return subprocess.run(...)` call) with:

```python
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=cmd, returncode=124,
            stdout="", stderr=f"ssh-timeout after {timeout}s",
        )
```

- [ ] **Step 4: Run the full ssh test file and verify all pass**

```
pytest tests/unit/test_qflix_mcp_ssh.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/local/qflix-mcp/lib/ssh.py tests/unit/test_qflix_mcp_ssh.py
git commit -m "fix(mcp): ssh_call catches TimeoutExpired (returncode=124)"
```

---

## Task 2: Add `--list-apps` flag to remote `logs.py`

**Files:**
- Modify: `scripts/mcp/logs.py`
- Test: `tests/unit/test_mcp_logs.py`

Today there is no way to discover valid log slugs without reading the source. Add a `--list-apps` flag that prints the two routing tables as JSON and exits.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_mcp_logs.py`:

```python
import json
import subprocess
from pathlib import Path

LOGS_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "mcp" / "logs.py"


def test_list_apps_returns_route_tables():
    proc = subprocess.run(
        ["python3", str(LOGS_SCRIPT), "--emit-json", "--list-apps"],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert "file_apps" in data and "systemd_apps" in data
    assert "sonarr" in data["file_apps"]
    assert "radarr" in data["file_apps"]
    assert "listmonk" in data["systemd_apps"]
    # Lists must be sorted for stable output
    assert data["file_apps"] == sorted(data["file_apps"])
    assert data["systemd_apps"] == sorted(data["systemd_apps"])
```

- [ ] **Step 2: Run test, expect failure**

```
pytest tests/unit/test_mcp_logs.py::test_list_apps_returns_route_tables -v
```

Expected: FAIL — `--list-apps` is not a recognized argument.

- [ ] **Step 3: Implement `--list-apps` in `scripts/mcp/logs.py`**

In `main()`, change the argparse setup and add an early return. Locate this block:

```python
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--emit-json", action="store_true")
    g.add_argument("--cron", action="store_true")
    ap.add_argument("--app", required=True, help="slug or 'all'")
```

Replace with:

```python
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--emit-json", action="store_true")
    g.add_argument("--cron", action="store_true")
    ap.add_argument("--list-apps", action="store_true",
                    help="print routing tables and exit")
    ap.add_argument("--app", help="slug or 'all'")
```

(The `--app` flag drops `required=True` because `--list-apps` is now a valid alternative.)

After `args = ap.parse_args()`, insert this block before the existing app-required logic:

```python
    if args.list_apps:
        out = {
            "file_apps": sorted(_FILE_LOGS.keys()),
            "systemd_apps": sorted(_SYSTEMD_LOGS.keys()),
        }
        if args.emit_json:
            json.dump(out, sys.stdout, default=str)
            sys.stdout.write("\n")
        return 0
    if not args.app:
        ap.error("--app required (unless --list-apps)")
```

- [ ] **Step 4: Run test, expect pass**

```
pytest tests/unit/test_mcp_logs.py -v
```

Expected: all 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp/logs.py tests/unit/test_mcp_logs.py
git commit -m "feat(mcp): logs.py --list-apps prints routing tables"
```

---

## Task 3: Add `qflix_list_log_apps` MCP tool

**Files:**
- Modify: `scripts/local/qflix-mcp/qflix_mcp.py`
- Test: `tests/unit/test_qflix_mcp_tools.py`

Wrap the new `--list-apps` flag as an MCP tool so callers can discover slugs without remembering script paths.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_qflix_mcp_tools.py`:

```python
from unittest.mock import patch, MagicMock


@patch("qflix_mcp.ssh_call")
def test_list_log_apps_returns_routes(mock_ssh):
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = '{"file_apps": ["sonarr", "radarr"], "systemd_apps": ["listmonk"]}'
    fake.stderr = ""
    mock_ssh.return_value = fake
    out = qflix_mcp.qflix_list_log_apps()
    assert out == {"file_apps": ["sonarr", "radarr"], "systemd_apps": ["listmonk"]}
    cmd = mock_ssh.call_args[0][0]
    assert "logs.py" in cmd and "--list-apps" in cmd


@patch("qflix_mcp.ssh_call")
def test_list_log_apps_ssh_timeout(mock_ssh):
    fake = MagicMock()
    fake.returncode = 124
    fake.stdout = ""
    fake.stderr = "ssh-timeout after 30s"
    mock_ssh.return_value = fake
    out = qflix_mcp.qflix_list_log_apps()
    assert out["status"] == "ssh-timeout"
    assert out["timeout_s"] == 30
```

- [ ] **Step 2: Run test, expect failure**

```
pytest tests/unit/test_qflix_mcp_tools.py::test_list_log_apps_returns_routes -v
```

Expected: FAIL — `qflix_list_log_apps` does not exist.

- [ ] **Step 3: Add the tool function in `qflix_mcp.py`**

Below the existing `qflix_arr_queue` function (before the `# ===== WRITE TOOLS` header), insert:

```python
def qflix_list_log_apps() -> dict:
    """Returns: known log slugs from the host's logs.py routing tables.

    Use when: you want to know which `app` values qflix_get_logs accepts.
    Returns {"file_apps": [...], "systemd_apps": [...]} or
    {"status": "ssh-timeout", "timeout_s": N} on SSH timeout.
    """
    proc = ssh_call("python3 ~/scripts/mcp/logs.py --emit-json --list-apps",
                    timeout=30)
    if proc.returncode == 124:
        return _parse_ssh_timeout(proc.stderr, default=30)
    if proc.returncode != 0:
        return {"status": "ssh-failed", "code": proc.returncode,
                "stderr": proc.stderr[:300]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "bad-json", "stdout": proc.stdout[:300]}
```

And add a helper at the top of the file (after the `DATA_ROOT = ...` line):

```python
def _parse_ssh_timeout(stderr: str, default: int) -> dict:
    """Extract the timeout integer from 'ssh-timeout after Ns' stderr."""
    import re
    m = re.search(r"ssh-timeout after (\d+)s", stderr or "")
    return {"status": "ssh-timeout",
            "timeout_s": int(m.group(1)) if m else default}
```

Register the tool — in `_build_server()` add after `server.tool()(qflix_arr_queue)`:

```python
    server.tool()(qflix_list_log_apps)
```

- [ ] **Step 4: Run tests, expect pass**

```
pytest tests/unit/test_qflix_mcp_tools.py -v -k list_log_apps
```

Expected: both new tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/local/qflix-mcp/qflix_mcp.py tests/unit/test_qflix_mcp_tools.py
git commit -m "feat(mcp): qflix_list_log_apps tool + _parse_ssh_timeout helper"
```

---

## Task 4: Rewrite `qflix_get_logs` to invoke remote `logs.py`

**Files:**
- Modify: `scripts/local/qflix-mcp/qflix_mcp.py`
- Test: `tests/unit/test_qflix_mcp_tools.py`

The current implementation reads from `B:\QFlix\data\logs\<date>\<app>.log` — a directory that's never populated. Replace it with an SSH call to the real `logs.py`. Drop the `date` parameter (replaced by `since`).

- [ ] **Step 1: Update the existing failing test to the new shape**

In `tests/unit/test_qflix_mcp_tools.py`, replace `test_get_logs_returns_lines` with:

```python
@patch("qflix_mcp.ssh_call")
def test_get_logs_returns_parsed_lines(mock_ssh):
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = json.dumps({
        "app": "sonarr",
        "source": "/home/q/.apps/sonarr/logs/sonarr.txt",
        "lines": [
            {"ts": "2026-05-13T10:00:00Z", "level": "Info",
             "message": "hello", "source_file": "sonarr.txt"},
            {"ts": "2026-05-13T10:01:00Z", "level": "Error",
             "message": "boom", "source_file": "sonarr.txt"},
        ],
    })
    fake.stderr = ""
    mock_ssh.return_value = fake
    out = qflix_mcp.qflix_get_logs(app="sonarr", since="6h", tail=50)
    assert out["app"] == "sonarr"
    assert len(out["lines"]) == 2
    cmd = mock_ssh.call_args[0][0]
    assert "logs.py" in cmd and "--app sonarr" in cmd
    assert "--since 6h" in cmd and "--tail 50" in cmd


@patch("qflix_mcp.ssh_call")
def test_get_logs_applies_grep_client_side(mock_ssh):
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = json.dumps({
        "app": "sonarr", "source": "x",
        "lines": [
            {"ts": "t", "level": "Info", "message": "hello world",
             "source_file": "x"},
            {"ts": "t", "level": "Error", "message": "boom",
             "source_file": "x"},
        ],
    })
    fake.stderr = ""
    mock_ssh.return_value = fake
    out = qflix_mcp.qflix_get_logs(app="sonarr", grep="boom")
    assert len(out["lines"]) == 1
    assert out["lines"][0]["message"] == "boom"


@patch("qflix_mcp.ssh_call")
def test_get_logs_handles_unsupported_app(mock_ssh):
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = json.dumps({"app": "bogus", "error": "unsupported", "lines": []})
    fake.stderr = ""
    mock_ssh.return_value = fake
    out = qflix_mcp.qflix_get_logs(app="bogus")
    assert out.get("error") == "unsupported"
    assert out["lines"] == []
```

Also delete the old `test_get_logs_returns_lines` function and remove its `(logs / "sonarr.log").write_text(...)` setup (the old cache-file fixture is no longer used).

- [ ] **Step 2: Run tests, expect failures**

```
pytest tests/unit/test_qflix_mcp_tools.py -v -k get_logs
```

Expected: all three new tests fail (the implementation still reads the dead cache).

- [ ] **Step 3: Replace `qflix_get_logs` in `qflix_mcp.py`**

Locate the existing `qflix_get_logs` function (around lines 105–122 in the current file) and replace its entire body with:

```python
def qflix_get_logs(app: str, since: str = "24h", tail: int = 500,
                   grep: Optional[str] = None) -> dict:
    """Returns: structured log lines for one app via the host's logs.py.

    Use when: investigating recent app behavior.
    `since` accepts journalctl-style durations ("6h", "30m", "2d").
    `grep` filters lines whose `message` contains the substring (case-insensitive).
    Use qflix_list_log_apps() to discover valid `app` slugs.
    """
    cmd = (f"python3 ~/scripts/mcp/logs.py --emit-json "
           f"--app {app} --since {since} --tail {int(tail)}")
    proc = ssh_call(cmd, timeout=60)
    if proc.returncode == 124:
        return _parse_ssh_timeout(proc.stderr, default=60)
    if proc.returncode != 0:
        return {"status": "ssh-failed", "code": proc.returncode,
                "stderr": proc.stderr[:300]}
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "bad-json", "stdout": proc.stdout[:300]}
    if grep and isinstance(result, dict) and "lines" in result:
        gl = grep.lower()
        result["lines"] = [
            ln for ln in result["lines"]
            if gl in (ln.get("message") or "").lower()
        ]
    return result
```

- [ ] **Step 4: Run tests, expect pass**

```
pytest tests/unit/test_qflix_mcp_tools.py -v -k get_logs
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/local/qflix-mcp/qflix_mcp.py tests/unit/test_qflix_mcp_tools.py
git commit -m "fix(mcp): qflix_get_logs invokes remote logs.py (was reading dead cache)"
```

---

## Task 5: Add `timeout` kwarg to `ArrClient`

**Files:**
- Modify: `scripts/mcp/lib/arr_client.py`
- Test: `tests/unit/test_mcp_arr_client.py`

Today `ArrClient` always uses 20s for GET / 30s for POST/DELETE. Allow callers to set a default at construction time so we can fail fast on a slow *arr instead of bleeding into the SSH window.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_mcp_arr_client.py`:

```python
from unittest.mock import patch, MagicMock


def _setup_secrets(tmp_path: Path) -> Path:
    s = tmp_path / "secrets"; s.mkdir()
    (s / "sonarr.key").write_text("K")
    (s / "sonarr.port").write_text("17026")
    (s / "sonarr.urlbase").write_text("sonarr")
    return s


@patch("lib.arr_client.urllib.request.urlopen")
def test_arr_client_passes_explicit_timeout(mock_open, tmp_path):
    from lib.arr_client import ArrClient
    resp = MagicMock()
    resp.read.return_value = b'{"records": []}'
    resp.status = 200
    resp.__enter__.return_value = resp
    mock_open.return_value = resp
    c = ArrClient("sonarr", "v3", secrets_dir=_setup_secrets(tmp_path), timeout=7)
    c.get("/queue")
    # urlopen called with timeout=7 (positional via kwarg)
    _, kwargs = mock_open.call_args
    assert kwargs.get("timeout") == 7


@patch("lib.arr_client.urllib.request.urlopen")
def test_arr_client_default_timeout_unchanged(mock_open, tmp_path):
    from lib.arr_client import ArrClient
    resp = MagicMock()
    resp.read.return_value = b'{}'
    resp.status = 200
    resp.__enter__.return_value = resp
    mock_open.return_value = resp
    c = ArrClient("sonarr", "v3", secrets_dir=_setup_secrets(tmp_path))
    c.get("/queue")
    _, kwargs = mock_open.call_args
    assert kwargs.get("timeout") == 20  # existing default
```

- [ ] **Step 2: Run tests, expect first to fail, second to pass**

```
pytest tests/unit/test_mcp_arr_client.py -v -k timeout
```

Expected: first new test fails (no `timeout` kwarg on `__init__`), second passes (existing behavior intact).

- [ ] **Step 3: Implement the kwarg**

In `scripts/mcp/lib/arr_client.py`, change `__init__` from:

```python
    def __init__(self, slug: str, version: str, *, secrets_dir: Optional[Path] = None):
        self.slug = slug
        self.version = version
        self.secrets = secrets_dir or Path(
            os.environ.get("MANITOBA_SECRETS", str(Path.home() / "secrets"))
        )
        self.api_key = _read(self.secrets / f"{slug}.key")
        self.port = _read(self.secrets / f"{slug}.port")
        self.urlbase = _read(self.secrets / f"{slug}.urlbase") or slug
```

to:

```python
    def __init__(self, slug: str, version: str, *,
                 secrets_dir: Optional[Path] = None,
                 timeout: Optional[int] = None):
        self.slug = slug
        self.version = version
        self.secrets = secrets_dir or Path(
            os.environ.get("MANITOBA_SECRETS", str(Path.home() / "secrets"))
        )
        self.api_key = _read(self.secrets / f"{slug}.key")
        self.port = _read(self.secrets / f"{slug}.port")
        self.urlbase = _read(self.secrets / f"{slug}.urlbase") or slug
        self._default_timeout = timeout
```

Then change `get` / `post` / `delete` so they fall back to the client default when the caller doesn't override:

```python
    def get(self, path: str, *, query: str = "", timeout: Optional[int] = None):
        return self._req("GET", path, query=query,
                          timeout=timeout if timeout is not None
                          else (self._default_timeout if self._default_timeout is not None else 20))

    def post(self, path: str, *, body: Optional[dict] = None,
             query: str = "", timeout: Optional[int] = None):
        return self._req("POST", path, body=body, query=query,
                          timeout=timeout if timeout is not None
                          else (self._default_timeout if self._default_timeout is not None else 30))

    def delete(self, path: str, *, query: str = "", timeout: Optional[int] = None):
        return self._req("DELETE", path, query=query,
                          timeout=timeout if timeout is not None
                          else (self._default_timeout if self._default_timeout is not None else 30))
```

- [ ] **Step 4: Run tests, expect pass**

```
pytest tests/unit/test_mcp_arr_client.py -v
```

Expected: all tests pass (existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp/lib/arr_client.py tests/unit/test_mcp_arr_client.py
git commit -m "feat(mcp): ArrClient(timeout=N) — per-instance default"
```

---

## Task 6: Refactor `unstick.py` `run()` into focused functions

**Files:**
- Modify: `scripts/mcp/unstick.py`
- Test: `tests/unit/test_mcp_unstick.py`

Today `run()` is one ~60-line function that does pre-flight checks, queue lookup, DELETE, and event emission. Split it so `--diagnose` can reuse pre-flight + lookup without DELETE, and so the event-emission path is shared by all return branches.

- [ ] **Step 1: Write failing tests for the new internal API**

Append to `tests/unit/test_mcp_unstick.py`:

```python
def test_preflight_passes_when_clean(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    refusal = unstick._preflight("sonarr", state_file=state, max_actions_per_day=10)
    assert refusal is None


def test_preflight_returns_unknown_slug():
    refusal = unstick._preflight("garbage", state_file=None, max_actions_per_day=10)
    assert refusal["status"] == "refused-unknown-slug"


def test_preflight_returns_red(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    state.write_text(json.dumps({"monitors": {"Sonarr": {"status": "down"}}}))
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    refusal = unstick._preflight("sonarr", state_file=state, max_actions_per_day=10)
    assert refusal["status"] == "refused-arr-red"


@patch("lib.arr_client.urllib.request.urlopen")
def test_resolve_queue_item_by_hash_found(mock_open, tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    mock_open.return_value = _resp({"records": [
        {"id": 99, "downloadId": "ABC", "title": "Some Show"},
    ]})
    from lib.arr_client import ArrClient
    c = ArrClient("sonarr", "v3", secrets_dir=secrets)
    out = unstick._resolve_queue_item(c, hash_="abc", queue_id=None)
    assert out["status"] == "found"
    assert out["queue_id"] == 99 and out["title"] == "Some Show"


@patch("lib.arr_client.urllib.request.urlopen")
def test_resolve_queue_item_already_removed(mock_open, tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    mock_open.return_value = _resp({"records": []})
    from lib.arr_client import ArrClient
    c = ArrClient("sonarr", "v3", secrets_dir=secrets)
    out = unstick._resolve_queue_item(c, hash_="missing", queue_id=None)
    assert out["status"] == "already-removed"


@patch("lib.arr_client.urllib.request.urlopen")
def test_execute_delete_success(mock_open, tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    mock_open.return_value = _resp("", status=200)
    from lib.arr_client import ArrClient
    c = ArrClient("sonarr", "v3", secrets_dir=secrets)
    out = unstick._execute_delete(c, queue_id=99, dry_run=False)
    assert out["status"] == "deleted+blocklisted"


def test_execute_delete_dry_run(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    from lib.arr_client import ArrClient
    c = ArrClient("sonarr", "v3", secrets_dir=secrets)
    out = unstick._execute_delete(c, queue_id=99, dry_run=True)
    assert out["status"] == "dry-run"
```

- [ ] **Step 2: Run tests, expect failures**

```
pytest tests/unit/test_mcp_unstick.py -v -k "preflight or resolve_queue or execute_delete"
```

Expected: all 7 new tests fail — symbols don't exist.

- [ ] **Step 3: Implement the decomposition in `scripts/mcp/unstick.py`**

Replace the entire current `run()` function (and its helpers `_lookup_queue_by_hash`) with the refactored shape below. Keep all module-level imports, constants, `_today_events_path`, `_count_today`, and `_append_event` intact.

```python
def _preflight(slug: str, *, state_file: Optional[Path],
               max_actions_per_day: int) -> Optional[dict]:
    """Returns a refusal dict if any guard trips, else None."""
    if slug not in ARR_VERSIONS:
        return {"status": "refused-unknown-slug", "slug": slug}
    if is_arr_red(slug, state_file=state_file):
        return {"status": "refused-arr-red", "slug": slug}
    used = _count_today()
    if used >= max_actions_per_day:
        return {"status": "refused-cap-hit", "count": used,
                "cap": max_actions_per_day}
    return None


def _resolve_queue_item(c: ArrClient, *, hash_: Optional[str],
                        queue_id: Optional[int]) -> dict:
    """Find the queue item by hash or id. Returns one of:
      {"status": "found", "queue_id": N, "title": "...", "hash": "..."}
      {"status": "already-removed"}
      {"status": "queue-fetch-failed", "code": N}
    """
    code, payload = c.get("/queue", timeout=15)
    if code != 200 or not isinstance(payload, dict):
        return {"status": "queue-fetch-failed", "code": code}
    records = payload.get("records") or []
    if hash_:
        target = hash_.lower()
        for q in records:
            if (q.get("downloadId") or "").lower() == target:
                return {"status": "found",
                        "queue_id": q.get("id"),
                        "title": q.get("title", "?"),
                        "hash": q.get("downloadId")}
        return {"status": "already-removed"}
    if queue_id is not None:
        for q in records:
            if q.get("id") == queue_id:
                return {"status": "found",
                        "queue_id": queue_id,
                        "title": q.get("title", "?"),
                        "hash": q.get("downloadId")}
        return {"status": "already-removed"}
    return {"status": "queue-fetch-failed", "code": 0}


def _execute_delete(c: ArrClient, *, queue_id: int, dry_run: bool) -> dict:
    """Single point of the destructive DELETE. Returns the action outcome."""
    if dry_run:
        return {"status": "dry-run"}
    code, _ = c.delete(f"/queue/{queue_id}",
                       query="removeFromClient=true&blocklist=true",
                       timeout=30)
    if code in (200, 204):
        return {"status": "deleted+blocklisted"}
    if code == 404:
        return {"status": "already-removed"}
    return {"status": "delete-failed", "code": code}


def _record_event(*, slug: str, queue_id: Optional[int], hash_: Optional[str],
                  title: str, reason: str, result_status: str) -> None:
    _append_event({
        "ts": dt.datetime.utcnow().isoformat() + "Z",
        "action": "unstick",
        "slug": slug,
        "queue_id": queue_id,
        "hash": hash_,
        "title": title,
        "reason": reason,
        "result": result_status,
        "post_action": ("sonarr-research-queued"
                         if result_status == "deleted+blocklisted" else None),
    })


def run(*, slug: str, queue_id: Optional[int] = None,
        hash_: Optional[str] = None, reason: str = "",
        dry_run: bool = False, max_actions_per_day: int = 10,
        state_file: Optional[Path] = None) -> dict:
    refusal = _preflight(slug, state_file=state_file,
                          max_actions_per_day=max_actions_per_day)
    if refusal is not None:
        _record_event(slug=slug, queue_id=queue_id, hash_=hash_,
                       title="?", reason=reason,
                       result_status=refusal["status"])
        return refusal

    c = ArrClient(slug, ARR_VERSIONS[slug], timeout=15)
    resolved = _resolve_queue_item(c, hash_=hash_, queue_id=queue_id)
    if resolved["status"] != "found":
        _record_event(slug=slug, queue_id=queue_id, hash_=hash_,
                       title="?", reason=reason,
                       result_status=resolved["status"])
        return resolved

    actual_qid = resolved["queue_id"]
    title = resolved["title"]
    hash_ = resolved.get("hash") or hash_

    action = _execute_delete(c, queue_id=actual_qid, dry_run=dry_run)
    final_status = action["status"]
    _record_event(slug=slug, queue_id=actual_qid, hash_=hash_,
                   title=title, reason=reason,
                   result_status=final_status)
    out = {"status": final_status,
           "pre": {"queue_id": actual_qid, "title": title, "hash": hash_}}
    if "code" in action:
        out["code"] = action["code"]
    return out
```

- [ ] **Step 4: Run the full unstick test file, expect all to pass**

```
pytest tests/unit/test_mcp_unstick.py -v
```

Expected: original 4 tests still pass, 7 new tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp/unstick.py tests/unit/test_mcp_unstick.py
git commit -m "refactor(mcp): unstick.py split into preflight/resolve/execute/record"
```

---

## Task 7: Always record an event row (including refusal paths)

**Files:**
- Test: `tests/unit/test_mcp_unstick.py`

Verify the refactor in Task 6 actually emits events for the refusal paths. The pure decomposition could be regression-prone, so this is a separate explicit check.

- [ ] **Step 1: Write failing tests for refusal-path event rows**

Append to `tests/unit/test_mcp_unstick.py`:

```python
def test_refused_arr_red_writes_event(tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    state.write_text(json.dumps({"monitors": {"Sonarr": {"status": "down"}}}))
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    unstick.run(slug="sonarr", queue_id=42, reason="t", state_file=state)
    log_files = list(events.glob("*.jsonl"))
    assert len(log_files) == 1
    line = json.loads(log_files[0].read_text().strip())
    assert line["result"] == "refused-arr-red"
    assert line["slug"] == "sonarr"


def test_refused_cap_hit_writes_event(tmp_path, monkeypatch):
    import datetime as dt_
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    today = events / f"{dt_.date.today().isoformat()}.jsonl"
    today.write_text("\n".join('{"action":"unstick"}' for _ in range(10)) + "\n")
    unstick.run(slug="sonarr", queue_id=42, reason="t",
                max_actions_per_day=10, state_file=state)
    line = json.loads(today.read_text().splitlines()[-1])
    assert line["result"] == "refused-cap-hit"


@patch("lib.arr_client.urllib.request.urlopen")
def test_already_removed_writes_event(mock_open, tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    mock_open.return_value = _resp({"records": []})
    unstick.run(slug="sonarr", hash_="dead", reason="t", state_file=state)
    log_files = list(events.glob("*.jsonl"))
    line = json.loads(log_files[0].read_text().strip())
    assert line["result"] == "already-removed"
```

- [ ] **Step 2: Run, expect pass**

```
pytest tests/unit/test_mcp_unstick.py -v -k "writes_event"
```

Expected: all 3 pass — the Task 6 refactor already routes through `_record_event` on every branch. If any fail, fix in Task 6's `run()` implementation and rerun.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_mcp_unstick.py
git commit -m "test(mcp): verify unstick records events on refusal paths"
```

---

## Task 8: Add `--diagnose` flag to `unstick.py`

**Files:**
- Modify: `scripts/mcp/unstick.py`
- Test: `tests/unit/test_mcp_unstick.py`

The diagnose mode runs the same pre-flight + queue lookups as the real path but with timing instrumentation and no DELETE. Outputs a JSON object with per-phase milliseconds.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_mcp_unstick.py`:

```python
@patch("lib.arr_client.urllib.request.urlopen")
def test_diagnose_returns_phase_timings(mock_open, tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    # 3 GETs: legacy-shape /queue?pageSize=500&includeUnknownSeriesItems=true,
    # default /queue, and a hash-resolve over default. The mock returns the
    # same payload for all three; the test only cares about structure.
    mock_open.side_effect = [
        _resp({"records": [{"id": 9, "downloadId": "ABC", "title": "T"}]}),
        _resp({"records": [{"id": 9, "downloadId": "ABC", "title": "T"}]}),
    ]
    out = unstick.diagnose(slug="sonarr", hash_="abc", state_file=state)
    assert out["status"] == "diagnose"
    assert out["slug"] == "sonarr"
    assert "state_read_ms" in out["phases"]
    assert "queue_lookup_paged_ms" in out["phases"]
    assert "queue_lookup_default_ms" in out["phases"]
    assert isinstance(out["phases"]["state_read_ms"], (int, float))
    # No event written (diagnose is pure-read)
    assert not list(events.glob("*.jsonl"))
```

- [ ] **Step 2: Run, expect failure**

```
pytest tests/unit/test_mcp_unstick.py::test_diagnose_returns_phase_timings -v
```

Expected: FAIL — `unstick.diagnose` does not exist.

- [ ] **Step 3: Implement `diagnose()` in `scripts/mcp/unstick.py`**

Add this function below `run()`:

```python
def diagnose(*, slug: str, hash_: str,
             state_file: Optional[Path] = None) -> dict:
    """Time each phase of unstick.py's pre-flight path. No DELETE, no event."""
    import time
    phases: dict = {}

    t0 = time.perf_counter()
    is_arr_red(slug, state_file=state_file)
    phases["state_read_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    if slug not in ARR_VERSIONS:
        return {"status": "diagnose", "slug": slug, "hash": hash_,
                "phases": phases, "error": "unknown-slug"}

    c = ArrClient(slug, ARR_VERSIONS[slug], timeout=30)

    t0 = time.perf_counter()
    code_p, payload_p = c.get("/queue",
                               query="pageSize=500&includeUnknownSeriesItems=true",
                               timeout=30)
    phases["queue_lookup_paged_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    queue_size_paged = len((payload_p or {}).get("records") or []) if isinstance(payload_p, dict) else 0

    t0 = time.perf_counter()
    code_d, payload_d = c.get("/queue", timeout=30)
    phases["queue_lookup_default_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    queue_size_default = len((payload_d or {}).get("records") or []) if isinstance(payload_d, dict) else 0

    t0 = time.perf_counter()
    resolved = _resolve_queue_item(c, hash_=hash_, queue_id=None)
    phases["hash_match_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    return {
        "status": "diagnose",
        "slug": slug, "hash": hash_,
        "phases": phases,
        "queue_size_paged": queue_size_paged,
        "queue_size_default": queue_size_default,
        "resolved_status": resolved.get("status"),
        "queue_lookup_paged_http_code": code_p,
        "queue_lookup_default_http_code": code_d,
    }
```

Then wire it into the CLI. In `main()`, add the `--diagnose` flag and dispatch. Replace the existing `main()` body with:

```python
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--emit-json", action="store_true")
    g.add_argument("--cron", action="store_true")
    ap.add_argument("--slug", required=True)
    ap.add_argument("--queue-id", type=int)
    ap.add_argument("--hash")
    ap.add_argument("--reason", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--diagnose", action="store_true",
                    help="time pre-flight phases without DELETE")
    ap.add_argument("--max-actions-per-day", type=int, default=10)
    args = ap.parse_args()
    if args.diagnose:
        if not args.hash:
            ap.error("--hash required with --diagnose")
        res = diagnose(slug=args.slug, hash_=args.hash)
    else:
        if not args.queue_id and not args.hash:
            ap.error("--queue-id or --hash required")
        res = run(slug=args.slug, queue_id=args.queue_id, hash_=args.hash,
                  reason=args.reason, dry_run=args.dry_run,
                  max_actions_per_day=args.max_actions_per_day)
    if args.emit_json:
        json.dump(res, sys.stdout, default=str)
        sys.stdout.write("\n")
    return 0
```

- [ ] **Step 4: Run tests, expect pass**

```
pytest tests/unit/test_mcp_unstick.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp/unstick.py tests/unit/test_mcp_unstick.py
git commit -m "feat(mcp): unstick.py --diagnose times pre-flight phases"
```

---

## Task 9: Add `qflix_diagnose_unstick` MCP tool + `timeout` param + structured ssh-timeout on write tools

**Files:**
- Modify: `scripts/local/qflix-mcp/qflix_mcp.py`
- Test: `tests/unit/test_qflix_mcp_tools.py`

Wrap the new `--diagnose` flag and lift the unstick timeout to 120s default. Surface SSH timeouts as `{status: "ssh-timeout", timeout_s: N}` across all three write tools so the caller can branch deterministically.

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_qflix_mcp_tools.py`:

```python
@patch("qflix_mcp.ssh_call")
def test_diagnose_unstick_returns_timings(mock_ssh):
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = json.dumps({
        "status": "diagnose", "slug": "radarr", "hash": "abc",
        "phases": {"state_read_ms": 0.5, "queue_lookup_paged_ms": 47000.0,
                    "queue_lookup_default_ms": 800.0, "hash_match_ms": 850.0},
        "queue_size_paged": 50, "queue_size_default": 50,
    })
    fake.stderr = ""
    mock_ssh.return_value = fake
    out = qflix_mcp.qflix_diagnose_unstick(slug="radarr", hash_="abc")
    assert out["status"] == "diagnose"
    assert out["phases"]["queue_lookup_paged_ms"] == 47000.0
    cmd = mock_ssh.call_args[0][0]
    assert "--diagnose" in cmd and "--slug radarr" in cmd and "--hash abc" in cmd
    # 180s timeout passed
    assert mock_ssh.call_args[1]["timeout"] == 180


@patch("qflix_mcp.ssh_call")
def test_unstick_torrent_returns_ssh_timeout_struct(mock_ssh):
    fake = MagicMock()
    fake.returncode = 124
    fake.stdout = ""
    fake.stderr = "ssh-timeout after 120s"
    mock_ssh.return_value = fake
    out = qflix_mcp.qflix_unstick_torrent(slug="radarr", hash_="abc")
    assert out["status"] == "ssh-timeout"
    assert out["timeout_s"] == 120


@patch("qflix_mcp.ssh_call")
def test_unstick_torrent_accepts_custom_timeout(mock_ssh):
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = '{"status": "deleted+blocklisted"}'
    fake.stderr = ""
    mock_ssh.return_value = fake
    qflix_mcp.qflix_unstick_torrent(slug="radarr", hash_="abc", timeout=200)
    assert mock_ssh.call_args[1]["timeout"] == 200


@patch("qflix_mcp.ssh_call")
def test_refresh_collect_returns_ssh_timeout_struct(mock_ssh):
    fake = MagicMock()
    fake.returncode = 124
    fake.stdout = ""
    fake.stderr = "ssh-timeout after 90s"
    mock_ssh.return_value = fake
    out = qflix_mcp.qflix_refresh_collect()
    assert out["status"] == "ssh-timeout"
    assert out["timeout_s"] == 90
```

- [ ] **Step 2: Run, expect failures**

```
pytest tests/unit/test_qflix_mcp_tools.py -v -k "diagnose_unstick or ssh_timeout_struct or custom_timeout"
```

Expected: all 4 new tests fail.

- [ ] **Step 3: Modify `qflix_unstick_torrent` and add `qflix_diagnose_unstick`**

Replace the existing `qflix_unstick_torrent` function with:

```python
def qflix_unstick_torrent(slug: str, queue_id: Optional[int] = None,
                          hash_: Optional[str] = None,
                          reason: str = "manual-via-mcp",
                          dry_run: bool = False,
                          timeout: int = 120) -> dict:
    """Manually unstick one *arr queue item (DELETE+blocklist+research).

    Use when: a specific torrent is wedged and you want to act now without
    waiting for the 3-hour rule.
    """
    args = [f"--slug {slug}", f'--reason "{reason}"']
    if queue_id is not None:
        args.append(f"--queue-id {int(queue_id)}")
    if hash_ is not None:
        args.append(f'--hash {hash_}')
    if dry_run:
        args.append("--dry-run")
    cmd = "python3 ~/scripts/mcp/unstick.py --emit-json " + " ".join(args)
    proc = ssh_call(cmd, timeout=timeout)
    if proc.returncode == 124:
        return _parse_ssh_timeout(proc.stderr, default=timeout)
    if proc.returncode != 0:
        return {"status": "ssh-failed", "code": proc.returncode,
                "stderr": proc.stderr[:300]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "bad-json", "stdout": proc.stdout[:300]}


def qflix_diagnose_unstick(slug: str, hash_: str) -> dict:
    """Time each phase of unstick.py's pre-flight path. No DELETE.

    Use when: unstick is hanging and you want to know which step is slow.
    Returns {status: "diagnose", phases: {state_read_ms, queue_lookup_paged_ms,
    queue_lookup_default_ms, hash_match_ms}, queue_size_*}.
    """
    cmd = (f"python3 ~/scripts/mcp/unstick.py --emit-json --diagnose "
           f"--slug {slug} --hash {hash_}")
    proc = ssh_call(cmd, timeout=180)
    if proc.returncode == 124:
        return _parse_ssh_timeout(proc.stderr, default=180)
    if proc.returncode != 0:
        return {"status": "ssh-failed", "code": proc.returncode,
                "stderr": proc.stderr[:300]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "bad-json", "stdout": proc.stdout[:300]}
```

Also update `qflix_trigger_missing_search` and `qflix_refresh_collect` to surface `ssh-timeout` (insert after the existing `proc = ssh_call(...)` lines, before `if proc.returncode != 0:`):

```python
    if proc.returncode == 124:
        return _parse_ssh_timeout(proc.stderr, default=120)  # use this default in trigger_missing_search
```

```python
    if proc.returncode == 124:
        return _parse_ssh_timeout(proc.stderr, default=90)  # use this default in refresh_collect
```

Match the default to the existing `timeout=` argument in each `ssh_call` invocation (120 for trigger_missing_search, 90 for refresh_collect).

Register the new tool — in `_build_server()` add after `server.tool()(qflix_unstick_torrent)`:

```python
    server.tool()(qflix_diagnose_unstick)
```

- [ ] **Step 4: Run tests, expect pass**

```
pytest tests/unit/test_qflix_mcp_tools.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/local/qflix-mcp/qflix_mcp.py tests/unit/test_qflix_mcp_tools.py
git commit -m "feat(mcp): qflix_diagnose_unstick + ssh-timeout struct + 120s default"
```

---

## Task 10: Deploy host-side script changes to seedbox

**Files:**
- None to modify; this task syncs the modified `scripts/mcp/*.py` to the host.

Before live testing the diagnose tool against the real seedbox, the modified `unstick.py`, `logs.py`, and `lib/arr_client.py` must be on the host.

- [ ] **Step 1: Identify the deploy mechanism**

Run from the repo root:

```
git log --oneline --all -- scripts/mcp/ | head -20
```

And look for any deploy/sync helper script:

```
ls scripts/local/*.ps1 scripts/local/*.sh 2>/dev/null | xargs grep -l "rsync\|scp\|deploy" 2>/dev/null
```

Look for an existing deploy command — possibly a script in `scripts/local/` or referenced in `README.md`.

- [ ] **Step 2: Sync the three modified files**

If a project-defined deploy script exists, use it. Otherwise, sync manually with `scp` over the same SSH config the MCP server uses:

```
scp scripts/mcp/unstick.py quadstronaut@<host>:~/scripts/mcp/unstick.py
scp scripts/mcp/logs.py quadstronaut@<host>:~/scripts/mcp/logs.py
scp scripts/mcp/lib/arr_client.py quadstronaut@<host>:~/scripts/mcp/lib/arr_client.py
```

The host name is in `secrets/seedbox.ssh-host` (read-only).

- [ ] **Step 3: Verify by SSHing and running `--help`**

```
ssh -o BatchMode=yes quadstronaut@<host> 'python3 ~/scripts/mcp/unstick.py --help 2>&1 | grep diagnose'
ssh -o BatchMode=yes quadstronaut@<host> 'python3 ~/scripts/mcp/logs.py --help 2>&1 | grep list-apps'
```

Expected: both print the new flag descriptions.

- [ ] **Step 4: No commit (deploy-only task)**

If any tooling files (deploy scripts) were modified, commit those — but the host-side `*.py` changes themselves are already committed earlier in this plan.

---

## Task 11: Run live diagnose against the seedbox and confirm root cause

**Files:**
- None to modify; this is an empirical verification step.

Goal: confirm the hypothesis that `pageSize=500&includeUnknownSeriesItems=true` is the slow query, by reading the phase timings from `qflix_diagnose_unstick`.

- [ ] **Step 1: Restart the MCP server so it picks up the local changes**

The MCP server is a long-running stdio process; the user's MCP client (Claude Code) needs to reconnect to load the new tools. Have the user reconnect or restart.

- [ ] **Step 2: Call the diagnose tool against the Radarr hash**

Run via the MCP client:

```
qflix_diagnose_unstick(slug="radarr", hash_="bdb9fa863641e6dba1f1f4db6961fdf41b8e53fe")
```

Expected: returns within 60s with `{status: "diagnose", phases: {...}}`.

- [ ] **Step 3: Interpret the phase timings**

Check the output:
- If `phases.queue_lookup_paged_ms >> phases.queue_lookup_default_ms` (e.g., 40000 vs 800): the slow query IS the root cause, and Task 12 (which switches to the default query shape) will fix it.
- If both are slow: a deeper *arr-side issue (DB lock, etc.) — escalate, do not proceed to Task 12 without diagnosing further.
- If `phases.state_read_ms` is high (e.g., > 5000): the `state.json` file is the problem — investigate file size or storage.

Document the actual numbers seen in a comment on the next commit message.

- [ ] **Step 4: No code change yet — proceed to Task 12 only if hypothesis confirmed**

If the phase data does not match the hypothesis, write up the unexpected result and ask the human for guidance before changing the lookup query. Don't blindly proceed with the planned fix.

---

## Task 12: Fix the slow `/queue` lookup query in `unstick.py`

**Files:**
- Modify: `scripts/mcp/unstick.py`
- Test: `tests/unit/test_mcp_unstick.py`

Drop `pageSize=500&includeUnknownSeriesItems=true` from `_resolve_queue_item`. The default query (matching what `qflix_arr_queue` cache uses) is fast. If `totalRecords > pageSize` in any response, follow `nextPage`-style pagination.

The Task 6 refactor already used the default `c.get("/queue", timeout=15)` shape in `_resolve_queue_item` — so this task is mostly verification. The remaining work is to handle multi-page queues correctly.

- [ ] **Step 1: Verify `_resolve_queue_item` already uses default query shape**

Open `scripts/mcp/unstick.py` and confirm the line inside `_resolve_queue_item`:

```python
    code, payload = c.get("/queue", timeout=15)
```

Has no `query="pageSize=500&..."`. (Should be the case from Task 6.)

- [ ] **Step 2: Write a failing test for pagination**

Append to `tests/unit/test_mcp_unstick.py`:

```python
@patch("lib.arr_client.urllib.request.urlopen")
def test_resolve_queue_item_follows_pagination(mock_open, tmp_path, monkeypatch):
    secrets, state, events = _setup(tmp_path)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    monkeypatch.setenv("QFLIX_MCP_EVENTS", str(events))
    import importlib; importlib.reload(unstick)
    # First page lacks the target; second page contains it.
    mock_open.side_effect = [
        _resp({"records": [{"id": 1, "downloadId": "OTHER", "title": "X"}],
                "page": 1, "pageSize": 1, "totalRecords": 2}),
        _resp({"records": [{"id": 2, "downloadId": "TARGET", "title": "Y"}],
                "page": 2, "pageSize": 1, "totalRecords": 2}),
    ]
    from lib.arr_client import ArrClient
    c = ArrClient("sonarr", "v3", secrets_dir=secrets)
    out = unstick._resolve_queue_item(c, hash_="target", queue_id=None)
    assert out["status"] == "found"
    assert out["queue_id"] == 2
```

- [ ] **Step 3: Run, expect failure**

```
pytest tests/unit/test_mcp_unstick.py::test_resolve_queue_item_follows_pagination -v
```

Expected: FAIL — current implementation only reads page 1.

- [ ] **Step 4: Update `_resolve_queue_item` for pagination**

Replace the function body with a pagination-aware version:

```python
def _resolve_queue_item(c: ArrClient, *, hash_: Optional[str],
                        queue_id: Optional[int]) -> dict:
    """Find the queue item by hash or id. Walks paginated /queue results."""
    target_hash = hash_.lower() if hash_ else None
    page = 1
    seen = 0
    while True:
        query = f"page={page}" if page > 1 else ""
        code, payload = c.get("/queue", query=query, timeout=15)
        if code != 200 or not isinstance(payload, dict):
            return {"status": "queue-fetch-failed", "code": code}
        records = payload.get("records") or []
        for q in records:
            if target_hash is not None:
                if (q.get("downloadId") or "").lower() == target_hash:
                    return {"status": "found",
                            "queue_id": q.get("id"),
                            "title": q.get("title", "?"),
                            "hash": q.get("downloadId")}
            elif queue_id is not None and q.get("id") == queue_id:
                return {"status": "found",
                        "queue_id": queue_id,
                        "title": q.get("title", "?"),
                        "hash": q.get("downloadId")}
        seen += len(records)
        total = payload.get("totalRecords", seen)
        if seen >= total or not records:
            return {"status": "already-removed"}
        page += 1
        if page > 50:  # hard safety cap
            return {"status": "already-removed"}
```

- [ ] **Step 5: Run all unstick tests, expect pass**

```
pytest tests/unit/test_mcp_unstick.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/mcp/unstick.py tests/unit/test_mcp_unstick.py
git commit -m "fix(mcp): unstick uses default /queue + follows pagination"
```

---

## Task 13: Deploy and verify the unstick fix on live host

**Files:**
- None to modify; live verification.

- [ ] **Step 1: Sync the updated `unstick.py` to the host**

Per Task 10's mechanism, push `scripts/mcp/unstick.py` to `~/scripts/mcp/unstick.py` on the seedbox.

- [ ] **Step 2: Run a dry-run against the Radarr hash**

Via the MCP client:

```
qflix_unstick_torrent(slug="radarr", hash_="bdb9fa863641e6dba1f1f4db6961fdf41b8e53fe", dry_run=True)
```

Expected: returns within 30s with `{status: "dry-run", pre: {queue_id: ..., title: "The Bobs Burgers Movie ...", hash: "bdb9fa..."}}`.

- [ ] **Step 3: Run the three real unsticks**

```
qflix_unstick_torrent(slug="radarr", hash_="bdb9fa863641e6dba1f1f4db6961fdf41b8e53fe", reason="manual-via-mcp: stalled, no connections")
qflix_unstick_torrent(slug="sonarr", hash_="62ccd57824da9cd96444759142df13361cccc5df", reason="manual-via-mcp: stalled, 99.6% done")
qflix_unstick_torrent(slug="sonarr", hash_="b1ae88c9451923f61f7b9bd28029d7a2dea8d3c0", reason="manual-via-mcp: stalled, 99.99% done")
```

Expected each: `{status: "deleted+blocklisted", pre: {...}}` within 30s.

- [ ] **Step 4: Verify `acted_on_at` populated**

```
qflix_list_stale()
```

Expected: the 3 hashes either disappear from the candidate list or show non-null `acted_on_at`. (Either is acceptable depending on how `qflix-collect.ps1` reconciles the state.)

- [ ] **Step 5: Verify event rows recorded**

```
qflix_recent_events(n=10)
```

Expected: 3 unstick rows newest-first, each with `result: "deleted+blocklisted"` and the corresponding hash.

- [ ] **Step 6: No commit (verification-only task)**

---

## Task 14: Add `meta-stuck` rule to `qflix-collect.ps1`

**Files:**
- Modify: `scripts/local/qflix-collect.ps1`

Add a new candidacy path: torrents in `metaDL` state with `size_bytes==0` AND added more than 24 hours ago are immediately flagged with `rule_matched="meta-stuck"`. Uses `added_on` instead of N-consecutive-sample analysis because qBit metadata-stuck magnets have a stable "never resolved" signature — the time-based check is equivalent in practice and avoids a load-24-snapshots refactor.

There is no PowerShell test framework in this repo, so verification is via state-file inspection after a real run.

- [ ] **Step 1: Read the current rule-3 (bad-grab) block in `qflix-collect.ps1`**

Familiarize yourself with lines ~251-273 (the existing bypass-the-3-hour-wait pattern for `bad_grab_signals`).

- [ ] **Step 2: Insert the meta-stuck block after the bad-grab block**

In `scripts/local/qflix-collect.ps1`, after the closing `}` of the bad-grab `foreach` loop (around line 273) and before `$out = @{ hashes = $hashes; ...`, insert:

```powershell
    # Rule 5 (meta-stuck): metaDL torrents whose metadata never resolved.
    # Triggers when state=='metaDL' AND size_bytes==0 AND added_on >= 24h ago.
    # Uses elapsed wall time rather than N-consecutive-samples since the
    # "never got metadata" signature is stable across snapshots.
    $nowEpoch = [int][DateTime]::UtcNow.Subtract([DateTime]::new(1970,1,1)).TotalSeconds
    foreach ($t in $latestSnap.qbit.torrents) {
        if ($t.state -ne 'metaDL') { continue }
        if ($t.size_bytes -ne 0) { continue }
        if (-not $t.added_on) { continue }
        $ageSeconds = $nowEpoch - [int]$t.added_on
        if ($ageSeconds -lt 86400) { continue }
        $h = $t.hash
        if ($hashes.ContainsKey($h) -and $hashes[$h].acted_on_at) { continue }
        if ($hashes.ContainsKey($h)) { continue }
        $hashes[$h] = @{
            first_zero_movement_at = ([DateTime]::UtcNow.ToString("o"))
            consecutive_zero_hours = [int]($ageSeconds / 3600)
            last_progress          = $t.progress
            rule_matched           = 'meta-stuck'
            candidate_for_unstick  = $true
            acted_on_at            = $null
        }
        $candidates += $h
    }
```

- [ ] **Step 3: Manually run the aggregator and inspect output**

From the workstation (Windows):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\local\qflix-collect.ps1
```

Then inspect `B:\QFlix\data\stale-state.json` — search for any entry with `"rule_matched": "meta-stuck"`. If the 5 hashes in the spec are present in qBit with state=`metaDL`, `size_bytes`=0, and `added_on` >= 24h ago, they should appear.

To verify rapidly via the MCP without staring at JSON:

```
qflix_list_stale()
```

Expected: includes entries for the 5 meta-stuck hashes:
- `ec47ce4115d23d287b7b387706360342057a4ca1`
- `64875cd622d574bc9763d222bf863ee3221b12c3`
- `a2c1277f2aba2a522925bf7c6de952816827a674`
- `8bb3bb41b8a9ce51d294cbae1b57d22c587b307b`
- `66e908daa8e60dcfee477dedee55bcefe41f1e08`

with `rule_matched: "meta-stuck"`.

- [ ] **Step 4: Commit**

```bash
git add scripts/local/qflix-collect.ps1
git commit -m "feat(collect): meta-stuck rule — metaDL torrents 24h+ old"
```

---

## Task 15: Unstick the 5 metadata-stuck magnets

**Files:**
- None to modify; live operational step.

- [ ] **Step 1: Confirm the 5 hashes are still in the *arr queues**

```
qflix_arr_queue(slug="radarr")
qflix_arr_queue(slug="sonarr")
```

Confirm each of the 5 hashes from Task 14 is still present with `errorMessage: "qBittorrent is downloading metadata"`. If any have already self-resolved or been removed, skip those.

- [ ] **Step 2: Trigger unsticks**

```
qflix_unstick_torrent(slug="radarr", hash_="ec47ce4115d23d287b7b387706360342057a4ca1", reason="meta-stuck: no metadata 24h+")
qflix_unstick_torrent(slug="radarr", hash_="64875cd622d574bc9763d222bf863ee3221b12c3", reason="meta-stuck: no metadata 24h+")
qflix_unstick_torrent(slug="sonarr", hash_="a2c1277f2aba2a522925bf7c6de952816827a674", reason="meta-stuck: no metadata 24h+")
qflix_unstick_torrent(slug="sonarr", hash_="8bb3bb41b8a9ce51d294cbae1b57d22c587b307b", reason="meta-stuck: no metadata 24h+")
qflix_unstick_torrent(slug="sonarr", hash_="66e908daa8e60dcfee477dedee55bcefe41f1e08", reason="meta-stuck: no metadata 24h+")
```

Expected each: `{status: "deleted+blocklisted"}` within 30s. The daily 10-action cap allows up to 10/day — 5 fits comfortably.

If the cap is hit (some other unstick already ran today), some will return `{status: "refused-cap-hit"}` — wait until the next UTC day or run the remainder manually.

- [ ] **Step 3: Verify via `qflix_recent_events`**

```
qflix_recent_events(n=15)
```

Expected: 8 recent unstick events total (3 from Task 13 + 5 from this task).

- [ ] **Step 4: No commit (operational verification)**

---

## Task 16: End-to-end sanity check

**Files:**
- None to modify; whole-suite verification.

- [ ] **Step 1: Run the full unit test suite**

```
pytest tests/unit/ -v
```

Expected: all tests pass (the existing suite plus everything added in Tasks 1–12).

- [ ] **Step 2: Exercise every new or modified MCP tool against the live host**

Run each via the MCP client:

```
qflix_list_log_apps()
qflix_get_logs(app="qbittorrent", since="2h", tail=20)
qflix_get_logs(app="sonarr", since="6h", tail=50, grep="warn")
qflix_diagnose_unstick(slug="radarr", hash_="<any-current-radarr-queue-hash>")
qflix_status()
```

Expected: all return structured non-empty results within the bumped timeouts. None hang.

- [ ] **Step 3: Confirm `qflix_status.recent_actions_24h` > 0**

After Tasks 13 and 15, this counter should reflect the manual unsticks. Confirms the events file is being read correctly.

- [ ] **Step 4: No commit (verification-only)**

---

## Self-Review Summary

**Spec coverage:**
- §1 SSH timeout hardening → Task 1 ✓
- §2 `qflix_get_logs` rewrite → Task 4 ✓
- §2 `qflix_list_log_apps` → Task 3 ✓
- §2 `qflix_diagnose_unstick` → Task 9 ✓
- §2 `qflix_unstick_torrent` timeout param → Task 9 ✓
- §2 ssh-timeout struct across write tools → Task 9 ✓
- §3 `logs.py --list-apps` → Task 2 ✓
- §4 `unstick.py` `run()` decomposition → Task 6 ✓
- §4 Lookup-query fix → Tasks 6 (uses default shape) + 12 (pagination) ✓
- §4 `--diagnose` flag → Task 8 ✓
- §4 ArrClient timeout → Task 5 ✓
- §4 Always-emit events → Task 7 (verifies Task 6) ✓
- §5 `meta-stuck` rule → Task 14 ✓
- Live verification (3 stale + 5 meta-stuck) → Tasks 11, 13, 15 ✓

**Placeholder scan:** None. Every step contains the actual code or command needed.

**Type consistency:** `_preflight` / `_resolve_queue_item` / `_execute_delete` / `_record_event` signatures match across Tasks 6, 8, 12. `qflix_diagnose_unstick(slug, hash_)` matches `unstick.diagnose(slug, hash_)` arg names. `_parse_ssh_timeout(stderr, default)` is consistently used in Tasks 3, 4, 9.

**Scope:** Single focused enhancement. No decomposition needed.
