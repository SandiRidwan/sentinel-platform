-- Staging: hockey teams. One row per team per season.

with source as (
    select * from {{ silver_table('hockey_teams') }}
)

select
    cast(year as integer)                              as season_year,
    trim(team)                                         as team_name,
    cast(wins as integer)                              as wins,
    cast(losses as integer)                            as losses,
    cast(ot_losses as integer)                         as ot_losses,
    cast(win_pct as double)                            as win_pct,
    cast(goals_for as integer)                         as goals_for,
    cast(goals_against as integer)                     as goals_against,
    cast(goals_for as integer) - cast(goals_against as integer) as goal_difference,
    _ingested_from                                     as ingested_from
from source
where team is not null
