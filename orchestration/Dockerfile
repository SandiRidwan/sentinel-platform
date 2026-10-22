# SENTINEL Airflow — LocalExecutor image carrying the project's own code.
#
# The platform code is copied in rather than mounted, so a DAG that imports
# tools.common fails at build time instead of at 03:00 in production.
FROM apache/airflow:2.10.5-python3.11

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

USER airflow

# Airflow itself, then the project's runtime dependencies (no Airflow duplicate).
COPY orchestration/requirements.txt /requirements-airflow.txt
RUN pip install --no-cache-dir -r /requirements-airflow.txt

# The platform. dags/, tools/, governance/ and transform/ must all be importable.
COPY --chown=airflow:root tools/ /opt/sentinel/tools/
COPY --chown=airflow:root governance/ /opt/sentinel/governance/
COPY --chown=airflow:root ingestion/ /opt/sentinel/ingestion/
COPY --chown=airflow:root transform/ /opt/sentinel/transform/
COPY --chown=airflow:root quality/ /opt/sentinel/quality/
# The shared pipeline module is imported by the DAG, so it must be inside the image.
COPY --chown=airflow:root orchestration/shared/ /opt/sentinel/orchestration/shared/

ENV PYTHONPATH=/opt/sentinel \
    SENTINEL_ROOT=/opt/sentinel \
    AIRFLOW__CORE__DAGS_FOLDER=/opt/airflow/dags \
    AIRFLOW__CORE__LOAD_EXAMPLES=false \
    AIRFLOW__CORE__DEFAULT_TIMEZONE=UTC \
    SENTINEL_LOG_JSON=1
