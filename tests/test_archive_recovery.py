"""Explicit archive recovery preserves existing copies and the original snapshot."""

import time
from types import SimpleNamespace

import pytest
from scripts import archive_season_copy as archive


def test_time_travel_fingerprint_alias_precedes_system_time(monkeypatch):
    queries = []
    monkeypatch.setattr(
        archive, "rows", lambda _, sql: queries.append(sql) or [{"n": 1, "h": 42}]
    )
    archive.fingerprint(None, "example-project.nba_gold.dim_game@1234")
    assert (
        "FROM `example-project.nba_gold.dim_game` AS t FOR SYSTEM_TIME AS OF TIMESTAMP_MILLIS(1234)"
        in queries[0]
    )
    assert not queries[0].endswith(" t")


class Client:
    def __init__(self, extra=None):
        self.events = []
        self.extra = extra

    def get_dataset(self, name):
        return SimpleNamespace(location="US")

    def list_tables(self, name):
        if name.endswith("_2025_26"):
            names = ["raw_game_logs"] if "nba_bronze_" in name else []
            if self.extra and "nba_bronze_" in name:
                names.append(self.extra)
        else:
            names = {
                "nba_bronze": ["raw_game_logs"],
                "nba_gold": ["dim_game", "fct_player_game_stats"],
            }.get(name.split(".")[-1], [])
        return [
            SimpleNamespace(reference=f"{name}.{n}", table_id=n, table_type="TABLE")
            for n in names
        ]

    def get_table(self, name):
        return SimpleNamespace(
            table_id=name.split(".")[-1], table_type="TABLE", etag="stable"
        )

    def create_dataset(self, ds):
        pytest.fail("Recovery recreated existing dataset")

    def copy_table(self, src, dest, **kw):
        self.events.append(("copy", dest))
        assert kw["job_config"].write_disposition == "WRITE_EMPTY"
        return SimpleNamespace(result=lambda **_: None)

    def load_table_from_json(self, data, dest, **kw):
        self.events.append(("manifest", dest))
        return SimpleNamespace(result=lambda **_: None)


def prepare(monkeypatch, fingerprint=None):
    monkeypatch.setattr(
        archive, "fingerprint", fingerprint or (lambda *_: {"n": 10, "h": 42})
    )
    monkeypatch.setattr(
        archive,
        "rows",
        lambda _, sql: [] if "winning_team_abbr" in sql else [{"d": "2026-06-13"}],
    )
    monkeypatch.setattr(archive, "validate_games", lambda *_: None)


def resume(client):
    return archive.copy_archive(
        client,
        "example-project",
        "2025-26",
        publish=True,
        writers_stopped=True,
        resume_snapshot_ms=int(time.time() * 1000) - 10000,
    )


def test_explicit_recovery_validates_and_reuses_existing_table(monkeypatch):
    prepare(monkeypatch)
    client = Client()
    result = resume(client)
    assert result["validated_assets"] == ["stats"]
    assert len(result["checks"]) == 3
    assert all(
        "raw_game_logs" not in dest
        for action, dest in client.events
        if action == "copy"
    )
    assert len([a for a, _ in client.events if a == "copy"]) == 2
    assert client.events[-1][0] == "manifest"


@pytest.mark.parametrize("extra", ["unexpected_table", "historical_backfill_manifest"])
def test_recovery_refuses_unexpected_objects_or_published_archive(monkeypatch, extra):
    prepare(monkeypatch)
    client = Client(extra)
    with pytest.raises(ValueError):
        resume(client)
    assert client.events == []


def test_recovery_rejects_existing_copy_mismatch_without_overwrite(monkeypatch):
    prepare(
        monkeypatch, lambda _, table: {"n": 9 if "_2025_26." in table else 10, "h": 42}
    )
    client = Client()
    with pytest.raises(ValueError, match="parity"):
        resume(client)
    assert client.events == []


def test_recovery_rejects_source_changes_since_original_snapshot(monkeypatch):
    prepare(monkeypatch, lambda _, table: {"n": 10 if "@" in table else 11, "h": 42})
    client = Client()
    with pytest.raises(ValueError, match="Source changed"):
        resume(client)
    assert client.events == []


@pytest.mark.parametrize(
    "options",
    [
        dict(publish=False, writers_stopped=True),
        dict(publish=True, writers_stopped=False),
    ],
)
def test_recovery_requires_explicit_publication_and_stopped_writers(options):
    with pytest.raises(ValueError, match="Explicit recovery"):
        archive.copy_archive(
            Client(),
            "example-project",
            "2025-26",
            resume_snapshot_ms=int(time.time() * 1000) - 10000,
            **options,
        )
