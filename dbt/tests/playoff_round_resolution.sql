with examples as (
    select 'explicit number wins' as test_case, '0042400401' as game_id,
        1 as playoff_round_number, 'Conference Semifinals' as playoff_round_name,
        1 as expected_number, 'First Round' as expected_name
    union all
    select 'explicit name wins', '0042400401', null, 'conference_semifinals',
        2, 'Conference Semifinals'
    union all
    select 'game ID supplies fallback', '0042400301', null, 'Unknown',
        3, 'Conference Finals'
    union all
    select 'noncanonical ID has no round', '0052400101', 1, 'First Round', null, null
),
resolved as (
    select
        test_case,
        expected_number,
        expected_name,
        {{ playoff_round_fields(
            'game_id', "'Playoffs'", source_columns=['playoff_round_number', 'playoff_round_name']
        ) }}
    from examples
),
game_examples as (
    select 'agreed explicit round across rows' as test_case, '0042400101' as game_id,
        1 as playoff_round_number, 'First Round' as playoff_round_name,
        '004240010' as playoff_series_id, 1 as playoff_series_game_number,
        1 as expected_number, 'First Round' as expected_name
    union all
    select 'agreed explicit round across rows', '0042400101',
        null, null, null, null,
        1, 'First Round'
    union all
    select 'conflicting explicit round across rows', '0042400102',
        1, 'First Round', '004240010', 2,
        null, null
    union all
    select 'conflicting explicit round across rows', '0042400102',
        2, 'Conference Semifinals', '004240010', 2,
        null, null
),
game_inputs as (
    select
        test_case,
        game_id,
        expected_number,
        expected_name,
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
        min(expected_number) as expected_number,
        min(expected_name) as expected_name,
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
        {{ playoff_round_resolved_columns(
            'game_id',
            "'Playoffs'",
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
union all
select test_case, expected_number, expected_name, playoff_round_number, playoff_round_name
from game_resolved
where playoff_round_number is distinct from expected_number
    or playoff_round_name is distinct from expected_name
