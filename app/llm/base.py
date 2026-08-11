from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from app.domain.metrics import ProviderTiming, TokenUsage
from app.domain.model_catalog import DomainModel, ModelParameters


class ModelRequest(DomainModel):
    """Providerunabhängiger Auftrag an ein Sprachmodell."""

    model_name: str = Field(min_length=1)
    instructions: str | None = None
    input_text: str = Field(min_length=1)

    parameters: ModelParameters = Field(default_factory=ModelParameters)

    response_schema: dict[str, Any] | None = None
    response_schema_name: str = Field(
        default="structured_response",
        min_length=1,
    )

    stream: bool = False

    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(DomainModel):
    """Providerunabhängige Antwort eines Sprachmodells."""

    provider_id: str = Field(min_length=1)

    requested_model_name: str = Field(min_length=1)
    actual_model_name: str = Field(min_length=1)

    text: str = Field(min_length=1)
    status: str | None = None

    provider_request_id: str | None = None

    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    provider_timing: ProviderTiming = Field(default_factory=ProviderTiming)

    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderHealthResult(DomainModel):
    """Ergebnis einer optionalen Provider-Erreichbarkeitsprüfung."""

    provider_id: str = Field(min_length=1)
    available: bool
    message: str | None = None
    latency_ms: float | None = Field(default=None, ge=0)


class DiscoveredModel(DomainModel):
    """Vom Provider gemeldetes Modell."""

    provider_id: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    display_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class ModelProvider(Protocol):
    """Einheitliche Schnittstelle für alle neuen Modelladapter."""

    provider_id: str

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Führt einen providerunabhängigen Modellauftrag aus."""
        ...


@runtime_checkable
class HealthCheckProvider(Protocol):
    """Optionale Schnittstelle für Provider-Erreichbarkeitstests."""

    provider_id: str

    async def check_health(self) -> ProviderHealthResult:
        """Prüft, ob der Provider erreichbar und verwendbar ist."""
        ...


@runtime_checkable
class ModelDiscoveryProvider(Protocol):
    """Optionale Schnittstelle zum Ermitteln verfügbarer Modelle."""

    provider_id: str

    async def discover_models(self) -> list[DiscoveredModel]:
        """Liefert die vom Provider aktuell gemeldeten Modelle."""
        ...


# ---------------------------------------------------------------------------
# Übergangskompatibilität mit Version 0.1
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMResponse:
    """Bisheriges einfaches Antwortformat der Version 0.1."""

    provider: str
    model: str
    text: str
    queue_duration_ms: float | None = None
    execution_duration_ms: float | None = None
    provider_request_id: str | None = None
    worker_id: str | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    """Bisherige Provider-Schnittstelle der Version 0.1."""

    provider_name: str
    model_name: str

    async def generate(
        self,
        prompt: str,
        *,
        response_schema: dict[str, Any] | None = None,
        response_schema_name: str = "structured_response",
    ) -> LLMResponse:
        """Führt den bisherigen Modellaufruf optional mit JSON-Schema aus."""
        ...
