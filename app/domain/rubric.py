from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class FeedbackTaskDraft:
    """Validierbare Eingabedaten für eine neue Aufgabe."""

    title: str
    subject: str
    grade_level: str
    instructions: str
    material: str
    rubric_title: str
    criteria: tuple[str, ...]
    criterion_titles: tuple[str, ...] = ()


@dataclass(frozen=True)
class RubricCriterion:
    """Ein geordnetes Kriterium für das spätere Feedback."""

    criterion_id: str
    text: str
    position: int
    title: str = ""


@dataclass(frozen=True)
class Rubric:
    """Eine Feedback-Vorlage mit ihren Einzelkriterien."""

    rubric_id: str
    title: str
    criteria: tuple[RubricCriterion, ...]


@dataclass(frozen=True)
class FeedbackTask:
    """Eine Aufgabe mit genau einer zugehörigen Feedback-Vorlage."""

    task_id: str
    title: str
    subject: str
    grade_level: str
    instructions: str
    material: str
    rubric: Rubric
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None

    def snapshot(self) -> dict[str, object]:
        """Erzeugt eine unveränderlich speicherbare Aufgabenansicht."""

        return {
            "task_id": self.task_id,
            "title": self.title,
            "subject": self.subject,
            "grade_level": self.grade_level,
            "instructions": self.instructions,
            "material": self.material,
            "rubric": {
                "rubric_id": self.rubric.rubric_id,
                "title": self.rubric.title,
                "criteria": [
                    {
                        "criterion_id": criterion.criterion_id,
                        "title": criterion.title,
                        "text": criterion.text,
                        "position": criterion.position,
                    }
                    for criterion in self.rubric.criteria
                ],
            },
        }


@dataclass(frozen=True)
class TaskDeleteResult:
    """Ergebnis eines sicheren Löschvorgangs."""

    task_id: str
    action: str

    @property
    def archived(self) -> bool:
        return self.action == "archived"
