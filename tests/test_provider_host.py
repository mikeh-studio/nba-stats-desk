"""Helper compatibility is checked at discovery, before serving a request."""

from importlib import import_module
from types import SimpleNamespace

import pytest
from app.agent import intelligence, provider_host
from app.agent.reference_planner import ReferenceProvider


def test_audited_helpers_exist_at_their_public_owner():
    for module, name in provider_host.HELPER_AUDIT:
        assert callable(getattr(import_module(module), name))
    provider_host.require_host_helpers(("nba_provider_helpers/1",))


@pytest.mark.parametrize(
    "versions",
    [
        (),
        ("nba_provider_helpers/0",),
        ("nba_provider_helpers/2",),
        ["nba_provider_helpers/1"],
        (1,),
        ("nba_provider_helpers/1", "nba_provider_helpers/1"),
    ],
)
def test_unsupported_or_malformed_helper_versions_fail(versions):
    with pytest.raises(ValueError, match="Unsupported provider helper version"):
        provider_host.require_host_helpers(versions)


def test_incompatible_helper_surface_fails_at_provider_discovery(monkeypatch):
    constructed = []

    def factory():
        provider_host.require_host_helpers(("nba_provider_helpers/0",))
        constructed.append(True)
        return ReferenceProvider()

    monkeypatch.setattr(
        intelligence,
        "entry_points",
        lambda **kwargs: [SimpleNamespace(load=lambda: factory)],
    )
    intelligence._load.cache_clear()
    try:
        with pytest.raises(intelligence.ProviderConfigurationError):
            intelligence._load("incompatible-helper-test")
        assert not constructed
    finally:
        intelligence._load.cache_clear()


def test_future_helper_version_requires_package_opt_in(monkeypatch):
    monkeypatch.setattr(provider_host, "HOST_HELPER_VERSION", "nba_provider_helpers/2")
    with pytest.raises(ValueError):
        provider_host.require_host_helpers(("nba_provider_helpers/1",))
