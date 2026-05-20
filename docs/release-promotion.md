# Release-promotion pipeline

## Why this exists

Manitoba — the operator's seedbox — runs **master** continuously. Real
customers stream against it, so a bad master commit is a real-people
problem. But waiting for the dust to settle on a multi-day soak before
shipping any change wastes the operator's bandwidth.

The compromise: **Manitoba is a rolling-release dev node with paying
users on it.** Customer nodes — spun up only when a new customer arrives —
get **release tags**, never raw master. A release tag is just a master
commit that has soaked on Manitoba for ≥7 days with no rollback.

This is a single-operator system, so the workflow is deliberately
process-light: tags, a cut script, and a smoke check. No PR gates, no
release branch, no version pin databases.

## The model

```
            master (rolling) -> Manitoba (live users, real Kuma page)
                |
                |  >=7 days, no rollback
                v
            release-X.Y.Z (annotated tag)
                |
                v
            customer node spin-up reads $QFLIX_RELEASE_TAG
                              git checkout $QFLIX_RELEASE_TAG
                              bash scripts/configure/*.sh
```

Customer nodes never `git pull` master. They check out a tag, install,
and stay frozen until the operator promotes a newer tag to them.

## Cutting a release

```sh
bash scripts/ops/cut-release.sh
```

The script:

1. Refuses if working tree is dirty or you're not on master.
2. Refuses if the most recent tag is <7 days old (the soak window).
3. Computes the next semver from the most recent `release-*` tag —
   patch bump by default, minor/major via `--minor` / `--major`.
4. Prints the commit summary since the last tag and pauses for review.
5. Generates an annotated tag with that summary.
6. Pushes the tag to `origin`.
7. Reminds you to add a CHANGELOG.md entry referencing the new tag.

Override the soak window in a hurry:

```sh
QFLIX_RELEASE_FORCE=1 bash scripts/ops/cut-release.sh
```

Don't override casually — the soak window IS the value the tag adds. A
forced tag with the same commit content as master means customer nodes
gain no extra safety from running the tag.

## Promoting a tag to a customer node

(Documented for the first customer, who doesn't exist yet.)

```sh
# On the customer's seedbox (NOT Manitoba):
export QFLIX_RELEASE_TAG=release-0.1.0
cd ~/qflix
git fetch origin --tags
git checkout $QFLIX_RELEASE_TAG
# then run the standard scripts/configure/*.sh batch
```

If a customer's seedbox needs to revert (regression discovered after
promotion), check out the previous `release-*` tag and re-run the
relevant configure scripts. Most configure scripts are idempotent and
will revert config files cleanly; install state in `~/.apps/` follows
whatever the configure scripts set on disk.

## Smoke gate

`scripts/smoke-test.sh` includes `release-tag-fresh`:

- **pass** if the most recent `release-*` tag is within 30 days
- **fail** if older — reminds you it's time to cut another. The
  customer-node freshness depends on this rhythm.

## Why dates aren't in the tag name

The CHANGELOG.md captures the dates. The tag name is a stable
identifier the operator types — `release-0.1.0` is shorter than
`release-2026-05-27`. Date-in-name also encourages thinking of tags as
calendar artifacts ("the May 27 release") instead of content artifacts
("everything up through the Tdarr Flow rollout"). The former rots; the
latter doesn't.

## What lives on master vs on tags

| Lives on master (rolling) | Lives on a tag (stable) |
|---|---|
| Newsletter templates, copy edits | Configure scripts that touch *arr state |
| Smoke-test additions | Buildarr declarative configs |
| Canary scripts, observability tweaks | Tdarr flow JSON + workflow attach |
| Documentation | Maintainerr rules, Kometa libraries |
| Anything Manitoba-internal | Anything a customer node would replay |

In practice every commit lands on master. The tag is what *customer*
nodes pin to, not a fork of the codebase.
