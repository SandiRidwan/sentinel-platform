{% test not_negative(model, column_name) %}
{#
  A generic test: the column must be null or >= 0.

  Written as a generic test rather than a one-off because "this number can never be
  negative" is the single most common business rule in a warehouse, and it should be
  applied by naming it, not by rewriting the same SQL at each call site.

  Fails only on rows that are actually negative; nulls are permitted and reported by the
  not_null test instead, so the two failures mean different things.
#}
select *
from {{ model }}
where {{ column_name }} is not null
  and {{ column_name }} < 0
{% endtest %}
