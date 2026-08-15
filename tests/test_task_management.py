from __future__ import annotations

import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from httpx import Response

from app import main
from app.domain.feedback_evaluation import MANUAL_META_EVALUATION_RUBRIC
from app.services.automatic_feedback_evaluation_service import (
    AUTOMATIC_EVALUATION_PROMPT_VERSION,
    AutomaticFeedbackEvaluationResult,
)
from app.services.criterion_wise_rubric_feedback_service import (
    CRITERION_REFRESH_OVERALL_FEEDBACK,
    CRITERION_WISE_FEEDBACK_LABEL,
    CRITERION_WISE_FEEDBACK_MODE,
    CRITERION_WISE_PIPELINE_VERSION,
)
from app.services.feedback_service import (
    STANDARD_FEEDBACK_MODE,
    STANDARD_FEEDBACK_PROMPT_VERSION,
    STANDARD_FEEDBACK_STUDENT_TEXT_PLACEHOLDER,
    FeedbackResult,
)
from app.services.rubric_feedback_service import (
    EVIDENCE_REPAIR_PROMPT_VERSION,
    RUBRIC_FEEDBACK_PROMPT_VERSION,
    CriterionFeedbackResult,
    EvidenceRepairAttempt,
    RubricFeedbackResult,
)
from app.services.task_store import TaskStore, TaskStoreError
from app.services.two_pass_rubric_feedback_service import (
    TWO_PASS_ANALYSIS_PROMPT_VERSION,
    TWO_PASS_EVIDENCE_VALIDATION_VERSION,
    TWO_PASS_FEEDBACK_LABEL,
    TWO_PASS_FEEDBACK_MODE,
    TWO_PASS_PIPELINE_VERSION,
    TWO_PASS_REVIEW_PROMPT_VERSION,
)


def _csrf_token_from(response: Response) -> str:
    match = re.search(
        r'name="csrf_token"\s+value="([^"]+)"',
        response.text,
    )

    if match is None:
        raise AssertionError("CSRF-Token fehlt in der Antwort.")

    return match.group(1)


class TaskManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = TaskStore(
            Path(self.temporary_directory.name) / "analysis.sqlite3"
        )
        self.store_patcher = patch.object(
            main,
            "task_store",
            self.store,
        )
        self.store_patcher.start()
        self.client = TestClient(main.app)

        login_csrf_token = _csrf_token_from(
            self.client.get("/login")
        )

        with patch.object(
            main,
            "verify_credentials",
            return_value=True,
        ):
            login_response = self.client.post(
                "/login",
                data={
                    "username": main.settings.auth_username,
                    "password": "integrationstest",
                    "csrf_token": login_csrf_token,
                },
                follow_redirects=False,
            )

        self.assertEqual(login_response.status_code, 303)
        self.csrf_token = _csrf_token_from(self.client.get("/"))

    def tearDown(self) -> None:
        self.client.close()
        self.store_patcher.stop()
        self.temporary_directory.cleanup()

    def _create_task(self):
        return asyncio.run(
            self.store.create_task(
                title="Gedichtinterpretation Klasse 8",
                subject="Deutsch",
                grade_level="8",
                instructions="Interpretiere das vorliegende Gedicht.",
                material="Ein anonymes Beispielgedicht.",
                rubric_title="Grundanforderungen Gedichtinterpretation",
                criterion_titles=[
                    "Einleitung: Grundangaben",
                    "Sprachliche Bilder",
                ],
                criteria=[
                    "Einleitung mit Titel, Autor und Thema",
                    "Sprachliche Bilder benennen und erläutern",
                ],
            )
        )

    def test_task_pages_require_login(self) -> None:
        anonymous_client = TestClient(main.app)

        try:
            for path in (
                "/tasks",
                "/tasks/new",
                "/feedback-evaluations",
            ):
                with self.subTest(path=path):
                    response = anonymous_client.get(
                        path,
                        follow_redirects=False,
                    )
                    self.assertEqual(response.status_code, 303)
                    self.assertEqual(
                        response.headers["location"],
                        "/login",
                    )
        finally:
            anonymous_client.close()

    def test_creates_task_with_multiple_criteria_in_browser(self) -> None:
        new_task_page = self.client.get("/tasks/new")

        self.assertIn('name="criterion_titles"', new_task_page.text)
        self.assertIn(
            "Überschrift in der Textanalyse",
            new_task_page.text,
        )
        self.assertIn('data-max-criteria="100"', new_task_page.text)
        self.assertIn(
            'data-max-criterion-chars="10000"',
            new_task_page.text,
        )
        self.assertIn('maxlength="10000"', new_task_page.text)

        response = self.client.post(
            "/tasks/new",
            data={
                "csrf_token": self.csrf_token,
                "title": "Gedichtinterpretation Klasse 8",
                "subject": "Deutsch",
                "grade_level": "8",
                "instructions": "Interpretiere das Gedicht.",
                "material": "Beispielgedicht",
                "rubric_title": "Feedback Gedichtinterpretation",
                "criterion_titles": [
                    "Einleitung: Thema",
                    "Form: Aufbau",
                    "Sprache: Bildlichkeit",
                ],
                "criteria": [
                    "Einleitung verfassen",
                    "Äußere Form beschreiben",
                    "Sprachliche Bilder erläutern",
                ],
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/tasks?notice=created",
        )
        tasks = asyncio.run(self.store.list_tasks())
        self.assertEqual(len(tasks), 1)
        self.assertEqual(len(tasks[0].rubric.criteria), 3)
        self.assertEqual(
            tasks[0].rubric.criteria[0].title,
            "Einleitung: Thema",
        )

        management_page = self.client.get("/tasks")
        self.assertIn("Gedichtinterpretation Klasse 8", management_page.text)
        self.assertIn("3 Kriterien", management_page.text)
        self.assertIn(
            '<details class="card task-card" data-task-card>',
            management_page.text,
        )
        self.assertIn(
            'class="task-card-summary"',
            management_page.text,
        )
        self.assertIn("Interpretiere das Gedicht.", management_page.text)
        self.assertIn("Beispielgedicht", management_page.text)
        self.assertIn("+ 2", management_page.text)
        self.assertIn("weitere Kriterien", management_page.text)
        self.assertRegex(
            management_page.text,
            r'/static/rubrics\.js\?v=[0-9a-f]{12}',
        )

    def test_task_form_uses_configured_criterion_limits(self) -> None:
        configured_store = TaskStore(
            Path(self.temporary_directory.name) / "configured.sqlite3",
            max_criteria=17,
            max_criterion_chars=2345,
        )

        with patch.object(main, "task_store", configured_store):
            response = self.client.get("/tasks/new")

        self.assertIn('data-max-criteria="17"', response.text)
        self.assertIn(
            'data-max-criterion-chars="2345"',
            response.text,
        )
        self.assertIn('maxlength="2345"', response.text)

    def test_task_can_be_edited_duplicated_and_deleted(self) -> None:
        task = self._create_task()
        edit_page = self.client.get(f"/tasks/{task.task_id}/edit")

        self.assertIn("Einleitung: Grundangaben", edit_page.text)

        edit_response = self.client.post(
            f"/tasks/{task.task_id}/edit",
            data={
                "csrf_token": self.csrf_token,
                "title": "Überarbeitete Gedichtanalyse",
                "subject": "Deutsch",
                "grade_level": "8",
                "instructions": "Analysiere das Gedicht.",
                "material": "Beispielgedicht",
                "rubric_title": "Überarbeitetes Feedback",
                "criterion_titles": [
                    "Einleitung",
                    "Inhalt",
                    "Form",
                ],
                "criteria": [
                    "Einleitung verfassen",
                    "Inhalt zusammenfassen",
                    "Form beschreiben",
                ],
            },
            follow_redirects=False,
        )
        self.assertEqual(edit_response.status_code, 303)

        duplicate_response = self.client.post(
            f"/tasks/{task.task_id}/duplicate",
            data={"csrf_token": self.csrf_token},
            follow_redirects=False,
        )
        self.assertEqual(duplicate_response.status_code, 303)
        self.assertEqual(len(asyncio.run(self.store.list_tasks())), 2)

        delete_response = self.client.post(
            f"/tasks/{task.task_id}/delete",
            data={"csrf_token": self.csrf_token},
            follow_redirects=False,
        )
        self.assertEqual(delete_response.status_code, 303)
        self.assertEqual(
            delete_response.headers["location"],
            "/tasks?notice=deleted",
        )
        self.assertEqual(len(asyncio.run(self.store.list_tasks())), 1)

    def test_default_task_is_configurable_and_preselected(self) -> None:
        task = self._create_task()
        management_page = self.client.get("/tasks")

        self.assertIn(
            f'action="/tasks/{task.task_id}/default"',
            management_page.text,
        )
        self.assertIn("Als Standard festlegen", management_page.text)

        response = self.client.post(
            f"/tasks/{task.task_id}/default",
            data={"csrf_token": self.csrf_token},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/tasks?notice=default")
        self.assertEqual(
            asyncio.run(self.store.get_default_feedback_task_id()),
            task.task_id,
        )

        management_page = self.client.get("/tasks?notice=default")
        self.assertIn("Standard-Kriterienvorlage festgelegt", management_page.text)
        self.assertIn("Standardvorlage", management_page.text)
        self.assertIn("Aktuelle Standardvorlage", management_page.text)

        start_page = self.client.get("/")
        self.assertIn(
            f'data-default-task-id="{task.task_id}"',
            start_page.text,
        )
        self.assertRegex(
            start_page.text,
            rf'value="{re.escape(task.task_id)}"[\s\S]{{0,180}}selected',
        )
        self.assertRegex(
            start_page.text,
            r'value="criterion_wise"[\s\S]{0,180}checked',
        )
        self.assertIn(
            "Standardverfahren: Kriterienweise Analyse",
            start_page.text,
        )
        self.assertIn(
            "Erweiterte Forschungsoptionen anzeigen",
            start_page.text,
        )
        self.assertNotIn(
            "Ohne Feedback-Vorlage – bisheriges Gesamtfeedback",
            start_page.text,
        )

    def test_task_is_selectable_and_rubric_feedback_is_persisted(
        self,
    ) -> None:
        task = self._create_task()
        start_page = self.client.get("/")

        self.assertIn('id="task-id"', start_page.text)
        self.assertIn(f'value="{task.task_id}"', start_page.text)
        self.assertIn(task.rubric.title, start_page.text)
        self.assertIn(
            f'data-task-preview="{task.task_id}"',
            start_page.text,
        )
        self.assertIn(
            'class="task-preview-summary"',
            start_page.text,
        )
        self.assertIn("Aufgabenstellung", start_page.text)
        self.assertIn(task.instructions, start_page.text)
        self.assertIn(task.material, start_page.text)
        self.assertIn(
            "Originaltext für diesen Lauf (optional)",
            start_page.text,
        )
        self.assertIn('name="original_text"', start_page.text)

        run_specific_original_text = (
            "Vollständiger Originaltext nur für diesen Analyselauf."
        )

        result = RubricFeedbackResult(
            provider="openai",
            model=main.settings.openai_model,
            task_id=task.task_id,
            task_title=task.title,
            rubric_title=task.rubric.title,
            criteria_feedback=tuple(
                CriterionFeedbackResult(
                    criterion_id=criterion.criterion_id,
                    criterion_title=criterion.title,
                    criterion_text=criterion.text,
                    status=(
                        "mostly_met"
                        if criterion.position == 0
                        else "not_assessable"
                    ),
                    status_label=(
                        "Überwiegend erfüllt"
                        if criterion.position == 0
                        else "Nicht beurteilbar"
                    ),
                    feedback=(
                        "Konkretes **Feedback**."
                        if criterion.position == 0
                        else "Dieser Befund wurde sicher verworfen."
                    ),
                    next_step=(
                        "Konkreter *nächster Schritt*."
                        if criterion.position == 0
                        else "Prüfe diesen Aspekt selbst."
                    ),
                    evidence_verified=(criterion.position == 0),
                )
                for criterion in task.rubric.criteria
            ),
            overall_feedback=(
                "Ein Teil konnte nicht sicher geprüft werden."
            ),
            duration_ms=350,
            reasoning_effort="max",
            evidence_warnings=(
                "K2 – Sprache: Bildlichkeit: Die KI hat keinen "
                "überprüfbaren Schülertextbeleg geliefert.",
            ),
        )

        with (
            patch.object(
                main.criterion_wise_rubric_feedback_service,
                "analyze_text",
                new=AsyncMock(return_value=result),
            ) as criterion_wise_analysis,
            patch.object(
                main.rubric_feedback_service,
                "analyze_text",
                new=AsyncMock(),
            ) as joint_analysis,
            patch.object(
                main.feedback_service,
                "analyze_text",
                new=AsyncMock(),
            ) as old_analysis,
        ):
            response = self.client.post(
                "/analyze",
                data={
                    "csrf_token": self.csrf_token,
                    "task_id": task.task_id,
                    "original_text": run_specific_original_text,
                    "student_text": "Anonymisierter Schülertext",
                    "provider": "openai",
                    "openai_model": main.settings.openai_model,
                    "openai_api_key": "test-api-key",
                },
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["x-analysis-outcome"],
            "success",
        )
        criterion_wise_analysis.assert_awaited_once()
        joint_analysis.assert_not_awaited()
        old_analysis.assert_not_awaited()
        analyzed_task = criterion_wise_analysis.await_args.kwargs["task"]
        analyzed_original_text = (
            criterion_wise_analysis.await_args.kwargs["original_text"]
        )
        self.assertEqual(
            analyzed_task.material,
            task.material,
        )
        self.assertEqual(
            analyzed_original_text,
            run_specific_original_text,
        )
        unchanged_task = asyncio.run(
            self.store.get_task(task.task_id)
        )
        self.assertIsNotNone(unchanged_task)
        self.assertEqual(unchanged_task.material, task.material)
        self.assertIn("Einleitung: Grundangaben", response.text)
        self.assertIn("Überwiegend erfüllt", response.text)
        self.assertIn("criterion-status-mostly_met", response.text)
        self.assertIn("criterion-status-not_assessable", response.text)
        self.assertIn("Hinweis zur Belegprüfung", response.text)
        self.assertIn(
            "Ein Kriterienbefund konnte nicht zuverlässig",
            response.text,
        )
        self.assertIn(
            "K2 – Sprache: Bildlichkeit",
            response.text,
        )
        self.assertIn("<h4>Feedback</h4>", response.text)
        self.assertIn(
            'class="criterion-feedback-copy"',
            response.text,
        )
        self.assertIn(
            'class="criterion-feedback-next-step"',
            response.text,
        )
        self.assertNotIn("<h4>Überarbeitung</h4>", response.text)
        self.assertEqual(
            response.text.count("data-refresh-criterion"),
            2,
        )
        self.assertIn(
            "Einzelne Rückmeldung neu erzeugen",
            response.text,
        )
        self.assertIn(
            "Jeder Klick ist ein zusätzlicher",
            response.text,
        )
        self.assertNotIn("Nächster Schritt", response.text)
        self.assertIn(
            "Konkretes <strong>Feedback</strong>.",
            response.text,
        )
        self.assertIn(
            "Konkreter <em>nächster Schritt</em>.",
            response.text,
        )
        self.assertIn(
            "Ein Teil konnte nicht sicher geprüft werden.",
            response.text,
        )
        self.assertNotIn("**Feedback**", response.text)
        self.assertNotIn("*nächster Schritt*", response.text)
        self.assertNotIn(
            "Einleitung mit Titel, Autor und Thema",
            response.text,
        )
        self.assertNotIn("bewertungsbogen", response.text.lower())
        self.assertEqual(
            asyncio.run(
                self.store.count_feedback_runs(task_id=task.task_id)
            ),
            1,
        )
        self.assertIn("Optional · Meta-Ebene", response.text)
        self.assertIn(
            "Für Feedback-Bewertung speichern",
            response.text,
        )
        self.assertIn(
            "Es wird noch keine Bewertung gestartet",
            response.text,
        )
        self.assertEqual(
            asyncio.run(
                self.store.list_feedback_runs_for_evaluation()
            ),
            [],
        )

        run_match = re.search(
            r'action="/feedback-runs/([^/]+)/save"',
            response.text,
        )
        self.assertIsNotNone(run_match)
        feedback_run_id = (
            run_match.group(1) if run_match is not None else ""
        )
        save_response = self.client.post(
            f"/feedback-runs/{feedback_run_id}/save",
            data={
                "csrf_token": self.csrf_token,
                "student_text": "Anonymisierter Schülertext",
            },
            follow_redirects=False,
        )

        self.assertEqual(save_response.status_code, 303)
        self.assertEqual(
            save_response.headers["location"],
            "/feedback-evaluations?notice=saved",
        )
        selected_runs = asyncio.run(
            self.store.list_feedback_runs_for_evaluation()
        )
        self.assertEqual(len(selected_runs), 1)
        self.assertEqual(
            selected_runs[0].feedback_run_id,
            feedback_run_id,
        )
        self.assertEqual(
            selected_runs[0].task_snapshot["material"],
            task.material,
        )
        self.assertEqual(
            selected_runs[0].original_text,
            run_specific_original_text,
        )
        self.assertEqual(
            selected_runs[0].feedback_payload["criteria"][0]["status"],
            "mostly_met",
        )
        self.assertFalse(
            selected_runs[0].feedback_payload["criteria"][1][
                "evidence_verified"
            ]
        )

        overview = self.client.get(
            "/feedback-evaluations?notice=saved"
        )
        self.assertEqual(overview.status_code, 200)
        self.assertIn("Feedback-Bewertung", overview.text)
        self.assertIn("Optional · Meta-Ebene", overview.text)
        self.assertIn(task.title, overview.text)
        self.assertIn(main.settings.openai_model, overview.text)
        self.assertIn("350 ms", overview.text)
        self.assertIn("Denktiefe", overview.text)
        self.assertIn("max", overview.text)
        self.assertIn("Noch nicht bewertet", overview.text)
        self.assertIn(
            '<details class="meta-feedback-details">',
            overview.text,
        )
        self.assertIn("Feedbackdetails anzeigen", overview.text)
        self.assertIn("Einzelfeedbacks", overview.text)
        self.assertIn("Einleitung: Grundangaben", overview.text)
        self.assertIn(
            "Konkretes <strong>Feedback</strong>.",
            overview.text,
        )
        self.assertIn(
            "Konkreter <em>nächster Schritt</em>.",
            overview.text,
        )
        self.assertIn(
            'class="meta-feedback-copy"',
            overview.text,
        )
        self.assertIn(
            'class="meta-feedback-next-step"',
            overview.text,
        )
        self.assertNotIn(
            "<strong>Überarbeitung:</strong>",
            overview.text,
        )
        self.assertIn(
            "Ein Teil konnte nicht sicher geprüft werden.",
            overview.text,
        )
        self.assertIn(
            "Der Feedbacklauf wurde für die spätere Bewertung gespeichert.",
            overview.text,
        )
        self.assertIn(
            "Anonymisierter Schülertext",
            overview.text,
        )
        self.assertIn(
            "Originaltext dieses Feedbacklaufs",
            overview.text,
        )
        self.assertIn(run_specific_original_text, overview.text)
        self.assertIn("Bewertungsgrundlage anzeigen", overview.text)
        self.assertIn("Manuell bewerten", overview.text)
        self.assertIn("Fachliche Korrektheit", overview.text)
        self.assertIn("Transparenz und Begründung", overview.text)
        self.assertIn("Adressaten- und Kontextpassung", overview.text)
        self.assertIn(
            "Handlungsorientierung und Lernaktivierung",
            overview.text,
        )
        self.assertIn('name="score_factual_correctness"', overview.text)
        self.assertIn(
            'name="justification_action_learning_activation"',
            overview.text,
        )
        self.assertIn("meta-feedback-v1", overview.text)

        manual_response = self.client.post(
            f"/feedback-runs/{feedback_run_id}/manual-evaluations",
            data={
                "csrf_token": self.csrf_token,
                "score_factual_correctness": "3",
                "justification_factual_correctness": (
                    "Die fachlichen Hinweise stimmen mit dem Text überein."
                ),
                "score_transparency_reasoning": "2",
                "justification_transparency_reasoning": (
                    "Die wichtigsten Urteile sind nachvollziehbar belegt."
                ),
                "score_audience_context_fit": "3",
                "justification_audience_context_fit": (
                    "Sprache und Umfang passen zur achten Klasse."
                ),
                "score_action_learning_activation": "2",
                "justification_action_learning_activation": (
                    "Die nächsten Schritte sind konkret umsetzbar."
                ),
            },
            follow_redirects=False,
        )

        self.assertEqual(manual_response.status_code, 303)
        self.assertEqual(
            manual_response.headers["location"],
            (
                "/feedback-evaluations?notice=evaluation-saved"
                f"&feedback_run_notice_id={feedback_run_id}"
                f"#feedback-run-{feedback_run_id}"
            ),
        )
        evaluated_runs = asyncio.run(
            self.store.list_feedback_runs_for_evaluation()
        )
        self.assertEqual(evaluated_runs[0].manual_evaluation_count, 1)
        self.assertEqual(len(evaluated_runs[0].evaluations), 1)
        evaluation_id = evaluated_runs[0].evaluations[0].evaluation_id

        evaluated_overview = self.client.get(
            "/feedback-evaluations?notice=evaluation-saved"
        )
        self.assertEqual(evaluated_overview.status_code, 200)
        self.assertRegex(
            evaluated_overview.text,
            r"1\s+Bewertung",
        )
        self.assertIn(
            "Gespeicherte Meta-Bewertungen",
            evaluated_overview.text,
        )
        self.assertIn("Ø 2,5 / 3", evaluated_overview.text)
        self.assertIn("aus 1 Meta-Bewertung", evaluated_overview.text)
        self.assertRegex(
            evaluated_overview.text,
            r'<details\s+class="meta-evaluation-history"\s+'
            r'aria-label="Gespeicherte Bewertungen"\s*>',
        )
        self.assertRegex(
            evaluated_overview.text,
            r'<details\s+class="meta-evaluation-record"\s*>',
        )
        self.assertIn("Manuelle Bewertung", evaluated_overview.text)
        self.assertIn("3/3", evaluated_overview.text)
        self.assertIn("erfüllt", evaluated_overview.text)
        self.assertIn(
            "Die fachlichen Hinweise stimmen mit dem Text überein.",
            evaluated_overview.text,
        )
        self.assertIn(
            "Die manuelle Bewertung wurde als eigenständiger Datensatz",
            evaluated_overview.text,
        )
        self.assertIn(
            "Weitere manuelle Bewertung anlegen",
            evaluated_overview.text,
        )
        self.assertIn("PDF exportieren", evaluated_overview.text)
        self.assertIn(
            (
                f'href="/feedback-runs/{feedback_run_id}/evaluations/'
                f'{evaluation_id}/pdf"'
            ),
            evaluated_overview.text,
        )

        pdf_response = self.client.get(
            f"/feedback-runs/{feedback_run_id}/evaluations/"
            f"{evaluation_id}/pdf"
        )
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(
            pdf_response.headers["content-type"],
            "application/pdf",
        )
        self.assertRegex(
            pdf_response.headers["content-disposition"],
            r'^attachment; filename="meta-bewertung-[^"]+\.pdf"$',
        )
        self.assertEqual(pdf_response.headers["cache-control"], "no-store")
        self.assertEqual(
            pdf_response.headers["x-content-type-options"],
            "nosniff",
        )
        self.assertTrue(pdf_response.content.startswith(b"%PDF-"))

        missing_pdf_response = self.client.get(
            f"/feedback-runs/{feedback_run_id}/evaluations/"
            "00000000-0000-0000-0000-000000000001/pdf"
        )
        self.assertEqual(missing_pdf_response.status_code, 404)

    def test_single_criterion_can_be_refreshed_and_persisted(
        self,
    ) -> None:
        task = self._create_task()
        student_text = "Anonymisierter Schülertext"
        original_text = "Laufbezogener Originaltext"
        first_criterion = task.rubric.criteria[0]
        second_criterion = task.rubric.criteria[1]
        feedback_run_id = asyncio.run(
            self.store.save_feedback_run(
                task=task,
                student_text=student_text,
                original_text=original_text,
                provider="openai",
                model=main.settings.openai_model,
                reasoning_effort="high",
                duration_ms=300,
                feedback_payload={
                    "criteria": [
                        {
                            "criterion_id": first_criterion.criterion_id,
                            "criterion_title": first_criterion.title,
                            "criterion_text": first_criterion.text,
                            "status": "partially_met",
                            "feedback": "Altes erstes Feedback.",
                            "next_step": "Alter Schritt.",
                            "evidence_verified": True,
                        },
                        {
                            "criterion_id": second_criterion.criterion_id,
                            "criterion_title": second_criterion.title,
                            "criterion_text": second_criterion.text,
                            "status": "met",
                            "feedback": "Unverändertes zweites Feedback.",
                            "next_step": "",
                            "evidence_verified": True,
                        },
                    ],
                    "overall_feedback": "Alte Zusammenfassung.",
                    "generation_context": {
                        "mode": "rubric_feedback",
                        "label": "Gemeinsame Analyse",
                    },
                },
            )
        )
        refreshed_result = RubricFeedbackResult(
            provider="openai",
            model=main.settings.openai_model,
            task_id=task.task_id,
            task_title=task.title,
            rubric_title=task.rubric.title,
            criteria_feedback=(
                CriterionFeedbackResult(
                    criterion_id=first_criterion.criterion_id,
                    criterion_title=first_criterion.title,
                    criterion_text=first_criterion.text,
                    status="mostly_met",
                    status_label="Überwiegend erfüllt",
                    feedback="Neu und gezielt geprüft.",
                    next_step="Ergänze den fehlenden Autor.",
                    evidence_quotes=(student_text,),
                ),
            ),
            overall_feedback="Einzelbefund.",
            duration_ms=210,
            queue_duration_ms=5.0,
            execution_duration_ms=180.0,
            provider_request_id="refresh-request",
            reasoning_effort="high",
            evidence_repair_attempts=(
                EvidenceRepairAttempt(
                    criterion_id=first_criterion.criterion_id,
                    prompt_version=EVIDENCE_REPAIR_PROMPT_VERSION,
                    outcome="accepted",
                    duration_ms=30,
                    initial_provider_request_id="refresh-request-initial",
                    provider_request_id="refresh-request",
                    resolved_to_assessable=True,
                ),
            ),
        )

        with patch.object(
            main.criterion_wise_rubric_feedback_service,
            "analyze_criterion",
            new=AsyncMock(return_value=refreshed_result),
        ) as refresh_analysis:
            response = self.client.post(
                f"/feedback-runs/{feedback_run_id}/criteria/"
                f"{first_criterion.criterion_id}/refresh",
                data={
                    "csrf_token": self.csrf_token,
                    "student_text": student_text,
                    "provider": "openai",
                    "openai_model": main.settings.openai_model,
                    "openai_reasoning_effort": "high",
                    "openai_api_key": "test-api-key",
                },
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["x-criterion-refresh-outcome"],
            "success",
        )
        refresh_analysis.assert_awaited_once()
        refresh_arguments = refresh_analysis.await_args.kwargs
        self.assertEqual(
            refresh_arguments["criterion_id"],
            first_criterion.criterion_id,
        )
        self.assertEqual(
            refresh_arguments["original_text"],
            original_text,
        )
        self.assertEqual(
            refresh_arguments["task"].snapshot(),
            task.snapshot(),
        )
        self.assertIn("Neu und gezielt geprüft.", response.text)
        self.assertIn("Überwiegend erfüllt", response.text)
        self.assertIn("data-refresh-criterion", response.text)
        self.assertIn(
            "einzeln neu analysiert und im",
            response.text,
        )
        self.assertNotIn(
            "Unverändertes zweites Feedback.",
            response.text,
        )

        selected = asyncio.run(
            self.store.select_feedback_run_for_evaluation(
                feedback_run_id=feedback_run_id,
                student_text=student_text,
            )
        )
        self.assertEqual(
            selected.feedback_payload["criteria"][0]["feedback"],
            "Neu und gezielt geprüft.",
        )
        self.assertEqual(
            selected.feedback_payload["criteria"][1]["feedback"],
            "Unverändertes zweites Feedback.",
        )
        self.assertEqual(
            selected.feedback_payload["overall_feedback"],
            CRITERION_REFRESH_OVERALL_FEEDBACK,
        )
        self.assertEqual(
            selected.generation_context["criterion_refreshes"]["count"],
            1,
        )
        refresh_item = selected.generation_context[
            "criterion_refreshes"
        ]["items"][0]
        self.assertEqual(
            refresh_item["evidence_repair_attempts"][0]["outcome"],
            "accepted",
        )

        overview = self.client.get("/feedback-evaluations")
        self.assertIn("Einzelaktualisierungen", overview.text)
        self.assertIn("zusätzlicher Modellaufruf", overview.text)

    def test_criterion_wise_feedback_is_selectable_and_persisted(
        self,
    ) -> None:
        task = self._create_task()
        result = RubricFeedbackResult(
            provider="ollama",
            model="mistral-small3.2:24b-instruct-2506-q8_0",
            task_id=task.task_id,
            task_title=task.title,
            rubric_title=task.rubric.title,
            criteria_feedback=tuple(
                CriterionFeedbackResult(
                    criterion_id=criterion.criterion_id,
                    criterion_title=criterion.title,
                    criterion_text=criterion.text,
                    status="mostly_met",
                    status_label="Überwiegend erfüllt",
                    feedback="Dieser Einzelbefund ist belegt.",
                    next_step="Prüfe diesen Aspekt gezielt.",
                    evidence_quotes=("Anonymisierter Schülertext",),
                )
                for criterion in task.rubric.criteria
            ),
            overall_feedback=(
                "Die Kriterien wurden getrennt analysiert."
            ),
            duration_ms=420,
            pipeline_mode=CRITERION_WISE_FEEDBACK_MODE,
            pipeline_label=CRITERION_WISE_FEEDBACK_LABEL,
            prompt_version=CRITERION_WISE_PIPELINE_VERSION,
            criterion_prompt_version=RUBRIC_FEEDBACK_PROMPT_VERSION,
            criterion_request_count=2,
            criterion_request_durations_ms=(180, 240),
            criterion_provider_request_ids=("request-1", "request-2"),
            evidence_repair_attempts=(
                EvidenceRepairAttempt(
                    criterion_id=task.rubric.criteria[0].criterion_id,
                    prompt_version=EVIDENCE_REPAIR_PROMPT_VERSION,
                    outcome="accepted",
                    duration_ms=40,
                    initial_provider_request_id="request-1a",
                    provider_request_id="request-1b",
                    resolved_to_assessable=True,
                ),
            ),
        )

        with (
            patch.object(
                main.criterion_wise_rubric_feedback_service,
                "analyze_text",
                new=AsyncMock(return_value=result),
            ) as criterion_wise_analysis,
            patch.object(
                main.rubric_feedback_service,
                "analyze_text",
                new=AsyncMock(),
            ) as joint_analysis,
            patch.object(
                main.two_pass_rubric_feedback_service,
                "analyze_text",
                new=AsyncMock(),
            ) as two_pass_analysis,
        ):
            response = self.client.post(
                "/analyze",
                data={
                    "csrf_token": self.csrf_token,
                    "task_id": task.task_id,
                    "student_text": "Anonymisierter Schülertext",
                    "provider": "ollama",
                    "ollama_base_url": main.settings.ollama_base_url,
                    "ollama_model": main.settings.ollama_model,
                    "rubric_analysis_mode": "criterion_wise",
                },
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["x-analysis-outcome"],
            "success",
        )
        criterion_wise_analysis.assert_awaited_once()
        joint_analysis.assert_not_awaited()
        two_pass_analysis.assert_not_awaited()
        self.assertIn("data-criterion-wise-result", response.text)
        self.assertIn(
            "Kriterienweise Analyse abgeschlossen",
            response.text,
        )
        self.assertIn("Reguläre Kriterienaufrufe", response.text)
        self.assertIn("Zusätzliche Belegreparaturen", response.text)
        self.assertIn("180 ms", response.text)
        self.assertIn("240 ms", response.text)

        run_match = re.search(
            r'action="/feedback-runs/([^/]+)/save"',
            response.text,
        )
        self.assertIsNotNone(run_match)
        feedback_run_id = (
            run_match.group(1) if run_match is not None else ""
        )
        save_response = self.client.post(
            f"/feedback-runs/{feedback_run_id}/save",
            data={
                "csrf_token": self.csrf_token,
                "student_text": "Anonymisierter Schülertext",
            },
            follow_redirects=False,
        )
        self.assertEqual(save_response.status_code, 303)

        selected = asyncio.run(
            self.store.get_feedback_run_for_evaluation(feedback_run_id)
        )
        self.assertEqual(
            selected.feedback_mode,
            CRITERION_WISE_FEEDBACK_MODE,
        )
        self.assertEqual(
            selected.feedback_mode_label,
            CRITERION_WISE_FEEDBACK_LABEL,
        )
        self.assertEqual(
            selected.generation_context["criterion_requests"]["count"],
            2,
        )
        self.assertEqual(
            selected.generation_context["criterion_requests"][
                "durations_ms"
            ],
            [180, 240],
        )

        overview = self.client.get("/feedback-evaluations")
        self.assertIn(CRITERION_WISE_FEEDBACK_LABEL, overview.text)
        self.assertIn(CRITERION_WISE_PIPELINE_VERSION, overview.text)
        self.assertIn("Getrennte Modellaufrufe", overview.text)
        self.assertIn("je ein Aufruf pro Kriterium", overview.text)

    def test_criterion_wise_feedback_is_server_default_for_task(
        self,
    ) -> None:
        task = self._create_task()
        result = RubricFeedbackResult(
            provider="ollama",
            model=main.settings.ollama_model,
            task_id=task.task_id,
            task_title=task.title,
            rubric_title=task.rubric.title,
            criteria_feedback=tuple(
                CriterionFeedbackResult(
                    criterion_id=criterion.criterion_id,
                    criterion_title=criterion.title,
                    criterion_text=criterion.text,
                    status="mostly_met",
                    status_label="Überwiegend erfüllt",
                    feedback="Belegter fokussierter Befund.",
                    next_step="Überarbeite diesen Aspekt gezielt.",
                    evidence_quotes=("Anonymisierter Schülertext",),
                )
                for criterion in task.rubric.criteria
            ),
            overall_feedback="Kriterienweise Standardanalyse.",
            duration_ms=300,
            pipeline_mode=CRITERION_WISE_FEEDBACK_MODE,
            pipeline_label=CRITERION_WISE_FEEDBACK_LABEL,
            prompt_version=CRITERION_WISE_PIPELINE_VERSION,
            criterion_prompt_version=RUBRIC_FEEDBACK_PROMPT_VERSION,
            criterion_request_count=2,
            criterion_request_durations_ms=(140, 160),
        )

        with (
            patch.object(
                main.criterion_wise_rubric_feedback_service,
                "analyze_text",
                new=AsyncMock(return_value=result),
            ) as criterion_wise_analysis,
            patch.object(
                main.rubric_feedback_service,
                "analyze_text",
                new=AsyncMock(),
            ) as joint_analysis,
        ):
            response = self.client.post(
                "/analyze",
                data={
                    "csrf_token": self.csrf_token,
                    "task_id": task.task_id,
                    "student_text": "Anonymisierter Schülertext",
                    "provider": "ollama",
                    "ollama_model": main.settings.ollama_model,
                },
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-analysis-outcome"], "success")
        criterion_wise_analysis.assert_awaited_once()
        joint_analysis.assert_not_awaited()
        self.assertIn("Kriterienweise Standardanalyse", response.text)

    def test_two_pass_feedback_is_opt_in_visible_and_persisted(
        self,
    ) -> None:
        task = self._create_task()
        result = RubricFeedbackResult(
            provider="openai",
            model="gpt-5.6-sol",
            task_id=task.task_id,
            task_title=task.title,
            rubric_title=task.rubric.title,
            criteria_feedback=tuple(
                CriterionFeedbackResult(
                    criterion_id=criterion.criterion_id,
                    criterion_title=criterion.title,
                    criterion_text=criterion.text,
                    status="mostly_met",
                    status_label="Überwiegend erfüllt",
                    feedback=(
                        "Stärke: Der Befund ist belegt. "
                        "Verbesserung: Ergänze einen wichtigen Punkt."
                    ),
                    next_step="Prüfe den genannten Punkt am Text.",
                    evidence_quotes=("Anonymisierter Schülertext",),
                    evidence_verified=True,
                )
                for criterion in task.rubric.criteria
            ),
            overall_feedback=(
                "Nutze nur die in zwei Schritten geprüften Hinweise."
            ),
            duration_ms=330,
            reasoning_effort="max",
            pipeline_mode=TWO_PASS_FEEDBACK_MODE,
            pipeline_label=TWO_PASS_FEEDBACK_LABEL,
            prompt_version=TWO_PASS_PIPELINE_VERSION,
            evidence_validation_version=(
                TWO_PASS_EVIDENCE_VALIDATION_VERSION
            ),
            analysis_prompt_version=(
                TWO_PASS_ANALYSIS_PROMPT_VERSION
            ),
            review_prompt_version=TWO_PASS_REVIEW_PROMPT_VERSION,
            analysis_duration_ms=120,
            review_duration_ms=180,
            candidate_finding_count=4,
            validated_candidate_count=3,
            accepted_finding_count=2,
            rejected_finding_count=2,
            analysis_provider_request_id="analysis-request",
            review_provider_request_id="review-request",
            pipeline_warnings=(
                "Ein unbelegter Kandidat wurde technisch verworfen.",
            ),
        )

        with (
            patch.object(
                main.two_pass_rubric_feedback_service,
                "analyze_text",
                new=AsyncMock(return_value=result),
            ) as two_pass_analysis,
            patch.object(
                main.rubric_feedback_service,
                "analyze_text",
                new=AsyncMock(),
            ) as one_pass_analysis,
            patch.object(
                main.feedback_service,
                "analyze_text",
                new=AsyncMock(),
            ) as standard_analysis,
        ):
            response = self.client.post(
                "/analyze",
                data={
                    "csrf_token": self.csrf_token,
                    "task_id": task.task_id,
                    "student_text": "Anonymisierter Schülertext",
                    "provider": "openai",
                    "openai_model": "gpt-5.6-sol",
                    "openai_reasoning_effort": "max",
                    "openai_api_key": "test-api-key",
                    "rubric_analysis_mode": "two_pass",
                },
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["x-analysis-outcome"],
            "success",
        )
        two_pass_analysis.assert_awaited_once()
        one_pass_analysis.assert_not_awaited()
        standard_analysis.assert_not_awaited()
        self.assertIn("data-two-pass-result", response.text)
        self.assertIn(
            "Zwei-Pass-Prüfung abgeschlossen",
            response.text,
        )
        self.assertIn(TWO_PASS_FEEDBACK_LABEL, response.text)
        self.assertIn("120 ms", response.text)
        self.assertIn("180 ms", response.text)
        self.assertIn("Technische Eingriffe anzeigen", response.text)

        run_match = re.search(
            r'action="/feedback-runs/([^/]+)/save"',
            response.text,
        )
        self.assertIsNotNone(run_match)
        feedback_run_id = (
            run_match.group(1) if run_match is not None else ""
        )
        save_response = self.client.post(
            f"/feedback-runs/{feedback_run_id}/save",
            data={
                "csrf_token": self.csrf_token,
                "student_text": "Anonymisierter Schülertext",
            },
            follow_redirects=False,
        )

        self.assertEqual(save_response.status_code, 303)
        selected = asyncio.run(
            self.store.get_feedback_run_for_evaluation(feedback_run_id)
        )
        self.assertEqual(selected.feedback_mode, TWO_PASS_FEEDBACK_MODE)
        self.assertEqual(
            selected.feedback_mode_label,
            TWO_PASS_FEEDBACK_LABEL,
        )
        self.assertEqual(
            selected.generation_prompt_version,
            TWO_PASS_PIPELINE_VERSION,
        )
        self.assertEqual(
            selected.generation_context["phase_prompt_versions"],
            {
                "candidate_analysis": (
                    TWO_PASS_ANALYSIS_PROMPT_VERSION
                ),
                "restricted_review": TWO_PASS_REVIEW_PROMPT_VERSION,
            },
        )
        self.assertEqual(
            selected.generation_context["finding_counts"],
            {
                "candidate": 4,
                "technically_validated": 3,
                "accepted": 2,
                "rejected": 2,
            },
        )

        overview = self.client.get("/feedback-evaluations")
        self.assertIn(TWO_PASS_FEEDBACK_LABEL, overview.text)
        self.assertIn(TWO_PASS_PIPELINE_VERSION, overview.text)
        self.assertIn("4 Kandidaten", overview.text)
        self.assertIn("2 übernommen", overview.text)
        self.assertIn("2 verworfen", overview.text)

    def test_two_pass_feedback_supports_runpod_provider(
        self,
    ) -> None:
        task = self._create_task()
        provider_override = object()
        result = RubricFeedbackResult(
            provider="runpod",
            model=main.settings.runpod_model,
            task_id=task.task_id,
            task_title=task.title,
            rubric_title=task.rubric.title,
            criteria_feedback=tuple(
                CriterionFeedbackResult(
                    criterion_id=criterion.criterion_id,
                    criterion_title=criterion.title,
                    criterion_text=criterion.text,
                    status="mostly_met",
                    status_label="Überwiegend erfüllt",
                    feedback="Der Befund wurde in zwei Phasen geprüft.",
                    next_step="Prüfe den genannten Aspekt.",
                    evidence_quotes=("Anonymisierter Schülertext",),
                )
                for criterion in task.rubric.criteria
            ),
            overall_feedback="Zwei-Pass-Prüfung über RunPod.",
            duration_ms=450,
            pipeline_mode=TWO_PASS_FEEDBACK_MODE,
            pipeline_label=TWO_PASS_FEEDBACK_LABEL,
            prompt_version=TWO_PASS_PIPELINE_VERSION,
        )

        with (
            patch.object(
                main,
                "_provider_for_request",
                return_value=provider_override,
            ) as provider_factory,
            patch.object(
                main.two_pass_rubric_feedback_service,
                "analyze_text",
                new=AsyncMock(return_value=result),
            ) as two_pass_analysis,
            patch.object(
                main.rubric_feedback_service,
                "analyze_text",
                new=AsyncMock(),
            ) as one_pass_analysis,
        ):
            response = self.client.post(
                "/analyze",
                data={
                    "csrf_token": self.csrf_token,
                    "task_id": task.task_id,
                    "student_text": "Anonymisierter Schülertext",
                    "provider": "runpod",
                    "runpod_endpoint": "rtx5090_32gb",
                    "runpod_tracking_id": (
                        "265d345e-1843-49af-b5aa-d875807fa504"
                    ),
                    "two_pass_feedback": "true",
                },
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["x-analysis-outcome"],
            "success",
        )
        provider_factory.assert_called_once()
        two_pass_analysis.assert_awaited_once()
        one_pass_analysis.assert_not_awaited()
        analysis_arguments = two_pass_analysis.await_args.kwargs
        self.assertEqual(analysis_arguments["provider_key"], "runpod")
        self.assertIs(
            analysis_arguments["provider_override"],
            provider_override,
        )
        self.assertIn(
            "Zwei-Pass-Prüfung über RunPod",
            response.text,
        )

    def test_empty_task_selection_can_be_saved_for_meta_evaluation(
        self,
    ) -> None:
        student_text = "Anonymisierter Schülertext"
        old_result = FeedbackResult(
            provider="openai",
            model=main.settings.openai_model,
            feedback="Bisheriges Gesamtfeedback",
            duration_ms=250,
        )

        with (
            patch.object(
                main.feedback_service,
                "analyze_text",
                new=AsyncMock(return_value=old_result),
            ) as old_analysis,
            patch.object(
                main.rubric_feedback_service,
                "analyze_text",
                new=AsyncMock(),
            ) as rubric_analysis,
        ):
            response = self.client.post(
                "/analyze",
                data={
                    "csrf_token": self.csrf_token,
                    "task_id": "",
                    "student_text": student_text,
                    "provider": "openai",
                    "openai_model": main.settings.openai_model,
                    "openai_api_key": "test-api-key",
                    "advanced_options": "true",
                },
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        self.assertEqual(response.status_code, 200)
        old_analysis.assert_awaited_once()
        rubric_analysis.assert_not_awaited()
        self.assertIn("Bisheriges Gesamtfeedback", response.text)
        self.assertNotIn("Kriterium 1", response.text)
        self.assertIn(
            "Für Feedback-Bewertung speichern",
            response.text,
        )

        run_match = re.search(
            r'action="/feedback-runs/([^/]+)/save"',
            response.text,
        )
        self.assertIsNotNone(run_match)
        feedback_run_id = (
            run_match.group(1) if run_match is not None else ""
        )
        self.assertEqual(asyncio.run(self.store.list_tasks()), [])
        self.assertEqual(
            asyncio.run(
                self.store.list_tasks(include_archived=True)
            ),
            [],
        )

        save_response = self.client.post(
            f"/feedback-runs/{feedback_run_id}/save",
            data={
                "csrf_token": self.csrf_token,
                "student_text": student_text,
            },
            follow_redirects=False,
        )
        self.assertEqual(save_response.status_code, 303)

        selected = asyncio.run(
            self.store.get_feedback_run_for_evaluation(feedback_run_id)
        )
        self.assertTrue(selected.is_standard_feedback)
        self.assertEqual(selected.feedback_mode, STANDARD_FEEDBACK_MODE)
        self.assertEqual(selected.criterion_count, 0)
        self.assertEqual(
            selected.generation_prompt_version,
            STANDARD_FEEDBACK_PROMPT_VERSION,
        )
        self.assertIn(
            STANDARD_FEEDBACK_STUDENT_TEXT_PLACEHOLDER,
            selected.generation_prompt_template or "",
        )
        self.assertNotIn(
            student_text,
            selected.generation_prompt_template or "",
        )

        overview = self.client.get("/feedback-evaluations")
        self.assertEqual(overview.status_code, 200)
        self.assertIn("Kontextarmes Standardfeedback", overview.text)
        self.assertIn("Bewusst reduzierter Erzeugungskontext", overview.text)
        self.assertIn(STANDARD_FEEDBACK_PROMPT_VERSION, overview.text)
        self.assertIn("Verwendete Systemnachricht", overview.text)
        self.assertIn("Verwendete Standardprompt-Vorlage", overview.text)
        self.assertIn("Gesamtfeedback", overview.text)
        self.assertNotIn("Einzelfeedbacks", overview.text)
        self.assertIn("Automatische Vorbewertung", overview.text)
        self.assertIn("Manuell bewerten", overview.text)

    def test_automatic_prerating_can_be_manually_adjusted_and_saved(
        self,
    ) -> None:
        task = self._create_task()
        student_text = "Anonymisierter Schülertext für die Vorbewertung."
        feedback_run_id = asyncio.run(
            self.store.save_feedback_run(
                task=task,
                student_text=student_text,
                provider="mistral",
                model="feedback-model",
                duration_ms=321,
                feedback_payload={
                    "criteria": [
                        {
                            "criterion_id": "criterion-1",
                            "criterion_title": "Einleitung",
                            "status": "partially_met",
                            "feedback": "Der Titel ist vorhanden.",
                            "next_step": "Ergänze Autor und Thema.",
                        }
                    ],
                    "overall_feedback": "Ein brauchbarer Anfang.",
                },
            )
        )
        asyncio.run(
            self.store.select_feedback_run_for_evaluation(
                feedback_run_id=feedback_run_id,
                student_text=student_text,
            )
        )
        scores = {
            "factual_correctness": 2,
            "transparency_reasoning": 3,
            "audience_context_fit": 2,
            "action_learning_activation": 1,
        }
        justifications = {
            key: (
                f"Detaillierte automatische Prüfung für {key}: Das "
                "Feedback enthält einen konkreten Befund und einen "
                "Überarbeitungsschritt. Die Aussage wurde gegen Aufgabe, "
                "Kriterien und Schülertext abgeglichen; eine klar "
                "bezeichnete Verbesserung bleibt erforderlich."
            )
            for key in scores
        }
        ratings = MANUAL_META_EVALUATION_RUBRIC.build_ratings(
            scores=scores,
            justifications=justifications,
        )
        result = AutomaticFeedbackEvaluationResult(
            provider="openai",
            model="gpt-5.6-sol",
            prompt_version=AUTOMATIC_EVALUATION_PROMPT_VERSION,
            duration_ms=2345,
            provider_request_id="resp-auto-web",
            ratings=ratings,
        )
        configured_provider = SimpleNamespace(
            configured=True,
            provider_name="openai",
            model_name="gpt-5.6-sol",
        )

        with (
            patch.object(
                main,
                "automatic_evaluation_provider",
                configured_provider,
            ),
            patch.object(
                main.automatic_feedback_evaluation_service,
                "evaluate",
                new=AsyncMock(return_value=result),
            ) as evaluate,
        ):
            overview = self.client.get("/feedback-evaluations")
            self.assertIn("Automatische Vorbewertung", overview.text)
            self.assertIn("Jetzt automatisch vorbewerten", overview.text)
            self.assertRegex(
                overview.text,
                r'/static/feedback_evaluations\.js\?v=[0-9a-f]{12}',
            )
            self.assertIn(
                "data-automatic-evaluation-form",
                overview.text,
            )
            self.assertIn(
                "data-automatic-evaluation-loading",
                overview.text,
            )
            self.assertIn(
                f'data-feedback-run-id="{feedback_run_id}"',
                overview.text,
            )
            self.assertIn(
                "data-automatic-evaluation-client-error",
                overview.text,
            )
            self.assertIn(
                "Name der Vorbewertung (optional)",
                overview.text,
            )
            self.assertIn(
                "an die OpenAI API übertragen",
                overview.text,
            )

            automatic_response = self.client.post(
                (
                    f"/feedback-runs/{feedback_run_id}/"
                    "automatic-evaluations"
                ),
                data={
                    "csrf_token": self.csrf_token,
                    "evaluation_name": "Sol max – Vorprüfung",
                },
                follow_redirects=False,
            )

            self.assertEqual(automatic_response.status_code, 303)
            self.assertEqual(
                automatic_response.headers["location"],
                (
                    "/feedback-evaluations?notice="
                    "automatic-evaluation-saved"
                    f"&automatic_feedback_run_id={feedback_run_id}"
                    f"#feedback-run-{feedback_run_id}"
                ),
            )
            evaluate.assert_awaited_once()

            prerated_overview = self.client.get(
                (
                    "/feedback-evaluations?notice="
                    "automatic-evaluation-saved"
                    f"&automatic_feedback_run_id={feedback_run_id}"
                )
            )

        self.assertIn("KI-Vorbewertung manuell prüfen", prerated_overview.text)
        self.assertIn(
            "data-open-after-automatic-evaluation",
            prerated_overview.text,
        )
        self.assertIn(
            "Vorbewertung abgeschlossen",
            prerated_overview.text,
        )
        self.assertIn(
            "meta-automatic-result-success",
            prerated_overview.text,
        )
        self.assertIn("gpt-5.6-sol", prerated_overview.text)
        self.assertIn(
            AUTOMATIC_EVALUATION_PROMPT_VERSION,
            prerated_overview.text,
        )
        self.assertIn("Sol max – Vorprüfung", prerated_overview.text)
        self.assertIn("Bewertung löschen", prerated_overview.text)
        self.assertIn(
            "data-confirm-evaluation-delete",
            prerated_overview.text,
        )
        self.assertIn("resp-auto-web", prerated_overview.text)
        self.assertIn(justifications["factual_correctness"], prerated_overview.text)
        self.assertRegex(
            prerated_overview.text,
            r'name="score_factual_correctness"\s+value="2"\s+checked',
        )
        automatic_evaluation = asyncio.run(
            self.store.get_feedback_run_for_evaluation(feedback_run_id)
        ).latest_automatic_evaluation
        self.assertIsNotNone(automatic_evaluation)
        automatic_evaluation_id = (
            automatic_evaluation.evaluation_id
            if automatic_evaluation is not None
            else ""
        )
        self.assertIn(
            f'value="{automatic_evaluation_id}"',
            prerated_overview.text,
        )

        manual_response = self.client.post(
            f"/feedback-runs/{feedback_run_id}/manual-evaluations",
            data={
                "csrf_token": self.csrf_token,
                "source_evaluation_id": automatic_evaluation_id,
                "evaluation_name": "Manuelle Schlussprüfung",
                "score_factual_correctness": "3",
                "justification_factual_correctness": (
                    "Manuell auf drei Punkte angehoben und geprüft."
                ),
                "score_transparency_reasoning": "3",
                "justification_transparency_reasoning": (
                    justifications["transparency_reasoning"]
                ),
                "score_audience_context_fit": "2",
                "justification_audience_context_fit": (
                    justifications["audience_context_fit"]
                ),
                "score_action_learning_activation": "1",
                "justification_action_learning_activation": (
                    justifications["action_learning_activation"]
                ),
            },
            follow_redirects=False,
        )

        self.assertEqual(manual_response.status_code, 303)
        stored_run = asyncio.run(
            self.store.get_feedback_run_for_evaluation(feedback_run_id)
        )
        self.assertEqual(stored_run.automatic_evaluation_count, 1)
        self.assertEqual(stored_run.manual_evaluation_count, 1)
        self.assertEqual(len(stored_run.evaluations), 2)
        self.assertEqual(stored_run.evaluations[0].evaluation_type, "manual")
        self.assertEqual(
            stored_run.evaluations[0].evaluation_name,
            "Manuelle Schlussprüfung",
        )
        self.assertEqual(stored_run.evaluations[0].ratings[0].score, 3)
        self.assertEqual(
            stored_run.evaluations[0].source_evaluation_id,
            automatic_evaluation_id,
        )
        self.assertEqual(stored_run.evaluations[1].evaluation_type, "automatic")
        self.assertEqual(
            stored_run.evaluations[1].evaluation_name,
            "Sol max – Vorprüfung",
        )
        self.assertEqual(stored_run.evaluations[1].ratings[0].score, 2)

        linked_delete_response = self.client.post(
            (
                f"/feedback-runs/{feedback_run_id}/evaluations/"
                f"{automatic_evaluation_id}/delete"
            ),
            data={"csrf_token": self.csrf_token},
            follow_redirects=False,
        )
        self.assertEqual(linked_delete_response.status_code, 303)
        self.assertIn(
            "notice=evaluation-delete-linked",
            linked_delete_response.headers["location"],
        )

        manual_evaluation_id = stored_run.evaluations[0].evaluation_id
        manual_delete_response = self.client.post(
            (
                f"/feedback-runs/{feedback_run_id}/evaluations/"
                f"{manual_evaluation_id}/delete"
            ),
            data={"csrf_token": self.csrf_token},
            follow_redirects=False,
        )
        self.assertEqual(manual_delete_response.status_code, 303)
        self.assertIn(
            "notice=evaluation-deleted",
            manual_delete_response.headers["location"],
        )

        automatic_delete_response = self.client.post(
            (
                f"/feedback-runs/{feedback_run_id}/evaluations/"
                f"{automatic_evaluation_id}/delete"
            ),
            data={"csrf_token": self.csrf_token},
            follow_redirects=False,
        )
        self.assertEqual(automatic_delete_response.status_code, 303)
        after_deletion = asyncio.run(
            self.store.get_feedback_run_for_evaluation(feedback_run_id)
        )
        self.assertEqual(after_deletion.evaluations, ())

    def test_feedback_run_selection_requires_valid_csrf_token(self) -> None:
        task = self._create_task()
        feedback_run_id = asyncio.run(
            self.store.save_feedback_run(
                task=task,
                student_text="Anonymisierter Schülertext",
                provider="openai",
                model="test-model",
                duration_ms=100,
                feedback_payload={
                    "criteria": [],
                    "overall_feedback": "Testfeedback",
                },
            )
        )

        response = self.client.post(
            f"/feedback-runs/{feedback_run_id}/save",
            data={
                "csrf_token": "ungueltig",
                "student_text": "Anonymisierter Schülertext",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            asyncio.run(
                self.store.list_feedback_runs_for_evaluation()
            ),
            [],
        )

    def test_feedback_run_can_be_removed_from_evaluation_overview(
        self,
    ) -> None:
        task = self._create_task()
        student_text = "Anonymisierter Schülertext zum Aufräumen."
        feedback_run_id = asyncio.run(
            self.store.save_feedback_run(
                task=task,
                student_text=student_text,
                provider="openai",
                model="test-model",
                duration_ms=100,
                feedback_payload={
                    "criteria": [],
                    "overall_feedback": "Testfeedback",
                },
            )
        )
        asyncio.run(
            self.store.select_feedback_run_for_evaluation(
                feedback_run_id=feedback_run_id,
                student_text=student_text,
            )
        )
        criterion_keys = [
            criterion.key
            for criterion in MANUAL_META_EVALUATION_RUBRIC.criteria
        ]
        asyncio.run(
            self.store.create_manual_feedback_evaluation(
                feedback_run_id=feedback_run_id,
                scores={key: 2 for key in criterion_keys},
                justifications={
                    key: "Ausreichend konkrete Testbegründung."
                    for key in criterion_keys
                },
            )
        )

        overview = self.client.get("/feedback-evaluations")
        self.assertIn("Feedbackbogen entfernen", overview.text)
        self.assertIn(
            "data-confirm-feedback-run-remove",
            overview.text,
        )
        self.assertIn(
            (
                f'action="/feedback-runs/{feedback_run_id}/'
                'remove-from-evaluation"'
            ),
            overview.text,
        )

        response = self.client.post(
            (
                f"/feedback-runs/{feedback_run_id}/"
                "remove-from-evaluation"
            ),
            data={"csrf_token": self.csrf_token},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            (
                "/feedback-evaluations?notice="
                "feedback-run-removed"
            ),
        )
        self.assertEqual(
            asyncio.run(
                self.store.list_feedback_runs_for_evaluation()
            ),
            [],
        )
        self.assertEqual(
            asyncio.run(self.store.count_feedback_runs()),
            1,
        )

        removed_overview = self.client.get(
            response.headers["location"]
        )
        self.assertIn(
            "Der Feedbackbogen und alle zugehörigen Bewertungen wurden",
            removed_overview.text,
        )
        self.assertIn(
            "Noch keine Feedbackläufe ausgewählt",
            removed_overview.text,
        )

    def test_failed_automatic_prerating_stores_no_partial_record(
        self,
    ) -> None:
        task = self._create_task()
        student_text = "Anonymisierter Schülertext"
        feedback_run_id = asyncio.run(
            self.store.save_feedback_run(
                task=task,
                student_text=student_text,
                provider="openai",
                model="feedback-model",
                duration_ms=100,
                feedback_payload={
                    "criteria": [],
                    "overall_feedback": "Testfeedback",
                },
            )
        )
        asyncio.run(
            self.store.select_feedback_run_for_evaluation(
                feedback_run_id=feedback_run_id,
                student_text=student_text,
            )
        )

        with patch.object(
            main.automatic_feedback_evaluation_service,
            "evaluate",
            new=AsyncMock(
                side_effect=RuntimeError("Simulierter Providerfehler")
            ),
        ):
            response = self.client.post(
                (
                    f"/feedback-runs/{feedback_run_id}/"
                    "automatic-evaluations"
                ),
                data={"csrf_token": self.csrf_token},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            (
                "/feedback-evaluations?notice="
                "automatic-evaluation-failed"
                f"&automatic_feedback_run_id={feedback_run_id}"
                f"#feedback-run-{feedback_run_id}"
            ),
        )
        failed_overview = self.client.get(
            (
                "/feedback-evaluations?notice="
                "automatic-evaluation-failed"
                f"&automatic_feedback_run_id={feedback_run_id}"
            )
        )
        self.assertIn(
            "Vorbewertung fehlgeschlagen",
            failed_overview.text,
        )
        self.assertIn(
            "meta-automatic-result-error",
            failed_overview.text,
        )
        stored_run = asyncio.run(
            self.store.get_feedback_run_for_evaluation(feedback_run_id)
        )
        self.assertEqual(stored_run.evaluations, ())

    def test_automatic_evaluation_script_keeps_request_visible(
        self,
    ) -> None:
        script = (
            Path(__file__).parents[1]
            / "app"
            / "static"
            / "feedback_evaluations.js"
        ).read_text(encoding="utf-8")

        self.assertIn("Vorbewertung läuft …", script)
        self.assertIn("updateElapsed", script)
        self.assertIn("setInterval", script)
        self.assertIn("SLOW_EVALUATION_DELAY_MS", script)
        self.assertIn("await fetch(form.action", script)
        self.assertIn('"X-Requested-With": "XMLHttpRequest"', script)
        self.assertIn(
            "HTMLFormElement.prototype.submit.call(form)",
            script,
        )
        self.assertIn(
            "data-open-after-automatic-evaluation",
            script,
        )
        self.assertIn("data-confirm-evaluation-delete", script)
        self.assertIn("data-confirm-feedback-run-remove", script)
        self.assertIn("Der gespeicherte Schülertext wird entfernt", script)
        self.assertIn("window.confirm", script)

    def test_manual_evaluation_requires_valid_csrf_token(self) -> None:
        task = self._create_task()
        student_text = "Anonymisierter Schülertext"
        feedback_run_id = asyncio.run(
            self.store.save_feedback_run(
                task=task,
                student_text=student_text,
                provider="openai",
                model="test-model",
                duration_ms=100,
                feedback_payload={
                    "criteria": [],
                    "overall_feedback": "Testfeedback",
                },
            )
        )
        asyncio.run(
            self.store.select_feedback_run_for_evaluation(
                feedback_run_id=feedback_run_id,
                student_text=student_text,
            )
        )

        response = self.client.post(
            f"/feedback-runs/{feedback_run_id}/manual-evaluations",
            data={
                "csrf_token": "ungueltig",
                "score_factual_correctness": "3",
                "justification_factual_correctness": "Begründung",
                "score_transparency_reasoning": "3",
                "justification_transparency_reasoning": "Begründung",
                "score_audience_context_fit": "3",
                "justification_audience_context_fit": "Begründung",
                "score_action_learning_activation": "3",
                "justification_action_learning_activation": "Begründung",
            },
        )

        self.assertEqual(response.status_code, 403)
        with patch.object(
            main.automatic_feedback_evaluation_service,
            "evaluate",
            new=AsyncMock(),
        ) as automatic_evaluate:
            automatic_response = self.client.post(
                (
                    f"/feedback-runs/{feedback_run_id}/"
                    "automatic-evaluations"
                ),
                data={"csrf_token": "ungueltig"},
            )

        self.assertEqual(automatic_response.status_code, 403)
        automatic_evaluate.assert_not_awaited()
        selected_run = asyncio.run(
            self.store.list_feedback_runs_for_evaluation()
        )[0]
        self.assertEqual(selected_run.evaluations, ())

    def test_evaluation_deletion_requires_valid_csrf_token(self) -> None:
        task = self._create_task()
        student_text = "Anonymisierter Schülertext"
        feedback_run_id = asyncio.run(
            self.store.save_feedback_run(
                task=task,
                student_text=student_text,
                provider="openai",
                model="test-model",
                duration_ms=100,
                feedback_payload={
                    "criteria": [],
                    "overall_feedback": "Testfeedback",
                },
            )
        )
        asyncio.run(
            self.store.select_feedback_run_for_evaluation(
                feedback_run_id=feedback_run_id,
                student_text=student_text,
            )
        )
        criterion_keys = [
            criterion.key
            for criterion in MANUAL_META_EVALUATION_RUBRIC.criteria
        ]
        evaluation = asyncio.run(
            self.store.create_manual_feedback_evaluation(
                feedback_run_id=feedback_run_id,
                scores={key: 2 for key in criterion_keys},
                justifications={
                    key: "Ausreichend konkrete Testbegründung."
                    for key in criterion_keys
                },
            )
        )

        response = self.client.post(
            (
                f"/feedback-runs/{feedback_run_id}/evaluations/"
                f"{evaluation.evaluation_id}/delete"
            ),
            data={"csrf_token": "ungueltig"},
        )

        self.assertEqual(response.status_code, 403)
        stored = asyncio.run(
            self.store.get_feedback_run_for_evaluation(feedback_run_id)
        )
        self.assertEqual(len(stored.evaluations), 1)

        remove_response = self.client.post(
            (
                f"/feedback-runs/{feedback_run_id}/"
                "remove-from-evaluation"
            ),
            data={"csrf_token": "ungueltig"},
        )

        self.assertEqual(remove_response.status_code, 403)
        still_selected = asyncio.run(
            self.store.get_feedback_run_for_evaluation(feedback_run_id)
        )
        self.assertEqual(len(still_selected.evaluations), 1)

    def test_writing_task_requires_valid_csrf_token(self) -> None:
        response = self.client.post(
            "/tasks/new",
            data={
                "csrf_token": "ungueltig",
                "title": "Aufgabe",
                "instructions": "Aufgabenstellung",
                "rubric_title": "Feedback",
                "criteria": ["Kriterium"],
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(asyncio.run(self.store.list_tasks()), [])

    def test_start_page_does_not_fall_back_when_task_store_fails(self) -> None:
        with patch.object(
            self.store,
            "list_tasks",
            new=AsyncMock(
                side_effect=TaskStoreError("SQLite nicht verfügbar")
            ),
        ):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Bitte versuche es nach einem Neustart erneut.",
            response.text,
        )
        self.assertNotIn(
            "Ohne Feedback-Vorlage – bisheriges Gesamtfeedback",
            response.text,
        )
        self.assertIn(
            "Erweiterte Forschungsoptionen anzeigen",
            response.text,
        )
        self.assertNotIn("bewertungsbogen", response.text.lower())
