from __future__ import annotations

import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from httpx import Response

from app import main
from app.services.feedback_service import FeedbackResult
from app.services.rubric_feedback_service import (
    CriterionFeedbackResult,
    RubricFeedbackResult,
)
from app.services.task_store import TaskStore, TaskStoreError


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
            for path in ("/tasks", "/tasks/new"):
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
                        "partially_met"
                        if criterion.position == 0
                        else "not_met"
                    ),
                    status_label=(
                        "Teilweise erfüllt"
                        if criterion.position == 0
                        else "Nicht erfüllt"
                    ),
                    feedback="Konkretes **Feedback**.",
                    next_step="Konkreter *nächster Schritt*.",
                )
                for criterion in task.rubric.criteria
            ),
            overall_feedback="Kurze **Zusammenfassung**.",
            duration_ms=350,
        )

        with (
            patch.object(
                main.rubric_feedback_service,
                "analyze_text",
                new=AsyncMock(return_value=result),
            ) as rubric_analysis,
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
                    "student_text": "Anonymisierter Schülertext",
                    "provider": "openai",
                    "openai_model": main.settings.openai_model,
                    "openai_api_key": "test-api-key",
                },
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        self.assertEqual(response.status_code, 200)
        rubric_analysis.assert_awaited_once()
        old_analysis.assert_not_awaited()
        self.assertIn("Einleitung: Grundangaben", response.text)
        self.assertIn("Teilweise erfüllt", response.text)
        self.assertIn("<h4>Feedback</h4>", response.text)
        self.assertIn("<h4>Überarbeitung</h4>", response.text)
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
            "Kurze <strong>Zusammenfassung</strong>.",
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

    def test_empty_task_selection_keeps_previous_analysis_path(self) -> None:
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
                    "student_text": "Anonymisierter Schülertext",
                    "provider": "openai",
                    "openai_model": main.settings.openai_model,
                    "openai_api_key": "test-api-key",
                },
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        self.assertEqual(response.status_code, 200)
        old_analysis.assert_awaited_once()
        rubric_analysis.assert_not_awaited()
        self.assertIn("Bisheriges Gesamtfeedback", response.text)
        self.assertNotIn("Kriterium 1", response.text)

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

    def test_start_page_keeps_old_path_when_task_store_fails(self) -> None:
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
            "Das bisherige Gesamtfeedback bleibt verfügbar.",
            response.text,
        )
        self.assertIn(
            "Ohne Feedback-Vorlage – bisheriges Gesamtfeedback",
            response.text,
        )
        self.assertNotIn("bewertungsbogen", response.text.lower())
