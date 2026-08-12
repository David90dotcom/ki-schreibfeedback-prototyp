from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from openai import AsyncOpenAI


OPENAI_EVALUATION_PROVIDER = "openai"
OPENAI_EVALUATION_REASONING_MODE = "pro"
OPENAI_EVALUATION_REASONING_EFFORT = "high"
OPENAI_EVALUATION_MAX_OUTPUT_TOKENS = 25000


@dataclass(frozen=True)
class AutomaticEvaluationModelResponse:
    """Für die Meta-Bewertung benötigte Teilmenge einer Modellantwort."""

    provider: str
    model: str
    text: str
    provider_request_id: str | None


class AutomaticEvaluationProvider(Protocol):
    """Kleine, testbare Schnittstelle für ein getrenntes Bewertungsmodell."""

    provider_name: str
    model_name: str

    async def evaluate(
        self,
        *,
        instructions: str,
        input_text: str,
        response_schema: dict[str, Any],
        response_schema_name: str,
    ) -> AutomaticEvaluationModelResponse:
        """Bewertet ein Feedback und liefert streng strukturiertes JSON."""
        ...


class OpenAIAutomaticEvaluationProvider:
    """Getrennter OpenAI-Responses-Adapter für die automatische Meta-Ebene."""

    provider_name = OPENAI_EVALUATION_PROVIDER

    def __init__(
        self,
        *,
        api_key: str | None,
        model_name: str,
    ) -> None:
        self.api_key = api_key
        self.model_name = model_name

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def evaluate(
        self,
        *,
        instructions: str,
        input_text: str,
        response_schema: dict[str, Any],
        response_schema_name: str,
    ) -> AutomaticEvaluationModelResponse:
        if not self.api_key:
            raise RuntimeError(
                "Kein OpenAI-API-Key für die automatische Vorbewertung "
                "verfügbar. Hinterlege OPENAI_API_KEY in der .env-Datei."
            )

        client = AsyncOpenAI(api_key=self.api_key)
        response = await client.responses.create(
            model=self.model_name,
            instructions=instructions,
            input=input_text,
            reasoning={
                "mode": OPENAI_EVALUATION_REASONING_MODE,
                "effort": OPENAI_EVALUATION_REASONING_EFFORT,
            },
            max_output_tokens=OPENAI_EVALUATION_MAX_OUTPUT_TOKENS,
            store=False,
            text={
                "verbosity": "high",
                "format": {
                    "type": "json_schema",
                    "name": response_schema_name,
                    "strict": True,
                    "schema": response_schema,
                },
            },
        )

        if response.status != "completed":
            raise RuntimeError(
                "Die automatische Vorbewertung wurde vom OpenAI-Modell "
                "nicht vollständig abgeschlossen."
            )

        response_text = response.output_text.strip()

        if not response_text:
            raise RuntimeError(
                "Das OpenAI-Modell hat keine auswertbare Vorbewertung "
                "zurückgegeben."
            )

        actual_model = (
            response.model.strip()
            if isinstance(response.model, str) and response.model.strip()
            else self.model_name
        )
        request_id = (
            response.id.strip()
            if isinstance(response.id, str) and response.id.strip()
            else None
        )

        return AutomaticEvaluationModelResponse(
            provider=self.provider_name,
            model=actual_model,
            text=response_text,
            provider_request_id=request_id,
        )
