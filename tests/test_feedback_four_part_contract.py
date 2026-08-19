from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone

from app.domain.rubric import FeedbackTask, Rubric, RubricCriterion
from app.llm.base import LLMResponse
from app.services.rubric_feedback_service import RubricFeedbackService
from app.services.student_feedback_sections import (
    StudentFeedbackSections,
    feedback_heading_for_criterion,
)


class _Provider:
    provider_name = "ollama"
    model_name = "test-model"

    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.prompts: list[str] = []
        self.schemas: list[dict[str, object] | None] = []

    async def generate(
        self,
        prompt: str,
        *,
        response_schema: dict[str, object] | None = None,
        response_schema_name: str = "structured_response",
    ) -> LLMResponse:
        self.prompts.append(prompt)
        self.schemas.append(response_schema)
        return LLMResponse(
            provider=self.provider_name,
            model=self.model_name,
            text=self.response_text,
            provider_request_id="request-four-part",
        )


def _task() -> FeedbackTask:
    now = datetime.now(timezone.utc)
    return FeedbackTask(
        task_id="task-four-part",
        title="Gedichtinterpretation",
        subject="Deutsch",
        grade_level="8",
        instructions="Interpretiere das Gedicht.",
        material="Ein kurzes Beispielgedicht.",
        rubric=Rubric(
            rubric_id="rubric-four-part",
            title="Interpretationsfeedback",
            criteria=(
                RubricCriterion(
                    criterion_id="criterion-language",
                    title="Sprachliche Mittel und Wirkung",
                    text=(
                        "Deute sprachliche Mittel und erkläre ihre Wirkung."
                    ),
                    position=0,
                ),
            ),
        ),
        created_at=now,
        updated_at=now,
    )


class StudentFeedbackSectionsTests(unittest.TestCase):
    def test_accepts_any_useful_number_of_formulation_helps(self) -> None:
        formulation_helps = [
            f"Offener Satzanfang {index}: ..."
            for index in range(1, 7)
        ]

        sections = StudentFeedbackSections.from_payload(
            {
                "staerke": "Belegte Stärke.",
                "rueckmeldung": "Kriterienbezogene Rückmeldung.",
                "naechster_schritt": "Prüfe die Verbindung im Text.",
                "formulierungshilfen": formulation_helps,
            }
        )

        self.assertEqual(
            sections.formulierungshilfen,
            tuple(formulation_helps),
        )

    def test_selects_criterion_specific_headings(self) -> None:
        self.assertEqual(
            feedback_heading_for_criterion(
                criterion_title="Sprachliche Mittel",
                criterion_text="Erkläre ihre Wirkung.",
            ),
            "Deine Interpretation",
        )
        self.assertEqual(
            feedback_heading_for_criterion(
                criterion_title="Rechtschreibung und Grammatik",
                criterion_text="Schreibe sprachlich richtig.",
            ),
            "Deine sprachliche Richtigkeit",
        )
        self.assertEqual(
            feedback_heading_for_criterion(
                criterion_title="Aufbau",
                criterion_text="Gliedere den Text nachvollziehbar.",
            ),
            "Dein Aufbau",
        )
        self.assertEqual(
            feedback_heading_for_criterion(
                criterion_title="Ausdruck",
                criterion_text="Erkläre die Wirkung deiner Wortwahl.",
            ),
            "Dein Ausdruck",
        )
        self.assertEqual(
            feedback_heading_for_criterion(
                criterion_title="Inhalt",
                criterion_text="Gib die zentralen Gedanken wieder.",
            ),
            "Deine inhaltliche Darstellung",
        )

    def test_formats_sections_in_required_order(self) -> None:
        rendered = StudentFeedbackSections(
            staerke="Du nennst die Metapher mit einem passenden Beleg.",
            rueckmeldung=(
                "Du beschreibst ihre Wirkung, erklärst den Zusammenhang "
                "aber noch nicht vollständig."
            ),
            naechster_schritt=(
                "Verknüpfe die Metapher mit deiner Aussage zur Hektik."
            ),
            formulierungshilfen=(
                "Durch ... wird deutlich, dass ...",
                "An ... lässt sich erkennen, dass ...",
            ),
        ).as_markdown(
            criterion_title="Sprachliche Mittel",
            criterion_text="Erkläre ihre Wirkung.",
        )

        headings = [
            "Das gelingt dir schon:",
            "Deine Interpretation:",
            "Daran kannst du weiterarbeiten:",
            "Formulierungshilfe:",
        ]
        positions = [rendered.index(heading) for heading in headings]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("1. Durch ... wird deutlich, dass ...", rendered)
        self.assertIn("2. An ... lässt sich erkennen, dass ...", rendered)


class FourPartRubricFeedbackContractTests(unittest.TestCase):
    def test_uses_new_schema_and_preserves_one_request(self) -> None:
        response_text = json.dumps(
            {
                "criteria": [
                    {
                        "criterion_id": "K1",
                        "status": "mostly_met",
                        "evidence_quotes": [
                            "Die Metapher zeigt die Hektik der Stadt."
                        ],
                        "staerke": (
                            "Du beziehst die Metapher auf die Hektik der "
                            "Stadt."
                        ),
                        "rueckmeldung": (
                            "Du nennst bereits eine plausible Wirkung, "
                            "erklärst den sprachlichen Zusammenhang aber "
                            "noch nicht vollständig."
                        ),
                        "naechster_schritt": (
                            "Erkläre, wodurch die Metapher die Hektik "
                            "sichtbar macht."
                        ),
                        "formulierungshilfen": [
                            "Durch ... wird deutlich, dass ..."
                        ],
                    }
                ],
                "overall_feedback": "Bearbeite den priorisierten Schritt.",
            },
            ensure_ascii=False,
        )
        provider = _Provider(response_text)
        service = RubricFeedbackService(
            providers={"ollama": provider},
            max_input_chars=8000,
        )

        result = asyncio.run(
            service.analyze_text(
                student_text=(
                    "Die Metapher zeigt die Hektik der Stadt."
                ),
                task=_task(),
                original_text="Ein kurzes Beispielgedicht.",
                provider_key="ollama",
            )
        )

        self.assertEqual(len(provider.prompts), 1)
        self.assertIn("1. EVIDENCE", provider.prompts[0])
        self.assertIn("4. VERIFICATION", provider.prompts[0])
        self.assertIn("Durch [X] wird deutlich", provider.prompts[0])
        self.assertIn('"staerke"', provider.prompts[0])

        schema = provider.schemas[0]
        assert schema is not None
        criterion_schema = schema["properties"]["criteria"]["items"]
        required = criterion_schema["required"]
        self.assertIn("staerke", required)
        self.assertIn("rueckmeldung", required)
        self.assertIn("naechster_schritt", required)
        self.assertIn("formulierungshilfen", required)
        self.assertNotIn("feedback", required)
        self.assertNotIn("next_step", required)
        formulation_schema = criterion_schema["properties"][
            "formulierungshilfen"
        ]
        self.assertNotIn("maxItems", formulation_schema)
        self.assertNotIn(
            "kein sicherer zusätzlicher Überarbeitungsschritt ermittelt",
            provider.prompts[0],
        )
        self.assertIn(
            "Weiterführungs- oder Qualitätssicherungsaktion",
            provider.prompts[0],
        )

        item = result.criteria_feedback[0]
        self.assertIn("Das gelingt dir schon:", item.display_feedback)
        self.assertIn("Deine Interpretation:", item.display_feedback)
        self.assertIn(
            "Daran kannst du weiterarbeiten:",
            item.display_feedback,
        )
        self.assertIn("Formulierungshilfe:", item.display_feedback)
        self.assertEqual(item.display_next_step, "")

        payload = item.payload()
        self.assertEqual(payload["feedback"], item.display_feedback)
        self.assertEqual(payload["next_step"], "")


if __name__ == "__main__":
    unittest.main()
