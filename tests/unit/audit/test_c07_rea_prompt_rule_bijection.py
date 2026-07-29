"""C-07 — the REA policy is in git, and the two copies cannot contradict.

This is the class whose absence caused the 2026-07-29 finding: three classes
the prompt listed as "must NEVER report" had no enforcement rule. The prompt was
asking, not enforcing, and neither copy was in git so nothing could compare them.

Every assertion here runs with NO ps1 present, which is the whole point.
"""
from __future__ import annotations

import re

import yaml

from lib.audit.detectors import c07_rea_prompt_rule_bijection as det
from lib.audit.model import FINDING, OK

EXPECTED_IDS = {
    "plex-client-abort-stream-write",
    "plex-nat-pmp-upnp",
    "tdarr-express-undefined-includes",
    "tdarr-worker-not-a-function",
    "tdarr-wasm-oom",
    "mediainfo-failure",
    "plex-post-reap-scan",
    "external-indexer-5xx-html",
    "indexer-severity-field-echo",
    "bare-stack-continuation",
}


def test_policy_file_carries_every_noise_class(ledgers):
    ids = {c["id"] for c in ledgers.rea["classes"]}
    assert EXPECTED_IDS <= ids, "missing from the tracked policy: " + str(
        sorted(EXPECTED_IDS - ids))


def test_no_findings_at_head(ctx):
    result = det.detect(ctx)
    findings = [v for v in result.verdicts if v.status == FINDING]
    assert findings == [], [f.detail for f in findings]


def test_every_prompt_segment_has_an_enforcement_rule(ctx, ledgers):
    """The exact 2026-07-29 root cause. A segment with no rule means the prompt
    is asking a stochastic model to hold a floor it demonstrably cannot."""
    result = det.detect(ctx)
    seg_verdicts = [v for v in result.verdicts if ":segment:" in v.instance_id
                    and v.path.endswith(".yaml")]
    assert len(seg_verdicts) == len(ledgers.rea["prompt_segments"])
    assert all(v.status == OK for v in seg_verdicts)


def test_every_rule_is_claimed_by_exactly_one_segment(ledgers):
    claimed = {}
    for seg in ledgers.rea["prompt_segments"]:
        for cid in seg["classes"]:
            claimed.setdefault(cid, []).append(seg["index"])
    for c in ledgers.rea["classes"]:
        assert len(claimed.get(c["id"], [])) == 1, (
            "class " + c["id"] + " claimed by " + str(claimed.get(c["id"])))


def test_every_rx_compiles(ledgers):
    for c in ledgers.rea["classes"]:
        re.compile(c["rx"])


def test_rx_escaping_survived_the_yaml_round_trip(ledgers):
    """A YAML single-quoted scalar doubles inner quotes. If the un-doubling is
    wrong the rule silently never matches — a suppression that suppresses
    nothing, which is indistinguishable from noise coming back."""
    by_id = {c["id"]: c for c in ledgers.rea["classes"]}
    rx = by_id["tdarr-express-undefined-includes"]["rx"]
    # YAML wrote ''includes''; the loaded scalar must carry single quotes, and
    # the regex escaping of the parens must have survived untouched.
    assert "'includes'" in rx and "''" not in rx
    assert r"\(reading" in rx and r"\)" in rx
    assert '"severity"' in by_id["indexer-severity-field-echo"]["rx"]
    assert by_id["bare-stack-continuation"]["field"] == "excerpt"


def test_rules_match_their_canonical_log_lines(ledgers):
    """The rules must actually fire. A bijection between two lists that both
    describe nothing would pass every structural check above."""
    by_id = {c["id"]: c["rx"] for c in ledgers.rea["classes"]}
    cases = [
        ("tdarr-express-undefined-includes",
         "TypeError: Cannot read properties of undefined (reading 'includes')"),
        ("tdarr-wasm-oom", "WebAssembly.instantiate(): Out of memory: wasm memory"),
        ("mediainfo-failure", "Error running MediaInfo on /data/x.mkv"),
        ("plex-post-reap-scan", "Failed to create parent iterator"),
        ("indexer-severity-field-echo", '{"severity": "error"}'),
        ("plex-nat-pmp-upnp", "NAT-PMP is not supported by the gateway"),
        ("plex-client-abort-stream-write",
         "Caught exception trying to stream file: protocol is shutdown (SSL routines)"),
        ("external-indexer-5xx-html", "<center>nginx/1.18.0</center>"),
        ("tdarr-worker-not-a-function", "worker2.getStatus is not a function"),
    ]
    for cid, hay in cases:
        assert re.search(by_id[cid], hay), cid + " no longer matches its log line"


def test_a_real_fault_is_not_suppressed(ledgers):
    """The direction that actually costs money: an over-broad rule silences a
    page. Disk-quota failures are the prompt's explicit MUST-report class."""
    real = "Unknown system error -122: Disk quota exceeded writing /data/Movies"
    for c in ledgers.rea["classes"]:
        if c["field"] == "excerpt":
            continue  # excerpt-scoped negative-lookahead rule, tested below
        assert not re.search(c["rx"], real), c["id"] + " suppresses a real fault"


def test_bare_stack_rule_respects_its_negative_lookahead(ledgers):
    by_id = {c["id"]: c["rx"] for c in ledgers.rea["classes"]}
    rx = by_id["bare-stack-continuation"]
    assert re.search(rx, "   at Foo.Bar()\n   at Baz.Qux()")
    assert not re.search(
        rx, "System.NullReferenceException: boom\n   at Foo.Bar()"), (
        "a fragment carrying a real exception header must NOT be suppressed")


def test_missing_ps1_is_a_counted_skip_not_a_silent_pass(ctx, repo, report):
    """CI has no ps1. The skip must be REPORTED — an audit that quietly narrows
    its own boundary is the original defect. It is reported through
    meta.s2_subjects rather than through a verdict detail, so that running the
    audit from the other host does not perturb report_digest."""
    result = det.detect(ctx)
    cross = [v for v in result.verdicts if v.instance_id.endswith(":cross-check")]
    assert len(cross) == 1
    assert cross[0].kind in ("ps1-cross-check", "ps1-drift")
    s2 = report["meta"]["s2_subjects"]
    assert "scripts/local-llm/qflix-rea.ps1" in s2
    assert s2["scripts/local-llm/qflix-rea.ps1"] == repo.exists(
        "scripts/local-llm/qflix-rea.ps1")


def test_the_cross_check_verdict_is_host_independent(ctx, repo, tmp_path):
    """The same commit must hash the same on the workstation (ps1 present) and
    on a CI runner (ps1 absent). Proved by running the detector against a
    checkout that HAS the subject and one that does not, and comparing the
    digest-visible fields."""
    from lib.audit.repo import Repo as _Repo

    absent = det.detect(ctx)
    # Synthesise a present-and-matching subject from the tracked policy itself.
    rea = ctx.ledgers.rea
    # Single-quoted PowerShell strings, exactly as the real ps1 writes them:
    # backslashes stay literal (which is why regexes are written that way) and
    # an inner quote is doubled.
    rules = "\n".join(
        "    @{ id = '" + c["id"] + "'\n"
        + ("       field = '" + c["field"] + "'\n" if c.get("field") else "")
        + "       rx = '" + c["rx"].replace("'", "''") + "' }"
        for c in rea["classes"])
    prompt = (rea["prompt_start_marker"] + " "
              + " ".join(s["marker"] for s in rea["prompt_segments"])
              + " " + rea["prompt_stop_marker"])
    (tmp_path / "scripts" / "local-llm").mkdir(parents=True)
    (tmp_path / "scripts" / "local-llm" / "qflix-rea.ps1").write_text(
        "$Script:NoiseFindingRules = @(\n" + rules + "\n)\n" + prompt + "\n",
        encoding="utf-8")

    class _C:
        repo = _Repo(tmp_path, tracked=[])
        ledgers = ctx.ledgers

    present = det.detect(_C())
    a = [v for v in absent.verdicts if v.path.endswith(".ps1")][0]
    b = [v for v in present.verdicts if v.path.endswith(".ps1")][0]
    assert (a.instance_id, a.kind, a.status, a.detail) == (
        b.instance_id, b.kind, b.status, b.detail), (
        "the ps1 layer leaks host state into the digest: " + repr(a.detail)
        + " vs " + repr(b.detail))


def test_ps1_parser_round_trips_the_real_table_shape():
    """Parser proof against a synthetic ps1, so it is tested in CI where the
    real file is absent."""
    sample = (
        "$Script:NoiseFindingRules = @(\n"
        "    @{ id = 'a-rule'\n"
        "       rx = '(?i)plain \\(escaped\\)' }\n"
        "    @{ id = 'b-rule'\n"
        "       rx = \"(?i)has a 'quote' inside\" }\n"
        "    @{ id = 'c-rule'\n"
        "       field = 'excerpt'\n"
        "       rx = '(?i)scoped' }\n"
        ")\n"
    )
    rules = det.parse_ps1_rules(sample)
    assert [r["id"] for r in rules] == ["a-rule", "b-rule", "c-rule"]
    assert rules[0]["rx"] == "(?i)plain \\(escaped\\)"
    assert rules[1]["rx"] == "(?i)has a 'quote' inside"
    assert rules[2]["field"] == "excerpt"


def test_cross_check_verdict_count_is_machine_independent(ctx):
    """report_digest must not depend on which host ran the audit. The ps1 layer
    is collapsed to ONE verdict for exactly this reason."""
    result = det.detect(ctx)
    cross = [v for v in result.verdicts if v.path.endswith(".ps1")]
    assert len(cross) == 1
    assert "ps1_cross_checked" not in result.metrics
    assert "ps1_rules_seen" not in result.metrics


def test_drift_between_ps1_and_yaml_is_reported(ledgers):
    """Synthetic drift: the ps1 has a rule the yaml does not."""
    drifted = (
        "$Script:NoiseFindingRules = @(\n"
        "    @{ id = 'not-in-yaml'\n"
        "       rx = '(?i)x' }\n"
        ")\n"
    )
    problems = det._ps1_drift(drifted, ledgers.rea, ledgers.rea["classes"],
                              {c["id"]: c for c in ledgers.rea["classes"]},
                              ledgers.rea["prompt_segments"])
    assert problems
    assert any("rule ids/order differ" in p for p in problems)


def test_deadman_reasons_are_in_git(ledgers):
    """REA's five early-return deadman paths, mirrored into git so C-09 can
    enumerate them without the ps1."""
    assert set(ledgers.rea["deadman_reasons"]) == {
        "tunnel_timeout", "no_models", "ssh_fail", "blob_parse", "all_models_noop"}
