from openai import AsyncOpenAI

from app.llm.base import LLMResponse


class OpenAIProvider:
    provider_name = "openai"

    def __init__(self, api_key: str | None, model_name: str) -> None:
        self.api_key = api_key
        self.model_name = model_name

    async def generate(self, prompt: str) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError(
                "OPENAI_API_KEY ist nicht gesetzt. Bitte trage den API-Key in der .env-Datei ein."
            )

        client = AsyncOpenAI(api_key=self.api_key)

        response = await client.chat.completions.create(
            model=self.model_name,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Du bist ein hilfreiches Assistenzsystem für lernförderliches "
                        "Schreibfeedback im Deutschunterricht."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            temperature=0.2,
        )

        text = response.choices[0].message.content or ""

        return LLMResponse(
            provider=self.provider_name,
            model=self.model_name,
            text=text.strip(),
        )