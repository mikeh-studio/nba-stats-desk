"""Fictional-only regression drafting; no provider or warehouse access."""

import hashlib
import json
import stat
from pathlib import Path

import pytest
from jsonschema import ValidationError
from scripts.ask_eval import draft as module
from scripts.ask_eval.fixtures import load_inputs

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    source, cases = load_inputs(ROOT)
    cases["cases"] = cases["cases"][:1]
    from app.agent.observability import AgentTrace
    from app.agent.private_traces import PrivateCapture
    from tests.test_api import _test_settings

    captured = []
    monkeypatch.setattr(
        "app.agent.private_traces.write_record",
        lambda settings, record: captured.append(dict(record)),
    )
    request = AgentTrace(
        request_id="00000000000040008000000000000001",
        question="Avery Example points per game in 2024-25",
        model="controlled",
        outcome="answered",
    )
    capture = PrivateCapture(
        _test_settings(),
        request,
        provider="openai",
        transport="json",
        static_version="fictional-test",
    )
    capture.answer({"semantic_evidence": {"rows": [{"player_id": 999999}]}})
    capture.finish(request)
    trace = captured[0]
    paths = {}
    for key, value in {"trace": trace, "source": source, "cases": cases}.items():
        paths[key] = tmp_path / (key + ".json")
        paths[key].write_text(json.dumps(value))
    review = {
        "schema": "ask_failure_review/1",
        "status": "confirmed_failure",
        "reviewer_kind": "agent",
        "reviewer": "fixture agent",
        "failure_explanation": "Fictional aggregate contradicts independently summed rows.",
        "expectations_independent_of_observed_answer": True,
        "mode": "private_reproduction",
    }
    for key in paths:
        review[key + "_sha256"] = hashlib.sha256(paths[key].read_bytes()).hexdigest()
    paths["review"] = tmp_path / "review.json"
    paths["review"].write_text(json.dumps(review))
    # Use a real ignored repository location; each test owns its fresh directory.
    import uuid

    paths["output"] = ROOT / "reports" / ("draft-test-" + uuid.uuid4().hex)
    yield paths
    import shutil

    shutil.rmtree(paths["output"], ignore_errors=True)


def edit(paths, key, change, rebind=True):
    value = json.loads(paths[key].read_text())
    change(value)
    paths[key].write_text(json.dumps(value))
    if rebind and key != "review":
        edit(
            paths,
            "review",
            lambda review: review.update(
                {key + "_sha256": hashlib.sha256(paths[key].read_bytes()).hexdigest()}
            ),
        )


def test_pending_compatible_immutable_independent(inputs):
    edit(inputs, "cases", lambda value: value.update(human_reviewed=True))
    before = inputs["trace"].read_bytes()
    result = module.draft(**inputs)
    source, cases = load_inputs(ROOT, inputs["output"])
    assert cases["human_reviewed"] is False
    assert cases["cases"][0]["review"] == {"status": "pending", "reviewer": None}
    assert cases["cases"][0]["turns"][0]["checks"][5]["equals"] == 5
    assert "999999" not in (inputs["output"] / "cases.json").read_text()
    assert result["failure_review"]["reviewer_kind"] == "agent"
    assert result["publication_authorized"] is False
    assert inputs["trace"].read_bytes() == before
    assert stat.S_IMODE(inputs["output"].stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600
        for path in inputs["output"].iterdir()
    )
    with pytest.raises(FileExistsError):
        module.draft(**inputs)


@pytest.mark.parametrize(
    "key,change",
    [
        ("trace", lambda value: value.update(schema="unknown")),
        ("review", lambda value: value.update(status="suspected")),
        (
            "review",
            lambda value: value.update(
                expectations_independent_of_observed_answer=False
            ),
        ),
        ("review", lambda value: value.update(reviewer_kind="automatic_approval")),
        ("review", lambda value: value.update(source_sha256="mismatch")),
        ("cases", lambda value: value["cases"][0].update(oracle_note="")),
        ("cases", lambda value: value["cases"][0]["turns"][0].update(checks=[])),
        ("source", lambda value: value.update(rows=[])),
        ("source", lambda value: value.update(provider_recordings=[])),
        ("source", lambda value: value.update(description="api_key=" + "private")),
    ],
)
def test_invalid_rejected_before_output(inputs, key, change):
    edit(inputs, key, change)
    with pytest.raises((module.DraftError, ValidationError, ValueError)):
        module.draft(**inputs)
    assert not inputs["output"].exists()


def test_missing_source_and_link_rejected(inputs):
    original = inputs["source"]
    inputs["source"] = original.with_name("absent.json")
    with pytest.raises(module.DraftError):
        module.draft(**inputs)
    inputs["source"].symlink_to(original)
    with pytest.raises(module.DraftError, match="symlink"):
        module.draft(**inputs)


def test_reconstruction_is_explicit(inputs):
    edit(inputs, "review", lambda value: value.update(mode="fictional_reconstruction"))
    with pytest.raises(module.DraftError, match="original frozen source"):
        module.draft(**inputs)
    original_bytes = inputs["source"].read_bytes()
    inputs["original_source"] = inputs["source"].with_name("original.json")
    inputs["original_source"].write_bytes(original_bytes)
    edit(
        inputs,
        "review",
        lambda value: value.update(
            reconstruction_note="Separately authored fictional counterpart, not text replacement.",
            separately_authored_fictional_inputs=True,
            original_source_sha256=value["source_sha256"],
        ),
    )
    with pytest.raises(module.DraftError, match="unchanged"):
        module.draft(**inputs)

    def fictional_rows(value):
        for row in value["rows"]:
            if row["player_id"] == 101:
                row["player_name"] = "Morgan Fictional"

    edit(inputs, "source", fictional_rows)
    edit(
        inputs,
        "cases",
        lambda value: value["cases"][0]["turns"][0].update(
            question="Morgan Fictional points per game in 2024-25"
        ),
    )
    module.draft(**inputs)
    assert inputs["original_source"].read_bytes() == original_bytes


def test_missing_plan_is_explicit_limitation(inputs):
    edit(
        inputs,
        "cases",
        lambda value: value["cases"][0]["turns"][0].pop("planner_response"),
    )
    result = module.draft(**inputs)
    assert "controlled_planner_response_missing:points_mean" in result["deficiencies"]


def test_output_must_be_ignored_reports(inputs, tmp_path):
    inputs["output"] = tmp_path / "public"
    with pytest.raises(module.DraftError, match="reports"):
        module.draft(**inputs)


def test_size_and_nonfinite_rejected(inputs, monkeypatch):
    monkeypatch.setattr(module, "MAX_BYTES", 8)
    with pytest.raises(module.DraftError, match="size"):
        module.draft(**inputs)
    monkeypatch.setattr(module, "MAX_BYTES", 8 * 1024 * 1024)
    inputs["trace"].write_text('{"x": NaN}')
    with pytest.raises(module.DraftError, match="JSON"):
        module.draft(**inputs)


def test_source_and_case_consistency(inputs):
    edit(
        inputs,
        "cases",
        lambda value: value["cases"][0]["turns"][0]["planner_response"]["queries"][
            0
        ].update(player_name="Uncaptured Person"),
    )
    with pytest.raises(module.DraftError, match="identity"):
        module.draft(**inputs)
    assert not inputs["output"].exists()


def test_frozen_content_hashes(inputs):
    result = module.draft(**inputs)
    for name, expected in result["frozen_file_sha256"].items():
        assert (
            hashlib.sha256((inputs["output"] / name).read_bytes()).hexdigest()
            == expected
        )


def test_unsafe_parent_output(inputs, tmp_path):
    import uuid

    link = ROOT / "reports" / ("draft-link-" + uuid.uuid4().hex)
    link.symlink_to(tmp_path, target_is_directory=True)
    inputs["output"] = link / "draft"
    try:
        with pytest.raises((OSError, module.DraftError)):
            module.draft(**inputs)
        assert not (tmp_path / "draft").exists()
    finally:
        link.unlink()


@pytest.mark.parametrize("value", ["NaN", "1e999"])
def test_all_nonfinite_numbers_rejected(inputs, value):
    inputs["trace"].write_text('{"time": ' + value + "}")
    with pytest.raises(module.DraftError, match="JSON"):
        module.draft(**inputs)
    assert not inputs["output"].exists()


def test_partial_write_cleanup(inputs, monkeypatch):
    calls = 0
    original = module.os.fsync

    def fail_second(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected disk failure")
        return original(fd)

    monkeypatch.setattr(module.os, "fsync", fail_second)
    with pytest.raises(OSError, match="injected"):
        module.draft(**inputs)
    assert not inputs["output"].exists()
    monkeypatch.setattr(module.os, "fsync", original)
    module.draft(**inputs)
    prior = (inputs["output"] / "provenance.json").read_bytes()
    with pytest.raises(FileExistsError):
        module.draft(**inputs)
    assert (inputs["output"] / "provenance.json").read_bytes() == prior


@pytest.mark.parametrize(
    "name", ["research_published_study", "research_home", "round_refusal"]
)
def test_existing_case_formats(inputs, name):
    _, corpus = load_inputs(ROOT)
    case = next(case for case in corpus["cases"] if case["id"] == name)
    edit(inputs, "cases", lambda value: value.update(cases=[case]))
    result = module.draft(**inputs)
    _, document = load_inputs(ROOT, inputs["output"])
    assert document["cases"][0]["id"] == name
    if name != "round_refusal":
        assert not any(
            "controlled_planner_response_missing" in value
            for value in result["deficiencies"]
        )


def test_null_identity_expectation(inputs):
    edit(
        inputs,
        "cases",
        lambda value: value["cases"][0]["turns"][0]["checks"].append(
            {"path": "resolved_player_id", "equals": None}
        ),
    )
    module.draft(**inputs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "bad"),
        ("time", "not-time"),
        ("time", True),
        ("outcome", {}),
        ("error_type", "private error text"),
    ],
)
def test_trace_required_shape(inputs, field, value):
    edit(inputs, "trace", lambda trace: trace.update({field: value}))
    with pytest.raises(module.DraftError, match="trace"):
        module.draft(**inputs)


def test_research_plan_identity_scope(inputs):
    _, corpus = load_inputs(ROOT)
    case = next(case for case in corpus["cases"] if case["id"] == "research_home")
    edit(inputs, "cases", lambda value: value.update(cases=[case]))
    edit(
        inputs,
        "cases",
        lambda value: value["cases"][0]["turns"][0]["research_response"][
            "query"
        ].update(player_ids=[999]),
    )
    with pytest.raises(module.DraftError, match="identity"):
        module.draft(**inputs)


def test_safe_existing_0755_parent(inputs):
    import uuid

    parent = ROOT / "reports" / ("draft-parent-" + uuid.uuid4().hex)
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    inputs["output"] = parent / "draft"
    try:
        module.draft(**inputs)
        assert stat.S_IMODE(parent.stat().st_mode) == 0o755
        assert stat.S_IMODE(inputs["output"].stat().st_mode) == 0o700
    finally:
        import shutil

        shutil.rmtree(parent)
