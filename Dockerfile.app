FROM europe-west1-docker.pkg.dev/pbmc-okf-portfolio/pbmc-containers/pbmc-knowledge@sha256:078bb88fff58ab7147b0188e1b1be15ed10c85dceefab1adb738058ece510f95
USER root
COPY requirements-app.txt requirements-knowledge.txt requirements-warehouse.txt requirements.txt /app/
RUN pip install --no-cache-dir -r requirements-app.txt
COPY --chown=app:app src/ /app/src/
COPY --chown=app:app schemas/ /app/schemas/
COPY --chown=app:app warehouse/dbt/ /app/warehouse/dbt/
COPY --chown=app:app examples/app-datasets.json /app/examples/app-datasets.json
ENV PYTHONPATH=/app/src SCRNA_MODE=cloud
USER app
EXPOSE 8080
ENTRYPOINT ["python", "-m", "pbmc_platform.app.web"]
