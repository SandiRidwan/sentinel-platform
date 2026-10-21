-- Country dimension with density, and a band so grouping does not depend on raw numbers.

with countries as (
    select * from {{ ref('stg_countries') }}
),

final as (
    select
        {{ surrogate_key(['country_name']) }} as country_key,
        country_name,
        capital_name,
        population,
        area_km2,
        population_density,
        case
            when population_density is null then 'unknown'
            when population_density >= 1000 then 'very_high'
            when population_density >= 100  then 'high'
            when population_density >= 10   then 'medium'
            else 'low'
        end as density_band
    from countries
)

select * from final
