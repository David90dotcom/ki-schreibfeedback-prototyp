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
from app.services.task_store import (
    FeedbackRunSelectionError,
    TaskStore,
    TaskStoreError,
)


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
                criterion_titles=[
                    "Einleitung: Grundangaben",
                    "Form: Aufbau",
                ],
                criteria=[
                    "Einleitung mit Titel, Autor und Thema",
                    "Äußere Form des Gedichts beschreiben",
                ],
            )
        )

    def test_criterion_limits_are_configurable(self) -> None:
        limited_store = TaskStore(
            Path(self.temporary_directory.name) / "limited.sqlite3",
            max_criteria=2,
            max_criterion_chars=5,
        )

        common_values = {
            "title": "Aufgabe",
            "subject": "Deutsch",
            "grade_level": "8",
            "instructions": "Bearbeite die Aufgabe.",
            "material": "",
            "rubric_title": "Feedback",
        }

        created = asyncio.run(
            limited_store.create_task(
                **common_values,
                criteria=["Eins", "Zwei"],
            )
        )

        self.assertEqual(len(created.rubric.criteria), 2)

        with self.assertRaisesRegex(ValueError, "höchstens 2 Kriterien"):
            asyncio.run(
                limited_store.create_task(
                    **common_values,
                    criteria=["Eins", "Zwei", "Drei"],
                )
            )

        with self.assertRaisesRegex(ValueError, "höchstens 5 Zeichen"):
            asyncio.run(
                limited_store.create_task(
                    **common_values,
                    criteria=["Zu lang"],
                )
            )

    def test_criterion_limits_must_be_positive_integers(self) -> None:
        database_path = (
            Path(self.temporary_directory.name) / "invalid.sqlite3"
        )

        for keyword, value in (
            ("max_criteria", 0),
            ("max_criteria", True),
            ("max_criterion_chars", -1),
        ):
            with self.subTest(keyword=keyword, value=value):
                with self.assertRaises(ValueError):
                    TaskStore(database_path, **{keyword: value})

    def test_creates_lists_and_updates_complete_rubric(self) -> None:
        task = self._create_task()

        listed = asyncio.run(self.store.list_tasks())

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].task_id, task.task_id)
        self.assertEqual(len(listed[0].rubric.criteria), 2)
        self.assertEqual(
            listed[0].rubric.criteria[0].title,
            "Einleitung: Grundangaben",
        )

        updated = asyncio.run(
            self.store.update_task(
                task.task_id,
                title="Gedichtanalyse Klasse 8",
                subject="Deutsch",
                grade_level="8",
                instructions="Analysiere das vorliegende Gedicht.",
                material="Ein kurzes Beispielgedicht.",
                rubric_title="Feedback Gedichtanalyse",
                criterion_titles=[
                    "Einleitung",
                    "Inhalt",
                    "Sprachliche Bilder",
                ],
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
        self.assertEqual(
            updated.rubric.criteria[2].title,
            "Sprachliche Bilder",
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
        self.assertEqual(
            [item.title for item in duplicate.rubric.criteria],
            [item.title for item in task.rubric.criteria],
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
                    feedback_json,
                    student_text,
                    selected_for_evaluation_at
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
        self.assertIsNone(row[3])
        self.assertIsNone(row[4])
        self.assertEqual(
            asyncio.run(
                self.store.list_feedback_runs_for_evaluation()
            ),
            [],
        )

    def test_feedback_run_is_selected_explicitly_for_evaluation(self) -> None:
        task = self._create_task()
        student_text = (
            "Anonymisierter Schülertext für die Meta-Ebene.\r\n"
            "Zweite Zeile mit \"Zitat\"."
        )
        feedback_run_id = asyncio.run(
            self.store.save_feedback_run(
                task=task,
                student_text=student_text,
                provider="openai",
                model="gpt-test",
                duration_ms=842,
                queue_duration_ms=12.5,
                execution_duration_ms=700.0,
                provider_request_id="request-1",
                feedback_payload={
                    "criteria": [
                        {
                            "criterion_id": "criterion-1",
                            "status": "met",
                            "feedback": "Treffendes Feedback.",
                            "next_step": "Weiter so.",
                        }
                    ],
                    "overall_feedback": "Zusammenfassung.",
                },
            )
        )

        selected = asyncio.run(
            self.store.select_feedback_run_for_evaluation(
                feedback_run_id=feedback_run_id,
                student_text=(
                    "  Anonymisierter Schülertext für die Meta-Ebene.\n"
                    "Zweite Zeile mit \"Zitat\".\n"
                ),
            )
        )
        selected_again = asyncio.run(
            self.store.select_feedback_run_for_evaluation(
                feedback_run_id=feedback_run_id,
                student_text=student_text,
            )
        )
        listed = asyncio.run(
            self.store.list_feedback_runs_for_evaluation()
        )

        self.assertEqual(selected.feedback_run_id, feedback_run_id)
        self.assertEqual(
            selected.student_text,
            student_text.replace("\r\n", "\n"),
        )
        self.assertEqual(selected.task_title, task.title)
        self.assertEqual(selected.rubric_title, task.rubric.title)
        self.assertEqual(selected.criterion_count, 1)
        self.assertEqual(selected.provider, "openai")
        self.assertEqual(selected.model, "gpt-test")
        self.assertEqual(selected.duration_ms, 842)
        self.assertEqual(
            selected_again.selected_for_evaluation_at,
            selected.selected_for_evaluation_at,
        )
        self.assertEqual(listed, [selected])

    def test_feedback_run_selection_rejects_mismatched_text(self) -> None:
        task = self._create_task()
        feedback_run_id = asyncio.run(
            self.store.save_feedback_run(
                task=task,
                student_text="Ursprünglicher Schülertext",
                provider="ollama",
                model="lokales-modell",
                duration_ms=100,
                feedback_payload={
                    "criteria": [],
                    "overall_feedback": "Feedback",
                },
            )
        )

        with self.assertRaises(FeedbackRunSelectionError):
            asyncio.run(
                self.store.select_feedback_run_for_evaluation(
                    feedback_run_id=feedback_run_id,
                    student_text="Ein anderer Schülertext",
                )
            )

        self.assertEqual(
            asyncio.run(
                self.store.list_feedback_runs_for_evaluation()
            ),
            [],
        )

    def test_migrates_existing_feedback_runs_additively(self) -> None:
        task = self._create_task()
        student_text = "Bestehender anonymisierter Schülertext"
        feedback_run_id = asyncio.run(
            self.store.save_feedback_run(
                task=task,
                student_text=student_text,
                provider="mistral",
                model="test-model",
                duration_ms=500,
                feedback_payload={
                    "criteria": [],
                    "overall_feedback": "Bestehendes Feedback",
                },
            )
        )

        with sqlite3.connect(self.store.database_path) as connection:
            connection.execute(
                "DROP INDEX idx_rubric_feedback_runs_evaluation"
            )
            connection.execute(
                """
                ALTER TABLE rubric_feedback_runs
                DROP COLUMN selected_for_evaluation_at
                """
            )
            connection.execute(
                """
                ALTER TABLE rubric_feedback_runs
                DROP COLUMN student_text
                """
            )

        asyncio.run(self.store.initialize())

        with sqlite3.connect(self.store.database_path) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(rubric_feedback_runs)"
                ).fetchall()
            }
            stored_ids = {
                row[0]
                for row in connection.execute(
                    "SELECT feedback_run_id FROM rubric_feedback_runs"
                ).fetchall()
            }

        self.assertIn("student_text", columns)
        self.assertIn("selected_for_evaluation_at", columns)
        self.assertIn(feedback_run_id, stored_ids)

        selected = asyncio.run(
            self.store.select_feedback_run_for_evaluation(
                feedback_run_id=feedback_run_id,
                student_text=student_text,
            )
        )
        self.assertEqual(selected.feedback_run_id, feedback_run_id)

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
                    rubric_title="Feedback",
                    criteria=[],
                )
            )

        with self.assertRaisesRegex(
            ValueError,
            "alle Feedback-Kriterien",
        ):
            asyncio.run(
                self.store.create_task(
                    title="Aufgabe",
                    subject="Deutsch",
                    grade_level="8",
                    instructions="Bearbeite die Aufgabe.",
                    material="",
                    rubric_title="Feedback",
                    criteria=["Gültig", "   "],
                )
            )

    def test_rejects_missing_or_misaligned_criterion_titles(self) -> None:
        common_values = {
            "title": "Aufgabe",
            "subject": "Deutsch",
            "grade_level": "8",
            "instructions": "Bearbeite die Aufgabe.",
            "material": "",
            "rubric_title": "Feedback",
            "criteria": ["Erstes Kriterium", "Zweites Kriterium"],
        }

        for criterion_titles in (["Nur eine Überschrift"], ["", "Form"]):
            with self.subTest(criterion_titles=criterion_titles):
                with self.assertRaisesRegex(ValueError, "Überschrift"):
                    asyncio.run(
                        self.store.create_task(
                            **common_values,
                            criterion_titles=criterion_titles,
                        )
                    )

    def test_migrates_existing_criteria_with_safe_fallback_titles(
        self,
    ) -> None:
        timestamp = "2026-08-11T12:00:00+00:00"

        with sqlite3.connect(self.store.database_path) as connection:
            connection.executescript(
                """
                CREATE TABLE feedback_tasks (
                    task_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    subject TEXT NOT NULL DEFAULT '',
                    grade_level TEXT NOT NULL DEFAULT '',
                    instructions TEXT NOT NULL,
                    material TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT
                );
                CREATE TABLE rubrics (
                    rubric_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT
                );
                CREATE TABLE rubric_criteria (
                    criterion_id TEXT PRIMARY KEY,
                    rubric_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    criterion_text TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT INTO feedback_tasks
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    "task-old",
                    "Bestehende Aufgabe",
                    "Deutsch",
                    "8",
                    "Bearbeite die Aufgabe.",
                    "",
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO rubrics
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (
                    "rubric-old",
                    "task-old",
                    "Bestehendes Feedback",
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO rubric_criteria
                VALUES (?, ?, ?, ?)
                """,
                (
                    "criterion-old",
                    "rubric-old",
                    0,
                    "Ein bestehendes Kriterium",
                ),
            )

        asyncio.run(self.store.initialize())
        task = asyncio.run(self.store.get_task("task-old"))

        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(
            task.rubric.criteria[0].title,
            "Kriterium 1",
        )

        with sqlite3.connect(self.store.database_path) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(rubric_criteria)"
                ).fetchall()
            }

        self.assertIn("criterion_title", columns)

    def test_bulk_create_validates_every_task_before_writing(self) -> None:
        drafts = (
            FeedbackTaskDraft(
                title="Gültige Aufgabe",
                subject="Deutsch",
                grade_level="8",
                instructions="Bearbeite die Aufgabe.",
                material="",
                rubric_title="Gültiges Feedback",
                criteria=("Gültiges Kriterium",),
            ),
            FeedbackTaskDraft(
                title="Ungültige Aufgabe",
                subject="Deutsch",
                grade_level="8",
                instructions="Bearbeite die Aufgabe.",
                material="",
                rubric_title="Ungültiges Feedback",
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
                rubric_title=f"Feedback {position}",
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
