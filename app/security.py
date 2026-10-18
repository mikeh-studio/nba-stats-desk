"""Ingress identity and local-only history boundaries."""

from ipaddress import ip_address, ip_network

from fastapi import HTTPException, Request

from app.config import Settings


def rate_limit_identity(request: Request, trusted_proxies: tuple[str, ...]) -> str:
    peer = request.client.host if request.client else "unknown"
    try:
        address = ip_address(peer)
    except ValueError:
        return peer
    networks = [ip_network(value) for value in trusted_proxies]
    if not any(address in network for network in networks):
        return str(address)
    values = request.headers.getlist("x-forwarded-for")
    if len(values) != 1 or len(values[0]) > 2048:
        return str(address)
    try:
        chain = [ip_address(value.strip()) for value in values[0].split(",")]
    except ValueError:
        return str(address)
    if len(chain) > 32:
        return str(address)
    # Walk from the socket inward; the first untrusted hop is the client.
    for hop in reversed(chain):
        if not any(hop in network for network in networks):
            return str(hop)
    return str(address)


def require_local_history(request: Request, settings: Settings) -> None:
    if not settings.agent_history_enabled:
        return
    if not is_direct_local(request):
        raise HTTPException(
            status_code=403,
            detail="Server history is available only through direct localhost access. Disable AGENT_HISTORY_ENABLED for public Ask access.",
        )


def is_direct_local(request: Request) -> bool:
    """Shared opt-in private storage boundary; requires original socket peer."""
    try:
        local_peer = bool(
            request.client and ip_address(request.client.host).is_loopback
        )
    except ValueError:
        local_peer = False
    local_host = request.url.hostname in {"localhost", "127.0.0.1", "::1"}
    forwarded = any(
        name
        in {
            "forwarded",
            "x-real-ip",
            "via",
            "true-client-ip",
            "cf-connecting-ip",
            "x-client-ip",
        }
        or name.startswith(("x-forwarded-", "x-original-forwarded-"))
        for name in request.headers
    )
    origin = request.headers.get("origin")
    same_origin = len(request.headers.getlist("origin")) <= 1 and (
        origin is None or origin == f"{request.url.scheme}://{request.url.netloc}"
    )
    local_host = local_host and len(request.headers.getlist("host")) == 1
    return local_peer and local_host and not forwarded and same_origin
