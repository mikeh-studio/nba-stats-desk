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
)
select *
from resolved
where playoff_round_number is distinct from expected_number
    or playoff_round_name is distinct from expected_name
