from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE_PATH = PROJECT_ROOT / ".env"

load_dotenv(dotenv_path=ENV_FILE_PATH, override=False)


class SettingsError(RuntimeError):
    """Ungültige oder unvollständige Anwendungskonfiguration."""


@dataclass(frozen=True, slots=True)
class Settings:
    """
    Vollständig validierte Backendkonfiguration.

    Bevorzugt werden die neuen Variablennamen ``*_DEFAULT_MODEL``. Die
    bisherigen Namen ``OLLAMA_MODEL`` und ``OPENAI_MODEL`` bleiben während
    des Umbaus als kompatible Fallbacks erhalten.
    """

    app_name: str

    ollama_base_url: str
    ollama_default_model: str

    openai_api_key: str | None = field(repr=False)
    openai_default_model: str

    model_catalog_path: Path
    analysis_database_path: Path

    max_input_chars: int
    max_criteria: int

    default_prompt_version: str
    default_criteria_version: str

    metrics_persistence_enabled: bool

    @classmethod
    def from_environment(cls) -> Settings:
        app_name = _get_first_nonempty_environment_value(
            "APP_NAME",
            default="KI-Schreibfeedback-Prototyp",
        )

        ollama_base_url = _get_first_nonempty_environment_value(
            "OLLAMA_BASE_URL",
            default="http://localhost:11434",
        ).rstrip("/")

        _validate_http_url(
            variable_name="OLLAMA_BASE_URL",
            value=ollama_base_url,
        )

        ollama_default_model = _get_first_nonempty_environment_value(
            "OLLAMA_DEFAULT_MODEL",
            "OLLAMA_MODEL",
            default="qwen3:30b",
        )

        openai_api_key = _get_optional_environment_value(
            "OPENAI_API_KEY"
        )

        openai_default_model = _get_first_nonempty_environment_value(
            "OPENAI_DEFAULT_MODEL",
            "OPENAI_MODEL",
            default="gpt-5.6-luna",
        )

        model_catalog_path = _resolve_project_path(
            _get_first_nonempty_environment_value(
                "MODEL_CATALOG_PATH",
                default="config/models.yaml",
            )
        )

        analysis_database_path = _resolve_project_path(
            _get_first_nonempty_environment_value(
                "ANALYSIS_DATABASE_PATH",
                default="data/analysis_runs.sqlite3",
            )
        )

        max_input_chars = _parse_integer_environment_value(
            "MAX_INPUT_CHARS",
            default=8000,
            minimum=1,
            maximum=100_000,
        )

        max_criteria = _parse_integer_environment_value(
            "MAX_CRITERIA",
            default=50,
            minimum=1,
            maximum=200,
        )

        default_prompt_version = _get_first_nonempty_environment_value(
            "DEFAULT_PROMPT_VERSION",
            default="feedback-v1",
        )

        default_criteria_version = (
            _get_first_nonempty_environment_value(
                "DEFAULT_CRITERIA_VERSION",
                default="criteria-v1",
            )
        )

        metrics_persistence_enabled = (
            _parse_boolean_environment_value(
                "METRICS_PERSISTENCE_ENABLED",
                default=True,
            )
        )

        return cls(
            app_name=app_name,
            ollama_base_url=ollama_base_url,
            ollama_default_model=ollama_default_model,
            openai_api_key=openai_api_key,
            openai_default_model=openai_default_model,
            model_catalog_path=model_catalog_path,
            analysis_database_path=analysis_database_path,
            max_input_chars=max_input_chars,
            max_criteria=max_criteria,
            default_prompt_version=default_prompt_version,
            default_criteria_version=default_criteria_version,
            metrics_persistence_enabled=metrics_persistence_enabled,
        )

    @property
    def ollama_model(self) -> str:
        """
        Kompatibilitätsalias für die bisherige Anwendung.

        Bestehender Code kann vorübergehend weiterhin
        ``settings.ollama_model`` verwenden.
        """

        return self.ollama_default_model

    @property
    def openai_model(self) -> str:
        """Kompatibilitätsalias für ``settings.openai_model``."""

        return self.openai_default_model

    @property
    def openai_configured(self) -> bool:
        """Zeigt ausschließlich an, ob ein OpenAI-Key vorhanden ist."""

        return bool(self.openai_api_key)

    def safe_summary(self) -> dict[str, Any]:
        """
        Liefert eine gefahrlos protokollierbare Konfigurationsübersicht.

        Der OpenAI-Key wird weder vollständig noch teilweise ausgegeben.
        """

        return {
            "app_name": self.app_name,
            "ollama_base_url": self.ollama_base_url,
            "ollama_default_model": self.ollama_default_model,
            "openai_default_model": self.openai_default_model,
            "openai_configured": self.openai_configured,
            "model_catalog_path": str(self.model_catalog_path),
            "analysis_database_path": str(
                self.analysis_database_path
            ),
            "max_input_chars": self.max_input_chars,
            "max_criteria": self.max_criteria,
            "default_prompt_version": self.default_prompt_version,
            "default_criteria_version": self.default_criteria_version,
            "metrics_persistence_enabled": (
                self.metrics_persistence_enabled
            ),
        }


def _get_first_nonempty_environment_value(
    *variable_names: str,
    default: str,
) -> str:
    for variable_name in variable_names:
        value = os.getenv(variable_name)

        if value is not None and value.strip():
            return value.strip()

    if not default.strip():
        joined_names = ", ".join(variable_names)
        raise SettingsError(
            f"Keine gültige Konfiguration für {joined_names} gefunden."
        )

    return default.strip()


def _get_optional_environment_value(
    variable_name: str,
) -> str | None:
    value = os.getenv(variable_name)

    if value is None:
        return None

    normalized_value = value.strip()
    return normalized_value or None


def _parse_integer_environment_value(
    variable_name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw_value = os.getenv(variable_name)

    if raw_value is None or not raw_value.strip():
        value = default
    else:
        try:
            value = int(raw_value.strip())
        except ValueError as exc:
            raise SettingsError(
                f"{variable_name} muss eine ganze Zahl sein."
            ) from exc

    if not minimum <= value <= maximum:
        raise SettingsError(
            f"{variable_name} muss zwischen {minimum} und {maximum} liegen."
        )

    return value


def _parse_boolean_environment_value(
    variable_name: str,
    *,
    default: bool,
) -> bool:
    raw_value = os.getenv(variable_name)

    if raw_value is None or not raw_value.strip():
        return default

    normalized_value = raw_value.strip().lower()

    true_values = {"1", "true", "yes", "on", "ja"}
    false_values = {"0", "false", "no", "off", "nein"}

    if normalized_value in true_values:
        return True

    if normalized_value in false_values:
        return False

    raise SettingsError(
        f"{variable_name} muss einen booleschen Wert enthalten."
    )


def _resolve_project_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    return path.resolve()


def _validate_http_url(
    *,
    variable_name: str,
    value: str,
) -> None:
    parsed_url = urlparse(value)

    if parsed_url.scheme not in {"http", "https"}:
        raise SettingsError(
            f"{variable_name} muss mit http:// oder https:// beginnen."
        )

    if not parsed_url.netloc:
        raise SettingsError(
            f"{variable_name} enthält keinen gültigen Host."
        )


settings = Settings.from_environment()