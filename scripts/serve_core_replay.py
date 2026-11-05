"""Explicitly synthetic localhost demo; no credentials, collectors or warehouse."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.serving.core_application import create_core_application
from scripts.build_synthetic_core_replay import synthetic_bundle
from scripts.validate_publication_contract import digest


def application():
    bundle = synthetic_bundle()
    pin = digest(bundle)
    return create_core_application(
        bundle,
        expected_digest=pin,
        clock=lambda: "2026-11-01T16:00:00Z",
        authorize=lambda binding, sha: (
            binding == bundle["binding"]["binding_id"] and sha == pin
        ),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(application(), host="127.0.0.1", port=8033, proxy_headers=False)
