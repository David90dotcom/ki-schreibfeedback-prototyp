from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from app.domain.rubric import (
    FeedbackTask,
    FeedbackTaskDraft,
    Rubric,
    RubricCriterion,
    TaskDeleteResult,
)


MAX_CRITERIA = 30
MAX_CRITERION_CHARS = 1500
MAX_MATERIAL_CHARS = 30000
MAX_TASK_INSTRUCTIONS_CHARS = 12000


class TaskStoreError(RuntimeError):
    """Fehler beim Speichern oder Lesen von Aufgaben."""


class TaskNotFoundError(TaskStoreError):
    """Die angeforderte Aufgabe ist nicht vorhanden oder archiviert."""


class TaskStore:
    """Verwaltet Aufgaben und Feedback-Vorlagen in SQLite."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
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
    ) -> FeedbackTask:
        normalized = self._validate_input(
            title=title,
            subject=subject,
            grade_level=grade_level,
            instructions=instructions,
            material=material,
            rubric_title=rubric_title,
            criteria=criteria,
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
    ) -> FeedbackTask:
        normalized = self._validate_input(
            title=title,
            subject=subject,
            grade_level=grade_level,
            instructions=instructions,
            material=material,
            rubric_title=rubric_title,
            criteria=criteria,
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
    ) -> str:
        feedback_run_id = str(uuid4())

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
                            duration_ms,
                            queue_duration_ms,
                            execution_duration_ms,
                            provider_request_id,
                            student_text_hash,
                            task_snapshot_json,
                            feedback_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            feedback_run_id,
                            task.task_id,
                            task.rubric.rubric_id,
                            datetime.now(timezone.utc).isoformat(),
                            provider,
                            model,
                            max(0, int(duration_ms)),
                            queue_duration_ms,
                            execution_duration_ms,
                            provider_request_id,
                            hashlib.sha256(
                                student_text.strip().encode("utf-8")
                            ).hexdigest(),
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
                duration_ms INTEGER NOT NULL,
                queue_duration_ms REAL,
                execution_duration_ms REAL,
                provider_request_id TEXT,
                student_text_hash TEXT NOT NULL,
                task_snapshot_json TEXT NOT NULL,
                feedback_json TEXT NOT NULL,
                FOREIGN KEY (task_id)
                    REFERENCES feedback_tasks (task_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (rubric_id)
                    REFERENCES rubrics (rubric_id)
                    ON DELETE RESTRICT
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
            """
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
        criteria: object,
    ) -> None:
        if not isinstance(criteria, tuple):
            raise TypeError("criteria muss als Tupel normalisiert sein.")

        connection.executemany(
            """
            INSERT INTO rubric_criteria (
                criterion_id,
                rubric_id,
                position,
                criterion_text
            )
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    str(uuid4()),
                    rubric_id,
                    position,
                    criterion,
                )
                for position, criterion in enumerate(criteria)
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
            SELECT criterion_id, criterion_text, position
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

    @staticmethod
    def _validate_input(
        *,
        title: str,
        subject: str,
        grade_level: str,
        instructions: str,
        material: str,
        rubric_title: str,
        criteria: list[str] | tuple[str, ...],
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
        if len(normalized_criteria) > MAX_CRITERIA:
            raise ValueError(
                "Eine Feedback-Vorlage darf höchstens "
                f"{MAX_CRITERIA} Kriterien enthalten."
            )
        if any(not criterion for criterion in normalized_criteria):
            raise ValueError(
                "Bitte fülle alle Feedback-Kriterien aus oder entferne "
                "leere Felder."
            )
        if any(
            len(criterion) > MAX_CRITERION_CHARS
            for criterion in normalized_criteria
        ):
            raise ValueError(
                "Ein Feedback-Kriterium darf höchstens "
                f"{MAX_CRITERION_CHARS} Zeichen lang sein."
            )

        return {
            "title": normalized_title,
            "subject": normalized_subject,
            "grade_level": normalized_grade_level,
            "instructions": normalized_instructions,
            "material": normalized_material,
            "rubric_title": normalized_rubric_title,
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
