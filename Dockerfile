FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 10001 app \
    && mkdir -p data/raw data/processed artifacts \
    && chown -R app:app /app

COPY --chown=app:app src/ ./src/
COPY --chown=app:app configs/ ./configs/

USER app

ENTRYPOINT ["python", "-m", "pbmc_platform.run_qc"]