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
    # Added 2026-08-03 after the three-finding false page. F1/F2/F3 in
    # docs — every one of these was a model reporting by-design log volume.
    "plex-client-profile-extra",
    "plex-metadata-agent-pseudo-identifier",
    "arr-parsing-no-matching-title",
    "arr-release-rejected-unknown-title",
    "arr-debug-only-excerpt",
    # Enrolled 2026-08-03: the plex_errors collector had been dropping these two
    # classes since 2026-07-25 with no rule here at all — 86% of that section's
    # suppression, uncounted and invisible to CI.
    "plex-credits-detection-chatter",
    "plex-unknown-metadata-type-folder",
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
        # 2026-08-03 classes. Every haystack below is a REAL line shape pulled
        # off the box that day, not an invented one — a rule that only matches
        # a line the author imagined is a suppression that suppresses nothing.
        ("plex-client-profile-extra",
         "ERROR - [Req#12f/Transcode] ClientProfileExtra: missing or invalid type parameter"),
        ("plex-metadata-agent-pseudo-identifier",
         "ERROR - Unable to find metadata agent provider for identifier 'library'"),
        # 'iva' is enumerated separately on purpose: keying on 'library' alone
        # would have re-paged on the identical non-issue (it fired Jul 11/21).
        ("plex-metadata-agent-pseudo-identifier",
         "ERROR - [MetadataAgentManager/getAgent] Unable to find metadata agent "
         "provider for identifier 'iva'"),
        ("arr-parsing-no-matching-title",
         "2026-08-03 04:55:44.8|Debug|ParsingService|No matching series Two Guys Garage"),
        # Radarr's real wording is "No matching movie FOR TITLES '<t>'", not
        # "No matching movie <t>".
        ("arr-parsing-no-matching-title",
         "2026-08-03 03:08:45.6|Debug|ParsingService|No matching movie for titles "
         "'The Tech Billionaire Takeover (2026)'"),
        # Carries the FULL line, level+logger included. An earlier draft asserted
        # only the bare "[Permanent] Unknown Series" tail, which is exactly the
        # over-broad shape that let model PROSE suppress unrelated real faults.
        ("arr-release-rejected-unknown-title",
         "2026-08-03 02:11:07.1|Debug|DownloadDecisionMaker|Release 'Some.Show.S01' "
         "from 'Indexer' rejected for the following reasons: [Permanent] Unknown Series"),
        # The two plex_errors collector exclusions that had no rule until now.
        ("plex-credits-detection-chatter",
         "ERROR - [CreditsDetectionManager/Response::fetch/MarkerResponse] "
         "incomplete marker attributes"),
        ("plex-credits-detection-chatter",
         "ERROR - [CreditsDetectionManager] BufferingLineReader: failed to read "
         "line (error: -1)"),
        ("plex-unknown-metadata-type-folder",
         "ERROR - [Req#5d3] Unknown metadata type: folder"),
        # 2026-08-06. The REA alert line verbatim. Note the repo path is
        # capitalised "Bazarr" upstream while the rx spells it lowercase — the
        # (?i) flag is load-bearing here, so this case pins it.
        ("bazarr-github-release-check-ratelimit",
         "Error trying to get releases from Github. Http error. "
         "requests.exceptions.HTTPError: 403 Client Error: rate limit exceeded "
         "for url: https://api.github.com/repos/morpheus65535/Bazarr/releases"
         "?per_page=100"),
        # 2026-08-13. The shape models ACTUALLY emit: norm() rewrote the URL to
        # <url> upstream and the model truncated its excerpt right after "rate
        # limit exceeded" — the 2026-08-13 page that forced the rx reshape.
        # The updater's verbatim message text is the marker that survives.
        ("bazarr-github-release-check-ratelimit",
         "Error trying to get releases from Github. Http error. "
         "403 Client Error: rate limit exceeded"),
        # 2026-08-14 v3. The arr_logs shape: cut -c1-220 amputates the
        # traceback (and the words "rate limit") from Bazarr's one-line log
        # entry, so the pair that survives is updater marker + "Http error."
        # - which check_update.py emits for HTTPError only.
        ("bazarr-github-release-check-ratelimit",
         "[/home/quadstronaut/.apps/bazarr/log/bazarr.log]       1 "
         "2026-08-14 00:50:17|ERROR   |root  |Error trying to get releases "
         "from Github. Http error.|<Traceback (most recent call last):"),
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


def test_new_rules_do_not_eat_real_faults(ledgers):
    """The 2026-08-03 classes are the widest ever added, and three of them are
    keyed on phrases that a GENUINE fault also uses. This is the test that
    matters: each new rule has a near-miss twin that must still page, and the
    only thing keeping them apart is a negative lookahead or an explicit
    enumeration. Delete a lookahead and the structural checks above all still
    pass while REA goes quiet on a real break."""
    by = {c["id"]: c["rx"] for c in ledgers.rea["classes"]}

    # F2's rule enumerates the PSEUDO-identifiers ('library', 'iva'). The same
    # message naming a REAL provider is a genuine agent/config break.
    for real_provider in (
            "Unable to find metadata agent provider for identifier "
            "'com.plexapp.agents.thetvdb'",
            "Unable to find metadata agent provider for identifier "
            "'tv.plex.agents.series'"):
        assert not re.search(
            by["plex-metadata-agent-pseudo-identifier"], real_provider), (
            "a real metadata agent provider must still page: " + real_provider)

    # F3's rule suppresses ParsingService Debug chatter. These are Error-level
    # IMPORT failures that happen to share the words "no matching series".
    # NOTE the plural "folders": the first draft guarded `folder\b` with no `s?`,
    # so the plural form was silently eaten. The level+logger anchor now in use
    # makes the whole family unmatchable rather than relying on a lookahead.
    for real_import_fault in (
            "|Error|ImportApprovedEpisodes|Couldn't import episode, "
            "no matching series folder on disk",
            "|Error|ImportApprovedEpisodes|Couldn't import episode, "
            "no matching series folders on disk",
            "|Error|DiskScanService|Import failed, no matching series files were readable"):
        assert not re.search(
            by["arr-parsing-no-matching-title"], real_import_fault), real_import_fault

    # These two rules carry no `field`, so they run against
    # signature + summary + excerpt JOINED. A model's PROSE restating the benign
    # phrase must NOT be able to suppress a finding whose excerpt is a real
    # Error. This is the regression that the bare-phrase drafts had.
    assert not re.search(
        by["arr-parsing-no-matching-title"],
        "arr:import-failed The importer reports no matching series for this "
        "release. |Error|ImportApprovedEpisodes|Import failed: Disk quota exceeded")
    assert not re.search(
        by["arr-release-rejected-unknown-title"],
        "arr:db-locked The release was rejected with [Permanent] Unknown Series. "
        "|Error|SeriesService|database is locked")

    # 2026-08-06, rx reshaped 2026-08-14. The Bazarr update-check rule requires
    # BOTH an updater marker AND a rate-limit token ON ONE LINE of the excerpt
    # ((?m)^ without (?s), field=excerpt). The markers are Bazarr's verbatim
    # updater text ("trying to get releases from github") or the path-anchored
    # slug (morpheus65535/bazarr/releases) — deliberately NOT "rate limit
    # exceeded" alone, NOT api.github.com generally, and NOT the bare repo
    # slug, because the cosmetic argument is specific to that one consumer:
    # Bazarr runs --no-update, so its release list is display-only. QFlix's
    # OWN GitHub callers (the lifecycle resolver, Tuesday.md:73) have no such
    # exemption — a silent rate-limit there really does degrade version
    # resolution — so every other GitHub 403 must still page, including 403s
    # against OTHER endpoints of the Bazarr repo itself.
    rl = by["bazarr-github-release-check-ratelimit"]
    for still_pages in (
            "403 Client Error: rate limit exceeded for url: "
            "https://api.github.com/repos/Radarr/Radarr/releases/latest",
            "403 Client Error: rate limit exceeded for url: "
            "https://api.github.com/rate_limit",
            "403 Client Error: rate limit exceeded for url: "
            "https://api.github.com/repos/morpheus65535/Bazarr/issues"):
        assert not re.search(rl, still_pages), (
            "a non-Bazarr-release GitHub rate-limit must still page: " + still_pages)
    # The one-line constraint itself: a benign updater line must not borrow a
    # rate-limit token from a DIFFERENT line of the same excerpt (a real
    # provider 429 next to updater chatter must page). Line 1 is the
    # Connection Error shape - the Http error shape legitimately suppresses
    # on its own line since the v3 token widening.
    assert not re.search(rl,
        "Error trying to get releases from Github. Connection Error.\n"
        "opensubtitles.com: 429 rate limit reached, all providers throttled"), (
        "cross-line token join must not suppress a real provider throttle")
    # Network-fault shapes still page (check_update.py logs these sentences
    # for ConnectionError/Timeout; only HTTPError gets "Http error.").
    for network_fault in (
            "Error trying to get releases from Github. Connection Error.",
            "Error trying to get releases from Github. Timeout Error."):
        assert not re.search(rl, network_fault), (
            "an updater network fault must still page: " + network_fault)

    # The structural backstop: fires only when the excerpt has an *arr |Debug|
    # token and NO error-level token anywhere in it. The guard must span every
    # log format that shares the arr_logs section, not just *arr NLog.
    rx = by["arr-debug-only-excerpt"]
    assert re.search(
        rx, "2026-08-03 04:55:44.8|Debug|ParsingService|No matching series Eureka")
    assert not re.search(
        rx, "2026-08-03 01:42:29.6|Error|QBittorrent|API Grab Limit reached")
    assert not re.search(rx, "2026-08-03|Debug|x\n2026-08-03|Error|y"), (
        "one Error line anywhere in the excerpt must un-suppress the whole finding")
    # Bazarr ships in the SAME section and PADS its level tokens. `cat -A` on the
    # box gives "|ERROR   |" and "|WARNING |" — neither satisfied the original
    # unpadded \|(?:Error|Fatal|Warn)\| guard, so a real Bazarr fault paired with
    # one *arr Debug line was silently eaten.
    for foreign_error in (
            "2026-08-03 06:12:38 - root  :  |ERROR   | BAZARR cannot insert "
            "episodes because of (sqlite3.IntegrityError)\n2026-08-03|Debug|x",
            "2026-08-03 06:12:38 - root  :  |WARNING | something odd"
            "\n2026-08-03|Debug|x",
            "Jul 21, 2026 01:46:20 [140] ERROR - Could not write header"
            "\n2026-08-03|Debug|x",
            "Traceback (most recent call last):\n  File a.py\n2026-08-03|Debug|x"):
        assert not re.search(rx, foreign_error), (
            "non-*arr error evidence must un-suppress: " + foreign_error[:40])

    # FU-2: this exact live Error line is one REA has never surfaced. No new
    # rule may be the reason it stays invisible — check it against ALL FIVE,
    # not just the excerpt rule.
    grab_limit = "2026-08-03 01:42:29.6|Error|QBittorrent|API Grab Limit reached"
    for cid in ("plex-client-profile-extra",
                "plex-metadata-agent-pseudo-identifier",
                "arr-parsing-no-matching-title",
                "arr-release-rejected-unknown-title",
                "arr-debug-only-excerpt",
                "plex-credits-detection-chatter",
                "plex-unknown-metadata-type-folder"):
        assert not re.search(by[cid], grab_limit), cid + " swallows a live *arr Error"

    # The credits rule is deliberately NARROWER than the collector grep it
    # replaced, which matched the bare subsystem name and therefore ate five
    # distinct message shapes. These two are plausible real faults and must
    # still reach a model.
    for real_credits_fault in (
            "ERROR - [CreditsDetectionManager] Job failed: Scanner job failed",
            "ERROR - [CreditsDetectionManager] Mis-matching media items detected, skipping"):
        assert not re.search(
            by["plex-credits-detection-chatter"], real_credits_fault), real_credits_fault
    # Anchored to the "folder" type specifically.
    assert not re.search(
        by["plex-unknown-metadata-type-folder"],
        "ERROR - [Req#5d3] Unknown metadata type: collection")


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
    # A CONFORMING prompt: one ';'-delimited clause per segment, each carrying
    # its own marker and the prompt_clause of every class that segment claims.
    # Both are required of the real prompt, so the stand-in must satisfy them or
    # it is testing a shape no real ps1 has.
    by_cid = {c["id"]: c for c in rea["classes"]}
    prompt = (rea["prompt_start_marker"] + " "
              + "; ".join(
                  s["marker"] + " "
                  + " ".join(by_cid[cid]["prompt_clause"] for cid in s["classes"])
                  for s in rea["prompt_segments"])
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


def _live_ps1(repo, ledgers):
    """The operator-local ps1, or None in CI. Layer 2 only exists where it does."""
    return repo.read_optional(
        ledgers.rea.get("source_script") or "scripts/local-llm/qflix-rea.ps1")


def test_an_unenforced_prompt_clause_is_reported(repo, ledgers):
    """THE founding defect direction, restored 2026-08-03.

    C-07 exists because on 2026-07-29 the prompt named classes it did not
    enforce. Both the module docstring and the yaml header claimed the detector
    "splits the text ... on ';'" and demanded every clause be claimed — but
    _ps1_drift only ever checked yaml->prompt. Adding a brand-new never-report
    clause to the prompt, with no class and no segment, returned NO drift: the
    detector enforced only the harmless direction.

    Non-vacuity matters here more than usual, so this asserts the CLEAN file is
    clean first — otherwise a detector that reports drift unconditionally would
    also pass."""
    ps1 = _live_ps1(repo, ledgers)
    if ps1 is None:
        import pytest
        pytest.skip("no ps1 on this host (CI) — layer 2 does not run")
    rea, classes = ledgers.rea, ledgers.rea["classes"]
    by_id = {c["id"]: c for c in classes}
    segments = rea["prompt_segments"]

    assert not det._ps1_drift(ps1, rea, classes, by_id, segments), (
        "baseline must be clean or this test proves nothing")

    injected = ps1.replace(
        ". CONVERSELY,",
        "; Kometa collection-builder warnings, which are cosmetic. CONVERSELY,", 1)
    assert injected != ps1, "the splice anchor moved; fix this test"
    problems = det._ps1_drift(injected, rea, classes, by_id, segments)
    assert any("claimed by no rule" in p for p in problems), (
        "a never-report clause with no enforcement rule must be reported, got: "
        + str(problems))


def test_prompt_clause_must_be_literal_prompt_text(repo, ledgers):
    """`prompt_clause` is the per-class quote of the prompt. C-07 only asserted
    it was non-empty, so four of the five 2026-08-03 classes shipped with a
    clause that appeared nowhere in the prompt — a second, unverified copy of
    the policy inside the file whose whole purpose is that copies cannot
    contradict."""
    ps1 = _live_ps1(repo, ledgers)
    if ps1 is None:
        import pytest
        pytest.skip("no ps1 on this host (CI) — layer 2 does not run")
    rea, classes = ledgers.rea, ledgers.rea["classes"]
    by_id = {c["id"]: c for c in classes}
    segments = rea["prompt_segments"]

    assert not det._ps1_drift(ps1, rea, classes, by_id, segments)

    mutated = [dict(c) for c in classes]
    mutated[0]["prompt_clause"] = "a clause that is nowhere in the prompt"
    problems = det._ps1_drift(ps1, rea, mutated,
                              {c["id"]: c for c in mutated}, segments)
    assert any("is not literal prompt text" in p for p in problems), problems


def test_deadman_reasons_are_in_git(ledgers):
    """REA's five early-return deadman paths, mirrored into git so C-09 can
    enumerate them without the ps1."""
    assert set(ledgers.rea["deadman_reasons"]) == {
        "tunnel_timeout", "no_models", "ssh_fail", "blob_parse", "all_models_noop"}
