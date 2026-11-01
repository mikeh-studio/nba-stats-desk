"""PTS section only; deliberately not the full WarehouseRepository protocol."""

from app.serving.context import PTSShadowRequest, ReadContext
from app.serving.contract import ServingError
from app.serving.presentation import render


class PlayerPTSShadow:
    def __init__(self, context: ReadContext):
        if type(context) is not ReadContext:
            raise ServingError("unsupported_version")
        self._context = context

    def get_pts(self, request: PTSShadowRequest):
        evidence = self._context.observe(request)
        payload = render(evidence, "player", self._context.authorize())
        self._context.check_emission(payload)
        return payload


class CurrentPlayerPTSShadow(PlayerPTSShadow):
    def __init__(self, context):
        from app.serving.current import CurrentReadContext

        if type(context) is not CurrentReadContext:
            raise ServingError("unsupported_version")
        self._context = context
