from dataclasses import replace

import pytest
from app.config import get_settings
from app.security import rate_limit_identity, require_local_history
from fastapi import HTTPException
from starlette.requests import Request


def request(peer="192.0.2.10", host="localhost", **headers):
    return Request(
        {
            "type": "http",
            "scheme": "http",
            "path": "/api/agent/history",
            "query_string": b"",
            "server": ("localhost", 80),
            "client": (peer, 123),
            "headers": [
                (b"host", host.encode()),
                *[
                    (k.replace("_", "-").encode(), v.encode())
                    for k, v in headers.items()
                ],
            ],
        }
    )


def test_untrusted_peer_cannot_rotate_forwarded_identity():
    for forwarded in ("198.51.100.1", "198.51.100.2", "bad"):
        assert (
            rate_limit_identity(request(x_forwarded_for=forwarded), ()) == "192.0.2.10"
        )


def test_trusted_proxy_walk_ignores_spoofed_prefix():
    req = request(peer="10.0.0.2", x_forwarded_for="203.0.113.99, 192.0.2.10, 10.0.0.1")
    assert rate_limit_identity(req, ("10.0.0.0/24",)) == "192.0.2.10"


@pytest.mark.parametrize("header", ["bad", "192.0.2.10,", "1" * 2049])
def test_invalid_forwarded_chain_fails_closed(header):
    assert (
        rate_limit_identity(
            request(peer="10.0.0.2", x_forwarded_for=header), ("10.0.0.0/24",)
        )
        == "10.0.0.2"
    )


def test_ipv6_client_address_is_canonical():
    assert (
        rate_limit_identity(
            request(peer="::1", x_forwarded_for="2001:db8::1"), ("::1/128",)
        )
        == "2001:db8::1"
    )


@pytest.mark.parametrize(
    "req",
    [
        request(),
        request(peer="127.0.0.1", host="evil.example"),
        request(peer="127.0.0.1", origin="https://evil.example"),
        request(peer="127.0.0.1", x_forwarded_for="127.0.0.1"),
        request(peer="127.0.0.1", forwarded="for=127.0.0.1"),
    ],
)
def test_history_rejects_remote_proxy_and_rebinding_requests(req):
    with pytest.raises(HTTPException) as error:
        require_local_history(req, replace(get_settings(), agent_history_enabled=True))
    assert error.value.status_code == 403


def test_direct_local_history_and_disabled_public_history_are_allowed():
    require_local_history(
        request(peer="127.0.0.1", origin="http://localhost"),
        replace(get_settings(), agent_history_enabled=True),
    )
    require_local_history(
        request(), replace(get_settings(), agent_history_enabled=False)
    )


@pytest.mark.parametrize("network", ["0.0.0.0/0", "::/0", "invalid"])
def test_invalid_or_universal_proxy_trust_is_rejected(network):
    with pytest.raises(ValueError):
        replace(get_settings(), agent_trusted_proxy_cidrs=(network,))


@pytest.mark.parametrize(
    "peer,host,origin",
    [("127.0.0.1", "localhost", "http://localhost"), ("::1", "[::1]", "http://[::1]")],
)
def test_direct_local_positive_origin_and_ipv6(peer, host, origin):
    from app.security import is_direct_local

    assert is_direct_local(request(peer=peer, host=host, origin=origin))


@pytest.mark.parametrize(
    "header,value",
    [
        ("origin", "null"),
        ("origin", ""),
        ("forwarded", "for=127.0.0.1"),
        ("x-real-ip", "127.0.0.1"),
        ("via", "proxy"),
        ("cf-connecting-ip", "127.0.0.1"),
        ("true-client-ip", "127.0.0.1"),
        ("x-client-ip", "127.0.0.1"),
        ("x-original-forwarded-for", "127.0.0.1"),
    ],
)
def test_trace_boundary_rejects_proxy_and_non_origin_headers(header, value):
    from app.security import is_direct_local

    assert not is_direct_local(request(peer="127.0.0.1", **{header: value}))


@pytest.mark.parametrize(
    "header,value", [(b"host", b"localhost"), (b"origin", b"http://localhost")]
)
def test_trace_boundary_rejects_duplicate_host_or_origin(header, value):
    from app.security import is_direct_local

    req = request(peer="127.0.0.1", origin="http://localhost")
    req.scope["headers"].append((header, value))
    assert not is_direct_local(req)
