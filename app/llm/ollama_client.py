import httpx

from app.llm.base import LLMResponse


class OllamaProvider:
    provider_name = "ollama"

    def __init__(self, base_url: str, model_name: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name

    async def discover_models(self) -> list[str]:
        url = f"{self.base_url}/api/tags"

        async with httpx.AsyncClient(
            timeout=10.0,
            trust_env=False,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()

        data = response.json()
        models = data.get("models", [])

        return sorted(
            {
                model.get("name", "").strip()
                for model in models
                if isinstance(model, dict)
                and model.get("name", "").strip()
            }
        )

    async def generate(self, prompt: str) -> LLMResponse:
        url = f"{self.base_url}/api/generate"

        payload = {
            "model": self.model_name,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.2,
            },
        }

        async with httpx.AsyncClient(
            timeout=180.0,
            trust_env=False,
        ) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()

        data = response.json()
        text = data.get("response", "").strip()

        return LLMResponse(
            provider=self.provider_name,
            model=self.model_name,
            text=text,
        )