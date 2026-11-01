"""Re-execute retained native PDFs against exact selected ESPN revision chains."""

from __future__ import annotations

import copy
import hashlib
import io
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pypdf
from pypdf.errors import PyPdfError
from scripts import espn_corrections as layers
from scripts import espn_sources as source
from scripts import parse_gamebooks as parser
from scripts.fetch_gamebooks import inspect_pdf

from .artifacts import document_bytes as pdf_bytes
from .artifacts import source_text as pbp_text
from .identity import canonical, digest, evidence, require
from .ledger import record, validate
from .selection import timestamp
from .store import parse_json

FIELDS = ["membership", "pts", "fgm", "fg3m", "ftm", "ast", "assist_events"]


def validate_catalog(value, inventory):
    validate(value)
    p = value["payload"]
    require(
        value["kind"] == "mapping_catalog"
        and p["inventory_id"] == inventory["record_id"],
        "PDF mapping inventory binding differs",
    )
    catalog = p["catalog"]
    require(
        catalog["schema_version"] == "unified_identity_catalog/1"
        and catalog["runtime_eligible"] is False,
        "Unsupported PDF identity catalog",
    )
    require(
        catalog["catalog_version"]
        == inventory["payload"]["identity_catalog_version"]
        == digest({k: v for k, v in catalog.items() if k != "catalog_version"}),
        "PDF identity catalog integrity failed",
    )


def player_aliases(catalog):
    aliases = {}
    for row in catalog["player_aliases"]:
        key = (row["provider"], row["native_id"])
        require(key not in aliases, "Duplicate catalog player alias")
        aliases[key] = (row["player_id"], 1)
    for row in sorted(
        catalog["player_alias_corrections"],
        key=lambda r: (r["provider"], r["native_id"], r["version"]),
    ):
        key = (row["provider"], row["native_id"])
        require(
            key in aliases
            and aliases[key] == (row["previous_player_id"], row["version"] - 1),
            "PDF alias correction history differs",
        )
        aliases[key] = (row["player_id"], row["version"])
    return aliases


def validate_pdf_inputs(value, inputs, mappings, selection):
    validate(value)
    p = value["payload"]
    require(
        value["kind"] == "pdf_inputs"
        and p["evaluation_inputs_id"] == inputs["record_id"]
        and p["mapping_catalog_id"] == mappings["record_id"]
        and p["selection_id"] == selection["record_id"]
        and inputs["payload"]["inventory_id"]
        == mappings["payload"]["inventory_id"]
        == selection["payload"]["inventory_id"],
        "PDF inputs/dependencies cross inventory or selection pins",
    )
    selected_scope = next(
        (
            s
            for s in selection["payload"]["scopes"]
            if s["scope"] == inputs["payload"]["scope"]
        ),
        None,
    )
    require(selected_scope is not None, "PDF input scope outside selection")
    ids = set(selected_scope["selected"])
    as_of = timestamp(selection["payload"]["selection_as_of"])
    by_id = {o["observation_id"]: o for o in inputs["payload"]["observations"]}
    catalog = mappings["payload"]["catalog"]
    for oid in ids:
        obs = by_id[oid]
        scopes = [
            r
            for r in catalog["game_scopes"]
            if r["game_uid"] == obs["semantic"]["game_uid"]
        ]
        require(scopes, "PDF game scope absent from mapping catalog")
        current = max(scopes, key=lambda r: r["version"])
        require(
            current["scope"] == obs["semantic"]["scope"]
            and current["version"] == obs["semantic"]["scope_version"],
            "PDF game scope version differs from selected facts",
        )
    require(
        len({p["observation_id"] for p in p["pbp_sources"]}) == len(p["pbp_sources"]),
        "Duplicate selected PBP capture",
    )
    for pbp in p["pbp_sources"]:
        require(
            pbp["observation_id"] in ids,
            "PBP source outside selected provider observations",
        )
        receipt = evidence(pbp["receipt"])
        require(
            receipt["kind"] in ("provider_capture", "synthetic_fixture")
            and receipt["captured_at"] is not None
            and timestamp(receipt["captured_at"]) <= as_of,
            "PBP original receipt/hash/clock differs",
        )
        parse_json(pbp_text(pbp))
    require(
        len({digest(a) for a in p["attempts"]}) == len(p["attempts"]),
        "Duplicate PDF attempt",
    )
    captured_clocks = [
        (a["observation_id"], timestamp(a["checked_at"]))
        for a in p["attempts"]
        if a["acquisition"] == "captured" and a["checked_at"] is not None
    ]
    require(
        len(set(captured_clocks)) == len(captured_clocks),
        "Ambiguous simultaneous PDF captures",
    )
    for attempt in p["attempts"]:
        require(
            attempt["observation_id"] in ids,
            "PDF attempt outside selected provider observations",
        )
        if attempt["checked_at"] is not None:
            require(
                timestamp(attempt["checked_at"]) <= as_of,
                "PDF attempt postdates selection",
            )
        if attempt["acquisition"] == "captured":
            receipt = evidence(attempt["receipt"])
            require(
                receipt["kind"] in ("provider_capture", "synthetic_fixture")
                and receipt["captured_at"] == attempt["checked_at"]
                and receipt["captured_at"] is not None,
                "PDF must retain its original capture clock",
            )
            pdf_bytes(attempt)
        else:
            require(
                attempt["pdf_base64"] is None and attempt.get("pdf_path") is None,
                "Non-captured attempt cannot supply successful PDF bytes",
            )
            if attempt["receipt"] is not None:
                receipt = evidence(attempt["receipt"])
                require(
                    receipt["captured_at"] == attempt["checked_at"],
                    "Failed attempt clock differs",
                )


def _candidate(obs, pbp, catalog):
    sem = obs["semantic"]
    raw = parse_json(pbp_text(pbp))
    s = sem["scope"]
    game = source.event_game(raw["header"], s["season"], date.fromisoformat(s["date"]))
    require(
        game["source_game_id"] == obs["source_game_id"]
        and game["home"] == s["home"]
        and game["away"] == s["away"],
        "PBP scope differs from selected game",
    )
    box = layers.normalize(raw, game, "box")
    require(
        layers.validate(box) == obs["source_chain"]["box_revision"]
        and box["result_revision"] == obs["source_chain"]["result_revision"],
        "PBP result/box revision is stale for selected facts",
    )
    if any(p.get("period", {}).get("number", 0) > 4 for p in raw.get("plays", [])):
        return None, None, "overtime_unsupported"
    try:
        normalized = layers.normalize(raw, game, "pbp")
    except (ValueError, KeyError, TypeError) as exc:
        return None, None, "pbp_not_reconciled:" + str(exc)[:256]
    revision = layers.validate(normalized)
    aliases = player_aliases(catalog)
    for binding in sem["identity_bindings"]:
        require(
            aliases.get(("espn", binding["source_player_id"]))
            == (binding["player_id"], binding["version"]),
            "PBP player mapping version differs",
        )
    candidate = {
        "game": game,
        "players": copy.deepcopy(normalized["players"]),
        "assists": copy.deepcopy(normalized["assists"]),
    }
    for player in candidate["players"]:
        binding = aliases.get(("espn", player["source_player_id"]))
        require(binding is not None, "PDF player ID unresolved")
        player["nba_player_id"] = str(binding[0])
    for edge in candidate["assists"]:
        edge["scorer_nba_id"] = str(aliases[("espn", edge["scorer_source_id"])][0])
        edge["assister_nba_id"] = str(aliases[("espn", edge["assister_source_id"])][0])
    nba = [
        r["native_id"]
        for r in catalog["game_aliases"]
        if r["game_uid"] == sem["game_uid"] and r["provider"] == "nba_api"
    ]
    require(len(nba) <= 1, "Ambiguous NBA game alias for legacy PDF adapter")
    if not nba:
        return None, revision, "nba_game_alias_unresolved"
    candidate["nba_game_id"] = nba[0]
    active = {int(p["nba_player_id"]): p for p in candidate["players"] if p["played"]}
    require(
        set(active) == {p["player_id"] for p in sem["appearances"]},
        "PBP/selected participant membership differs",
    )
    for p in sem["appearances"]:
        require(
            active[p["player_id"]]["team"] == p["team_at_game"]
            and all(
                active[p["player_id"]]["stats"][k.upper()] == p["stats"][k]
                for k in ("pts", "fgm", "fg3m", "ftm", "ast")
            ),
            "PBP supported components differ from selected observations",
        )
    return candidate, revision, None


def comparison_holds(candidate, parsed):
    """Only internally accepted, identity-checked PDFs can dispute selected facts."""
    if parsed["status"] != "reconciled" or parsed["errors"]:
        return False, []
    pdf_players = {p["player_id"]: p for p in parsed["player_validation"]}
    active = [p for p in candidate["players"] if p["played"]]
    if set(pdf_players) != {p["nba_player_id"] for p in active} or any(
        p["team"] != pdf_players[p["nba_player_id"]]["team"] for p in active
    ):
        return True, FIELDS[1:]
    try:
        source.compare_pdf(candidate, parsed)
    except ValueError:
        # The accepted membership agrees, but an atomic supported count/event
        # comparison failed. Preserve unrelated metric components.
        return False, FIELDS[1:]
    return False, []


def box_membership_differs(candidate, reader):
    fragments = parser.page_fragments(reader.pages[0])
    require(
        "FINAL BOX" in " ".join(f["text"] for f in fragments), "Missing final box score"
    )
    players, _ = parser.box_score(fragments, source.pdf_game(candidate))
    try:
        parser.resolve_identities(
            players, source.pdf_identity(candidate), source.pdf_game(candidate)
        )
    except ValueError as exc:
        if str(exc).startswith(
            ("Unresolved identity:", "Duplicate active player identity")
        ):
            return True
        raise
    pdf_members = {(p["player_id"], p["team"]) for p in players if p["played"]}
    selected_members = {
        (p["nba_player_id"], p["team"]) for p in candidate["players"] if p["played"]
    }
    return pdf_members != selected_members


def evaluate_pdf(inputs, mappings, selection, evidence_inputs):
    validate_pdf_inputs(inputs, evidence_inputs, mappings, selection)
    p = inputs["payload"]
    requested = evidence_inputs["payload"]["scope"]
    chosen = next(s for s in selection["payload"]["scopes"] if s["scope"] == requested)
    obs_by_id = {
        o["observation_id"]: o for o in evidence_inputs["payload"]["observations"]
    }
    catalog = mappings["payload"]["catalog"]
    pbps = {r["observation_id"]: r for r in p["pbp_sources"]}
    all_checks, summaries = [], []
    for oid in chosen["selected"]:
        obs = obs_by_id[oid]
        sem = obs["semantic"]
        attempts = [a for a in p["attempts"] if a["observation_id"] == oid]
        if not attempts:
            due = datetime.combine(
                date.fromisoformat(sem["scope"]["date"]) + timedelta(days=1),
                time(),
                ZoneInfo("America/New_York"),
            )
            not_due = timestamp(selection["payload"]["selection_as_of"]) < due
            attempts = [
                {
                    "observation_id": oid,
                    "acquisition": "not_yet_due" if not_due else "not_attempted",
                    "checked_at": None,
                    "url": "",
                    "pdf_base64": None,
                    "receipt": None,
                    "error": None,
                }
            ]
        checks = []
        for attempt in attempts:
            aliases = [
                r["native_id"]
                for r in catalog["game_aliases"]
                if r["game_uid"] == sem["game_uid"] and r["provider"] == "nba_api"
            ]
            bindings = {
                "game_uid": sem["game_uid"],
                "observation_id": oid,
                "nba_game_id": aliases[0] if len(aliases) == 1 else None,
                "provider_result_revision": obs.get("source_chain", {}).get(
                    "result_revision"
                ),
                "provider_box_revision": obs.get("source_chain", {}).get(
                    "box_revision"
                ),
                "provider_pbp_revision": None,
                "pdf_sha256": attempt["receipt"]["sha256"]
                if attempt["acquisition"] == "captured"
                else None,
                "mapping_version": catalog["catalog_version"],
                "parser_version": parser.VERSION + ";pypdf/" + pypdf.__version__,
                "parser_sha256": hashlib.sha256(
                    Path(parser.__file__).read_bytes()
                ).hexdigest(),
                "rule_version": "atomic_espn_pdf/1",
                "rule_sha256": hashlib.sha256(
                    Path(source.__file__).read_bytes()
                ).hexdigest(),
                "compared_fields": FIELDS,
            }
            state, reason, matches = attempt["acquisition"], attempt["error"] or "", []
            membership_hold, component_holds = False, []
            if sem["provider"] != "espn":
                state, reason = (
                    "unsupported",
                    "legacy_pdf_comparator_supports_selected_espn_only",
                )
            elif state == "legacy_audit":
                state, reason = (
                    "stale",
                    "legacy_match_requires_retained_bytes_and_new_binding",
                )
            elif state == "captured":
                if oid not in pbps:
                    state, reason = "unsupported", "accepted_pbp_capture_missing"
                else:
                    try:
                        candidate, revision, unsupported = _candidate(
                            obs, pbps[oid], catalog
                        )
                        bindings["provider_pbp_revision"] = revision
                        if unsupported:
                            state, reason = "unsupported", unsupported
                        else:
                            game = source.pdf_game(candidate)
                            day = game["date"].replace("-", "")
                            url = f"https://statsdmz.nba.com/pdfs/{day}/{day}_{game['away']}{game['home']}_book.pdf"
                            require(attempt["url"] == url, "PDF URL identity differs")
                            body = pdf_bytes(attempt)
                            reader = pypdf.PdfReader(io.BytesIO(body))
                            if reader.is_encrypted or not 0 < len(reader.pages) <= 100:
                                raise PyPdfError("Unsupported encrypted/oversized PDF")
                            inspected = inspect_pdf(body, game)
                            if not inspected.get("text_pages") or not inspected.get(
                                "pbp_marker_pages"
                            ):
                                state, reason = (
                                    "unsupported",
                                    "native_pbp_text_unavailable_no_ocr",
                                )
                            else:
                                require(
                                    inspected["identity_verified"],
                                    "PDF printed date/team identity differs",
                                )
                                membership_hold = box_membership_differs(
                                    candidate, reader
                                )
                                if membership_hold:
                                    component_holds = FIELDS[1:]
                                    require(
                                        False,
                                        "PDF/selected played membership or identity differs",
                                    )
                                with tempfile.TemporaryDirectory(
                                    prefix="nba-offline-pdf-"
                                ) as folder:
                                    path = Path(folder) / "game.pdf"
                                    identity_path = Path(folder) / "identity.json"
                                    path.write_bytes(body)
                                    identity_path.write_text(
                                        canonical(source.pdf_identity(candidate))
                                    )
                                    parsed = parser.parse_document(
                                        path,
                                        {
                                            "game": game,
                                            "sha256": bindings["pdf_sha256"],
                                            "url": url,
                                        },
                                        identity_path,
                                    )
                                membership_hold, component_holds = comparison_holds(
                                    candidate, parsed
                                )
                                matches = source.compare_pdf(candidate, parsed)
                                state, reason = (
                                    "matched",
                                    "atomic_supported_fields_and_events_match",
                                )
                    except PyPdfError as exc:
                        state, reason = (
                            "unsupported",
                            "unreadable_or_encrypted_pdf:" + str(exc)[:256],
                        )
                    except (ValueError, KeyError, TypeError, StopIteration) as exc:
                        state = "stale" if "stale" in str(exc).lower() else "mismatch"
                        reason = str(exc)[:512] or type(exc).__name__
            checks.append(
                {
                    "attempt_id": digest(attempt),
                    "observation_id": oid,
                    "acquisition": attempt["acquisition"],
                    "checked_at": attempt["checked_at"],
                    "outcome": state,
                    "reason": reason,
                    "bindings": bindings,
                    "event_matches": matches,
                    "membership_hold": membership_hold,
                    "component_holds": component_holds,
                }
            )
        checks.sort(
            key=lambda c: (
                timestamp(c["checked_at"])
                if c["checked_at"]
                else datetime.min.replace(tzinfo=timezone.utc),
                c["attempt_id"],
            )
        )
        captured = [c for c in checks if c["acquisition"] == "captured"]
        current_document = captured[-1]["bindings"]["pdf_sha256"] if captured else None
        valid = [
            c
            for c in checks
            if c["outcome"] == "matched"
            and c["bindings"]["pdf_sha256"] == current_document
        ]
        resolutions = [c for c in checks if c["outcome"] == "matched"]
        for check in checks:
            if (
                check["outcome"] == "matched"
                and check["bindings"]["pdf_sha256"] != current_document
            ):
                check["outcome"], check["reason"] = "stale", "superseded_pdf_document"
            # Negative evidence survives an unreadable replacement. Only a later
            # fully compatible match can resolve a previous atomic dispute.
            if (
                resolutions
                and check["checked_at"] is not None
                and timestamp(check["checked_at"])
                < timestamp(resolutions[-1]["checked_at"])
            ):
                check["membership_hold"], check["component_holds"] = False, []
        summaries.append(
            {
                "observation_id": oid,
                "latest_attempt_id": checks[-1]["attempt_id"],
                "latest_outcome": checks[-1]["outcome"],
                "valid_match_id": valid[-1]["attempt_id"] if valid else None,
                "supported_fields_matched": bool(valid),
            }
        )
        all_checks.extend(checks)
    return record(
        "pdf_checks",
        {
            "version": "scoped_pdf_checks/1",
            "inputs_id": inputs["record_id"],
            "selection_id": selection["record_id"],
            "scope": requested,
            "checks": all_checks,
            "games": summaries,
            "runtime_eligible": False,
        },
        [inputs["record_id"]],
    )


def pdf_capability(coverage_record, checks):
    validate(coverage_record)
    require(
        coverage_record["kind"] == "coverage", "PDF capability needs metric coverage"
    )
    require(
        len(checks) == len(coverage_record["payload"]["scopes"])
        and {canonical(c["payload"]["scope"]) for c in checks}
        == {canonical(s) for s in coverage_record["payload"]["scopes"]},
        "PDF check scopes differ from requested coverage",
    )
    for check in checks:
        validate(check)
        require(check["kind"] == "pdf_checks", "PDF check record required")
        require(
            check["payload"]["selection_id"]
            == coverage_record["payload"]["selection_id"],
            "PDF check/coverage selection pins differ",
        )
    matches = [g for c in checks for g in c["payload"]["games"]]
    require(
        len({g["observation_id"] for g in matches}) == len(matches),
        "Duplicate PDF observation contribution",
    )
    all_matched = bool(matches) and all(g["supported_fields_matched"] for g in matches)
    from .evaluation import metric_catalog

    definitions = metric_catalog()["payload"]["metrics"]
    membership_hold = any(
        c["membership_hold"] for batch in checks for c in batch["payload"]["checks"]
    )
    component_holds = {
        field
        for batch in checks
        for c in batch["payload"]["checks"]
        for field in c["component_holds"]
    }
    metrics = []
    for metric in coverage_record["payload"]["metrics"]:
        key = metric["request"]["metric"]
        reasons = []
        supported = set(definitions[key]["components"]) <= set(FIELDS)
        dependent_hold = membership_hold or bool(
            set(definitions[key]["components"]) & component_holds
        )
        if dependent_hold:
            reasons.append("applicable_pdf_discrepancy_holds_dependent_claims")
        if not supported:
            reasons.append("metric_components_outside_pdf_supported_set")
        if not all_matched:
            reasons.append("missing_applicable_supported_pdf_matches")
        observed = all_matched and supported
        full = observed and metric["level"] == "cohort_complete"
        if not full:
            reasons.append("complete_scope_corroboration_not_established")
        metrics.append(
            {
                "metric": key,
                "observed_membership_supported": observed,
                "full_scope_supported": full,
                "league_rank_eligible": metric["league_rank_eligible"]
                and not dependent_hold,
                "percentile_eligible": metric["percentile_eligible"]
                and not dependent_hold,
                "reasons": reasons,
            }
        )
    return record(
        "pdf_capability",
        {
            "version": "pdf_scope_capability/1",
            "coverage_id": coverage_record["record_id"],
            "check_ids": sorted(c["record_id"] for c in checks),
            "metrics": metrics,
            "runtime_eligible": False,
        },
        [coverage_record["record_id"], *[c["record_id"] for c in checks]],
    )
