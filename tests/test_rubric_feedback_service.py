from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone

from app.domain.rubric import (
    FeedbackTask,
    Rubric,
    RubricCriterion,
)
from app.llm.base import LLMResponse
from app.services.rubric_feedback_service import (
    RubricFeedbackError,
    RubricFeedbackService,
)


class _RubricProvider:
    provider_name = "mistral"
    model_name = "mistral-small-latest"

    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.prompts: list[str] = []

    async def generate(self, prompt: str) -> LLMResponse:
        self.prompts.append(prompt)
        return LLMResponse(
            provider=self.provider_name,
            model=self.model_name,
            text=self.response_text,
            queue_duration_ms=10.0,
            execution_duration_ms=25.0,
            provider_request_id="request-123",
            worker_id="worker-456",
        )


def _task() -> FeedbackTask:
    timestamp = datetime.now(timezone.utc)
    return FeedbackTask(
        task_id="task-1",
        title="Gedichtinterpretation",
        subject="Deutsch",
        grade_level="8",
        instructions="Interpretiere das Gedicht.",
        material="Das Beispielgedicht",
        rubric=Rubric(
            rubric_id="rubric-1",
            title="Bewertungsbogen Gedichtinterpretation",
            criteria=(
                RubricCriterion(
                    criterion_id="criterion-1",
                    text="Einleitung mit Titel und Autor",
                    position=0,
                ),
                RubricCriterion(
                    criterion_id="criterion-2",
                    text="Sprachliche Bilder erläutern",
                    position=1,
                ),
            ),
        ),
        created_at=timestamp,
        updated_at=timestamp,
    )


class RubricFeedbackServiceTests(unittest.TestCase):
    def test_uses_one_request_and_orders_results_by_rubric(self) -> None:
        provider = _RubricProvider(
            json.dumps(
                {
                    "criteria": [
                        {
                            "criterion_id": "criterion-2",
                            "status": "not_met",
                            "feedback": "Sprachliche Bilder fehlen.",
                            "next_step": "Suche und erläutere ein Bild.",
                        },
                        {
                            "criterion_id": "criterion-1",
                            "status": "partially_met",
                            "feedback": "Der Titel ist vorhanden.",
                            "next_step": "Ergänze den Autor.",
                        },
                    ],
                    "overall_feedback": "Die Grundidee ist erkennbar.",
                },
                ensure_ascii=False,
            )
        )
        service = RubricFeedbackService(
            providers={"mistral": provider},
            max_input_chars=8000,
        )

        result = asyncio.run(
            service.analyze_text(
                student_text="Ein anonymisierter Schülertext.",
                task=_task(),
                provider_key="mistral",
            )
        )

        self.assertEqual(len(provider.prompts), 1)
        self.assertIn("Interpretiere das Gedicht.", provider.prompts[0])
        self.assertIn("Das Beispielgedicht", provider.prompts[0])
        self.assertIn("criterion-1", provider.prompts[0])
        self.assertEqual(
            [item.criterion_id for item in result.criteria_feedback],
            ["criterion-1", "criterion-2"],
        )
        self.assertEqual(
            result.criteria_feedback[0].status_label,
            "Teilweise erfüllt",
        )
        self.assertEqual(result.provider_request_id, "request-123")
        self.assertEqual(result.worker_id, "worker-456")
        self.assertEqual(
            result.payload()["overall_feedback"],
            "Die Grundidee ist erkennbar.",
        )

    def test_accepts_json_inside_optional_code_fence(self) -> None:
        provider = _RubricProvider(
            """```json
{
  "criteria": [
    {
      "criterion_id": "criterion-1",
      "status": "met",
      "feedback": "Die Einleitung ist vollständig.",
      "next_step": "Behalte diese klare Einleitung bei."
    },
    {
      "criterion_id": "criterion-2",
      "status": "not_assessable",
      "feedback": "Das Kriterium ist nicht beurteilbar.",
      "next_step": "Prüfe die Aufgabenstellung."
    }
  ],
  "overall_feedback": "Strukturierte Rückmeldung."
}
```"""
        )
        service = RubricFeedbackService(
            providers={"mistral": provider},
            max_input_chars=8000,
        )

        result = asyncio.run(
            service.analyze_text(
                student_text="Schülertext",
                task=_task(),
                provider_key="mistral",
            )
        )

        self.assertEqual(result.criteria_feedback[0].status, "met")

    def test_rejects_missing_or_unknown_criteria(self) -> None:
        provider = _RubricProvider(
            json.dumps(
                {
                    "criteria": [
                        {
                            "criterion_id": "criterion-1",
                            "status": "met",
                            "feedback": "Erfüllt.",
                            "next_step": "Beibehalten.",
                        }
                    ],
                    "overall_feedback": "Unvollständig.",
                }
            )
        )
        service = RubricFeedbackService(
            providers={"mistral": provider},
            max_input_chars=8000,
        )

        with self.assertRaisesRegex(
            RubricFeedbackError,
            "nicht zu jedem",
        ):
            asyncio.run(
                service.analyze_text(
                    student_text="Schülertext",
                    task=_task(),
                    provider_key="mistral",
                )
            )

    def test_existing_input_limit_also_applies_to_rubric_feedback(
        self,
    ) -> None:
        provider = _RubricProvider("{}")
        service = RubricFeedbackService(
            providers={"mistral": provider},
            max_input_chars=5,
        )

        with self.assertRaisesRegex(ValueError, "maximal 5"):
            asyncio.run(
                service.analyze_text(
                    student_text="zu langer Text",
                    task=_task(),
                    provider_key="mistral",
                )
            )

        self.assertEqual(provider.prompts, [])
