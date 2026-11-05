"""Assemble a weekly proposal from verified inputs; never grant authority.

A separate validator must attest to the final manifest and binding before the
production promoter or reader accepts them.
"""

import copy

from app.serving.publication import ordered
from app.serving.publication_contract import (
    PROFILE,
    allocation_key,
    digest,
    fingerprint,
    validate_binding,
)
from app.serving.weekly_profile import weekly_scope


def proposal(
    *, accepted, models, evidence, project, namespace, materialized_at, generation
):
    scope = weekly_scope(accepted["scope"])
    if accepted["status"] != "accepted" or accepted["runtime_eligible"] is not False:
        raise ValueError("verified offline accepted input required")
    # All declarations come from explicit verified receipt metadata, never a demo.
    required = {
        "run_id",
        "versions",
        "inputs",
        "selected_games",
        "checks",
        "freshness",
        "validation_as_of",
        "auxiliary",
        "historical_routes",
    }
    if set(evidence) != required:
        raise ValueError("incomplete weekly evidence")
    manifest = dict(
        version="publication-manifest/2",
        profile="core_stats_weekly_v1",
        purpose="production",
        evidence_kind="retained",
        scope=scope,
        run_id=evidence["run_id"],
        build_id=evidence["run_id"],
        versions=copy.deepcopy(evidence["versions"]),
        inputs=copy.deepcopy(evidence["inputs"]),
        selected_games=copy.deepcopy(evidence["selected_games"]),
        checks=copy.deepcopy(evidence["checks"]),
        freshness=copy.deepcopy(evidence["freshness"]),
        auxiliary=copy.deepcopy(evidence["auxiliary"]),
        validation_as_of=evidence["validation_as_of"],
        materialized_at=materialized_at,
        built_at=materialized_at,
        data_through=scope["last"],
        capabilities=list(PROFILE["capabilities"]),
    )
    manifest["allocation_key"] = allocation_key(manifest)
    manifest["input_fingerprint"] = fingerprint(manifest)
    manifest["publication_id"] = (
        "pub_" + digest([manifest["input_fingerprint"], project, namespace])[:16]
    )
    coverage = dict(
        expected_games=len(accepted["games"]),
        observed_games=len(accepted["games"]),
        expected_participants=len(accepted["participants"]),
        observed_participants=len(accepted["participants"]),
        played_rows=len(accepted["appearances"]),
        unresolved_identities=0,
        open_required_holds=0,
        observed_players=len(accepted["players"]),
        observed_teams=len(accepted["teams"]),
    )
    manifest["coverage"] = coverage
    manifest["fields"] = []
    for name in PROFILE["assets"]["fct_player_game_stats"]["required_fields"]:
        missing = sum(row[name] is None for row in models["fct_player_game_stats"])
        manifest["fields"].append(
            dict(
                name=name,
                missing_count=missing,
                status="nullable" if missing else "complete",
            )
        )
    manifest["assets"] = [
        dict(
            name=name,
            relation=f"{project}.{namespace}_release_{manifest['publication_id'][4:]}.{name}",
            schema_version="core_stats_weekly_v1",
            digest=digest(ordered(name, models[name])),
            rows=len(models[name]),
            dependencies=spec["dependencies"],
            fields=spec["required_fields"],
            built_from=manifest["input_fingerprint"],
        )
        for name, spec in PROFILE["assets"].items()
    ]
    routes = copy.deepcopy(evidence["historical_routes"])
    routes.append(
        dict(
            season=scope["season"],
            phase=scope["phase"],
            mode="release",
            target=manifest["publication_id"],
        )
    )
    binding = dict(
        version="publication-binding/2",
        binding_id=manifest["publication_id"],
        generation=generation,
        publication_id=manifest["publication_id"],
        manifest_digest=digest(manifest),
        profile=manifest["profile"],
        relations={a["name"]: a["relation"] for a in manifest["assets"]},
        routes=routes,
        capabilities=manifest["capabilities"],
        expires_at=manifest["freshness"]["expires_at"],
    )
    validate_binding(binding, manifest)
    return dict(manifest=manifest, binding=binding)
