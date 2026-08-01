from __future__ import annotations

from collections.abc import Iterable

from app.domain.model_catalog import (
    DomainModel,
    ModelCapabilities,
    ModelDefinition,
    ProcessingLocation,
    ProviderDefinition,
)


class RegistryError(RuntimeError):
    """Fehler beim Aufbau oder Zugriff auf den Modellkatalog."""


class ResolvedModel(DomainModel):
    """Zusammengehörige Anbieter- und Modelldefinition."""

    provider: ProviderDefinition
    model: ModelDefinition

    @property
    def provider_id(self) -> str:
        return self.provider.id

    @property
    def model_id(self) -> str:
        return self.model.id

    @property
    def processing_location(
        self,
    ) -> ProcessingLocation:
        return self.provider.processing_location


class ProviderRegistry:
    """Registry aller bekannten Modellanbieter."""

    def __init__(
        self,
        providers: Iterable[
            ProviderDefinition
        ] = (),
    ) -> None:
        self._providers: dict[
            str,
            ProviderDefinition,
        ] = {}

        for provider in providers:
            self.register(provider)

    def register(
        self,
        provider: ProviderDefinition,
    ) -> None:
        """Registriert genau einen Anbieter."""

        if provider.id in self._providers:
            raise RegistryError(
                (
                    "Provider-ID ist mehrfach vorhanden: "
                    f"{provider.id}"
                )
            )

        self._providers[provider.id] = provider

    def register_provider(
        self,
        provider: ProviderDefinition,
    ) -> None:
        """Alias für register()."""

        self.register(provider)

    def get(
        self,
        provider_id: str,
    ) -> ProviderDefinition | None:
        """Liefert einen Anbieter oder None."""

        normalized_id = self._normalize_id(
            provider_id
        )
        return self._providers.get(normalized_id)

    def get_provider(
        self,
        provider_id: str,
    ) -> ProviderDefinition | None:
        """Alias für get()."""

        return self.get(provider_id)

    def require(
        self,
        provider_id: str,
    ) -> ProviderDefinition:
        """Liefert einen Anbieter oder löst einen RegistryError aus."""

        provider = self.get(provider_id)

        if provider is None:
            raise RegistryError(
                (
                    "Unbekannter Modellanbieter: "
                    f"{provider_id}"
                )
            )

        return provider

    def require_provider(
        self,
        provider_id: str,
    ) -> ProviderDefinition:
        """Alias für require()."""

        return self.require(provider_id)

    def list(
        self,
        *,
        enabled_only: bool = False,
    ) -> tuple[ProviderDefinition, ...]:
        """Liefert die registrierten Anbieter sortiert nach ID."""

        providers = sorted(
            self._providers.values(),
            key=lambda provider: provider.id,
        )

        if enabled_only:
            providers = [
                provider
                for provider in providers
                if provider.enabled
            ]

        return tuple(providers)

    def list_providers(
        self,
        *,
        enabled_only: bool = False,
    ) -> tuple[ProviderDefinition, ...]:
        """Alias für list()."""

        return self.list(
            enabled_only=enabled_only
        )

    def __contains__(
        self,
        provider_id: object,
    ) -> bool:
        if not isinstance(provider_id, str):
            return False

        return (
            self._normalize_id(provider_id)
            in self._providers
        )

    def __len__(self) -> int:
        return len(self._providers)

    @staticmethod
    def _normalize_id(
        value: str,
    ) -> str:
        return value.strip().lower()


class ModelRegistry:
    """
    Registry aller Modelle und ihrer zugehörigen Provider.

    Der Verarbeitungsort wird ausschließlich über die
    ProviderDefinition bestimmt und nicht im Modell dupliziert.
    """

    def __init__(
        self,
        providers: (
            Iterable[ProviderDefinition]
            | ProviderRegistry
        ) = (),
        models: Iterable[ModelDefinition] = (),
        provider_registry: ProviderRegistry | None = None,
    ) -> None:
        if provider_registry is not None:
            self._provider_registry = (
                provider_registry
            )
        elif isinstance(
            providers,
            ProviderRegistry,
        ):
            self._provider_registry = providers
        else:
            self._provider_registry = (
                ProviderRegistry(providers)
            )

        self._models: dict[
            str,
            ModelDefinition,
        ] = {}

        for model in models:
            self.register_model(model)

    @property
    def provider_registry(
        self,
    ) -> ProviderRegistry:
        return self._provider_registry

    def register_provider(
        self,
        provider: ProviderDefinition,
    ) -> None:
        """Registriert einen weiteren Anbieter."""

        self._provider_registry.register(
            provider
        )

    def register_model(
        self,
        model: ModelDefinition,
    ) -> None:
        """Registriert ein Modell und prüft dessen Providerbezug."""

        if model.id in self._models:
            raise RegistryError(
                (
                    "Modell-ID ist mehrfach vorhanden: "
                    f"{model.id}"
                )
            )

        provider = self._provider_registry.get(
            model.provider_id
        )

        if provider is None:
            raise RegistryError(
                (
                    f"Modell '{model.id}' verweist auf den "
                    "unbekannten Provider "
                    f"'{model.provider_id}'."
                )
            )

        self._validate_model_capabilities(
            provider=provider,
            model=model,
        )

        self._models[model.id] = model

    def get_model(
        self,
        model_id: str,
    ) -> ModelDefinition | None:
        """Liefert ein Modell oder None."""

        normalized_id = self._normalize_id(
            model_id
        )
        return self._models.get(normalized_id)

    def require_model(
        self,
        model_id: str,
    ) -> ModelDefinition:
        """Liefert ein Modell oder löst einen RegistryError aus."""

        model = self.get_model(model_id)

        if model is None:
            raise RegistryError(
                f"Unbekanntes Modell: {model_id}"
            )

        return model

    def get_provider(
        self,
        provider_id: str,
    ) -> ProviderDefinition | None:
        """Liefert einen Provider oder None."""

        return self._provider_registry.get(
            provider_id
        )

    def require_provider(
        self,
        provider_id: str,
    ) -> ProviderDefinition:
        """Liefert einen Provider oder löst einen RegistryError aus."""

        return self._provider_registry.require(
            provider_id
        )

    def resolve(
        self,
        model_id: str,
        *,
        require_enabled: bool = True,
    ) -> ResolvedModel:
        """Löst eine interne Modell-ID zu Provider und Modell auf."""

        model = self.require_model(model_id)
        provider = self.require_provider(
            model.provider_id
        )

        if require_enabled and not provider.enabled:
            raise RegistryError(
                (
                    "Der Provider ist deaktiviert: "
                    f"{provider.id}"
                )
            )

        if require_enabled and not model.enabled:
            raise RegistryError(
                (
                    "Das Modell ist deaktiviert: "
                    f"{model.id}"
                )
            )

        return ResolvedModel(
            provider=provider,
            model=model,
        )

    def list_models(
        self,
        *,
        provider_id: str | None = None,
        enabled_only: bool = False,
    ) -> tuple[ModelDefinition, ...]:
        """Liefert Modelle mit optionalem Providerfilter."""

        models = sorted(
            self._models.values(),
            key=lambda model: model.id,
        )

        if provider_id is not None:
            normalized_provider_id = (
                self._normalize_id(provider_id)
            )

            models = [
                model
                for model in models
                if (
                    model.provider_id
                    == normalized_provider_id
                )
            ]

        if enabled_only:
            enabled_provider_ids = {
                provider.id
                for provider
                in self._provider_registry.list(
                    enabled_only=True
                )
            }

            models = [
                model
                for model in models
                if (
                    model.enabled
                    and model.provider_id
                    in enabled_provider_ids
                )
            ]

        return tuple(models)

    def list_providers(
        self,
        *,
        enabled_only: bool = False,
    ) -> tuple[ProviderDefinition, ...]:
        """Liefert die registrierten Provider."""

        return self._provider_registry.list(
            enabled_only=enabled_only
        )

    def __contains__(
        self,
        model_id: object,
    ) -> bool:
        if not isinstance(model_id, str):
            return False

        return (
            self._normalize_id(model_id)
            in self._models
        )

    def __len__(self) -> int:
        return len(self._models)

    @staticmethod
    def _validate_model_capabilities(
        *,
        provider: ProviderDefinition,
        model: ModelDefinition,
    ) -> None:
        provider_capabilities = (
            provider.capabilities
            .supported_model_capabilities
        )

        unsupported_capabilities: list[str] = []

        for capability_name in (
            ModelCapabilities.model_fields
        ):
            capability_value = getattr(
                model.capabilities,
                capability_name,
            )

            if (
                bool(capability_value)
                and capability_name
                not in provider_capabilities
            ):
                unsupported_capabilities.append(
                    capability_name
                )

        if unsupported_capabilities:
            formatted_capabilities = ", ".join(
                sorted(unsupported_capabilities)
            )

            raise RegistryError(
                (
                    f"Modell '{model.id}' verwendet "
                    "Capabilities, die vom Provider "
                    f"'{provider.id}' nicht unterstützt werden: "
                    f"{formatted_capabilities}"
                )
            )

    @staticmethod
    def _normalize_id(
        value: str,
    ) -> str:
        return value.strip().lower()