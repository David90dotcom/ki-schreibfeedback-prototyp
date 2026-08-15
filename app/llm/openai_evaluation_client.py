from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from openai import AsyncOpenAI

from app.llm.openai_client import OPENAI_REASONING_EFFORTS


OPENAI_EVALUATION_PROVIDER = "openai"
OPENAI_EVALUATION_REASONING_MODE: str | None = None
OPENAI_EVALUATION_REASONING_EFFORT = "medium"
OPENAI_EVALUATION_REASONING_MODES = ("pro",)
OPENAI_EVALUATION_MAX_OUTPUT_TOKENS = 25000


@dataclass(frozen=True)
class AutomaticEvaluationModelResponse:
    """Für die Meta-Bewertung benötigte Teilmenge einer Modellantwort."""

    provider: str
    model: str
    text: str
    provider_request_id: str | None
    reasoning_mode: str | None = None
    reasoning_effort: str | None = None


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
        model_name: str | None = None,
        reasoning_mode: str | None = OPENAI_EVALUATION_REASONING_MODE,
        reasoning_effort: str | None = (
            OPENAI_EVALUATION_REASONING_EFFORT
        ),
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
        model_name: str | None = None,
        reasoning_mode: str | None = OPENAI_EVALUATION_REASONING_MODE,
        reasoning_effort: str | None = (
            OPENAI_EVALUATION_REASONING_EFFORT
        ),
    ) -> AutomaticEvaluationModelResponse:
        if not self.api_key:
            raise RuntimeError(
                "Kein OpenAI-API-Key für die automatische Vorbewertung "
                "verfügbar. Hinterlege OPENAI_API_KEY in der .env-Datei."
            )

        effective_model = (model_name or self.model_name).strip()
        normalized_reasoning_mode = (
            reasoning_mode.strip().lower()
            if isinstance(reasoning_mode, str)
            else ""
        )
        normalized_reasoning_effort = (
            reasoning_effort.strip().lower()
            if isinstance(reasoning_effort, str)
            else ""
        )

        if not effective_model:
            raise ValueError("Das Bewertungsmodell darf nicht leer sein.")
        if (
            normalized_reasoning_mode
            and normalized_reasoning_mode
            not in OPENAI_EVALUATION_REASONING_MODES
        ):
            raise ValueError("Der ausgewählte Denkmodus ist ungültig.")
        if (
            normalized_reasoning_effort
            and normalized_reasoning_effort
            not in OPENAI_REASONING_EFFORTS
        ):
            raise ValueError("Der ausgewählte Denkaufwand ist ungültig.")

        request: dict[str, Any] = {
            "model": effective_model,
            "instructions": instructions,
            "input": input_text,
            "max_output_tokens": OPENAI_EVALUATION_MAX_OUTPUT_TOKENS,
            "store": False,
            "text": {
                "verbosity": "high",
                "format": {
                    "type": "json_schema",
                    "name": response_schema_name,
                    "strict": True,
                    "schema": response_schema,
                },
            },
        }
        reasoning: dict[str, str] = {}

        if normalized_reasoning_mode:
            reasoning["mode"] = normalized_reasoning_mode
        if normalized_reasoning_effort:
            reasoning["effort"] = normalized_reasoning_effort
        if reasoning:
            request["reasoning"] = reasoning

        client = AsyncOpenAI(api_key=self.api_key)
        response = await client.responses.create(**request)

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
            else effective_model
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
            reasoning_mode=normalized_reasoning_mode or None,
            reasoning_effort=normalized_reasoning_effort or None,
        )
