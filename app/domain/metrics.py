from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from app.domain.model_catalog import DomainModel


class MetricSource(str, Enum):
    """Herkunft eines Messwerts."""

    PROVIDER = "provider"
    CLIENT = "client"
    CALCULATED = "calculated"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class ErrorType(str, Enum):
    """Providerunabhängige Kategorien fehlgeschlagener Aufrufe."""

    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    RATE_LIMIT = "rate_limit"
    MODEL_NOT_FOUND = "model_not_found"
    INVALID_REQUEST = "invalid_request"
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INVALID_RESPONSE = "invalid_response"
    STRUCTURED_OUTPUT = "structured_output"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class IntegerMetric(DomainModel):
    """Ganzzahliger Messwert mit dokumentierter Herkunft."""

    value: int | None = Field(default=None, ge=0)
    source: MetricSource = MetricSource.UNAVAILABLE

    @model_validator(mode="after")
    def validate_availability(
        self,
    ) -> IntegerMetric:
        if (
            self.value is None
            and self.source != MetricSource.UNAVAILABLE
        ):
            raise ValueError(
                (
                    "Ein nicht verfügbarer Messwert muss die "
                    "Quelle 'unavailable' besitzen."
                )
            )

        if (
            self.value is not None
            and self.source == MetricSource.UNAVAILABLE
        ):
            raise ValueError(
                (
                    "Für einen vorhandenen Messwert muss eine "
                    "Quelle angegeben werden."
                )
            )

        return self


class FloatMetric(DomainModel):
    """Fließkomma-Messwert mit dokumentierter Herkunft."""

    value: float | None = Field(default=None, ge=0)
    source: MetricSource = MetricSource.UNAVAILABLE
    unit: str | None = None

    @model_validator(mode="after")
    def validate_availability(
        self,
    ) -> FloatMetric:
        if (
            self.value is None
            and self.source != MetricSource.UNAVAILABLE
        ):
            raise ValueError(
                (
                    "Ein nicht verfügbarer Messwert muss die "
                    "Quelle 'unavailable' besitzen."
                )
            )

        if (
            self.value is not None
            and self.source == MetricSource.UNAVAILABLE
        ):
            raise ValueError(
                (
                    "Für einen vorhandenen Messwert muss eine "
                    "Quelle angegeben werden."
                )
            )

        return self


class TokenUsage(DomainModel):
    """Einheitliches Format für Tokenmesswerte aller Provider."""

    input_tokens: IntegerMetric = Field(
        default_factory=IntegerMetric
    )
    output_tokens: IntegerMetric = Field(
        default_factory=IntegerMetric
    )
    reasoning_tokens: IntegerMetric = Field(
        default_factory=IntegerMetric
    )
    cached_input_tokens: IntegerMetric = Field(
        default_factory=IntegerMetric
    )
    cache_write_tokens: IntegerMetric = Field(
        default_factory=IntegerMetric
    )
    total_tokens: IntegerMetric = Field(
        default_factory=IntegerMetric
    )


class ProviderTiming(DomainModel):
    """Optionale, vom Provider gelieferte Zeitmesswerte."""

    queue_duration_ms: FloatMetric = Field(
        default_factory=lambda: FloatMetric(
            unit="ms"
        )
    )
    load_duration_ms: FloatMetric = Field(
        default_factory=lambda: FloatMetric(
            unit="ms"
        )
    )
    prompt_evaluation_duration_ms: FloatMetric = Field(
        default_factory=lambda: FloatMetric(
            unit="ms"
        )
    )
    generation_duration_ms: FloatMetric = Field(
        default_factory=lambda: FloatMetric(
            unit="ms"
        )
    )
    time_to_first_token_ms: FloatMetric = Field(
        default_factory=lambda: FloatMetric(
            unit="ms"
        )
    )
    execution_duration_ms: FloatMetric = Field(
        default_factory=lambda: FloatMetric(
            unit="ms"
        )
    )

class PerformanceMetrics(DomainModel):
    """Technische Messwerte eines einzelnen Modellaufrufs."""

    started_at: datetime
    finished_at: datetime

    total_duration_ms: FloatMetric
    tokens_per_second: FloatMetric = Field(
        default_factory=lambda: FloatMetric(
            unit="tokens/s"
        )
    )

    token_usage: TokenUsage = Field(
        default_factory=TokenUsage
    )
    provider_timing: ProviderTiming = Field(
        default_factory=ProviderTiming
    )

    retry_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_timestamps(
        self,
    ) -> PerformanceMetrics:
        if self.started_at.tzinfo is None:
            raise ValueError(
                "started_at benötigt eine Zeitzone."
            )

        if self.finished_at.tzinfo is None:
            raise ValueError(
                "finished_at benötigt eine Zeitzone."
            )

        if self.finished_at < self.started_at:
            raise ValueError(
                "finished_at darf nicht vor started_at liegen."
            )

        return self


class AnalysisRunRecord(DomainModel):
    """Protokoll eines Modell- und Analyseaufrufs."""

    run_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(
            timezone.utc
        )
    )

    provider_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)

    requested_model_name: str = Field(min_length=1)
    actual_model_name: str | None = None

    prompt_version: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)

    success: bool
    status: str | None = None

    metrics: PerformanceMetrics

    error_type: ErrorType | None = None
    error_message: str | None = None

    provider_request_id: str | None = None
    provider_metadata: dict[str, Any] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def validate_result_state(
        self,
    ) -> AnalysisRunRecord:
        if self.success and self.error_type is not None:
            raise ValueError(
                (
                    "Ein erfolgreicher Aufruf darf keinen "
                    "error_type besitzen."
                )
            )

        if not self.success and self.error_type is None:
            raise ValueError(
                (
                    "Ein fehlgeschlagener Aufruf benötigt einen "
                    "error_type."
                )
            )

        return self