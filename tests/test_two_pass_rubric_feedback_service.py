from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone

from app.domain.rubric import FeedbackTask, Rubric, RubricCriterion
from app.llm.base import LLMResponse
from app.services.two_pass_rubric_feedback_service import (
    TWO_PASS_ANALYSIS_PROMPT_VERSION,
    TWO_PASS_EVIDENCE_VALIDATION_VERSION,
    TWO_PASS_FEEDBACK_LABEL,
    TWO_PASS_FEEDBACK_MODE,
    TWO_PASS_PIPELINE_VERSION,
    TWO_PASS_REVIEW_PROMPT_VERSION,
    TwoPassRubricFeedbackService,
)


STUDENT_TEXT = (
    "In meiner Einleitung nenne ich den Titel. "
    "Der Menschentrichter ist ein Tunnel."
)
ORIGINAL_TEXT = (
    "Da zeigt die Stadt dir asphaltglatt im Menschentrichter "
    "Millionen Gesichter."
)


def _task() -> FeedbackTask:
    now = datetime.now(timezone.utc)
    return FeedbackTask(
        task_id="task-two-pass",
        title="Gedichtinterpretation",
        subject="Deutsch",
        grade_level="8",
        instructions="Interpretiere das Gedicht.",
        material="",
        rubric=Rubric(
            rubric_id="rubric-two-pass",
            title="Zwei Kriterien",
            criteria=(
                RubricCriterion(
                    criterion_id="criterion-introduction",
                    title="Einleitung",
                    text="Nenne Titel und Autor.",
                    position=0,
                ),
                RubricCriterion(
                    criterion_id="criterion-images",
                    title="Sprachliche Bilder",
                    text=(
                        "Deute sprachliche Bilder nachvollziehbar."
                    ),
                    position=1,
                ),
            ),
        ),
        created_at=now,
        updated_at=now,
    )


def _finding(
    *,
    role: str,
    kind: str,
    claim: str,
    student_quote: str,
    student_feedback: str,
    next_step: str = "",
    criterion_quote: str = "",
    source_scope: str = "none",
    source_quote: str = "",
) -> dict[str, str]:
    return {
        "role": role,
        "kind": kind,
        "claim": claim,
        "student_quote": student_quote,
        "criterion_quote": criterion_quote,
        "source_scope": source_scope,
        "source_quote": source_quote,
        "student_feedback": student_feedback,
        "next_step": next_step,
    }


def _candidate_response(
    *,
    first_findings: list[dict[str, str]],
    second_findings: list[dict[str, str]],
) -> str:
    return json.dumps(
        {
            "criteria": [
                {
                    "criterion_id": "K1",
                    "findings": first_findings,
                },
                {
                    "criterion_id": "K2",
                    "findings": second_findings,
                },
            ]
        },
        ensure_ascii=False,
    )


class _QueuedProvider:
    provider_name = "ollama"
    model_name = "qwen3.5-35b-a3b:q5_k_m"

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.schema_names: list[str] = []
        self.schemas: list[dict[str, object] | None] = []

    async def generate(
        self,
        prompt: str,
        *,
        response_schema: dict[str, object] | None = None,
        response_schema_name: str = "structured_response",
    ) -> LLMResponse:
        self.prompts.append(prompt)
        self.schema_names.append(response_schema_name)
        self.schemas.append(response_schema)
        position = len(self.prompts)

        if not self.responses:
            raise AssertionError("Unerwarteter zusätzlicher Modellaufruf.")

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


class TwoPassRubricFeedbackServiceTests(unittest.TestCase):
    def test_uses_same_model_twice_and_keeps_only_accepted_findings(
        self,
    ) -> None:
        candidates = _candidate_response(
            first_findings=[
                _finding(
                    role="strength",
                    kind="strength",
                    claim="Der Titel wird genannt.",
                    student_quote="nenne ich den Titel",
                    criterion_quote="Titel",
                    student_feedback="Du nennst den Titel klar.",
                ),
                _finding(
                    role="improvement",
                    kind="missing_requirement",
                    claim="Der Autor fehlt.",
                    student_quote=(
                        "In meiner Einleitung nenne ich den Titel."
                    ),
                    criterion_quote="Titel und Autor",
                    student_feedback=(
                        "In der Einleitung fehlt noch der Autor."
                    ),
                    next_step="Ergänze den Autor in der Einleitung.",
                ),
            ],
            second_findings=[
                _finding(
                    role="improvement",
                    kind="source_mismatch",
                    claim="Menschentrichter bedeutet niemals Tunnel.",
                    student_quote=(
                        "Der Menschentrichter ist ein Tunnel."
                    ),
                    criterion_quote="sprachliche Bilder",
                    source_scope="run_original_text",
                    source_quote=(
                        "im Menschentrichter Millionen Gesichter"
                    ),
                    student_feedback=(
                        "Deine Deutung des Menschentrichters ist falsch."
                    ),
                    next_step="Ersetze die Deutung vollständig.",
                )
            ],
        )
        review = json.dumps(
            {
                "criteria": [
                    {
                        "criterion_id": "K1",
                        "status": "mostly_met",
                        "decisions": [
                            {
                                "finding_id": "K1-F1",
                                "verdict": "accept",
                                "reason": "Die Stärke ist direkt belegt.",
                            },
                            {
                                "finding_id": "K1-F2",
                                "verdict": "accept",
                                "reason": (
                                    "Der Autor fehlt im gesamten Text."
                                ),
                            },
                        ],
                    },
                    {
                        "criterion_id": "K2",
                        "status": "not_assessable",
                        "decisions": [
                            {
                                "finding_id": "K2-F1",
                                "verdict": "reject",
                                "reason": (
                                    "Der Originalbeleg widerlegt die "
                                    "Deutung nicht eindeutig."
                                ),
                            }
                        ],
                    },
                ]
            },
            ensure_ascii=False,
        )
        provider = _QueuedProvider(candidates, review)
        service = TwoPassRubricFeedbackService(
            providers={"ollama": provider},
            max_input_chars=8000,
        )

        result = asyncio.run(
            service.analyze_text(
                student_text=STUDENT_TEXT,
                task=_task(),
                original_text=ORIGINAL_TEXT,
                provider_key="ollama",
            )
        )

        self.assertEqual(len(provider.prompts), 2)
        self.assertEqual(
            provider.schema_names,
            [
                "rubric_candidate_findings",
                "rubric_restricted_review",
            ],
        )
        self.assertIn(
            "noch kein fertiges Gesamtfeedback",
            provider.prompts[0],
        )
        self.assertIn(
            "keine neuen Fehler",
            provider.prompts[1],
        )
        self.assertIn('"finding_id": "K1-F1"', provider.prompts[1])
        self.assertIn(ORIGINAL_TEXT, provider.prompts[1])
        self.assertEqual(result.pipeline_mode, TWO_PASS_FEEDBACK_MODE)
        self.assertEqual(result.pipeline_label, TWO_PASS_FEEDBACK_LABEL)
        self.assertEqual(result.provider, "ollama")
        self.assertEqual(result.model, provider.model_name)
        self.assertEqual(result.provider_request_id, "request-2")
        self.assertEqual(result.analysis_provider_request_id, "request-1")
        self.assertEqual(result.review_provider_request_id, "request-2")
        self.assertEqual(result.queue_duration_ms, 30.0)
        self.assertEqual(result.execution_duration_ms, 60.0)
        self.assertEqual(result.reasoning_effort, "high")
        self.assertEqual(result.candidate_finding_count, 3)
        self.assertEqual(result.validated_candidate_count, 3)
        self.assertEqual(result.accepted_finding_count, 2)
        self.assertEqual(result.rejected_finding_count, 1)
        self.assertIn(
            "Stärke: Du nennst den Titel klar.",
            result.criteria_feedback[0].feedback,
        )
        self.assertIn(
            "Verbesserung: In der Einleitung fehlt noch der Autor.",
            result.criteria_feedback[0].feedback,
        )
        self.assertEqual(
            result.criteria_feedback[0].next_step,
            "Ergänze den Autor in der Einleitung.",
        )
        self.assertEqual(
            result.criteria_feedback[0].evidence_quotes,
            (
                "nenne ich den Titel",
                "In meiner Einleitung nenne ich den Titel.",
            ),
        )
        self.assertEqual(
            result.criteria_feedback[1].status,
            "not_assessable",
        )
        self.assertNotIn(
            "Deine Deutung des Menschentrichters ist falsch",
            result.criteria_feedback[1].feedback,
        )
        self.assertEqual(len(result.evidence_warnings), 1)

        context = result.payload()["generation_context"]
        self.assertEqual(context["mode"], TWO_PASS_FEEDBACK_MODE)
        self.assertEqual(
            context["prompt_version"],
            TWO_PASS_PIPELINE_VERSION,
        )
        self.assertEqual(
            context["evidence_validation"],
            TWO_PASS_EVIDENCE_VALIDATION_VERSION,
        )
        self.assertEqual(
            context["phase_prompt_versions"],
            {
                "candidate_analysis": TWO_PASS_ANALYSIS_PROMPT_VERSION,
                "restricted_review": TWO_PASS_REVIEW_PROMPT_VERSION,
            },
        )
        self.assertEqual(
            context["finding_counts"],
            {
                "candidate": 3,
                "technically_validated": 3,
                "accepted": 2,
                "rejected": 1,
            },
        )

    def test_discards_invalid_source_quote_before_review(self) -> None:
        candidates = _candidate_response(
            first_findings=[
                _finding(
                    role="improvement",
                    kind="source_mismatch",
                    claim="Unbelegter Quellenwiderspruch.",
                    student_quote=(
                        "In meiner Einleitung nenne ich den Titel."
                    ),
                    criterion_quote="Titel",
                    source_scope="run_original_text",
                    source_quote="Dieses Zitat ist erfunden.",
                    student_feedback=(
                        "Die Einleitung widerspricht dem Gedicht."
                    ),
                    next_step="Schreibe sie vollständig neu.",
                )
            ],
            second_findings=[
                _finding(
                    role="strength",
                    kind="strength",
                    claim="Ein Bild wird gedeutet.",
                    student_quote=(
                        "Der Menschentrichter ist ein Tunnel."
                    ),
                    criterion_quote="sprachliche Bilder",
                    student_feedback=(
                        "Du versuchst, den Menschentrichter zu deuten."
                    ),
                )
            ],
        )
        review = json.dumps(
            {
                "criteria": [
                    {
                        "criterion_id": "K1",
                        "status": "not_assessable",
                        "decisions": [],
                    },
                    {
                        "criterion_id": "K2",
                        "status": "mostly_met",
                        "decisions": [
                            {
                                "finding_id": "K2-F1",
                                "verdict": "accept",
                                "reason": "Der Versuch ist belegt.",
                            }
                        ],
                    },
                ]
            },
            ensure_ascii=False,
        )
        provider = _QueuedProvider(candidates, review)
        service = TwoPassRubricFeedbackService(
            providers={"ollama": provider},
            max_input_chars=8000,
        )

        result = asyncio.run(
            service.analyze_text(
                student_text=STUDENT_TEXT,
                task=_task(),
                original_text=ORIGINAL_TEXT,
                provider_key="ollama",
            )
        )

        self.assertEqual(len(provider.prompts), 2)
        self.assertNotIn(
            "Dieses Zitat ist erfunden",
            provider.prompts[1],
        )
        self.assertEqual(result.candidate_finding_count, 2)
        self.assertEqual(result.validated_candidate_count, 1)
        self.assertEqual(result.accepted_finding_count, 1)
        self.assertEqual(result.rejected_finding_count, 1)
        self.assertEqual(len(result.pipeline_warnings), 1)
        self.assertIn(
            "Kandidatenbefund 1 wurde technisch verworfen",
            result.pipeline_warnings[0],
        )
        self.assertNotIn(
            "Schreibe sie vollständig neu",
            result.criteria_feedback[0].next_step,
        )

    def test_discards_improvement_without_safe_next_step(self) -> None:
        candidates = _candidate_response(
            first_findings=[
                _finding(
                    role="improvement",
                    kind="missing_requirement",
                    claim="Der Autor fehlt.",
                    student_quote=(
                        "In meiner Einleitung nenne ich den Titel."
                    ),
                    criterion_quote="Titel und Autor",
                    student_feedback=(
                        "In der Einleitung fehlt noch der Autor."
                    ),
                    next_step="",
                )
            ],
            second_findings=[],
        )
        provider = _QueuedProvider(candidates)
        service = TwoPassRubricFeedbackService(
            providers={"ollama": provider},
            max_input_chars=8000,
        )

        result = asyncio.run(
            service.analyze_text(
                student_text=STUDENT_TEXT,
                task=_task(),
                original_text=ORIGINAL_TEXT,
                provider_key="ollama",
            )
        )

        self.assertEqual(len(provider.prompts), 1)
        self.assertEqual(result.validated_candidate_count, 0)
        self.assertTrue(
            all(
                item.status == "not_assessable"
                for item in result.criteria_feedback
            )
        )
        self.assertIn(
            "sicherer Überarbeitungsschritt",
            result.pipeline_warnings[0],
        )

    def test_returns_safe_result_if_review_adds_unknown_finding(self) -> None:
        candidates = _candidate_response(
            first_findings=[
                _finding(
                    role="strength",
                    kind="strength",
                    claim="Der Titel wird genannt.",
                    student_quote="nenne ich den Titel",
                    criterion_quote="Titel",
                    student_feedback="Du nennst den Titel klar.",
                )
            ],
            second_findings=[],
        )
        invalid_review = json.dumps(
            {
                "criteria": [
                    {
                        "criterion_id": "K1",
                        "status": "met",
                        "decisions": [
                            {
                                "finding_id": "K1-NEU",
                                "verdict": "accept",
                                "reason": "Neu erfunden.",
                            }
                        ],
                    },
                    {
                        "criterion_id": "K2",
                        "status": "not_assessable",
                        "decisions": [],
                    },
                ]
            },
            ensure_ascii=False,
        )
        provider = _QueuedProvider(candidates, invalid_review)
        service = TwoPassRubricFeedbackService(
            providers={"ollama": provider},
            max_input_chars=8000,
        )

        result = asyncio.run(
            service.analyze_text(
                student_text=STUDENT_TEXT,
                task=_task(),
                original_text=ORIGINAL_TEXT,
                provider_key="ollama",
            )
        )

        self.assertEqual(len(provider.prompts), 2)
        self.assertTrue(
            all(
                item.status == "not_assessable"
                for item in result.criteria_feedback
            )
        )
        self.assertNotIn(
            "Du nennst den Titel klar",
            result.criteria_feedback[0].feedback,
        )
        self.assertEqual(result.accepted_finding_count, 0)
        self.assertEqual(result.rejected_finding_count, 1)
        self.assertIn(
            "strukturell nicht auswertbar",
            result.pipeline_warnings[-1],
        )

    def test_skips_review_when_no_candidate_survives(self) -> None:
        candidates = _candidate_response(
            first_findings=[],
            second_findings=[],
        )
        provider = _QueuedProvider(candidates)
        service = TwoPassRubricFeedbackService(
            providers={"ollama": provider},
            max_input_chars=8000,
        )

        result = asyncio.run(
            service.analyze_text(
                student_text=STUDENT_TEXT,
                task=_task(),
                original_text=ORIGINAL_TEXT,
                provider_key="ollama",
            )
        )

        self.assertEqual(len(provider.prompts), 1)
        self.assertEqual(result.review_duration_ms, 0)
        self.assertIsNone(result.review_provider_request_id)
        self.assertEqual(result.accepted_finding_count, 0)
        self.assertTrue(
            all(
                not item.evidence_verified
                for item in result.criteria_feedback
            )
        )
        self.assertIn(
            "kein Kandidatenbefund",
            result.pipeline_warnings[-1],
        )

    def test_discards_technical_status_from_student_feedback(self) -> None:
        candidates = _candidate_response(
            first_findings=[
                _finding(
                    role="strength",
                    kind="strength",
                    claim="Der Titel wird genannt.",
                    student_quote="nenne ich den Titel",
                    criterion_quote="Titel",
                    student_feedback=(
                        "Dieses Kriterium ist partially_met."
                    ),
                )
            ],
            second_findings=[],
        )
        provider = _QueuedProvider(candidates)
        service = TwoPassRubricFeedbackService(
            providers={"ollama": provider},
            max_input_chars=8000,
        )

        result = asyncio.run(
            service.analyze_text(
                student_text=STUDENT_TEXT,
                task=_task(),
                original_text=ORIGINAL_TEXT,
                provider_key="ollama",
            )
        )

        self.assertEqual(len(provider.prompts), 1)
        self.assertNotIn(
            "partially_met",
            " ".join(
                item.feedback
                for item in result.criteria_feedback
            ),
        )
        self.assertIn(
            "technischen Statuswert",
            result.pipeline_warnings[0],
        )


if __name__ == "__main__":
    unittest.main()
