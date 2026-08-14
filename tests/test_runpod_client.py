from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, call, patch

import httpx

from app.domain.metrics import (
    FloatMetric,
    MetricSource,
    ProviderTiming,
)
from app.domain.model_catalog import ModelParameters
from app.llm.base import ModelRequest, ModelResponse
from app.llm.errors import (
    ProviderAuthenticationError,
    ProviderInvalidRequestError,
    ProviderTimeoutError,
    ProviderUnknownError,
)
from app.llm.runpod_client import RunPodProvider


class RunPodProviderTests(unittest.IsolatedAsyncioTestCase):
    MODEL_NAME = (
        "RedHatAI/"
        "Mistral-Small-3.2-24B-Instruct-2506-FP8"
    )
    ENDPOINT_ID = "test-endpoint"
    API_KEY = "test-runpod-api-key"

    def _provider(self) -> RunPodProvider:
        return RunPodProvider(
            api_key=self.API_KEY,
            endpoint_id=self.ENDPOINT_ID,
            model_name=self.MODEL_NAME,
            job_timeout_seconds=30.0,
            poll_interval_seconds=0.01,
        )

    def test_default_job_timeout_is_bounded(self) -> None:
        provider = RunPodProvider(
            api_key=self.API_KEY,
            endpoint_id=self.ENDPOINT_ID,
            model_name=self.MODEL_NAME,
        )

        self.assertEqual(provider.job_timeout_seconds, 900.0)

    async def test_cancel_job_uses_exact_request_id(self) -> None:
        recorded_requests: list[httpx.Request] = []
        callback = AsyncMock()

        def handler(request: httpx.Request) -> httpx.Response:
            recorded_requests.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "job-to-cancel-e1",
                    "status": "CANCELLED",
                },
            )

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        )
        provider = RunPodProvider(
            api_key=self.API_KEY,
            endpoint_id=self.ENDPOINT_ID,
            model_name=self.MODEL_NAME,
            job_status_callback=callback,
        )

        with patch(
            "app.llm.runpod_client.httpx.AsyncClient",
            return_value=client,
        ) as mocked_client:
            payload = await provider.cancel_job(
                "job-to-cancel-e1"
            )

        self.assertEqual(
            payload,
            {
                "id": "job-to-cancel-e1",
                "status": "CANCELLED",
            },
        )
        self.assertEqual(len(recorded_requests), 1)
        self.assertEqual(recorded_requests[0].method, "POST")
        self.assertEqual(
            recorded_requests[0].url.path,
            f"/v2/{self.ENDPOINT_ID}/cancel/job-to-cancel-e1",
        )
        self.assertEqual(
            mocked_client.call_args.kwargs["headers"]["Authorization"],
            f"Bearer {self.API_KEY}",
        )
        callback.assert_awaited_once_with(
            "job-to-cancel-e1",
            "CANCELLED",
        )

    async def test_cancel_job_rejects_path_injection(self) -> None:
        provider = self._provider()

        with patch(
            "app.llm.runpod_client.httpx.AsyncClient"
        ) as client_class:
            with self.assertRaisesRegex(
                ProviderInvalidRequestError,
                "kein gültiges Format",
            ):
                await provider.cancel_job("../../purge-queue")

        client_class.assert_not_called()

    async def test_complete_reports_individual_job_statuses(
        self,
    ) -> None:
        callback = AsyncMock()
        status_requests = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal status_requests

            if request.url.path.endswith("/run"):
                return httpx.Response(
                    200,
                    json={
                        "id": "tracked-job-e1",
                        "status": "IN_QUEUE",
                    },
                )

            status_requests += 1
            return httpx.Response(
                200,
                json={
                    "id": "tracked-job-e1",
                    "status": "COMPLETED",
                    "output": {
                        "model": self.MODEL_NAME,
                        "choices": [
                            {
                                "message": {
                                    "content": "Fertiges Feedback",
                                },
                                "finish_reason": "stop",
                            }
                        ],
                    },
                },
            )

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        )
        provider = RunPodProvider(
            api_key=self.API_KEY,
            endpoint_id=self.ENDPOINT_ID,
            model_name=self.MODEL_NAME,
            poll_interval_seconds=0.01,
            job_status_callback=callback,
        )

        with (
            patch(
                "app.llm.runpod_client.httpx.AsyncClient",
                return_value=client,
            ),
            patch(
                "app.llm.runpod_client.asyncio.sleep",
                new=AsyncMock(),
            ),
        ):
            response = await provider.complete(self._request())

        self.assertEqual(response.text, "Fertiges Feedback")
        self.assertEqual(status_requests, 1)
        self.assertEqual(
            callback.await_args_list,
            [
                call("tracked-job-e1", "IN_QUEUE"),
                call("tracked-job-e1", "COMPLETED"),
            ],
        )

    @staticmethod
    def _request() -> ModelRequest:
        return ModelRequest(
            model_name=RunPodProviderTests.MODEL_NAME,
            instructions="Gib lernförderliches Schreibfeedback.",
            input_text="Ein kurzer Beispieltext.",
            parameters=ModelParameters(
                temperature=0.15,
                max_output_tokens=4000,
                seed=7,
            ),
            response_schema={
                "type": "object",
                "properties": {
                    "feedback": {"type": "string"},
                },
                "required": ["feedback"],
            },
        )

    async def test_complete_submits_polls_and_maps_response(
        self,
    ) -> None:
        recorded_requests: list[httpx.Request] = []

        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            recorded_requests.append(request)

            if request.url.path.endswith("/run"):
                return httpx.Response(
                    200,
                    json={
                        "id": "job-123",
                        "status": "IN_QUEUE",
                    },
                )

            if request.url.path.endswith(
                "/status/job-123"
            ):
                return httpx.Response(
                    200,
                    json={
                        "id": "job-123",
                        "status": "COMPLETED",
                        "delayTime": 12,
                        "executionTime": 345,
                        "output": {
                            "id": "chatcmpl-test-123",
                            "object": "chat.completion",
                            "model": self.MODEL_NAME,
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": (
                                            "Simuliertes RunPod-Feedback"
                                        ),
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 21,
                                "completion_tokens": 9,
                                "total_tokens": 30,
                            },
                        },
                    },
                )

            return httpx.Response(404)

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        )
        provider = self._provider()

        with (
            patch(
                "app.llm.runpod_client.httpx.AsyncClient",
                return_value=client,
            ) as mocked_client,
            patch(
                "app.llm.runpod_client.asyncio.sleep",
                new=AsyncMock(),
            ) as mocked_sleep,
        ):
            response = await provider.complete(
                self._request()
            )

        self.assertEqual(len(recorded_requests), 2)
        submit_request, status_request = (
            recorded_requests
        )

        self.assertEqual(
            submit_request.method,
            "POST",
        )
        self.assertEqual(
            submit_request.url.path,
            f"/v2/{self.ENDPOINT_ID}/run",
        )

        client_headers = (
            mocked_client.call_args.kwargs["headers"]
        )
        self.assertEqual(
            client_headers["Authorization"],
            f"Bearer {self.API_KEY}",
        )
        self.assertFalse(
            mocked_client.call_args.kwargs["trust_env"]
        )

        request_body = json.loads(
            submit_request.content
        )
        self.assertEqual(
            request_body["policy"],
            {
                "executionTimeout": 600_000,
                "ttl": 900_000,
            },
        )
        worker_input = request_body["input"]

        self.assertEqual(
            worker_input,
            {
                "route": "/v1/chat/completions",
                "method": "POST",
                "body": {
                    "model": self.MODEL_NAME,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Gib lernförderliches "
                                "Schreibfeedback."
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                "Ein kurzer Beispieltext."
                            ),
                        },
                    ],
                    "stream": False,
                    "temperature": 0.15,
                    "max_tokens": 4000,
                    "seed": 7,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "structured_response",
                            "schema": (
                                self._request()
                                .response_schema
                            ),
                        },
                    },
                },
            },
        )

        self.assertEqual(
            status_request.method,
            "GET",
        )
        self.assertEqual(
            status_request.url.path,
            (
                f"/v2/{self.ENDPOINT_ID}"
                "/status/job-123"
            ),
        )
        mocked_sleep.assert_awaited_once()

        self.assertEqual(
            response.provider_id,
            "runpod",
        )
        self.assertEqual(
            response.requested_model_name,
            self.MODEL_NAME,
        )
        self.assertEqual(
            response.actual_model_name,
            self.MODEL_NAME,
        )
        self.assertEqual(
            response.text,
            "Simuliertes RunPod-Feedback",
        )
        self.assertEqual(
            response.status,
            "completed",
        )
        self.assertEqual(
            response.provider_request_id,
            "job-123",
        )
        self.assertEqual(
            response.token_usage.input_tokens.value,
            21,
        )
        self.assertEqual(
            response.token_usage.output_tokens.value,
            9,
        )
        self.assertEqual(
            response.token_usage.total_tokens.value,
            30,
        )
        self.assertEqual(
            response.token_usage.total_tokens.source,
            MetricSource.PROVIDER,
        )
        self.assertEqual(
            (
                response.provider_timing
                .queue_duration_ms.value
            ),
            12.0,
        )
        self.assertEqual(
            response.raw_metadata[
                "execution_time_ms"
            ],
            345.0,
        )

    async def test_http_401_becomes_authentication_error(
        self,
    ) -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                401,
                json={
                    "error": "Invalid API key",
                },
            )

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        )
        provider = self._provider()

        with patch.object(
            provider,
            "_create_http_client",
            return_value=client,
        ):
            with self.assertRaises(
                ProviderAuthenticationError
            ) as raised:
                await provider.complete(
                    self._request()
                )

        error = raised.exception

        self.assertEqual(
            error.provider_id,
            "runpod",
        )
        self.assertEqual(
            error.model_name,
            self.MODEL_NAME,
        )
        self.assertEqual(
            error.status_code,
            401,
        )
        self.assertFalse(error.retryable)
        self.assertEqual(
            error.details["operation"],
            "run",
        )
        self.assertEqual(
            error.details["response_error"],
            "Invalid API key",
        )

    async def test_transient_status_409_is_retried_without_cancel(
        self,
    ) -> None:
        status_requests = 0
        cancel_requests = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal status_requests, cancel_requests

            if request.url.path.endswith("/run"):
                return httpx.Response(
                    200,
                    json={
                        "id": "job-transient-409",
                        "status": "IN_QUEUE",
                    },
                )

            if request.url.path.endswith(
                "/status/job-transient-409"
            ):
                status_requests += 1

                if status_requests == 1:
                    return httpx.Response(
                        409,
                        json={
                            "error": "Job status is transitioning",
                        },
                    )

                return httpx.Response(
                    200,
                    json={
                        "id": "job-transient-409",
                        "status": "COMPLETED",
                        "output": {
                            "model": self.MODEL_NAME,
                            "text": "Feedback nach erneutem Statusabruf",
                        },
                    },
                )

            if request.url.path.endswith(
                "/cancel/job-transient-409"
            ):
                cancel_requests += 1
                return httpx.Response(
                    200,
                    json={
                        "id": "job-transient-409",
                        "status": "CANCELLED",
                    },
                )

            return httpx.Response(404)

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        )
        provider = self._provider()

        with (
            patch.object(
                provider,
                "_create_http_client",
                return_value=client,
            ),
            patch(
                "app.llm.runpod_client.asyncio.sleep",
                new=AsyncMock(),
            ),
            self.assertLogs(
                "app.llm.runpod_client",
                level="WARNING",
            ) as captured_logs,
        ):
            response = await provider.complete(self._request())

        self.assertEqual(
            response.text,
            "Feedback nach erneutem Statusabruf",
        )
        self.assertEqual(status_requests, 2)
        self.assertEqual(cancel_requests, 0)
        self.assertIn(
            "der Auftrag bleibt aktiv",
            "\n".join(captured_logs.output),
        )

    async def test_repeated_status_409_is_bounded_and_cancelled(
        self,
    ) -> None:
        status_requests = 0
        cancel_requests = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal status_requests, cancel_requests

            if request.url.path.endswith("/run"):
                return httpx.Response(
                    200,
                    json={
                        "id": "job-persistent-409",
                        "status": "IN_QUEUE",
                    },
                )

            if request.url.path.endswith(
                "/status/job-persistent-409"
            ):
                status_requests += 1
                return httpx.Response(
                    409,
                    json={"message": "Job remains locked"},
                )

            if request.url.path.endswith(
                "/cancel/job-persistent-409"
            ):
                cancel_requests += 1
                return httpx.Response(
                    200,
                    json={
                        "id": "job-persistent-409",
                        "status": "CANCELLED",
                    },
                )

            return httpx.Response(404)

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        )
        provider = self._provider()

        with (
            patch.object(
                provider,
                "_create_http_client",
                return_value=client,
            ),
            patch(
                "app.llm.runpod_client.asyncio.sleep",
                new=AsyncMock(),
            ),
            patch(
                "app.llm.runpod_client."
                "RUNPOD_STATUS_MAX_TRANSIENT_RETRIES",
                2,
            ),
            self.assertLogs(
                "app.llm.runpod_client",
                level="WARNING",
            ),
        ):
            with self.assertRaises(
                ProviderUnknownError
            ) as raised:
                await provider.complete(self._request())

        error = raised.exception
        self.assertEqual(status_requests, 3)
        self.assertEqual(cancel_requests, 1)
        self.assertEqual(error.status_code, 409)
        self.assertEqual(
            error.details,
            {
                "operation": "status",
                "response_error": "Job remains locked",
                "request_id": "job-persistent-409",
                "job_status": "IN_QUEUE",
                "status_retry_count": 3,
            },
        )

    async def test_generate_preserves_runpod_timing_and_worker_id(
        self,
    ) -> None:
        provider = self._provider()
        model_response = ModelResponse(
            provider_id="runpod",
            requested_model_name=self.MODEL_NAME,
            actual_model_name=self.MODEL_NAME,
            text="Feedback mit Messwerten",
            status="completed",
            provider_request_id="job-789",
            provider_timing=ProviderTiming(
                queue_duration_ms=FloatMetric(
                    value=1200,
                    source=MetricSource.PROVIDER,
                    unit="ms",
                ),
                execution_duration_ms=FloatMetric(
                    value=3400,
                    source=MetricSource.PROVIDER,
                    unit="ms",
                ),
            ),
            raw_metadata={"worker_id": "worker-789"},
        )

        schema = {
            "type": "object",
            "properties": {},
        }
        complete = AsyncMock(return_value=model_response)

        with patch.object(
            provider,
            "complete",
            new=complete,
        ):
            response = await provider.generate(
                "Testprompt",
                response_schema=schema,
                response_schema_name="rubric_feedback",
            )

        self.assertEqual(response.queue_duration_ms, 1200)
        self.assertEqual(response.execution_duration_ms, 3400)
        self.assertEqual(response.provider_request_id, "job-789")
        self.assertEqual(response.worker_id, "worker-789")
        request = complete.await_args.args[0]
        self.assertEqual(request.response_schema, schema)
        self.assertEqual(
            request.response_schema_name,
            "rubric_feedback",
        )

    async def test_timed_out_job_becomes_timeout_error(
        self,
    ) -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "job-timeout",
                    "status": "TIMED_OUT",
                },
            )

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        )
        provider = self._provider()

        with patch.object(
            provider,
            "_create_http_client",
            return_value=client,
        ):
            with self.assertRaises(
                ProviderTimeoutError
            ) as raised:
                await provider.complete(
                    self._request()
                )

        error = raised.exception

        self.assertEqual(
            error.provider_id,
            "runpod",
        )
        self.assertEqual(
            error.model_name,
            self.MODEL_NAME,
        )
        self.assertTrue(error.retryable)
        self.assertEqual(
            error.details,
            {
                "request_id": "job-timeout",
                "job_status": "TIMED_OUT",
            },
        )


if __name__ == "__main__":
    unittest.main()
