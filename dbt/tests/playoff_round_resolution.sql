with examples as (
    select 'explicit number wins' as test_case, '0042400401' as game_id,
        1 as playoff_round_number, 'Conference Semifinals' as playoff_round_name,
        1 as expected_number, 'First Round' as expected_name, 'explicit' as expected_source
    union all
    select 'explicit name wins', '0042400401', null, 'conference_semifinals',
        2, 'Conference Semifinals', 'explicit'
    union all
    select 'game ID supplies fallback', '0042400301', null, 'Unknown',
        3, 'Conference Finals', 'game_id'
    union all
    select 'noncanonical ID has no round', '0052400101', 1, 'First Round', null, null, 'none'
),
resolved as (
    select
        test_case,
        expected_number,
        expected_name,
        expected_source,
        {{ playoff_round_fields(
            'game_id', "'Playoffs'", source_columns=['playoff_round_number', 'playoff_round_name']
        ) }}
    from examples
),
game_examples as (
    select 'agreed explicit round across rows' as test_case, '0042400101' as game_id,
        'Playoffs' as round_season_type,
        1 as playoff_round_number, 'First Round' as playoff_round_name,
        '004240010' as playoff_series_id, 1 as playoff_series_game_number,
        1 as expected_number, 'First Round' as expected_name, 'explicit' as expected_source
    union all
    select 'agreed explicit round across rows', '0042400101', 'Playoffs',
        null, null, null, null,
        1, 'First Round', 'explicit'
    union all
    select 'conflicting explicit round across rows', '0042400102', 'Playoffs',
        1, 'First Round', '004240010', 2,
        null, null, 'conflict'
    union all
    select 'conflicting explicit round across rows', '0042400102', 'Playoffs',
        2, 'Conference Semifinals', '004240010', 2,
        null, null, 'conflict'
    union all
    select 'explicit regular-season phase conflicts with a playoff ID', '0042400401', 'Regular Season',
        1, 'First Round', '004240010', 1,
        null, null, 'conflict'
    union all
    select 'non-playoff ID never supplies explicit round evidence', '0022400401', 'Regular Season',
        1, 'First Round', '004240010', 1,
        null, null, 'none'
),
game_inputs as (
    select
        test_case,
        game_id,
        round_season_type,
        expected_number,
        expected_name,
        expected_source,
        {{ playoff_round_explicit_inputs([
            'playoff_round_number',
            'playoff_round_name',
            'playoff_series_id',
            'playoff_series_game_number'
        ]) }}
    from game_examples
),
game_aggregated as (
    select
        test_case,
        game_id,
        min(round_season_type) as round_season_type,
        min(expected_number) as expected_number,
        min(expected_name) as expected_name,
        min(expected_source) as expected_source,
        case when count(distinct explicit_playoff_round_number) = 1 then min(explicit_playoff_round_number) end as agreed_round,
        case when count(distinct explicit_playoff_series_id) = 1 then min(explicit_playoff_series_id) end as agreed_series_id,
        case when count(distinct explicit_playoff_series_game_number) = 1 then min(explicit_playoff_series_game_number) end as agreed_series_game_number,
        (
            max(case when explicit_playoff_round_row_conflict then 1 else 0 end) = 1
            or count(distinct explicit_playoff_round_number) > 1
            or count(distinct explicit_playoff_series_id) > 1
            or count(distinct explicit_playoff_series_game_number) > 1
        ) as has_conflict
    from game_inputs
    group by 1, 2
),
game_resolved as (
    select
        test_case,
        expected_number,
        expected_name,
        expected_source,
        {{ playoff_round_resolved_columns(
            'game_id',
            'round_season_type',
            'agreed_round',
            'agreed_series_id',
            'agreed_series_game_number',
            'has_conflict'
        ) }}
    from game_aggregated
)
select test_case, expected_number, expected_name, playoff_round_number, playoff_round_name
from resolved
where playoff_round_number is distinct from expected_number
    or playoff_round_name is distinct from expected_name
    or playoff_round_source is distinct from expected_source
union all
select test_case, expected_number, expected_name, playoff_round_number, playoff_round_name
from game_resolved
where playoff_round_number is distinct from expected_number
    or playoff_round_name is distinct from expected_name
    or playoff_round_source is distinct from expected_source
