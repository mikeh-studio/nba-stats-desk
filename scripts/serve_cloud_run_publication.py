"""Cloud Run launcher for the explicit publication-enabled app composition.

NBA_PUBLICATION_CONFIG is non-secret operator configuration; source policies,
revocation registries and the mode switch are read from separately controlled GCS.
"""

import json
import os
import re

os.environ["PYTHON_DOTENV_DISABLED"] = "1"


def config_from_environment():
    config = json.loads(os.environ["NBA_PUBLICATION_CONFIG"])
    if not isinstance(config, dict) or set(config) != {
        "project",
        "namespace",
        "source_authorities",
        "mode_path",
    }:
        raise ValueError("explicit cloud publication configuration required")
    if (
        not isinstance(config["source_authorities"], dict)
        or not config["source_authorities"]
    ):
        raise ValueError("source authority configuration required")
    if not isinstance(config["mode_path"], str) or not config["mode_path"].startswith(
        "gs://"
    ):
        raise ValueError("cloud mode control must use GCS")
    for provider, authority in config["source_authorities"].items():
        if (
            provider not in {"nba_api", "espn"}
            or not isinstance(authority, dict)
            or set(authority) != {"policy_path", "policy_digest", "registry_path"}
            or not all(
                isinstance(authority[k], str) and authority[k].startswith("gs://")
                for k in ("policy_path", "registry_path")
            )
            or not re.fullmatch(r"[a-f0-9]{64}", authority["policy_digest"])
        ):
            raise ValueError("cloud source authority configuration differs")
    return config


def main():
    import uvicorn
    from scripts.serve_production_candidate import create_config

    port = int(os.environ.get("PORT", "8080"))
    if not 1 <= port <= 65535:
        raise ValueError("invalid port")
    uvicorn.run(
        create_config(config_from_environment()),
        host="0.0.0.0",
        port=port,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
