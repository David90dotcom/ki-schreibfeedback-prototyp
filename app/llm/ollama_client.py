from __future__ import annotations

import json
from time import perf_counter
from typing import Any

import httpx

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
    ProviderConnectionError,
    ProviderError,
    ProviderInvalidResponseError,
    ProviderTimeoutError,
    provider_error_from_http_status,
)


class OllamaProvider:
    """Adapter für lokale Ollama-Modelle."""

    provider_id = "ollama"
    provider_name = "ollama"

    def __init__(
        self,
        base_url: str,
        model_name: str,
        timeout_seconds: float = 240.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.timeout_seconds = timeout_seconds

    async def generate(self, prompt: str) -> LLMResponse:
        """
        Übergangskompatibilität mit Version 0.1.

        Die bisherige Oberfläche kann diese Methode weiterhin unverändert
        verwenden.
        """

        request = ModelRequest(
            model_name=self.model_name,
            input_text=prompt,
            parameters=ModelParameters(
                temperature=0.2,
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
        """Führt einen Modellauftrag über die Ollama-API aus."""

        payload = self._build_payload(request)

        try:
            if request.stream:
                return await self._complete_streaming(
                    request=request,
                    payload=payload,
                )

            return await self._complete_non_streaming(
                request=request,
                payload=payload,
            )

        except ProviderError:
            raise

        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                "Der Ollama-Aufruf hat das Zeitlimit überschritten.",
                provider_id=self.provider_id,
                model_name=request.model_name,
            ) from exc

        except httpx.RequestError as exc:
            raise ProviderConnectionError(
                (
                    "Ollama konnte nicht erreicht werden. "
                    "Prüfe, ob Ollama lokal gestartet ist."
                ),
                provider_id=self.provider_id,
                model_name=request.model_name,
                details={
                    "exception_type": type(exc).__name__,
                },
            ) from exc

        except Exception as exc:
            raise ProviderInvalidResponseError(
                (
                    "Die Antwort von Ollama konnte nicht vollständig "
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
        request: ModelRequest,
        payload: dict[str, Any],
    ) -> ModelResponse:
        url = f"{self.base_url}/api/generate"

        async with httpx.AsyncClient(
            timeout=self.timeout_seconds
        ) as client:
            response = await client.post(
                url,
                json=payload,
            )

        await self._raise_for_http_error(
            response=response,
            model_name=request.model_name,
        )

        data = self._decode_json_response(
            response=response,
            model_name=request.model_name,
        )

        text = str(data.get("response") or "").strip()

        return self._build_model_response(
            request=request,
            data=data,
            text=text,
            time_to_first_token_ms=None,
        )

    async def _complete_streaming(
        self,
        *,
        request: ModelRequest,
        payload: dict[str, Any],
    ) -> ModelResponse:
        url = f"{self.base_url}/api/generate"

        started_at = perf_counter()
        time_to_first_token_ms: float | None = None

        text_parts: list[str] = []
        final_data: dict[str, Any] = {}

        async with httpx.AsyncClient(
            timeout=self.timeout_seconds
        ) as client:
            async with client.stream(
                "POST",
                url,
                json=payload,
            ) as response:
                await self._raise_for_http_error(
                    response=response,
                    model_name=request.model_name,
                )

                async for line in response.aiter_lines():
                    cleaned_line = line.strip()

                    if not cleaned_line:
                        continue

                    try:
                        chunk = json.loads(cleaned_line)
                    except json.JSONDecodeError as exc:
                        raise ProviderInvalidResponseError(
                            (
                                "Ollama hat während des Streamings "
                                "ungültiges JSON geliefert."
                            ),
                            provider_id=self.provider_id,
                            model_name=request.model_name,
                        ) from exc

                    if not isinstance(chunk, dict):
                        raise ProviderInvalidResponseError(
                            (
                                "Ollama hat während des Streamings ein "
                                "unerwartetes Datenformat geliefert."
                            ),
                            provider_id=self.provider_id,
                            model_name=request.model_name,
                        )

                    if chunk.get("error"):
                        raise ProviderInvalidResponseError(
                            f"Ollama-Fehler: {chunk['error']}",
                            provider_id=self.provider_id,
                            model_name=request.model_name,
                        )

                    text_part = str(chunk.get("response") or "")

                    if text_part:
                        if time_to_first_token_ms is None:
                            time_to_first_token_ms = round(
                                (perf_counter() - started_at) * 1000,
                                3,
                            )

                        text_parts.append(text_part)

                    final_data = chunk

        text = "".join(text_parts).strip()

        return self._build_model_response(
            request=request,
            data=final_data,
            text=text,
            time_to_first_token_ms=time_to_first_token_ms,
        )

    def _build_payload(
        self,
        request: ModelRequest,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model_name,
            "prompt": request.input_text,
            "stream": request.stream,
        }

        if request.instructions:
            payload["system"] = request.instructions

        if request.response_schema is not None:
            payload["format"] = request.response_schema

        options: dict[str, Any] = {}

        temperature = getattr(
            request.parameters,
            "temperature",
            None,
        )
        max_output_tokens = getattr(
            request.parameters,
            "max_output_tokens",
            None,
        )
        seed = getattr(
            request.parameters,
            "seed",
            None,
        )

        if temperature is not None:
            options["temperature"] = temperature

        if max_output_tokens is not None:
            options["num_predict"] = max_output_tokens

        if seed is not None:
            options["seed"] = seed

        if options:
            payload["options"] = options

        return payload

    def _build_model_response(
        self,
        *,
        request: ModelRequest,
        data: dict[str, Any],
        text: str,
        time_to_first_token_ms: float | None,
    ) -> ModelResponse:
        if not text:
            raise ProviderInvalidResponseError(
                "Ollama hat keinen auswertbaren Antworttext geliefert.",
                provider_id=self.provider_id,
                model_name=request.model_name,
                details={
                    "done": data.get("done"),
                    "done_reason": data.get("done_reason"),
                },
            )

        actual_model_name = str(
            data.get("model") or request.model_name
        )

        token_usage = self._build_token_usage(data)
        provider_timing = self._build_provider_timing(
            data=data,
            time_to_first_token_ms=time_to_first_token_ms,
        )

        status = (
            "completed"
            if data.get("done", True)
            else "incomplete"
        )

        return ModelResponse(
            provider_id=self.provider_id,
            requested_model_name=request.model_name,
            actual_model_name=actual_model_name,
            text=text,
            status=status,
            provider_request_id=None,
            token_usage=token_usage,
            provider_timing=provider_timing,
            raw_metadata={
                "created_at": data.get("created_at"),
                "done": data.get("done"),
                "finish_reason": data.get("done_reason"),
                "total_duration_ns": data.get("total_duration"),
                "load_duration_ns": data.get("load_duration"),
                "prompt_eval_duration_ns": data.get(
                    "prompt_eval_duration"
                ),
                "eval_duration_ns": data.get("eval_duration"),
            },
        )

    def _build_token_usage(
        self,
        data: dict[str, Any],
    ) -> TokenUsage:
        input_tokens = self._optional_non_negative_int(
            data.get("prompt_eval_count")
        )
        output_tokens = self._optional_non_negative_int(
            data.get("eval_count")
        )

        total_tokens: int | None = None

        if input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens

        return TokenUsage(
            input_tokens=self._integer_metric(
                input_tokens,
                MetricSource.PROVIDER,
            ),
            output_tokens=self._integer_metric(
                output_tokens,
                MetricSource.PROVIDER,
            ),
            total_tokens=self._integer_metric(
                total_tokens,
                MetricSource.CALCULATED,
            ),
        )

    def _build_provider_timing(
        self,
        *,
        data: dict[str, Any],
        time_to_first_token_ms: float | None,
    ) -> ProviderTiming:
        return ProviderTiming(
            load_duration_ms=self._nanoseconds_metric(
                data.get("load_duration")
            ),
            prompt_evaluation_duration_ms=self._nanoseconds_metric(
                data.get("prompt_eval_duration")
            ),
            generation_duration_ms=self._nanoseconds_metric(
                data.get("eval_duration")
            ),
            time_to_first_token_ms=self._float_metric(
                time_to_first_token_ms,
                MetricSource.CLIENT,
                unit="ms",
            ),
        )

    async def check_health(self) -> ProviderHealthResult:
        """Prüft, ob der lokale Ollama-Dienst erreichbar ist."""

        url = f"{self.base_url}/api/tags"
        started_at = perf_counter()

        try:
            async with httpx.AsyncClient(
                timeout=min(self.timeout_seconds, 10.0)
            ) as client:
                response = await client.get(url)

            latency_ms = round(
                (perf_counter() - started_at) * 1000,
                3,
            )

            if response.status_code >= 400:
                return ProviderHealthResult(
                    provider_id=self.provider_id,
                    available=False,
                    message=(
                        "Ollama antwortet mit HTTP-Status "
                        f"{response.status_code}."
                    ),
                    latency_ms=latency_ms,
                )

            return ProviderHealthResult(
                provider_id=self.provider_id,
                available=True,
                message="Ollama ist erreichbar.",
                latency_ms=latency_ms,
            )

        except httpx.RequestError as exc:
            latency_ms = round(
                (perf_counter() - started_at) * 1000,
                3,
            )

            return ProviderHealthResult(
                provider_id=self.provider_id,
                available=False,
                message=(
                    "Ollama ist nicht erreichbar: "
                    f"{type(exc).__name__}"
                ),
                latency_ms=latency_ms,
            )

    async def discover_models(self) -> list[DiscoveredModel]:
        """Liest die lokal in Ollama verfügbaren Modelle aus."""

        url = f"{self.base_url}/api/tags"

        try:
            async with httpx.AsyncClient(
                timeout=min(self.timeout_seconds, 30.0)
            ) as client:
                response = await client.get(url)

        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                "Die Ollama-Modellliste konnte nicht rechtzeitig geladen werden.",
                provider_id=self.provider_id,
            ) from exc

        except httpx.RequestError as exc:
            raise ProviderConnectionError(
                "Die Ollama-Modellliste konnte nicht geladen werden.",
                provider_id=self.provider_id,
            ) from exc

        await self._raise_for_http_error(
            response=response,
            model_name=None,
        )

        data = self._decode_json_response(
            response=response,
            model_name=None,
        )

        raw_models = data.get("models", [])

        if not isinstance(raw_models, list):
            raise ProviderInvalidResponseError(
                "Ollama hat eine ungültige Modellliste geliefert.",
                provider_id=self.provider_id,
            )

        discovered_models: list[DiscoveredModel] = []

        for raw_model in raw_models:
            if not isinstance(raw_model, dict):
                continue

            model_name = str(
                raw_model.get("name")
                or raw_model.get("model")
                or ""
            ).strip()

            if not model_name:
                continue

            discovered_models.append(
                DiscoveredModel(
                    provider_id=self.provider_id,
                    model_name=model_name,
                    display_name=model_name,
                    metadata={
                        "modified_at": raw_model.get("modified_at"),
                        "size": raw_model.get("size"),
                        "digest": raw_model.get("digest"),
                        "details": raw_model.get("details"),
                    },
                )
            )

        return discovered_models

    async def _raise_for_http_error(
        self,
        *,
        response: httpx.Response,
        model_name: str | None,
    ) -> None:
        if response.status_code < 400:
            return

        await response.aread()

        message, details = self._extract_error_information(response)

        raise provider_error_from_http_status(
            provider_id=self.provider_id,
            model_name=model_name,
            status_code=response.status_code,
            message=message,
            details=details,
        )

    def _decode_json_response(
        self,
        *,
        response: httpx.Response,
        model_name: str | None,
    ) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderInvalidResponseError(
                "Ollama hat keine gültige JSON-Antwort geliefert.",
                provider_id=self.provider_id,
                model_name=model_name,
                status_code=response.status_code,
            ) from exc

        if not isinstance(data, dict):
            raise ProviderInvalidResponseError(
                "Ollama hat ein unerwartetes Antwortformat geliefert.",
                provider_id=self.provider_id,
                model_name=model_name,
                status_code=response.status_code,
            )

        return data

    def _extract_error_information(
        self,
        response: httpx.Response,
    ) -> tuple[str, dict[str, Any]]:
        response_text = response.text.strip()
        provider_message = response_text

        try:
            data = response.json()

            if isinstance(data, dict) and data.get("error"):
                provider_message = str(data["error"])
        except ValueError:
            pass

        if not provider_message:
            provider_message = "Keine weitere Fehlermeldung verfügbar."

        message = (
            f"Ollama-Fehler {response.status_code}: "
            f"{provider_message}"
        )

        return message, {
            "response_body": response_text[:2000],
        }

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

    def _nanoseconds_metric(
        self,
        value: Any,
    ) -> FloatMetric:
        nanoseconds = self._optional_non_negative_int(value)

        if nanoseconds is None:
            return FloatMetric(unit="ms")

        milliseconds = round(
            nanoseconds / 1_000_000,
            3,
        )

        return FloatMetric(
            value=milliseconds,
            source=MetricSource.PROVIDER,
            unit="ms",
        )