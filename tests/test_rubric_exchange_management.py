from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from httpx import Response

from app import main
from app.domain.rubric import FeedbackTaskDraft
from app.services import rubric_exchange_service as exchange
from app.services.rubric_exchange_service import (
    RUBRIC_EXPORT_FORMAT,
    RUBRIC_EXPORT_VERSION,
    RubricExchangeService,
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


class RubricExchangeManagementTests(unittest.TestCase):
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
        self.csrf_token = _csrf_token_from(self.client.get("/tasks"))

    def tearDown(self) -> None:
        self.client.close()
        self.store_patcher.stop()
        self.temporary_directory.cleanup()

    def _create_task(self, suffix: str = ""):
        return asyncio.run(
            self.store.create_task(
                title=f"Gedichtinterpretation{suffix}",
                subject="Deutsch",
                grade_level="8",
                instructions="Interpretiere das Gedicht.",
                material="Ein anonymes Beispielgedicht.",
                rubric_title=f"Grundanforderungen{suffix}",
                criterion_titles=(
                    "Einleitung: Grundangaben",
                    "Sprache: Bildlichkeit",
                ),
                criteria=(
                    "Einleitung mit Titel und Autor",
                    "Sprachliche Bilder erläutern",
                ),
            )
        )

    def _upload(self, content: bytes, *, csrf_token: str | None = None):
        is_bundle = RubricExchangeService.is_bundle_content(content)

        return self.client.post(
            "/tasks/import",
            data={
                "csrf_token": (
                    self.csrf_token
                    if csrf_token is None
                    else csrf_token
                )
            },
            files={
                "import_file": (
                    (
                        "feedback.zip"
                        if is_bundle
                        else "feedback.json"
                    ),
                    content,
                    (
                        "application/zip"
                        if is_bundle
                        else "application/json"
                    ),
                )
            },
            follow_redirects=False,
        )

    def test_single_export_can_be_imported_as_independent_copy(self) -> None:
        source = self._create_task()

        export_response = self.client.get(
            f"/tasks/{source.task_id}/export"
        )

        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(
            export_response.headers["content-type"],
            "application/json",
        )
        self.assertIn(
            "attachment",
            export_response.headers["content-disposition"],
        )
        self.assertIn(
            "feedback-einzeln.json",
            export_response.headers["content-disposition"],
        )
        self.assertNotIn(source.task_id, export_response.text)

        import_response = self._upload(export_response.content)

        self.assertEqual(import_response.status_code, 303)
        self.assertEqual(
            import_response.headers["location"],
            "/tasks?notice=imported&imported_count=1",
        )
        tasks = asyncio.run(self.store.list_tasks())
        self.assertEqual(len(tasks), 2)
        self.assertIn(
            source.task_id,
            {task.task_id for task in tasks},
        )
        imported_tasks = [
            task
            for task in tasks
            if task.task_id != source.task_id
        ]
        self.assertEqual(len(imported_tasks), 1)
        imported = imported_tasks[0]
        self.assertEqual(imported.title, source.title)
        self.assertNotEqual(imported.task_id, source.task_id)
        self.assertNotEqual(
            imported.rubric.rubric_id,
            source.rubric.rubric_id,
        )
        self.assertEqual(
            [item.title for item in imported.rubric.criteria],
            [item.title for item in source.rubric.criteria],
        )

    def test_total_export_includes_archived_and_reimports_as_active(self) -> None:
        active = self._create_task(" aktiv")
        archived = self._create_task(" archiviert")
        asyncio.run(
            self.store.save_feedback_run(
                task=archived,
                student_text="Anonymisierter Text",
                provider="openai",
                model="test-model",
                duration_ms=50,
                feedback_payload={
                    "criteria": [],
                    "overall_feedback": "Test",
                },
            )
        )
        asyncio.run(self.store.delete_task(archived.task_id))

        export_response = self.client.get("/tasks/export-all")

        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(
            export_response.headers["content-type"],
            "application/zip",
        )
        self.assertIn(
            "feedback-gesamt.zip",
            export_response.headers["content-disposition"],
        )
        exported_drafts = RubricExchangeService.parse_import(
            export_response.content
        )
        self.assertEqual(len(exported_drafts), 2)
        self.assertEqual(
            {draft.title for draft in exported_drafts},
            {active.title, archived.title},
        )

        import_response = self._upload(export_response.content)

        self.assertEqual(import_response.status_code, 303)
        self.assertIn("imported_count=2", import_response.headers["location"])
        active_tasks = asyncio.run(self.store.list_tasks())
        self.assertEqual(len(active_tasks), 3)
        self.assertEqual(
            {task.title for task in active_tasks},
            {active.title, archived.title},
        )

    def test_total_export_of_201_tasks_round_trips_through_routes(
        self,
    ) -> None:
        drafts = tuple(
            FeedbackTaskDraft(
                title=f"Aufgabe {position:03d}",
                subject="Deutsch",
                grade_level="8",
                instructions="Bearbeite die Aufgabe.",
                material="",
                rubric_title=f"Feedback {position:03d}",
                criteria=("Gültiges Kriterium",),
            )
            for position in range(201)
        )
        asyncio.run(self.store.create_tasks(drafts))

        export_response = self.client.get("/tasks/export-all")

        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(
            len(
                RubricExchangeService.parse_import(
                    export_response.content
                )
            ),
            201,
        )

        import_response = self._upload(export_response.content)

        self.assertEqual(import_response.status_code, 303)
        self.assertEqual(
            import_response.headers["location"],
            "/tasks?notice=imported&imported_count=201",
        )
        self.assertEqual(
            len(asyncio.run(self.store.list_tasks())),
            402,
        )

        notice_response = self.client.get(
            import_response.headers["location"]
        )
        self.assertEqual(notice_response.status_code, 200)
        self.assertIn(
            "201 Feedback-Vorlagen wurden als neue Kopien importiert.",
            notice_response.text,
        )

    def test_failed_import_keeps_total_export_for_archived_only_data(
        self,
    ) -> None:
        archived = self._create_task(" archiviert")
        asyncio.run(
            self.store.save_feedback_run(
                task=archived,
                student_text="Anonymisierter Text",
                provider="openai",
                model="test-model",
                duration_ms=50,
                feedback_payload={
                    "criteria": [],
                    "overall_feedback": "Test",
                },
            )
        )
        asyncio.run(self.store.delete_task(archived.task_id))

        response = self._upload(b"{}")

        self.assertEqual(response.status_code, 422)
        self.assertIn("Gesamtexport als ZIP", response.text)
        self.assertIn('href="/tasks/export-all"', response.text)
        self.assertEqual(asyncio.run(self.store.list_tasks()), [])
        self.assertEqual(
            len(
                asyncio.run(
                    self.store.list_tasks(include_archived=True)
                )
            ),
            1,
        )

    def test_invalid_collection_is_rejected_without_partial_import(self) -> None:
        document = {
            "format": RUBRIC_EXPORT_FORMAT,
            "format_version": RUBRIC_EXPORT_VERSION,
            "export_type": "collection",
            "tasks": [
                {
                    "title": "Gültige Aufgabe",
                    "subject": "Deutsch",
                    "grade_level": "8",
                    "instructions": "Bearbeite die Aufgabe.",
                    "material": "",
                    "rubric": {
                        "title": "Gültiges Feedback",
                        "criteria": [{"text": "Gültiges Kriterium"}],
                    },
                },
                {
                    "title": "Ungültige Aufgabe",
                    "subject": "Deutsch",
                    "grade_level": "8",
                    "instructions": "Bearbeite die Aufgabe.",
                    "material": "",
                    "rubric": {
                        "title": "Ungültiges Feedback",
                        "criteria": [],
                    },
                },
            ],
        }

        response = self._upload(
            json.dumps(document).encode("utf-8")
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("mindestens ein Kriterium", response.text)
        self.assertEqual(asyncio.run(self.store.list_tasks()), [])

    def test_invalid_later_bundle_part_is_rejected_before_any_write(
        self,
    ) -> None:
        source_tasks = asyncio.run(
            self.store.create_tasks(
                tuple(
                    FeedbackTaskDraft(
                        title=f"Aufgabe {position:03d}",
                        subject="Deutsch",
                        grade_level="8",
                        instructions="Bearbeite die Aufgabe.",
                        material="",
                        rubric_title=(
                            f"Feedback {position:03d}"
                        ),
                        criteria=("Gültiges Kriterium",),
                    )
                    for position in range(201)
                )
            )
        )
        bundle = RubricExchangeService.export_collection_bundle(
            source_tasks
        )

        with zipfile.ZipFile(io.BytesIO(bundle), mode="r") as archive:
            members = {
                info.filename: archive.read(info)
                for info in archive.infolist()
            }

        manifest = json.loads(members["manifest.json"])
        later_part = manifest["parts"][1]
        later_document = json.loads(members[later_part["name"]])
        later_document["tasks"][0]["rubric"]["criteria"] = []
        changed_part = (
            json.dumps(
                later_document,
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        members[later_part["name"]] = changed_part
        later_part["size_bytes"] = len(changed_part)
        later_part["sha256"] = hashlib.sha256(
            changed_part
        ).hexdigest()
        members["manifest.json"] = (
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")

        changed_buffer = io.BytesIO()

        with zipfile.ZipFile(
            changed_buffer,
            mode="w",
            compression=zipfile.ZIP_STORED,
        ) as archive:
            for name, content in members.items():
                archive.writestr(name, content)

        empty_store = TaskStore(
            Path(self.temporary_directory.name) / "empty.sqlite3"
        )

        with patch.object(main, "task_store", empty_store):
            response = self._upload(changed_buffer.getvalue())

        self.assertEqual(response.status_code, 422)
        self.assertIn("mindestens ein Kriterium", response.text)
        self.assertEqual(asyncio.run(empty_store.list_tasks()), [])

    def test_import_requires_valid_csrf_token(self) -> None:
        response = self._upload(b"{}", csrf_token="ungueltig")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(asyncio.run(self.store.list_tasks()), [])

    def test_bundle_upload_is_read_only_to_the_configured_limit(
        self,
    ) -> None:
        content = b"PK\x03\x04" + b"x" * 20

        with (
            patch.object(main, "MAX_RUBRIC_BUNDLE_BYTES", 8),
            patch.object(exchange, "MAX_RUBRIC_BUNDLE_BYTES", 8),
        ):
            response = self._upload(content)

        self.assertEqual(response.status_code, 422)
        self.assertIn("höchstens 64 MiB", response.text)
        self.assertEqual(asyncio.run(self.store.list_tasks()), [])

    def test_exchange_routes_require_login_and_controls_are_visible(
        self,
    ) -> None:
        task = self._create_task()
        management_page = self.client.get("/tasks")

        self.assertIn("Gesamtexport", management_page.text)
        self.assertIn("Feedback importieren", management_page.text)
        self.assertNotIn(
            "bewertungsbogen",
            management_page.text.lower(),
        )
        self.assertIn(
            f"/tasks/{task.task_id}/export",
            management_page.text,
        )

        anonymous_client = TestClient(main.app)

        try:
            for path in (
                "/tasks/export-all",
                f"/tasks/{task.task_id}/export",
            ):
                with self.subTest(path=path):
                    response = anonymous_client.get(
                        path,
                        follow_redirects=False,
                    )
                    self.assertEqual(response.status_code, 303)
                    self.assertEqual(response.headers["location"], "/login")
        finally:
            anonymous_client.close()
