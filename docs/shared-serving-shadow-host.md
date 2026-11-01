# Isolated Shared Serving shadow host

`app.serving.shadow_host.create_shadow_app(selection, evidence_kind=...)` builds a
separate FastAPI app from an explicitly constructed `TwoSeasonPTS` selection.
It is never imported by `app.main` or the normal application factory. Run only
on loopback with proxy headers disabled. No environment source discovery occurs.

The selection must already bind historical and current snapshots to independent
read policies, source/identity authority, freshness policy and cross-season
identity approval. The host adds no permission and cannot make held data ready.
Both `synthetic` and `retained-unapproved` labels visibly deny source certification.

POST `/api/ask`, `/api/ask/stream`, `/api/player` or `/api/comparison` with precisely:

```json
{"season":"both","player_ids":[1],"aggregation":"average"}
```

`season` is `2025-26`, `2026-27` or `both`; aggregation is `total` or `average`.
Comparison requires exactly two distinct IDs; other routes require one. Only
independently approved player IDs are accepted. Bodies are limited to 2 KiB,
responses retain the existing 1 MiB budget, and the existing reader budgets apply.
Single-season requests authorize that season; two-season requests require both.
Neither averages nor totals are pooled across seasons.

SSE is one verified terminal event. Denial, expiry, missing players or integrity
failure yields a non-success JSON response before any terminal event. There is
no old-reader, warehouse or provider fallback. Responses are not cacheable.
Foreign Host and Origin headers are rejected; this is a loopback diagnostic,
not an authenticated internet-facing service.

`/ask` is a dedicated structured PTS validation page. It displays populated
tables and unavailable states, using text-only DOM insertion. Each season's
section retains phase/date scope, evidence as-of and cutoff, original evidence
clocks, the separate freshness/authority check times and all limitations. These
qualifiers remain visible for Ask, Player and comparison responses; a recent
read check does not relabel older source evidence as freshly collected.
It does not claim
to be the normal natural-language Ask UI, a live model test or production.

Tests use synthetic facts and authorities. Installed ASGI checks, actual
loopback/browser checks, real evidence approval and live/provider acceptance are
separate results. Runtime remains disabled, as do rankings, pooled arithmetic
and archetype/model outputs. This phase changes no source guards, deployment,
host lock, helper contract, dependency pin, weekly refresh or legacy behavior.
