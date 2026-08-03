from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.domain.metrics import MetricSource
from app.domain.model_catalog import ModelParameters
from app.llm.base import ModelRequest
from app.llm.errors import (
    ProviderAuthenticationError,
    ProviderTimeoutError,
)
from app.llm.runpod_client import RunPodProvider


class RunPodProviderTests(unittest.IsolatedAsyncioTestCase):
    MODEL_NAME = "ministral-3:14b-instruct-2512-q8_0"
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
                            "response": (
                                "Simuliertes RunPod-Feedback"
                            ),
                            "model": self.MODEL_NAME,
                            "prompt_eval_count": 21,
                            "eval_count": 9,
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
        worker_input = request_body["input"]

        self.assertEqual(
            worker_input["model"],
            self.MODEL_NAME,
        )
        self.assertEqual(
            worker_input["prompt"],
            "Ein kurzer Beispieltext.",
        )
        self.assertEqual(
            worker_input["system"],
            "Gib lernförderliches Schreibfeedback.",
        )
        self.assertFalse(worker_input["stream"])
        self.assertEqual(
            worker_input["options"],
            {
                "temperature": 0.15,
                "num_predict": 4000,
                "seed": 7,
            },
        )
        self.assertEqual(
            worker_input["format"],
            self._request().response_schema,
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