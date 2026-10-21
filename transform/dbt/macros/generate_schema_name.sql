{% macro generate_schema_name(custom_schema_name, node) -%}
{#
  Use the custom schema as-is instead of dbt's default "<target_schema>_<custom_schema>".

  Default behaviour produced "gold_silver" and "gold_gold" — the target schema is
  concatenated with the custom one. That is surprising in a warehouse where "silver" and
  "gold" are the layer names people already say out loud.
#}
  {%- if custom_schema_name is none -%}
    {{ target.schema }}
  {%- else -%}
    {{ custom_schema_name | trim }}
  {%- endif -%}
{%- endmacro %}
