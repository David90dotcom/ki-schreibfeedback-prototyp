from __future__ import annotations

import asyncio
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from httpx import Response

from app import main
from app.services.rubric_feedback_service import (
    CriterionFeedbackResult,
    RubricFeedbackResult,
)
from app.services.student_account_store import StudentAccountStore
from app.services.student_analysis_gate import StudentAnalysisGate
from app.services.task_store import TaskStore
from app.security import LoginRateLimiter


def _csrf_token_from(response: Response) -> str:
    match = re.search(
        r'name="csrf_token"\s+value="([^"]+)"',
        response.text,
    )

    if match is None:
        raise AssertionError("CSRF-Token fehlt in der Antwort.")

    return match.group(1)


class StudentPortalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "analysis.sqlite3"
        )
        codes = iter(("123456", "654321", "111222", "333444"))
        self.student_store = StudentAccountStore(
            self.database_path,
            code_secret="studenten-test-secret",
            code_factory=lambda: next(codes),
        )
        self.task_store = TaskStore(self.database_path)
        self.patchers = (
            patch.object(main, "student_account_store", self.student_store),
            patch.object(main, "task_store", self.task_store),
            patch.object(main, "student_analysis_gate", StudentAnalysisGate()),
            patch.object(
                main,
                "student_login_rate_limiter",
                LoginRateLimiter(max_attempts=3, window_seconds=60),
            ),
        )

        for patcher in self.patchers:
            patcher.start()

        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        self.client.close()

        for patcher in reversed(self.patchers):
            patcher.stop()

        self.temporary_directory.cleanup()

    def _admin_login(self) -> None:
        csrf_token = _csrf_token_from(self.client.get("/login"))

        with patch.object(main, "verify_credentials", return_value=True):
            response = self.client.post(
                "/login",
                data={
                    "username": main.settings.auth_username,
                    "password": "test-passwort",
                    "csrf_token": csrf_token,
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)

    def _create_account(self, label: str = "Testperson 01"):
        return asyncio.run(self.student_store.create_account(label))

    def _student_login(self, access_code: str) -> Response:
        csrf_token = _csrf_token_from(self.client.get("/schueler"))
        return self.client.post(
            "/schueler/login",
            data={
                "access_code": access_code,
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )

    def _create_task(self):
        return asyncio.run(
            self.task_store.create_task(
                title="Gedichtanalyse Klasse 8",
                subject="Deutsch",
                grade_level="8",
                instructions="Analysiere das Gedicht.",
                material="Ein kurzes Gedicht.",
                rubric_title="Feedback zur Gedichtanalyse",
                criterion_titles=("Einleitung",),
                criteria=("Titel, Autor und Thema nennen.",),
            )
        )

    def test_public_student_link_and_admin_routes_are_separated(self) -> None:
        student_page = self.client.get("/schueler")
        admin_page = self.client.get(
            "/schuelerzugange",
            follow_redirects=False,
        )

        self.assertEqual(student_page.status_code, 200)
        self.assertIn("Schülerzugang", student_page.text)
        self.assertIn('pattern="[0-9]{6}"', student_page.text)
        self.assertEqual(admin_page.status_code, 303)
        self.assertEqual(admin_page.headers["location"], "/login")

    def test_admin_creates_code_that_is_only_shown_once(self) -> None:
        self._admin_login()
        csrf_token = _csrf_token_from(self.client.get("/schuelerzugange"))
        created = self.client.post(
            "/schuelerzugange/new",
            data={
                "label": "Testperson 01",
                "csrf_token": csrf_token,
            },
        )

        self.assertEqual(created.status_code, 200)
        self.assertIn("123456", created.text)
        self.assertIn("Testperson 01", created.text)
        self.assertIn("wird nur jetzt angezeigt", created.text)

        overview = self.client.get("/schuelerzugange")

        self.assertEqual(overview.status_code, 200)
        self.assertIn("Testperson 01", overview.text)
        self.assertNotIn("123456", overview.text)

        with sqlite3.connect(self.database_path) as connection:
            stored_digest = connection.execute(
                "SELECT code_digest FROM student_accounts"
            ).fetchone()[0]

        self.assertNotEqual(stored_digest, "123456")

    def test_admin_account_mutations_require_csrf(self) -> None:
        self._admin_login()

        create_response = self.client.post(
            "/schuelerzugange/new",
            data={"label": "Ohne CSRF"},
        )

        self.assertEqual(create_response.status_code, 403)
        self.assertEqual(asyncio.run(self.student_store.list_accounts()), [])

    def test_admin_can_deactivate_reactivate_and_delete_account(self) -> None:
        issued = self._create_account()
        self._admin_login()
        page = self.client.get("/schuelerzugange")
        csrf_token = _csrf_token_from(page)
        account_id = issued.account.account_id

        disabled = self.client.post(
            f"/schuelerzugange/{account_id}/status",
            data={"active": "false", "csrf_token": csrf_token},
            follow_redirects=False,
        )
        self.assertEqual(disabled.status_code, 303)
        self.assertIn("notice=disabled", disabled.headers["location"])

        enabled = self.client.post(
            f"/schuelerzugange/{account_id}/status",
            data={"active": "true", "csrf_token": csrf_token},
            follow_redirects=False,
        )
        self.assertEqual(enabled.status_code, 303)

        deleted = self.client.post(
            f"/schuelerzugange/{account_id}/delete",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
        self.assertEqual(deleted.status_code, 303)
        self.assertEqual(asyncio.run(self.student_store.list_accounts()), [])

    def test_valid_code_opens_portal_and_student_cannot_open_admin(self) -> None:
        issued = self._create_account()
        task = self._create_task()
        asyncio.run(self.task_store.set_default_feedback_task(task.task_id))

        login_response = self._student_login(issued.access_code)

        self.assertEqual(login_response.status_code, 303)
        self.assertEqual(login_response.headers["location"], "/schueler")

        portal = self.client.get("/schueler")
        protected_admin = self.client.get("/tasks", follow_redirects=False)

        self.assertEqual(portal.status_code, 200)
        self.assertIn("Dein Schreibfeedback", portal.text)
        self.assertIn("Gedichtanalyse Klasse 8", portal.text)
        self.assertIn("Analysiere das Gedicht.", portal.text)
        self.assertNotIn("Erweiterte Forschungsoptionen", portal.text)
        self.assertNotIn('name="provider"', portal.text)
        self.assertNotIn('name="openai_model"', portal.text)
        self.assertEqual(protected_admin.status_code, 303)
        self.assertEqual(protected_admin.headers["location"], "/login")

    def test_invalid_and_deactivated_codes_are_rejected(self) -> None:
        issued = self._create_account()

        invalid = self._student_login("999999")
        self.assertEqual(invalid.status_code, 401)
        self.assertIn("Zugangscode ist ungültig", invalid.text)

        asyncio.run(
            self.student_store.set_account_active(
                issued.account.account_id,
                active=False,
            )
        )
        disabled = self._student_login(issued.access_code)
        self.assertEqual(disabled.status_code, 401)

    def test_repeated_invalid_codes_are_rate_limited(self) -> None:
        for _ in range(3):
            response = self._student_login("999999")
            self.assertEqual(response.status_code, 401)

        blocked = self._student_login("999999")

        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.headers["retry-after"], "60")
        self.assertIn(
            "Zu viele fehlgeschlagene Anmeldeversuche",
            blocked.text,
        )

    def test_active_session_ends_immediately_after_deactivation(self) -> None:
        issued = self._create_account()
        self._create_task()
        self._student_login(issued.access_code)

        asyncio.run(
            self.student_store.set_account_active(
                issued.account.account_id,
                active=False,
            )
        )
        response = self.client.get("/schueler")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Sechsstelliger Zugangscode", response.text)
        self.assertNotIn("Dein Schreibfeedback", response.text)

    def test_existing_session_ends_after_code_rotation(self) -> None:
        issued = self._create_account()
        self._create_task()
        self._student_login(issued.access_code)

        replacement = asyncio.run(
            self.student_store.issue_new_code(issued.account.account_id)
        )
        response = self.client.get("/schueler")

        self.assertEqual(replacement.access_code, "654321")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Sechsstelliger Zugangscode", response.text)
        self.assertNotIn("Dein Schreibfeedback", response.text)

    def test_student_analysis_uses_only_server_configured_provider(self) -> None:
        issued = self._create_account()
        task = self._create_task()
        self._student_login(issued.access_code)
        portal = self.client.get("/schueler")
        csrf_token = _csrf_token_from(portal)
        result = RubricFeedbackResult(
            provider=main.settings.student_feedback_provider,
            model="serverseitiges-modell",
            task_id=task.task_id,
            task_title=task.title,
            rubric_title=task.rubric.title,
            criteria_feedback=(
                CriterionFeedbackResult(
                    criterion_id=task.rubric.criteria[0].criterion_id,
                    criterion_title="Einleitung",
                    criterion_text="Titel, Autor und Thema nennen.",
                    status="partially_met",
                    status_label="Teilweise erfüllt",
                    feedback="Du hast den Titel genannt.",
                    next_step="Ergänze Autor und Thema.",
                ),
            ),
            overall_feedback="Überarbeite als Nächstes deine Einleitung.",
            duration_ms=1200,
        )

        with patch.object(
            main.criterion_wise_rubric_feedback_service,
            "analyze_text",
            new=AsyncMock(return_value=result),
        ) as analyze_mock:
            response = self.client.post(
                "/schueler/analyze",
                data={
                    "student_text": "Mein anonymisierter Schülertext.",
                    "task_id": task.task_id,
                    "provider": "runpod",
                    "csrf_token": csrf_token,
                },
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-analysis-outcome"], "success")
        self.assertIn("Dein Feedback", response.text)
        self.assertIn("Du hast den Titel genannt.", response.text)
        self.assertIn("Ergänze Autor und Thema.", response.text)
        self.assertNotIn("serverseitiges-modell", response.text)
        self.assertNotIn("Anbieter", response.text)
        analyze_mock.assert_awaited_once()
        self.assertEqual(
            analyze_mock.await_args.kwargs["provider_key"],
            main.settings.student_feedback_provider,
        )
        self.assertNotIn("provider_override", analyze_mock.await_args.kwargs)

        with sqlite3.connect(self.database_path) as connection:
            stored = connection.execute(
                """
                SELECT student_text_hash, student_text
                FROM rubric_feedback_runs
                """
            ).fetchone()

        self.assertIsNotNone(stored)
        self.assertEqual(len(stored[0]), 64)
        self.assertIsNone(stored[1])

    def test_student_analysis_requires_csrf_and_valid_task(self) -> None:
        issued = self._create_account()
        self._student_login(issued.access_code)

        missing_csrf = self.client.post(
            "/schueler/analyze",
            data={"student_text": "Text", "task_id": "nicht-vorhanden"},
        )
        self.assertEqual(missing_csrf.status_code, 403)

        portal = self.client.get("/schueler")
        response = self.client.post(
            "/schueler/analyze",
            data={
                "student_text": "Text",
                "task_id": "nicht-vorhanden",
                "csrf_token": _csrf_token_from(portal),
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-analysis-outcome"], "error")
        self.assertIn("nicht verfügbar", response.text)


if __name__ == "__main__":
    unittest.main()
