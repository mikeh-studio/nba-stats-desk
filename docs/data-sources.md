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
- **PBP Stats:** a source of play-by-play and supporting game and player identity
  information for workflows that use it. Identity candidates still require the
  relevant verification; they are not interchangeable with another provider's IDs.
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
match is not proof of complete league-wide coverage.

The optional manual publication workflow builds a candidate, validates its
scope, freshness, authority and output, and publishes a versioned release only
when the checks pass. A failed candidate does not replace the prior release;
that release can serve only while its own freshness and source authority remain
valid. Supported app views can read the pinned release; other features continue
using their existing sources. See the [publication reader](core-production-preparation.md)
and [architecture](architecture.md) for the public interfaces.

Some repository code also supports isolated development and rehearsal flows.
Those flows do not establish production readiness, source approval, complete
coverage, or that a scheduler is running. The opt-in local ESPN Ask review uses
mapped, accepted dev box scores with partial coverage, preserves provider-specific
IDs, and does not support league cohort claims or assist-pair relationships.
Operational configuration, source captures and review evidence are maintained
privately. Synthetic examples and tests do not establish live source access.
