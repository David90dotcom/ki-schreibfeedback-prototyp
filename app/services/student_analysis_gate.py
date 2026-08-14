from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator


class StudentAnalysisInProgressError(RuntimeError):
    """Für dieses Konto läuft bereits eine Feedbackanalyse."""


class StudentAnalysisGate:
    """Verhindert parallele kostenpflichtige Läufe desselben Kontos."""

    def __init__(self) -> None:
        self._active_account_ids: set[str] = set()
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def reserve(self, account_id: str) -> AsyncIterator[None]:
        async with self._lock:
            if account_id in self._active_account_ids:
                raise StudentAnalysisInProgressError(
                    "Für dieses Schülerkonto wird bereits ein Feedback "
                    "erstellt. Bitte warte auf das laufende Ergebnis."
                )

            self._active_account_ids.add(account_id)

        try:
            yield
        finally:
            async with self._lock:
                self._active_account_ids.discard(account_id)
