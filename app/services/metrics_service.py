from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Callable, Generic, TypeVar

from app.domain.metrics import (
    AnalysisRunRecord,
    FloatMetric,
    MetricSource,
    PerformanceMetrics,
    ProviderTiming,
    TokenUsage,
)
from app.llm.base import ModelProvider, ModelRequest, ModelResponse
from app.llm.errors import (
    ProviderError,
    ProviderInvalidResponseError,
    ProviderUnknownError,
)


ValidatedOutput = TypeVar("ValidatedOutput")

ResponseValidator = Callable[
    [ModelResponse],
    ValidatedOutput,
]


@dataclass(frozen=True)
class ModelExecutionResult(Generic[ValidatedOutput]):
    """Ergebnis eines gemessenen und validierten Modellaufrufs."""

    response: ModelResponse | None
    validated_output: ValidatedOutput | None
    run_record: AnalysisRunRecord
    error: ProviderError | None

    @property
    def success(self) -> bool:
        return self.run_record.success

    def require_output(self) -> ValidatedOutput:
        """
        Liefert das validierte Ergebnis oder löst den erfassten Fehler aus.
        """

        if self.error is not None:
            raise self.error

        if self.validated_output is None:
            raise RuntimeError(
                "Der Modellaufruf enthält kein validiertes Ergebnis."
            )

        return self.validated_output


class MetricsService:
    """
    Misst und protokolliert einen vollständigen Modellaufruf.

    Die Messdauer umfasst:

    1. Provideraufruf
    2. Empfang der Modellantwort
    3. Validierung der Antwort
    """

    async def execute(
        self,
        *,
        provider: ModelProvider,
        request: ModelRequest,
        model_id: str,
        prompt_version: str,
        schema_version: str,
        validator: ResponseValidator[ValidatedOutput],
        retry_count: int = 0,
    ) -> ModelExecutionResult[ValidatedOutput]:
        started_at = datetime.now(timezone.utc)
        started_counter = perf_counter()

        response: ModelResponse | None = None

        try:
            response = await provider.complete(request)

            self._validate_response_identity(
                provider=provider,
                request=request,
                response=response,
            )

            validated_output = validator(response)

            finished_at = datetime.now(timezone.utc)
            total_duration_ms = self._elapsed_milliseconds(
                started_counter
            )

            metrics = self._build_metrics(
                started_at=started_at,
                finished_at=finished_at,
                total_duration_ms=total_duration_ms,
                response=response,
                retry_count=retry_count,
            )

            run_record = AnalysisRunRecord(
                provider_id=response.provider_id,
                model_id=model_id,
                requested_model_name=request.model_name,
                actual_model_name=response.actual_model_name,
                prompt_version=prompt_version,
                schema_version=schema_version,
                success=True,
                status=response.status or "completed",
                metrics=metrics,
                error_type=None,
                error_message=None,
                provider_request_id=response.provider_request_id,
                provider_metadata=response.raw_metadata,
            )

            return ModelExecutionResult(
                response=response,
                validated_output=validated_output,
                run_record=run_record,
                error=None,
            )

        except ProviderError as exc:
            return self._build_failure_result(
                error=exc,
                response=response,
                provider=provider,
                request=request,
                model_id=model_id,
                prompt_version=prompt_version,
                schema_version=schema_version,
                retry_count=retry_count,
                started_at=started_at,
                started_counter=started_counter,
            )

        except Exception as exc:
            normalized_error = ProviderUnknownError(
                (
                    "Während des Modellaufrufs oder der "
                    "Antwortvalidierung ist ein unerwarteter Fehler "
                    "aufgetreten."
                ),
                provider_id=provider.provider_id,
                model_name=request.model_name,
                details={
                    "exception_type": type(exc).__name__,
                },
            )

            return self._build_failure_result(
                error=normalized_error,
                response=response,
                provider=provider,
                request=request,
                model_id=model_id,
                prompt_version=prompt_version,
                schema_version=schema_version,
                retry_count=retry_count,
                started_at=started_at,
                started_counter=started_counter,
            )

    def _build_failure_result(
        self,
        *,
        error: ProviderError,
        response: ModelResponse | None,
        provider: ModelProvider,
        request: ModelRequest,
        model_id: str,
        prompt_version: str,
        schema_version: str,
        retry_count: int,
        started_at: datetime,
        started_counter: float,
    ) -> ModelExecutionResult[ValidatedOutput]:
        finished_at = datetime.now(timezone.utc)
        total_duration_ms = self._elapsed_milliseconds(
            started_counter
        )

        metrics = self._build_metrics(
            started_at=started_at,
            finished_at=finished_at,
            total_duration_ms=total_duration_ms,
            response=response,
            retry_count=retry_count,
        )

        provider_request_id: str | None = None
        actual_model_name: str | None = None

        if response is not None:
            provider_request_id = response.provider_request_id
            actual_model_name = response.actual_model_name
        else:
            request_id = error.details.get("request_id")

            if isinstance(request_id, str):
                provider_request_id = request_id

        provider_metadata = {
            "error": {
                "retryable": error.retryable,
                "status_code": error.status_code,
                "details": error.details,
            }
        }

        if response is not None:
            provider_metadata["response"] = response.raw_metadata

        status = "failed"

        if error.error_type.value == "structured_output":
            status = "validation_failed"

        run_record = AnalysisRunRecord(
            provider_id=provider.provider_id,
            model_id=model_id,
            requested_model_name=request.model_name,
            actual_model_name=actual_model_name,
            prompt_version=prompt_version,
            schema_version=schema_version,
            success=False,
            status=status,
            metrics=metrics,
            error_type=error.error_type,
            error_message=error.message,
            provider_request_id=provider_request_id,
            provider_metadata=provider_metadata,
        )

        return ModelExecutionResult(
            response=response,
            validated_output=None,
            run_record=run_record,
            error=error,
        )

    def _validate_response_identity(
        self,
        *,
        provider: ModelProvider,
        request: ModelRequest,
        response: ModelResponse,
    ) -> None:
        if response.provider_id != provider.provider_id:
            raise ProviderInvalidResponseError(
                (
                    "Die Provider-ID der Modellantwort stimmt nicht "
                    "mit dem aufgerufenen Provider überein."
                ),
                provider_id=provider.provider_id,
                model_name=request.model_name,
                details={
                    "response_provider_id": response.provider_id,
                },
            )

        if (
            response.requested_model_name
            != request.model_name
        ):
            raise ProviderInvalidResponseError(
                (
                    "Der Modellname der Antwort stimmt nicht mit dem "
                    "angefragten Modell überein."
                ),
                provider_id=provider.provider_id,
                model_name=request.model_name,
                details={
                    "response_requested_model_name": (
                        response.requested_model_name
                    ),
                },
            )

        if response.status in {
            "failed",
            "cancelled",
            "incomplete",
        }:
            raise ProviderInvalidResponseError(
                (
                    "Der Provider hat den Modellauftrag nicht "
                    "vollständig abgeschlossen."
                ),
                provider_id=provider.provider_id,
                model_name=request.model_name,
                details={
                    "response_status": response.status,
                    "provider_request_id": (
                        response.provider_request_id
                    ),
                },
            )

    def _build_metrics(
        self,
        *,
        started_at: datetime,
        finished_at: datetime,
        total_duration_ms: float,
        response: ModelResponse | None,
        retry_count: int,
    ) -> PerformanceMetrics:
        if response is None:
            token_usage = TokenUsage()
            provider_timing = ProviderTiming()
        else:
            token_usage = response.token_usage
            provider_timing = response.provider_timing

        tokens_per_second = self._calculate_tokens_per_second(
            token_usage=token_usage,
            provider_timing=provider_timing,
            total_duration_ms=total_duration_ms,
        )

        return PerformanceMetrics(
            started_at=started_at,
            finished_at=finished_at,
            total_duration_ms=FloatMetric(
                value=total_duration_ms,
                source=MetricSource.CLIENT,
                unit="ms",
            ),
            tokens_per_second=tokens_per_second,
            token_usage=token_usage,
            provider_timing=provider_timing,
            retry_count=retry_count,
        )

    @staticmethod
    def _calculate_tokens_per_second(
        *,
        token_usage: TokenUsage,
        provider_timing: ProviderTiming,
        total_duration_ms: float,
    ) -> FloatMetric:
        output_tokens = token_usage.output_tokens.value

        if output_tokens is None:
            return FloatMetric(unit="tokens/s")

        generation_duration_ms = (
            provider_timing.generation_duration_ms.value
        )

        if (
            generation_duration_ms is not None
            and generation_duration_ms > 0
        ):
            value = round(
                output_tokens
                / (generation_duration_ms / 1000),
                3,
            )

            return FloatMetric(
                value=value,
                source=MetricSource.CALCULATED,
                unit="tokens/s",
            )

        if total_duration_ms > 0:
            value = round(
                output_tokens
                / (total_duration_ms / 1000),
                3,
            )

            return FloatMetric(
                value=value,
                source=MetricSource.ESTIMATED,
                unit="tokens/s",
            )

        return FloatMetric(unit="tokens/s")

    @staticmethod
    def _elapsed_milliseconds(
        started_counter: float,
    ) -> float:
        return round(
            (perf_counter() - started_counter) * 1000,
            3,
        )