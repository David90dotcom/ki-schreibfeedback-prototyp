from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.domain.feedback_evaluation import (
    MANUAL_META_EVALUATION_RUBRIC,
)
from app.domain.rubric import FeedbackTaskDraft
from app.services.analysis_run_store import AnalysisRunStore
from app.services.runpod_job_store import RunPodJobStore
from app.services.task_store import (
    FeedbackEvaluationDeleteConflictError,
    FeedbackEvaluationValidationError,
    FeedbackRunNotFoundError,
    FeedbackRunRefreshError,
    FeedbackRunSelectionError,
    STANDARD_FEEDBACK_RUBRIC_ID,
    STANDARD_FEEDBACK_TASK_ID,
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

    def _create_selected_feedback_run(
        self,
        *,
        original_text: str = "",
    ) -> str:
        task = self._create_task()
        student_text = "Anonymisierter Text für die Qualitätsbewertung."
        feedback_run_id = asyncio.run(
            self.store.save_feedback_run(
                task=task,
                student_text=student_text,
                original_text=original_text,
                provider="openai",
                model="erzeuger-modell",
                reasoning_effort="max",
                duration_ms=640,
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
                    "overall_feedback": "Ein sinnvoller Anfang.",
                },
            )
        )
        asyncio.run(
            self.store.select_feedback_run_for_evaluation(
                feedback_run_id=feedback_run_id,
                student_text=student_text,
            )
        )
        return feedback_run_id

    def test_standard_feedback_context_is_stable_and_hidden(self) -> None:
        first = asyncio.run(
            self.store.get_or_create_standard_feedback_task()
        )
        second = asyncio.run(
            self.store.get_or_create_standard_feedback_task()
        )

        self.assertEqual(first.task_id, STANDARD_FEEDBACK_TASK_ID)
        self.assertEqual(first.rubric.rubric_id, STANDARD_FEEDBACK_RUBRIC_ID)
        self.assertEqual(second, first)
        self.assertEqual(first.rubric.criteria, ())
        self.assertIn("keine konkrete Aufgabenstellung", first.instructions)
        self.assertEqual(asyncio.run(self.store.list_tasks()), [])
        self.assertEqual(
            asyncio.run(self.store.list_tasks(include_archived=True)),
            [],
        )
        self.assertIsNone(
            asyncio.run(
                self.store.get_task(
                    STANDARD_FEEDBACK_TASK_ID,
                    include_archived=True,
                )
            )
        )

    def test_default_feedback_task_can_be_selected_and_changed(self) -> None:
        self.assertIsNone(
            asyncio.run(self.store.get_default_feedback_task_id())
        )
        first = self._create_task()
        second = asyncio.run(self.store.duplicate_task(first.task_id))

        selected = asyncio.run(
            self.store.set_default_feedback_task(first.task_id)
        )

        self.assertEqual(selected.task_id, first.task_id)
        self.assertEqual(
            asyncio.run(self.store.get_default_feedback_task_id()),
            first.task_id,
        )

        asyncio.run(
            self.store.set_default_feedback_task(second.task_id)
        )

        self.assertEqual(
            asyncio.run(self.store.get_default_feedback_task_id()),
            second.task_id,
        )

        with sqlite3.connect(self.store.database_path) as connection:
            setting_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM application_settings
                WHERE setting_key = 'default_feedback_task_id'
                """
            ).fetchone()[0]

        self.assertEqual(setting_count, 1)

    def test_student_feedback_configuration_uses_fallback_and_persists(
        self,
    ) -> None:
        fallback = asyncio.run(
            self.store.get_student_feedback_configuration(
                fallback_provider="mistral",
                fallback_model="mistral-small-latest",
            )
        )

        self.assertEqual(fallback.provider, "mistral")
        self.assertEqual(fallback.model, "mistral-small-latest")

        stored = asyncio.run(
            self.store.set_student_feedback_configuration(
                provider="openai",
                model="gpt-5.6-luna",
            )
        )
        reopened_store = TaskStore(self.store.database_path)
        reloaded = asyncio.run(
            reopened_store.get_student_feedback_configuration(
                fallback_provider="mistral",
                fallback_model="mistral-small-latest",
            )
        )

        self.assertEqual(stored.provider, "openai")
        self.assertEqual(stored.model, "gpt-5.6-luna")
        self.assertEqual(reloaded, stored)

        with sqlite3.connect(self.store.database_path) as connection:
            rows = connection.execute(
                """
                SELECT setting_key, setting_value
                FROM application_settings
                WHERE setting_key LIKE 'student_feedback_%'
                ORDER BY setting_key
                """
            ).fetchall()

        self.assertEqual(
            rows,
            [
                ("student_feedback_model", "gpt-5.6-luna"),
                ("student_feedback_provider", "openai"),
            ],
        )

    def test_student_feedback_configuration_rejects_empty_values(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "dürfen nicht leer"):
            asyncio.run(
                self.store.set_student_feedback_configuration(
                    provider="openai",
                    model=" ",
                )
            )

    def test_duplicate_does_not_replace_default_feedback_task(self) -> None:
        source = self._create_task()
        asyncio.run(
            self.store.set_default_feedback_task(source.task_id)
        )

        duplicate = asyncio.run(
            self.store.duplicate_task(source.task_id)
        )

        self.assertNotEqual(duplicate.task_id, source.task_id)
        self.assertEqual(
            asyncio.run(self.store.get_default_feedback_task_id()),
            source.task_id,
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
        asyncio.run(
            self.store.set_default_feedback_task(task.task_id)
        )

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
        self.assertIsNone(
            asyncio.run(self.store.get_default_feedback_task_id())
        )

    def test_used_task_is_archived_and_snapshot_remains(self) -> None:
        task = self._create_task()
        asyncio.run(
            self.store.set_default_feedback_task(task.task_id)
        )
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
        self.assertIsNone(
            asyncio.run(self.store.get_default_feedback_task_id())
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

    def test_unselected_feedback_run_can_refresh_one_criterion(
        self,
    ) -> None:
        task = self._create_task()
        student_text = "Anonymisierter Schülertext."
        original_text = "Laufbezogener Originaltext."
        first_criterion = task.rubric.criteria[0]
        second_criterion = task.rubric.criteria[1]
        feedback_run_id = asyncio.run(
            self.store.save_feedback_run(
                task=task,
                student_text=student_text,
                original_text=original_text,
                provider="openai",
                model="gpt-test",
                reasoning_effort="high",
                duration_ms=500,
                queue_duration_ms=10.0,
                execution_duration_ms=400.0,
                provider_request_id="request-initial",
                feedback_payload={
                    "criteria": [
                        {
                            "criterion_id": first_criterion.criterion_id,
                            "status": "partially_met",
                            "feedback": "Altes erstes Feedback.",
                            "next_step": "Alter erster Schritt.",
                        },
                        {
                            "criterion_id": second_criterion.criterion_id,
                            "status": "met",
                            "feedback": "Zweites Feedback bleibt.",
                            "next_step": "",
                        },
                    ],
                    "overall_feedback": "Alte Zusammenfassung.",
                    "generation_context": {
                        "mode": "rubric_feedback",
                    },
                },
            )
        )

        refreshable = asyncio.run(
            self.store.get_feedback_run_for_refresh(
                feedback_run_id=feedback_run_id,
                student_text="  Anonymisierter Schülertext.\r\n",
            )
        )
        refresh_count = asyncio.run(
            self.store.update_feedback_run_criterion(
                feedback_run_id=feedback_run_id,
                student_text=student_text,
                provider="openai",
                model="gpt-test",
                reasoning_effort="high",
                criterion_payload={
                    "criterion_id": first_criterion.criterion_id,
                    "criterion_title": first_criterion.title,
                    "criterion_text": first_criterion.text,
                    "status": "mostly_met",
                    "feedback": "Neues erstes Feedback.",
                    "next_step": "Neuer erster Schritt.",
                    "evidence_verified": True,
                },
                overall_feedback="Neutral aktualisiert.",
                prompt_version="criterion-refresh-v1",
                evidence_validation_version="evidence-v1",
                duration_ms=200,
                queue_duration_ms=5.0,
                execution_duration_ms=170.0,
                provider_request_id="request-refresh",
            )
        )

        self.assertEqual(refreshable.task.snapshot(), task.snapshot())
        self.assertEqual(refreshable.original_text, original_text)
        self.assertEqual(refreshable.provider, "openai")
        self.assertEqual(refreshable.model, "gpt-test")
        self.assertEqual(refresh_count, 1)

        selected = asyncio.run(
            self.store.select_feedback_run_for_evaluation(
                feedback_run_id=feedback_run_id,
                student_text=student_text,
            )
        )
        self.assertEqual(selected.duration_ms, 700)
        self.assertEqual(selected.queue_duration_ms, 15.0)
        self.assertEqual(selected.execution_duration_ms, 570.0)
        self.assertEqual(
            selected.provider_request_id,
            "request-refresh",
        )
        self.assertEqual(
            selected.feedback_payload["criteria"][0]["feedback"],
            "Neues erstes Feedback.",
        )
        self.assertEqual(
            selected.feedback_payload["criteria"][1]["feedback"],
            "Zweites Feedback bleibt.",
        )
        self.assertEqual(
            selected.feedback_payload["overall_feedback"],
            "Neutral aktualisiert.",
        )
        refresh_context = selected.generation_context[
            "criterion_refreshes"
        ]
        self.assertEqual(refresh_context["count"], 1)
        self.assertEqual(
            refresh_context["items"][0]["criterion_id"],
            first_criterion.criterion_id,
        )
        self.assertEqual(
            refresh_context["items"][0]["provider_request_id"],
            "request-refresh",
        )

    def test_feedback_run_refresh_rejects_changed_or_selected_run(
        self,
    ) -> None:
        task = self._create_task()
        student_text = "Unveränderter Schülertext."
        feedback_run_id = asyncio.run(
            self.store.save_feedback_run(
                task=task,
                student_text=student_text,
                provider="ollama",
                model="lokales-modell",
                duration_ms=100,
                feedback_payload={
                    "criteria": [],
                    "overall_feedback": "Test.",
                },
            )
        )

        with self.assertRaises(FeedbackRunRefreshError):
            asyncio.run(
                self.store.get_feedback_run_for_refresh(
                    feedback_run_id=feedback_run_id,
                    student_text="Veränderter Schülertext.",
                )
            )

        asyncio.run(
            self.store.select_feedback_run_for_evaluation(
                feedback_run_id=feedback_run_id,
                student_text=student_text,
            )
        )

        with self.assertRaises(FeedbackRunRefreshError):
            asyncio.run(
                self.store.get_feedback_run_for_refresh(
                    feedback_run_id=feedback_run_id,
                    student_text=student_text,
                )
            )

    def test_feedback_run_is_selected_explicitly_for_evaluation(self) -> None:
        task = self._create_task()
        student_text = (
            "Anonymisierter Schülertext für die Meta-Ebene.\r\n"
            "Zweite Zeile mit \"Zitat\"."
        )
        original_text = (
            "Ein Originaltext, der unabhängig von der Aufgabe nur zu "
            "diesem Lauf gehört."
        )
        feedback_run_id = asyncio.run(
            self.store.save_feedback_run(
                task=task,
                student_text=student_text,
                original_text=original_text,
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
        self.assertEqual(selected.original_text, original_text)
        self.assertEqual(
            selected.task_snapshot["material"],
            task.material,
        )
        self.assertEqual(
            selected_again.selected_for_evaluation_at,
            selected.selected_for_evaluation_at,
        )
        self.assertEqual(listed, [selected])

    def test_manual_feedback_evaluations_are_versioned_and_separate(
        self,
    ) -> None:
        feedback_run_id = self._create_selected_feedback_run()
        criterion_keys = [
            criterion.key
            for criterion in MANUAL_META_EVALUATION_RUBRIC.criteria
        ]
        first_scores = dict(zip(criterion_keys, (3, 2, 2, 1)))
        second_scores = dict(zip(criterion_keys, (2, 3, 3, 2)))
        first_justifications = {
            key: f"Erste begründete Einschätzung zu {key}."
            for key in criterion_keys
        }
        second_justifications = {
            key: f"  Zweite begründete Einschätzung zu {key}.  "
            for key in criterion_keys
        }

        first = asyncio.run(
            self.store.create_manual_feedback_evaluation(
                feedback_run_id=feedback_run_id,
                scores=first_scores,
                justifications=first_justifications,
            )
        )
        second = asyncio.run(
            self.store.create_manual_feedback_evaluation(
                feedback_run_id=feedback_run_id,
                scores=second_scores,
                justifications=second_justifications,
                evaluation_name="  Zweite Prüfung  ",
            )
        )
        selected_run = asyncio.run(
            self.store.list_feedback_runs_for_evaluation()
        )[0]

        self.assertNotEqual(first.evaluation_id, second.evaluation_id)
        self.assertEqual(first.evaluation_type, "manual")
        self.assertEqual(
            first.rubric_version,
            MANUAL_META_EVALUATION_RUBRIC.version,
        )
        self.assertEqual(len(first.ratings), 4)
        self.assertEqual(first.ratings[0].score, 3)
        self.assertEqual(first.ratings[0].rating_label, "erfüllt")
        self.assertEqual(
            first.ratings[0].criterion_title,
            "Fachliche Korrektheit",
        )
        self.assertEqual(
            second.ratings[0].justification,
            second_justifications[criterion_keys[0]].strip(),
        )
        self.assertIsNone(first.evaluator_provider)
        self.assertIsNone(first.evaluator_model)
        self.assertIsNone(first.duration_ms)
        self.assertIsNone(first.evaluation_name)
        self.assertEqual(second.evaluation_name, "Zweite Prüfung")
        self.assertEqual(first.average_score, 2.0)
        self.assertEqual(second.average_score, 2.5)
        self.assertEqual(selected_run.manual_evaluation_count, 2)
        self.assertEqual(selected_run.aggregate_score, 2.25)
        self.assertEqual(selected_run.reasoning_effort, "max")
        self.assertEqual(
            {item.evaluation_id for item in selected_run.evaluations},
            {first.evaluation_id, second.evaluation_id},
        )

        with sqlite3.connect(self.store.database_path) as connection:
            evaluation_count = connection.execute(
                "SELECT COUNT(*) FROM feedback_evaluations"
            ).fetchone()[0]
            rating_count = connection.execute(
                "SELECT COUNT(*) FROM feedback_evaluation_ratings"
            ).fetchone()[0]

        self.assertEqual(evaluation_count, 2)
        self.assertEqual(rating_count, 8)

    def test_manual_feedback_evaluation_rejects_incomplete_input(
        self,
    ) -> None:
        feedback_run_id = self._create_selected_feedback_run()
        criterion_keys = [
            criterion.key
            for criterion in MANUAL_META_EVALUATION_RUBRIC.criteria
        ]

        with self.assertRaises(FeedbackEvaluationValidationError):
            asyncio.run(
                self.store.create_manual_feedback_evaluation(
                    feedback_run_id=feedback_run_id,
                    scores={criterion_keys[0]: 3},
                    justifications={criterion_keys[0]: "Begründung"},
                )
            )

        with self.assertRaises(FeedbackEvaluationValidationError):
            asyncio.run(
                self.store.create_manual_feedback_evaluation(
                    feedback_run_id=feedback_run_id,
                    scores={key: 2 for key in criterion_keys},
                    justifications={
                        key: ("   " if key == criterion_keys[2] else "Beleg")
                        for key in criterion_keys
                    },
                )
            )

        selected_run = asyncio.run(
            self.store.list_feedback_runs_for_evaluation()
        )[0]
        self.assertEqual(selected_run.evaluations, ())

    def test_automatic_evaluation_is_stored_with_model_metadata(
        self,
    ) -> None:
        feedback_run_id = self._create_selected_feedback_run()
        criterion_keys = [
            criterion.key
            for criterion in MANUAL_META_EVALUATION_RUBRIC.criteria
        ]
        scores = dict(zip(criterion_keys, (3, 2, 2, 1)))
        justifications = {
            key: f"Detaillierte automatische Begründung für {key}."
            for key in criterion_keys
        }

        automatic = asyncio.run(
            self.store.create_automatic_feedback_evaluation(
                feedback_run_id=feedback_run_id,
                scores=scores,
                justifications=justifications,
                evaluator_provider="openai",
                evaluator_model="gpt-5.6-sol",
                evaluator_prompt_version="meta-evaluator-v1",
                duration_ms=1234,
                provider_request_id="resp-auto-1",
                evaluation_name="  Sol max – automatischer Lauf  ",
            )
        )
        selected_run = asyncio.run(
            self.store.get_feedback_run_for_evaluation(feedback_run_id)
        )

        self.assertEqual(automatic.evaluation_type, "automatic")
        self.assertEqual(automatic.type_label, "Automatische Vorbewertung")
        self.assertEqual(automatic.evaluator_provider, "openai")
        self.assertEqual(automatic.evaluator_model, "gpt-5.6-sol")
        self.assertEqual(
            automatic.evaluator_prompt_version,
            "meta-evaluator-v1",
        )
        self.assertEqual(automatic.duration_ms, 1234)
        self.assertEqual(automatic.provider_request_id, "resp-auto-1")
        self.assertEqual(
            automatic.evaluation_name,
            "Sol max – automatischer Lauf",
        )
        self.assertIsNone(automatic.source_evaluation_id)
        self.assertEqual(selected_run.automatic_evaluation_count, 1)
        self.assertEqual(selected_run.manual_evaluation_count, 0)
        self.assertEqual(
            selected_run.latest_automatic_evaluation,
            automatic,
        )

        manual = asyncio.run(
            self.store.create_manual_feedback_evaluation(
                feedback_run_id=feedback_run_id,
                scores={key: 2 for key in criterion_keys},
                justifications={
                    key: f"Manuell geprüfte Begründung für {key}."
                    for key in criterion_keys
                },
                source_evaluation_id=automatic.evaluation_id,
                evaluation_name="Manuelle Kontrollprüfung",
            )
        )
        reloaded = asyncio.run(
            self.store.get_feedback_run_for_evaluation(feedback_run_id)
        )

        self.assertNotEqual(manual.evaluation_id, automatic.evaluation_id)
        self.assertEqual(
            manual.source_evaluation_id,
            automatic.evaluation_id,
        )
        self.assertEqual(reloaded.automatic_evaluation_count, 1)
        self.assertEqual(reloaded.manual_evaluation_count, 1)
        self.assertEqual(len(reloaded.evaluations), 2)

        with self.assertRaises(
            FeedbackEvaluationDeleteConflictError
        ):
            asyncio.run(
                self.store.delete_feedback_evaluation(
                    feedback_run_id=feedback_run_id,
                    evaluation_id=automatic.evaluation_id,
                )
            )

        asyncio.run(
            self.store.delete_feedback_evaluation(
                feedback_run_id=feedback_run_id,
                evaluation_id=manual.evaluation_id,
            )
        )
        asyncio.run(
            self.store.delete_feedback_evaluation(
                feedback_run_id=feedback_run_id,
                evaluation_id=automatic.evaluation_id,
            )
        )
        after_deletion = asyncio.run(
            self.store.get_feedback_run_for_evaluation(feedback_run_id)
        )
        self.assertEqual(after_deletion.evaluations, ())

        with sqlite3.connect(self.store.database_path) as connection:
            rating_count = connection.execute(
                "SELECT COUNT(*) FROM feedback_evaluation_ratings"
            ).fetchone()[0]

        self.assertEqual(rating_count, 0)

    def test_removing_feedback_run_from_evaluation_cleans_meta_data(
        self,
    ) -> None:
        original_text = "Originaltext bleibt Teil des technischen Laufs."
        feedback_run_id = self._create_selected_feedback_run(
            original_text=original_text
        )
        criterion_keys = [
            criterion.key
            for criterion in MANUAL_META_EVALUATION_RUBRIC.criteria
        ]
        automatic = asyncio.run(
            self.store.create_automatic_feedback_evaluation(
                feedback_run_id=feedback_run_id,
                scores={key: 2 for key in criterion_keys},
                justifications={
                    key: f"Automatische Begründung für {key}."
                    for key in criterion_keys
                },
                evaluator_provider="openai",
                evaluator_model="gpt-5.6-sol",
                evaluator_prompt_version="meta-evaluator-v1",
                duration_ms=500,
            )
        )
        asyncio.run(
            self.store.create_manual_feedback_evaluation(
                feedback_run_id=feedback_run_id,
                scores={key: 3 for key in criterion_keys},
                justifications={
                    key: f"Manuelle Begründung für {key}."
                    for key in criterion_keys
                },
                source_evaluation_id=automatic.evaluation_id,
            )
        )

        asyncio.run(
            self.store.remove_feedback_run_from_evaluation(
                feedback_run_id=feedback_run_id,
            )
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
        with self.assertRaises(FeedbackRunNotFoundError):
            asyncio.run(
                self.store.get_feedback_run_for_evaluation(
                    feedback_run_id
                )
            )

        with sqlite3.connect(self.store.database_path) as connection:
            connection.row_factory = sqlite3.Row
            stored_run = connection.execute(
                """
                SELECT student_text_hash,
                       student_text,
                       selected_for_evaluation_at,
                       original_text,
                       feedback_json
                FROM rubric_feedback_runs
                WHERE feedback_run_id = ?
                """,
                (feedback_run_id,),
            ).fetchone()
            evaluation_count = connection.execute(
                "SELECT COUNT(*) FROM feedback_evaluations"
            ).fetchone()[0]
            rating_count = connection.execute(
                "SELECT COUNT(*) FROM feedback_evaluation_ratings"
            ).fetchone()[0]

        self.assertIsNotNone(stored_run)
        self.assertEqual(
            stored_run["student_text_hash"],
            hashlib.sha256(
                "Anonymisierter Text für die Qualitätsbewertung.".encode(
                    "utf-8"
                )
            ).hexdigest(),
        )
        self.assertIsNone(stored_run["student_text"])
        self.assertIsNone(stored_run["selected_for_evaluation_at"])
        self.assertEqual(stored_run["original_text"], original_text)
        self.assertIn("Ein sinnvoller Anfang.", stored_run["feedback_json"])
        self.assertEqual(evaluation_count, 0)
        self.assertEqual(rating_count, 0)

        selected_again = asyncio.run(
            self.store.select_feedback_run_for_evaluation(
                feedback_run_id=feedback_run_id,
                student_text=(
                    "Anonymisierter Text für die Qualitätsbewertung."
                ),
            )
        )
        self.assertEqual(selected_again.evaluations, ())

    def test_manual_feedback_evaluation_requires_selected_run(self) -> None:
        task = self._create_task()
        feedback_run_id = asyncio.run(
            self.store.save_feedback_run(
                task=task,
                student_text="Anonymisierter Text",
                provider="ollama",
                model="lokales-modell",
                duration_ms=100,
                feedback_payload={
                    "criteria": [],
                    "overall_feedback": "Feedback",
                },
            )
        )
        criterion_keys = [
            criterion.key
            for criterion in MANUAL_META_EVALUATION_RUBRIC.criteria
        ]

        with self.assertRaises(FeedbackRunSelectionError):
            asyncio.run(
                self.store.create_manual_feedback_evaluation(
                    feedback_run_id=feedback_run_id,
                    scores={key: 2 for key in criterion_keys},
                    justifications={key: "Begründung" for key in criterion_keys},
                )
            )

    def test_migrates_evaluator_metadata_columns_additively(self) -> None:
        self._create_selected_feedback_run()

        with sqlite3.connect(self.store.database_path) as connection:
            connection.execute(
                """
                ALTER TABLE feedback_evaluations
                DROP COLUMN evaluator_prompt_version
                """
            )
            connection.execute(
                """
                ALTER TABLE feedback_evaluations
                DROP COLUMN source_evaluation_id
                """
            )
            connection.execute(
                """
                ALTER TABLE feedback_evaluations
                DROP COLUMN evaluation_name
                """
            )
            connection.execute(
                """
                ALTER TABLE rubric_feedback_runs
                DROP COLUMN reasoning_effort
                """
            )

        asyncio.run(self.store.initialize())

        with sqlite3.connect(self.store.database_path) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(feedback_evaluations)"
                ).fetchall()
            }

        self.assertIn("evaluator_prompt_version", columns)
        self.assertIn("source_evaluation_id", columns)
        self.assertIn("evaluation_name", columns)

        with sqlite3.connect(self.store.database_path) as connection:
            run_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(rubric_feedback_runs)"
                ).fetchall()
            }

        self.assertIn("reasoning_effort", run_columns)

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
                "DROP TABLE feedback_evaluation_ratings"
            )
            connection.execute("DROP TABLE feedback_evaluations")
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

        self.assertIn("student_text", columns)
        self.assertIn("selected_for_evaluation_at", columns)
        self.assertIn("original_text", columns)
        self.assertIn(feedback_run_id, stored_ids)
        self.assertIn("feedback_evaluations", tables)
        self.assertIn("feedback_evaluation_ratings", tables)

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
