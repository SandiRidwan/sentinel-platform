-- Staging: quotes. Tags arrive as an array; the count is derived here because it is a
-- property of the row, not a business rule.

with source as (
    select * from {{ silver_table('quotes') }}
)

select
    trim(text)                                       as quote_text,
    trim(author)                                     as author_name,
    nullif(trim(author_url), '')                     as author_url,
    tags                                             as tags,
    case
        when tags is null then 0
        else len(tags)
    end                                              as tag_count,
    _ingested_from                                   as ingested_from
from source
where text is not null
