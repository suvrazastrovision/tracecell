# syntax=docker/dockerfile:1
# Build one component with --target; plain docker build retains the original QC default.
# Override WORKER_IMAGE with the pinned warehouse image for warehouse/dbt drills.
ARG WORKER_IMAGE=europe-west1-docker.pkg.dev/pbmc-okf-portfolio/pbmc-containers/pbmc-qc@sha256:f5eea9f5f0e0ac564c0ecedcb9e562288eec5507690828570fc065264302ae93

FROM python:3.11-slim-bookworm AS python-runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src
WORKDIR /app
RUN useradd --create-home --uid 10001 app \
    && mkdir -p data/raw data/processed artifacts \
    && chown -R app:app /app
RUN python -m pip install --no-cache-dir --upgrade "pip>=25.1,<27"

FROM python-runtime AS warehouse
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install --group warehouse
COPY --chown=app:app src/ ./src/
COPY --chown=app:app configs/ ./configs/
COPY --chown=app:app warehouse/dbt/ ./warehouse/dbt/
USER app
ENTRYPOINT ["python", "-m", "pbmc_platform.run_cloud_warehouse"]

# Keep the verified QC producer; replace only its cloud handoff wrappers.
FROM europe-west1-docker.pkg.dev/pbmc-okf-portfolio/pbmc-containers/pbmc-qc@sha256:f5eea9f5f0e0ac564c0ecedcb9e562288eec5507690828570fc065264302ae93 AS qc-workflow
COPY --chown=app:app src/pbmc_platform/run_cloud_qc.py src/pbmc_platform/cloud_release.py /app/src/pbmc_platform/

# Retain the independently verified warehouse runtime, including jsonschema.
FROM europe-west1-docker.pkg.dev/pbmc-okf-portfolio/pbmc-containers/pbmc-warehouse@sha256:ccb29689345a600fa92123eb91363eb023c8bb4d41d474dd0417b8dd0ddc1398 AS knowledge
COPY --chown=app:app src/ /app/src/
COPY --chown=app:app schemas/ /app/schemas/
ENTRYPOINT ["python", "-m", "pbmc_platform.run_cloud_knowledge"]

# Share the same pinned runtime across the app, archived assistant and recovery drills.
FROM europe-west1-docker.pkg.dev/pbmc-okf-portfolio/pbmc-containers/pbmc-knowledge@sha256:078bb88fff58ab7147b0188e1b1be15ed10c85dceefab1adb738058ece510f95 AS knowledge-runtime

FROM knowledge-runtime AS assistant
COPY --chown=app:app src/ /app/src/
COPY --chown=app:app schemas/ /app/schemas/
COPY --chown=app:app configs/assistant-package.json /app/configs/assistant-package.json
COPY --chown=app:app examples/verified-pbmc/ /app/examples/verified-pbmc/
EXPOSE 8080
ENTRYPOINT ["python", "-m", "pbmc_platform.assistant_web", "--cloud-run"]

FROM knowledge-runtime AS app
USER root
COPY pyproject.toml /app/
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade "pip>=25.1,<27" \
    && python -m pip install --group app
COPY --chown=app:app src/ /app/src/
COPY --chown=app:app schemas/ /app/schemas/
COPY --chown=app:app warehouse/dbt/ /app/warehouse/dbt/
COPY --chown=app:app examples/app-datasets.json /app/examples/app-datasets.json
ENV PYTHONPATH=/app/src SCRNA_MODE=cloud
USER app
EXPOSE 8080
ENTRYPOINT ["python", "-m", "pbmc_platform.app.web"]

FROM ${WORKER_IMAGE} AS drills
COPY --chown=app:app ops/drill_worker.py /app/ops/drill_worker.py

FROM knowledge-runtime AS knowledge-drills
COPY --chown=app:app ops/knowledge_drill_worker.py /app/ops/knowledge_drill_worker.py
ENTRYPOINT ["python", "/app/ops/knowledge_drill_worker.py"]

# Keep this last: builds without --target must still produce the original QC worker.
FROM python-runtime AS qc
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install --group qc
COPY --chown=app:app src/ ./src/
COPY --chown=app:app configs/ ./configs/
USER app
ENTRYPOINT ["python", "-m", "pbmc_platform.run_qc"]
