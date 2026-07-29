# The Convergent Audit Regime

> **What this delivers:** run-to-run determinism. Two runs of `qflix-audit`
> against the same commit produce a **byte-identical `report_digest`**. "Will
> you find new stuff?" becomes "did the digest change?" — one hex string,
> comparable by eye.
>
> **What this does NOT deliver:** "no new findings ever again." That is not
> achievable and this document will not pretend otherwise. Residual risks
> **R1–R6** remain, permanently, and are enumerated in
> [`docs/audit-residual-risk.md`](audit-residual-risk.md). Upstream software
> will change, the live box will drift, semantic defects have no syntactic
> signature, two script bodies live outside git, CI itself is unwatched, and
> nobody has thought of every defect class yet.

---

## Why "audit qflix" was non-deterministic

It was an **LLM search with no oracle, no ledger, no taxonomy and no declared
boundary**. Re-running it re-sampled the same space, so it produced new results
every time — not because the system changed, but because the sampler did.

The repo already documented the mechanism, in its own words, at
`scripts/local-llm/qflix-rea.ps1:61-65`:

> *"a prompt is advisory and consensus has no floor: on 2026-07-28
> qwen3-coder:30b reported the tdarr … TypeError that the prompt EXPLICITLY
> forbids, and one model out of three was enough to page."*

That is measured run-to-run variance on **identical input**, already known and
already fought at the REA layer — with none of REA's enforcement layer applied
to a Claude session running "audit qflix".

Four more causes, all confirmed against the tree:

| cause | evidence |
|---|---|
| No coverage ledger | Nothing recorded what was audited, at what depth, with what result. The 2026-07-27 audit declared its scope in **prose inside a commit message** (`15bc997`) — unqueryable, and demonstrably false the next day. |
| No defect-class taxonomy | The regression file was named after a **date** (`tests/unit/test_audit_2026_07_27.py`), not a class. Its own docstring says it covers the "highest-risk NEW" logic: sampling language, in the test suite. |
| Prose findings + one-off fixes | `ad8198e`: `qflix_status` and `qflix_arr_queue` computed snapshot freshness; three **sibling** functions in the **same file** had none and served a 19-day-stale snapshot as live. Fixed once, per instance, never generalised. |
| Unstated enumeration boundary | `manitoba-maint-flaresolverr-canary.timer` exists on disk but is absent from `manifest/apps.yaml:canaries`. Whether an audit "saw" it depended on which files that run happened to open. Two runs, two answers, neither buggy. |

## The guarantee

For every defect class **C** enrolled in `manifest/defect-classes.yaml`, the
detector for C enumerates **100% of instances** inside a declared,
machine-computable boundary **B(C)**; it is deterministic (no LLM, no network,
no wall-clock in the compared output); and it runs in CI on every push.

Therefore a finding can only be *new* when **(a)** an input changed or **(b)** a
class was newly enrolled. Both are attributable and both are logged.

## Artifacts

| artifact | what it is |
|---|---|
| `manifest/audit-scope.yaml` | Every audit surface (S1–S5), its host, its enumerability, the partition of every tracked path into exactly one area, the CI execution map, and the residual register. |
| `manifest/defect-classes.yaml` | The taxonomy. Per class: id, provenance, detector, boundary, status, waivers. |
| `manifest/decommissioned.yaml` | Retired components and where their names may still legitimately appear. |
| `manifest/jobs.yaml` | The timer ↔ dead-man ledger. Every timer declares a monitor or a written reason. |
| `manifest/rea-noise-classes.yaml` | The REA noise policy, extracted from the gitignored ps1 so it lives in git even though the script does not. |
| `scripts/maint/qflix-audit.py` | The runner. `--json` emits `{meta, coverage, findings, waived, audit_log, summary, report_digest}`. |
| `scripts/maint/lib/audit/` | Detectors, engine, ledger validation, digest. Stdlib + pyyaml. Offline. |
| `tests/unit/test_audit_regime.py` | **The meta-check — this is what audits the monitor.** |
| `docs/audit-residual-risk.md` | The honest half. One row per un-enumerable surface. |

## Exit codes

| code | meaning |
|---|---|
| **0** | No **enforced** findings. An advisory backlog may exist and is reported. |
| **1** | **FINDINGS** — at least one finding in an enforced class. |
| **2** | **REGIME INTEGRITY** — the auditor itself is broken: taxonomy↔detector bijection, scope partition, waiver or residual discipline, CI-map drift, or digest hygiene. |

**1 and 2 are deliberately different.** "The auditor is broken" must never look
like "the auditor found nothing". The Kuma monitor pushes DOWN for both, with a
message that names which.

## The enrolled classes

| id | class | status at landing | boundary |
|---|---|---|---|
| C-01 | timer-without-deadman | **enforced** | every tracked `scripts/*/systemd/*.timer` ∪ `manifest/jobs.yaml` |
| C-02 | unchecked-subprocess | advisory | every `subprocess.{run,call,Popen}` call in `scripts/**/*.py` |
| C-03 | error-swallowing-handler | advisory | every `ast.ExceptHandler` in `scripts/**/*.py` |
| C-04 | mtime-freshness | advisory | mtime accessors across `scripts/**`, scored for freshness context |
| C-05 | sibling-inconsistent-envelope | advisory | functions of modules that define a freshness helper |
| C-06 | doc-count-drift (exhaustive) | advisory | every `<N> <noun>` claim on four doc surfaces |
| C-07 | prompt-vs-rule-table-contradiction | **enforced** | `rea-noise-classes.yaml` × the ps1 when present |
| C-08 | decommissioned-still-referenced | advisory | `decommissioned.yaml` × every tracked text file |
| C-09 | silent-exit-on-missing-prerequisite | advisory | clean-exit sites in canaries + self-pushing jobs |
| C-10 | test-not-in-CI / subject-not-tracked | **enforced** | tracked test files ∪ their subjects ∪ declared CI jobs |
| L-01…L-06 | live classes | **residual** | box state; `scripts/maint/qflix-audit-live.py` (Phase 5) |

`advisory` is not a synonym for *ignored*. An advisory class **enumerates
exhaustively and reports**; its findings simply do not fail the build, because
its backlog has not been adjudicated yet. A class flips to `enforced` when its
backlog reaches zero — and from that moment it cannot surface a new finding
without an input change. That flip is the whole migration.

## Waivers, and why nothing is silenced quietly

A waiver needs an `id`, a `match` selector, an `owner`, a `date`, and a `reason`
of at least 40 characters. Anything less fails the meta-check (exit 2).

Every applied waiver appears **twice**: in the JSON `waived[]` array with its
full reason, and as a line in `audit_log[]` (and in
`~/.opt/maint/audit/qflix-audit.log`) carrying its class id and rule id. A
waived instance is **never merely absent** from the output. That is the second
design law — every suppression is written down with its rule id.

## The digest

`report_digest` is SHA-256 over the canonical findings body: classes sorted by
id, instances sorted by `(path, lineno, instance_id, kind)`, no timestamps, no
absolute paths, no hostnames, no dict-iteration order. `meta{}` — including
`generated_at`, `host` and `commit` — is **excluded**, so changing any of it
cannot change the string.

Hygiene is enforced in **production code**, not only in a test: if a detector
ever leaks an absolute path, an FQDN, an ISO timestamp or a PID into a finding,
`lib/audit/model.assert_digest_hygiene` raises and the audit exits 2. A digest
that churns for an indefensible reason is worse than no digest.

One deliberate design note: the REA cross-check against the untracked ps1 is
collapsed into a **single** verdict whose OK text is identical whether the
subject was present-and-matching or absent-and-unchecked. A per-rule verdict
stream — or even two different detail strings — would make the digest depend on
*which machine ran the audit*, and the operator's one-hex-string comparison
would break for a reason that has nothing to do with the code. Verified: the
digest is byte-identical with and without `scripts/local-llm/qflix-rea.ps1`
present.

Where the skip is reported instead: **`meta.s2_subjects`**, a
`{path: present}` map printed in `--json` and called out loudly in the human
summary ("S2 SUBJECTS ABSENT ON THIS HOST — their cross-checks DID NOT RUN").
Environment belongs in `meta`, which is outside the digest. A real policy drift
is *not* environment and still flips the digest.

## Operating it

```bash
python3 scripts/maint/qflix-audit.py            # human summary
python3 scripts/maint/qflix-audit.py --json     # machine report
python3 scripts/maint/qflix-audit.py --digest-only
```

On the box it runs from `manitoba-maint-audit.timer` (daily 06:30 UTC) and
self-pushes the Kuma monitor **"QFlix Audit Regime"**, registered in
`lib/kuma.py:STANDALONE_SELF_PUSH_MONITORS`.

> **Deploy note.** Until `bootstrap-kuma-monitors.py` has been run on the box,
> `manitoba-maint kuma audit` reports `QFlix Audit Regime` as `manifest_only`.
> That is a **true** finding: the repo declares the intent and the monitor
> genuinely does not exist yet. Deploying closes it.

## What audits the auditor

`tests/unit/test_audit_regime.py` asserts, as hard invariants:

- **bijection** — every class names a detector that exists and imports; every
  detector module is claimed by exactly one class; every class names a
  pytest-collectable, git-tracked test module;
- **CI bijection** — every tracked test file is executed by ≥1 CI job, and the
  declared CI map matches the real workflow file;
- **closure** — every file a test imports or dot-sources is git-tracked, or is
  an S2 member with a residual reason and an owner;
- **coverage** — every git-tracked path maps to exactly one scope area or an
  explicit out-of-scope entry;
- **freshness** — every residual has an owner, a cadence, a `last_reviewed`
  inside that cadence, and a row in the residual register.

And the honest limit on that: **the CI job itself is unwatched (R5).** If
GitHub Actions stops running, all of the above stays green.
