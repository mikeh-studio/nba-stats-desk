# Data sources and validation

NBA Stats Desk uses NBA data to support basketball analysis. Source access,
coverage and publication depend on the workflow and the data available to its
operator. This repository grants no source license or permission to collect,
store or redistribute third-party data.

## Source roles

- **NBA Stats API:** a primary source for game, player and team statistics in the
  existing pipeline. The manual publication workflow tries NBA data first.
- **NBA gamebook PDFs:** an additional source for box scores and play-by-play
  evidence. They can corroborate supported statistics and assist relationships;
  PDF availability or a readable text layer does not establish complete or
  correct extraction.
- **PBP Stats:** a source of game-list and player identity information for the
  gamebook and alternative-provider workflows. Identity candidates still require
  verification; they are not interchangeable with another provider's IDs.
- **Other providers:** a permitted and configured alternative such as ESPN may
  provide games, box scores or play-by-play when needed. Provider availability
  alone does not establish rights to automate collection or publish its data.
  ESPN-specific IDs remain distinct from NBA IDs.
- **Injury reports:** a separate input for availability features; they are not
  part of the core statistics publication described below.

## How data becomes usable

Source records retain their provider and scope. Game and player identities must
be verified before records from different sources can be related; unresolved or
conflicting identities are not guessed. Source contracts, reconciliation and
quality checks guard the applicable pipeline. For example, gamebook comparisons
can check extracted player and team totals against the PDF's box score, but a
match is not proof of complete league-wide coverage. Some player mapping uses
name-based resolution; it is not a provider-certified crosswalk.

The optional manual publication workflow builds a candidate, validates its
scope, freshness, authority and output, and publishes a versioned release only
when the checks pass. A failed candidate does not replace the prior release;
that release can serve only while its own freshness and source authority remain
valid. Supported app views can read the pinned release; other features continue
using their existing sources. See the [publication reader](core-production-preparation.md)
and [architecture](architecture.md) for the public interfaces.

## Isolated development workflows

The `nba_gamebook_dev` and `nba_espn_dev` Airflow DAGs are separate from the
production pipeline and are configured to start paused. The production
`make airflow-unpause` target does not enable the gamebook DAG. Their default
development datasets are `nba_gamebook_dev` and `nba_gamebook_dev_espn`,
respectively. Configuration and operating procedures belong to the operator.
These workflows do not establish source approval, complete coverage, production
readiness, or that a scheduler is running.

### Local ESPN Ask review

`ASK_LOCAL_ESPN_DEV=true` opts into a local review of accepted, mapped development
box scores. Requests must have direct localhost access; forwarded or non-local
requests are rejected. Answers identify partial coverage, count excluded unmapped
player rows, preserve `espn:<id>` game keys, and set `cohort_complete=false`.
Rankings, percentiles, peer comparisons, similarity, and assist-pair relationships
are not supported. Invalid evidence fails as `invalid_evidence`; current-season
player overviews without a phase use regular season and disclose that choice.
Other app pages retain their existing sources. This is a development preview,
not a publication or a source license.

Operational configuration, source captures and review evidence are maintained
privately. Synthetic examples and tests do not establish live source access.
