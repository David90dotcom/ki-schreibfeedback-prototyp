from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone

from app.domain.rubric import FeedbackTask, Rubric, RubricCriterion
from app.llm.base import LLMResponse
from app.services.criterion_wise_rubric_feedback_service import (
    CRITERION_WISE_FEEDBACK_LABEL,
    CRITERION_WISE_FEEDBACK_MODE,
    CRITERION_WISE_PIPELINE_VERSION,
    CriterionWiseRubricFeedbackService,
)
from app.services.rubric_feedback_service import (
    RUBRIC_FEEDBACK_PROMPT_VERSION,
)


STUDENT_TEXT = (
    "In der Einleitung nenne ich den Titel. "
    "Später erkläre ich ein sprachliches Bild."
)


def _task() -> FeedbackTask:
    now = datetime.now(timezone.utc)
    return FeedbackTask(
        task_id="task-criterion-wise",
        title="Gedichtinterpretation",
        subject="Deutsch",
        grade_level="8",
        instructions="Interpretiere das Gedicht.",
        material="Ein kurzes Beispielgedicht.",
        rubric=Rubric(
            rubric_id="rubric-criterion-wise",
            title="Zwei Kriterien",
            criteria=(
                RubricCriterion(
                    criterion_id="criterion-introduction",
                    title="Einleitung",
                    text="Nenne Titel und Autor.",
                    position=0,
                ),
                RubricCriterion(
                    criterion_id="criterion-language",
                    title="Sprachliche Gestaltung",
                    text="Erläutere ein sprachliches Bild.",
                    position=1,
                ),
            ),
        ),
        created_at=now,
        updated_at=now,
    )


def _response(
    *,
    status: str,
    quote: str,
    feedback: str,
    next_step: str,
) -> str:
    return json.dumps(
        {
            "criteria": [
                {
                    "criterion_id": "K1",
                    "status": status,
                    "evidence_quotes": [quote],
                    "feedback": feedback,
                    "next_step": next_step,
                }
            ],
            "overall_feedback": "Getrennter Einzelbefund.",
        },
        ensure_ascii=False,
    )


class _QueuedProvider:
    provider_name = "ollama"
    model_name = "mistral-small3.2:24b-instruct-2506-q8_0"

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.schema_names: list[str] = []

    async def generate(
        self,
        prompt: str,
        *,
        response_schema: dict[str, object] | None = None,
        response_schema_name: str = "structured_response",
    ) -> LLMResponse:
        del response_schema
        self.prompts.append(prompt)
        self.schema_names.append(response_schema_name)
        position = len(self.prompts)

        return LLMResponse(
            provider=self.provider_name,
            model=self.model_name,
            text=self.responses.pop(0),
            queue_duration_ms=10.0 * position,
            execution_duration_ms=20.0 * position,
            provider_request_id=f"request-{position}",
            worker_id=f"worker-{position}",
            raw_metadata={"reasoning_effort": "high"},
        )


class CriterionWiseRubricFeedbackServiceTests(unittest.TestCase):
    def test_analyzes_one_selected_criterion(self) -> None:
        provider = _QueuedProvider(
            _response(
                status="mostly_met",
                quote="erkläre ich ein sprachliches Bild",
                feedback="Die Erklärung ist nachvollziehbar.",
                next_step="Präzisiere die Wirkung.",
            )
        )
        service = CriterionWiseRubricFeedbackService(
            providers={"ollama": provider},
            max_input_chars=8000,
        )

        result = asyncio.run(
            service.analyze_criterion(
                student_text=STUDENT_TEXT,
                task=_task(),
                criterion_id="criterion-language",
                original_text="Ein konkreter Originaltext.",
                provider_key="ollama",
            )
        )

        self.assertEqual(len(provider.prompts), 1)
        self.assertIn(
            "Erläutere ein sprachliches Bild.",
            provider.prompts[0],
        )
        self.assertNotIn(
            "Nenne Titel und Autor.",
            provider.prompts[0],
        )
        self.assertEqual(
            result.criteria_feedback[0].criterion_id,
            "criterion-language",
        )

    def test_uses_one_focused_request_per_criterion(self) -> None:
        provider = _QueuedProvider(
            _response(
                status="partially_met",
                quote="nenne ich den Titel",
                feedback="Du nennst den Titel, aber der Autor fehlt.",
                next_step="Ergänze den Autor in der Einleitung.",
            ),
            _response(
                status="mostly_met",
                quote="erkläre ich ein sprachliches Bild",
                feedback="Du erläuterst ein sprachliches Bild.",
                next_step="Prüfe die Wirkung noch genauer.",
            ),
        )
        service = CriterionWiseRubricFeedbackService(
            providers={"ollama": provider},
            max_input_chars=8000,
        )

        result = asyncio.run(
            service.analyze_text(
                student_text=STUDENT_TEXT,
                task=_task(),
                original_text="Ein konkreter Originaltext.",
                provider_key="ollama",
            )
        )

        self.assertEqual(len(provider.prompts), 2)
        self.assertEqual(
            provider.schema_names,
            ["rubric_feedback", "rubric_feedback"],
        )
        self.assertIn(STUDENT_TEXT, provider.prompts[0])
        self.assertIn(STUDENT_TEXT, provider.prompts[1])
        self.assertIn("Nenne Titel und Autor.", provider.prompts[0])
        self.assertNotIn(
            "Erläutere ein sprachliches Bild.",
            provider.prompts[0],
        )
        self.assertIn(
            "Erläutere ein sprachliches Bild.",
            provider.prompts[1],
        )
        self.assertNotIn("Nenne Titel und Autor.", provider.prompts[1])
        self.assertEqual(
            [item.criterion_id for item in result.criteria_feedback],
            ["criterion-introduction", "criterion-language"],
        )
        self.assertEqual(result.pipeline_mode, CRITERION_WISE_FEEDBACK_MODE)
        self.assertEqual(result.pipeline_label, CRITERION_WISE_FEEDBACK_LABEL)
        self.assertEqual(result.prompt_version, CRITERION_WISE_PIPELINE_VERSION)
        self.assertEqual(result.criterion_request_count, 2)
        self.assertEqual(result.criterion_prompt_version, RUBRIC_FEEDBACK_PROMPT_VERSION)
        self.assertEqual(
            result.criterion_provider_request_ids,
            ("request-1", "request-2"),
        )
        self.assertEqual(result.queue_duration_ms, 30.0)
        self.assertEqual(result.execution_duration_ms, 60.0)
        self.assertEqual(result.provider_request_id, "request-2")
        context = result.payload()["generation_context"]
        self.assertEqual(context["criterion_requests"]["count"], 2)
        self.assertEqual(
            context["criterion_requests"]["prompt_version"],
            RUBRIC_FEEDBACK_PROMPT_VERSION,
        )

    def test_invalid_single_response_does_not_discard_other_criteria(
        self,
    ) -> None:
        provider = _QueuedProvider(
            "kein gültiges JSON",
            _response(
                status="mostly_met",
                quote="erkläre ich ein sprachliches Bild",
                feedback="Die Erklärung ist nachvollziehbar.",
                next_step="Präzisiere die Wirkung.",
            ),
        )
        service = CriterionWiseRubricFeedbackService(
            providers={"ollama": provider},
            max_input_chars=8000,
        )

        result = asyncio.run(
            service.analyze_text(
                student_text=STUDENT_TEXT,
                task=_task(),
                provider_key="ollama",
            )
        )

        self.assertEqual(len(provider.prompts), 2)
        self.assertEqual(
            result.criteria_feedback[0].status,
            "not_assessable",
        )
        self.assertFalse(result.criteria_feedback[0].evidence_verified)
        self.assertEqual(
            result.criteria_feedback[1].status,
            "mostly_met",
        )
        self.assertEqual(len(result.evidence_warnings), 1)
        self.assertIn("K1 – Einleitung", result.evidence_warnings[0])
        self.assertEqual(
            result.criterion_provider_request_ids,
            (None, "request-2"),
        )

    def test_aggregates_one_successful_evidence_repair(self) -> None:
        provider = _QueuedProvider(
            _response(
                status="partially_met",
                quote="Nur sinngemäß formulierter Beleg",
                feedback="Der Titel ist vorhanden.",
                next_step="Ergänze den Autor.",
            ),
            _response(
                status="partially_met",
                quote="nenne ich den Titel",
                feedback="Der Titel ist vorhanden.",
                next_step="Ergänze den Autor.",
            ),
            _response(
                status="mostly_met",
                quote="erkläre ich ein sprachliches Bild",
                feedback="Das sprachliche Bild wird erläutert.",
                next_step="Präzisiere die Wirkung.",
            ),
        )
        service = CriterionWiseRubricFeedbackService(
            providers={"ollama": provider},
            max_input_chars=8000,
        )

        result = asyncio.run(
            service.analyze_text(
                student_text=STUDENT_TEXT,
                task=_task(),
                provider_key="ollama",
            )
        )

        self.assertEqual(len(provider.prompts), 3)
        self.assertIn("Reparaturdurchgang", provider.prompts[1])
        self.assertEqual(
            [item.status for item in result.criteria_feedback],
            ["partially_met", "mostly_met"],
        )
        self.assertEqual(result.evidence_warnings, ())
        self.assertEqual(result.criterion_request_count, 2)
        self.assertEqual(
            result.criterion_provider_request_ids,
            ("request-2", "request-3"),
        )
        self.assertEqual(result.queue_duration_ms, 60.0)
        self.assertEqual(result.execution_duration_ms, 120.0)
        self.assertEqual(len(result.evidence_repair_attempts), 1)
        self.assertTrue(
            result.evidence_repair_attempts[0].resolved_to_assessable
        )
        context = result.payload()["generation_context"]
        self.assertEqual(context["evidence_repair"]["attempt_count"], 1)


if __name__ == "__main__":
    unittest.main()
