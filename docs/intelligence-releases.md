# Intelligence host releases

A host release is an immutable annotated `intelligence-host-vX.Y.Z` Git tag on a
reviewed `main` commit whose CI passed for that exact commit. The released
artifact is this repository's source tree, not a pip distribution. GitHub
Releases, package publication and deployment are separate operations; none is
required to consume a source-host release.

Optional intelligence providers pin a host release so they build against a known
[provider contract](intelligence-contract.md), helper surface and semantic version.

## Versions and compatibility

- **Patch**: compatible corrections and documentation.
- **Minor**: compatible additions.
- **Breaking** provider, helper or semantic changes: require a migration review,
  matching contract-version changes and an explicitly proposed host version.

A tag label does not establish compatibility. Check the provider contract, helper
surface and semantic version, and run [acceptance](intelligence-acceptance.md).
Provider package versions are independent of host tag versions, and documentation
changes alone do not require a new tag.

A published tag is never moved, deleted and recreated, or reused. Repairs ship as
a new version, or consumers roll back to a prior accepted release. Consumers pin
the annotated tag object and its peeled commit, not a branch, PR ref or bare tag
name.

## Release checklist

1. Merge a reviewed PR whose final head passed CI.
2. Record the resulting `main` commit and its successful
   `.github/workflows/ci.yml` **push to main** run, including the `test` job.
   A green PR run is not evidence for the merged commit.
3. Confirm provider compatibility against that exact `main` commit. A check
   against a PR tree can inform review but does not replace it.
4. Review the unused tag name, target SHA and complete annotation. Tag
   publication is approved separately from the merge; neither authorizes a
   deployment.
5. Create the annotated tag on the reviewed SHA, push only that tag, then confirm
   the remote tag object and peeled commit match the reviewed values. An
   unexpected remote object is reported, not silently replaced.

## Verifying a release

Replace `VERSION` with the release version:

```sh
git cat-file -t refs/tags/intelligence-host-vVERSION
git cat-file -p refs/tags/intelligence-host-vVERSION
git rev-parse 'refs/tags/intelligence-host-vVERSION^{}'
git ls-remote origin 'refs/tags/intelligence-host-vVERSION' 'refs/tags/intelligence-host-vVERSION^{}'
git merge-base --is-ancestor 'refs/tags/intelligence-host-vVERSION^{}' origin/main
```

Fetch `origin/main` before the ancestry check. These commands verify identity,
not CI or compatibility. Reject lightweight or moved tags, targets not on `main`,
incompatible contracts, and missing or failed CI for the exact commit; another
commit's successful run is never a substitute.

## Annotation template

```text
NBA Stats Desk intelligence host vX.Y.Z

Source commit: <full main SHA>
Provider API: <API_VERSION>
Helper surface: <HOST_HELPER_VERSION>
Semantic contract: <semantic version>
Public CI: <exact-main run URL>

Changes: <concise behavior changes>
Compatibility: <supported interfaces and migration requirements>

Reviewed source baseline for the standalone reference application and
optional intelligence providers. This tag does not change deployment.
```

Annotations describe public behavior and interfaces only.

## Release history

Both releases declare `nba_intelligence/2`, `nba_provider_helpers/1` and
`nba_semantics/0.1`. Identities were verified against `origin` on 2026-10-25.
This table records source history, not deployment or adoption.

| Tag | Annotated object | Peeled source commit | Summary |
| --- | --- | --- | --- |
| `intelligence-host-v0.1.0` | `84556393ac34256b01a2751857bb8069a33f933d` | `62b8bf3811d040d2ec9e80e4d3e6112466ec452c` | Reference application and optional-provider baseline. |
| `intelligence-host-v0.1.1` | `1b27f0d37775c7efe71e82323f69e89a977c395a` | `2f15c28306bbafcde983f0e1c905991b715a79e2` | Identity clarification before planning, question-bound validation before identity scanning, and restored comparison-format regression coverage. |

Later `main` commits are not part of these tags. See
[runtime selection](intelligence-runtime.md) for explicit provider activation;
installing or tagging a provider does not change the reference default.
