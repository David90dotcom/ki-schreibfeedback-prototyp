from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class StoredFeedbackRun:
    """Für eine spätere Meta-Bewertung ausgewählter Feedbacklauf."""

    feedback_run_id: str
    task_id: str
    rubric_id: str
    created_at: datetime
    selected_for_evaluation_at: datetime
    provider: str
    model: str
    duration_ms: int
    queue_duration_ms: float | None
    execution_duration_ms: float | None
    provider_request_id: str | None
    student_text: str
    task_snapshot: dict[str, object]
    feedback_payload: dict[str, object]

    @property
    def task_title(self) -> str:
        value = self.task_snapshot.get("title")
        return value if isinstance(value, str) and value else "Ohne Titel"

    @property
    def rubric_title(self) -> str:
        rubric = self.task_snapshot.get("rubric")

        if not isinstance(rubric, dict):
            return "Feedback"

        value = rubric.get("title")
        return value if isinstance(value, str) and value else "Feedback"

    @property
    def criterion_count(self) -> int:
        criteria = self.feedback_payload.get("criteria")
        return len(criteria) if isinstance(criteria, list) else 0
