# Running intelligence providers

The app defaults to `ASK_INTELLIGENCE_PROVIDER=reference`. Installing an optional
package does not change this selection. Reference planning and the governed
public handlers need no model credentials; source access and
`OPENAI_AGENT_ENABLED` still apply independently.

Set `ASK_INTELLIGENCE_PROVIDER` to one installed entry-point name in
`nba_stats_desk.intelligence.v2` to opt into a compatible provider. Unknown,
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

Governed answers use deterministic public rendering. Version 2 exposes only
planning-system text; it has no answer-generation prompt or call. Special public
handlers select validated scope or verified chart candidates before deterministic
rendering. The optional legacy repository workflow retains its separate narration
contract and is never a fallback for failed governed planning. See the
[model-call boundary](intelligence-contract.md#model-call-boundary).

See the [provider contract](intelligence-contract.md) for conformance tests and
[host releases](intelligence-contract.md#host-releases). Local fixture tests do not
establish live warehouse or model quality. The full Python suite requires Node.js
for the browser-producer/Python-consumer contract test; use the JavaScript
toolchain version configured in CI.

The offline evaluation server explicitly installs its authored fixture adapter;
it is never registered by the application loader. Set `ASK_EVAL_INTELLIGENCE=reference`
for reference-only evaluation. Live evaluation respects `ASK_INTELLIGENCE_PROVIDER`
unless `ASK_EVAL_INTELLIGENCE` overrides it. The live semantic-language runner
requires an explicitly selected model-backed provider and records its prompt hash.

To use an installed model-backed provider, set `ASK_INTELLIGENCE_PROVIDER` to its
entry-point name. Otherwise the application keeps the deterministic reference
default. Provider startup accepts any configured provider key/model pair. Requests check only their
selected provider and fail with sanitized `provider_unavailable` if it lacks
credentials/model. The factory cache is independent of readiness. Version 2
entry points reject incompatible version 1 installations at discovery.

## Trusted session profiles

A Python composition root may install `RuntimePolicy` with
`configure_runtime_policy(policy)` **before application lifespan**. With no policy,
reference and normal configured runtime behavior are unchanged. HTTP cannot install
or replace a policy. The consumer supplies an immutable model choice, positive call
and output-token ceilings, a source policy and a durable `BudgetAuthority`:

- `admit(question=..., streaming=..., provider=..., model=...)` returns an opaque
  fixed-case admission. A failed admitted request remains consumed.
- `reserve_attempt(admission, model=..., max_output_tokens=...)` durably reserves
  each physical attempt before send, including attempts that fail. It must enforce
  the session-wide ceiling across threads, processes, restarts and admissions.
- `finish(admission, outcome=...)` records `success`, `error`, `cancelled` or
  `admission_error`. Pending or failed journal writes must fail closed.

The host retains one admission through the shared JSON/SSE worker path and copied
contexts. SDK clones retain that authority. The initial profile supports only the
OpenAI Responses SDK at its fixed API endpoint, schema-only planning, `tools=[]`
and `store=False`. Planning and serialized output limits are clamped to the trusted
ceiling; both application and SDK retries are disabled. Unsupported providers,
client adapters, schema operations, endpoints and missing admissions fail before
transport. `on_wire_attempt` is an optional bounded-metadata observer immediately
at the SDK HTTP request hook, after reservation. The hook also checks the actual
serialized endpoint, model, token cap, storage and tools, and uses a single-use
permit to reject raw SDK calls, retries and redirects. Observers are not themselves proof of a
physical request; the offline tests also inspect actual SDK HTTP serialization.

The profile skips startup prewarm, keeps credential readiness checks, refuses
history/traces and alternate sources, and permits only the Ask shell, static assets
and JSON/SSE Ask endpoints. Unknown/background routes are rejected before their
FastAPI dependencies. Its source policy is installed before lifespan and applies
to identity and data reads. The production repository dependency requires a
source policy whenever a runtime profile is installed. Model-only fixture
compositions may omit a source policy with trusted dependency overrides. The
visible default season remains unchanged; explicit
question and evidence scopes determine historical analysis. Disconnect/cancellation
closes the admission to further attempts while an already dispatched request may
still finish and remains charged.

These controls govern trusted installed Python components through the host
transport. They are not a sandbox for arbitrary Python code or an external network
spending ceiling. A durable authority must implement its own exclusive session
creation, crash/restart rules and admission validation. Tests use synthetic data
and fake HTTP transports; zero-call deterministic answers establish bypass
behavior, not live-model or source readiness.
