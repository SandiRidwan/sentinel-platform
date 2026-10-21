-- Category dimension.
--
-- A category can appear under more than one parent in the source, so the key is the slug
-- (stable) and the name is kept for display only. Renaming a category must not orphan the
-- products that reference it.

with categories as (
    select * from {{ ref('stg_book_categories') }}
),

deduplicated as (
    select
        *,
        row_number() over (partition by category_slug order by category_name) as rn
    from categories
)

select
    {{ surrogate_key(['category_slug']) }} as category_key,
    category_slug,
    category_name,
    category_url
from deduplicated
where rn = 1
