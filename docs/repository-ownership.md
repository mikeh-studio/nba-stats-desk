# Repository ownership map

The **thin split** gives the public repository the platform and the whole web app.
The private repository owns production intelligence. Ownership governs new work;
it does not claim that the package extraction or removal of old copies is done.

[repository-ownership.json](repository-ownership.json) is the executable path map,
vendored identically with this document in both repositories. Update both copies
and their common policy tests together. The table below explains its capabilities.

| Capability | Authoritative home |
| --- | --- |
| Pipeline, contracts, dbt, season configuration, governed arithmetic, identity, evidence and claim validation, read adapters | Public |
| App composition: `app/main.py`, `app/config.py`, `app/model_catalog.py` | Public |
| All `app/static/**`, `app/templates/**`, browser interactions and implementation tests | Public |
| Conversation and follow-ups, route definitions/labels, metric presentation, deterministic server prose, service orchestration | Public |
| Evaluation harness, reusable synthetic fixtures and tests of public code | Public |
| Standalone reference planner supporting governed overviews, comparisons and rankings | Public; implemented behind the Phase 3 hook |
| Production `planner.py`, private `semantic_planner.py`, `router.py`, `question_intent.py`, prompt text and private intelligence eval cases | Private |
| Private repository tooling and compatibility tests | Private |
| Existing similarity and prop research models | Their existing private research repositories; unchanged |
| Credentials, user records, feedback, traces, raw data, exports and model binaries | Outside Git |

## Mixed files and the provider interface

`service.py` orchestration is public. The governed app loads production intelligence
from the private `src/nba_stats_desk_product` package through an explicitly selected
entry point. Its prompt sets and planner/classifier remain private. Governed
execution and schema are owned by public `plan_execution`; literal scope and
conversation policy are owned by public `scope_policy`. The public reference
provider requires no production prompt or model key.

Public `semantic_planner` and `question_intent` now provide compatibility exports
of public responsibilities. Existing non-governed planner/router/prompt snapshots
are a frozen legacy exception accessed only through `legacy_loader`; removing
that path requires the separately reviewed migration. Missing legacy code fails
unavailable, never falling back from a failed governed request.

The private wheel has its own namespace and imports shared execution from the
public application. The old private app/test tree remains on its existing pin for
compatibility checks. Released host tags and isolated package assembly are
available. Removing old copies and changing production images still require
separately reviewed cutover. No package installation changes the
selected provider or authorizes deployment. Previously published code remains
public; policy changes do not erase history.

## Machine-readable rules

Paths are repository-relative POSIX paths. Patterns use `fnmatchcase`: `*` can
cross directory separators and `**` expresses a subtree by convention. The highest
numeric priority wins. Multiple matching rules at the winning priority are an
error, even if they name the same owner. Unmatched application paths fail closed
through the public `app/**` rule. Invalid maps fail validation.

`owner` is authoritative development ownership. `private_presence` is separate:

- `owned`: private intelligence or private tests may be developed here.
- `frozen`: public-owned copies temporarily remain in private for compatibility.
  This is not permission for new private UI work; the Active migration freeze is
  unchanged. The private checker permits only paths in the staged manifest
  `imported_files`/`imported_test_files` inventories; new paths are rejected.
  Existing imported files may be edited subject to the repository policy. Remove
  these copies only after the separately approved deployment cutover.
- `vendored`: shared public policy utilities and tests maintained identically in
  both repositories, independently of the runtime pin.
- `exception`: a compatibility shim requires an exact path and explanation in
  the private migration manifest's `ownership_exceptions`.
- `forbidden`: private application/platform copies are rejected; manifest
  exceptions cannot override this rule.

Static assets and templates are classified by subtree patterns, never by an
ownership entry for every file. Import inventories also bound which existing
public-owned copies may remain during the freeze; do not expand these historical
inventories to authorize new UI work. Import hashes remain provenance, not a
requirement for existing file contents to stay byte-identical.
The private checker checks staged package (`src/`), app, pipeline and test paths; it is an ownership
and frozen-file admission check, not a secret scanner. Required
private modules are checked explicitly so deletion cannot silently load a public
fallback. Repository-local policy/tooling remains maintained in its own checkout.

For public `product-ui` and `policy` intents, startup rejects staged changes to
private-owned paths, including deletions and either side of a rename. It reads the
staged map; unstaged changes are not publication checks. Explicit `shared-fix`,
`reference-maintenance` and `public-promotion` scopes remain available for curated
maintenance/promotion and still require exact-diff publication approval. The CI
`--check-policy` mode validates layout only, not an implementation intent.

## Transition and publication

Public PR #97 merged as `2e2a3190c400b3824464345b83df633478d0b992`.
The private dependency still pins its ancestor `735bf4f0552209ccbc26e164692dbcd4aa1944fd`.
That commit is on public main, but it is not a released tag or proof of live data
readiness. Do not change the gitlink to obtain newer instructions.

The temporary private app and submodule remain until separately approved cutover;
package assembly alone does not retire them.
Keep upstream and third-party license notices and original import hashes. Never
edit through dependency symlinks or automatically mirror branches. Public app/UI
work starts in public; production intelligence starts in private. Every public
push still needs explicit user approval covering that exact diff. Private source,
private evaluation cases and operational records must not enter public changes.

Policy checks do not authorize archive publication, IAM, deployment, scheduling,
warehouse writes, live polling, serving activation or history rewriting.
