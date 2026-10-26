# Intelligence host releases

A host release is an immutable annotated `intelligence-host-vX.Y.Z` Git tag on a
reviewed public `main` commit with successful CI for that exact main commit.
The artifact is this repository's source tree. It is not a pip distribution.
A GitHub Release, binary or package-index publication, and deployment are separate
operations; none is required to consume a source-host release.

## Versions and compatibility

Use patch versions for compatible corrections and documentation, and minor versions
for compatible additions. Breaking provider, helper or semantic changes require
an explicit migration review, corresponding contract-version changes and an
explicitly proposed host version. The tag label cannot establish compatibility:
check the [provider contract](intelligence-contract.md), helper surface and semantic
version, and run [acceptance](intelligence-acceptance.md). Consumer package versions
are independent of host tag versions. Documentation changes alone do not require
publishing a tag or changing a consumer package version.

Never move, delete and recreate, or reuse a published tag to repair a release.
Propose a new version, or have the consumer review a rollback to a prior accepted
release. Consumers must pin the annotated object and peeled commit, not a branch,
PR ref or a tag name without its immutable identity.

## Release sequence

1. Prepare a focused source PR and inspect its entire public diff. Obtain Mike's
   explicit approval of that exact diff before its first public push. Keep private
   source, prompts, cases, plans and operational evidence out of the public change.
2. Require green checks on the final PR head and separately authorize the merge.
   Record the resulting main commit and its successful `.github/workflows/ci.yml`
   **push to main** run, including the successful `test` job. A green PR run does
   not supply the main-commit evidence.
3. Record installed-provider pairing evidence for the exact proposed main commit
   and compatible consumer artifact. A pairing against a PR tree can inform review,
   but does not replace recording the final commit's identity and pairing result.
   Keep private pairing details in the consumer's private evidence; public notes
   describe only the public compatibility contract.
4. Review the proposed unused tag name, exact target SHA and complete sanitized
   annotation. Obtain separate approval covering that exact tag publication.
   Source-push or merge approval does not authorize a tag or deployment.
5. Only after approval, create the annotated tag on the reviewed SHA and push that
   one tag to `origin`. Inspect the remote tag object and peeled target and compare
   both with the approved values. Record the result; do not silently replace an
   unexpected remote object.

Read-only inspection examples, replacing `VERSION` with the reviewed version:

```sh
git cat-file -t refs/tags/intelligence-host-vVERSION
git cat-file -p refs/tags/intelligence-host-vVERSION
git rev-parse 'refs/tags/intelligence-host-vVERSION^{}'
git ls-remote origin 'refs/tags/intelligence-host-vVERSION' 'refs/tags/intelligence-host-vVERSION^{}'
git merge-base --is-ancestor 'refs/tags/intelligence-host-vVERSION^{}' origin/main
```

The ancestry example uses the locally fetched `origin/main`; refresh it before a
release decision. These commands inspect identity, not CI or compatibility. Reject
lightweight/moved tags, unmerged targets, incompatible contracts and pending,
failed or unavailable exact-main CI. If historical CI is unavailable, recover
valid evidence for that exact commit where possible or review a new release;
do not substitute another commit's successful run.

## Public annotation template

Fill every field and review the complete text before tag publication. This is a
future release template, not an instruction to publish now.

```text
NBA Stats Desk intelligence host vX.Y.Z

Source commit: <full public main SHA>
Provider API: <API_VERSION>
Helper surface: <HOST_HELPER_VERSION>
Semantic contract: <semantic version>
Public CI: <exact-main run URL>

Public changes: <concise public behavior changes>
Compatibility: <supported interfaces and migration requirements>

Reviewed source baseline for the standalone reference application and
optional intelligence providers. This tag does not change deployment.
```

Do not add private prompts, source, corpora/results, plans or internal review
notes. Public notes document public changes; consumer adoption and distribution
have their own review records.

## Existing source releases

These historical identities and annotations were inspected from Git on 2026-10-25.
Both declare `nba_intelligence/2`, `nba_provider_helpers/1` and `nba_semantics/0.1`.
They are release history, not a claim about current deployment or consumer adoption.

| Tag | Annotated object | Peeled source commit | Annotation summary |
| --- | --- | --- | --- |
| `intelligence-host-v0.1.0` | `84556393ac34256b01a2751857bb8069a33f933d` | `62b8bf3811d040d2ec9e80e4d3e6112466ec452c` | Reviewed reference application and optional-provider baseline. |
| `intelligence-host-v0.1.1` | `1b27f0d37775c7efe71e82323f69e89a977c395a` | `2f15c28306bbafcde983f0e1c905991b715a79e2` | Identity clarification before planning, question-bound validation before identity scanning, and restored comparison-format regression coverage; no deployment change. |

Later main commits and documentation are not retroactively part of these tags.
In particular, subsequent ownership/documentation corrections do not move v0.1.1.
See [runtime selection](intelligence-runtime.md) for explicit provider activation;
installation and tagging do not change the reference default.
