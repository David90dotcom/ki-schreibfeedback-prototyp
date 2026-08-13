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
    EVIDENCE_VALIDATION_VERSION,
    PARTIAL_RESULT_OVERALL_FEEDBACK,
    RUBRIC_FEEDBACK_MODE,
    RUBRIC_FEEDBACK_PROMPT_VERSION,
    UNVERIFIED_CRITERION_FEEDBACK,
    UNVERIFIED_CRITERION_NEXT_STEP,
    RubricFeedbackError,
    RubricFeedbackService,
)


FIRST_CRITERION_ID = "b5af67d6-9b38-4f42-a289-8114c2eb061e"
SECOND_CRITERION_ID = "769d500a-d4ca-4353-93c0-7277c80d77a4"


class _RubricProvider:
    def __init__(
        self,
        response_text: str,
        *,
        provider_name: str = "mistral",
        model_name: str = "mistral-small-latest",
        finish_reason: str | None = None,
    ) -> None:
        self.response_text = response_text
        self.provider_name = provider_name
        self.model_name = model_name
        self.finish_reason = finish_reason
        self.prompts: list[str] = []
        self.response_schemas: list[dict[str, object] | None] = []
        self.response_schema_names: list[str] = []

    async def generate(
        self,
        prompt: str,
        *,
        response_schema: dict[str, object] | None = None,
        response_schema_name: str = "structured_response",
    ) -> LLMResponse:
        self.prompts.append(prompt)
        self.response_schemas.append(response_schema)
        self.response_schema_names.append(response_schema_name)
        return LLMResponse(
            provider=self.provider_name,
            model=self.model_name,
            text=self.response_text,
            queue_duration_ms=10.0,
            execution_duration_ms=25.0,
            provider_request_id="request-123",
            worker_id="worker-456",
            raw_metadata={
                "finish_reason": self.finish_reason,
            },
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
            title="Feedback Gedichtinterpretation",
            criteria=(
                RubricCriterion(
                    criterion_id=FIRST_CRITERION_ID,
                    title="Einleitung: Grundangaben",
                    text="Einleitung mit Titel und Autor",
                    position=0,
                ),
                RubricCriterion(
                    criterion_id=SECOND_CRITERION_ID,
                    title="Sprache: Bildlichkeit",
                    text="Sprachliche Bilder erläutern",
                    position=1,
                ),
            ),
        ),
        created_at=timestamp,
        updated_at=timestamp,
    )


def _response_with_references(*references: str) -> str:
    return json.dumps(
        {
            "criteria": [
                {
                    "criterion_id": reference,
                    "status": "met",
                    "evidence_quotes": ["Schülertext"],
                    "feedback": "Erfüllt.",
                    "next_step": "Beibehalten.",
                }
                for reference in references
            ],
            "overall_feedback": "Zusammenfassung.",
        }
    )


class RubricFeedbackServiceTests(unittest.TestCase):
    def test_uses_one_request_and_orders_results_by_rubric(self) -> None:
        provider = _RubricProvider(
            json.dumps(
                {
                    "criteria": [
                        {
                            "criterion_id": "K2",
                            "status": "not_met",
                            "evidence_quotes": [
                                "Ein anonymisierter Schülertext."
                            ],
                            "feedback": "Sprachliche Bilder fehlen.",
                            "next_step": "Suche und erläutere ein Bild.",
                        },
                        {
                            "criterion_id": "K1",
                            "status": "mostly_met",
                            "evidence_quotes": [
                                "anonymisierter Schülertext"
                            ],
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
                original_text="Ein laufbezogenes Beispielgedicht.",
                provider_key="mistral",
            )
        )

        self.assertEqual(len(provider.prompts), 1)
        self.assertIn("Interpretiere das Gedicht.", provider.prompts[0])
        self.assertIn("Das Beispielgedicht", provider.prompts[0])
        self.assertIn(
            "Ein laufbezogenes Beispielgedicht.",
            provider.prompts[0],
        )
        self.assertIn(
            '"original_text_for_this_run"',
            provider.prompts[0],
        )
        self.assertIn(
            "unabhängig",
            provider.prompts[0],
        )
        self.assertIn('"criterion_id": "K1"', provider.prompts[0])
        self.assertIn('"criterion_id": "K2"', provider.prompts[0])
        self.assertNotIn(FIRST_CRITERION_ID, provider.prompts[0])
        self.assertNotIn(SECOND_CRITERION_ID, provider.prompts[0])
        self.assertNotIn(
            "Einleitung: Grundangaben",
            provider.prompts[0],
        )
        self.assertIn("höchstens drei kurze", provider.prompts[0])
        self.assertIn(
            "Klartext ohne Markdown-Markierungen",
            provider.prompts[0],
        )
        self.assertIn(
            "keine Sternchen",
            provider.prompts[0],
        )
        self.assertIn(
            "mostly_met = überwiegend erfüllt",
            provider.prompts[0],
        )
        self.assertIn(
            "Technische Statuswerte gehören ausschließlich",
            provider.prompts[0],
        )
        self.assertIn(
            "Ausschließlich student_text zeigt",
            provider.prompts[0],
        )
        self.assertIn(
            "Ein Kriterium ist niemals ein Beleg",
            provider.prompts[0],
        )
        self.assertIn(
            "mit „Du hast ...“",
            provider.prompts[0],
        )
        self.assertIn(
            "wörtlich übernommene Ausschnitte",
            provider.prompts[0],
        )
        self.assertIn(
            "jeder Aussage, etwas „fehle“",
            provider.prompts[0],
        )
        self.assertIn(
            "keinen passenden Ausschnitt sicher und wörtlich",
            provider.prompts[0],
        )
        self.assertIn(
            "Empfehlung zur eigenen Kontrolle anhand des Kriteriums",
            provider.prompts[0],
        )
        self.assertIn(
            "inhaltlichen Überarbeitungshinweis",
            provider.prompts[0],
        )
        self.assertEqual(
            provider.response_schema_names,
            ["rubric_feedback"],
        )
        response_schema = provider.response_schemas[0]
        self.assertIsNotNone(response_schema)
        assert response_schema is not None
        properties = response_schema["properties"]
        assert isinstance(properties, dict)
        criteria_schema = properties["criteria"]
        assert isinstance(criteria_schema, dict)
        self.assertEqual(criteria_schema["minItems"], 2)
        self.assertEqual(criteria_schema["maxItems"], 2)
        item_schema = criteria_schema["items"]
        assert isinstance(item_schema, dict)
        item_properties = item_schema["properties"]
        assert isinstance(item_properties, dict)
        reference_schema = item_properties["criterion_id"]
        assert isinstance(reference_schema, dict)
        self.assertEqual(
            reference_schema["enum"],
            ["K1", "K2"],
        )
        status_schema = item_properties["status"]
        assert isinstance(status_schema, dict)
        self.assertEqual(
            status_schema["enum"],
            [
                "met",
                "mostly_met",
                "partially_met",
                "not_met",
                "not_assessable",
            ],
        )
        evidence_schema = item_properties["evidence_quotes"]
        assert isinstance(evidence_schema, dict)
        self.assertEqual(evidence_schema["minItems"], 0)
        self.assertEqual(evidence_schema["maxItems"], 3)
        self.assertIn(
            "evidence_quotes",
            item_schema["required"],
        )
        self.assertEqual(
            [item.criterion_id for item in result.criteria_feedback],
            [FIRST_CRITERION_ID, SECOND_CRITERION_ID],
        )
        self.assertEqual(
            result.criteria_feedback[0].status_label,
            "Überwiegend erfüllt",
        )
        self.assertEqual(
            result.criteria_feedback[0].criterion_title,
            "Einleitung: Grundangaben",
        )
        self.assertEqual(
            result.criteria_feedback[0].evidence_quotes,
            ("anonymisierter Schülertext",),
        )
        self.assertTrue(
            result.criteria_feedback[0].evidence_verified
        )
        self.assertEqual(result.evidence_warnings, ())
        self.assertEqual(result.provider_request_id, "request-123")
        self.assertEqual(result.worker_id, "worker-456")
        self.assertEqual(
            result.payload()["overall_feedback"],
            "Die Grundidee ist erkennbar.",
        )
        self.assertEqual(
            result.payload()["criteria"][0]["criterion_title"],
            "Einleitung: Grundangaben",
        )
        self.assertNotIn(
            "evidence_quotes",
            result.payload()["criteria"][0],
        )
        self.assertTrue(
            result.payload()["criteria"][0]["evidence_verified"]
        )
        generation_context = result.payload()["generation_context"]
        self.assertEqual(
            generation_context["mode"],
            RUBRIC_FEEDBACK_MODE,
        )
        self.assertEqual(
            generation_context["prompt_version"],
            RUBRIC_FEEDBACK_PROMPT_VERSION,
        )
        self.assertEqual(
            generation_context["evidence_validation"],
            EVIDENCE_VALIDATION_VERSION,
        )
        self.assertEqual(
            generation_context["validated_quote_count"],
            2,
        )
        self.assertEqual(
            generation_context["unverified_criterion_count"],
            0,
        )

    def test_accepts_json_inside_optional_code_fence(self) -> None:
        provider = _RubricProvider(
            """```json
{
  "criteria": [
    {
      "criterion_id": "K1",
      "status": "met",
      "evidence_quotes": ["Schülertext"],
      "feedback": "Die Einleitung ist vollständig.",
      "next_step": "Behalte diese klare Einleitung bei."
    },
    {
      "criterion_id": "K2",
      "status": "not_assessable",
      "evidence_quotes": [],
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
        self.assertEqual(result.evidence_warnings, ())

    def test_accepts_only_normalized_verbatim_student_quotes(self) -> None:
        provider = _RubricProvider(
            json.dumps(
                {
                    "criteria": [
                        {
                            "criterion_id": "K1",
                            "status": "met",
                            "evidence_quotes": [
                                "„IN DEM GEDICHT: Luftveränderung – von "
                                "Kurt Tucholsky; aus dem Jahr 1924 geht "
                                "es um Reisen …“"
                            ],
                            "feedback": "Die Aussage ist vorhanden.",
                            "next_step": "Behalte sie bei.",
                        },
                        {
                            "criterion_id": "K2",
                            "status": "not_met",
                            "evidence_quotes": ["Danach endet es."],
                            "feedback": "Ein sprachliches Bild fehlt.",
                            "next_step": "Ergänze ein passendes Bild.",
                        },
                    ],
                    "overall_feedback": "Kurze Zusammenfassung.",
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
                student_text=(
                    "In dem Gedicht „Luftveränderung“, von Kurt "
                    "Tucholsky, aus dem Jahr 1924 geht es um Reisen.\n"
                    "Danach endet es."
                ),
                task=_task(),
                provider_key="mistral",
            )
        )

        self.assertEqual(
            result.criteria_feedback[0].evidence_quotes,
            (
                "„IN DEM GEDICHT: Luftveränderung – von Kurt "
                "Tucholsky; aus dem Jahr 1924 geht es um Reisen …“",
            ),
        )
        self.assertEqual(
            result.criteria_feedback[1].evidence_quotes,
            ("Danach endet es.",),
        )

    def test_replaces_unverified_quote_with_safe_partial_result(self) -> None:
        for quote, expected_error, shows_preview in (
            (
                "Das steht nur im Erwartungshorizont.",
                "nicht als zusammenhängende Wortfolge",
                True,
            ),
            (
                "Ein weiterer Schülertext.",
                "nicht als zusammenhängende Wortfolge",
                True,
            ),
            (
                "Ein anderer Schulertext.",
                "nicht als zusammenhängende Wortfolge",
                True,
            ),
            ("E", "zu kurz", False),
        ):
            with self.subTest(quote=quote):
                provider = _RubricProvider(
                    json.dumps(
                        {
                            "criteria": [
                                {
                                    "criterion_id": "K1",
                                    "status": "met",
                                    "evidence_quotes": [quote],
                                    "feedback": "Angeblich belegt.",
                                    "next_step": "Beibehalten.",
                                },
                                {
                                    "criterion_id": "K2",
                                    "status": "not_assessable",
                                    "evidence_quotes": [],
                                    "feedback": "Keine sichere Bewertung.",
                                    "next_step": "Prüfe das Kriterium.",
                                },
                            ],
                            "overall_feedback": "Zusammenfassung.",
                        }
                    )
                )
                service = RubricFeedbackService(
                    providers={"mistral": provider},
                    max_input_chars=8000,
                )

                result = asyncio.run(
                    service.analyze_text(
                        student_text="Ein anderer Schülertext.",
                        task=_task(),
                        provider_key="mistral",
                    )
                )

                first_result = result.criteria_feedback[0]
                self.assertEqual(first_result.status, "not_assessable")
                self.assertFalse(first_result.evidence_verified)
                self.assertEqual(
                    first_result.feedback,
                    UNVERIFIED_CRITERION_FEEDBACK,
                )
                self.assertEqual(
                    first_result.next_step,
                    UNVERIFIED_CRITERION_NEXT_STEP,
                )
                self.assertNotIn("Angeblich belegt", first_result.feedback)
                self.assertEqual(
                    result.overall_feedback,
                    PARTIAL_RESULT_OVERALL_FEEDBACK,
                )
                self.assertEqual(len(result.evidence_warnings), 1)
                self.assertIn(
                    expected_error,
                    result.evidence_warnings[0],
                )
                self.assertIn("K1 – Einleitung", result.evidence_warnings[0])

                if shows_preview:
                    self.assertIn(
                        f"Zurückgewiesener Beleg: „{quote}“",
                        result.evidence_warnings[0],
                    )

                payload = result.payload()
                self.assertFalse(
                    payload["criteria"][0]["evidence_verified"]
                )
                self.assertEqual(
                    payload["generation_context"][
                        "unverified_criterion_count"
                    ],
                    1,
                )

    def test_replaces_empty_assessable_evidence_with_safe_result(
        self,
    ) -> None:
        for status in (
            "met",
            "mostly_met",
            "partially_met",
            "not_met",
        ):
            with self.subTest(status=status):
                provider = _RubricProvider(
                    json.dumps(
                        {
                            "criteria": [
                                {
                                    "criterion_id": "K1",
                                    "status": status,
                                    "evidence_quotes": [],
                                    "feedback": "Angeblicher Befund.",
                                    "next_step": "Überarbeite den Text.",
                                },
                                {
                                    "criterion_id": "K2",
                                    "status": "not_assessable",
                                    "evidence_quotes": [],
                                    "feedback": "Keine sichere Bewertung.",
                                    "next_step": "Prüfe das Kriterium.",
                                },
                            ],
                            "overall_feedback": "Zusammenfassung.",
                        }
                    )
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

                first_result = result.criteria_feedback[0]
                self.assertEqual(first_result.status, "not_assessable")
                self.assertFalse(first_result.evidence_verified)
                self.assertEqual(
                    first_result.feedback,
                    UNVERIFIED_CRITERION_FEEDBACK,
                )
                self.assertNotIn(
                    "Überarbeite den Text",
                    first_result.next_step,
                )
                self.assertIn(
                    "keinen überprüfbaren Schülertextbeleg",
                    result.evidence_warnings[0],
                )

    def test_rejects_missing_short_reference(self) -> None:
        provider = _RubricProvider(
            _response_with_references("K1")
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

    def test_rejects_unknown_short_reference(self) -> None:
        provider = _RubricProvider(
            _response_with_references("K1", "K3")
        )
        service = RubricFeedbackService(
            providers={"mistral": provider},
            max_input_chars=8000,
        )

        with self.assertRaisesRegex(
            RubricFeedbackError,
            "unbekanntes Kriterium",
        ):
            asyncio.run(
                service.analyze_text(
                    student_text="Schülertext",
                    task=_task(),
                    provider_key="mistral",
                )
            )

    def test_rejects_duplicate_short_reference(self) -> None:
        provider = _RubricProvider(
            _response_with_references("K1", "K1")
        )
        service = RubricFeedbackService(
            providers={"mistral": provider},
            max_input_chars=8000,
        )

        with self.assertRaisesRegex(
            RubricFeedbackError,
            "mehrfach",
        ):
            asyncio.run(
                service.analyze_text(
                    student_text="Schülertext",
                    task=_task(),
                    provider_key="mistral",
                )
            )

    def test_reports_truncated_json_response_separately(self) -> None:
        provider = _RubricProvider(
            '{"criteria": [{"criterion_id": "K1"',
            finish_reason="length",
        )
        service = RubricFeedbackService(
            providers={"mistral": provider},
            max_input_chars=8000,
        )

        with self.assertRaisesRegex(
            RubricFeedbackError,
            "Ausgabelimit",
        ):
            asyncio.run(
                service.analyze_text(
                    student_text="Schülertext",
                    task=_task(),
                    provider_key="mistral",
                )
            )

    def test_keeps_generic_error_for_other_invalid_json(self) -> None:
        provider = _RubricProvider("Kein JSON")
        service = RubricFeedbackService(
            providers={"mistral": provider},
            max_input_chars=8000,
        )

        with self.assertRaisesRegex(
            RubricFeedbackError,
            "kein gültiges strukturiertes",
        ):
            asyncio.run(
                service.analyze_text(
                    student_text="Schülertext",
                    task=_task(),
                    provider_key="mistral",
                )
            )

    def test_short_references_apply_to_every_provider(self) -> None:
        response_text = _response_with_references("K1", "K2")

        for provider_key in (
            "ollama",
            "mistral",
            "openai",
            "runpod",
        ):
            with self.subTest(provider=provider_key):
                provider = _RubricProvider(
                    response_text,
                    provider_name=provider_key,
                    model_name="test-model",
                )
                service = RubricFeedbackService(
                    providers={provider_key: provider},
                    max_input_chars=8000,
                )

                result = asyncio.run(
                    service.analyze_text(
                        student_text="Schülertext",
                        task=_task(),
                        provider_key=provider_key,
                    )
                )

                self.assertEqual(result.provider, provider_key)
                self.assertEqual(
                    [
                        item.criterion_id
                        for item in result.criteria_feedback
                    ],
                    [FIRST_CRITERION_ID, SECOND_CRITERION_ID],
                )
                self.assertNotIn(
                    FIRST_CRITERION_ID,
                    provider.prompts[0],
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
