"""Explicit, evidence-backed game-alias recovery in the isolated ESPN dev store."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import espn_sources as source  # noqa: E402
from scripts.gamebook_store import canonical, require  # noqa: E402
from scripts.parse_gamebooks import sha  # noqa: E402

DATASET = "nba_gamebook_dev_espn_only"
PAGE_METHOD = "reviewed_nba_visible_final_v1"


def local_bytes(root, name, limit=4 * 1024 * 1024):
    path = (root / name).resolve()
    require(
        path.is_relative_to(root.resolve()) and path.is_file(), "Invalid capture path"
    )
    require(path.stat().st_size <= limit, "Capture exceeds bound")
    return path.read_bytes()


def timestamp(value):
    require(
        datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo,
        "Missing capture timezone",
    )


def pbp_records(root, entry, season):
    body = local_bytes(root, entry["body"])
    meta = json.loads(local_bytes(root, entry["capture"]))
    expected = f"https://api.pbpstats.com/get-games/nba?Season={season}&SeasonType=Regular+Season"
    require(
        meta["url"] == expected
        and meta["http_status"] == 200
        and meta["status"] == "captured",
        "Invalid PBP Stats capture",
    )
    require(meta["sha256"] == sha(body), "PBP Stats capture hash differs")
    timestamp(meta["started_at"])
    rows = json.loads(body)["results"]
    require(
        isinstance(rows, list) and 0 < len(rows) <= 1500,
        "Invalid PBP Stats record count",
    )
    return rows, meta


def verify_page(game, meta, body):
    """Reviewed visible NBA evidence; no claim of HTTP or PDF certification."""
    require(
        meta["method"] == "operator_reviewed_cua_dom", "Unknown page observation method"
    )
    timestamp(meta["observed_at"])
    require(
        meta["sha256"] == sha(body) and len(body) <= 1024 * 1024,
        "NBA page hash or size differs",
    )
    prefix = (
        f"https://www.nba.com/game/{game['away'].lower()}-vs-{game['home'].lower()}-"
    )
    require(meta["url"].startswith(prefix), "NBA page matchup differs")
    gid = meta["url"][len(prefix) :]
    require(
        re.fullmatch(r"002" + game["season"][2:4] + r"\d{5}", gid),
        "NBA page season/phase differs",
    )
    day = date.fromisoformat(game["date"])
    title = f"{source.TEAM_NAMES[game['away']]} vs {source.TEAM_NAMES[game['home']]} {day.strftime('%b')} {day.day}, {day.year} Game Summary | NBA.com"
    require(meta["title"] == title, "NBA page title/date differs")
    text = re.sub(r"\s+", " ", body.decode("utf-8"))
    require(
        f"{game['away']} {game['away_score']} FINAL {game['home_score']} {game['home']}"
        in text,
        "NBA game scoreboard is not the expected final",
    )
    # The score must belong to this game's labelled linescore, not another game's ticker.
    pattern = (
        r"LINESCORES TEAM Q1 Q2 Q3 Q4 FINAL "
        + re.escape(game["away"])
        + r" ((?:\d+ ){4})(\d+) "
        + re.escape(game["home"])
        + r" ((?:\d+ ){4})(\d+)(?: |$)"
    )
    matches = list(re.finditer(pattern, text))
    require(len(matches) == 1 and "FINAL" in text, "Missing/ambiguous final linescore")
    match = matches[0]
    for quarters, total, side in ((1, 2, "away"), (3, 4, "home")):
        require(
            sum(map(int, match[quarters].split()))
            == int(match[total])
            == game[side + "_score"],
            "NBA page final score differs",
        )
    record = {
        "GameId": gid,
        "Date": game["date"],
        "HomeTeamAbbreviation": game["home"],
        "AwayTeamAbbreviation": game["away"],
        "HomePoints": game["home_score"],
        "AwayPoints": game["away_score"],
        "method": PAGE_METHOD,
        "nba_page_url": meta["url"],
        "nba_page_capture_sha256": meta["sha256"],
        "observed_at": meta["observed_at"],
        "scope": "reviewed game identity and final score only; no PDF or PBP certification",
    }
    require(
        source.game_mapping(game, [record]) == gid,
        "Official mapping failed scope guard",
    )
    return record


def plan(rows, records, pages):
    require(
        0 < len(rows) <= 1500 and len({r["source_game_id"] for r in rows}) == len(rows),
        "Invalid game batch",
    )
    require(
        set(pages) <= {r["source_game_id"] for r in rows},
        "Page outside requested scope",
    )
    mappings, checks = [], []
    for row in rows:
        require(
            row["result_status"] == row["box_status"] == "accepted",
            "Current result/box not accepted",
        )
        game = {
            k: row[k]
            for k in (
                "source_game_id",
                "season",
                "home",
                "away",
                "home_score",
                "away_score",
            )
        }
        game.update(
            date=str(row["game_date"]),
            provider="espn",
            final=True,
            phase="Regular Season",
        )
        bound = {
            k: row[k] for k in ("source_game_id", "result_revision", "box_revision")
        }
        selected = None
        try:
            gid = source.game_mapping(game, records)
            if gid:
                selected = dict(
                    next(r for r in records if r["GameId"] == gid),
                    method="pbpstats_final_scope_v1",
                )
            status, detail = (
                ("matched", None)
                if gid
                else ("unresolved", "No scoped PBP Stats record")
            )
        except (ValueError, KeyError, TypeError) as exc:
            status, detail = (
                "mismatch",
                canonical(
                    {
                        "error": str(exc),
                        "accepted_espn": game,
                        "provider_records": [
                            r
                            for r in records
                            if r.get("Date") == game["date"]
                            and r.get("HomeTeamAbbreviation") == game["home"]
                            and r.get("AwayTeamAbbreviation") == game["away"]
                        ],
                    }
                ),
            )
        checks.append(dict(bound, source="pbpstats", status=status, detail=detail))
        if game["source_game_id"] in pages:
            meta, body = pages[game["source_game_id"]]
            official = verify_page(game, meta, body)
            require(
                selected is None or selected["GameId"] == official["GameId"],
                "Official/PBP Stats identity conflict",
            )
            selected = official
            checks.append(
                dict(
                    bound,
                    source="nba_page",
                    status="verified_identity",
                    detail="No PDF/PBP certification",
                )
            )
        if selected:
            require(
                row.get("nba_game_id") in (None, selected["GameId"]),
                "Existing game alias conflicts",
            )
            mappings.append(dict(bound, record=selected))
    require(
        len({r["record"]["GameId"] for r in mappings}) == len(mappings),
        "Duplicate NBA destination",
    )
    return {"mappings": mappings, "checks": checks, "scope": rows}


def publication_sql(prefix):
    require(
        re.fullmatch(r"[a-z][a-z0-9-]{4,62}\." + DATASET, prefix),
        "Wrong recovery destination",
    )
    return f"""BEGIN TRANSACTION;
UPDATE `{prefix}.gamebook_store_control` SET generation=generation+1 WHERE store_id='main';
ASSERT @@row_count=1 AS 'store_control_missing';
CREATE TEMP TABLE scoped AS SELECT JSON_VALUE(x,'$.source_game_id') source_game_id,JSON_VALUE(x,'$.result_revision') result_revision,JSON_VALUE(x,'$.box_revision') box_revision FROM UNNEST(JSON_QUERY_ARRAY(@scope)) x;
ASSERT NOT EXISTS(SELECT source_game_id FROM scoped GROUP BY 1 HAVING COUNT(*)!=1) AS 'duplicate_scope';
ASSERT NOT EXISTS(SELECT 1 FROM scoped s LEFT JOIN `{prefix}.espn_layer_status` g USING(source_game_id) WHERE g.source_game_id IS NULL OR g.result_revision IS DISTINCT FROM s.result_revision OR g.box_revision IS DISTINCT FROM s.box_revision OR g.result_status!='accepted' OR g.box_status!='accepted') AS 'stale_mapping_review';
CREATE TEMP TABLE proposed AS SELECT JSON_VALUE(x,'$.source_game_id') source_game_id,JSON_VALUE(x,'$.record.GameId') nba_game_id,JSON_QUERY(x,'$.record') record FROM UNNEST(JSON_QUERY_ARRAY(@mappings)) x;
ASSERT NOT EXISTS(SELECT source_game_id FROM proposed GROUP BY 1 HAVING COUNT(*)!=1) AS 'duplicate_espn_mapping';
ASSERT NOT EXISTS(SELECT nba_game_id FROM proposed GROUP BY 1 HAVING COUNT(*)!=1) AS 'duplicate_nba_mapping';
ASSERT NOT EXISTS(SELECT 1 FROM proposed p LEFT JOIN scoped s USING(source_game_id) WHERE s.source_game_id IS NULL) AS 'mapping_outside_scope';
ASSERT NOT EXISTS(SELECT 1 FROM proposed p JOIN `{prefix}.espn_layer_status` g USING(source_game_id) WHERE JSON_VALUE(p.record,'$.Date') IS DISTINCT FROM CAST(g.game_date AS STRING) OR JSON_VALUE(p.record,'$.HomeTeamAbbreviation') IS DISTINCT FROM g.home OR JSON_VALUE(p.record,'$.AwayTeamAbbreviation') IS DISTINCT FROM g.away OR SAFE_CAST(JSON_VALUE(p.record,'$.HomePoints') AS INT64) IS DISTINCT FROM g.home_score OR SAFE_CAST(JSON_VALUE(p.record,'$.AwayPoints') AS INT64) IS DISTINCT FROM g.away_score OR NOT REGEXP_CONTAINS(p.nba_game_id,CONCAT('^002',SUBSTR(g.season,3,2),'[0-9]{{5}}$'))) AS 'mapping_scope_conflict';
ASSERT NOT EXISTS(SELECT 1 FROM proposed p JOIN `{prefix}.espn_mappings` m ON p.source_game_id=m.source_game_id OR p.nba_game_id=m.nba_game_id WHERE p.source_game_id!=m.source_game_id OR p.nba_game_id!=m.nba_game_id) AS 'mapping_conflict';
CREATE TEMP TABLE additions AS SELECT * FROM proposed p WHERE NOT EXISTS(SELECT 1 FROM `{prefix}.espn_mappings` m WHERE m.source_game_id=p.source_game_id);
INSERT INTO `{prefix}.espn_mappings` SELECT *,@evidence FROM additions;
CREATE TEMP TABLE reviews AS SELECT TO_HEX(SHA256(CONCAT(@evidence,x))) review_id,JSON_VALUE(x,'$.source_game_id') source_game_id,JSON_VALUE(x,'$.result_revision') result_revision,JSON_VALUE(x,'$.box_revision') box_revision,JSON_VALUE(x,'$.source') source,JSON_VALUE(x,'$.status') status,JSON_VALUE(x,'$.detail') detail FROM UNNEST(JSON_QUERY_ARRAY(@checks)) x;
INSERT INTO `{prefix}.espn_game_mapping_reviews` SELECT *,@evidence,CURRENT_TIMESTAMP() FROM reviews r WHERE NOT EXISTS(SELECT 1 FROM `{prefix}.espn_game_mapping_reviews` old WHERE old.review_id=r.review_id);
COMMIT TRANSACTION;
SELECT COUNT(*) inserted FROM additions;"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--publish-dev", action="store_true")
    args = parser.parse_args()
    require(
        args.output.resolve().is_relative_to(ROOT / "reports"),
        "Output must be private reports/",
    )
    args.output.mkdir(parents=True, exist_ok=False)
    bundle = args.bundle.resolve()
    manifest = json.loads(local_bytes(bundle, "manifest.json"))
    first, last = (
        date.fromisoformat(manifest["first"]),
        date.fromisoformat(manifest["last"]),
    )
    require(0 <= (last - first).days < 31, "Recovery requires at most 31 dates")
    records, _ = pbp_records(bundle, manifest["pbpstats"], manifest["season"])
    require(len(manifest.get("nba_pages", [])) <= 20, "Too many manual page reviews")
    require(len(manifest.get("attachments", [])) <= 8, "Too many evidence attachments")
    pages = {}
    for entry in manifest.get("nba_pages", []):
        sid = source.identifier(entry["source_game_id"])
        require(sid not in pages, "Duplicate page identity")
        pages[sid] = (
            json.loads(local_bytes(bundle, entry["capture"])),
            local_bytes(bundle, entry["body"]),
        )
    declared = {
        "manifest.json",
        manifest["pbpstats"]["body"],
        manifest["pbpstats"]["capture"],
        *manifest.get("attachments", []),
    }
    for entry in manifest.get("nba_pages", []):
        declared.update((entry["body"], entry["capture"]))
    sources = args.output / "sources"
    sources.mkdir()
    total_bytes = 0
    for name in sorted(declared):
        body = local_bytes(bundle, name)
        total_bytes += len(body)
        require(total_bytes <= 32 * 1024 * 1024, "Evidence bundle exceeds bound")
        destination = sources / Path(name).relative_to(".")
        require(
            destination.resolve().is_relative_to(sources.resolve()),
            "Invalid output evidence path",
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
    from scripts.gamebook_cloud import CloudStore
    from scripts.gamebook_ingestion import QueryCheckpoint, archive_sources

    cloud = CloudStore(args.project, DATASET, args.bucket, args.output / "cloud")
    dataset = cloud.bq.get_dataset(cloud.destination, timeout=30)
    require(
        dataset.labels.get("purpose") == "gamebook-dev"
        and dataset.location.upper() == cloud.location.upper(),
        "Wrong dataset ownership/location",
    )
    if args.publish_dev:
        checkpoint = QueryCheckpoint(cloud, "game-mapping-recovery")
        checkpoint.recover()
        cloud.query_checkpoint = checkpoint
    rows = cloud.query(
        f"SELECT * FROM `{cloud.destination}.espn_layer_status` WHERE season=@season AND game_date BETWEEN @first AND @last ORDER BY game_date,source_game_id LIMIT 1501",
        [
            cloud.parameter(k, v)
            for k, v in {
                "season": manifest["season"],
                "first": str(first),
                "last": str(last),
            }.items()
        ],
    )
    # Dates stay strings throughout evidence; CloudStore's canonical serializer is JSON.
    rows = json.loads(json.dumps(rows, default=str))
    result = plan(rows, records, pages)
    existing = {row["source_game_id"] for row in rows if row.get("nba_game_id")}
    result["additions_by_method"] = {
        method: sum(
            r["source_game_id"] not in existing and r["record"]["method"] == method
            for r in result["mappings"]
        )
        for method in ("pbpstats_final_scope_v1", PAGE_METHOD)
    }
    (args.output / "plan.json").write_text(json.dumps(result, indent=2) + "\n")
    if args.publish_dev:
        evidence = archive_sources(cloud, sources)
        review_evidence = cloud.upload(
            canonical(result).encode(), "game_mapping_review", "json"
        )
        evidence = cloud.upload(
            canonical({"source_bundle": evidence, "review": review_evidence}).encode(),
            "game_mapping_receipt",
            "json",
        )
        cloud.query(
            f"CREATE TABLE IF NOT EXISTS `{cloud.destination}.espn_game_mapping_reviews` (review_id STRING,source_game_id STRING,result_revision STRING,box_revision STRING,source STRING,status STRING,detail STRING,evidence_uri STRING,finished_at TIMESTAMP)"
        )
        result["publication"] = cloud.query(
            publication_sql(cloud.destination),
            [
                cloud.parameter(k, v)
                for k, v in {
                    "scope": canonical(rows),
                    "mappings": canonical(result["mappings"]),
                    "checks": canonical(result["checks"]),
                    "evidence": evidence,
                }.items()
            ],
        )[0]
        result["evidence_uri"] = evidence
        after = cloud.query(
            f"SELECT * FROM `{cloud.destination}.espn_layer_status` WHERE season=@season AND game_date BETWEEN @first AND @last ORDER BY game_date,source_game_id LIMIT 1501",
            [
                cloud.parameter(k, v)
                for k, v in {
                    "season": manifest["season"],
                    "first": str(first),
                    "last": str(last),
                }.items()
            ],
        )
        after = json.loads(json.dumps(after, default=str))
        before_by_id = {r["source_game_id"]: r for r in rows}
        require(len(after) == len(rows), "Readback membership changed")
        preserved = (
            "result_revision",
            "box_revision",
            "pbp_revision",
            "result_status",
            "box_status",
            "pbp_status",
            "home_score",
            "away_score",
            "pdf_verification_status",
        )
        for row in after:
            require(
                all(
                    row[k] == before_by_id[row["source_game_id"]][k] for k in preserved
                ),
                "Readback fact or health changed",
            )
        desired = {
            r["source_game_id"]: r["record"]["GameId"] for r in result["mappings"]
        }
        require(
            all(
                r["nba_game_id"] == desired[r["source_game_id"]]
                and r["nba_game_mapping_status"] == "resolved"
                for r in after
                if r["source_game_id"] in desired
            ),
            "Mapping readback failed",
        )
        result["readback"] = {
            "games": len(after),
            "resolved": sum(r["nba_game_mapping_status"] == "resolved" for r in after),
            "facts_health_pdf_preserved": True,
        }
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "reviewed": len(rows),
                "verified": len(result["mappings"]),
                "discrepancies": sum(
                    r["status"] == "mismatch" for r in result["checks"]
                ),
                "publication": result.get("publication"),
                "additions_by_method": result["additions_by_method"],
                "readback": result.get("readback"),
            }
        )
    )


if __name__ == "__main__":
    main()
