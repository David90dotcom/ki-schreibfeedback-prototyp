from __future__ import annotations

import asyncio
import csv
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from httpx import Response

from app import main
from app.services.feedback_evaluation_exchange_service import (
    FEEDBACK_EVALUATION_EXPORT_FORMAT,
    FeedbackEvaluationExchangeError,
    FeedbackEvaluationExchangeService,
)
from app.services.task_store import TaskStore


def _csrf_token_from(response: Response) -> str:
    match = re.search(
        r'name="csrf_token"\s+value="([^"]+)"',
        response.text,
    )

    if match is None:
        raise AssertionError("CSRF-Token fehlt in der Antwort.")

    return match.group(1)


async def _create_evaluated_feedback_run(store: TaskStore):
    task = await store.create_task(
        title="Gedichtanalyse",
        subject="Deutsch",
        grade_level="8",
        instructions="Analysiere das Gedicht.",
        material="Ein anonymes Beispielgedicht.",
        rubric_title="Analysefeedback",
        criterion_titles=("Einleitung", "Sprache"),
        criteria=(
            "Nenne Titel und Autor.",
            "Untersuche ein sprachliches Mittel.",
        ),
    )
    student_text = "Anonymisierter Schülertext nur für den JSON-Export."
    feedback_run_id = await store.save_feedback_run(
        task=task,
        student_text=student_text,
        provider="ollama",
        model="lokales-testmodell",
        reasoning_effort=None,
        duration_ms=12_345,
        queue_duration_ms=125.5,
        execution_duration_ms=12_000.0,
        provider_request_id="feedback-request-1",
        original_text="Anonymes Originalmaterial.",
        feedback_payload={
            "criteria": [
                {
                    "criterion_id": task.rubric.criteria[0].criterion_id,
                    "feedback": "Ausführlicher Feedbacktext nur im JSON.",
                }
            ],
            "overall_feedback": "Zusammenfassung des Feedbacks.",
        },
    )
    await store.select_feedback_run_for_evaluation(
        feedback_run_id=feedback_run_id,
        student_text=student_text,
    )
    scores = {
        "factual_correctness": 3,
        "transparency_reasoning": 2,
        "audience_context_fit": 2,
        "action_learning_activation": 1,
    }
    justifications = {
        key: f"Nicht für CSV bestimmte Begründung zu {key}."
        for key in scores
    }
    automatic = await store.create_automatic_feedback_evaluation(
        feedback_run_id=feedback_run_id,
        scores=scores,
        justifications=justifications,
        evaluator_provider="openai",
        evaluator_model="gpt-test",
        evaluator_prompt_version="meta-prompt-test",
        duration_ms=2_500,
        queue_duration_ms=50.0,
        execution_duration_ms=2_300.0,
        provider_request_id="evaluation-request-1",
        evaluation_name="Automatische Ausgangsbewertung",
    )
    await store.create_manual_feedback_evaluation(
        feedback_run_id=feedback_run_id,
        scores={**scores, "action_learning_activation": 2},
        justifications=justifications,
        source_evaluation_id=automatic.evaluation_id,
        evaluation_name="Manuelle Schlussbewertung",
    )
    return await store.get_feedback_run_for_evaluation(feedback_run_id)


class FeedbackEvaluationExchangeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.source_store = TaskStore(
            Path(self.temporary_directory.name) / "source.sqlite3"
        )
        self.feedback_run = asyncio.run(
            _create_evaluated_feedback_run(self.source_store)
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_json_round_trip_preserves_complete_meta_records(self) -> None:
        content = FeedbackEvaluationExchangeService.export_json(
            (self.feedback_run,)
        )
        imported_runs = FeedbackEvaluationExchangeService.parse_import(
            content
        )

        self.assertIn(
            f'"format": "{FEEDBACK_EVALUATION_EXPORT_FORMAT}"',
            content.decode("utf-8"),
        )
        self.assertEqual(len(imported_runs), 1)
        imported = imported_runs[0]
        self.assertEqual(imported.student_text, self.feedback_run.student_text)
        self.assertEqual(imported.feedback_payload, self.feedback_run.feedback_payload)
        self.assertEqual(len(imported.evaluations), 2)
        self.assertEqual(
            {item.evaluation_type for item in imported.evaluations},
            {"automatic", "manual"},
        )
        manual = next(
            item
            for item in imported.evaluations
            if item.evaluation_type == "manual"
        )
        automatic = next(
            item
            for item in imported.evaluations
            if item.evaluation_type == "automatic"
        )
        self.assertEqual(
            manual.source_evaluation_id,
            automatic.evaluation_id,
        )

    def test_csv_contains_metrics_and_scores_but_no_free_text(self) -> None:
        content = FeedbackEvaluationExchangeService.export_csv(
            (self.feedback_run,)
        )
        decoded = content.decode("utf-8-sig")
        rows = list(
            csv.DictReader(
                io.StringIO(decoded),
                delimiter=";",
            )
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["feedback_duration_ms"], "12345")
        self.assertEqual(rows[0]["feedback_model"], "lokales-testmodell")
        self.assertEqual(rows[0]["score_factual_correctness"], "3")
        self.assertEqual(rows[0]["average_score"], "2.25")
        self.assertNotIn(self.feedback_run.student_text, decoded)
        self.assertNotIn("Ausführlicher Feedbacktext", decoded)
        self.assertNotIn("Nicht für CSV bestimmte Begründung", decoded)
        self.assertNotIn("justification", decoded)

    def test_import_into_fresh_store_creates_independent_copies(self) -> None:
        content = FeedbackEvaluationExchangeService.export_json(
            (self.feedback_run,)
        )
        parsed = FeedbackEvaluationExchangeService.parse_import(content)
        target_store = TaskStore(
            Path(self.temporary_directory.name) / "target.sqlite3"
        )

        run_count, evaluation_count = asyncio.run(
            target_store.import_feedback_evaluation_runs(parsed)
        )
        imported_runs = asyncio.run(
            target_store.list_feedback_runs_for_evaluation()
        )

        self.assertEqual((run_count, evaluation_count), (1, 2))
        self.assertEqual(len(imported_runs), 1)
        imported = imported_runs[0]
        self.assertNotEqual(
            imported.feedback_run_id,
            self.feedback_run.feedback_run_id,
        )
        self.assertEqual(imported.student_text, self.feedback_run.student_text)
        self.assertEqual(imported.provider, "ollama")
        self.assertEqual(imported.duration_ms, 12_345)
        self.assertEqual(len(imported.evaluations), 2)
        imported_manual = next(
            item
            for item in imported.evaluations
            if item.evaluation_type == "manual"
        )
        imported_automatic = next(
            item
            for item in imported.evaluations
            if item.evaluation_type == "automatic"
        )
        self.assertEqual(
            imported_manual.source_evaluation_id,
            imported_automatic.evaluation_id,
        )
        self.assertNotEqual(
            imported_automatic.evaluation_id,
            next(
                item.evaluation_id
                for item in self.feedback_run.evaluations
                if item.evaluation_type == "automatic"
            ),
        )
        self.assertEqual(
            asyncio.run(target_store.list_tasks()),
            [],
        )
        self.assertEqual(
            len(
                asyncio.run(
                    target_store.list_tasks(include_archived=True)
                )
            ),
            1,
        )

    def test_wrong_file_format_is_rejected(self) -> None:
        with self.assertRaises(FeedbackEvaluationExchangeError):
            FeedbackEvaluationExchangeService.parse_import(
                b'{"format":"anderes-format"}'
            )


class FeedbackEvaluationExchangeRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = TaskStore(
            Path(self.temporary_directory.name) / "analysis.sqlite3"
        )
        self.store_patcher = patch.object(main, "task_store", self.store)
        self.store_patcher.start()
        self.client = TestClient(main.app)

        login_csrf_token = _csrf_token_from(self.client.get("/login"))

        with patch.object(main, "verify_credentials", return_value=True):
            response = self.client.post(
                "/login",
                data={
                    "username": main.settings.auth_username,
                    "password": "integrationstest",
                    "csrf_token": login_csrf_token,
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.feedback_run = asyncio.run(
            _create_evaluated_feedback_run(self.store)
        )

    def tearDown(self) -> None:
        self.client.close()
        self.store_patcher.stop()
        self.temporary_directory.cleanup()

    def test_page_exports_and_reimports_meta_evaluations(self) -> None:
        page = self.client.get("/feedback-evaluations")
        csrf_token = _csrf_token_from(page)

        self.assertIn("JSON zur Übertragung", page.text)
        self.assertIn("CSV für Auswertung", page.text)
        self.assertIn(
            'action="/feedback-evaluations/import"',
            page.text,
        )
        self.assertIn("keine", page.text)
        self.assertIn("Schülertexte", page.text)

        json_export = self.client.get(
            "/feedback-evaluations/export-json"
        )
        csv_export = self.client.get(
            "/feedback-evaluations/export-csv"
        )

        self.assertEqual(json_export.status_code, 200)
        self.assertEqual(
            json_export.headers["content-type"],
            "application/json",
        )
        self.assertIn(
            "meta-bewertungen.json",
            json_export.headers["content-disposition"],
        )
        self.assertEqual(csv_export.status_code, 200)
        self.assertTrue(
            csv_export.headers["content-type"].startswith("text/csv")
        )
        self.assertIn(
            "meta-bewertungen.csv",
            csv_export.headers["content-disposition"],
        )

        import_response = self.client.post(
            "/feedback-evaluations/import",
            data={"csrf_token": csrf_token},
            files={
                "import_file": (
                    "meta-bewertungen.json",
                    json_export.content,
                    "application/json",
                )
            },
            follow_redirects=False,
        )

        self.assertEqual(import_response.status_code, 303)
        self.assertEqual(
            import_response.headers["location"],
            (
                "/feedback-evaluations?notice=evaluation-imported"
                "&imported_run_count=1&imported_evaluation_count=2"
            ),
        )
        imported_runs = asyncio.run(
            self.store.list_feedback_runs_for_evaluation()
        )
        self.assertEqual(len(imported_runs), 2)
        self.assertEqual(
            sum(len(item.evaluations) for item in imported_runs),
            4,
        )

    def test_import_requires_csrf_and_json_export_requires_login(self) -> None:
        response = self.client.post(
            "/feedback-evaluations/import",
            data={"csrf_token": "ungueltig"},
            files={
                "import_file": (
                    "meta.json",
                    b"{}",
                    "application/json",
                )
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 403)

        logged_out = TestClient(main.app)

        try:
            for path in (
                "/feedback-evaluations/export-json",
                "/feedback-evaluations/export-csv",
            ):
                unauthenticated = logged_out.get(
                    path,
                    follow_redirects=False,
                )
                self.assertEqual(unauthenticated.status_code, 303)
                self.assertEqual(
                    unauthenticated.headers["location"],
                    "/login",
                )
        finally:
            logged_out.close()
