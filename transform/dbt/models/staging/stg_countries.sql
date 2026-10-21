-- Staging: countries. Country names are kept exactly as the source states them; any
-- normalisation is a mart concern, so the raw shape stays auditable here.

with source as (
    select * from {{ silver_table('countries') }}
)

select
    trim(name)                                     as country_name,
    nullif(trim(capital), '')                      as capital_name,
    cast(population as bigint)                     as population,
    cast(area_km2 as double)                       as area_km2,
    case
        when cast(area_km2 as double) > 0
            then cast(population as double) / cast(area_km2 as double)
        else null
    end                                            as population_density,
    _ingested_from                                 as ingested_from
from source
where name is not null
