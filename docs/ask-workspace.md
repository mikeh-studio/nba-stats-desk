# Ask workspace

## Two-player comparisons

Explicit `A vs B`, `A versus B`, and `Compare A and B` questions use a
deterministic two-player scorecard. Nicknames use the existing source-backed
alias catalog. Each side is resolved separately; ambiguous identities require
selection and already-resolved sides are retained during clarification.
The scorecard shares one date/phase scope and one qualified cohort per metric
across points, rebounds, assists, steals and blocks. Minutes and sample sizes
come from the same appearances. Differences use unrounded averages; missing
components withhold edges and percentiles rather than becoming zero.

The established winning-impact section currently has **unavailable values**.
The governed player-game source has no Win Shares or on/off possession totals.
No external metric feed has been added, no custom contribution score calculated,
and raw plus/minus is not substituted for possession-based net rating. The net
rating chart selector explains this missing-data state; the five core chart
selectors remain usable. Completing advanced analytics requires a separately
verified matching-scope source and its integration, not a UI placeholder change.

Supported scope is a shared date range and regular season, playoffs, or both.
Explicit playoff years resolve to the corresponding season. Round, clutch,
opponent, home/away, wins-only and per-possession/per-minute filters are not
silently approximated. Period-versus-period questions keep their existing route.
Saved comparison context includes both full names and explicit dates. Fill-only
follow-ups preserve that scope, and an assists follow-up selects the AST chart.
Narrow-screen tables scroll horizontally and are keyboard-focusable.

Ask uses one tab per conversation. Five tabs are visible on desktop, including
the new-question draft. At smaller widths, fewer tabs are shown; More remains
available. Opening a sixth chat moves the oldest open tab out of the strip. It
does not delete the conversation. Closing a tab has the same non-destructive
storage behavior. Follow-ups remain in their original conversation.

More opens searchable local history. Search includes opening questions and
follow-ups, and the local endpoint pages beyond the browser's recent cache.
Opening a result restores the stored response payload without rerunning queries.
The question selector lets users inspect earlier answers and their matching
tables/charts. New follow-ups continue the latest turn, not an older inspected
answer. Suggestions fill the follow-up composer without submitting.

## Persistence and boundaries

- Browser cache: the existing season-specific `askChatHistory:v1` key remains
  compatible. Up to 25 recent conversations (favoring open tabs) are cached;
  storage failures are surfaced rather than silently discarding in-memory work.
- Navigation/drafts: `askOpenTabs:v1:<season>` stores tab order, the active tab,
  and per-conversation drafts. Closing a tab retains its draft.
- Local history: when `AGENT_HISTORY_ENABLED=true`, the existing JSONL file
  retains all recorded turns. `/api/agent/history` supports `q`, `offset`, `limit`
  and exact `conversation_id` filtering. Local-access checks are unchanged.
- A restarted server can rehydrate conversational context from that local file.
  Governed overview context uses the saved explicit dates, not a newly evaluated
  relative window. Without the local file, browser-cached answers remain
  reviewable, but conversational context recovery is not guaranteed.
- This is local persistence, not authenticated multi-user or cloud-synced history.
  Tabs share browser-origin storage; simultaneous independent browser windows
  are not a collaborative editing surface.
- Navigation within Ask and the season selector are disabled during a request
  so streaming responses cannot land in another chat. Partial-stream failures
  are shown without automatically issuing a second paid request.

## Analysis presentation

Governed player overviews use the same evidence for the overall assessment,
up to three supported takeaways, five-stat percentile table and monthly chart.
The chart initially selects the strongest eligible category; PTS/REB/AST/STL/BLK
controls switch the series. Other Ask intents retain their original answer and
table/chart renderers instead of receiving invented overview insights.

The current overview can identify relative category strength, monthly variation
and defensive box-score activity. It does not yet compute opponent defensive
ratings or establish minutes/efficiency/turnover drivers. These limitations are
visible; no mockup numbers or causal explanations are embedded in the app.
Thin percentile bars use native meters. Icons are vendored Tabler assets under
their MIT license; the existing locally hosted Barlow fonts and real player
headshots are reused.
