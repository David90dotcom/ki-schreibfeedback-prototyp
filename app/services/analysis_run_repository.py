from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from app.domain.metrics import AnalysisRunRecord


@runtime_checkable
class AnalysisRunRepository(Protocol):
    """
    Providerunabhängige Speicherschnittstelle für Analyseläufe.

    Mögliche Implementierungen:

    - lokale SQLite-Datenbank,
    - selbst gehostetes PostgreSQL,
    - verwaltetes PostgreSQL,
    - temporärer In-Memory-Speicher für Tests.
    """

    async def initialize(self) -> None:
        """Bereitet den Speicher für die Verwendung vor."""
        ...

    async def save(
        self,
        record: AnalysisRunRecord,
    ) -> None:
        """Speichert einen technischen Analyselauf."""
        ...

    async def get(
        self,
        run_id: UUID | str,
    ) -> AnalysisRunRecord | None:
        """Lädt einen einzelnen Analyselauf anhand seiner ID."""
        ...

    async def list_runs(
        self,
        *,
        limit: int = 100,
        provider_id: str | None = None,
        model_id: str | None = None,
        success: bool | None = None,
    ) -> list[AnalysisRunRecord]:
        """Lädt gefilterte Analyseläufe."""
        ...