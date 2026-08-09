from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock


ACTIVE_RUNPOD_JOB_STATUSES = frozenset(
    {
        "IN_QUEUE",
        "IN_PROGRESS",
        "RUNNING",
    }
)
TERMINAL_RUNPOD_JOB_STATUSES = frozenset(
    {
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "TIMED_OUT",
        "NOT_FOUND",
    }
)


class RunPodJobStoreError(RuntimeError):
    """Fehler beim Speichern oder Lesen aktiver RunPod-Aufträge."""


@dataclass(frozen=True)
class RunPodTrackedJob:
    """Technische Zuordnung eines Browserlaufs zu einem RunPod-Job."""

    tracking_id: str
    job_id: str
    endpoint_key: str
    endpoint_id: str
    status: str
    created_at: datetime
    updated_at: datetime


class RunPodJobStore:
    """
    Speichert ausschließlich technische Jobdaten in SQLite.

    Schülertexte, Prompts und API-Schlüssel werden nicht gespeichert.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self._write_lock = Lock()

    async def record_status(
        self,
        *,
        tracking_id: str,
        job_id: str,
        endpoint_key: str,
        endpoint_id: str,
        status: str,
    ) -> None:
        """Registriert einen neuen Job oder aktualisiert seinen Status."""

        await asyncio.to_thread(
            self._record_status_sync,
            tracking_id,
            job_id,
            endpoint_key,
            endpoint_id,
            status,
        )

    async def update_known_job(
        self,
        *,
        endpoint_id: str,
        job_id: str,
        status: str,
    ) -> bool:
        """Aktualisiert einen bereits registrierten Job, sofern vorhanden."""

        return await asyncio.to_thread(
            self._update_known_job_sync,
            endpoint_id,
            job_id,
            status,
        )

    async def list_active(
        self,
        *,
        endpoint_key: str,
        limit: int = 25,
    ) -> list[RunPodTrackedJob]:
        """Lädt aktive Jobs eines öffentlichen Endpoint-Schlüssels."""

        if limit < 1 or limit > 100:
            raise ValueError("limit muss zwischen 1 und 100 liegen.")

        return await asyncio.to_thread(
            self._list_active_sync,
            endpoint_key,
            limit,
        )

    def _record_status_sync(
        self,
        tracking_id: str,
        job_id: str,
        endpoint_key: str,
        endpoint_id: str,
        status: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()

        with self._write_lock:
            try:
                self.database_path.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                with self._connect() as connection:
                    self._create_schema(connection)
                    existing = connection.execute(
                        """
                        SELECT job_id, endpoint_id, status
                        FROM runpod_jobs
                        WHERE tracking_id = ?
                        """,
                        (tracking_id,),
                    ).fetchone()

                    if existing is not None and (
                        existing["job_id"] != job_id
                        or existing["endpoint_id"] != endpoint_id
                    ):
                        raise RunPodJobStoreError(
                            "Die Tracking-ID ist bereits einem "
                            "anderen Job zugeordnet."
                        )

                    if (
                        existing is not None
                        and existing["status"]
                        in TERMINAL_RUNPOD_JOB_STATUSES
                    ):
                        return

                    connection.execute(
                        """
                        INSERT INTO runpod_jobs (
                            tracking_id,
                            job_id,
                            endpoint_key,
                            endpoint_id,
                            status,
                            created_at,
                            updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(tracking_id) DO UPDATE SET
                            status = excluded.status,
                            updated_at = excluded.updated_at
                        """,
                        (
                            tracking_id,
                            job_id,
                            endpoint_key,
                            endpoint_id,
                            self._normalized_status(status),
                            now,
                            now,
                        ),
                    )
                    connection.commit()
            except RunPodJobStoreError:
                raise
            except sqlite3.Error as exc:
                raise RunPodJobStoreError(
                    "Der RunPod-Auftrag konnte nicht gespeichert werden."
                ) from exc

    def _update_known_job_sync(
        self,
        endpoint_id: str,
        job_id: str,
        status: str,
    ) -> bool:
        with self._write_lock:
            try:
                self.database_path.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                with self._connect() as connection:
                    self._create_schema(connection)
                    existing = connection.execute(
                        """
                        SELECT status
                        FROM runpod_jobs
                        WHERE endpoint_id = ? AND job_id = ?
                        """,
                        (endpoint_id, job_id),
                    ).fetchone()

                    if existing is None:
                        return False

                    if (
                        existing["status"]
                        in TERMINAL_RUNPOD_JOB_STATUSES
                    ):
                        return True

                    cursor = connection.execute(
                        """
                        UPDATE runpod_jobs
                        SET status = ?, updated_at = ?
                        WHERE endpoint_id = ? AND job_id = ?
                        """,
                        (
                            self._normalized_status(status),
                            datetime.now(timezone.utc).isoformat(),
                            endpoint_id,
                            job_id,
                        ),
                    )
                    connection.commit()
                    return cursor.rowcount > 0
            except sqlite3.Error as exc:
                raise RunPodJobStoreError(
                    "Der RunPod-Jobstatus konnte nicht aktualisiert werden."
                ) from exc

    def _list_active_sync(
        self,
        endpoint_key: str,
        limit: int,
    ) -> list[RunPodTrackedJob]:
        placeholders = ", ".join(
            "?" for _ in ACTIVE_RUNPOD_JOB_STATUSES
        )
        statuses = sorted(ACTIVE_RUNPOD_JOB_STATUSES)

        try:
            self.database_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            with self._connect() as connection:
                self._create_schema(connection)
                rows = connection.execute(
                    f"""
                    SELECT
                        tracking_id,
                        job_id,
                        endpoint_key,
                        endpoint_id,
                        status,
                        created_at,
                        updated_at
                    FROM runpod_jobs
                    WHERE endpoint_key = ?
                      AND status IN ({placeholders})
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (endpoint_key, *statuses, limit),
                ).fetchall()

            return [self._row_to_job(row) for row in rows]
        except sqlite3.Error as exc:
            raise RunPodJobStoreError(
                "Die aktiven RunPod-Aufträge konnten nicht gelesen werden."
            ) from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runpod_jobs (
                tracking_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                endpoint_key TEXT NOT NULL,
                endpoint_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(endpoint_id, job_id)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_runpod_jobs_active
            ON runpod_jobs (endpoint_key, status, created_at)
            """
        )

    @staticmethod
    def _normalized_status(status: str) -> str:
        normalized = status.strip().upper()

        if not normalized:
            raise ValueError("Der RunPod-Jobstatus darf nicht leer sein.")

        return normalized

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> RunPodTrackedJob:
        return RunPodTrackedJob(
            tracking_id=row["tracking_id"],
            job_id=row["job_id"],
            endpoint_key=row["endpoint_key"],
            endpoint_id=row["endpoint_id"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
