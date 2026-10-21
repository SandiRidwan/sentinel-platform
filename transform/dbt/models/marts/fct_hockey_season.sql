-- Hockey season fact: one row per team per season, plus the rate measures a sports
-- consumer would otherwise recompute inconsistently.

with teams as (
    select * from {{ ref('stg_hockey_teams') }}
),

final as (
    select
        {{ surrogate_key(['season_year', 'team_name']) }} as season_key,
        season_year,
        team_name,
        wins,
        losses,
        ot_losses,
        win_pct,
        goals_for,
        goals_against,
        goal_difference,
        cast(wins + losses + ot_losses as bigint)          as games_played,
        case
            when (wins + losses + ot_losses) > 0
                then cast(wins as double) / cast(wins + losses + ot_losses as double)
            else null
        end                                                as win_rate_recomputed
    from teams
)

select * from final
