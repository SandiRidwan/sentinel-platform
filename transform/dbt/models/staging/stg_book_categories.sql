-- Staging: catalogue categories. The dimension the product fact joins to.

with source as (
    select * from {{ silver_table('book_categories') }}
)

select
    trim(name)        as category_name,
    lower(trim(slug)) as category_slug,
    trim(url)         as category_url,
    _ingested_from    as ingested_from
from source
where name is not null
