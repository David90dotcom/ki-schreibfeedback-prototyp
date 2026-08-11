from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.llm.ollama_client import OllamaProvider


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
            180.0,
        )
        self.assertFalse(
            client_class.call_args.kwargs["trust_env"]
        )


if __name__ == "__main__":
    unittest.main()
