"""Open an explicitly selected offline weekly snapshot for a candidate app."""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

from app.serving.contract import ServingError
from app.serving.current import CurrentReadContext, CurrentSnapshotHandle


def _selected_snapshot_id(data_root):
    base = Path(data_root).resolve() / "reports/weekly-snapshots"
    value = json.loads((base / "app-selection.json").read_bytes())
    if (
        type(value) is not dict
        or set(value) != {"schema", "snapshot_id", "run_id", "runtime_eligible"}
        or value["schema"] != "offline-weekly-app-selection/1"
        or value["runtime_eligible"] is not False
        or not isinstance(value["snapshot_id"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["snapshot_id"])
        or not isinstance(value["run_id"], str)
        or not re.fullmatch(r"[0-9a-f]{32}", value["run_id"])
    ):
        raise ValueError("invalid selection")
    return value["snapshot_id"]


def open_weekly_selection(data_root, policy):
    """Read the operator's offline selection and bind existing reader guards.

    The caller provides the read policy. A selection does not grant authority or
    configure a live runtime.
    """
    base = Path(data_root).resolve() / "reports/weekly-snapshots"
    try:
        sid = _selected_snapshot_id(data_root)
        handle = CurrentSnapshotHandle.open(base / "snapshots" / sid, policy)
        manifest = handle.describe()
        return CurrentReadContext.bind(
            handle, expected_snapshot_id=sid, scope=manifest["scope"]
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ServingError("weekly_selection_unavailable") from exc


def create_weekly_candidate_application(
    data_root,
    *,
    policy,
    binding,
    check_binding,
    players,
    settings,
    jobs_app=None,
    historical_root=None,
    historical_policy=None,
    historical_snapshot_id=None,
    composition_authority=None,
    binding_loader=None,
    refresh_context_loader=None,
):
    """Compose the selected weekly snapshot into the existing offline app."""
    from app.serving.candidate_application import create_candidate_application
    from app.serving.candidate_service import CandidatePTSService

    historical = (
        historical_root,
        historical_policy,
        historical_snapshot_id,
        composition_authority,
    )
    if any(value is not None for value in historical) and any(
        value is None for value in historical
    ):
        raise ServingError("weekly_historical_configuration_incomplete")
    if binding_loader is not None and not callable(binding_loader):
        raise ValueError("binding loader must be callable")
    if refresh_context_loader is not None and not callable(refresh_context_loader):
        raise ValueError("refresh context loader must be callable")
    if refresh_context_loader is not None and binding_loader is None:
        raise ValueError("refresh context requires a binding loader")

    def build(current_binding, context=None):
        context = context or {}
        allowed = {
            "policy",
            "check_binding",
            "players",
            "historical_policy",
            "composition_authority",
        }
        if type(context) is not dict or not set(context) <= allowed:
            raise ValueError("invalid refresh context")
        read_policy = context.get("policy", policy)
        selection = open_weekly_selection(data_root, read_policy)
        sid = selection.binding["snapshot_id"]
        if historical_root is not None:
            from app.serving.composite import TwoSeasonPTS

            selection = TwoSeasonPTS.open(
                historical_root,
                context.get("historical_policy", historical_policy),
                Path(data_root).resolve() / "reports/weekly-snapshots/snapshots" / sid,
                read_policy,
                historical_snapshot_id=historical_snapshot_id,
                current_snapshot_id=sid,
                authority=context.get("composition_authority", composition_authority),
            )
        service = CandidatePTSService(
            selection,
            binding=current_binding,
            check_binding=context.get("check_binding", check_binding),
            players=context.get("players", players),
            evidence_kind="retained-unapproved",
        )
        if _selected_snapshot_id(data_root) != sid:
            raise ServingError("weekly_selection_changed")
        return sid, service

    sid, service = build(binding)
    if binding_loader is None:
        return create_candidate_application(
            service, settings=settings, jobs_app=jobs_app
        )

    lock = threading.Lock()
    selected = [sid, service, binding]

    def current_service():
        from fastapi import HTTPException

        with lock:
            try:
                next_binding = binding_loader()
                if (
                    _selected_snapshot_id(data_root) != selected[0]
                    or next_binding != selected[2]
                ):
                    context = (
                        refresh_context_loader() if refresh_context_loader else None
                    )
                    next_sid, next_service = build(next_binding, context)
                    selected[:] = [next_sid, next_service, next_binding]
            except Exception:
                logging.getLogger(__name__).warning("Weekly selection refresh failed")
            try:
                selected[1].check()
            except ServingError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Candidate authority unavailable.",
                    headers={"Cache-Control": "no-store"},
                ) from exc
            return selected[1]

    return create_candidate_application(
        service, settings=settings, jobs_app=jobs_app, service_provider=current_service
    )
