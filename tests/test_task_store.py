from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.domain.rubric import FeedbackTaskDraft
from app.services.analysis_run_store import AnalysisRunStore
from app.services.runpod_job_store import RunPodJobStore
from app.services.task_store import TaskStore, TaskStoreError


class TaskStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = TaskStore(
            Path(self.temporary_directory.name) / "analysis.sqlite3"
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _create_task(self):
        return asyncio.run(
            self.store.create_task(
                title="Gedichtinterpretation Klasse 8",
                subject="Deutsch",
                grade_level="8",
                instructions="Interpretiere das vorliegende Gedicht.",
                material="Ein kurzes Beispielgedicht.",
                rubric_title="Grundanforderungen Gedichtinterpretation",
                criteria=[
                    "Einleitung mit Titel, Autor und Thema",
                    "Äußere Form des Gedichts beschreiben",
                ],
            )
        )

    def test_creates_lists_and_updates_complete_rubric(self) -> None:
        task = self._create_task()

        listed = asyncio.run(self.store.list_tasks())

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].task_id, task.task_id)
        self.assertEqual(len(listed[0].rubric.criteria), 2)

        updated = asyncio.run(
            self.store.update_task(
                task.task_id,
                title="Gedichtanalyse Klasse 8",
                subject="Deutsch",
                grade_level="8",
                instructions="Analysiere das vorliegende Gedicht.",
                material="Ein kurzes Beispielgedicht.",
                rubric_title="Bewertungsbogen Gedichtanalyse",
                criteria=[
                    "Einleitung verfassen",
                    "Inhalt zusammenfassen",
                    "Sprachliche Bilder erläutern",
                ],
            )
        )

        self.assertEqual(updated.title, "Gedichtanalyse Klasse 8")
        self.assertEqual(
            [item.position for item in updated.rubric.criteria],
            [0, 1, 2],
        )
        self.assertEqual(
            updated.rubric.criteria[2].text,
            "Sprachliche Bilder erläutern",
        )

    def test_duplicate_is_independent_copy(self) -> None:
        task = self._create_task()

        duplicate = asyncio.run(
            self.store.duplicate_task(task.task_id)
        )

        self.assertNotEqual(duplicate.task_id, task.task_id)
        self.assertNotEqual(
            duplicate.rubric.rubric_id,
            task.rubric.rubric_id,
        )
        self.assertEqual(
            [item.text for item in duplicate.rubric.criteria],
            [item.text for item in task.rubric.criteria],
        )
        self.assertTrue(duplicate.title.endswith("(Kopie)"))

    def test_unused_task_is_deleted_completely(self) -> None:
        task = self._create_task()

        result = asyncio.run(
            self.store.delete_task(task.task_id)
        )

        self.assertEqual(result.action, "deleted")
        self.assertIsNone(
            asyncio.run(
                self.store.get_task(
                    task.task_id,
                    include_archived=True,
                )
            )
        )

    def test_used_task_is_archived_and_snapshot_remains(self) -> None:
        task = self._create_task()
        asyncio.run(
            self.store.save_feedback_run(
                task=task,
                student_text="Anonymisierter Schülertext",
                provider="mistral",
                model="mistral-small-latest",
                duration_ms=1250,
                feedback_payload={
                    "criteria": [
                        {
                            "criterion_id": (
                                task.rubric.criteria[0].criterion_id
                            ),
                            "status": "partially_met",
                            "feedback": "Der Titel ist vorhanden.",
                            "next_step": "Ergänze Autor und Thema.",
                        }
                    ],
                    "overall_feedback": "Guter Anfang.",
                },
            )
        )

        result = asyncio.run(
            self.store.delete_task(task.task_id)
        )

        self.assertEqual(result.action, "archived")
        self.assertEqual(asyncio.run(self.store.list_tasks()), [])
        archived = asyncio.run(
            self.store.get_task(
                task.task_id,
                include_archived=True,
            )
        )
        self.assertIsNotNone(archived)
        self.assertIsNotNone(archived.archived_at)
        self.assertEqual(
            asyncio.run(
                self.store.count_feedback_runs(task_id=task.task_id)
            ),
            1,
        )

    def test_feedback_run_does_not_store_student_text(self) -> None:
        task = self._create_task()
        student_text = "Dieser Text darf nicht in SQLite stehen."
        asyncio.run(
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

        with sqlite3.connect(self.store.database_path) as connection:
            row = connection.execute(
                """
                SELECT
                    student_text_hash,
                    task_snapshot_json,
                    feedback_json
                FROM rubric_feedback_runs
                """
            ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(
            row[0],
            hashlib.sha256(student_text.encode("utf-8")).hexdigest(),
        )
        self.assertNotIn(student_text, row[1])
        self.assertNotIn(student_text, row[2])

    def test_shares_database_with_existing_sqlite_stores(self) -> None:
        analysis_store = AnalysisRunStore(self.store.database_path)
        runpod_store = RunPodJobStore(self.store.database_path)

        asyncio.run(analysis_store.initialize())
        task = self._create_task()
        asyncio.run(
            runpod_store.record_status(
                tracking_id="tracking-1",
                job_id="job-1",
                endpoint_key="standard",
                endpoint_id="endpoint-1",
                status="IN_QUEUE",
            )
        )

        with sqlite3.connect(self.store.database_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                    """
                ).fetchall()
            }

        self.assertIn("analysis_runs", tables)
        self.assertIn("runpod_jobs", tables)
        self.assertIn("feedback_tasks", tables)
        self.assertIn("rubrics", tables)
        self.assertEqual(
            asyncio.run(self.store.get_task(task.task_id)),
            task,
        )

    def test_rejects_empty_or_oversized_criteria(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "mindestens ein Kriterium",
        ):
            asyncio.run(
                self.store.create_task(
                    title="Aufgabe",
                    subject="Deutsch",
                    grade_level="8",
                    instructions="Bearbeite die Aufgabe.",
                    material="",
                    rubric_title="Bewertungsbogen",
                    criteria=[],
                )
            )

        with self.assertRaisesRegex(
            ValueError,
            "alle Bewertungskriterien",
        ):
            asyncio.run(
                self.store.create_task(
                    title="Aufgabe",
                    subject="Deutsch",
                    grade_level="8",
                    instructions="Bearbeite die Aufgabe.",
                    material="",
                    rubric_title="Bewertungsbogen",
                    criteria=["Gültig", "   "],
                )
            )

    def test_bulk_create_validates_every_task_before_writing(self) -> None:
        drafts = (
            FeedbackTaskDraft(
                title="Gültige Aufgabe",
                subject="Deutsch",
                grade_level="8",
                instructions="Bearbeite die Aufgabe.",
                material="",
                rubric_title="Gültiger Bewertungsbogen",
                criteria=("Gültiges Kriterium",),
            ),
            FeedbackTaskDraft(
                title="Ungültige Aufgabe",
                subject="Deutsch",
                grade_level="8",
                instructions="Bearbeite die Aufgabe.",
                material="",
                rubric_title="Ungültiger Bewertungsbogen",
                criteria=(),
            ),
        )

        with self.assertRaisesRegex(
            ValueError,
            "mindestens ein Kriterium",
        ):
            asyncio.run(self.store.create_tasks(drafts))

        self.assertEqual(asyncio.run(self.store.list_tasks()), [])

    def test_bulk_create_rolls_back_database_error_midway(self) -> None:
        drafts = tuple(
            FeedbackTaskDraft(
                title=f"Aufgabe {position}",
                subject="Deutsch",
                grade_level="8",
                instructions="Bearbeite die Aufgabe.",
                material="",
                rubric_title=f"Bewertungsbogen {position}",
                criteria=("Gültiges Kriterium",),
            )
            for position in range(2)
        )
        original_insert = TaskStore._insert_normalized_task
        insertion_count = 0

        def insert_or_fail(
            connection: sqlite3.Connection,
            normalized: dict[str, object],
            timestamp: str,
        ):
            nonlocal insertion_count
            insertion_count += 1

            if insertion_count == 2:
                raise sqlite3.IntegrityError("Erzwungener Testfehler")

            return original_insert(connection, normalized, timestamp)

        with patch.object(
            TaskStore,
            "_insert_normalized_task",
            new=staticmethod(insert_or_fail),
        ):
            with self.assertRaises(TaskStoreError):
                asyncio.run(self.store.create_tasks(drafts))

        self.assertEqual(asyncio.run(self.store.list_tasks()), [])
