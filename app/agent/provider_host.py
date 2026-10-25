"""Versioned public helper surface used by installed intelligence packages.

The audit list is maintenance metadata, not an import sandbox or a claim that
all public functions are provider APIs. Keep semantic execution owned here.
"""

HOST_HELPER_VERSION = "nba_provider_helpers/1"

# Public capabilities, reviewed separately from private planning heuristics.
HELPER_AUDIT = (
    ("app.agent.plan_execution", "execute_plan"),
    ("app.agent.plan_execution", "leader_game_floor"),
    ("app.agent.plan_execution", "normalize_duplicate_rank_plan"),
    ("app.agent.plan_execution", "single_period_rank_plan"),
    ("app.agent.scope_policy", "explicit_scope"),
    ("app.agent.scope_policy", "has_specific_metric"),
    ("app.agent.semantics", "load_contract"),
    ("app.agent.semantics", "SemanticError"),
)


def require_host_helpers(supported_versions: tuple[str, ...]) -> None:
    """Reject helper incompatibility during provider import/factory construction.

    Packages pass their own literal supported versions, never this host's current
    constant. Discovery sanitizes the exception as ProviderConfigurationError.
    Exact source identity and import-origin checks belong to the assembly verifier.
    """
    if (
        type(supported_versions) is not tuple
        or not supported_versions
        or any(type(version) is not str for version in supported_versions)
        or len(set(supported_versions)) != len(supported_versions)
        or HOST_HELPER_VERSION not in supported_versions
    ):
        raise ValueError("Unsupported provider helper version")
