# Tuesday session — extend Monday 04:00 maintenance to systemd-installed apps

> **Status (2026-05-16): SUPERSEDED.** The cp.ultra.cc Playwright clicker
> was replaced by `scripts/maint/app-upgrade-all.sh` (2026-05-13), which
> runs `app-<name> upgrade` for every UCC-managed app sequentially during
> the Monday window. The systemd/cron/library-class apps documented below
> (conjurr, newsletterr — now purged; kometa, listmonk, recyclarr,
> tdarr-server, python-plexapi) all upgrade via their own existing paths
> (cron timers, dedicated installers, or pip). The "extend window to
> systemd apps" plan never landed in this form — the audit cycle proved
> the per-class upgrade paths were already sufficient.
>
> The original design is kept below as historical reference. Do **not**
> implement from this document — see the live state in `manifest/apps.yaml`
> + `scripts/maint/app-upgrade-all.sh`.

---

**Status (original):** design doc. Plan to execute on a Tuesday night before the next Monday window.

**Goal:** make the 04:00–08:00 Monday window upgrade *every* manitoba-managed app, not just the 12 UCC apps the cp.ultra.cc clicker handles. "Production-grade automated maintenance" = nothing manual.

---

## Scope — apps not currently auto-upgraded

The cp clicker covers 12 UCC apps via `Upgrade & Repair`. Everything else upgrades manually today. The list of un-automated apps from `manifest/apps.yaml`, grouped by upgrade kind:

| Upgrade kind     | Apps                                                                                            | Source of latest |
|------------------|-------------------------------------------------------------------------------------------------|------------------|
| `git_checkout`   | conjurr, newsletterr, kometa                                                                    | upstream `main`/`master` (default branch HEAD) |
| `tarball_swap`   | listmonk, recyclarr                                                                             | GitHub Releases API → latest tag |
| `zip_swap`       | tdarr-server, tdarr-node                                                                        | **PINNED 2.17.01 — DO NOT upgrade**, GLIBC blocker |
| `pip_install`    | python-plexapi                                                                                  | PyPI latest |
| **NOT IN MANIFEST** — installed via unofficial-installers, behavior TBD | sonarr2, radarr2, audiobookshelf, kavita, komga, calibre-web, flaresolverr | varies — investigate per app |

The "NOT IN MANIFEST" group is the unknown — those apps were installed via the unofficial-installer scripts (e.g. `app-sonarr2 install`). They may have their own update verbs or may need git/binary-swap treatment.

---

## Design — per upgrade kind

The pin-lift policy already removed `version_pin` blocks from the manifest for everything except Tdarr. `lib/lifecycle.py` currently raises `LifecycleError` when no pin is configured. **We need a "use latest" path per upgrade kind**, triggered when no pin is set.

### `git_checkout` (conjurr, newsletterr, kometa)

Today: `_resolve_target_version` raises if no pin. With no pin, we want HEAD of the default branch.

Change in `lib/lifecycle.py::_apply_git_checkout`:
1. If no pin → run `git -C <repo> remote show origin | grep "HEAD branch"` to discover the default branch (`main`, `master`, or anything else)
2. `git -C <repo> fetch --prune origin`
3. `git -C <repo> checkout <default_branch>` (for repos that may be on a tag)
4. `git -C <repo> reset --hard origin/<default_branch>`
5. Run `post_steps`
6. `target_version` recorded in state = the new HEAD SHA (`git rev-parse HEAD`) so rollback can checkout the previous SHA on health failure

State change: instead of `state["apps"][name]["previous_version"] = "v3.10.1"`, it becomes the SHA. Rollback is `git checkout <prev-sha>`.

### `tarball_swap` (listmonk, recyclarr)

Today: URL template uses `{version}` which has to be filled.

Add a `latest_source` field per upgrade config:
```yaml
upgrade:
  kind: tarball_swap
  url_template: "https://github.com/knadh/listmonk/releases/download/v{version}/listmonk_v{version}_linux_amd64.tar.gz"
  latest_source:
    kind: github_release
    repo: "knadh/listmonk"
```

Resolver in lifecycle: GET `https://api.github.com/repos/<repo>/releases/latest`, return `tag_name.lstrip("v")`. Cache the result for 1h to avoid rate limits.

### `pip_install` (python-plexapi)

Trivial: drop the `==<version>` constraint when no pin. `pip install --upgrade plexapi` — pip resolves latest.

`previous_version` recording: capture pre-upgrade version via `pip show plexapi | grep Version` so rollback can `pip install plexapi==<prev>`.

### `zip_swap` (Tdarr — KEEP PINNED)

Tdarr stays pinned at 2.17.01. The version_pin block is intentional. **Do not change** Tdarr's behavior — the upgrade flow remains pin-driven for it.

### Unofficial-installer apps (Sonarr2, Radarr2, Audiobookshelf, Kavita, Komga, Calibre-Web, Flaresolverr)

**Investigation pass first** — these aren't in the manifest at all. Tuesday work has two phases for each:

**Phase A (per-app discovery):**
1. SSH to seedbox, find the app's binary/repo path (`~/.apps/<name>/`)
2. Determine update mechanism — does the install script have an update path? Is there a service unit that invokes it? Check `~/.apps/<name>/install.sh`, look for git repos, look for binary symlinks
3. Check whether the app has a `app-<name> update` verb on Ultra.cc (some unofficial installers register one)

**Phase B (manifest extension):**
1. Add the app to `manifest/apps.yaml` with `class: systemd` and the right `upgrade.kind`
2. If GitHub-Releases-driven → tarball_swap with `latest_source: github_release`
3. If git-clone-and-build → git_checkout with `latest_source: git_default_branch`
4. If neither (e.g. binary downloaded from a non-GitHub URL) → write a custom `latest_source` resolver per app

---

## Implementation plan

### 1. Lifecycle changes (`lib/lifecycle.py`)

Add `_resolve_latest_for_kind(app)`:
- `git_checkout` → `_resolve_git_default_branch_head(repo_path)`
- `tarball_swap` → `_resolve_github_release_latest(repo)`
- `pip_install` → returns sentinel `"latest"`; pip handles it
- `zip_swap` → raises (Tdarr only, must be pinned)

Modify `_resolve_target_version`:
```python
def _resolve_target_version(app, explicit):
    if explicit:
        return explicit
    pin = app.upgrade.version_pin if app.upgrade else None
    if pin and pin.source and pin.key:
        v = _read_versions_env(pin.key)
        if v:
            return v
    # No pin → derive from latest_source if configured
    if app.upgrade and app.upgrade.raw.get("latest_source"):
        return _resolve_latest_for_kind(app)
    raise LifecycleError(...)
```

Modify `_apply_pip_install` to skip the `==<version>` clause when version is `"latest"`.

### 2. Manifest updates (`manifest/apps.yaml`)

For each unpinned app, add a `latest_source` block:
```yaml
listmonk:
  upgrade:
    kind: tarball_swap
    url_template: ...
    latest_source: { kind: github_release, repo: "knadh/listmonk" }
```

For unofficial-installer apps, add new manifest entries (Phase B per-app).

### 3. Rollback adjustments (`lib/lifecycle.py` + `lib/state.py`)

`previous_version` semantics depend on kind:
- git_checkout: store the SHA pre-upgrade
- tarball_swap: store the version-string pre-upgrade
- pip_install: store the package version pre-upgrade

`lifecycle.downgrade` already takes a `previous_version` arg — just make sure the kind-specific apply functions handle SHA-vs-tag inputs correctly.

### 4. New systemd unit: `manitoba-maint-upgrade-sweep.service` + `.timer`

Fires Monday 05:00 (after the cp clicker at 04:30 but inside the maintenance window). Runs:
```
manitoba-maint upgrade --all --auto
```

`--auto` mode iterates every manifest app whose class is in {systemd, cron, library} (UCC is handled by the cp clicker), calls `lifecycle.upgrade(app, target_version=None)` (which now resolves latest), records state, rolls back on health-failure.

Notifiarr summary at end: per-app result.

### 5. Tests

For each `latest_source` resolver — unit test with a mocked HTTP response. For lifecycle's "no pin → use latest" path — integration test with a fake manifest.

---

## Order of execution (Tuesday night ~3-4 hours)

1. **Phase 1 (1h)** — investigation pass on the 11 unofficial-installer apps. Build a per-app upgrade-mechanism table. Add to this doc.
2. **Phase 2 (1h)** — manifest updates: add `latest_source` to existing 5 systemd/cron/library apps; add new manifest entries for the 11 investigated apps.
3. **Phase 3 (1h)** — code changes in lifecycle.py for the four resolver patterns + the `--all --auto` upgrade verb.
4. **Phase 4 (30m)** — systemd timer + service + 240-install.sh integration.
5. **Phase 5 (30m)** — dry-run end-to-end on the seedbox; pick one app and test live.

Land before next Monday's 04:00 window. First production run that following Monday.

---

## Risks + mitigations

- **Upstream main branch unstable** — apps like Kometa pull from `main`; HEAD might be broken. Mitigation: post-upgrade health probe + auto-rollback to previous SHA. Already wired in lifecycle.
- **GitHub rate limits on unauthenticated API** — 60 req/h per IP. Mitigation: cache `latest_source` results for 1h; only 17 apps + monthly upgrade frequency = trivial usage.
- **Unofficial-installer apps with no clear update path** — some may need manual intervention. Mitigation: skip those in `--auto` mode and Notifiarr-warn; operator handles them on their own cadence.
- **Failed upgrade leaves app stopped** — recovery loop already handles this via `pusher → recovery.run → 3-attempt restart`. Auto-rollback is the additional safety.
- **Window doesn't have time** — 240-min window. cp clicker 12 apps × ~2min each = 24min. systemd-app sweep ~17 apps × ~2min each = 34min. Total ~60min of upgrade work, leaves 3h margin.

---

## Out of scope (do later)

- Automatic dependency-graph ordering (e.g. upgrade Plex before TitleCardMaker). Today's design upgrades in manifest order; if ordering matters, we add a `depends_on` field later.
- Multi-version testing pipelines / canary upgrades. Production = the prod environment.
- Rollback-of-rollback if downgrade itself fails. Today the operator handles that.
