from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.llm.mistral_client import (
    MISTRAL_API_BASE_URL,
    MistralProvider,
)


class MistralProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_uses_mistral_api_and_maps_response(self) -> None:
        completion = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="  Mistral-Testantwort  "
                        )
                    )
                ]
            )
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=completion)
            )
        )

        with patch(
            "app.llm.mistral_client.AsyncOpenAI",
            return_value=client,
        ) as client_class:
            provider = MistralProvider(
                api_key="test-mistral-key",
                model_name="mistral-medium-latest",
            )
            response = await provider.generate("Testprompt")

        client_class.assert_called_once_with(
            api_key="test-mistral-key",
            base_url=MISTRAL_API_BASE_URL,
        )
        completion.assert_awaited_once()
        request = completion.await_args.kwargs
        self.assertEqual(request["model"], "mistral-medium-latest")
        self.assertEqual(request["messages"][1]["content"], "Testprompt")
        self.assertEqual(response.provider, "mistral")
        self.assertEqual(response.model, "mistral-medium-latest")
        self.assertEqual(response.text, "Mistral-Testantwort")

    async def test_generate_rejects_missing_api_key(self) -> None:
        provider = MistralProvider(
            api_key=None,
            model_name="mistral-small-latest",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "MISTRAL_API_KEY",
        ):
            await provider.generate("Testprompt")


if __name__ == "__main__":
    unittest.main()
