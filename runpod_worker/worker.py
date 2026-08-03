from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

import httpx


DEFAULT_MODEL = "ministral-3:14b-instruct-2512-q8_0"
ALLOWED_OPTION_KEYS = frozenset(
    {
        "temperature",
        "num_predict",
        "seed",
    }
)


@dataclass(frozen=True)
class WorkerSettings:
    model_name: str = DEFAULT_MODEL
    ollama_base_url: str = "http://127.0.0.1:11434"
    request_timeout_seconds: float = 600.0
    max_prompt_chars: int = 50_000

    @classmethod
    def from_environment(cls) -> "WorkerSettings":
        return cls(
            model_name=os.getenv(
                "OLLAMA_MODEL",
                DEFAULT_MODEL,
            ).strip(),
            ollama_base_url=os.getenv(
                "OLLAMA_BASE_URL",
                "http://127.0.0.1:11434",
            ).rstrip("/"),
            request_timeout_seconds=float(
                os.getenv(
                    "OLLAMA_REQUEST_TIMEOUT_SECONDS",
                    "600",
                )
            ),
            max_prompt_chars=int(
                os.getenv(
                    "MAX_PROMPT_CHARS",
                    "50000",
                )
            ),
        )


def _required_text(
    payload: Mapping[str, Any],
    key: str,
    *,
    max_chars: int,
) -> str:
    value = payload.get(key)

    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"'{key}' muss ein nichtleerer Text sein."
        )

    if len(value) > max_chars:
        raise ValueError(
            f"'{key}' darf höchstens "
            f"{max_chars} Zeichen enthalten."
        )

    return value


def _optional_text(
    payload: Mapping[str, Any],
    key: str,
    *,
    max_chars: int,
) -> str | None:
    value = payload.get(key)

    if value is None:
        return None

    if not isinstance(value, str):
        raise ValueError(
            f"'{key}' muss ein Text sein."
        )

    if len(value) > max_chars:
        raise ValueError(
            f"'{key}' darf höchstens "
            f"{max_chars} Zeichen enthalten."
        )

    return value


def _build_ollama_payload(
    job: Mapping[str, Any],
    settings: WorkerSettings,
) -> dict[str, Any]:
    job_input = job.get("input")

    if not isinstance(job_input, Mapping):
        raise ValueError(
            "Der RunPod-Auftrag benötigt "
            "ein Objekt 'input'."
        )

    requested_model = job_input.get(
        "model",
        settings.model_name,
    )

    if requested_model != settings.model_name:
        raise ValueError(
            "Dieses Endpoint erlaubt ausschließlich "
            "das konfigurierte Modell "
            f"'{settings.model_name}'."
        )

    if job_input.get("stream", False) is not False:
        raise ValueError(
            "Der Worker unterstützt nur "
            "'stream: false'."
        )

    prompt = _required_text(
        job_input,
        "prompt",
        max_chars=settings.max_prompt_chars,
    )
    system = _optional_text(
        job_input,
        "system",
        max_chars=settings.max_prompt_chars,
    )

    options = job_input.get("options", {})

    if not isinstance(options, Mapping):
        raise ValueError(
            "'options' muss ein Objekt sein."
        )

    unknown_options = (
        set(options) - ALLOWED_OPTION_KEYS
    )

    if unknown_options:
        names = ", ".join(
            sorted(
                str(name)
                for name in unknown_options
            )
        )
        raise ValueError(
            f"Nicht erlaubte Ollama-Optionen: "
            f"{names}."
        )

    response_format = job_input.get("format")

    if (
        response_format is not None
        and not isinstance(
            response_format,
            (str, Mapping),
        )
    ):
        raise ValueError(
            "'format' muss ein Text "
            "oder JSON-Schema sein."
        )

    ollama_payload: dict[str, Any] = {
        "model": settings.model_name,
        "prompt": prompt,
        "stream": False,
        "keep_alive": -1,
        "options": dict(options),
    }

    if system is not None:
        ollama_payload["system"] = system

    if response_format is not None:
        ollama_payload["format"] = response_format

    return ollama_payload


def _response_payload(
    data: Any,
    requested_model: str,
) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise RuntimeError(
            "Ollama hat keine gültige "
            "JSON-Antwort geliefert."
        )

    generated_text = data.get("response")

    if (
        not isinstance(generated_text, str)
        or not generated_text.strip()
    ):
        raise RuntimeError(
            "Ollama hat keinen Antworttext geliefert."
        )

    result: dict[str, Any] = {
        "response": generated_text,
        "model": data.get(
            "model",
            requested_model,
        ),
    }

    for key in (
        "done",
        "done_reason",
        "total_duration",
        "load_duration",
        "prompt_eval_count",
        "prompt_eval_duration",
        "eval_count",
        "eval_duration",
    ):
        if key in data:
            result[key] = data[key]

    return result


def process_job(
    job: Mapping[str, Any],
    *,
    settings: WorkerSettings,
    client: httpx.Client,
) -> dict[str, Any]:
    ollama_payload = _build_ollama_payload(
        job,
        settings,
    )

    try:
        response = client.post(
            "/api/generate",
            json=ollama_payload,
        )
        response.raise_for_status()

    except httpx.TimeoutException as exc:
        raise RuntimeError(
            "Zeitüberschreitung bei "
            "der Ollama-Anfrage."
        ) from exc

    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            "Ollama hat die Anfrage mit "
            "HTTP-Status "
            f"{exc.response.status_code} abgelehnt."
        ) from exc

    except httpx.RequestError as exc:
        raise RuntimeError(
            "Ollama ist im Worker "
            "nicht erreichbar."
        ) from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            "Ollama hat keine gültige "
            "JSON-Antwort geliefert."
        ) from exc

    return _response_payload(
        data,
        settings.model_name,
    )


SETTINGS = WorkerSettings.from_environment()

OLLAMA_CLIENT = httpx.Client(
    base_url=SETTINGS.ollama_base_url,
    timeout=SETTINGS.request_timeout_seconds,
    trust_env=False,
)


def handler(
    job: Mapping[str, Any],
) -> dict[str, Any]:
    return process_job(
        job,
        settings=SETTINGS,
        client=OLLAMA_CLIENT,
    )