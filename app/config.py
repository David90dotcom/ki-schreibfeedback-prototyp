from dataclasses import dataclass
import os

from dotenv import load_dotenv


load_dotenv()


def _first_configured_value(*names: str, fallback: str) -> str:
    """Liefert den ersten nicht leeren Wert aus der .env-Datei."""
    for name in names:
        value = os.getenv(name, "").strip()

        if value:
            return value

    return fallback


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv(
        "APP_NAME",
        "KI-Schreibfeedback-Prototyp",
    )

    ollama_base_url: str = _first_configured_value(
        "OLLAMA_BASE_URL",
        fallback="http://localhost:11434",
    )

    ollama_model: str = _first_configured_value(
        "OLLAMA_DEFAULT_MODEL",
        "OLLAMA_MODEL",
        fallback="ministral-3:14b-instruct-2512-q8_0",
    )

    openai_api_key: str | None = (
        os.getenv("OPENAI_API_KEY", "").strip() or None
    )

    openai_model: str = _first_configured_value(
        "OPENAI_DEFAULT_MODEL",
        "OPENAI_MODEL",
        fallback="gpt-5.6-luna",
    )

    runpod_api_key: str | None = (
        os.getenv("RUNPOD_API_KEY", "").strip() or None
    )

    runpod_endpoint_id: str | None = (
        os.getenv("RUNPOD_ENDPOINT_ID", "").strip() or None
    )

    runpod_model: str = _first_configured_value(
        "RUNPOD_DEFAULT_MODEL",
        "RUNPOD_MODEL",
        fallback="ministral-3:14b-instruct-2512-q8_0",
    )

    runpod_job_timeout_seconds: float = float(
        os.getenv("RUNPOD_JOB_TIMEOUT_SECONDS", "900")
    )

    runpod_poll_interval_seconds: float = float(
        os.getenv("RUNPOD_POLL_INTERVAL_SECONDS", "1")
    )

    max_input_chars: int = int(
        os.getenv("MAX_INPUT_CHARS", "8000")
    )


settings = Settings()