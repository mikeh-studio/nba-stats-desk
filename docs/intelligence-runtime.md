# Running intelligence providers

The app defaults to `ASK_INTELLIGENCE_PROVIDER=reference`. Installing an optional
package does not change this selection. Reference planning and the governed
public handlers need no model credentials; source access and
`OPENAI_AGENT_ENABLED` still apply independently.

Set `ASK_INTELLIGENCE_PROVIDER` to one installed entry-point name in
`nba_stats_desk.intelligence.v1` to opt into a compatible provider. Unknown,
duplicate or incompatible selections fail startup. A selected model-backed
provider needs configured host model credentials. No module paths, request
parameters, URLs or automatic fallback select a provider.

Reference accepts complete scalar requests such as “Alex Example points per game
in the 2024-25 regular season,” explicit top-N rankings with a game minimum, and
same-player season comparisons. It also supports the bounded “who led the league
in scoring [season]” grammar and the existing verified appearance-floor policy.
The public overview, two-player scorecard and split handlers retain their existing
scope grammars. A metric-change continuation uses the authorized prior scope and
produces a fresh plan. Shooting rankings require an attempt threshold. Unknown
modifiers are withheld; reference is not intended to match production language
coverage.

The host owns the plan schema, transport, configured model, retries, deadline and
usage. Three transport attempts and 3,000 output tokens per attempt bound hook
planning within the configured planner timeout and remaining request deadline.
Providers cannot supply SQL, tool calls, evidence or authoritative aggregation
provenance. Returned records are checked structurally before the two approved
ranking normalizations, then public scope/identity/semantic validation and normal
evidence execution. Provider failures use sanitized error envelopes; they never
trigger another provider.

Governed answers continue to use deterministic public rendering. Optional answer
prompt text is available through the contract but is not consumed by this path;
there are no new model-written factual answers. Existing special public handlers
and the optional legacy repository workflow keep their separate contracts.

See [conformance criteria](intelligence-acceptance.md), the
[ownership audit](intelligence-ownership-audit.md), and the
[provider contract](intelligence-contract.md). Local fixture tests do not establish
live warehouse/model quality or authorize deployment or public publication.

The offline evaluation server explicitly installs its authored fixture adapter;
it is never registered by the application loader. Set `ASK_EVAL_INTELLIGENCE=reference`
for reference-only evaluation. Live evaluation respects `ASK_INTELLIGENCE_PROVIDER`
unless `ASK_EVAL_INTELLIGENCE` overrides it. The live semantic-language runner
requires an explicitly selected model-backed provider and records its prompt hash.

Before a future production cutover or dependency pin bump, explicitly configure
`ASK_INTELLIGENCE_PROVIDER=production` and install its reviewed package. Otherwise
the application intentionally keeps the deterministic reference default. Provider
readiness checks the current request's provider credentials/model (OpenAI at
startup/default), not an unrelated provider's configured key.
