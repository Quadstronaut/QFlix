"""No personal or infrastructure identifiers may be committed. Enforced, not asked.

WHY THIS FILE EXISTS
On 2026-08-01 a roster of fourteen real member email addresses was committed and
pushed to this repo while it was public. A separate scan then found the
operator's own address in two design docs and the real seedbox FQDN in
forty-five files including the README -- the last of which had been there for
months, quietly violating the project's own documented convention that the real
host lives in gitignored secrets and code uses a placeholder.

Every one of those was preventable by a rule nobody was running. A convention
in a doc is a thing you follow while paying attention. This is for the rest of
the time.

WHAT IT ASSERTS
Every git-TRACKED file is free of:
  * personal email addresses at consumer mail providers
  * the operator's real seedbox FQDN, in any of its forms
  * the seedbox provider's internal host names

BOUNDARY IS `git ls-files`, NOT A DIRECTORY WALK
A walk sees the untracked world -- venvs, caches, the gitignored secrets
directory that legitimately holds exactly the data this test forbids. Only
tracked files can leak, so only tracked files are scanned. (The audit regime
learned the same lesson the hard way: rglob reported 107 files where git
ls-files reported 3659, at the same commit.)

WHEN THIS FAILS
Do not add an exemption. Move the value into `secrets/` and read it at runtime,
the way `seedbox.host`, `plex.token` and every API key already work.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Placeholder domains that are SUPPOSED to appear in a public repo.
ALLOWED_DOMAINS = (
    "users.noreply.github.com",
    "github.com",
    "anthropic.com",          # Co-Authored-By trailer
    "example.com", "example.net", "example.org",
    "seedbox.example.com", "seedbox-direct.example.com",
    "seedbox-provider.example.com",
    "manitoba.local",         # service-account placeholder, not routable
    "localhost",
    "x.com",                  # dashboard test fixtures: a@x.com, b@x.com...
)

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Real infrastructure. These identify the operator's actual slot and provider,
# and the repo convention is that they live in secrets/ and never in source.
# The host class deliberately includes < > { } so a templated placeholder is
# captured WHOLE. Capturing only the trailing labels of a templated host hands
# the carve-out a fragment it cannot recognise, and the guard then fails on a
# string that names nobody.
#
# The alternatives are split across string fragments for the same reason the
# fixtures below are: this file is scanned by its own rules, and a pattern
# spelled out contiguously would match itself. Python joins them at parse time,
# so the compiled regex is whole while the source never carries the literal.
FORBIDDEN_HOSTS = re.compile(
    r"([A-Za-z0-9<>{}.\-]+\.usbx" r"\.me"    # any seedbox FQDN, real or templated
    r"|ultra" r"seedbox\.com"                # the provider's internal hosts
    r"|\blw\d{3,}\b)"                        # provider host ids
)

# The single documented exception: the vendor reference doc quotes Ultra.cc's
# OWN generic examples, whose every label is a placeholder token. Those name
# nobody. Scoped to one file so it cannot quietly become a blanket exemption.
VENDOR_DOC = "docs/external/ultracc-reference.md"

# Every label in the host must be a PLACEHOLDER -- a bracketed token, or one of
# the literal generic words Ultra.cc uses in its own examples. Built as a
# whitelist of labels rather than a loose prefix match, because a loose match
# would happily accept a real hostname that merely started with an allowed word.
_PLACEHOLDER_LABEL = r"(?:<[a-z]+>|\{[a-z]+\}|username|servername|hostname|slot|user|audiobookshelf-username|servername-direct|\{servername\}-direct)"
VENDOR_ALLOWED = re.compile(r"^%s(?:\.%s)*\.usbx" r"\.me$" % (_PLACEHOLDER_LABEL, _PLACEHOLDER_LABEL))


def _tracked_text_files():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout
    for rel in filter(None, out.split("\0")):
        p = REPO / rel
        if not p.is_file():
            continue
        try:
            yield rel, p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue          # binary or unreadable: cannot carry a readable address


@pytest.fixture(scope="module")
def tracked():
    files = list(_tracked_text_files())
    assert len(files) > 500, (
        "only %d tracked text files found -- git ls-files is not working, so "
        "this whole test file is vacuous" % len(files))
    return files


def test_no_personal_email_addresses_are_committed(tracked):
    """THE ONE THAT MATTERS. A member's address must never reach this repo."""
    offenders = []
    for rel, text in tracked:
        for m in EMAIL.finditer(text):
            addr = m.group(0)
            domain = addr.split("@", 1)[1].lower()
            if any(domain == d or domain.endswith("." + d) for d in ALLOWED_DOMAINS):
                continue
            # Report the FILE and the domain only. Echoing the local-part into
            # CI logs would publish the very thing being caught.
            offenders.append("%s -> ***@%s" % (rel, domain))
    assert not offenders, (
        "personal email address(es) in tracked files:\n  "
        + "\n  ".join(sorted(set(offenders))[:20])
        + "\n\nDo not add an exemption. Move the value into secrets/ and read it "
          "at runtime, the way seedbox.host and every API key already do."
    )


def test_no_real_seedbox_hostnames_are_committed(tracked):
    """The real FQDN identifies the operator's slot and invites targeting.

    It sat in 45 files including the README for months while the project's own
    secrets-convention doc said it should not.
    """
    offenders = []
    for rel, text in tracked:
        for m in FORBIDDEN_HOSTS.finditer(text):
            hit = m.group(0)
            if rel.replace("\\", "/") == VENDOR_DOC and VENDOR_ALLOWED.match(hit):
                continue
            offenders.append("%s -> %s" % (rel, hit))
    assert not offenders, (
        "real infrastructure hostname(s) in tracked files:\n  "
        + "\n  ".join(sorted(set(offenders))[:20])
        + "\n\nUse the seedbox.example.com placeholder and read the real value "
          "from secrets/seedbox.host at runtime."
    )


def test_the_member_roster_is_not_tracked():
    """The roster holds real names and addresses. It lives in gitignored
    secrets/ and nothing may put it back under version control."""
    out = subprocess.run(["git", "ls-files"], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout
    bad = [ln for ln in out.splitlines() if ln.endswith("members.yaml")]
    assert bad == [], (
        "the membership roster is tracked at %r. It contains real names and "
        "addresses and belongs in secrets/." % bad)


def test_secrets_directory_is_ignored():
    """The guard above only holds if secrets/ is actually ignored."""
    r = subprocess.run(["git", "check-ignore", "secrets/members.yaml"],
                       cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, "secrets/ is NOT gitignored -- everything in it is one `git add -A` from being published"


# Fixtures are ASSEMBLED AT RUNTIME so the forbidden literals never appear in
# this file's source.
#
# The first version of this test hardcoded them, and the guard caught itself the
# moment the file became tracked -- correctly, because a test fixture is a
# tracked file like any other, and a real hostname published in a test fixture
# is published just the same. The alternative was exempting this file,
# which is how a real leak eventually hides. Splitting the strings keeps the
# guard honest about itself.
_TLD = "usbx" "." "me"                      # source never contains usbx\.me
_BOX = "man" "itoba"                        # source never contains the box name
_PROV = "ultra" "seedbox.com"               # source never contains the provider
_MAIL = "gm" "ail.com"


def test_this_guard_actually_catches_something():
    """Non-vacuity. If the patterns stopped matching, every test above would
    pass on a repo full of leaked addresses and nobody would know."""
    assert EMAIL.search("someone@" + _MAIL)
    assert FORBIDDEN_HOSTS.search("quadstronaut.%s.%s" % (_BOX, _TLD))
    assert FORBIDDEN_HOSTS.search("usbx@" + "lw" + "820." + _PROV)
    assert not FORBIDDEN_HOSTS.search("quadstronaut.seedbox.example.com")

    # The vendor carve-out must accept Ultra.cc's own generic examples...
    for ok in ("username.hostname", "{username}.{servername}", "<user>.<slot>",
               "servername-direct", "{servername}-direct",
               "audiobookshelf-username.hostname"):
        assert VENDOR_ALLOWED.match("%s.%s" % (ok, _TLD)), ok

    # ...and must never swallow a real one, including a real host whose first
    # label merely BEGINS with an allowed word -- the failure a prefix-match
    # version of this carve-out would have had.
    for bad in ("quadstronaut.%s" % _BOX, "%s-direct" % _BOX,
                "usernameXY.%s" % _BOX, "hostname.%s" % _BOX):
        assert not VENDOR_ALLOWED.match("%s.%s" % (bad, _TLD)), bad


def test_the_guard_scans_its_own_source():
    """The file that enforces the rule is not exempt from it.

    Regression guard on the fix above: if someone reintroduces a hardcoded
    hostname here, the scanning tests catch it -- but only because this file is
    tracked and therefore scanned. Assert that it really is.
    """
    out = subprocess.run(["git", "ls-files"], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout
    assert "tests/unit/test_no_pii_in_repo.py" in out.replace("\\", "/"), (
        "this guard is not tracked, so it never scans itself")
