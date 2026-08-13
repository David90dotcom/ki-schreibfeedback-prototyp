from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.llm.ollama_client import (
    OllamaProvider,
    OllamaRequestTimeoutError,
)


class OllamaProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_sends_json_schema_as_format(self) -> None:
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "response": '{"criteria": []}',
            "done_reason": "stop",
        }
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        client.post.return_value = response
        schema = {
            "type": "object",
            "properties": {},
        }

        with patch(
            "app.llm.ollama_client.httpx.AsyncClient",
            return_value=client,
        ) as client_class:
            provider = OllamaProvider(
                base_url="http://localhost:11434",
                model_name="ministral-3:14b-instruct-2512-q8_0",
            )
            result = await provider.generate(
                "JSON-Testprompt",
                response_schema=schema,
                response_schema_name="rubric_feedback",
            )

        request = client.post.await_args
        self.assertEqual(request.args[0], "http://localhost:11434/api/generate")
        self.assertEqual(request.kwargs["json"]["format"], schema)
        self.assertEqual(
            result.raw_metadata["finish_reason"],
            "stop",
        )
        self.assertEqual(
            client_class.call_args.kwargs["timeout"],
            600.0,
        )
        self.assertFalse(
            client_class.call_args.kwargs["trust_env"]
        )

    async def test_generate_uses_configured_request_timeout(self) -> None:
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "response": "Ergebnis",
            "done_reason": "stop",
        }
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        client.post.return_value = response

        with patch(
            "app.llm.ollama_client.httpx.AsyncClient",
            return_value=client,
        ) as client_class:
            provider = OllamaProvider(
                base_url="http://localhost:11434",
                model_name="mistral-small3.2:24b-instruct-2506-q8_0",
                request_timeout_seconds=900,
            )
            await provider.generate("Langer Testprompt")

        self.assertEqual(
            client_class.call_args.kwargs["timeout"],
            900.0,
        )

    async def test_read_timeout_has_actionable_message(self) -> None:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        client.post.side_effect = httpx.ReadTimeout(
            "Ollama antwortet zu langsam."
        )

        with patch(
            "app.llm.ollama_client.httpx.AsyncClient",
            return_value=client,
        ):
            provider = OllamaProvider(
                base_url="http://localhost:11434",
                model_name="mistral-small3.2:24b-instruct-2506-q8_0",
                request_timeout_seconds=600,
            )

            with self.assertRaisesRegex(
                OllamaRequestTimeoutError,
                "innerhalb von 600 Sekunden",
            ) as raised:
                await provider.generate("Langer Testprompt")

        self.assertIn(
            "OLLAMA_REQUEST_TIMEOUT_SECONDS",
            str(raised.exception),
        )
        self.assertIn("Zwei-Pass-Modus", str(raised.exception))

    def test_rejects_non_positive_request_timeout(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "größer als null",
        ):
            OllamaProvider(
                base_url="http://localhost:11434",
                model_name="test-model",
                request_timeout_seconds=0,
            )


if __name__ == "__main__":
    unittest.main()
