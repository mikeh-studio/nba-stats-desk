# Reviewed synthetic provider recording

One authorized OpenAI `gpt-5.4-mini` capture on 2026-10-18, fixed semantic date
2026-10-18, 14 attempted calls (24-call cap), six selected synthetic cases, both
JSON and SSE. Files `manifest.json`, `terminal.json`, and `call-*.json` are byte-for-byte
copies of the original immutable private capture. No paid rerun was performed.

Agent privacy review inspected every request, response, output item, usage/finish
metadata, source/evidence/context, and schema. All question/statistical evidence
is fictional checked-in fixture content. Research planner identity options also
contain the already-public NBA pair catalog; no actual private study data was sent.
Provider response/message IDs and usage are retained for faithful shape replay;
metadata/user/safety identifier fields contain no account identity. No credentials,
local paths, personal emails, private conversations, or private attachments were found.
This agent privacy review is **not human expectation approval**. Cases remain pending.

The capture and historical exact offline replay both pass **4/6 cases**, with zero harness
mismatches. `date_range` returns total 12 instead of expected average 4 in both
transports. `research_home` chooses a study instead of a breakdown and returns an
unsupported-pair response in both transports. The date-range question is ambiguous
about average versus total, so its result is an unreviewed expectation mismatch,
not a confirmed application defect.
The original research result is a retained routing failure. Neither is an approved answer. The original independent case expectations were not changed.
`observed-outcomes.json` records which assertions failed, separately from those
expectations. Current-code replay passes **5/6**: bounded descriptive kind correction also applies
the existing breakdown Regular Season default instead of the inherited study Both
default. The original independent research checks now pass (6 points, 3 home
appearances). CI requires exit 1, the unchanged date-range failures, research
passing, every other outcome unchanged, all 14 calls consumed, and zero provider
calls. Historical capture files and `observed-outcomes.json` are unchanged. This
is not 6/6 accuracy or human expectation approval.

Replay the reviewed capture:

```bash
python scripts/evaluate_ask.py \
  --replay-cassettes tests/fixtures/ask/recordings/openai-mini-20261018 \
  --output reports/ask-harness/new-replay
```

Exit 1 remains expected for the preserved date-range mismatch.

Provider retention disclosure: the fictional research-scope calls (0012 and 0013)
omitted `store`, and their provider responses report `store: true`. The semantic
planner calls sent `store: false`. Standardizing retention across production routes
is a separate gap; this replay feature does not change those original requests or
claim to delete provider-held data. All captured content remains fictional/public
fixture material as described above.

This v1 bundle is accepted only with the pinned original 16 raw-file hashes and
canonical cassette hash. New captures use version 2 with manifest binding. Replay
covers the app's adapted provider boundary and does not reproduce provider timing,
post-call scheduling, or native-extension network behavior.
