"""Read-only provider catalog audit; never changes Ask's enabled models."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import AGENT_MODEL_VALUES, get_settings


def fetch_catalog(provider: str, key: str, configured: set[str]) -> dict[str, dict]:
    client: Any
    if provider in {"openai", "openrouter"}:
        from openai import OpenAI

        client = OpenAI(
            api_key=key,
            timeout=30,
            max_retries=1,
            base_url="https://openrouter.ai/api/v1"
            if provider == "openrouter"
            else None,
        )
    else:
        from anthropic import Anthropic

        client = Anthropic(api_key=key, timeout=30, max_retries=1)
    with client:
        # SDK iteration follows all pages, including Anthropic's default 20-item page.
        models = {
            item.id: {"shutdown_date": getattr(item, "shutdown_date", None)}
            for item in client.models.list()
        }
        # Aliases may be retrievable even when only dated IDs appear in the list.
        for model in (
            sorted(configured - models.keys()) if provider != "openrouter" else []
        ):
            try:
                item = client.models.retrieve(model)
            except Exception as exc:
                if getattr(exc, "status_code", None) == 404:
                    continue
                raise
            models[model] = {
                "shutdown_date": getattr(item, "shutdown_date", None),
                "resolved_id": item.id,
            }
        return models


def audit_provider(
    provider: str,
    key: str,
    configured: set[str],
    previous: dict,
    now: str,
    fetch=fetch_catalog,
) -> dict:
    result: dict[str, Any] = {"checked_at": now, "configured": sorted(configured)}
    try:
        if not key:
            raise ValueError("missing_credentials")
        models = fetch(provider, key, configured)
    except Exception as exc:
        # Do not persist SDK exception strings: they may contain request details.
        result.update(
            status="stale" if previous.get("last_success_at") else "unverified",
            error="missing_credentials" if not key else type(exc).__name__,
            last_success_at=previous.get("last_success_at"),
            models=previous.get("models", {}),
        )
        return result
    previous_ids = set(previous.get("models", {}))
    prefix = (
        ""
        if provider == "openrouter"
        else "claude-"
        if provider == "claude"
        else "gpt-"
    )
    result.update(
        status="fresh",
        last_success_at=now,
        models=models,
        baseline=not bool(previous.get("last_success_at")),
        added=sorted(models.keys() - previous_ids) if previous_ids else [],
        removed=sorted(previous_ids - models.keys()),
        unconfigured=sorted(
            m for m in models if m.startswith(prefix) and m not in configured
        ),
        unavailable=sorted(configured - models.keys()),
        retiring={
            m: v["shutdown_date"] for m, v in models.items() if v.get("shutdown_date")
        },
    )
    return result


def markdown(report: dict) -> str:
    lines = [
        "# Ask model catalog review",
        "",
        f"Checked: {report['checked_at']}",
        "",
        "Discovery only. No model or default was changed. Unconfigured IDs need compatibility review; they are not recommendations.",
        "",
    ]
    for provider, item in report["providers"].items():
        lines += [
            f"## {provider}",
            "",
            f"Status: {item['status']}",
            f"Last successful check: {item.get('last_success_at') or 'never'}",
            "",
        ]
        if item["status"] != "fresh":
            lines += [
                f"Refresh failed: {item['error']}. Availability is unknown; any stored catalog is stale.",
                "",
            ]
            continue
        if item["baseline"]:
            lines += [
                "First successful snapshot; release changes will be compared on subsequent runs.",
                "",
            ]
        for label in ("unavailable", "unconfigured", "added", "removed", "retiring"):
            values = item[label]
            lines += [f"### {label.capitalize()}", ""]
            if isinstance(values, dict):
                lines += [f"- `{m}`: {day}" for m, day in sorted(values.items())]
            else:
                lines += [f"- `{m}`" for m in values]
            if not values:
                lines += ["None."]
            lines += [""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("reports/model-catalog")
    )
    args = parser.parse_args()
    folder = args.output_dir
    folder.mkdir(parents=True, exist_ok=True)
    state_path = folder / "latest.json"
    try:
        previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    except (ValueError, OSError):
        previous = {}
    settings = get_settings()
    now = datetime.now(timezone.utc).isoformat()
    keys = {
        "openai": settings.openai_api_key,
        "claude": settings.anthropic_api_key,
        "openrouter": settings.openrouter_api_key,
    }
    report: dict[str, Any] = {"checked_at": now, "providers": {}}
    for provider, key in keys.items():
        configured = set(AGENT_MODEL_VALUES[provider])
        configured.add(
            settings.openrouter_agent_model
            if provider == "openrouter"
            else settings.openai_agent_model
            if provider == "openai"
            else settings.anthropic_agent_model
        )
        report["providers"][provider] = audit_provider(
            provider,
            key or "",
            configured,
            previous.get("providers", {}).get(provider, {}),
            now,
        )
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    temporary = folder / "latest.json.tmp"
    temporary.write_text(encoded)
    temporary.replace(state_path)
    (folder / "review.md").write_text(markdown(report))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    (folder / f"{stamp}.json").write_text(encoded)
    print(f"Model catalog review: {folder / 'review.md'}")
    return 0 if all(p["status"] == "fresh" for p in report["providers"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
