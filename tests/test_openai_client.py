from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.llm.openai_client import OpenAIProvider


class OpenAIProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_sends_strict_json_schema(self) -> None:
        completion = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(
                            content='{"criteria": []}'
                        ),
                    )
                ]
            )
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=completion)
            )
        )
        schema = {
            "type": "object",
            "properties": {},
        }

        with patch(
            "app.llm.openai_client.AsyncOpenAI",
            return_value=client,
        ):
            provider = OpenAIProvider(
                api_key="test-openai-key",
                model_name="gpt-5.6-luna",
                reasoning_effort="max",
            )
            response = await provider.generate(
                "JSON-Testprompt",
                response_schema=schema,
                response_schema_name="rubric_feedback",
            )

        request = completion.await_args.kwargs
        self.assertEqual(request["reasoning_effort"], "max")
        self.assertEqual(
            request["response_format"],
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "rubric_feedback",
                    "strict": True,
                    "schema": schema,
                },
            },
        )
        self.assertEqual(
            response.raw_metadata["finish_reason"],
            "stop",
        )
        self.assertEqual(
            response.raw_metadata["reasoning_effort"],
            "max",
        )

    async def test_invalid_reasoning_effort_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Denktiefe"):
            OpenAIProvider(
                api_key="test-openai-key",
                model_name="gpt-5.6-sol",
                reasoning_effort="unbegrenzt",
            )

    async def test_generate_rejects_missing_api_key(self) -> None:
        provider = OpenAIProvider(
            api_key=None,
            model_name="gpt-5.6-luna",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "OPENAI_API_KEY",
        ):
            await provider.generate("Testprompt")


if __name__ == "__main__":
    unittest.main()
