"""Explicit offline Ask shadow adapter; never registered in StatsAgent/factories."""

from app.serving.context import PTSShadowRequest, ReadContext
from app.serving.contract import canonical_json
from app.serving.presentation import render


class HistoricalPTSAskShadow:
    def __init__(self, context: ReadContext):
        self._context = context

    def answer(self, request: PTSShadowRequest):
        evidence = self._context.observe(request)
        payload = render(evidence, "ask", self._context.authorize())
        self._context.check_emission(payload)
        return payload

    def answer_json(self, request: PTSShadowRequest):
        payload = self.answer(request)
        data = canonical_json(payload)
        self._context.check_emission(payload)
        return data

    def answer_sse(self, request: PTSShadowRequest):
        # A lazy, terminal-only offline envelope: no unverified partial prose.
        data = self.answer_json(request).decode("utf-8")
        import json

        self._context.check_emission(json.loads(data))
        yield "event: final\ndata: " + data + "\n\n"
