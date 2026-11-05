# Publication reader

The optional publication reader lets supported parts of the app use a validated,
versioned BigQuery release. Starting the normal app does not enable it; an operator
must explicitly configure the reader and supply appropriate warehouse access and
source authority. This repository does not provide a source license, credentials,
private producer or production state.

## What it serves

Supported, self-contained Ask questions and selected Player, Compare and
regular-season Performance views can use a pinned publication. Other questions and
page features continue to use their existing data sources. The reader is not a
full-app cutover. The current rolling contract covers the 2026–27 regular
season. The [synthetic demo](core-offline-replay.md) demonstrates the
core flow without a warehouse or credentials.

## Configuration and failure behavior

`scripts/serve_production_candidate.py` composes the reader with the existing app.
Its JSON configuration identifies `project`, `namespace`, and `mode_path`, plus
`policy_path`, `policy_digest`, and `registry_path` for source authority. The
authority fields can instead be supplied per provider in `source_authorities`.
The digest pins the policy used for validation. Authority documents and reader
configuration must be independently administered and mounted read-only. The
mode document selects `production` or `legacy`; the latter disables publication
requests. The launcher mounts the reader at `/publication-core`, with access
controlled by the selected mode. The version 2
[publication contracts](../contracts/publication/README.md) describe the release
interface. Version 1 is limited to the fixed-window offline rehearsal and cannot
authorize a production release.

A release must pass its scope, freshness, authority and integrity checks before
it can serve. A failed candidate does not replace the current release, which
remains usable only while its own validity and source authority hold. If a
publication is unavailable, Ask can use its existing service after a publication
503; affected publication sections show an unavailable state while existing page
data remains available. Revoked, missing or expired authority prevents serving;
changing mode alone does not revoke a release. Authority changes can
take up to 30 seconds to reach an individual process because of short-lived
caches; expiry is checked against the current time, and a failed refresh does not
reuse an expired cache entry. The reader rejects rehearsal receipts for production
and does not accept caller-supplied warehouse relations or SQL.

A fork needs its own permitted data, warehouse and authority configuration. The
private operator runbook and environment-specific deployment details are not part
of this public repository. See the [public/private boundary](public-private-boundary.md).
