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

    async def generate(self, prompt: str) -> LLMResponse:
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

        response = await client.chat.completions.create(
            model=self.model_name,
            messages=[
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
        )

        text = response.choices[0].message.content or ""

        return LLMResponse(
            provider=self.provider_name,
            model=self.model_name,
            text=text.strip(),
        )
