{% macro pbmc_release_id() %}
    {% set release = env_var('PBMC_RELEASE_ID') %}
    {% if not modules.re.fullmatch('[a-f0-9]{64}', release) %}
        {{ exceptions.raise_compiler_error('PBMC_RELEASE_ID must be a full lowercase SHA-256 release ID') }}
    {% endif %}
    {{ return(release) }}
{% endmacro %}
