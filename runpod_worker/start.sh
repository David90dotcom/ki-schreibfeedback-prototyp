#!/usr/bin/env bash
set -Eeuo pipefail

OLLAMA_LOG="/tmp/ollama.log"
OLLAMA_URL="${OLLAMA_BASE_URL%/}"
worker_pid=""

ollama serve >"${OLLAMA_LOG}" 2>&1 &
ollama_pid=$!

cleanup() {
    if [[ -n "${worker_pid}" ]]; then
        kill "${worker_pid}" 2>/dev/null || true
        wait "${worker_pid}" 2>/dev/null || true
    fi

    kill "${ollama_pid}" 2>/dev/null || true
    wait "${ollama_pid}" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

ready=0

for attempt in $(seq 1 120); do
    if curl -fsS \
        "${OLLAMA_URL}/api/tags" \
        >/dev/null; then
        ready=1
        break
    fi

    if ! kill -0 "${ollama_pid}" 2>/dev/null; then
        echo "Ollama wurde unerwartet beendet." >&2
        cat "${OLLAMA_LOG}" >&2
        exit 1
    fi

    sleep 1
done

if [[ "${ready}" -ne 1 ]]; then
    echo "Ollama wurde nicht rechtzeitig bereit." >&2
    cat "${OLLAMA_LOG}" >&2
    exit 1
fi

if ! ollama show "${OLLAMA_MODEL}" >/dev/null 2>&1; then
    echo \
        "Das konfigurierte Modell '${OLLAMA_MODEL}' fehlt." \
        >&2
    exit 1
fi

python3 -u -m runpod_worker.handler &
worker_pid=$!

wait "${worker_pid}"