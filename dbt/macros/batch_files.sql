{#- The files the daily batch writes for every day (ADR-0020), under ATALAYERO_BATCH__DIR, read
    with `reader` and cast to `columns` (name -> type). Before the batch has written any, the
    same columns with no rows, so that the project builds before the first replay and in CI. -#}
{% macro batch_files(reader, pattern, columns) %}
    {%- set glob = env_var('ATALAYERO_BATCH__DIR', 'data/batch') ~ '/days/*/' ~ pattern -%}
    {%- set found = 0 -%}
    {%- if execute -%}
        {%- set found = run_query("select count(*) from glob('" ~ glob ~ "')").columns[0].values()[0] | int -%}
    {%- endif -%}
    {%- if found > 0 and reader == 'read_json' -%}
        select * from read_json('{{ glob }}', format = 'auto', columns = {
            {%- for name, type in columns.items() %}'{{ name }}': '{{ type }}'{% if not loop.last %}, {% endif %}{% endfor -%}
        })
    {%- elif found > 0 -%}
        select
            {%- for name, type in columns.items() %}
            {{ name }}::{{ type }} as {{ name }}{% if not loop.last %},{% endif %}
            {%- endfor %}
        from {{ reader }}('{{ glob }}')
    {%- else -%}
        select
            {%- for name, type in columns.items() %}
            null::{{ type }} as {{ name }}{% if not loop.last %},{% endif %}
            {%- endfor %}
        where false
    {%- endif -%}
{% endmacro %}
