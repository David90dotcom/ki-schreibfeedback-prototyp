from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from app.domain.student_account import (
    MAX_STUDENT_ACCOUNT_LABEL_CHARS,
    STUDENT_ACCESS_CODE_LENGTH,
    IssuedStudentAccessCode,
    StudentAccount,
)


MAX_CODE_GENERATION_ATTEMPTS = 100


class StudentAccountStoreError(RuntimeError):
    """Allgemeiner Fehler der Schülerkontenverwaltung."""


class StudentAccountNotFoundError(StudentAccountStoreError):
    """Das angeforderte Schülerkonto ist nicht vorhanden."""


class StudentAccountConflictError(StudentAccountStoreError):
    """Die Kontobezeichnung ist bereits vergeben."""


class StudentAccessCodeGenerationError(StudentAccountStoreError):
    """Es konnte kein eindeutiger Zugangscode erzeugt werden."""


class StudentAccountStore:
    """Verwaltet pseudonyme Schülerkonten in der Anwendungsdatenbank."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        code_secret: str,
        code_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(code_secret, str) or not code_secret:
            raise ValueError("Das Secret für Schülercodes darf nicht leer sein.")

        self.database_path = Path(database_path)
        self._code_secret = code_secret.encode("utf-8")
        self._code_factory = code_factory or self._generate_access_code
        self._write_lock = Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def list_accounts(self) -> list[StudentAccount]:
        return await asyncio.to_thread(self._list_accounts_sync)

    async def create_account(
        self,
        label: str,
    ) -> IssuedStudentAccessCode:
        normalized_label = self._normalize_label(label)
        return await asyncio.to_thread(
            self._create_account_sync,
            normalized_label,
        )

    async def issue_new_code(
        self,
        account_id: str,
    ) -> IssuedStudentAccessCode:
        return await asyncio.to_thread(
            self._issue_new_code_sync,
            account_id,
        )

    async def set_account_active(
        self,
        account_id: str,
        *,
        active: bool,
    ) -> StudentAccount:
        return await asyncio.to_thread(
            self._set_account_active_sync,
            account_id,
            active,
        )

    async def delete_account(self, account_id: str) -> None:
        await asyncio.to_thread(
            self._delete_account_sync,
            account_id,
        )

    async def authenticate_code(
        self,
        access_code: str,
    ) -> StudentAccount | None:
        normalized_code = self._normalize_access_code(access_code)

        if normalized_code is None:
            return None

        return await asyncio.to_thread(
            self._authenticate_code_sync,
            normalized_code,
        )

    async def get_active_account(
        self,
        account_id: str,
    ) -> StudentAccount | None:
        return await asyncio.to_thread(
            self._get_active_account_sync,
            account_id,
        )

    @staticmethod
    def _generate_access_code() -> str:
        upper_bound = 10**STUDENT_ACCESS_CODE_LENGTH
        return f"{secrets.randbelow(upper_bound):0{STUDENT_ACCESS_CODE_LENGTH}d}"

    @staticmethod
    def _normalize_label(label: str) -> str:
        if not isinstance(label, str):
            raise ValueError("Die Kontobezeichnung muss Text sein.")

        normalized = " ".join(label.split())

        if not normalized:
            raise ValueError("Bitte gib eine Kontobezeichnung ein.")

        if len(normalized) > MAX_STUDENT_ACCOUNT_LABEL_CHARS:
            raise ValueError(
                "Die Kontobezeichnung darf höchstens "
                f"{MAX_STUDENT_ACCOUNT_LABEL_CHARS} Zeichen enthalten."
            )

        return normalized

    @staticmethod
    def _normalize_access_code(access_code: str) -> str | None:
        if not isinstance(access_code, str):
            return None

        normalized = access_code.strip()

        if (
            len(normalized) != STUDENT_ACCESS_CODE_LENGTH
            or not normalized.isascii()
            or not normalized.isdigit()
        ):
            return None

        return normalized

    def _code_digest(self, access_code: str) -> str:
        return hmac.new(
            self._code_secret,
            access_code.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

    def _initialize_sync(self) -> None:
        try:
            with self._connect() as connection:
                self._create_schema(connection)
        except sqlite3.Error as exc:
            raise StudentAccountStoreError(
                "Die Schülerkonten konnten nicht initialisiert werden."
            ) from exc

    def _list_accounts_sync(self) -> list[StudentAccount]:
        try:
            with self._connect() as connection:
                self._create_schema(connection)
                rows = connection.execute(
                    """
                    SELECT *
                    FROM student_accounts
                    ORDER BY created_at DESC, label COLLATE NOCASE
                    """
                ).fetchall()
                return [self._account_from_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StudentAccountStoreError(
                "Die Schülerkonten konnten nicht geladen werden."
            ) from exc

    def _create_account_sync(
        self,
        normalized_label: str,
    ) -> IssuedStudentAccessCode:
        with self._write_lock:
            try:
                with self._connect() as connection:
                    self._create_schema(connection)
                    account_id = str(uuid4())
                    timestamp = datetime.now(timezone.utc).isoformat()
                    access_code, code_digest = self._unique_code(
                        connection
                    )
                    connection.execute(
                        """
                        INSERT INTO student_accounts (
                            account_id,
                            label,
                            code_digest,
                            access_version,
                            created_at,
                            updated_at,
                            disabled_at,
                            last_login_at
                        )
                        VALUES (?, ?, ?, 1, ?, ?, NULL, NULL)
                        """,
                        (
                            account_id,
                            normalized_label,
                            code_digest,
                            timestamp,
                            timestamp,
                        ),
                    )
                    account = self._load_account(connection, account_id)
                    assert account is not None
                    return IssuedStudentAccessCode(account, access_code)
            except sqlite3.IntegrityError as exc:
                if "student_accounts.label" in str(exc):
                    raise StudentAccountConflictError(
                        "Diese Kontobezeichnung ist bereits vergeben."
                    ) from exc
                raise StudentAccountStoreError(
                    "Das Schülerkonto konnte nicht erstellt werden."
                ) from exc
            except sqlite3.Error as exc:
                raise StudentAccountStoreError(
                    "Das Schülerkonto konnte nicht erstellt werden."
                ) from exc

    def _issue_new_code_sync(
        self,
        account_id: str,
    ) -> IssuedStudentAccessCode:
        with self._write_lock:
            try:
                with self._connect() as connection:
                    self._create_schema(connection)
                    account = self._load_account(connection, account_id)

                    if account is None:
                        raise StudentAccountNotFoundError(
                            "Das Schülerkonto wurde nicht gefunden."
                        )

                    access_code, code_digest = self._unique_code(connection)
                    connection.execute(
                        """
                        UPDATE student_accounts
                        SET code_digest = ?,
                            access_version = access_version + 1,
                            updated_at = ?
                        WHERE account_id = ?
                        """,
                        (
                            code_digest,
                            datetime.now(timezone.utc).isoformat(),
                            account_id,
                        ),
                    )
                    updated = self._load_account(connection, account_id)
                    assert updated is not None
                    return IssuedStudentAccessCode(updated, access_code)
            except StudentAccountNotFoundError:
                raise
            except sqlite3.Error as exc:
                raise StudentAccountStoreError(
                    "Der Zugangscode konnte nicht erneuert werden."
                ) from exc

    def _set_account_active_sync(
        self,
        account_id: str,
        active: bool,
    ) -> StudentAccount:
        with self._write_lock:
            try:
                with self._connect() as connection:
                    self._create_schema(connection)
                    if self._load_account(connection, account_id) is None:
                        raise StudentAccountNotFoundError(
                            "Das Schülerkonto wurde nicht gefunden."
                        )

                    timestamp = datetime.now(timezone.utc).isoformat()
                    connection.execute(
                        """
                        UPDATE student_accounts
                        SET disabled_at = ?,
                            access_version = access_version + ?,
                            updated_at = ?
                        WHERE account_id = ?
                        """,
                        (
                            None if active else timestamp,
                            0 if active else 1,
                            timestamp,
                            account_id,
                        ),
                    )
                    updated = self._load_account(connection, account_id)
                    assert updated is not None
                    return updated
            except StudentAccountNotFoundError:
                raise
            except sqlite3.Error as exc:
                raise StudentAccountStoreError(
                    "Der Kontostatus konnte nicht geändert werden."
                ) from exc

    def _delete_account_sync(self, account_id: str) -> None:
        with self._write_lock:
            try:
                with self._connect() as connection:
                    self._create_schema(connection)
                    cursor = connection.execute(
                        "DELETE FROM student_accounts WHERE account_id = ?",
                        (account_id,),
                    )

                    if cursor.rowcount == 0:
                        raise StudentAccountNotFoundError(
                            "Das Schülerkonto wurde nicht gefunden."
                        )
            except StudentAccountNotFoundError:
                raise
            except sqlite3.Error as exc:
                raise StudentAccountStoreError(
                    "Das Schülerkonto konnte nicht gelöscht werden."
                ) from exc

    def _authenticate_code_sync(
        self,
        access_code: str,
    ) -> StudentAccount | None:
        digest = self._code_digest(access_code)

        with self._write_lock:
            try:
                with self._connect() as connection:
                    self._create_schema(connection)
                    row = connection.execute(
                        """
                        SELECT *
                        FROM student_accounts
                        WHERE code_digest = ? AND disabled_at IS NULL
                        """,
                        (digest,),
                    ).fetchone()

                    if row is None or not hmac.compare_digest(
                        row["code_digest"], digest
                    ):
                        return None

                    timestamp = datetime.now(timezone.utc).isoformat()
                    connection.execute(
                        """
                        UPDATE student_accounts
                        SET last_login_at = ?
                        WHERE account_id = ?
                        """,
                        (timestamp, row["account_id"]),
                    )
                    return self._load_account(
                        connection,
                        row["account_id"],
                    )
            except sqlite3.Error as exc:
                raise StudentAccountStoreError(
                    "Der Schülerzugang konnte nicht geprüft werden."
                ) from exc

    def _get_active_account_sync(
        self,
        account_id: str,
    ) -> StudentAccount | None:
        try:
            with self._connect() as connection:
                self._create_schema(connection)
                row = connection.execute(
                    """
                    SELECT *
                    FROM student_accounts
                    WHERE account_id = ? AND disabled_at IS NULL
                    """,
                    (account_id,),
                ).fetchone()
                return self._account_from_row(row) if row is not None else None
        except sqlite3.Error as exc:
            raise StudentAccountStoreError(
                "Das Schülerkonto konnte nicht geprüft werden."
            ) from exc

    def _unique_code(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[str, str]:
        for _ in range(MAX_CODE_GENERATION_ATTEMPTS):
            access_code = self._normalize_access_code(self._code_factory())

            if access_code is None:
                raise StudentAccessCodeGenerationError(
                    "Die Codeerzeugung lieferte keinen sechsstelligen Code."
                )

            digest = self._code_digest(access_code)
            known = connection.execute(
                "SELECT 1 FROM student_accounts WHERE code_digest = ?",
                (digest,),
            ).fetchone()

            if known is None:
                return access_code, digest

        raise StudentAccessCodeGenerationError(
            "Es konnte kein eindeutiger sechsstelliger Code erzeugt werden."
        )

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS student_accounts (
                account_id TEXT PRIMARY KEY,
                label TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK (
                    length(trim(label)) BETWEEN 1 AND 80
                ),
                code_digest TEXT NOT NULL UNIQUE,
                access_version INTEGER NOT NULL DEFAULT 1 CHECK (
                    access_version >= 1
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                disabled_at TEXT,
                last_login_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_student_accounts_status
            ON student_accounts (disabled_at, created_at DESC);
            """
        )
        StudentAccountStore._migrate_access_version(connection)

    @staticmethod
    def _migrate_access_version(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(student_accounts)"
            ).fetchall()
        }

        if "access_version" not in columns:
            connection.execute(
                """
                ALTER TABLE student_accounts
                ADD COLUMN access_version INTEGER NOT NULL DEFAULT 1
                """
            )

    @staticmethod
    def _load_account(
        connection: sqlite3.Connection,
        account_id: str,
    ) -> StudentAccount | None:
        row = connection.execute(
            "SELECT * FROM student_accounts WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        return (
            StudentAccountStore._account_from_row(row)
            if row is not None
            else None
        )

    @staticmethod
    def _account_from_row(row: sqlite3.Row) -> StudentAccount:
        return StudentAccount(
            account_id=row["account_id"],
            label=row["label"],
            access_version=int(row["access_version"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            disabled_at=(
                datetime.fromisoformat(row["disabled_at"])
                if row["disabled_at"]
                else None
            ),
            last_login_at=(
                datetime.fromisoformat(row["last_login_at"])
                if row["last_login_at"]
                else None
            ),
        )
