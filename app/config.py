from dataclasses import dataclass
from math import isfinite
import os

from dotenv import load_dotenv


load_dotenv()


APP_MODE_LOCAL = "local"
APP_MODE_LAN_HTTPS = "lan_https"
APP_MODE_PRODUCTION = "production"
VALID_APP_MODES = {
    APP_MODE_LOCAL,
    APP_MODE_LAN_HTTPS,
    APP_MODE_PRODUCTION,
}

OLLAMA_DEFAULT_MODEL = (
    "mistral-small3.2:24b-instruct-2506-q8_0"
)

RUNPOD_VLLM_MODEL = (
    "RedHatAI/Mistral-Small-3.2-24B-Instruct-2506-FP8"
)

LEGACY_RUNPOD_OLLAMA_MODEL = (
    "ministral-3:14b-instruct-2512-q8_0"
)

def _first_configured_value(*names: str, fallback: str) -> str:
    """Liefert den ersten nicht leeren Wert aus der .env-Datei."""
    for name in names:
        value = os.getenv(name, "").strip()

        if value:
            return value

    return fallback

def _configured_runpod_model() -> str:
    """Migriert die frühere Ollama-ID auf die vLLM-Modell-ID."""

    value = _first_configured_value(
        "RUNPOD_DEFAULT_MODEL",
        "RUNPOD_MODEL",
        fallback=RUNPOD_VLLM_MODEL,
    )

    if value == LEGACY_RUNPOD_OLLAMA_MODEL:
        return RUNPOD_VLLM_MODEL

    return value

def _configured_app_mode() -> str:
    """Liest und validiert den zentralen Betriebsmodus."""
    app_mode = os.getenv(
        "APP_MODE",
        APP_MODE_LOCAL,
    ).strip().lower()

    if app_mode not in VALID_APP_MODES:
        allowed_values = ", ".join(sorted(VALID_APP_MODES))
        raise ValueError(
            "APP_MODE muss einen der folgenden Werte haben: "
            f"{allowed_values}."
        )

    return app_mode


def _positive_int_from_env(
    name: str,
    fallback: int,
) -> int:
    """Liest eine positive Ganzzahl aus der Umgebung."""
    raw_value = os.getenv(name, str(fallback)).strip()

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(
            f"{name} muss eine positive Ganzzahl sein."
        ) from exc

    if value <= 0:
        raise ValueError(
            f"{name} muss eine positive Ganzzahl sein."
        )

    return value


def _positive_float_from_env(
    name: str,
    fallback: float,
) -> float:
    """Liest eine positive endliche Fließkommazahl aus der Umgebung."""
    raw_value = os.getenv(name, str(fallback)).strip()

    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(
            f"{name} muss eine positive Zahl sein."
        ) from exc

    if not isfinite(value) or value <= 0:
        raise ValueError(
            f"{name} muss eine positive Zahl sein."
        )

    return value


def _bounded_int_from_env(
    name: str,
    fallback: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """Liest eine Ganzzahl innerhalb eines dokumentierten Bereichs."""

    value = _positive_int_from_env(name, fallback)

    if not minimum <= value <= maximum:
        raise ValueError(
            f"{name} muss zwischen {minimum} und {maximum} liegen."
        )

    return value


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv(
        "APP_NAME",
        "KI-Schreibfeedback-Prototyp",
    )

    app_mode: str = _configured_app_mode()

    auth_username: str = _first_configured_value(
        "AUTH_USERNAME",
        fallback="pruefer",
    )

    auth_password_hash: str | None = (
        os.getenv("AUTH_PASSWORD_HASH", "").strip() or None
    )

    session_secret: str | None = (
        os.getenv("SESSION_SECRET", "").strip() or None
    )

    session_max_age_seconds: int = _positive_int_from_env(
        "SESSION_MAX_AGE_SECONDS",
        3600,
    )

    login_rate_limit_attempts: int = _positive_int_from_env(
        "LOGIN_RATE_LIMIT_ATTEMPTS",
        5,
    )

    login_rate_limit_window_seconds: int = (
        _positive_int_from_env(
            "LOGIN_RATE_LIMIT_WINDOW_SECONDS",
            300,
        )
    )

    ollama_base_url: str = _first_configured_value(
        "OLLAMA_BASE_URL",
        fallback="http://localhost:11434",
    )

    ollama_model: str = _first_configured_value(
        "OLLAMA_DEFAULT_MODEL",
        "OLLAMA_MODEL",
        fallback=OLLAMA_DEFAULT_MODEL,
    )

    ollama_request_timeout_seconds: float = (
        _positive_float_from_env(
            "OLLAMA_REQUEST_TIMEOUT_SECONDS",
            600.0,
        )
    )

    openai_api_key: str | None = (
        os.getenv("OPENAI_API_KEY", "").strip() or None
    )

    openai_model: str = _first_configured_value(
        "OPENAI_DEFAULT_MODEL",
        "OPENAI_MODEL",
        fallback="gpt-5.6-luna",
    )

    openai_evaluation_model: str = _first_configured_value(
        "OPENAI_EVALUATION_MODEL",
        fallback="gpt-5.6-sol",
    )

    mistral_api_key: str | None = (
        os.getenv("MISTRAL_API_KEY", "").strip() or None
    )

    mistral_model: str = _first_configured_value(
        "MISTRAL_DEFAULT_MODEL",
        "MISTRAL_MODEL",
        fallback="mistral-small-latest",
    )

    runpod_api_key: str | None = (
        os.getenv("RUNPOD_API_KEY", "").strip() or None
    )

    runpod_endpoint_id: str | None = (
        os.getenv("RUNPOD_ENDPOINT_ID", "").strip() or None
    )

    runpod_model: str = _configured_runpod_model()

    runpod_job_timeout_seconds: float = float(
        os.getenv("RUNPOD_JOB_TIMEOUT_SECONDS", "900")
    )

    runpod_poll_interval_seconds: float = float(
        os.getenv("RUNPOD_POLL_INTERVAL_SECONDS", "1")
    )

    runpod_idle_timeout_seconds: int = _bounded_int_from_env(
        "RUNPOD_IDLE_TIMEOUT_SECONDS",
        5,
        minimum=1,
        maximum=3600,
    )

    analysis_database_path: str = _first_configured_value(
        "ANALYSIS_DATABASE_PATH",
        fallback="data/analysis_runs.sqlite3",
    )

    max_input_chars: int = int(
        os.getenv("MAX_INPUT_CHARS", "8000")
    )

    max_criteria: int = _positive_int_from_env(
        "MAX_CRITERIA",
        100,
    )

    max_criterion_chars: int = _positive_int_from_env(
        "MAX_CRITERION_CHARS",
        10000,
    )

    @property
    def session_cookie_secure(self) -> bool:
        """HTTPS-Modi dürfen Session-Cookies nur verschlüsselt senden."""
        return self.app_mode in {
            APP_MODE_LAN_HTTPS,
            APP_MODE_PRODUCTION,
        }

    @property
    def browser_overrides_allowed(self) -> bool:
        """Freie Provider-Eingaben bleiben auf lokale Entwicklung begrenzt."""
        return self.app_mode == APP_MODE_LOCAL

    @property
    def api_docs_enabled(self) -> bool:
        """Die interaktive API-Dokumentation bleibt online verborgen."""
        return self.app_mode != APP_MODE_PRODUCTION


settings = Settings()
