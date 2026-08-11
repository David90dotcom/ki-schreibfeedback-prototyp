from typing import Any

from openai import AsyncOpenAI

from app.llm.base import LLMResponse


MISTRAL_API_BASE_URL = "https://api.mistral.ai/v1"


class MistralProvider:
    """Cloudprovider für die OpenAI-kompatible Mistral-API."""

    provider_id = "mistral"
    provider_name = "mistral"

    def __init__(
        self,
        api_key: str | None,
        model_name: str,
    ) -> None:
        self.api_key = api_key
        self.model_name = model_name

    async def generate(
        self,
        prompt: str,
        *,
        response_schema: dict[str, Any] | None = None,
        response_schema_name: str = "structured_response",
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError(
                "Kein Mistral-API-Key verfügbar. Hinterlege "
                "MISTRAL_API_KEY in der .env-Datei oder gib für "
                "diesen Aufruf einen abweichenden Key ein."
            )

        client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=MISTRAL_API_BASE_URL,
        )

        request: dict[str, Any] = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Du bist ein hilfreiches Assistenzsystem "
                        "für lernförderliches Schreibfeedback im "
                        "Deutschunterricht."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
        }

        if response_schema is not None:
            request["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema_name,
                    "schema": response_schema,
                },
            }

        response = await client.chat.completions.create(
            **request,
        )

        choice = response.choices[0]
        text = choice.message.content or ""

        return LLMResponse(
            provider=self.provider_name,
            model=self.model_name,
            text=text.strip(),
            raw_metadata={
                "finish_reason": choice.finish_reason,
            },
        )
