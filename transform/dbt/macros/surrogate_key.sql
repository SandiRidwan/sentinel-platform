{% macro surrogate_key(fields) %}
{#
  Deterministic surrogate key from one or more business columns.

  Written here rather than pulled from dbt_utils so the project has no package
  dependency to fetch — `dbt build` works on a machine with no network, which matters for
  a repository someone clones to read.

  nulls are coalesced to a fixed sentinel before hashing, so a null in a key column
  produces a stable key instead of a null key that breaks joins differently per dialect.
#}
  {%- set dialect_hash = {'duckdb': 'md5', 'bigquery': 'to_hex(md5', 'snowflake': 'md5'} -%}
  {%- set parts = [] -%}
  {%- for field in fields -%}
    {%- do parts.append("coalesce(cast(" ~ field ~ " as varchar), '~NULL~')") -%}
  {%- endfor -%}
  {%- set expression = parts | join(" || '|' || ") -%}
  {%- if target.type == 'bigquery' -%}
    to_hex(md5({{ expression }}))
  {%- elif target.type == 'snowflake' -%}
    md5({{ expression }})
  {%- else -%}
    md5({{ expression }})
  {%- endif -%}
{% endmacro %}
