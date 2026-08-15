from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone

from app.domain.feedback_evaluation import (
    AUTOMATIC_EVALUATION_TYPE,
    MANUAL_META_EVALUATION_RUBRIC,
    StoredFeedbackEvaluation,
    StoredFeedbackRun,
)
from app.services.feedback_evaluation_pdf_service import (
    FeedbackEvaluationPdfError,
    FeedbackEvaluationPdfService,
)


class FeedbackEvaluationPdfServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        created_at = datetime(2026, 8, 12, 16, 45, tzinfo=timezone.utc)
        scores = {
            "factual_correctness": 3,
            "transparency_reasoning": 2,
            "audience_context_fit": 1,
            "action_learning_activation": 0,
        }
        ratings = MANUAL_META_EVALUATION_RUBRIC.build_ratings(
            scores=scores,
            justifications={
                key: (
                    "Die Aussage wurde ausführlich gegen Aufgabe, Material, "
                    "Kriterien und Schülertext geprüft. Konkrete Textbezüge "
                    "machen den Befund nachvollziehbar; die verbleibende "
                    "Einschränkung ist eindeutig bezeichnet."
                )
                for key in scores
            },
        )
        self.evaluation = StoredFeedbackEvaluation(
            evaluation_id="12345678-1234-5678-1234-567812345678",
            feedback_run_id="87654321-4321-8765-4321-876543218765",
            created_at=created_at,
            evaluation_type=AUTOMATIC_EVALUATION_TYPE,
            evaluation_name="Prüfung für Gedicht & Quelle",
            rubric_version=MANUAL_META_EVALUATION_RUBRIC.version,
            ratings=ratings,
            evaluator_provider="openai",
            evaluator_model="gpt-5.6-sol",
            evaluator_reasoning_mode="pro",
            evaluator_reasoning_effort="high",
            evaluator_prompt_version="meta-evaluator-v1",
            source_evaluation_id=None,
            duration_ms=2345,
            queue_duration_ms=None,
            execution_duration_ms=2250,
            provider_request_id="resp_pdf_test",
        )
        self.feedback_run = StoredFeedbackRun(
            feedback_run_id=self.evaluation.feedback_run_id,
            task_id="task-1",
            rubric_id="rubric-1",
            created_at=datetime(
                2026,
                8,
                12,
                16,
                40,
                tzinfo=timezone.utc,
            ),
            selected_for_evaluation_at=created_at,
            provider="openai",
            model="gpt-5.6-sol",
            reasoning_effort="max",
            duration_ms=12500,
            queue_duration_ms=200,
            execution_duration_ms=12300,
            provider_request_id="resp_feedback_test",
            student_text="Dieser Schülertext darf nicht im PDF erscheinen.",
            original_text="Auch der Originaltext bleibt außerhalb des PDFs.",
            task_snapshot={
                "title": "Gedichtinterpretation: Frühling & Nacht",
                "rubric": {"title": "Fünf Kriterien"},
            },
            feedback_payload={"overall_feedback": "Gespeichertes Feedback"},
            evaluations=(self.evaluation,),
        )
        self.service = FeedbackEvaluationPdfService()

    def test_render_creates_downloadable_pdf_with_safe_filename(self) -> None:
        result = self.service.render(
            feedback_run=self.feedback_run,
            evaluation=self.evaluation,
        )

        self.assertEqual(
            result.filename,
            (
                "meta-bewertung-20260812-1645-"
                "prufung-fur-gedicht-quelle-12345678.pdf"
            ),
        )
        self.assertTrue(result.content.startswith(b"%PDF-"))
        self.assertTrue(result.content.rstrip().endswith(b"%%EOF"))
        self.assertGreater(len(result.content), 10_000)

    def test_render_rejects_evaluation_from_another_feedback_run(self) -> None:
        mismatched_evaluation = replace(
            self.evaluation,
            feedback_run_id="other-feedback-run",
        )

        with self.assertRaisesRegex(
            FeedbackEvaluationPdfError,
            "gehört nicht",
        ):
            self.service.render(
                feedback_run=self.feedback_run,
                evaluation=mismatched_evaluation,
            )


if __name__ == "__main__":
    unittest.main()
