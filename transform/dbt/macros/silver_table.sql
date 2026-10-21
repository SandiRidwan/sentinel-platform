{% macro silver_table(table_name, silver_root=none) %}
{#
  Resolve a silver table to a queryable relation, per warehouse.

  DuckDB reads the Parquet written by the transform step directly, so `dbt run` works with
  no load step and no cloud account. BigQuery and Snowflake read a table that a loader
  creates from the same files. One model, three dialects, no `if target` inside every model.
#}
  {%- set root = silver_root or var('silver_root', '../../data/staging/zones/silver') -%}
  {%- if target.type == 'duckdb' -%}
    read_parquet('{{ root }}/{{ table_name }}/*.parquet')
  {%- elif target.type == 'bigquery' -%}
    `{{ env_var('GCP_PROJECT') }}.{{ env_var('BIGQUERY_DATASET', 'sentinel') }}.silver_{{ table_name }}`
  {%- elif target.type == 'snowflake' -%}
    {{ target.database }}.SILVER.{{ table_name | upper }}
  {%- else -%}
    {{ table_name }}
  {%- endif -%}
{% endmacro %}


{% macro get_custom_schema(schema) %}
{# Keep dbt's generated schema suffix but make it explicit and predictable. #}
  {{ schema }}
{% endmacro %}
