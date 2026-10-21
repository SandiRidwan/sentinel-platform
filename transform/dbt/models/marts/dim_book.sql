-- Book dimension, joined to its category.
--
-- The category join is a left join on purpose: a product whose category was renamed or
-- missing must still appear in the fact table. Silently dropping it would make revenue
-- look lower than it is.

with books as (
    select * from {{ ref('stg_books') }}
),

categories as (
    select * from {{ ref('dim_category') }}
),

ranked as (
    select
        *,
        row_number() over (partition by upc order by ingested_from desc) as rn
    from books
    where upc is not null
)

select
    {{ surrogate_key(['r.upc']) }} as book_key,
    r.upc,
    r.book_title,
    r.price,
    r.currency,
    r.is_in_stock,
    r.stock_count,
    r.rating,
    r.category_name,
    c.category_key,
    r.product_url
from ranked r
left join categories c
    on lower(trim(r.category_name)) = c.category_slug
where r.rn = 1
