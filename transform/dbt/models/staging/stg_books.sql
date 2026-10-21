-- Staging: books. One row per catalogue product.
--
-- Staging models do the smallest possible amount of work: rename, cast, trim. Anything
-- that involves a business rule belongs in a mart, where it can be tested in isolation.

with source as (
    select * from {{ silver_table('books') }}
),

cleaned as (
    select
        trim(title)                      as book_title,
        cast(price as double)            as price,
        upper(trim(currency))            as currency,
        case
            when lower(coalesce(availability, '')) like '%in stock%' then true
            else false
        end                              as is_in_stock,
        cast(stock_count as bigint)      as stock_count,
        cast(rating as integer)          as rating,
        nullif(trim(category), '')       as category_name,
        nullif(trim(upc), '')            as upc,
        trim(url)                        as product_url,
        _ingested_from                   as ingested_from
    from source
    where title is not null
)

select * from cleaned
