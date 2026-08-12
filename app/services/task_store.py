from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from app.domain.feedback_evaluation import (
    AUTOMATIC_EVALUATION_TYPE,
    MANUAL_EVALUATION_TYPE,
    MANUAL_META_EVALUATION_RUBRIC,
    MAX_EVALUATION_NAME_CHARS,
    FeedbackEvaluationRating,
    StoredFeedbackEvaluation,
    StoredFeedbackRun,
)
from app.domain.rubric import (
    FeedbackTask,
    FeedbackTaskDraft,
    Rubric,
    RubricCriterion,
    TaskDeleteResult,
)


MAX_CRITERIA = 100
MAX_CRITERION_CHARS = 10000
MAX_CRITERION_TITLE_CHARS = 120
MAX_MATERIAL_CHARS = 30000
MAX_TASK_INSTRUCTIONS_CHARS = 12000


def _normalize_student_text(value: str) -> str:
    """Vereinheitlicht Browser-Zeilenumbrüche für Hash und Speicherung."""

    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _normalize_evaluation_name(value: str | None) -> str | None:
    """Normalisiert einen optionalen, nutzerseitigen Bewertungsnamen."""

    if value is None:
        return None

    if not isinstance(value, str):
        raise ValueError("Der Bewertungsname muss Text sein.")

    normalized = value.strip()

    if not normalized:
        return None

    if len(normalized) > MAX_EVALUATION_NAME_CHARS:
        raise ValueError(
            "Der Bewertungsname darf höchstens "
            f"{MAX_EVALUATION_NAME_CHARS} Zeichen enthalten."
        )

    return normalized


class TaskStoreError(RuntimeError):
    """Fehler beim Speichern oder Lesen von Aufgaben."""


class TaskNotFoundError(TaskStoreError):
    """Die angeforderte Aufgabe ist nicht vorhanden oder archiviert."""


class FeedbackRunNotFoundError(TaskStoreError):
    """Der angeforderte Feedbacklauf ist nicht vorhanden."""


class FeedbackRunSelectionError(TaskStoreError):
    """Der Feedbacklauf kann nicht zur Bewertung ausgewählt werden."""


class FeedbackEvaluationValidationError(TaskStoreError):
    """Die übermittelte Meta-Bewertung ist nicht vollständig gültig."""


class FeedbackEvaluationNotFoundError(TaskStoreError):
    """Die angeforderte Meta-Bewertung wurde nicht gefunden."""


class FeedbackEvaluationDeleteConflictError(TaskStoreError):
    """Eine verknüpfte Bewertung darf nicht unbemerkt gelöscht werden."""


class TaskStore:
    """Verwaltet Aufgaben und Feedback-Vorlagen in SQLite."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        max_criteria: int = MAX_CRITERIA,
        max_criterion_chars: int = MAX_CRITERION_CHARS,
    ) -> None:
        if type(max_criteria) is not int or max_criteria <= 0:
            raise ValueError(
                "Die maximale Kriterienanzahl muss eine positive "
                "Ganzzahl sein."
            )
        if (
            type(max_criterion_chars) is not int
            or max_criterion_chars <= 0
        ):
            raise ValueError(
                "Die maximale Länge eines Feedback-Kriteriums muss "
                "eine positive Ganzzahl sein."
            )

        self.database_path = Path(database_path)
        self.max_criteria = max_criteria
        self.max_criterion_chars = max_criterion_chars
        self._write_lock = Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def list_tasks(
        self,
        *,
        include_archived: bool = False,
    ) -> list[FeedbackTask]:
        return await asyncio.to_thread(
            self._list_tasks_sync,
            include_archived,
        )

    async def get_task(
        self,
        task_id: str,
        *,
        include_archived: bool = False,
    ) -> FeedbackTask | None:
        return await asyncio.to_thread(
            self._get_task_sync,
            task_id,
            include_archived,
        )

    async def create_task(
        self,
        *,
        title: str,
        subject: str,
        grade_level: str,
        instructions: str,
        material: str,
        rubric_title: str,
        criteria: list[str] | tuple[str, ...],
        criterion_titles: list[str] | tuple[str, ...] | None = None,
    ) -> FeedbackTask:
        normalized = self._validate_input(
            title=title,
            subject=subject,
            grade_level=grade_level,
            instructions=instructions,
            material=material,
            rubric_title=rubric_title,
            criteria=criteria,
            criterion_titles=criterion_titles,
        )

        return await asyncio.to_thread(
            self._create_task_sync,
            normalized,
        )

    async def create_tasks(
        self,
        drafts: Sequence[FeedbackTaskDraft],
    ) -> list[FeedbackTask]:
        """Legt mehrere vollständig validierte Aufgaben atomar an."""

        if not drafts:
            raise ValueError(
                "Die Importdatei enthält keine Feedback-Vorlagen."
            )

        normalized_tasks = tuple(
            self._validate_input(
                title=draft.title,
                subject=draft.subject,
                grade_level=draft.grade_level,
                instructions=draft.instructions,
                material=draft.material,
                rubric_title=draft.rubric_title,
                criteria=draft.criteria,
                criterion_titles=(
                    draft.criterion_titles or None
                ),
            )
            for draft in drafts
        )

        return await asyncio.to_thread(
            self._create_tasks_sync,
            normalized_tasks,
        )

    async def update_task(
        self,
        task_id: str,
        *,
        title: str,
        subject: str,
        grade_level: str,
        instructions: str,
        material: str,
        rubric_title: str,
        criteria: list[str] | tuple[str, ...],
        criterion_titles: list[str] | tuple[str, ...] | None = None,
    ) -> FeedbackTask:
        normalized = self._validate_input(
            title=title,
            subject=subject,
            grade_level=grade_level,
            instructions=instructions,
            material=material,
            rubric_title=rubric_title,
            criteria=criteria,
            criterion_titles=criterion_titles,
        )

        return await asyncio.to_thread(
            self._update_task_sync,
            task_id,
            normalized,
        )

    async def duplicate_task(
        self,
        task_id: str,
    ) -> FeedbackTask:
        source = await self.get_task(task_id)

        if source is None:
            raise TaskNotFoundError(
                "Die zu duplizierende Aufgabe wurde nicht gefunden."
            )

        return await self.create_task(
            title=f"{source.title} (Kopie)",
            subject=source.subject,
            grade_level=source.grade_level,
            instructions=source.instructions,
            material=source.material,
            rubric_title=f"{source.rubric.title} (Kopie)",
            criteria=[
                criterion.text
                for criterion in source.rubric.criteria
            ],
            criterion_titles=[
                criterion.title
                for criterion in source.rubric.criteria
            ],
        )

    async def delete_task(
        self,
        task_id: str,
    ) -> TaskDeleteResult:
        return await asyncio.to_thread(
            self._delete_task_sync,
            task_id,
        )

    async def save_feedback_run(
        self,
        *,
        task: FeedbackTask,
        student_text: str,
        provider: str,
        model: str,
        duration_ms: int,
        feedback_payload: dict[str, object],
        provider_request_id: str | None = None,
        queue_duration_ms: float | None = None,
        execution_duration_ms: float | None = None,
        reasoning_effort: str | None = None,
        original_text: str = "",
    ) -> str:
        feedback_run_id = str(uuid4())
        normalized_original_text = _normalize_student_text(original_text)

        if len(normalized_original_text) > MAX_MATERIAL_CHARS:
            raise ValueError(
                "Der Originaltext darf höchstens "
                f"{MAX_MATERIAL_CHARS} Zeichen enthalten."
            )

        await asyncio.to_thread(
            self._save_feedback_run_sync,
            feedback_run_id,
            task,
            student_text,
            provider,
            model,
            duration_ms,
            feedback_payload,
            provider_request_id,
            queue_duration_ms,
            execution_duration_ms,
            reasoning_effort,
            normalized_original_text or None,
        )

        return feedback_run_id

    async def count_feedback_runs(
        self,
        *,
        task_id: str | None = None,
    ) -> int:
        return await asyncio.to_thread(
            self._count_feedback_runs_sync,
            task_id,
        )

    async def select_feedback_run_for_evaluation(
        self,
        *,
        feedback_run_id: str,
        student_text: str,
    ) -> StoredFeedbackRun:
        """Speichert den anonymisierten Text erst nach bewusstem Klick."""

        normalized_student_text = _normalize_student_text(student_text)

        if not normalized_student_text:
            raise FeedbackRunSelectionError(
                "Der Schülertext des Feedbacklaufs fehlt."
            )

        return await asyncio.to_thread(
            self._select_feedback_run_for_evaluation_sync,
            feedback_run_id,
            normalized_student_text,
        )

    async def list_feedback_runs_for_evaluation(
        self,
        *,
        limit: int = 200,
    ) -> list[StoredFeedbackRun]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit muss zwischen 1 und 1000 liegen.")

        return await asyncio.to_thread(
            self._list_feedback_runs_for_evaluation_sync,
            limit,
        )

    async def get_feedback_run_for_evaluation(
        self,
        feedback_run_id: str,
    ) -> StoredFeedbackRun:
        return await asyncio.to_thread(
            self._get_feedback_run_for_evaluation_sync,
            feedback_run_id,
        )

    async def create_manual_feedback_evaluation(
        self,
        *,
        feedback_run_id: str,
        scores: dict[str, int],
        justifications: dict[str, str],
        source_evaluation_id: str | None = None,
        evaluation_name: str | None = None,
    ) -> StoredFeedbackEvaluation:
        try:
            ratings = MANUAL_META_EVALUATION_RUBRIC.build_ratings(
                scores=scores,
                justifications=justifications,
            )
            normalized_name = _normalize_evaluation_name(
                evaluation_name
            )
        except ValueError as exc:
            raise FeedbackEvaluationValidationError(str(exc)) from exc

        return await asyncio.to_thread(
            self._create_manual_feedback_evaluation_sync,
            str(uuid4()),
            feedback_run_id,
            ratings,
            normalized_name,
            (
                source_evaluation_id.strip()
                if isinstance(source_evaluation_id, str)
                and source_evaluation_id.strip()
                else None
            ),
        )

    async def create_automatic_feedback_evaluation(
        self,
        *,
        feedback_run_id: str,
        scores: dict[str, int],
        justifications: dict[str, str],
        evaluator_provider: str,
        evaluator_model: str,
        evaluator_prompt_version: str,
        duration_ms: int,
        provider_request_id: str | None = None,
        queue_duration_ms: float | None = None,
        execution_duration_ms: float | None = None,
        evaluation_name: str | None = None,
    ) -> StoredFeedbackEvaluation:
        try:
            ratings = MANUAL_META_EVALUATION_RUBRIC.build_ratings(
                scores=scores,
                justifications=justifications,
            )
            normalized_name = _normalize_evaluation_name(
                evaluation_name
            )
        except ValueError as exc:
            raise FeedbackEvaluationValidationError(str(exc)) from exc

        normalized_provider = evaluator_provider.strip()
        normalized_model = evaluator_model.strip()
        normalized_prompt_version = evaluator_prompt_version.strip()

        if (
            not normalized_provider
            or not normalized_model
            or not normalized_prompt_version
        ):
            raise FeedbackEvaluationValidationError(
                "Anbieter, Modell oder Prompt-Version der automatischen "
                "Vorbewertung fehlen."
            )

        if type(duration_ms) is not int or duration_ms < 0:
            raise FeedbackEvaluationValidationError(
                "Die Dauer der automatischen Vorbewertung ist ungültig."
            )

        return await asyncio.to_thread(
            self._create_automatic_feedback_evaluation_sync,
            str(uuid4()),
            feedback_run_id,
            ratings,
            normalized_name,
            normalized_provider,
            normalized_model,
            normalized_prompt_version,
            duration_ms,
            queue_duration_ms,
            execution_duration_ms,
            (
                provider_request_id.strip()
                if isinstance(provider_request_id, str)
                and provider_request_id.strip()
                else None
            ),
        )

    async def delete_feedback_evaluation(
        self,
        *,
        feedback_run_id: str,
        evaluation_id: str,
    ) -> None:
        await asyncio.to_thread(
            self._delete_feedback_evaluation_sync,
            feedback_run_id,
            evaluation_id,
        )

    async def remove_feedback_run_from_evaluation(
        self,
        *,
        feedback_run_id: str,
    ) -> None:
        await asyncio.to_thread(
            self._remove_feedback_run_from_evaluation_sync,
            feedback_run_id,
        )

    def _initialize_sync(self) -> None:
        try:
            self.database_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            with self._connect() as connection:
                self._create_schema(connection)

        except sqlite3.Error as exc:
            raise TaskStoreError(
                "Die Aufgaben-Datenbank konnte nicht initialisiert werden."
            ) from exc

    def _list_tasks_sync(
        self,
        include_archived: bool,
    ) -> list[FeedbackTask]:
        try:
            with self._connect() as connection:
                self._create_schema(connection)
                where_clause = (
                    ""
                    if include_archived
                    else "WHERE archived_at IS NULL"
                )
                rows = connection.execute(
                    f"""
                    SELECT task_id
                    FROM feedback_tasks
                    {where_clause}
                    ORDER BY updated_at DESC, title COLLATE NOCASE
                    """
                ).fetchall()

                return [
                    self._load_task(
                        connection,
                        row["task_id"],
                        include_archived=True,
                    )
                    for row in rows
                ]

        except sqlite3.Error as exc:
            raise TaskStoreError(
                "Die Aufgaben konnten nicht geladen werden."
            ) from exc

    def _get_task_sync(
        self,
        task_id: str,
        include_archived: bool,
    ) -> FeedbackTask | None:
        try:
            with self._connect() as connection:
                self._create_schema(connection)
                return self._load_task(
                    connection,
                    task_id,
                    include_archived=include_archived,
                )

        except sqlite3.Error as exc:
            raise TaskStoreError(
                "Die Aufgabe konnte nicht geladen werden."
            ) from exc

    def _create_task_sync(
        self,
        normalized: dict[str, object],
    ) -> FeedbackTask:
        with self._write_lock:
            try:
                self.database_path.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                with self._connect() as connection:
                    self._create_schema(connection)
                    return self._insert_normalized_task(
                        connection,
                        normalized,
                        datetime.now(timezone.utc).isoformat(),
                    )

            except sqlite3.Error as exc:
                raise TaskStoreError(
                    "Die Aufgabe konnte nicht gespeichert werden."
                ) from exc

    def _create_tasks_sync(
        self,
        normalized_tasks: tuple[dict[str, object], ...],
    ) -> list[FeedbackTask]:
        with self._write_lock:
            try:
                self.database_path.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                with self._connect() as connection:
                    self._create_schema(connection)
                    created_tasks = [
                        self._insert_normalized_task(
                            connection,
                            normalized,
                            datetime.now(timezone.utc).isoformat(),
                        )
                        for normalized in normalized_tasks
                    ]

                return created_tasks

            except sqlite3.Error as exc:
                raise TaskStoreError(
                    "Die Feedback-Vorlagen konnten nicht importiert werden."
                ) from exc

    def _update_task_sync(
        self,
        task_id: str,
        normalized: dict[str, object],
    ) -> FeedbackTask:
        timestamp = datetime.now(timezone.utc).isoformat()

        with self._write_lock:
            try:
                with self._connect() as connection:
                    self._create_schema(connection)
                    existing = self._load_task(
                        connection,
                        task_id,
                        include_archived=False,
                    )

                    if existing is None:
                        raise TaskNotFoundError(
                            "Die zu bearbeitende Aufgabe wurde nicht gefunden."
                        )

                    connection.execute(
                        """
                        UPDATE feedback_tasks
                        SET title = ?,
                            subject = ?,
                            grade_level = ?,
                            instructions = ?,
                            material = ?,
                            updated_at = ?
                        WHERE task_id = ?
                        """,
                        (
                            normalized["title"],
                            normalized["subject"],
                            normalized["grade_level"],
                            normalized["instructions"],
                            normalized["material"],
                            timestamp,
                            task_id,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE rubrics
                        SET title = ?, updated_at = ?
                        WHERE rubric_id = ?
                        """,
                        (
                            normalized["rubric_title"],
                            timestamp,
                            existing.rubric.rubric_id,
                        ),
                    )
                    connection.execute(
                        """
                        DELETE FROM rubric_criteria
                        WHERE rubric_id = ?
                        """,
                        (existing.rubric.rubric_id,),
                    )
                    self._insert_criteria(
                        connection,
                        existing.rubric.rubric_id,
                        normalized["criterion_titles"],
                        normalized["criteria"],
                    )

                    updated = self._load_task(
                        connection,
                        task_id,
                        include_archived=True,
                    )

                return updated

            except TaskNotFoundError:
                raise
            except sqlite3.Error as exc:
                raise TaskStoreError(
                    "Die Aufgabe konnte nicht aktualisiert werden."
                ) from exc

    def _delete_task_sync(
        self,
        task_id: str,
    ) -> TaskDeleteResult:
        with self._write_lock:
            try:
                with self._connect() as connection:
                    self._create_schema(connection)
                    task = self._load_task(
                        connection,
                        task_id,
                        include_archived=False,
                    )

                    if task is None:
                        raise TaskNotFoundError(
                            "Die zu löschende Aufgabe wurde nicht gefunden."
                        )

                    used = connection.execute(
                        """
                        SELECT 1
                        FROM rubric_feedback_runs
                        WHERE rubric_id = ?
                        LIMIT 1
                        """,
                        (task.rubric.rubric_id,),
                    ).fetchone()

                    if used is None:
                        connection.execute(
                            """
                            DELETE FROM feedback_tasks
                            WHERE task_id = ?
                            """,
                            (task_id,),
                        )
                        action = "deleted"
                    else:
                        timestamp = datetime.now(
                            timezone.utc
                        ).isoformat()
                        connection.execute(
                            """
                            UPDATE feedback_tasks
                            SET archived_at = ?, updated_at = ?
                            WHERE task_id = ?
                            """,
                            (timestamp, timestamp, task_id),
                        )
                        connection.execute(
                            """
                            UPDATE rubrics
                            SET archived_at = ?, updated_at = ?
                            WHERE rubric_id = ?
                            """,
                            (
                                timestamp,
                                timestamp,
                                task.rubric.rubric_id,
                            ),
                        )
                        action = "archived"

                return TaskDeleteResult(
                    task_id=task_id,
                    action=action,
                )

            except TaskNotFoundError:
                raise
            except sqlite3.Error as exc:
                raise TaskStoreError(
                    "Die Aufgabe konnte nicht gelöscht werden."
                ) from exc

    def _save_feedback_run_sync(
        self,
        feedback_run_id: str,
        task: FeedbackTask,
        student_text: str,
        provider: str,
        model: str,
        duration_ms: int,
        feedback_payload: dict[str, object],
        provider_request_id: str | None,
        queue_duration_ms: float | None,
        execution_duration_ms: float | None,
        reasoning_effort: str | None,
        original_text: str | None,
    ) -> None:
        with self._write_lock:
            try:
                with self._connect() as connection:
                    self._create_schema(connection)
                    known_task = self._load_task(
                        connection,
                        task.task_id,
                        include_archived=True,
                    )

                    if known_task is None:
                        raise TaskNotFoundError(
                            "Die verwendete Aufgabe wurde nicht gefunden."
                        )

                    connection.execute(
                        """
                        INSERT INTO rubric_feedback_runs (
                            feedback_run_id,
                            task_id,
                            rubric_id,
                            created_at,
                            provider,
                            model,
                            reasoning_effort,
                            duration_ms,
                            queue_duration_ms,
                            execution_duration_ms,
                            provider_request_id,
                            student_text_hash,
                            original_text,
                            task_snapshot_json,
                            feedback_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            feedback_run_id,
                            task.task_id,
                            task.rubric.rubric_id,
                            datetime.now(timezone.utc).isoformat(),
                            provider,
                            model,
                            (
                                reasoning_effort.strip()
                                if isinstance(reasoning_effort, str)
                                and reasoning_effort.strip()
                                else None
                            ),
                            max(0, int(duration_ms)),
                            queue_duration_ms,
                            execution_duration_ms,
                            provider_request_id,
                            hashlib.sha256(
                                _normalize_student_text(student_text).encode(
                                    "utf-8"
                                )
                            ).hexdigest(),
                            original_text,
                            json.dumps(
                                task.snapshot(),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            json.dumps(
                                feedback_payload,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        ),
                    )

            except TaskNotFoundError:
                raise
            except sqlite3.Error as exc:
                raise TaskStoreError(
                    "Das kriterienspezifische Feedback konnte nicht "
                    "gespeichert werden."
                ) from exc

    def _count_feedback_runs_sync(
        self,
        task_id: str | None,
    ) -> int:
        try:
            with self._connect() as connection:
                self._create_schema(connection)

                if task_id is None:
                    row = connection.execute(
                        "SELECT COUNT(*) AS total FROM rubric_feedback_runs"
                    ).fetchone()
                else:
                    row = connection.execute(
                        """
                        SELECT COUNT(*) AS total
                        FROM rubric_feedback_runs
                        WHERE task_id = ?
                        """,
                        (task_id,),
                    ).fetchone()

                return int(row["total"])

        except sqlite3.Error as exc:
            raise TaskStoreError(
                "Die gespeicherten Feedbackläufe konnten nicht gezählt werden."
            ) from exc

    def _select_feedback_run_for_evaluation_sync(
        self,
        feedback_run_id: str,
        student_text: str,
    ) -> StoredFeedbackRun:
        student_text_hash = hashlib.sha256(
            student_text.encode("utf-8")
        ).hexdigest()

        with self._write_lock:
            try:
                with self._connect() as connection:
                    self._create_schema(connection)
                    row = connection.execute(
                        """
                        SELECT student_text_hash,
                               selected_for_evaluation_at
                        FROM rubric_feedback_runs
                        WHERE feedback_run_id = ?
                        """,
                        (feedback_run_id,),
                    ).fetchone()

                    if row is None:
                        raise FeedbackRunNotFoundError(
                            "Der Feedbacklauf wurde nicht gefunden."
                        )

                    if not hmac.compare_digest(
                        row["student_text_hash"],
                        student_text_hash,
                    ):
                        raise FeedbackRunSelectionError(
                            "Der Schülertext passt nicht zum Feedbacklauf."
                        )

                    selected_at = (
                        row["selected_for_evaluation_at"]
                        or datetime.now(timezone.utc).isoformat()
                    )
                    connection.execute(
                        """
                        UPDATE rubric_feedback_runs
                        SET student_text = ?,
                            selected_for_evaluation_at = ?
                        WHERE feedback_run_id = ?
                        """,
                        (
                            student_text,
                            selected_at,
                            feedback_run_id,
                        ),
                    )
                    selected = self._load_stored_feedback_run(
                        connection,
                        feedback_run_id,
                    )

                if selected is None:
                    raise FeedbackRunNotFoundError(
                        "Der Feedbacklauf wurde nicht gefunden."
                    )

                return selected

            except (
                FeedbackRunNotFoundError,
                FeedbackRunSelectionError,
            ):
                raise
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise TaskStoreError(
                    "Der Feedbacklauf besitzt ein ungültiges Datenformat."
                ) from exc
            except sqlite3.Error as exc:
                raise TaskStoreError(
                    "Der Feedbacklauf konnte nicht ausgewählt werden."
                ) from exc

    def _list_feedback_runs_for_evaluation_sync(
        self,
        limit: int,
    ) -> list[StoredFeedbackRun]:
        try:
            with self._connect() as connection:
                self._create_schema(connection)
                rows = connection.execute(
                    """
                    SELECT feedback_run_id
                    FROM rubric_feedback_runs
                    WHERE selected_for_evaluation_at IS NOT NULL
                    ORDER BY selected_for_evaluation_at DESC,
                             created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()

                runs = [
                    self._load_stored_feedback_run(
                        connection,
                        row["feedback_run_id"],
                    )
                    for row in rows
                ]

            return [run for run in runs if run is not None]

        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise TaskStoreError(
                "Ein Feedbacklauf besitzt ein ungültiges Datenformat."
            ) from exc
        except sqlite3.Error as exc:
            raise TaskStoreError(
                "Die ausgewählten Feedbackläufe konnten nicht geladen werden."
            ) from exc

    def _get_feedback_run_for_evaluation_sync(
        self,
        feedback_run_id: str,
    ) -> StoredFeedbackRun:
        try:
            with self._connect() as connection:
                self._create_schema(connection)
                feedback_run = self._load_stored_feedback_run(
                    connection,
                    feedback_run_id,
                )

            if feedback_run is None:
                raise FeedbackRunNotFoundError(
                    "Der Feedbacklauf wurde nicht für die Bewertung gefunden."
                )

            return feedback_run

        except FeedbackRunNotFoundError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise TaskStoreError(
                "Der Feedbacklauf besitzt ein ungültiges Datenformat."
            ) from exc
        except sqlite3.Error as exc:
            raise TaskStoreError(
                "Der Feedbacklauf konnte nicht geladen werden."
            ) from exc

    def _create_manual_feedback_evaluation_sync(
        self,
        evaluation_id: str,
        feedback_run_id: str,
        ratings: tuple[FeedbackEvaluationRating, ...],
        evaluation_name: str | None,
        source_evaluation_id: str | None,
    ) -> StoredFeedbackEvaluation:
        return self._create_feedback_evaluation_sync(
            evaluation_id=evaluation_id,
            feedback_run_id=feedback_run_id,
            evaluation_type=MANUAL_EVALUATION_TYPE,
            evaluation_name=evaluation_name,
            ratings=ratings,
            evaluator_provider=None,
            evaluator_model=None,
            evaluator_prompt_version=None,
            source_evaluation_id=source_evaluation_id,
            duration_ms=None,
            queue_duration_ms=None,
            execution_duration_ms=None,
            provider_request_id=None,
        )

    def _create_automatic_feedback_evaluation_sync(
        self,
        evaluation_id: str,
        feedback_run_id: str,
        ratings: tuple[FeedbackEvaluationRating, ...],
        evaluation_name: str | None,
        evaluator_provider: str,
        evaluator_model: str,
        evaluator_prompt_version: str,
        duration_ms: int,
        queue_duration_ms: float | None,
        execution_duration_ms: float | None,
        provider_request_id: str | None,
    ) -> StoredFeedbackEvaluation:
        return self._create_feedback_evaluation_sync(
            evaluation_id=evaluation_id,
            feedback_run_id=feedback_run_id,
            evaluation_type=AUTOMATIC_EVALUATION_TYPE,
            evaluation_name=evaluation_name,
            ratings=ratings,
            evaluator_provider=evaluator_provider,
            evaluator_model=evaluator_model,
            evaluator_prompt_version=evaluator_prompt_version,
            source_evaluation_id=None,
            duration_ms=duration_ms,
            queue_duration_ms=queue_duration_ms,
            execution_duration_ms=execution_duration_ms,
            provider_request_id=provider_request_id,
        )

    def _create_feedback_evaluation_sync(
        self,
        *,
        evaluation_id: str,
        feedback_run_id: str,
        evaluation_type: str,
        evaluation_name: str | None,
        ratings: tuple[FeedbackEvaluationRating, ...],
        evaluator_provider: str | None,
        evaluator_model: str | None,
        evaluator_prompt_version: str | None,
        source_evaluation_id: str | None,
        duration_ms: int | None,
        queue_duration_ms: float | None,
        execution_duration_ms: float | None,
        provider_request_id: str | None,
    ) -> StoredFeedbackEvaluation:
        with self._write_lock:
            try:
                with self._connect() as connection:
                    self._create_schema(connection)
                    feedback_run = connection.execute(
                        """
                        SELECT selected_for_evaluation_at,
                               student_text
                        FROM rubric_feedback_runs
                        WHERE feedback_run_id = ?
                        """,
                        (feedback_run_id,),
                    ).fetchone()

                    if feedback_run is None:
                        raise FeedbackRunNotFoundError(
                            "Der Feedbacklauf wurde nicht gefunden."
                        )

                    if (
                        feedback_run["selected_for_evaluation_at"] is None
                        or feedback_run["student_text"] is None
                    ):
                        raise FeedbackRunSelectionError(
                            "Der Feedbacklauf wurde noch nicht ausdrücklich "
                            "für die Bewertung gespeichert."
                        )

                    if source_evaluation_id is not None:
                        source_evaluation = connection.execute(
                            """
                            SELECT feedback_run_id,
                                   evaluation_type
                            FROM feedback_evaluations
                            WHERE evaluation_id = ?
                            """,
                            (source_evaluation_id,),
                        ).fetchone()

                        if (
                            source_evaluation is None
                            or source_evaluation["feedback_run_id"]
                            != feedback_run_id
                            or source_evaluation["evaluation_type"]
                            != AUTOMATIC_EVALUATION_TYPE
                        ):
                            raise FeedbackEvaluationValidationError(
                                "Die ausgewählte KI-Vorbewertung gehört "
                                "nicht zu diesem Feedbacklauf."
                            )

                    created_at = datetime.now(timezone.utc).isoformat()
                    connection.execute(
                        """
                        INSERT INTO feedback_evaluations (
                            evaluation_id,
                            feedback_run_id,
                            created_at,
                            evaluation_type,
                            evaluation_name,
                            rubric_version,
                            evaluator_provider,
                            evaluator_model,
                            evaluator_prompt_version,
                            source_evaluation_id,
                            duration_ms,
                            queue_duration_ms,
                            execution_duration_ms,
                            provider_request_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            evaluation_id,
                            feedback_run_id,
                            created_at,
                            evaluation_type,
                            evaluation_name,
                            MANUAL_META_EVALUATION_RUBRIC.version,
                            evaluator_provider,
                            evaluator_model,
                            evaluator_prompt_version,
                            source_evaluation_id,
                            duration_ms,
                            queue_duration_ms,
                            execution_duration_ms,
                            provider_request_id,
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO feedback_evaluation_ratings (
                            evaluation_id,
                            criterion_key,
                            position,
                            criterion_title,
                            criterion_question,
                            score,
                            rating_label,
                            justification
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                evaluation_id,
                                rating.criterion_key,
                                rating.position,
                                rating.criterion_title,
                                rating.criterion_question,
                                rating.score,
                                rating.rating_label,
                                rating.justification,
                            )
                            for rating in ratings
                        ],
                    )
                    evaluation = self._load_stored_feedback_evaluation(
                        connection,
                        evaluation_id,
                    )

                if evaluation is None:
                    raise TaskStoreError(
                        "Die gespeicherte Bewertung konnte nicht geladen "
                        "werden."
                    )

                return evaluation

            except (
                FeedbackRunNotFoundError,
                FeedbackRunSelectionError,
                TaskStoreError,
            ):
                raise
            except (TypeError, ValueError) as exc:
                raise TaskStoreError(
                    "Die gespeicherte Bewertung besitzt ein ungültiges "
                    "Datenformat."
                ) from exc
            except sqlite3.Error as exc:
                raise TaskStoreError(
                    "Die Bewertung konnte nicht gespeichert werden."
                ) from exc

    def _delete_feedback_evaluation_sync(
        self,
        feedback_run_id: str,
        evaluation_id: str,
    ) -> None:
        with self._write_lock:
            try:
                with self._connect() as connection:
                    self._create_schema(connection)
                    evaluation = connection.execute(
                        """
                        SELECT evaluation_type
                        FROM feedback_evaluations
                        WHERE evaluation_id = ?
                          AND feedback_run_id = ?
                        """,
                        (evaluation_id, feedback_run_id),
                    ).fetchone()

                    if evaluation is None:
                        raise FeedbackEvaluationNotFoundError(
                            "Die Bewertung wurde nicht gefunden."
                        )

                    if (
                        evaluation["evaluation_type"]
                        == AUTOMATIC_EVALUATION_TYPE
                    ):
                        linked_manual_evaluation = connection.execute(
                            """
                            SELECT 1
                            FROM feedback_evaluations
                            WHERE feedback_run_id = ?
                              AND source_evaluation_id = ?
                            LIMIT 1
                            """,
                            (feedback_run_id, evaluation_id),
                        ).fetchone()

                        if linked_manual_evaluation is not None:
                            raise FeedbackEvaluationDeleteConflictError(
                                "Die KI-Vorbewertung ist noch mit einer "
                                "manuellen Prüfung verknüpft. Lösche zuerst "
                                "die verknüpfte manuelle Bewertung."
                            )

                    connection.execute(
                        """
                        DELETE FROM feedback_evaluations
                        WHERE evaluation_id = ?
                          AND feedback_run_id = ?
                        """,
                        (evaluation_id, feedback_run_id),
                    )

            except (
                FeedbackEvaluationNotFoundError,
                FeedbackEvaluationDeleteConflictError,
            ):
                raise
            except sqlite3.Error as exc:
                raise TaskStoreError(
                    "Die Bewertung konnte nicht gelöscht werden."
                ) from exc

    def _remove_feedback_run_from_evaluation_sync(
        self,
        feedback_run_id: str,
    ) -> None:
        with self._write_lock:
            try:
                with self._connect() as connection:
                    self._create_schema(connection)
                    feedback_run = connection.execute(
                        """
                        SELECT selected_for_evaluation_at,
                               student_text
                        FROM rubric_feedback_runs
                        WHERE feedback_run_id = ?
                        """,
                        (feedback_run_id,),
                    ).fetchone()

                    if (
                        feedback_run is None
                        or feedback_run["selected_for_evaluation_at"] is None
                        or feedback_run["student_text"] is None
                    ):
                        raise FeedbackRunNotFoundError(
                            "Der Feedbacklauf wurde nicht für die Bewertung "
                            "gefunden."
                        )

                    connection.execute(
                        """
                        DELETE FROM feedback_evaluations
                        WHERE feedback_run_id = ?
                        """,
                        (feedback_run_id,),
                    )
                    connection.execute(
                        """
                        UPDATE rubric_feedback_runs
                        SET student_text = NULL,
                            selected_for_evaluation_at = NULL
                        WHERE feedback_run_id = ?
                        """,
                        (feedback_run_id,),
                    )

            except FeedbackRunNotFoundError:
                raise
            except sqlite3.Error as exc:
                raise TaskStoreError(
                    "Der Feedbackbogen konnte nicht aus der "
                    "Feedback-Bewertung entfernt werden."
                ) from exc

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        connection = sqlite3.connect(
            self.database_path,
            timeout=30.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS feedback_tasks (
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

            CREATE TABLE IF NOT EXISTS rubrics (
                rubric_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived_at TEXT,
                FOREIGN KEY (task_id)
                    REFERENCES feedback_tasks (task_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS rubric_criteria (
                criterion_id TEXT PRIMARY KEY,
                rubric_id TEXT NOT NULL,
                position INTEGER NOT NULL CHECK (position >= 0),
                criterion_title TEXT NOT NULL,
                criterion_text TEXT NOT NULL,
                UNIQUE (rubric_id, position),
                FOREIGN KEY (rubric_id)
                    REFERENCES rubrics (rubric_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS rubric_feedback_runs (
                feedback_run_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                rubric_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                reasoning_effort TEXT,
                duration_ms INTEGER NOT NULL,
                queue_duration_ms REAL,
                execution_duration_ms REAL,
                provider_request_id TEXT,
                student_text_hash TEXT NOT NULL,
                original_text TEXT,
                task_snapshot_json TEXT NOT NULL,
                feedback_json TEXT NOT NULL,
                student_text TEXT,
                selected_for_evaluation_at TEXT,
                FOREIGN KEY (task_id)
                    REFERENCES feedback_tasks (task_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (rubric_id)
                    REFERENCES rubrics (rubric_id)
                    ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS feedback_evaluations (
                evaluation_id TEXT PRIMARY KEY,
                feedback_run_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                evaluation_type TEXT NOT NULL
                    CHECK (evaluation_type IN ('manual', 'automatic')),
                evaluation_name TEXT CHECK (
                    evaluation_name IS NULL
                    OR length(trim(evaluation_name)) BETWEEN 1 AND 120
                ),
                rubric_version TEXT NOT NULL,
                evaluator_provider TEXT,
                evaluator_model TEXT,
                evaluator_prompt_version TEXT,
                source_evaluation_id TEXT,
                duration_ms INTEGER CHECK (
                    duration_ms IS NULL OR duration_ms >= 0
                ),
                queue_duration_ms REAL,
                execution_duration_ms REAL,
                provider_request_id TEXT,
                FOREIGN KEY (feedback_run_id)
                    REFERENCES rubric_feedback_runs (feedback_run_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS feedback_evaluation_ratings (
                evaluation_id TEXT NOT NULL,
                criterion_key TEXT NOT NULL,
                position INTEGER NOT NULL CHECK (position >= 0),
                criterion_title TEXT NOT NULL,
                criterion_question TEXT NOT NULL,
                score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 3),
                rating_label TEXT NOT NULL,
                justification TEXT NOT NULL CHECK (
                    length(trim(justification)) > 0
                ),
                PRIMARY KEY (evaluation_id, criterion_key),
                UNIQUE (evaluation_id, position),
                FOREIGN KEY (evaluation_id)
                    REFERENCES feedback_evaluations (evaluation_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS
                idx_feedback_tasks_active
            ON feedback_tasks (archived_at, updated_at DESC);

            CREATE INDEX IF NOT EXISTS
                idx_rubric_criteria_order
            ON rubric_criteria (rubric_id, position);

            CREATE INDEX IF NOT EXISTS
                idx_rubric_feedback_runs_task
            ON rubric_feedback_runs (task_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS
                idx_feedback_evaluations_run
            ON feedback_evaluations (
                feedback_run_id,
                created_at DESC
            );
            """
        )
        TaskStore._migrate_criterion_titles(connection)
        TaskStore._migrate_feedback_evaluation_columns(connection)
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS
                idx_rubric_feedback_runs_evaluation
            ON rubric_feedback_runs (
                selected_for_evaluation_at DESC,
                created_at DESC
            )
            """
        )

    @staticmethod
    def _migrate_criterion_titles(
        connection: sqlite3.Connection,
    ) -> None:
        """Ergänzt Überschriften in Datenbanken früherer Versionen."""

        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(rubric_criteria)"
            ).fetchall()
        }

        if "criterion_title" in columns:
            return

        connection.execute(
            """
            ALTER TABLE rubric_criteria
            ADD COLUMN criterion_title TEXT NOT NULL DEFAULT ''
            """
        )
        connection.execute(
            """
            UPDATE rubric_criteria
            SET criterion_title = 'Kriterium ' || (position + 1)
            WHERE criterion_title = ''
            """
        )

    @staticmethod
    def _migrate_feedback_evaluation_columns(
        connection: sqlite3.Connection,
    ) -> None:
        """Erweitert vorhandene 0.8-Datenbanken additiv für 0.9."""

        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(rubric_feedback_runs)"
            ).fetchall()
        }

        if "student_text" not in columns:
            connection.execute(
                """
                ALTER TABLE rubric_feedback_runs
                ADD COLUMN student_text TEXT
                """
            )

        if "selected_for_evaluation_at" not in columns:
            connection.execute(
                """
                ALTER TABLE rubric_feedback_runs
                ADD COLUMN selected_for_evaluation_at TEXT
                """
            )

        if "reasoning_effort" not in columns:
            connection.execute(
                """
                ALTER TABLE rubric_feedback_runs
                ADD COLUMN reasoning_effort TEXT
                """
            )

        if "original_text" not in columns:
            connection.execute(
                """
                ALTER TABLE rubric_feedback_runs
                ADD COLUMN original_text TEXT
                """
            )

        evaluation_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(feedback_evaluations)"
            ).fetchall()
        }

        if "evaluator_prompt_version" not in evaluation_columns:
            connection.execute(
                """
                ALTER TABLE feedback_evaluations
                ADD COLUMN evaluator_prompt_version TEXT
                """
            )

        if "source_evaluation_id" not in evaluation_columns:
            connection.execute(
                """
                ALTER TABLE feedback_evaluations
                ADD COLUMN source_evaluation_id TEXT
                """
            )

        if "evaluation_name" not in evaluation_columns:
            connection.execute(
                """
                ALTER TABLE feedback_evaluations
                ADD COLUMN evaluation_name TEXT
                """
            )

    @staticmethod
    def _load_feedback_evaluations(
        connection: sqlite3.Connection,
        feedback_run_id: str,
    ) -> tuple[StoredFeedbackEvaluation, ...]:
        rows = connection.execute(
            """
            SELECT evaluation_id
            FROM feedback_evaluations
            WHERE feedback_run_id = ?
            ORDER BY created_at DESC, evaluation_id DESC
            """,
            (feedback_run_id,),
        ).fetchall()

        evaluations = [
            TaskStore._load_stored_feedback_evaluation(
                connection,
                row["evaluation_id"],
            )
            for row in rows
        ]
        return tuple(
            evaluation
            for evaluation in evaluations
            if evaluation is not None
        )

    @staticmethod
    def _load_stored_feedback_evaluation(
        connection: sqlite3.Connection,
        evaluation_id: str,
    ) -> StoredFeedbackEvaluation | None:
        row = connection.execute(
            """
            SELECT evaluation_id,
                   feedback_run_id,
                   created_at,
                   evaluation_type,
                   evaluation_name,
                   rubric_version,
                   evaluator_provider,
                   evaluator_model,
                   evaluator_prompt_version,
                   source_evaluation_id,
                   duration_ms,
                   queue_duration_ms,
                   execution_duration_ms,
                   provider_request_id
            FROM feedback_evaluations
            WHERE evaluation_id = ?
            """,
            (evaluation_id,),
        ).fetchone()

        if row is None:
            return None

        rating_rows = connection.execute(
            """
            SELECT criterion_key,
                   position,
                   criterion_title,
                   criterion_question,
                   score,
                   rating_label,
                   justification
            FROM feedback_evaluation_ratings
            WHERE evaluation_id = ?
            ORDER BY position
            """,
            (evaluation_id,),
        ).fetchall()
        ratings = tuple(
            FeedbackEvaluationRating(
                criterion_key=rating_row["criterion_key"],
                position=rating_row["position"],
                criterion_title=rating_row["criterion_title"],
                criterion_question=rating_row["criterion_question"],
                score=rating_row["score"],
                rating_label=rating_row["rating_label"],
                justification=rating_row["justification"],
            )
            for rating_row in rating_rows
        )

        return StoredFeedbackEvaluation(
            evaluation_id=row["evaluation_id"],
            feedback_run_id=row["feedback_run_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            evaluation_type=row["evaluation_type"],
            evaluation_name=row["evaluation_name"],
            rubric_version=row["rubric_version"],
            ratings=ratings,
            evaluator_provider=row["evaluator_provider"],
            evaluator_model=row["evaluator_model"],
            evaluator_prompt_version=row["evaluator_prompt_version"],
            source_evaluation_id=row["source_evaluation_id"],
            duration_ms=row["duration_ms"],
            queue_duration_ms=row["queue_duration_ms"],
            execution_duration_ms=row["execution_duration_ms"],
            provider_request_id=row["provider_request_id"],
        )

    @staticmethod
    def _load_stored_feedback_run(
        connection: sqlite3.Connection,
        feedback_run_id: str,
    ) -> StoredFeedbackRun | None:
        row = connection.execute(
            """
            SELECT feedback_run_id,
                   task_id,
                   rubric_id,
                   created_at,
                   selected_for_evaluation_at,
                   provider,
                   model,
                   reasoning_effort,
                   duration_ms,
                   queue_duration_ms,
                   execution_duration_ms,
                   provider_request_id,
                   student_text,
                   original_text,
                   task_snapshot_json,
                   feedback_json
            FROM rubric_feedback_runs
            WHERE feedback_run_id = ?
              AND selected_for_evaluation_at IS NOT NULL
              AND student_text IS NOT NULL
            """,
            (feedback_run_id,),
        ).fetchone()

        if row is None:
            return None

        task_snapshot = json.loads(row["task_snapshot_json"])
        feedback_payload = json.loads(row["feedback_json"])

        if not isinstance(task_snapshot, dict):
            raise TypeError("task_snapshot_json ist kein Objekt.")
        if not isinstance(feedback_payload, dict):
            raise TypeError("feedback_json ist kein Objekt.")

        return StoredFeedbackRun(
            feedback_run_id=row["feedback_run_id"],
            task_id=row["task_id"],
            rubric_id=row["rubric_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            selected_for_evaluation_at=datetime.fromisoformat(
                row["selected_for_evaluation_at"]
            ),
            provider=row["provider"],
            model=row["model"],
            reasoning_effort=row["reasoning_effort"],
            duration_ms=row["duration_ms"],
            queue_duration_ms=row["queue_duration_ms"],
            execution_duration_ms=row["execution_duration_ms"],
            provider_request_id=row["provider_request_id"],
            student_text=row["student_text"],
            task_snapshot=task_snapshot,
            feedback_payload=feedback_payload,
            evaluations=TaskStore._load_feedback_evaluations(
                connection,
                feedback_run_id,
            ),
            original_text=row["original_text"],
        )

    @staticmethod
    def _insert_normalized_task(
        connection: sqlite3.Connection,
        normalized: dict[str, object],
        timestamp: str,
    ) -> FeedbackTask:
        task_id = str(uuid4())
        rubric_id = str(uuid4())

        connection.execute(
            """
            INSERT INTO feedback_tasks (
                task_id,
                title,
                subject,
                grade_level,
                instructions,
                material,
                created_at,
                updated_at,
                archived_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                task_id,
                normalized["title"],
                normalized["subject"],
                normalized["grade_level"],
                normalized["instructions"],
                normalized["material"],
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO rubrics (
                rubric_id,
                task_id,
                title,
                created_at,
                updated_at,
                archived_at
            )
            VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (
                rubric_id,
                task_id,
                normalized["rubric_title"],
                timestamp,
                timestamp,
            ),
        )
        TaskStore._insert_criteria(
            connection,
            rubric_id,
            normalized["criterion_titles"],
            normalized["criteria"],
        )

        created = TaskStore._load_task(
            connection,
            task_id,
            include_archived=True,
        )

        if created is None:
            raise sqlite3.IntegrityError(
                "Die neu angelegte Aufgabe konnte nicht geladen werden."
            )

        return created

    @staticmethod
    def _insert_criteria(
        connection: sqlite3.Connection,
        rubric_id: str,
        criterion_titles: object,
        criteria: object,
    ) -> None:
        if not isinstance(criterion_titles, tuple):
            raise TypeError(
                "criterion_titles muss als Tupel normalisiert sein."
            )
        if not isinstance(criteria, tuple):
            raise TypeError("criteria muss als Tupel normalisiert sein.")
        if len(criterion_titles) != len(criteria):
            raise ValueError(
                "Kriterienüberschriften und Kriterien müssen "
                "gleich lang sein."
            )

        connection.executemany(
            """
            INSERT INTO rubric_criteria (
                criterion_id,
                rubric_id,
                position,
                criterion_title,
                criterion_text
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    str(uuid4()),
                    rubric_id,
                    position,
                    criterion_title,
                    criterion,
                )
                for position, (criterion_title, criterion) in enumerate(
                    zip(criterion_titles, criteria, strict=True)
                )
            ],
        )

    @staticmethod
    def _load_task(
        connection: sqlite3.Connection,
        task_id: str,
        *,
        include_archived: bool,
    ) -> FeedbackTask | None:
        archive_filter = (
            ""
            if include_archived
            else (
                "AND task.archived_at IS NULL "
                "AND rubric.archived_at IS NULL"
            )
        )
        row = connection.execute(
            f"""
            SELECT
                task.task_id,
                task.title,
                task.subject,
                task.grade_level,
                task.instructions,
                task.material,
                task.created_at,
                task.updated_at,
                task.archived_at,
                rubric.rubric_id,
                rubric.title AS rubric_title
            FROM feedback_tasks AS task
            JOIN rubrics AS rubric ON rubric.task_id = task.task_id
            WHERE task.task_id = ?
            {archive_filter}
            """,
            (task_id,),
        ).fetchone()

        if row is None:
            return None

        criterion_rows = connection.execute(
            """
            SELECT
                criterion_id,
                criterion_title,
                criterion_text,
                position
            FROM rubric_criteria
            WHERE rubric_id = ?
            ORDER BY position
            """,
            (row["rubric_id"],),
        ).fetchall()

        return FeedbackTask(
            task_id=row["task_id"],
            title=row["title"],
            subject=row["subject"],
            grade_level=row["grade_level"],
            instructions=row["instructions"],
            material=row["material"],
            rubric=Rubric(
                rubric_id=row["rubric_id"],
                title=row["rubric_title"],
                criteria=tuple(
                    RubricCriterion(
                        criterion_id=criterion_row["criterion_id"],
                        title=criterion_row["criterion_title"],
                        text=criterion_row["criterion_text"],
                        position=criterion_row["position"],
                    )
                    for criterion_row in criterion_rows
                ),
            ),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            archived_at=(
                datetime.fromisoformat(row["archived_at"])
                if row["archived_at"]
                else None
            ),
        )

    def _validate_input(
        self,
        *,
        title: str,
        subject: str,
        grade_level: str,
        instructions: str,
        material: str,
        rubric_title: str,
        criteria: list[str] | tuple[str, ...],
        criterion_titles: list[str] | tuple[str, ...] | None,
    ) -> dict[str, object]:
        normalized_title = TaskStore._required_text(
            title,
            "Bitte gib einen Aufgabentitel ein.",
            200,
        )
        normalized_instructions = TaskStore._required_text(
            instructions,
            "Bitte gib eine Aufgabenstellung ein.",
            MAX_TASK_INSTRUCTIONS_CHARS,
        )
        normalized_rubric_title = TaskStore._required_text(
            rubric_title,
            "Bitte gib einen Namen für das Feedback ein.",
            200,
        )
        normalized_subject = subject.strip()
        normalized_grade_level = grade_level.strip()
        normalized_material = material.strip()

        if len(normalized_subject) > 100:
            raise ValueError("Das Fach darf höchstens 100 Zeichen lang sein.")
        if len(normalized_grade_level) > 100:
            raise ValueError(
                "Die Jahrgangsstufe darf höchstens 100 Zeichen lang sein."
            )
        if len(normalized_material) > MAX_MATERIAL_CHARS:
            raise ValueError(
                "Das Material darf höchstens "
                f"{MAX_MATERIAL_CHARS} Zeichen lang sein."
            )

        normalized_criteria = tuple(
            criterion.strip()
            for criterion in criteria
        )

        if not normalized_criteria:
            raise ValueError(
                "Die Feedback-Vorlage benötigt mindestens ein Kriterium."
            )
        if len(normalized_criteria) > self.max_criteria:
            raise ValueError(
                "Eine Feedback-Vorlage darf höchstens "
                f"{self.max_criteria} Kriterien enthalten."
            )
        if any(not criterion for criterion in normalized_criteria):
            raise ValueError(
                "Bitte fülle alle Feedback-Kriterien aus oder entferne "
                "leere Felder."
            )
        if any(
            len(criterion) > self.max_criterion_chars
            for criterion in normalized_criteria
        ):
            raise ValueError(
                "Ein Feedback-Kriterium darf höchstens "
                f"{self.max_criterion_chars} Zeichen lang sein."
            )

        normalized_criterion_titles = (
            tuple(
                title.strip()
                for title in criterion_titles
            )
            if criterion_titles is not None
            else tuple(
                f"Kriterium {position}"
                for position in range(1, len(normalized_criteria) + 1)
            )
        )

        if len(normalized_criterion_titles) != len(normalized_criteria):
            raise ValueError(
                "Bitte gib zu jedem Feedback-Kriterium genau eine "
                "Überschrift an."
            )
        if any(not title for title in normalized_criterion_titles):
            raise ValueError(
                "Bitte gib für jedes Feedback-Kriterium eine "
                "Überschrift an."
            )
        if any(
            len(title) > MAX_CRITERION_TITLE_CHARS
            for title in normalized_criterion_titles
        ):
            raise ValueError(
                "Eine Kriterienüberschrift darf höchstens "
                f"{MAX_CRITERION_TITLE_CHARS} Zeichen lang sein."
            )

        return {
            "title": normalized_title,
            "subject": normalized_subject,
            "grade_level": normalized_grade_level,
            "instructions": normalized_instructions,
            "material": normalized_material,
            "rubric_title": normalized_rubric_title,
            "criterion_titles": normalized_criterion_titles,
            "criteria": normalized_criteria,
        }

    @staticmethod
    def _required_text(
        value: str,
        missing_message: str,
        maximum_length: int,
    ) -> str:
        normalized = value.strip()

        if not normalized:
            raise ValueError(missing_message)
        if len(normalized) > maximum_length:
            raise ValueError(
                "Ein Eingabefeld überschreitet die erlaubte Länge von "
                f"{maximum_length} Zeichen."
            )

        return normalized
