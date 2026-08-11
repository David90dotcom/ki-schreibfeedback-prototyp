from __future__ import annotations

import json
from dataclasses import dataclass
from time import perf_counter

from app.domain.rubric import FeedbackTask
from app.llm.base import LLMProvider


STATUS_LABELS = {
    "met": "Erfüllt",
    "partially_met": "Teilweise erfüllt",
    "not_met": "Nicht erfüllt",
    "not_assessable": "Nicht beurteilbar",
}


class RubricFeedbackError(ValueError):
    """Die Modellantwort ist kein gültiges Kriterienfeedback."""


@dataclass(frozen=True)
class CriterionFeedbackResult:
    criterion_id: str
    criterion_text: str
    status: str
    status_label: str
    feedback: str
    next_step: str

    def payload(self) -> dict[str, str]:
        return {
            "criterion_id": self.criterion_id,
            "criterion_text": self.criterion_text,
            "status": self.status,
            "feedback": self.feedback,
            "next_step": self.next_step,
        }


@dataclass(frozen=True)
class RubricFeedbackResult:
    provider: str
    model: str
    task_id: str
    task_title: str
    rubric_title: str
    criteria_feedback: tuple[CriterionFeedbackResult, ...]
    overall_feedback: str
    duration_ms: int
    queue_duration_ms: float | None = None
    execution_duration_ms: float | None = None
    provider_request_id: str | None = None
    worker_id: str | None = None

    def payload(self) -> dict[str, object]:
        return {
            "criteria": [
                item.payload()
                for item in self.criteria_feedback
            ],
            "overall_feedback": self.overall_feedback,
        }


class RubricFeedbackService:
    """Erzeugt in einer Anfrage Feedback zu allen Kriterien."""

    def __init__(
        self,
        *,
        providers: dict[str, LLMProvider],
        max_input_chars: int,
    ) -> None:
        self.providers = providers
        self.max_input_chars = max_input_chars

    async def analyze_text(
        self,
        *,
        student_text: str,
        task: FeedbackTask,
        provider_key: str,
        provider_override: LLMProvider | None = None,
    ) -> RubricFeedbackResult:
        cleaned_text = student_text.strip()

        if not cleaned_text:
            raise ValueError("Bitte gib einen Text ein.")
        if len(cleaned_text) > self.max_input_chars:
            raise ValueError(
                "Der Text ist zu lang. Erlaubt sind maximal "
                f"{self.max_input_chars} Zeichen."
            )
        if not task.rubric.criteria:
            raise ValueError(
                "Der ausgewählte Bewertungsbogen enthält keine Kriterien."
            )

        provider = (
            provider_override
            or self.providers.get(provider_key)
        )

        if provider is None:
            raise ValueError(
                "Der ausgewählte Modellanbieter ist nicht bekannt."
            )

        prompt = self._build_prompt(
            student_text=cleaned_text,
            task=task,
        )
        started_at = perf_counter()
        response = await provider.generate(prompt)
        duration_ms = int(
            (perf_counter() - started_at) * 1000
        )
        criteria_feedback, overall_feedback = self._parse_response(
            response.text,
            task,
        )

        return RubricFeedbackResult(
            provider=response.provider,
            model=response.model,
            task_id=task.task_id,
            task_title=task.title,
            rubric_title=task.rubric.title,
            criteria_feedback=criteria_feedback,
            overall_feedback=overall_feedback,
            duration_ms=duration_ms,
            queue_duration_ms=response.queue_duration_ms,
            execution_duration_ms=response.execution_duration_ms,
            provider_request_id=response.provider_request_id,
            worker_id=response.worker_id,
        )

    @staticmethod
    def _build_prompt(
        *,
        student_text: str,
        task: FeedbackTask,
    ) -> str:
        analysis_input = {
            "task": task.snapshot(),
            "student_text": student_text,
        }
        serialized_input = json.dumps(
            analysis_input,
            ensure_ascii=False,
            indent=2,
        )

        return f"""
Du analysierst einen anonymisierten Schülertext ausschließlich anhand der
übermittelten Aufgabe und der Bewertungskriterien.

Erzeuge zu jedem Kriterium genau eine eigene Rückmeldung. Beurteile nur, was
am Schülertext tatsächlich erkennbar ist. Erfinde keine Textbelege und schreibe
keine fertige Musterlösung. Formuliere verständlich, wertschätzend, konkret und
handlungsorientiert.

Antworte ausschließlich als gültiges JSON-Objekt ohne Markdown-Codeblock und
ohne zusätzlichen Text. Verwende exakt diese Struktur:

{{
  "criteria": [
    {{
      "criterion_id": "ID aus der Eingabe",
      "status": "met | partially_met | not_met | not_assessable",
      "feedback": "konkrete Rückmeldung zum Kriterium",
      "next_step": "konkreter nächster Überarbeitungsschritt"
    }}
  ],
  "overall_feedback": "kurze zusammenfassende Rückmeldung"
}}

Jede criterion_id aus der Eingabe muss genau einmal vorkommen. Füge keine
eigenen Kriterien hinzu.

Eingabe:
{serialized_input}
""".strip()

    def _parse_response(
        self,
        response_text: str,
        task: FeedbackTask,
    ) -> tuple[tuple[CriterionFeedbackResult, ...], str]:
        cleaned_response = self._remove_optional_code_fence(
            response_text
        )

        try:
            payload = json.loads(cleaned_response)
        except json.JSONDecodeError as exc:
            raise RubricFeedbackError(
                "Die KI hat kein gültiges strukturiertes "
                "Kriterienfeedback zurückgegeben. Bitte versuche es "
                "erneut oder wähle ein anderes Modell."
            ) from exc

        if not isinstance(payload, dict):
            raise RubricFeedbackError(
                "Die KI-Antwort besitzt nicht das erwartete "
                "Kriterienformat."
            )

        raw_criteria = payload.get("criteria")

        if not isinstance(raw_criteria, list):
            raise RubricFeedbackError(
                "In der KI-Antwort fehlt die Liste der Kriterien."
            )

        expected_by_id = {
            criterion.criterion_id: criterion
            for criterion in task.rubric.criteria
        }
        parsed_by_id: dict[str, CriterionFeedbackResult] = {}

        for raw_item in raw_criteria:
            if not isinstance(raw_item, dict):
                raise RubricFeedbackError(
                    "Ein Kriterienergebnis besitzt ein ungültiges Format."
                )

            criterion_id = self._required_string(
                raw_item,
                "criterion_id",
            )

            if criterion_id not in expected_by_id:
                raise RubricFeedbackError(
                    "Die KI-Antwort enthält ein unbekanntes Kriterium."
                )
            if criterion_id in parsed_by_id:
                raise RubricFeedbackError(
                    "Die KI-Antwort enthält ein Kriterium mehrfach."
                )

            status = self._required_string(raw_item, "status")

            if status not in STATUS_LABELS:
                raise RubricFeedbackError(
                    "Die KI-Antwort enthält einen ungültigen "
                    "Erfüllungsstatus."
                )

            criterion = expected_by_id[criterion_id]
            parsed_by_id[criterion_id] = CriterionFeedbackResult(
                criterion_id=criterion_id,
                criterion_text=criterion.text,
                status=status,
                status_label=STATUS_LABELS[status],
                feedback=self._required_string(
                    raw_item,
                    "feedback",
                ),
                next_step=self._required_string(
                    raw_item,
                    "next_step",
                ),
            )

        if set(parsed_by_id) != set(expected_by_id):
            raise RubricFeedbackError(
                "Die KI hat nicht zu jedem Bewertungskriterium eine "
                "Rückmeldung erzeugt."
            )

        overall_feedback = self._required_string(
            payload,
            "overall_feedback",
        )
        ordered_feedback = tuple(
            parsed_by_id[criterion.criterion_id]
            for criterion in task.rubric.criteria
        )

        return ordered_feedback, overall_feedback

    @staticmethod
    def _required_string(
        payload: dict[str, object],
        key: str,
    ) -> str:
        value = payload.get(key)

        if not isinstance(value, str) or not value.strip():
            raise RubricFeedbackError(
                f"In der KI-Antwort fehlt das Feld '{key}'."
            )

        cleaned = value.strip()

        if len(cleaned) > 10000:
            raise RubricFeedbackError(
                f"Das Feld '{key}' in der KI-Antwort ist zu lang."
            )

        return cleaned

    @staticmethod
    def _remove_optional_code_fence(text: str) -> str:
        cleaned = text.strip()

        if not cleaned.startswith("```"):
            return cleaned

        lines = cleaned.splitlines()

        if len(lines) < 3 or not lines[-1].strip().startswith("```"):
            return cleaned

        return "\n".join(lines[1:-1]).strip()
