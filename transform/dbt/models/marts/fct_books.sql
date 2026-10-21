-- Book fact: one row per product, with the measures analytics actually aggregates.
--
-- Kept separate from dim_book so a consumer can sum value without accidentally summing a
-- descriptive attribute, and so the dimension can grow columns without changing the fact.

with books as (
    select * from {{ ref('dim_book') }}
),

categories as (
    select * from {{ ref('dim_category') }}
)

select
    b.book_key,
    c.category_key,
    b.upc,
    b.currency,
    b.price,
    b.stock_count,
    b.rating,
    b.is_in_stock,
    -- Inventory value is a derived measure, computed once here rather than in every
    -- downstream query where the definition would drift.
    case
        when b.is_in_stock and b.stock_count is not null and b.price is not null
            then b.stock_count * b.price
        else 0
    end                                          as inventory_value,
    case when b.rating is not null then 1 else 0 end as is_rated
from books b
left join categories c
    on b.category_key = c.category_key
