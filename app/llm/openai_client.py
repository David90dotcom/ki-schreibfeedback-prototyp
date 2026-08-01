from __future__ import annotations

from time import perf_counter
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    OpenAIError,
    PermissionDeniedError,
    RateLimitError,
)

from app.domain.metrics import (
    FloatMetric,
    IntegerMetric,
    MetricSource,
    ProviderTiming,
    TokenUsage,
)
from app.domain.model_catalog import ModelParameters
from app.llm.base import (
    DiscoveredModel,
    LLMResponse,
    ModelRequest,
    ModelResponse,
    ProviderHealthResult,
)
from app.llm.errors import (
    ProviderAuthenticationError,
    ProviderAuthorizationError,
    ProviderConnectionError,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderInvalidResponseError,
    ProviderModelNotFoundError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderUnknownError,
    provider_error_from_http_status,
)


DEFAULT_OPENAI_INSTRUCTIONS = """
Du bist ein hilfreiches Assistenzsystem für lernförderliches
Schreibfeedback im Deutschunterricht.

Deine Rückmeldungen sind verständlich, konkret, wertschätzend,
textbezogen und überarbeitungsorientiert.
""".strip()


class OpenAIProvider:
    """Adapter für Modelle der OpenAI Responses API."""

    provider_id = "openai"
    provider_name = "openai"

    def __init__(
        self,
        api_key: str | None,
        model_name: str,
        timeout_seconds: float = 240.0,
    ) -> None:
        self.model_name = model_name
        self.timeout_seconds = timeout_seconds

        self._client: AsyncOpenAI | None = None

        if api_key:
            self._client = AsyncOpenAI(
                api_key=api_key,
                timeout=timeout_seconds,
                max_retries=0,
            )

    async def generate(self, prompt: str) -> LLMResponse:
        """
        Übergangskompatibilität mit Version 0.1.

        Die bisherige Oberfläche kann diese Methode weiterhin verwenden.
        """

        request = ModelRequest(
            model_name=self.model_name,
            instructions=DEFAULT_OPENAI_INSTRUCTIONS,
            input_text=prompt,
            parameters=ModelParameters(
                temperature=None,
                reasoning_effort=None,
            ),
            stream=False,
        )

        response = await self.complete(request)

        return LLMResponse(
            provider=self.provider_name,
            model=response.actual_model_name,
            text=response.text,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Führt einen Auftrag über die OpenAI Responses API aus."""

        client = self._require_client(request.model_name)
        payload = self._build_payload(request)

        try:
            if request.stream:
                return await self._complete_streaming(
                    client=client,
                    request=request,
                    payload=payload,
                )

            return await self._complete_non_streaming(
                client=client,
                request=request,
                payload=payload,
            )

        except ProviderError:
            raise

        except OpenAIError as exc:
            raise self._map_openai_error(
                exc=exc,
                model_name=request.model_name,
            ) from exc

        except Exception as exc:
            raise ProviderInvalidResponseError(
                (
                    "Die OpenAI-Antwort konnte nicht vollständig "
                    "verarbeitet werden."
                ),
                provider_id=self.provider_id,
                model_name=request.model_name,
                details={
                    "exception_type": type(exc).__name__,
                },
            ) from exc

    async def _complete_non_streaming(
        self,
        *,
        client: AsyncOpenAI,
        request: ModelRequest,
        payload: dict[str, Any],
    ) -> ModelResponse:
        response = await client.responses.create(
            **payload,
            stream=False,
        )

        return self._build_model_response(
            request=request,
            response=response,
            streamed_text=None,
            time_to_first_token_ms=None,
        )

    async def _complete_streaming(
        self,
        *,
        client: AsyncOpenAI,
        request: ModelRequest,
        payload: dict[str, Any],
    ) -> ModelResponse:
        started_at = perf_counter()
        time_to_first_token_ms: float | None = None

        text_parts: list[str] = []
        final_response: Any | None = None

        stream = await client.responses.create(
            **payload,
            stream=True,
        )

        async with stream:
            async for event in stream:
                event_type = getattr(event, "type", None)

                if event_type == "response.output_text.delta":
                    delta = str(getattr(event, "delta", "") or "")

                    if delta:
                        if time_to_first_token_ms is None:
                            time_to_first_token_ms = round(
                                (perf_counter() - started_at) * 1000,
                                3,
                            )

                        text_parts.append(delta)

                elif event_type in {
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                }:
                    final_response = getattr(
                        event,
                        "response",
                        None,
                    )

        if final_response is None:
            raise ProviderInvalidResponseError(
                (
                    "Der OpenAI-Stream wurde ohne abschließende "
                    "Response beendet."
                ),
                provider_id=self.provider_id,
                model_name=request.model_name,
            )

        streamed_text = "".join(text_parts).strip()

        return self._build_model_response(
            request=request,
            response=final_response,
            streamed_text=streamed_text,
            time_to_first_token_ms=time_to_first_token_ms,
        )

    def _build_payload(
        self,
        request: ModelRequest,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model_name,
            "input": request.input_text,
            "store": False,
        }

        if request.instructions:
            payload["instructions"] = request.instructions

        max_output_tokens = getattr(
            request.parameters,
            "max_output_tokens",
            None,
        )
        temperature = getattr(
            request.parameters,
            "temperature",
            None,
        )
        reasoning_effort = getattr(
            request.parameters,
            "reasoning_effort",
            None,
        )

        if max_output_tokens is not None:
            payload["max_output_tokens"] = max_output_tokens

        if temperature is not None:
            payload["temperature"] = temperature

        if reasoning_effort is not None:
            effort_value = getattr(
                reasoning_effort,
                "value",
                reasoning_effort,
            )

            payload["reasoning"] = {
                "effort": str(effort_value),
            }

        if request.response_schema is not None:
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": self._sanitize_schema_name(
                        request.response_schema_name
                    ),
                    "schema": request.response_schema,
                    "strict": True,
                }
            }

        return payload

    def _build_model_response(
        self,
        *,
        request: ModelRequest,
        response: Any,
        streamed_text: str | None,
        time_to_first_token_ms: float | None,
    ) -> ModelResponse:
        status = str(
            getattr(response, "status", None) or "unknown"
        )

        if status == "failed":
            raise ProviderInvalidResponseError(
                "OpenAI hat den Modellauftrag als fehlgeschlagen beendet.",
                provider_id=self.provider_id,
                model_name=request.model_name,
                details={
                    "response_id": getattr(response, "id", None),
                    "error": self._serializable_value(
                        getattr(response, "error", None)
                    ),
                },
            )

        response_text = str(
            getattr(response, "output_text", "") or ""
        ).strip()

        text = streamed_text or response_text

        if not text:
            raise ProviderInvalidResponseError(
                "OpenAI hat keinen auswertbaren Antworttext geliefert.",
                provider_id=self.provider_id,
                model_name=request.model_name,
                details={
                    "response_id": getattr(response, "id", None),
                    "status": status,
                    "incomplete_details": self._serializable_value(
                        getattr(
                            response,
                            "incomplete_details",
                            None,
                        )
                    ),
                },
            )

        requested_model_name = request.model_name
        actual_model_name = str(
            getattr(response, "model", None)
            or requested_model_name
        )

        return ModelResponse(
            provider_id=self.provider_id,
            requested_model_name=requested_model_name,
            actual_model_name=actual_model_name,
            text=text,
            status=status,
            provider_request_id=getattr(
                response,
                "id",
                None,
            ),
            token_usage=self._build_token_usage(response),
            provider_timing=ProviderTiming(
                time_to_first_token_ms=self._float_metric(
                    time_to_first_token_ms,
                    MetricSource.CLIENT,
                    unit="ms",
                )
            ),
            raw_metadata={
                "created_at": getattr(
                    response,
                    "created_at",
                    None,
                ),
                "status": status,
                "service_tier": getattr(
                    response,
                    "service_tier",
                    None,
                ),
                "incomplete_details": self._serializable_value(
                    getattr(
                        response,
                        "incomplete_details",
                        None,
                    )
                ),
            },
        )

    def _build_token_usage(
        self,
        response: Any,
    ) -> TokenUsage:
        usage = getattr(response, "usage", None)

        if usage is None:
            return TokenUsage()

        input_tokens = self._optional_non_negative_int(
            getattr(usage, "input_tokens", None)
        )
        output_tokens = self._optional_non_negative_int(
            getattr(usage, "output_tokens", None)
        )
        total_tokens = self._optional_non_negative_int(
            getattr(usage, "total_tokens", None)
        )

        input_details = getattr(
            usage,
            "input_tokens_details",
            None,
        )
        output_details = getattr(
            usage,
            "output_tokens_details",
            None,
        )

        cached_input_tokens = self._optional_non_negative_int(
            getattr(input_details, "cached_tokens", None)
        )
        cache_write_tokens = self._optional_non_negative_int(
            getattr(input_details, "cache_write_tokens", None)
        )
        reasoning_tokens = self._optional_non_negative_int(
            getattr(output_details, "reasoning_tokens", None)
        )

        return TokenUsage(
            input_tokens=self._integer_metric(
                input_tokens,
                MetricSource.PROVIDER,
            ),
            output_tokens=self._integer_metric(
                output_tokens,
                MetricSource.PROVIDER,
            ),
            reasoning_tokens=self._integer_metric(
                reasoning_tokens,
                MetricSource.PROVIDER,
            ),
            cached_input_tokens=self._integer_metric(
                cached_input_tokens,
                MetricSource.PROVIDER,
            ),
            cache_write_tokens=self._integer_metric(
                cache_write_tokens,
                MetricSource.PROVIDER,
            ),
            total_tokens=self._integer_metric(
                total_tokens,
                MetricSource.PROVIDER,
            ),
        )

    async def check_health(self) -> ProviderHealthResult:
        """
        Prüft API-Key und Modellzugriff ohne kostenpflichtige Textgenerierung.
        """

        if self._client is None:
            return ProviderHealthResult(
                provider_id=self.provider_id,
                available=False,
                message="OPENAI_API_KEY ist nicht gesetzt.",
            )

        started_at = perf_counter()

        try:
            await self._client.models.retrieve(self.model_name)

            latency_ms = round(
                (perf_counter() - started_at) * 1000,
                3,
            )

            return ProviderHealthResult(
                provider_id=self.provider_id,
                available=True,
                message=(
                    "OpenAI ist erreichbar und das konfigurierte "
                    "Modell ist verfügbar."
                ),
                latency_ms=latency_ms,
            )

        except OpenAIError as exc:
            latency_ms = round(
                (perf_counter() - started_at) * 1000,
                3,
            )

            mapped_error = self._map_openai_error(
                exc=exc,
                model_name=self.model_name,
            )

            return ProviderHealthResult(
                provider_id=self.provider_id,
                available=False,
                message=mapped_error.message,
                latency_ms=latency_ms,
            )

    async def discover_models(self) -> list[DiscoveredModel]:
        """Liest die für den API-Key sichtbaren OpenAI-Modelle aus."""

        client = self._require_client(model_name=None)

        try:
            page = await client.models.list()

        except OpenAIError as exc:
            raise self._map_openai_error(
                exc=exc,
                model_name=None,
            ) from exc

        discovered_models: list[DiscoveredModel] = []

        for raw_model in page.data:
            model_name = str(
                getattr(raw_model, "id", "") or ""
            ).strip()

            if not model_name:
                continue

            discovered_models.append(
                DiscoveredModel(
                    provider_id=self.provider_id,
                    model_name=model_name,
                    display_name=model_name,
                    metadata={
                        "created": getattr(
                            raw_model,
                            "created",
                            None,
                        ),
                        "owned_by": getattr(
                            raw_model,
                            "owned_by",
                            None,
                        ),
                    },
                )
            )

        return discovered_models

    async def close(self) -> None:
        """Schließt die vom OpenAI-Client verwendeten Verbindungen."""

        if self._client is not None:
            await self._client.close()

    def _require_client(
        self,
        model_name: str | None,
    ) -> AsyncOpenAI:
        if self._client is None:
            raise ProviderAuthenticationError(
                (
                    "OPENAI_API_KEY ist nicht gesetzt. "
                    "Prüfe die lokale .env-Datei."
                ),
                provider_id=self.provider_id,
                model_name=model_name,
            )

        return self._client

    def _map_openai_error(
        self,
        *,
        exc: OpenAIError,
        model_name: str | None,
    ) -> ProviderError:
        message = self._openai_error_message(exc)
        details = self._openai_error_details(exc)

        common_arguments = {
            "provider_id": self.provider_id,
            "model_name": model_name,
            "status_code": getattr(
                exc,
                "status_code",
                None,
            ),
            "details": details,
        }

        if isinstance(exc, APITimeoutError):
            return ProviderTimeoutError(
                message,
                **common_arguments,
            )

        if isinstance(exc, AuthenticationError):
            return ProviderAuthenticationError(
                message,
                **common_arguments,
            )

        if isinstance(exc, PermissionDeniedError):
            return ProviderAuthorizationError(
                message,
                **common_arguments,
            )

        if isinstance(exc, RateLimitError):
            return ProviderRateLimitError(
                message,
                **common_arguments,
            )

        if isinstance(exc, NotFoundError):
            return ProviderModelNotFoundError(
                message,
                **common_arguments,
            )

        if isinstance(exc, BadRequestError):
            return ProviderInvalidRequestError(
                message,
                **common_arguments,
            )

        if isinstance(exc, InternalServerError):
            return ProviderUnavailableError(
                message,
                **common_arguments,
            )

        if isinstance(exc, APIConnectionError):
            return ProviderConnectionError(
                message,
                **common_arguments,
            )

        if isinstance(exc, APIStatusError):
            status_code = getattr(
                exc,
                "status_code",
                None,
            )

            if isinstance(status_code, int):
                return provider_error_from_http_status(
                    provider_id=self.provider_id,
                    model_name=model_name,
                    status_code=status_code,
                    message=message,
                    details=details,
                )

        return ProviderUnknownError(
            message,
            **common_arguments,
        )

    def _openai_error_message(
        self,
        exc: OpenAIError,
    ) -> str:
        provider_message = str(
            getattr(exc, "message", None)
            or str(exc)
            or type(exc).__name__
        ).strip()

        if len(provider_message) > 2000:
            provider_message = provider_message[:2000]

        return f"OpenAI-Fehler: {provider_message}"

    @staticmethod
    def _openai_error_details(
        exc: OpenAIError,
    ) -> dict[str, Any]:
        details: dict[str, Any] = {
            "exception_type": type(exc).__name__,
        }

        request_id = getattr(exc, "request_id", None)

        if request_id:
            details["request_id"] = request_id

        body = getattr(exc, "body", None)

        if isinstance(body, dict):
            for key in ("type", "code", "param"):
                value = body.get(key)

                if value is not None:
                    details[key] = value

        return details

    @staticmethod
    def _optional_non_negative_int(
        value: Any,
    ) -> int | None:
        if isinstance(value, bool):
            return None

        if isinstance(value, int) and value >= 0:
            return value

        return None

    @staticmethod
    def _integer_metric(
        value: int | None,
        source: MetricSource,
    ) -> IntegerMetric:
        if value is None:
            return IntegerMetric()

        return IntegerMetric(
            value=value,
            source=source,
        )

    @staticmethod
    def _float_metric(
        value: float | None,
        source: MetricSource,
        *,
        unit: str,
    ) -> FloatMetric:
        if value is None:
            return FloatMetric(unit=unit)

        return FloatMetric(
            value=value,
            source=source,
            unit=unit,
        )

    @staticmethod
    def _sanitize_schema_name(name: str) -> str:
        sanitized_characters: list[str] = []

        for character in name:
            if character.isalnum() or character in {"_", "-"}:
                sanitized_characters.append(character)
            else:
                sanitized_characters.append("_")

        sanitized_name = "".join(sanitized_characters).strip("_")

        if not sanitized_name:
            sanitized_name = "structured_response"

        return sanitized_name[:64]

    @staticmethod
    def _serializable_value(value: Any) -> Any:
        if value is None:
            return None

        model_dump = getattr(value, "model_dump", None)

        if callable(model_dump):
            return model_dump()

        if isinstance(
            value,
            (str, int, float, bool, list, dict),
        ):
            return value

        return str(value)