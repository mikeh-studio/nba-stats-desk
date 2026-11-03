"""Synthetic native PDFs exercise exact source/dependency and atomic-field rules."""

import base64
import copy
import hashlib
import io

import pytest
from app.data.unified.evaluation import coverage, reconciliation
from app.data.unified.ledger import LedgerStore, record
from app.data.unified.pdf_checks import evaluate_pdf, pdf_capability
from app.data.unified.selection import policy_record, select
from app.data.unified.snapshot import evaluation_snapshot, mapping_snapshot, snapshot
from app.data.unified.store import ObservationStore
from pypdf import PdfReader, PdfWriter
from scripts import parse_gamebooks as parser
from tests.support.historical import ingest, receipt

from test_gamebook_assists import document, fragment, pdf_from_pages
from test_unified_espn import retained, seed
from test_unified_evaluation import AS_OF, authority, metric_catalog, query


def fixture(tmp_path, assist_total=1):
    raw = retained()
    path = tmp_path / "source.sqlite"
    store = ObservationStore(path)
    seed(store)
    result = ingest(store, raw)
    store.attach_game_alias(
        result["game_uid"],
        "nba_api",
        "0022600001",
        store.game_scope(result["game_uid"])[0],
        receipt(),
    )
    store.close()
    requested = {
        "season": "2026-27",
        "phase": "Regular Season",
        "first": "2026-10-22",
        "last": "2026-10-22",
    }
    inv, policy = snapshot(path), policy_record()
    chosen = select(inv, policy, [requested], AS_OF)
    inputs = evaluation_snapshot(path, inv, requested)
    mapping = mapping_snapshot(path, inv)
    pdf_path, _, _ = document(tmp_path, assist_total)
    pages = [parser.page_fragments(p) for p in PdfReader(pdf_path).pages]
    for page in pages:
        for f in page:
            f["text"] = {"Hosts": "Knicks", "Visitors": "Celtics"}.get(
                f["text"], f["text"]
            )
    pages[0].append(fragment(10, 10, "October 22, 2026 Boston Celtics New York Knicks"))
    body = pdf_from_pages(pages)
    stamp = "2026-10-23T12:00:00+00:00"
    attempt = {
        "observation_id": result["observation_id"],
        "acquisition": "captured",
        "checked_at": stamp,
        "url": "https://statsdmz.nba.com/pdfs/20261022/20261022_BOSNYK_book.pdf",
        "pdf_base64": base64.b64encode(body).decode(),
        "receipt": receipt(body, stamp),
        "error": None,
    }
    pbp = {
        "observation_id": result["observation_id"],
        "source_body": raw["source_body"],
        "receipt": raw["source"],
    }
    payload = {
        "version": "scoped_pdf_inputs/1",
        "selection_id": chosen["record_id"],
        "evaluation_inputs_id": inputs["record_id"],
        "mapping_catalog_id": mapping["record_id"],
        "attempts": [attempt],
        "pbp_sources": [pbp],
    }
    pdf_inputs = record(
        "pdf_inputs",
        payload,
        [chosen["record_id"], inputs["record_id"], mapping["record_id"]],
    )
    return inv, policy, chosen, inputs, mapping, pdf_inputs


def reinput(value, payload):
    return record("pdf_inputs", payload, value["depends_on"])


def test_native_pdf_reexecutes_and_only_supported_components_are_corroborated(tmp_path):
    inv, policy, chosen, inputs, mapping, pdf_inputs = fixture(tmp_path)
    result = evaluate_pdf(pdf_inputs, mapping, chosen, inputs)
    check = result["payload"]["checks"][0]
    assert check["outcome"] == "matched" and len(check["event_matches"]) == 1
    assert result["payload"]["games"][0]["supported_fields_matched"]
    assert check["bindings"]["provider_pbp_revision"]
    expected = authority(inputs)
    rec = reconciliation(chosen, inputs, expected)
    covered = coverage(
        [rec],
        metric_catalog(),
        [query(), query("ast"), query("dreb"), query("fg_pct", min_attempts=1)],
    )
    capability = pdf_capability(covered, [result])
    metrics = {m["metric"]: m for m in capability["payload"]["metrics"]}
    assert (
        metrics["pts"]["full_scope_supported"]
        and metrics["ast"]["full_scope_supported"]
    )
    assert not metrics["dreb"]["observed_membership_supported"]
    assert not metrics["fg_pct"]["observed_membership_supported"]
    assert not capability["payload"]["runtime_eligible"]
    ledger = LedgerStore()
    values = [
        policy,
        inv,
        chosen,
        inputs,
        expected,
        metric_catalog(),
        rec,
        covered,
        mapping,
        pdf_inputs,
        result,
        capability,
    ]
    for v in values:
        ledger.append(v)
    assert ledger.read(capability["record_id"]) == capability
    assert [ledger.append(v) for v in values] == ["noop"] * len(values)
    ledger.close()


def test_later_failure_preserves_still_valid_binding_and_exposes_attempt(tmp_path):
    _, _, chosen, inputs, mapping, value = fixture(tmp_path)
    p = copy.deepcopy(value["payload"])
    failure = {
        **p["attempts"][0],
        "acquisition": "unavailable",
        "checked_at": "2026-10-29T00:00:00+00:00",
        "pdf_base64": None,
        "receipt": receipt(b"HTTP 403", "2026-10-29T00:00:00+00:00"),
        "error": "HTTP 403",
    }
    p["attempts"].append(failure)
    result = evaluate_pdf(reinput(value, p), mapping, chosen, inputs)
    game = result["payload"]["games"][0]
    assert (
        game["latest_outcome"] == "unavailable"
        and game["supported_fields_matched"]
        and game["valid_match_id"]
    )


def test_new_mismatched_document_supersedes_old_match_without_partial_credit(tmp_path):
    _, _, chosen, inputs, mapping, value = fixture(tmp_path)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    *_, changed = fixture(other_dir, assist_total=2)
    p = copy.deepcopy(value["payload"])
    new = copy.deepcopy(changed["payload"]["attempts"][0])
    new["observation_id"] = p["attempts"][0]["observation_id"]
    new["checked_at"] = new["receipt"]["captured_at"] = "2026-10-29T00:00:00+00:00"
    p["attempts"].append(new)
    result = evaluate_pdf(reinput(value, p), mapping, chosen, inputs)
    assert result["payload"]["games"][0]["latest_outcome"] == "mismatch"
    assert not result["payload"]["games"][0]["supported_fields_matched"]
    assert {c["outcome"] for c in result["payload"]["checks"]} == {"stale", "mismatch"}


@pytest.mark.parametrize(
    "mode",
    [
        "missing_pbp",
        "stale_box",
        "changed_event",
        "legacy",
        "scan",
        "overtime",
        "wrong_header",
    ],
)
def test_stale_unsupported_or_mismatched_evidence_cannot_inherit_a_match(
    tmp_path, mode
):
    _, _, chosen, inputs, mapping, value = fixture(tmp_path)
    p = copy.deepcopy(value["payload"])
    if mode == "missing_pbp":
        p["pbp_sources"] = []
    elif mode == "legacy":
        p["attempts"][0].update(acquisition="legacy_audit", pdf_base64=None)
    elif mode == "scan":
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        out = io.BytesIO()
        writer.write(out)
        body = out.getvalue()
        p["attempts"][0]["pdf_base64"] = base64.b64encode(body).decode()
        p["attempts"][0]["receipt"]["sha256"] = hashlib.sha256(body).hexdigest()
    elif mode == "wrong_header":
        body = base64.b64decode(p["attempts"][0]["pdf_base64"])
        pages = [
            parser.page_fragments(page) for page in PdfReader(io.BytesIO(body)).pages
        ]
        for f in pages[0]:
            f["text"] = f["text"].replace("October 22", "October 23")
        body = pdf_from_pages(pages)
        p["attempts"][0]["pdf_base64"] = base64.b64encode(body).decode()
        p["attempts"][0]["receipt"]["sha256"] = hashlib.sha256(body).hexdigest()
    else:
        import json

        raw = json.loads(p["pbp_sources"][0]["source_body"])
        if mode == "stale_box":
            raw["boxscore"]["players"][0]["statistics"][0]["athletes"][0]["stats"][
                0
            ] = "25"
        elif mode == "changed_event":
            raw["plays"][0]["clock"]["displayValue"] = "10:59"
        else:
            raw["plays"][-1]["period"]["number"] = 5
        body = json.dumps(raw).encode()
        p["pbp_sources"][0]["source_body"] = body.decode()
        p["pbp_sources"][0]["receipt"]["sha256"] = hashlib.sha256(body).hexdigest()
    result = evaluate_pdf(reinput(value, p), mapping, chosen, inputs)
    assert not result["payload"]["games"][0]["supported_fields_matched"]
    assert result["payload"]["checks"][0]["outcome"] in (
        "unsupported",
        "stale",
        "mismatch",
    )


@pytest.mark.parametrize("mode", ["bytes", "clock", "catalog", "selection"])
def test_pdf_bindings_fail_closed_before_comparison(tmp_path, mode):
    _, _, chosen, inputs, mapping, value = fixture(tmp_path)
    p = copy.deepcopy(value["payload"])
    if mode == "bytes":
        p["attempts"][0]["receipt"]["sha256"] = "f" * 64
    elif mode == "clock":
        p["attempts"][0]["checked_at"] = "2026-10-31T00:00:00+00:00"
    elif mode == "catalog":
        p["mapping_catalog_id"] = "f" * 64
    else:
        p["selection_id"] = "f" * 64
    deps = [p["selection_id"], p["evaluation_inputs_id"], p["mapping_catalog_id"]]
    with pytest.raises(ValueError):
        evaluate_pdf(record("pdf_inputs", p, deps), mapping, chosen, inputs)


@pytest.mark.parametrize("encrypted", [False, True])
def test_unreadable_pdf_is_an_explicit_non_statistical_failure(tmp_path, encrypted):
    _, _, chosen, inputs, mapping, value = fixture(tmp_path)
    p = copy.deepcopy(value["payload"])
    body = b"not a PDF"
    if encrypted:
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.encrypt("secret")
        out = io.BytesIO()
        writer.write(out)
        body = out.getvalue()
    p["attempts"][0]["pdf_base64"] = base64.b64encode(body).decode()
    p["attempts"][0]["receipt"]["sha256"] = hashlib.sha256(body).hexdigest()
    checked = evaluate_pdf(reinput(value, p), mapping, chosen, inputs)["payload"][
        "checks"
    ][0]
    assert checked["outcome"] == "unsupported"
    assert not checked["membership_hold"] and not checked["component_holds"]


def test_readable_pdf_zero_point_extra_participant_holds_membership(tmp_path):
    _, _, chosen, inputs, mapping, value = fixture(tmp_path)
    p = copy.deepcopy(value["payload"])
    body = base64.b64decode(p["attempts"][0]["pdf_base64"])
    pages = [parser.page_fragments(page) for page in PdfReader(io.BytesIO(body)).pages]
    # Home players at y230,245; insert a new zero-point played row before totals.
    for f in pages[0]:
        if f["y"] >= 260:
            f["y"] += 15
    pages[0] += [
        fragment(10, 260, "4"),
        fragment(30, 260, "Extra Player"),
        fragment(130, 260, "00:00"),
    ]
    pages[0] += [fragment(x, 260, "0") for x in (200, 230, 260, 300, 340)]
    body = pdf_from_pages(pages)
    p["attempts"][0]["pdf_base64"] = base64.b64encode(body).decode()
    p["attempts"][0]["receipt"]["sha256"] = hashlib.sha256(body).hexdigest()
    checked = evaluate_pdf(reinput(value, p), mapping, chosen, inputs)["payload"][
        "checks"
    ][0]
    assert checked["outcome"] == "mismatch" and checked["membership_hold"]


def test_supported_event_discrepancy_holds_dependent_core_claims_only(tmp_path):
    from test_unified_freshness import discovery

    _, _, chosen, inputs, mapping, value = fixture(tmp_path)
    p = copy.deepcopy(value["payload"])
    import json

    raw = json.loads(p["pbp_sources"][0]["source_body"])
    raw["plays"][0]["clock"]["displayValue"] = "10:59"
    body = json.dumps(raw).encode()
    p["pbp_sources"][0]["source_body"] = body.decode()
    p["pbp_sources"][0]["receipt"]["sha256"] = hashlib.sha256(body).hexdigest()
    checked = evaluate_pdf(reinput(value, p), mapping, chosen, inputs)
    rec = reconciliation(chosen, inputs, discovery(inputs))
    covered = coverage([rec], metric_catalog(), [query(), query("ast"), query("dreb")])
    combined = pdf_capability(covered, [checked])
    metrics = {m["metric"]: m for m in combined["payload"]["metrics"]}
    assert (
        not metrics["ast"]["league_rank_eligible"]
        and not metrics["pts"]["league_rank_eligible"]
    )
    assert metrics["dreb"]["league_rank_eligible"]


def test_external_evidence_is_bounded_hashed_and_symlink_contained(
    tmp_path, monkeypatch
):
    import app.data.unified.artifacts as checks
    from scripts.check_unified_pdfs import retain_artifacts

    monkeypatch.setattr(checks, "REPORTS", tmp_path)
    _, _, chosen, inputs, mapping, value = fixture(tmp_path)
    data = copy.deepcopy({k: value["payload"][k] for k in ("attempts", "pbp_sources")})
    data = retain_artifacts(data, tmp_path / "evidence")
    assert (
        data["attempts"][0]["pdf_base64"] is None
        and data["pbp_sources"][0]["source_body"] is None
    )
    p = {**value["payload"], **data}
    assert evaluate_pdf(reinput(value, p), mapping, chosen, inputs)["payload"]["games"][
        0
    ]["supported_fields_matched"]
    path = __import__("pathlib").Path(data["attempts"][0]["pdf_path"])
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash"):
        evaluate_pdf(reinput(value, p), mapping, chosen, inputs)
    outside = tmp_path.parent / "outside-proof.pdf"
    outside.write_bytes(b"outside")
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="private content-addressed"):
        evaluate_pdf(reinput(value, p), mapping, chosen, inputs)
    with pytest.raises(ValueError, match="symlink"):
        retain_artifacts(copy.deepcopy(value["payload"]), tmp_path / "evidence")


def test_unreadable_new_document_cannot_resolve_a_real_discrepancy(tmp_path):
    _, _, chosen, inputs, mapping, value = fixture(tmp_path)
    p = copy.deepcopy(value["payload"])
    body = base64.b64decode(p["attempts"][0]["pdf_base64"])
    pages = [parser.page_fragments(page) for page in PdfReader(io.BytesIO(body)).pages]
    for f in pages[1]:
        f["text"] = f["text"].replace("11:00", "10:59")
    body = pdf_from_pages(pages)
    p["attempts"][0]["pdf_base64"] = base64.b64encode(body).decode()
    p["attempts"][0]["receipt"]["sha256"] = hashlib.sha256(body).hexdigest()
    new = copy.deepcopy(p["attempts"][0])
    new["checked_at"] = new["receipt"]["captured_at"] = "2026-10-29T00:00:00+00:00"
    new["pdf_base64"] = base64.b64encode(b"not a PDF").decode()
    new["receipt"]["sha256"] = hashlib.sha256(b"not a PDF").hexdigest()
    p["attempts"].append(new)
    result = evaluate_pdf(reinput(value, p), mapping, chosen, inputs)
    assert result["payload"]["games"][0]["latest_outcome"] == "unsupported"
    assert result["payload"]["checks"][0]["component_holds"]
    # A later fully compatible capture can adjudicate the prior discrepancy.
    good = copy.deepcopy(value["payload"]["attempts"][0])
    good["checked_at"] = good["receipt"]["captured_at"] = "2026-10-29T12:00:00+00:00"
    p["attempts"].append(good)
    result = evaluate_pdf(reinput(value, p), mapping, chosen, inputs)
    assert result["payload"]["games"][0]["supported_fields_matched"]
    assert not any(c["component_holds"] for c in result["payload"]["checks"])
    last = copy.deepcopy(new)
    last["checked_at"] = last["receipt"]["captured_at"] = "2026-10-29T18:00:00+00:00"
    p["attempts"].append(last)
    result = evaluate_pdf(reinput(value, p), mapping, chosen, inputs)
    assert not result["payload"]["games"][0]["supported_fields_matched"]
    assert not any(c["component_holds"] for c in result["payload"]["checks"])


def test_artifact_directory_symlink_cannot_redirect_a_write(tmp_path, monkeypatch):
    import app.data.unified.artifacts as checks
    from scripts.check_unified_pdfs import retain_artifacts

    reports = tmp_path / "reports"
    reports.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (reports / "evidence").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(checks, "REPORTS", reports)
    with pytest.raises(ValueError, match="inside private reports"):
        retain_artifacts({"attempts": [], "pbp_sources": []}, reports / "evidence")
    assert list(outside.iterdir()) == []
