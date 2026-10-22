-- Local reference schema, version 1. SQLite; not BigQuery deployment DDL.
-- All timestamps are UTC ISO strings; IDs and hashes are strings.
PRAGMA user_version = 1;

CREATE TABLE IF NOT EXISTS gamebook_documents (
    document_sha256 TEXT PRIMARY KEY CHECK(length(document_sha256) = 64),
    artifact_path TEXT NOT NULL UNIQUE,
    byte_count INTEGER NOT NULL CHECK(byte_count > 0)
);

-- Immutable successful interpretations, distinct from repeatable processing attempts.
CREATE TABLE IF NOT EXISTS gamebook_revisions (
    game_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    document_sha256 TEXT NOT NULL REFERENCES gamebook_documents,
    payload_sha256 TEXT NOT NULL,
    payload_path TEXT NOT NULL,
    identity_sha256 TEXT NOT NULL,
    identity_path TEXT NOT NULL,
    source_url TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    parser_sha256 TEXT NOT NULL,
    pypdf_version TEXT NOT NULL,
    extraction_schema_version TEXT NOT NULL,
    storage_schema_version INTEGER NOT NULL CHECK(storage_schema_version = 1),
    event_count INTEGER NOT NULL CHECK(event_count >= 0),
    relationship_count INTEGER NOT NULL CHECK(relationship_count >= 0),
    raw_row_count INTEGER NOT NULL CHECK(raw_row_count > 0),
    validation_scope TEXT NOT NULL CHECK(validation_scope = 'same_document_reconciled'),
    independent_review_status TEXT NOT NULL CHECK(independent_review_status = 'not_run'),
    PRIMARY KEY(game_id, revision)
);

CREATE TABLE IF NOT EXISTS gamebook_extraction_runs (
    attempt_id TEXT PRIMARY KEY,
    game_id TEXT NOT NULL,
    candidate_revision TEXT,
    expected_previous_revision TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('published', 'noop', 'rejected', 'conflict', 'failed')),
    input_path TEXT NOT NULL,
    errors_json TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gamebook_events (
    game_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    event_id TEXT NOT NULL,
    game_date TEXT NOT NULL,
    season TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase = 'Regular Season'),
    team_abbreviation TEXT NOT NULL,
    period INTEGER NOT NULL CHECK(period BETWEEN 1 AND 4),
    clock TEXT NOT NULL,
    clock_seconds_remaining REAL NOT NULL CHECK(clock_seconds_remaining BETWEEN 0 AND 720),
    kind TEXT NOT NULL CHECK(kind IN ('field_goal', 'free_throw')),
    scorer_id TEXT NOT NULL,
    scorer_name TEXT NOT NULL,
    points INTEGER NOT NULL,
    page INTEGER NOT NULL CHECK(page > 0),
    source_row INTEGER NOT NULL CHECK(source_row >= 0),
    bbox_json TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    PRIMARY KEY(game_id, revision, event_id),
    UNIQUE(game_id, revision, page, source_row, team_abbreviation),
    FOREIGN KEY(game_id, revision) REFERENCES gamebook_revisions,
    CHECK((kind = 'field_goal' AND points IN (2, 3)) OR (kind = 'free_throw' AND points = 1))
);

CREATE TABLE IF NOT EXISTS event_relationships (
    game_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    event_id TEXT NOT NULL,
    relationship_type TEXT NOT NULL CHECK(relationship_type = 'assist'),
    from_player_id TEXT NOT NULL,
    from_player_name TEXT NOT NULL,
    to_player_id TEXT NOT NULL,
    PRIMARY KEY(game_id, revision, event_id, relationship_type),
    FOREIGN KEY(game_id, revision, event_id) REFERENCES gamebook_events,
    CHECK(from_player_id <> to_player_id)
);

CREATE TABLE IF NOT EXISTS gamebook_coverage (
    game_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source = 'nba_gamebook_pdf'),
    domain TEXT NOT NULL CHECK(domain = 'assists'),
    game_date TEXT NOT NULL,
    season TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase = 'Regular Season'),
    away TEXT NOT NULL,
    home TEXT NOT NULL,
    current_revision TEXT,
    published_at TEXT,
    last_attempt_id TEXT REFERENCES gamebook_extraction_runs,
    last_failure_attempt_id TEXT REFERENCES gamebook_extraction_runs,
    health TEXT NOT NULL CHECK(health IN ('pending', 'healthy', 'failed')),
    PRIMARY KEY(game_id, source, domain),
    FOREIGN KEY(game_id, current_revision) REFERENCES gamebook_revisions
);

CREATE TABLE IF NOT EXISTS gamebook_publication_history (
    publication_id TEXT PRIMARY KEY,
    game_id TEXT NOT NULL,
    previous_revision TEXT,
    new_revision TEXT NOT NULL,
    published_at TEXT NOT NULL,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES gamebook_extraction_runs,
    reason TEXT NOT NULL,
    FOREIGN KEY(game_id, previous_revision) REFERENCES gamebook_revisions,
    FOREIGN KEY(game_id, new_revision) REFERENCES gamebook_revisions
);

CREATE VIEW IF NOT EXISTS current_gamebook_events AS
SELECT e.* FROM gamebook_events e
JOIN gamebook_coverage c ON c.game_id = e.game_id AND c.current_revision = e.revision;

CREATE VIEW IF NOT EXISTS current_event_relationships AS
SELECT r.* FROM event_relationships r
JOIN gamebook_coverage c ON c.game_id = r.game_id AND c.current_revision = r.revision;

-- Historical pair rows retain the revision; the current view selects exactly one.
CREATE VIEW IF NOT EXISTS assist_pair_game AS
SELECT e.game_id, e.revision, e.game_date, e.season, e.phase, e.team_abbreviation,
       r.from_player_id, r.from_player_name, r.to_player_id, e.scorer_name AS to_player_name,
       count(*) AS assists, sum(e.points) AS assisted_field_goal_points
FROM gamebook_events e JOIN event_relationships r
USING(game_id, revision, event_id)
WHERE r.relationship_type = 'assist'
GROUP BY e.game_id, e.revision, e.game_date, e.season, e.phase, e.team_abbreviation,
         r.from_player_id, r.from_player_name, r.to_player_id, e.scorer_name;

CREATE VIEW IF NOT EXISTS current_assist_pair_game AS
SELECT p.* FROM assist_pair_game p
JOIN gamebook_coverage c ON c.game_id = p.game_id AND c.current_revision = p.revision;
