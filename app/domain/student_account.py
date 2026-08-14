from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


STUDENT_ACCESS_CODE_LENGTH = 6
MAX_STUDENT_ACCOUNT_LABEL_CHARS = 80


@dataclass(frozen=True)
class StudentAccount:
    """Pseudonymes Konto für die reduzierte Schüleransicht."""

    account_id: str
    label: str
    access_version: int
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None = None
    last_login_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.disabled_at is None


@dataclass(frozen=True)
class IssuedStudentAccessCode:
    """Ein nur unmittelbar nach der Ausgabe sichtbarer Zugangscode."""

    account: StudentAccount
    access_code: str
