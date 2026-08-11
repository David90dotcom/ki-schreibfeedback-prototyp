from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, ValidationError, model_validator

from app.domain.model_catalog import (
    DomainModel,
    ModelDefinition,
    ProviderDefinition,
)
from app.llm.registry import ModelRegistry, RegistryError


FORBIDDEN_SECRET_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "bearer_token",
    "client_secret",
    "mistral_api_key",
    "openai_api_key",
    "password",
    "secret",
    "token",
}


class CatalogLoadError(RuntimeError):
    """Fehler beim Lesen oder Validieren des Modellkatalogs."""

    def __init__(
        self,
        *,
        source_path: Path,
        public_message: str,
        technical_message: str | None = None,
    ) -> None:
        self.source_path = source_path
        self.public_message = public_message
        self.technical_message = technical_message

        super().__init__(public_message)


class CatalogDocument(DomainModel):
    """Vollständig validierter Inhalt der Datei ``config/models.yaml``."""

    schema_version: str = Field(default="1", min_length=1)

    providers: tuple[ProviderDefinition, ...] = Field(min_length=1)
    models: tuple[ModelDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> CatalogDocument:
        provider_ids = [provider.id for provider in self.providers]
        model_ids = [model.id for model in self.models]

        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError(
                "Der Modellkatalog enthält doppelte Provider-IDs."
            )

        if len(model_ids) != len(set(model_ids)):
            raise ValueError(
                "Der Modellkatalog enthält doppelte Modell-IDs."
            )

        return self

    def build_registry(self) -> ModelRegistry:
        """Erzeugt eine validierte Registry aus diesem Katalog."""

        return ModelRegistry(
            providers=self.providers,
            models=self.models,
        )


def load_model_catalog(source_path: str | Path) -> CatalogDocument:
    """
    Liest und validiert einen YAML-Modellkatalog.

    Der Katalog darf ausschließlich ungefährliche Metadaten enthalten.
    Zugangsdaten werden nur über Umgebungsvariablen geladen und sind deshalb
    innerhalb der YAML-Datei ausdrücklich verboten.
    """

    path = Path(source_path)

    if not path.is_file():
        raise CatalogLoadError(
            source_path=path,
            public_message=(
                "Der konfigurierte Modellkatalog wurde nicht gefunden."
            ),
            technical_message=(
                f"Model catalog file does not exist: {path}"
            ),
        )

    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CatalogLoadError(
            source_path=path,
            public_message=(
                "Der Modellkatalog konnte nicht gelesen werden."
            ),
            technical_message=(
                f"{type(exc).__name__}: {exc}"
            ),
        ) from exc

    if not raw_text.strip():
        raise CatalogLoadError(
            source_path=path,
            public_message="Der Modellkatalog ist leer.",
            technical_message=f"Model catalog is empty: {path}",
        )

    try:
        raw_data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise CatalogLoadError(
            source_path=path,
            public_message=(
                "Der Modellkatalog enthält ungültiges YAML."
            ),
            technical_message=(
                f"{type(exc).__name__}: {exc}"
            ),
        ) from exc

    if not isinstance(raw_data, dict):
        raise CatalogLoadError(
            source_path=path,
            public_message=(
                "Der Modellkatalog benötigt ein YAML-Objekt auf der "
                "obersten Ebene."
            ),
            technical_message=(
                f"Expected mapping at document root, got "
                f"{type(raw_data).__name__}."
            ),
        )

    try:
        _reject_embedded_secrets(raw_data)
    except ValueError as exc:
        raise CatalogLoadError(
            source_path=path,
            public_message=(
                "Der Modellkatalog darf keine Zugangsdaten enthalten."
            ),
            technical_message=str(exc),
        ) from exc

    try:
        return CatalogDocument.model_validate(raw_data)
    except ValidationError as exc:
        raise CatalogLoadError(
            source_path=path,
            public_message=(
                "Der Modellkatalog entspricht nicht dem erwarteten Schema."
            ),
            technical_message=str(exc),
        ) from exc


def load_model_registry(source_path: str | Path) -> ModelRegistry:
    """Lädt den Modellkatalog und erzeugt daraus eine ModelRegistry."""

    path = Path(source_path)
    catalog = load_model_catalog(path)

    try:
        return catalog.build_registry()
    except RegistryError as exc:
        raise CatalogLoadError(
            source_path=path,
            public_message=(
                "Die Provider- und Modellbeziehungen im Modellkatalog sind "
                "ungültig."
            ),
            technical_message=str(exc),
        ) from exc


def _reject_embedded_secrets(
    value: Any,
    *,
    current_path: str = "root",
) -> None:
    """
    Durchsucht den Rohkatalog rekursiv nach typischen Secret-Feldnamen.

    Es werden nur Schlüssel geprüft. Modellnamen oder Beschreibungen dürfen
    selbstverständlich Wörter wie ``token`` enthalten.
    """

    if isinstance(value, dict):
        for key, nested_value in value.items():
            normalized_key = str(key).strip().lower().replace("-", "_")
            nested_path = f"{current_path}.{key}"

            if normalized_key in FORBIDDEN_SECRET_KEYS:
                raise ValueError(
                    f"Verbotenes Secret-Feld im Modellkatalog: {nested_path}"
                )

            _reject_embedded_secrets(
                nested_value,
                current_path=nested_path,
            )

        return

    if isinstance(value, list):
        for index, nested_value in enumerate(value):
            _reject_embedded_secrets(
                nested_value,
                current_path=f"{current_path}[{index}]",
            )
