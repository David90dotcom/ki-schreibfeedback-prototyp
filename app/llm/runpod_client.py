from __future__ import annotations

import asyncio
from time import perf_counter
from typing import Any
from urllib.parse import quote

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
    LLMResponse,
    ModelRequest,
    ModelResponse,
    ProviderHealthResult,
)
from app.llm.errors import (
    ProviderAuthenticationError,
    ProviderCancelledError,
    ProviderConnectionError,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderInvalidResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    provider_error_from_http_status,
)


RUNPOD_API_BASE_URL = "https://api.runpod.ai/v2"
ACTIVE_JOB_STATUSES = {"IN_QUEUE", "IN_PROGRESS", "RUNNING"}


class RunPodProvider:
    """Adapter für einen RunPod-Serverless-Queue-Endpunkt."""

    provider_id = "runpod"
    provider_name = "runpod"

    def __init__(
        self,
        *,
        api_key: str | None,
        endpoint_id: str | None,
        model_name: str,
        job_timeout_seconds: float = 900.0,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self.api_key = api_key.strip() if api_key else None
        self.endpoint_id = endpoint_id.strip() if endpoint_id else None
        self.model_name = model_name.strip()
        self.job_timeout_seconds = float(job_timeout_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)

        if not self.model_name:
            raise ValueError("Der RunPod-Modellname darf nicht leer sein.")
        if self.job_timeout_seconds <= 0:
            raise ValueError(
                "RUNPOD_JOB_TIMEOUT_SECONDS muss größer als 0 sein."
            )
        if self.poll_interval_seconds <= 0:
            raise ValueError(
                "RUNPOD_POLL_INTERVAL_SECONDS muss größer als 0 sein."
            )

    async def generate(self, prompt: str) -> LLMResponse:
        """Unterstützt den derzeitigen Webapp-Datenfluss."""

        response = await self.complete(
            ModelRequest(
                model_name=self.model_name,
                input_text=prompt,
                parameters=ModelParameters(
                    temperature=0.15,
                    max_output_tokens=4000,
                ),
            )
        )

        return LLMResponse(
            provider=self.provider_name,
            model=response.actual_model_name,
            text=response.text,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Sendet einen Queue-Job und wartet auf dessen Ergebnis."""

        self._validate_configuration(request.model_name)

        if request.stream:
            raise ProviderInvalidRequestError(
                "RunPod-Streaming ist in Version 0.3a nicht aktiviert.",
                provider_id=self.provider_id,
                model_name=request.model_name,
            )

        deadline = (
            asyncio.get_running_loop().time()
            + self.job_timeout_seconds
        )

        async with self._create_http_client() as client:
            submitted = await self._request_json(
                client=client,
                method="POST",
                url=self._url("run"),
                model_name=request.model_name,
                operation="run",
                json_body={"input": self._worker_input(request)},
            )

            job_id = submitted.get("id")
            if not isinstance(job_id, str) or not job_id.strip():
                raise self._invalid_response(
                    "Die RunPod-Antwort enthält keine Job-ID.",
                    request.model_name,
                    "run",
                )

            completed = await self._wait_for_job(
                client=client,
                job_id=job_id.strip(),
                payload=submitted,
                model_name=request.model_name,
                deadline=deadline,
            )

        return self._model_response(request, job_id.strip(), completed)

    async def check_health(self) -> ProviderHealthResult:
        """Prüft den Endpunkt, ohne einen Modelljob auszulösen."""

        if not self.api_key or not self.endpoint_id:
            return ProviderHealthResult(
                provider_id=self.provider_id,
                available=False,
                message=(
                    "RUNPOD_API_KEY und RUNPOD_ENDPOINT_ID "
                    "müssen gesetzt sein."
                ),
            )

        started_at = perf_counter()

        try:
            async with self._create_http_client() as client:
                await self._request_json(
                    client=client,
                    method="GET",
                    url=self._url("health"),
                    model_name=self.model_name,
                    operation="health",
                )
        except ProviderError as exc:
            return ProviderHealthResult(
                provider_id=self.provider_id,
                available=False,
                message=exc.message,
                latency_ms=self._elapsed_ms(started_at),
            )

        return ProviderHealthResult(
            provider_id=self.provider_id,
            available=True,
            message="RunPod-Endpunkt ist erreichbar.",
            latency_ms=self._elapsed_ms(started_at),
        )

    def _validate_configuration(self, model_name: str) -> None:
        if not self.api_key:
            raise ProviderAuthenticationError(
                (
                    "Kein RunPod-API-Key verfügbar. Hinterlege "
                    "RUNPOD_API_KEY in der .env-Datei."
                ),
                provider_id=self.provider_id,
                model_name=model_name,
            )
        if not self.endpoint_id:
            raise ProviderInvalidRequestError(
                (
                    "Keine RunPod-Endpoint-ID verfügbar. Hinterlege "
                    "RUNPOD_ENDPOINT_ID in der .env-Datei."
                ),
                provider_id=self.provider_id,
                model_name=model_name,
            )

    def _create_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=min(30.0, self.job_timeout_seconds),
            trust_env=False,
        )

    def _url(self, operation: str, job_id: str | None = None) -> str:
        endpoint_id = quote(self.endpoint_id or "", safe="")
        url = f"{RUNPOD_API_BASE_URL}/{endpoint_id}/{operation}"

        if job_id is not None:
            url = f"{url}/{quote(job_id, safe='')}"

        return url

    @staticmethod
    def _worker_input(request: ModelRequest) -> dict[str, Any]:
        worker_input: dict[str, Any] = {
            "model": request.model_name,
            "prompt": request.input_text,
            "stream": False,
        }
        options: dict[str, Any] = {}

        if request.instructions:
            worker_input["system"] = request.instructions
        if request.parameters.temperature is not None:
            options["temperature"] = request.parameters.temperature
        if request.parameters.max_output_tokens is not None:
            options["num_predict"] = request.parameters.max_output_tokens
        if request.parameters.seed is not None:
            options["seed"] = request.parameters.seed
        if options:
            worker_input["options"] = options
        if request.response_schema is not None:
            worker_input["format"] = request.response_schema

        return worker_input

    async def _wait_for_job(
        self,
        *,
        client: httpx.AsyncClient,
        job_id: str,
        payload: dict[str, Any],
        model_name: str,
        deadline: float,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()

        while True:
            raw_status = payload.get("status")
            if not isinstance(raw_status, str) or not raw_status.strip():
                raise self._invalid_response(
                    "Die RunPod-Antwort enthält keinen Jobstatus.",
                    model_name,
                    "status",
                )

            status = raw_status.strip().upper()

            if status == "COMPLETED":
                return payload
            if status == "FAILED":
                raise ProviderUnavailableError(
                    "Der RunPod-Worker konnte den Auftrag nicht ausführen.",
                    provider_id=self.provider_id,
                    model_name=model_name,
                    details={"request_id": job_id, "job_status": status},
                )
            if status == "CANCELLED":
                raise ProviderCancelledError(
                    "Der RunPod-Auftrag wurde abgebrochen.",
                    provider_id=self.provider_id,
                    model_name=model_name,
                    details={"request_id": job_id, "job_status": status},
                )
            if status == "TIMED_OUT":
                raise ProviderTimeoutError(
                    "Der RunPod-Worker hat das Zeitlimit überschritten.",
                    provider_id=self.provider_id,
                    model_name=model_name,
                    details={"request_id": job_id, "job_status": status},
                )
            if status not in ACTIVE_JOB_STATUSES:
                raise self._invalid_response(
                    f"RunPod hat den unbekannten Jobstatus '{status}' geliefert.",
                    model_name,
                    "status",
                )

            remaining_seconds = deadline - loop.time()
            if remaining_seconds <= 0:
                await self._cancel_job_safely(client, job_id, model_name)
                raise ProviderTimeoutError(
                    "Der RunPod-Auftrag hat das Zeitlimit überschritten.",
                    provider_id=self.provider_id,
                    model_name=model_name,
                    details={"request_id": job_id, "job_status": status},
                )

            await asyncio.sleep(
                min(self.poll_interval_seconds, remaining_seconds)
            )
            payload = await self._request_json(
                client=client,
                method="GET",
                url=self._url("status", job_id),
                model_name=model_name,
                operation="status",
            )

    async def _cancel_job_safely(
        self,
        client: httpx.AsyncClient,
        job_id: str,
        model_name: str,
    ) -> None:
        try:
            await self._request_json(
                client=client,
                method="POST",
                url=self._url("cancel", job_id),
                model_name=model_name,
                operation="cancel",
            )
        except ProviderError:
            pass

    async def _request_json(
        self,
        *,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        model_name: str,
        operation: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await client.request(method, url, json=json_body)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                "Die RunPod-API hat nicht rechtzeitig geantwortet.",
                provider_id=self.provider_id,
                model_name=model_name,
                details={"operation": operation},
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderConnectionError(
                "Die RunPod-API ist nicht erreichbar.",
                provider_id=self.provider_id,
                model_name=model_name,
                details={"operation": operation},
            ) from exc

        if response.is_error:
            raise provider_error_from_http_status(
                provider_id=self.provider_id,
                model_name=model_name,
                status_code=response.status_code,
                message=(
                    "Die RunPod-API hat die Anfrage abgelehnt "
                    f"(HTTP {response.status_code})."
                ),
                details={"operation": operation},
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise self._invalid_response(
                "RunPod hat keine gültige JSON-Antwort geliefert.",
                model_name,
                operation,
            ) from exc

        if not isinstance(payload, dict):
            raise self._invalid_response(
                "Die RunPod-Antwort ist kein JSON-Objekt.",
                model_name,
                operation,
            )

        return payload

    def _model_response(
        self,
        request: ModelRequest,
        job_id: str,
        payload: dict[str, Any],
    ) -> ModelResponse:
        raw_output = payload.get("output")

        if isinstance(raw_output, str):
            output: dict[str, Any] = {"text": raw_output}
        elif isinstance(raw_output, dict):
            output = raw_output
        else:
            raise self._invalid_response(
                "Der abgeschlossene RunPod-Auftrag enthält keine Ausgabe.",
                request.model_name,
                "output",
            )

        text_value = output.get("text", output.get("response"))
        if isinstance(text_value, list) and all(
            isinstance(part, str) for part in text_value
        ):
            text_value = "".join(text_value)
        if not isinstance(text_value, str) or not text_value.strip():
            raise self._invalid_response(
                "Die RunPod-Modellantwort ist leer.",
                request.model_name,
                "output",
            )

        actual_model = output.get("model")
        if not isinstance(actual_model, str) or not actual_model.strip():
            actual_model = request.model_name

        input_tokens = self._integer(
            output.get("input_tokens", output.get("prompt_eval_count"))
        )
        output_tokens = self._integer(
            output.get("output_tokens", output.get("eval_count"))
        )
        total_tokens = self._integer(output.get("total_tokens"))
        if (
            total_tokens is None
            and input_tokens is not None
            and output_tokens is not None
        ):
            total_tokens = input_tokens + output_tokens

        delay_time = self._number(payload.get("delayTime"))
        execution_time = self._number(payload.get("executionTime"))

        return ModelResponse(
            provider_id=self.provider_id,
            requested_model_name=request.model_name,
            actual_model_name=actual_model.strip(),
            text=text_value.strip(),
            status="completed",
            provider_request_id=job_id,
            token_usage=TokenUsage(
                input_tokens=self._integer_metric(input_tokens),
                output_tokens=self._integer_metric(output_tokens),
                total_tokens=self._integer_metric(total_tokens),
            ),
            provider_timing=ProviderTiming(
                queue_duration_ms=self._float_metric(delay_time),
            ),
            raw_metadata={
                "job_status": payload.get("status"),
                "delay_time_ms": delay_time,
                "execution_time_ms": execution_time,
            },
        )

    def _invalid_response(
        self,
        message: str,
        model_name: str,
        operation: str,
    ) -> ProviderInvalidResponseError:
        return ProviderInvalidResponseError(
            message,
            provider_id=self.provider_id,
            model_name=model_name,
            details={"operation": operation},
        )

    @staticmethod
    def _integer(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, float) and value >= 0 and value.is_integer():
            return int(value)
        return None

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)
        return None

    @staticmethod
    def _integer_metric(value: int | None) -> IntegerMetric:
        if value is None:
            return IntegerMetric()
        return IntegerMetric(value=value, source=MetricSource.PROVIDER)

    @staticmethod
    def _float_metric(value: float | None) -> FloatMetric:
        if value is None:
            return FloatMetric(unit="ms")
        return FloatMetric(
            value=value,
            source=MetricSource.PROVIDER,
            unit="ms",
        )

    @staticmethod
    def _elapsed_ms(started_at: float) -> float:
        return round((perf_counter() - started_at) * 1000, 3)