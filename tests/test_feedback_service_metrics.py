from __future__ import annotations

import unittest

from app.llm.base import LLMResponse
from app.services.feedback_service import (
    STANDARD_FEEDBACK_CLOUD_SYSTEM_PROMPT,
    STANDARD_FEEDBACK_MODE,
    STANDARD_FEEDBACK_PROMPT_VERSION,
    STANDARD_FEEDBACK_STUDENT_TEXT_PLACEHOLDER,
    FeedbackResult,
    FeedbackService,
)


class _MetricProvider:
    provider_name = "runpod"
    model_name = "test-model"

    def __init__(self) -> None:
        self.last_prompt = ""

    async def generate(self, prompt: str) -> LLMResponse:
        self.last_prompt = prompt

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
        self.assertIn("das Feedback leicht verstehen", provider.last_prompt)
        self.assertIn("nicht unnötig schwierige Sprache", provider.last_prompt)
        self.assertIn("Verwende keine Tabellen", provider.last_prompt)
        self.assertIn("keine senkrechten Striche als", provider.last_prompt)
        self.assertIn("Spaltentrenner", provider.last_prompt)
        self.assertIn("**Original:**", provider.last_prompt)
        self.assertIn("**Mögliche Überarbeitung:**", provider.last_prompt)
        self.assertIn("**Begründung:**", provider.last_prompt)
        self.assertEqual(
            STANDARD_FEEDBACK_PROMPT_VERSION,
            "standard-feedback-v3",
        )
        self.assertIn("Ein kurzer Schülertext.", provider.last_prompt)

        payload = result.payload()
        context = payload["generation_context"]
        self.assertIsInstance(context, dict)
        self.assertEqual(context["mode"], STANDARD_FEEDBACK_MODE)
        self.assertEqual(
            context["prompt_version"],
            STANDARD_FEEDBACK_PROMPT_VERSION,
        )
        self.assertIn(
            STANDARD_FEEDBACK_STUDENT_TEXT_PLACEHOLDER,
            context["prompt_template"],
        )
        self.assertNotIn(
            "Ein kurzer Schülertext.",
            context["prompt_template"],
        )
        self.assertIsNone(context["system_prompt"])

        cloud_context = FeedbackResult(
            provider="openai",
            model="test-cloud-model",
            feedback="Cloud-Feedback",
            duration_ms=1,
        ).payload()["generation_context"]
        self.assertEqual(
            cloud_context["system_prompt"],
            STANDARD_FEEDBACK_CLOUD_SYSTEM_PROMPT,
        )


if __name__ == "__main__":
    unittest.main()
