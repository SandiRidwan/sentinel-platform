{% snapshot dim_book_snapshot %}
{#
  Type-2 slowly changing dimension over the product dimension.

  `check_cols` on the attributes that a price or stock change would move. The point is that
  a price at a point in time is recoverable, which a plain table overwrite destroys.
#}
{{
    config(
        target_schema='gold',
        unique_key='upc',
        strategy='check',
        check_cols=['price', 'is_in_stock', 'stock_count', 'rating', 'category_name'],
        invalidate_hard_deletes=True,
    )
}}

select
    upc,
    book_title,
    price,
    currency,
    is_in_stock,
    stock_count,
    rating,
    category_name
from {{ ref('stg_books') }}
where upc is not null

{% endsnapshot %}
