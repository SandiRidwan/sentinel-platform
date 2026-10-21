{{ config(enabled=var('bench', false), materialized='table', schema='gold', tags=['bench']) }}
{#-
  Bench-only model. Disabled unless `--vars '{bench: true}'` is passed, so a normal
  `dbt build` never pays for ten million rows.

  It is a real model, not a synthetic timing harness: it reads the same silver schema
  through the same macro as the production marts, and applies the same kind of
  transformation (dedupe + derived column). What it measures is therefore the cost of a
  model on this warehouse, not the cost of a loop.
-#}

with source as (
    select * from {{ silver_table('books_bench') }}
),

deduped as (
    select
        *,
        row_number() over (partition by upc order by _ingested_from desc) as rn
    from source
    where upc is not null
)

select
    {{ surrogate_key(['upc']) }} as book_key,
    upc,
    title as book_title,
    price,
    currency,
    availability = 'In stock' as is_in_stock,
    stock_count,
    rating,
    category as category_name,
    url as product_url,
    price * stock_count as inventory_value
from deduped
where rn = 1
