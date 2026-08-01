from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock

from app.config import Settings
from app.llm.base import ModelProvider
from app.llm.ollama_client import OllamaProvider
from app.llm.openai_client import OpenAIProvider


ProviderBuilder = Callable[[], ModelProvider]
ConfigurationCheck = Callable[[], bool]


class ProviderFactoryError(RuntimeError):
    """Basisklasse für Konfigurationsfehler der Provider-Factory."""


class ProviderNotRegisteredError(ProviderFactoryError):
    """Für den angeforderten Provider ist kein Adapter registriert."""


class ProviderNotConfiguredError(ProviderFactoryError):
    """Der Adapter ist vorhanden, aber nicht verwendbar konfiguriert."""


@dataclass(frozen=True)
class ProviderAvailability:
    """Konfigurationsstatus eines Provider-Adapters."""

    provider_id: str
    registered: bool
    configured: bool
    instantiated: bool
    selectable: bool
    message: str


@dataclass(frozen=True)
class ProviderRegistration:
    """Interne Registrierung eines Provider-Adapters."""

    provider_id: str
    builder: ProviderBuilder
    configuration_check: ConfigurationCheck
    configuration_error: str


class ProviderFactory:
    """
    Erstellt und verwaltet providerunabhängige Modelladapter.

    Neue Anbieter werden ausschließlich über register(...) ergänzt.
    AnalysisService und MetricsService müssen dafür nicht verändert werden.
    """

    def __init__(self) -> None:
        self._registrations: dict[
            str,
            ProviderRegistration,
        ] = {}
        self._instances: dict[str, ModelProvider] = {}
        self._lock = RLock()

    def register(
        self,
        *,
        provider_id: str,
        builder: ProviderBuilder,
        configuration_check: ConfigurationCheck | None = None,
        configuration_error: str | None = None,
    ) -> None:
        """Registriert einen neuen Provider-Adapter."""

        normalized_id = self._normalize_provider_id(
            provider_id
        )

        if configuration_check is None:
            configuration_check = lambda: True

        if configuration_error is None:
            configuration_error = (
                f"Provider '{normalized_id}' ist nicht vollständig "
                "konfiguriert."
            )

        with self._lock:
            if normalized_id in self._registrations:
                raise ProviderFactoryError(
                    (
                        "Für den Provider wurde bereits ein Adapter "
                        f"registriert: {normalized_id}"
                    )
                )

            self._registrations[normalized_id] = (
                ProviderRegistration(
                    provider_id=normalized_id,
                    builder=builder,
                    configuration_check=configuration_check,
                    configuration_error=configuration_error,
                )
            )

    def get(
        self,
        provider_id: str,
    ) -> ModelProvider:
        """
        Liefert eine wiederverwendete Provider-Instanz.

        Die Instanz wird erst beim ersten tatsächlichen Zugriff erzeugt.
        """

        normalized_id = self._normalize_provider_id(
            provider_id
        )

        with self._lock:
            existing_instance = self._instances.get(
                normalized_id
            )

            if existing_instance is not None:
                return existing_instance

            registration = self._registrations.get(
                normalized_id
            )

            if registration is None:
                raise ProviderNotRegisteredError(
                    (
                        "Für den ausgewählten Anbieter ist kein "
                        f"Adapter registriert: {normalized_id}"
                    )
                )

            if not self._is_registration_configured(
                registration
            ):
                raise ProviderNotConfiguredError(
                    registration.configuration_error
                )

            provider = registration.builder()

            if provider.provider_id != normalized_id:
                raise ProviderFactoryError(
                    (
                        "Die Provider-ID des erzeugten Adapters stimmt "
                        "nicht mit seiner Registrierung überein. "
                        f"Erwartet: {normalized_id}; "
                        f"erhalten: {provider.provider_id}"
                    )
                )

            self._instances[normalized_id] = provider

            return provider

    def get_availability(
        self,
        provider_id: str,
    ) -> ProviderAvailability:
        """Liefert den lokalen Konfigurationsstatus eines Providers."""

        normalized_id = self._normalize_provider_id(
            provider_id
        )

        with self._lock:
            registration = self._registrations.get(
                normalized_id
            )

            if registration is None:
                return ProviderAvailability(
                    provider_id=normalized_id,
                    registered=False,
                    configured=False,
                    instantiated=False,
                    selectable=False,
                    message=(
                        "Für diesen Provider ist kein Adapter "
                        "registriert."
                    ),
                )

            configured = self._is_registration_configured(
                registration
            )
            instantiated = normalized_id in self._instances

            if configured:
                message = "Provider ist lokal konfiguriert."
            else:
                message = registration.configuration_error

            return ProviderAvailability(
                provider_id=normalized_id,
                registered=True,
                configured=configured,
                instantiated=instantiated,
                selectable=configured,
                message=message,
            )

    def list_availability(
        self,
    ) -> list[ProviderAvailability]:
        """Liefert den Status aller registrierten Provider."""

        with self._lock:
            provider_ids = sorted(
                self._registrations
            )

        return [
            self.get_availability(provider_id)
            for provider_id in provider_ids
        ]

    def registered_provider_ids(self) -> tuple[str, ...]:
        """Liefert alle registrierten Provider-IDs."""

        with self._lock:
            return tuple(
                sorted(self._registrations)
            )

    async def close_all(self) -> None:
        """Schließt Netzwerkclients aller erzeugten Provider."""

        with self._lock:
            instances = tuple(
                self._instances.values()
            )
            self._instances.clear()

        closing_errors: list[str] = []

        for provider in instances:
            close_method = getattr(
                provider,
                "close",
                None,
            )

            if not callable(close_method):
                continue

            try:
                result = close_method()

                if inspect.isawaitable(result):
                    await result

            except Exception as exc:
                closing_errors.append(
                    (
                        f"{provider.provider_id}: "
                        f"{type(exc).__name__}"
                    )
                )

        if closing_errors:
            raise ProviderFactoryError(
                (
                    "Nicht alle Provider konnten sauber geschlossen "
                    "werden: "
                    + ", ".join(closing_errors)
                )
            )

    @staticmethod
    def _normalize_provider_id(
        provider_id: str,
    ) -> str:
        normalized_id = provider_id.strip().lower()

        if not normalized_id:
            raise ValueError(
                "provider_id darf nicht leer sein."
            )

        return normalized_id

    @staticmethod
    def _is_registration_configured(
        registration: ProviderRegistration,
    ) -> bool:
        try:
            return bool(
                registration.configuration_check()
            )
        except Exception:
            return False


def create_default_provider_factory(
    settings: Settings,
) -> ProviderFactory:
    """
    Erstellt die Factory mit den momentan implementierten Adaptern.

    Weitere Anbieter werden später an dieser Stelle oder über ein
    separates Registrierungsmodul ergänzt.
    """

    factory = ProviderFactory()

    factory.register(
        provider_id="ollama",
        builder=lambda: OllamaProvider(
            base_url=settings.ollama_base_url,
            model_name=settings.ollama_model,
        ),
        configuration_check=lambda: bool(
            settings.ollama_base_url.strip()
        ),
        configuration_error=(
            "OLLAMA_BASE_URL ist nicht konfiguriert."
        ),
    )

    factory.register(
        provider_id="openai",
        builder=lambda: OpenAIProvider(
            api_key=settings.openai_api_key,
            model_name=settings.openai_model,
        ),
        configuration_check=lambda: bool(
            settings.openai_api_key
        ),
        configuration_error=(
            "OPENAI_API_KEY ist nicht gesetzt. "
            "Prüfe die lokale .env-Datei."
        ),
    )

    return factory