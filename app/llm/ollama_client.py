from math import isfinite
from typing import Any

import httpx

from app.llm.base import LLMResponse


class OllamaRequestTimeoutError(TimeoutError):
    """Ollama hat innerhalb des erlaubten Zeitfensters nicht geantwortet."""


class OllamaProvider:
    provider_name = "ollama"

    def __init__(
        self,
        base_url: str,
        model_name: str,
        request_timeout_seconds: float = 600.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.request_timeout_seconds = float(
            request_timeout_seconds
        )

        if (
            not isfinite(self.request_timeout_seconds)
            or self.request_timeout_seconds <= 0
        ):
            raise ValueError(
                "Das Ollama-Anfragezeitlimit muss größer als null sein."
            )

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

    async def generate(
        self,
        prompt: str,
        *,
        response_schema: dict[str, Any] | None = None,
        response_schema_name: str = "structured_response",
    ) -> LLMResponse:
        url = f"{self.base_url}/api/generate"

        payload = {
            "model": self.model_name,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.2,
            },
        }

        if response_schema is not None:
            payload["format"] = response_schema

        try:
            async with httpx.AsyncClient(
                timeout=self.request_timeout_seconds,
                trust_env=False,
            ) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
        except httpx.ReadTimeout as exc:
            timeout_label = f"{self.request_timeout_seconds:g}"
            raise OllamaRequestTimeoutError(
                "Das lokale Ollama-Modell hat innerhalb von "
                f"{timeout_label} Sekunden keine vollständige Antwort "
                "geliefert. Große oder neu geladene Modelle benötigen "
                "möglicherweise länger. Versuche es nach dem Vorladen "
                "erneut, erhöhe OLLAMA_REQUEST_TIMEOUT_SECONDS oder "
                "deaktiviere für einen schnelleren Test den Zwei-Pass-Modus."
            ) from exc

        data = response.json()
        text = data.get("response", "").strip()

        return LLMResponse(
            provider=self.provider_name,
            model=self.model_name,
            text=text,
            raw_metadata={
                "finish_reason": data.get("done_reason"),
            },
        )
