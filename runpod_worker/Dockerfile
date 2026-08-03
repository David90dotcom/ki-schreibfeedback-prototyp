FROM ollama/ollama:0.32.5

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OLLAMA_HOST=127.0.0.1:11434 \
    OLLAMA_BASE_URL=http://127.0.0.1:11434 \
    OLLAMA_MODEL=ministral-3:14b-instruct-2512-q8_0 \
    OLLAMA_REQUEST_TIMEOUT_SECONDS=600 \
    MAX_PROMPT_CHARS=50000

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Das Modell wird in das Image eingebettet.
# Dadurch muss es nicht bei jedem Worker-Kaltstart neu geladen werden.
RUN set -eux; \
    ollama serve >/tmp/ollama-build.log 2>&1 & \
    ollama_pid=$!; \
    trap 'kill "$ollama_pid" 2>/dev/null || true' EXIT; \
    ready=0; \
    for attempt in $(seq 1 60); do \
        if curl -fsS \
            "${OLLAMA_BASE_URL}/api/tags" \
            >/dev/null; then \
            ready=1; \
            break; \
        fi; \
        sleep 1; \
    done; \
    if [ "$ready" -ne 1 ]; then \
        cat /tmp/ollama-build.log; \
        exit 1; \
    fi; \
    ollama pull "$OLLAMA_MODEL"; \
    ollama show "$OLLAMA_MODEL" >/dev/null

RUN python3 -m venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

COPY runpod_worker/requirements.txt \
    /app/runpod_worker/requirements.txt

RUN pip install --no-cache-dir \
        -r /app/runpod_worker/requirements.txt

COPY runpod_worker/__init__.py \
     runpod_worker/worker.py \
     runpod_worker/handler.py \
     runpod_worker/start.sh \
     /app/runpod_worker/

# Entfernt vorsorglich Windows-CRLF aus dem Shellskript.
RUN sed -i 's/\r$//' /app/runpod_worker/start.sh \
    && chmod +x /app/runpod_worker/start.sh

# Überschreibt den Ollama-Entrypoint des Basis-Images.
ENTRYPOINT []

CMD ["/app/runpod_worker/start.sh"]