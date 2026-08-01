from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from threading import Lock
from uuid import UUID

from app.domain.metrics import AnalysisRunRecord


class AnalysisRunStoreError(RuntimeError):
    """Fehler beim Speichern oder Lesen von Analyseläufen."""


class AnalysisRunStore:
    """
    Speichert technische Analyseläufe in einer lokalen SQLite-Datenbank.

    Schülertexte, Prompts und API-Schlüssel werden hier nicht gespeichert.
    """

    def __init__(
        self,
        database_path: str | Path,
    ) -> None:
        self.database_path = Path(database_path)
        self._write_lock = Lock()

    async def initialize(self) -> None:
        """Legt Datenbank und Tabellen an, sofern sie noch nicht existieren."""

        await asyncio.to_thread(self._initialize_sync)

    async def save(
        self,
        record: AnalysisRunRecord,
    ) -> None:
        """Speichert einen vollständigen Laufdatensatz."""

        await asyncio.to_thread(
            self._save_sync,
            record,
        )

    async def get(
        self,
        run_id: UUID | str,
    ) -> AnalysisRunRecord | None:
        """Lädt einen einzelnen Analyselauf."""

        return await asyncio.to_thread(
            self._get_sync,
            str(run_id),
        )

    async def list_runs(
        self,
        *,
        limit: int = 100,
        provider_id: str | None = None,
        model_id: str | None = None,
        success: bool | None = None,
    ) -> list[AnalysisRunRecord]:
        """Lädt Analyseläufe mit optionalen Filtern."""

        if limit < 1 or limit > 1000:
            raise ValueError(
                "limit muss zwischen 1 und 1000 liegen."
            )

        return await asyncio.to_thread(
            self._list_runs_sync,
            limit,
            provider_id,
            model_id,
            success,
        )

    def _initialize_sync(self) -> None:
        try:
            self.database_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            with self._connect() as connection:
                self._create_schema(connection)
                connection.commit()

        except sqlite3.Error as exc:
            raise AnalysisRunStoreError(
                "Die Datenbank für Analyseläufe konnte nicht initialisiert werden."
            ) from exc

    def _save_sync(
        self,
        record: AnalysisRunRecord,
    ) -> None:
        with self._write_lock:
            try:
                self.database_path.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                with self._connect() as connection:
                    self._create_schema(connection)

                    token_usage = record.metrics.token_usage

                    connection.execute(
                        """
                        INSERT INTO analysis_runs (
                            run_id,
                            created_at,
                            provider_id,
                            model_id,
                            requested_model_name,
                            actual_model_name,
                            prompt_version,
                            schema_version,
                            success,
                            status,
                            total_duration_ms,
                            input_tokens,
                            output_tokens,
                            reasoning_tokens,
                            cached_input_tokens,
                            cache_write_tokens,
                            total_tokens,
                            tokens_per_second,
                            retry_count,
                            error_type,
                            error_message,
                            provider_request_id,
                            record_json
                        )
                        VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        (
                            str(record.run_id),
                            record.created_at.isoformat(),
                            record.provider_id,
                            record.model_id,
                            record.requested_model_name,
                            record.actual_model_name,
                            record.prompt_version,
                            record.schema_version,
                            int(record.success),
                            record.status,
                            record.metrics.total_duration_ms.value,
                            token_usage.input_tokens.value,
                            token_usage.output_tokens.value,
                            token_usage.reasoning_tokens.value,
                            token_usage.cached_input_tokens.value,
                            token_usage.cache_write_tokens.value,
                            token_usage.total_tokens.value,
                            record.metrics.tokens_per_second.value,
                            record.metrics.retry_count,
                            (
                                record.error_type.value
                                if record.error_type is not None
                                else None
                            ),
                            record.error_message,
                            record.provider_request_id,
                            record.model_dump_json(),
                        ),
                    )

                    connection.commit()

            except sqlite3.IntegrityError as exc:
                raise AnalysisRunStoreError(
                    (
                        "Der Analyselauf wurde bereits gespeichert: "
                        f"{record.run_id}"
                    )
                ) from exc

            except sqlite3.Error as exc:
                raise AnalysisRunStoreError(
                    "Der Analyselauf konnte nicht gespeichert werden."
                ) from exc

    def _get_sync(
        self,
        run_id: str,
    ) -> AnalysisRunRecord | None:
        try:
            with self._connect() as connection:
                self._create_schema(connection)

                row = connection.execute(
                    """
                    SELECT record_json
                    FROM analysis_runs
                    WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()

            if row is None:
                return None

            return self._deserialize_record(
                row["record_json"]
            )

        except sqlite3.Error as exc:
            raise AnalysisRunStoreError(
                "Der Analyselauf konnte nicht gelesen werden."
            ) from exc

    def _list_runs_sync(
        self,
        limit: int,
        provider_id: str | None,
        model_id: str | None,
        success: bool | None,
    ) -> list[AnalysisRunRecord]:
        conditions: list[str] = []
        parameters: list[object] = []

        if provider_id is not None:
            conditions.append("provider_id = ?")
            parameters.append(provider_id)

        if model_id is not None:
            conditions.append("model_id = ?")
            parameters.append(model_id)

        if success is not None:
            conditions.append("success = ?")
            parameters.append(int(success))

        where_clause = ""

        if conditions:
            where_clause = (
                "WHERE " + " AND ".join(conditions)
            )

        parameters.append(limit)

        query = f"""
            SELECT record_json
            FROM analysis_runs
            {where_clause}
            ORDER BY created_at DESC
            LIMIT ?
        """

        try:
            with self._connect() as connection:
                self._create_schema(connection)
                rows = connection.execute(
                    query,
                    parameters,
                ).fetchall()

            return [
                self._deserialize_record(row["record_json"])
                for row in rows
            ]

        except sqlite3.Error as exc:
            raise AnalysisRunStoreError(
                "Die Analyseläufe konnten nicht gelesen werden."
            ) from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30.0,
        )
        connection.row_factory = sqlite3.Row

        connection.execute(
            "PRAGMA journal_mode = WAL"
        )
        connection.execute(
            "PRAGMA foreign_keys = ON"
        )

        return connection

    @staticmethod
    def _create_schema(
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_runs (
                run_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                requested_model_name TEXT NOT NULL,
                actual_model_name TEXT,
                prompt_version TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                success INTEGER NOT NULL
                    CHECK (success IN (0, 1)),
                status TEXT,
                total_duration_ms REAL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                reasoning_tokens INTEGER,
                cached_input_tokens INTEGER,
                cache_write_tokens INTEGER,
                total_tokens INTEGER,
                tokens_per_second REAL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                error_type TEXT,
                error_message TEXT,
                provider_request_id TEXT,
                record_json TEXT NOT NULL
            )
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS
                idx_analysis_runs_provider_model
            ON analysis_runs (
                provider_id,
                model_id
            )
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS
                idx_analysis_runs_created_at
            ON analysis_runs (
                created_at DESC
            )
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS
                idx_analysis_runs_success
            ON analysis_runs (
                success
            )
            """
        )

    @staticmethod
    def _deserialize_record(
        serialized_record: str,
    ) -> AnalysisRunRecord:
        try:
            return AnalysisRunRecord.model_validate_json(
                serialized_record
            )
        except Exception as exc:
            raise AnalysisRunStoreError(
                (
                    "Ein gespeicherter Analyselauf besitzt ein "
                    "ungültiges Datenformat."
                )
            ) from exc