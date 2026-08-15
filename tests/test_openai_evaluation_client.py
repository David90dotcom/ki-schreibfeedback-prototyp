from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.llm.openai_evaluation_client import (
    OPENAI_EVALUATION_MAX_OUTPUT_TOKENS,
    OpenAIAutomaticEvaluationProvider,
)


class OpenAIAutomaticEvaluationProviderTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_evaluate_uses_terra_medium_without_storage_by_default(
        self,
    ) -> None:
        create_response = AsyncMock(
            return_value=SimpleNamespace(
                status="completed",
                output_text='{"ratings": {}}',
                model="gpt-5.6-terra",
                id="resp-evaluation-1",
            )
        )
        client = SimpleNamespace(
            responses=SimpleNamespace(create=create_response)
        )
        schema = {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }

        with patch(
            "app.llm.openai_evaluation_client.AsyncOpenAI",
            return_value=client,
        ):
            provider = OpenAIAutomaticEvaluationProvider(
                api_key="test-openai-key",
                model_name="gpt-5.6-terra",
            )
            response = await provider.evaluate(
                instructions="Prüfe unabhängig.",
                input_text="Abgegrenzte Bewertungsdaten",
                response_schema=schema,
                response_schema_name="feedback_quality_evaluation",
            )

        request = create_response.await_args.kwargs
        self.assertEqual(request["model"], "gpt-5.6-terra")
        self.assertEqual(
            request["reasoning"],
            {"effort": "medium"},
        )
        self.assertEqual(
            request["max_output_tokens"],
            OPENAI_EVALUATION_MAX_OUTPUT_TOKENS,
        )
        self.assertIs(request["store"], False)
        self.assertEqual(request["instructions"], "Prüfe unabhängig.")
        self.assertEqual(
            request["text"]["format"],
            {
                "type": "json_schema",
                "name": "feedback_quality_evaluation",
                "strict": True,
                "schema": schema,
            },
        )
        self.assertEqual(response.provider, "openai")
        self.assertEqual(response.model, "gpt-5.6-terra")
        self.assertEqual(response.provider_request_id, "resp-evaluation-1")
        self.assertIsNone(response.reasoning_mode)
        self.assertEqual(response.reasoning_effort, "medium")

    async def test_evaluate_accepts_model_mode_and_effort_per_run(
        self,
    ) -> None:
        create_response = AsyncMock(
            return_value=SimpleNamespace(
                status="completed",
                output_text='{"ratings": {}}',
                model="gpt-5.6-luna",
                id="resp-evaluation-2",
            )
        )
        client = SimpleNamespace(
            responses=SimpleNamespace(create=create_response)
        )

        with patch(
            "app.llm.openai_evaluation_client.AsyncOpenAI",
            return_value=client,
        ):
            provider = OpenAIAutomaticEvaluationProvider(
                api_key="test-openai-key",
                model_name="gpt-5.6-terra",
            )
            response = await provider.evaluate(
                instructions="Prüfe unabhängig.",
                input_text="Abgegrenzte Bewertungsdaten",
                response_schema={},
                response_schema_name="feedback_quality_evaluation",
                model_name="gpt-5.6-luna",
                reasoning_mode="pro",
                reasoning_effort="low",
            )

        request = create_response.await_args.kwargs
        self.assertEqual(request["model"], "gpt-5.6-luna")
        self.assertEqual(
            request["reasoning"],
            {"mode": "pro", "effort": "low"},
        )
        self.assertEqual(response.reasoning_mode, "pro")
        self.assertEqual(response.reasoning_effort, "low")

    async def test_evaluate_rejects_missing_api_key(self) -> None:
        provider = OpenAIAutomaticEvaluationProvider(
            api_key=None,
            model_name="gpt-5.6-sol",
        )

        with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
            await provider.evaluate(
                instructions="Prüfe.",
                input_text="Daten",
                response_schema={},
                response_schema_name="evaluation",
            )


if __name__ == "__main__":
    unittest.main()
