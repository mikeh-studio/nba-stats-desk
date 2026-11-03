# Offline reconciliation, coverage and PDF checks

Phase 2 evaluates pinned normalized observations in a separate append-only local
ledger. It does not register a serving source, change Ask/Player behavior, write
warehouse data, enable jobs or change source/model guards. Read
[source selection](unified-source-selection.md) first. The original pending
records remain immutable; evaluated records are separate evidence.

## Evidence and eligibility

| Record | Purpose |
| --- | --- |
| metric_catalog | Schema-pinned public formulas and component dependencies, including DREB |
| expected_inventory | Explicitly reviewed game universe, results and every calendar day, including no-game dates |
| evaluation_inputs | Exact inventory-bound observations, original captures, participant manifests, disputes and unchanged-fact revalidations |
| reconciliation | Missing/unexpected games, result conflicts, participant lineage, component holds, contiguous cutoff, freshness and correction reviews |
| coverage | Per-metric complete-case values, qualification, compatible pooled sums and base eligibility prerequisites |
| mapping_catalog | Exact identity catalog bound to the source inventory |
| pdf_inputs | Exact selection/observation/mapping pins and retained PDF/PBP captures or attempt failures |
| pdf_checks | Re-executed comparisons bound to result, box, PBP, PDF, mappings, parser/library and comparison rule |
| pdf_capability | Final combined core rank/percentile eligibility and separate stronger PDF capabilities |

Observed games and matching score totals cannot establish complete coverage.
The reviewed expected inventory is an explicit trust root: its receipt binds
original evidence bytes and a reviewed inventory digest. It is not an automated
proof that arbitrary provider text is authoritative. All requested dates must be
listed and finalized before complete-scope claims pass. Provider participant
declarations must exactly match played appearances; DNPs and zero-point played
participants remain distinct. Synthetic authority only tests the machinery and
must never certify real league coverage.

Missing inventory or participant evidence leaves values `observed_sample`.
Complete membership can establish player completeness; complete metric inputs
and a known qualifying population can establish cohort completeness. Ranks and
percentiles are separate capabilities. Percentiles require at least two qualified
players. All records remain `runtime_eligible: false` in this offline phase.

Each metric uses identical complete-case appearance membership for its required
components. Ratios use sums, including across seasons, rather than averages of
percentages. Games played counts affirmative played appearances, including
reported zero minutes. Unknown or unverified legacy-zero minutes cannot support
minute-dependent metrics. Missing values remain unknown. Precision classes cannot
be silently mixed.

Average/ratio qualification defaults to 70% of the maximum eligible team games,
summed across requested seasons. An unknown team-game denominator leaves
qualification unknown, never a guessed minimum of one. Explicit game minimums
are disclosed; totals default to one valid appearance. Shooting ratios require an
explicit positive attempt minimum and per-36 metrics an explicit minute minimum.
Overrides cannot bypass coverage or dependent component holds.

Disputes preserve selected observations. Identity, membership and result disputes
hold affected complete claims; component disputes hold dependent metrics. One
refused season does not invalidate an independently requested valid season, but
a pooled request cannot silently omit its refused season. No source fallback,
field mixing or NBA-box/ESPN-PBP bridge is introduced.

## Weekly freshness and corrections

Historical facts do not expire solely from capture age. Current scope discovery
requires a separately retained upstream receipt attested to the same inventory;
a review/build/replay clock cannot refresh it. The weekly profile warns after
eight days and blocks after ten days. Current cutoff lag also blocks after ten
days. Publication/control ages belong to the release phase; these offline records
cannot certify a publication.

Correction work is due at D+1 through D+7, D+14 and D+30, evaluated at the next
Sunday 8 a.m. America/Los_Angeles refresh with DST respected. Work more than ten
days overdue makes shared capture health stale. A fresh unchanged capture can
satisfy milestones only after re-normalization proves the selected result/box
revisions unchanged. Finalized old game captures can carry forward.

Open discrepancies also receive D+21/D+28 checks and a post-D+30 `needs_review`
diagnostic. These reviews identify the specific observation, membership hold and
components; a disputed assist does not mark unrelated games or DREB unresolved.
Dependent component holds remain until evidence-backed adjudication or a fresh
compatible revision. These functions calculate health; they do not activate a
scheduler or fetch anything.

## PDF scope and failures

The existing native-text parser and comparator are reused. Initially they support
selected ESPN result/box/PBP chains plus an explicit verified NBA game alias for
the legacy PDF adapter. Historical NBA observations remain unsupported by this
adapter. They are never relabeled as ESPN. Overtime and scans remain unsupported
for PDF verification while staying in core coverage denominators.

The atomic supported set is played membership, PTS, FGM, FG3M, FTM, AST and assist
events. A match grants no PDF credit for attempts, shooting percentages, DREB,
other rebounds, minutes or other unregistered components. Stronger full-scope
corroboration needs applicable matches for every selected game plus complete
scope coverage; verifying one game cannot certify a season.

Printed date/team identity, box membership and parser reconciliation are checked
before comparison. A readable same-game box with an unresolved or additional
played participant holds membership without inventing an ID. An accepted
supported-count/event discrepancy holds the atomic supported components and
their dependent certified claims. Unreadable/missing PDFs do not invent a
statistical disagreement. Base `coverage` is a prerequisite audit record; consumers
must use **final `pdf_capability` rank/percentile flags** after PDF evidence is
evaluated, so new contradictions cannot be bypassed with an earlier base flag.

Acquisition and comparison are separate. Outcomes include not attempted, not yet
due (before the next Eastern calendar day), unavailable, unsupported, stale,
mismatch and matched. The latest attempt remains visible. A later failed download
does not erase a valid match for unchanged facts/document bytes. New successful
document bytes supersede older positive matches. Negative discrepancy holds
survive unreadable replacements until a later compatible match or explicit
adjudication resolves them. Changed result, box, PBP,
mapping or parser/rule versions require applicable new checks. Legacy match flags
alone are audit-only.

Large captures use local content-addressed artifacts under private `reports/`.
The CLI copies bytes to SHA-named files without overwriting conflicting existing
content, preserving original receipt clocks. Stored references resolve containment
(including symlinks), enforce per-file bounds and verify SHA before parsing on
every append/read. Removing or modifying referenced evidence makes verification
fail closed. Keep artifacts with their dependent ledger; copying paths elsewhere
creates new input IDs. Inline evidence remains supported for small fixtures.
Bounded metadata records can reference a whole scope without embedding all PDFs
and PBP bytes in a single 16 MiB ledger record. No automatic cleanup is enabled.

## Commands

Use the project virtualenv and `PYTHON_DOTENV_DISABLED=1`. Inputs and outputs stay
private. Each output directory must be new; reusing exact inputs in the same
ledger yields no-op appends. Exit zero means evaluation completed, not completeness
or publication approval.

```sh
python scripts/evaluate_unified_coverage.py \
  --source reports/retained/registry.sqlite \
  --selection reports/selection/selection.json \
  --ledger reports/evaluation/ledger.sqlite --output reports/evaluation/first \
  --metric pts --metric dreb --metric fg_pct --min-attempts 1

python scripts/check_unified_pdfs.py \
  --source reports/retained/registry.sqlite \
  --source-ledger reports/evaluation/ledger.sqlite --coverage-id COVERAGE_HASH \
  --bundle reports/pdf/bundle.json \
  --ledger reports/pdf/ledger.sqlite --output reports/pdf/first
```

Optional evaluation inputs are `--authority`, `--disputes`, and
`--revalidations` JSON lists. The PDF bundle has `schema_version:
offline_pdf_bundle/1`, `attempts` and `pbp_sources`. Each captured PDF supplies
exactly one of `pdf_base64` or `pdf_path`; each PBP capture supplies exactly one of
`source_body` or `source_path`, with the inline field explicitly null when using a
path. Referenced inputs must already be SHA-named artifacts under `reports/`.
Original receipt hashes/clocks and observation IDs are mandatory. The CLI retains
them beside its new ledger. See the ledger schema and tests for exact fields.

Source SQLite inputs are read-only and hash-checked before/after. The PDF CLI
copies the evaluation ledger through read-only SQLite backup and appends only to
its separate destination. Schema failures omit raw observations/document bytes
from exceptions. Live acquisition, authority review, governed warehouse releases,
shared readers, Jobs integration and controlled runtime adoption remain separate
work.
