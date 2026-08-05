FROM python:3.12.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --gid 10001 appgroup \
    && useradd --uid 10001 --gid appgroup \
        --no-create-home --shell /usr/sbin/nologin appuser

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appgroup app/ ./app/
COPY --chown=appuser:appgroup config/ ./config/

RUN mkdir -p /app/data \
    && chown appuser:appgroup /app/data

USER appuser

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
