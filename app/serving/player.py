"""PTS section only; deliberately not the full WarehouseRepository protocol."""

from app.serving.context import PTSShadowRequest, ReadContext
from app.serving.presentation import render


class PlayerPTSShadow:
    def __init__(self, context: ReadContext):
        self._context = context

    def get_pts(self, request: PTSShadowRequest):
        evidence = self._context.observe(request)
        payload = render(evidence, "player", self._context.authorize())
        self._context.check_emission(payload)
        return payload
