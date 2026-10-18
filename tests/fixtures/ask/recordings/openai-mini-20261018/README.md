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

The capture and exact offline replay both pass **4/6 cases**, with zero harness
mismatches. `date_range` returns total 12 instead of expected average 4 in both
transports. `research_home` chooses a study instead of a breakdown and returns an
unsupported-pair response in both transports. The date-range question is ambiguous
about average versus total, so its result is an unreviewed expectation mismatch,
not a confirmed application defect.
The research result is a retained routing failure. Neither is an approved answer. The original independent case expectations were not changed.
`observed-outcomes.json` records which assertions failed, separately from those
expectations. The CI test requires exit 1, these exact failures, all other case
checks passing, all 14 calls consumed, and zero provider calls. It establishes
that the harness detects the recorded mismatches; it does not report 6/6 accuracy.

Replay the reviewed capture:

```bash
python scripts/evaluate_ask.py \
  --replay-cassettes tests/fixtures/ask/recordings/openai-mini-20261018 \
  --output reports/ask-harness/new-replay
```

Exit 1 is expected for the two preserved mismatches.
