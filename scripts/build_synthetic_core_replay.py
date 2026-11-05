"""Build an explicitly synthetic demonstration; never process real evidence."""

import json

from app.serving.core_models import build_models
from scripts.validate_publication_contract import (
    CONTRACTS,
    allocation_key,
    digest,
    fingerprint,
    validate_binding,
)


def synthetic_bundle():
    accepted = json.loads((CONTRACTS / "synthetic-accepted.json").read_text())
    manifest = json.loads((CONTRACTS / "synthetic-manifest.json").read_text())
    models = build_models(accepted, materialized_at=manifest["materialized_at"])
    manifest["selected_games"] = []
    for game in accepted["games"]:
        gid = game["game_id"]
        receipt = next(a for a in accepted["appearances"] if a["game_id"] == gid)
        manifest["selected_games"].append(
            dict(
                game_uid=game["game_uid"],
                game_id=gid,
                observation_id=receipt["observation_id"],
                fact_revision=receipt["fact_revision"],
                result_revision=game["result_revision"],
                box_revision=digest([gid, "box"]),
                decision_id="synthetic_" + gid,
                mapping_version=manifest["versions"]["identity"],
                captured_at="2026-11-01T12:00:00Z",
                last_verified_at="2026-11-01T12:00:00Z",
                participants=sum(p["game_id"] == gid for p in accepted["participants"]),
                played=sum(a["game_id"] == gid for a in accepted["appearances"]),
            )
        )
    manifest["selected_games"].sort(key=lambda r: r["game_uid"])
    manifest["inputs"].sort(key=lambda r: r["role"])
    for i in manifest["inputs"]:
        i["digest"] = digest(["synthetic", i["role"], accepted])
    manifest["allocation_key"] = allocation_key(manifest)
    manifest["input_fingerprint"] = fingerprint(manifest)
    manifest["coverage"].update(
        expected_games=len(accepted["games"]),
        observed_games=len(accepted["games"]),
        expected_participants=len(accepted["participants"]),
        observed_participants=len(accepted["participants"]),
        played_rows=len(accepted["appearances"]),
        observed_players=len(accepted["players"]),
        observed_teams=len(accepted["teams"]),
    )
    for f in manifest["fields"]:
        f["missing_count"] = sum(
            r[f["name"]] is None for r in models["fct_player_game_stats"]
        )
        f["status"] = "nullable" if f["missing_count"] else "complete"
    for a in manifest["assets"]:
        a.update(
            rows=len(models[a["name"]]),
            digest=digest(models[a["name"]]),
            built_from=manifest["input_fingerprint"],
        )
    binding = json.loads((CONTRACTS / "synthetic-binding.json").read_text())
    binding["manifest_digest"] = digest(manifest)
    validate_binding(binding, manifest)
    return dict(
        version="core-offline-bundle/1",
        accepted=accepted,
        models=models,
        manifest=manifest,
        binding=binding,
    )
