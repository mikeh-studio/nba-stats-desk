"""Loopback-only synthetic metric selection demo; no provider or warehouse calls."""

import argparse
import os
import sys
from pathlib import Path

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def create():
    from app.serving.core_application import create_core_application
    from app.serving.metric_products import make_core_product
    from app.serving.publication_contract import digest
    from app.serving.shot_products import make_shot_product
    from scripts.build_synthetic_core_replay import synthetic_bundle

    bundle = synthetic_bundle()
    core = make_core_product(bundle["manifest"], bundle["binding"])
    games = sorted(bundle["accepted"]["games"], key=lambda r: r["game_id"])
    coverage, rows = [], []
    for i, game in enumerate(games):
        coverage.append(
            dict(
                game_id=game["game_id"],
                game_date=game["game_date"],
                status="complete",
                expected_events=1 if i == 0 else 9,
            )
        )
        for pid in (101, 202):
            rows.append(
                dict(
                    game_id=game["game_id"],
                    player_id=pid,
                    observed_attempts=(1 if i == 0 else 9) if pid == 101 else 0,
                    contested_attempts=1 if pid == 101 and i == 0 else 0,
                )
            )
    shots = make_shot_product(
        core,
        coverage=coverage,
        rows=rows,
        expires_at=core["expires_at"],
        source_revision=digest(["synthetic source", rows]),
        inventory_revision=digest(["synthetic inventory", coverage]),
        source_through=max(g["game_date"] for g in games),
    )
    expected = digest(bundle)
    shot_digest = digest(shots["product"])
    return create_core_application(
        bundle,
        expected_digest=expected,
        clock=lambda: "2026-11-01T16:00:00Z",
        authorize=lambda bid, content: (
            bid == bundle["binding"]["binding_id"] and content == expected
        ),
        metric_product=core,
        shot_product=shots,
        shot_authorize=lambda product: digest(product) == shot_digest,
        metric_product_options=("core_box_score", "shot_events"),
    )


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8038)
    args = parser.parse_args()
    uvicorn.run(create(), host="127.0.0.1", port=args.port, proxy_headers=False)
