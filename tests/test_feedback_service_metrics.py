from __future__ import annotations

import unittest

from app.llm.base import LLMResponse
from app.services.feedback_service import FeedbackService


class _MetricProvider:
    provider_name = "runpod"
    model_name = "test-model"

    async def generate(self, prompt: str) -> LLMResponse:
        if "Schülertext" not in prompt:
            raise AssertionError("Der Feedbackprompt wurde nicht erzeugt.")

        return LLMResponse(
            provider="runpod",
            model=self.model_name,
            text="Testfeedback",
            queue_duration_ms=1234.0,
            execution_duration_ms=5678.0,
            provider_request_id="job-123",
            worker_id="worker-456",
        )


class FeedbackServiceMetricTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_metrics_reach_feedback_result(self) -> None:
        provider = _MetricProvider()
        service = FeedbackService(
            providers={"runpod": provider},
            max_input_chars=8000,
        )

        result = await service.analyze_text(
            student_text="Ein kurzer Schülertext.",
            provider_key="runpod",
        )

        self.assertEqual(result.queue_duration_ms, 1234.0)
        self.assertEqual(result.execution_duration_ms, 5678.0)
        self.assertEqual(result.provider_request_id, "job-123")
        self.assertEqual(result.worker_id, "worker-456")


if __name__ == "__main__":
    unittest.main()
