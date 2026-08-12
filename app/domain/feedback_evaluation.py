from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime


MANUAL_EVALUATION_TYPE = "manual"
MAX_META_JUSTIFICATION_CHARS = 2000
META_EVALUATION_SCORE_OPTIONS = (
    (0, "nicht erfüllt"),
    (1, "teilweise erfüllt"),
    (2, "überwiegend erfüllt"),
    (3, "erfüllt"),
)


@dataclass(frozen=True)
class MetaEvaluationCriterion:
    """Ein versioniertes Qualitätskriterium der Meta-Ebene."""

    key: str
    title: str
    question: str


@dataclass(frozen=True)
class FeedbackEvaluationRating:
    """Gespeicherte Bewertung eines einzelnen Qualitätskriteriums."""

    criterion_key: str
    position: int
    criterion_title: str
    criterion_question: str
    score: int
    rating_label: str
    justification: str


@dataclass(frozen=True)
class MetaEvaluationRubric:
    """Unveränderlicher Bewertungsbogen für vergleichbare Läufe."""

    version: str
    title: str
    criteria: tuple[MetaEvaluationCriterion, ...]

    def build_ratings(
        self,
        *,
        scores: Mapping[str, int],
        justifications: Mapping[str, str],
    ) -> tuple[FeedbackEvaluationRating, ...]:
        expected_keys = {
            criterion.key for criterion in self.criteria
        }

        if set(scores) != expected_keys:
            raise ValueError(
                "Für jedes Qualitätskriterium muss genau eine "
                "Bewertungsstufe gewählt werden."
            )

        if set(justifications) != expected_keys:
            raise ValueError(
                "Für jedes Qualitätskriterium ist genau eine "
                "Begründung erforderlich."
            )

        score_labels = dict(META_EVALUATION_SCORE_OPTIONS)
        ratings: list[FeedbackEvaluationRating] = []

        for position, criterion in enumerate(self.criteria):
            score = scores[criterion.key]
            justification_value = justifications[criterion.key]

            if type(score) is not int or score not in score_labels:
                raise ValueError(
                    "Die Bewertungsstufen müssen zwischen 0 und 3 liegen."
                )

            if not isinstance(justification_value, str):
                raise ValueError(
                    "Die Begründungen müssen als Text angegeben werden."
                )

            justification = justification_value.strip()

            if not justification:
                raise ValueError(
                    "Zu jedem Qualitätskriterium ist eine kurze "
                    "Begründung erforderlich."
                )

            if len(justification) > MAX_META_JUSTIFICATION_CHARS:
                raise ValueError(
                    "Eine Begründung darf höchstens "
                    f"{MAX_META_JUSTIFICATION_CHARS} Zeichen enthalten."
                )

            ratings.append(
                FeedbackEvaluationRating(
                    criterion_key=criterion.key,
                    position=position,
                    criterion_title=criterion.title,
                    criterion_question=criterion.question,
                    score=score,
                    rating_label=score_labels[score],
                    justification=justification,
                )
            )

        return tuple(ratings)


MANUAL_META_EVALUATION_RUBRIC = MetaEvaluationRubric(
    version="meta-feedback-v1",
    title="Qualität des KI-Schreibfeedbacks",
    criteria=(
        MetaEvaluationCriterion(
            key="factual_correctness",
            title="Fachliche Korrektheit",
            question=(
                "Bewertet das Feedback Sprache, Inhalt, Aufbau und "
                "formale Anforderungen zutreffend, ohne relevante "
                "Probleme zu übersehen oder falsche Beanstandungen "
                "zu erfinden?"
            ),
        ),
        MetaEvaluationCriterion(
            key="transparency_reasoning",
            title="Transparenz und Begründung",
            question=(
                "Sind zentrale Urteile anhand konkreter Textstellen "
                "oder eindeutig fehlender Bestandteile nachvollziehbar "
                "begründet?"
            ),
        ),
        MetaEvaluationCriterion(
            key="audience_context_fit",
            title="Adressaten- und Kontextpassung",
            question=(
                "Passt das Feedback zur Aufgabe, Textsorte und "
                "Jahrgangsstufe, ist es verständlich formuliert und "
                "priorisiert es die wichtigsten Punkte?"
            ),
        ),
        MetaEvaluationCriterion(
            key="action_learning_activation",
            title="Handlungsorientierung und Lernaktivierung",
            question=(
                "Enthält das Feedback konkrete, selbstständig "
                "umsetzbare nächste Schritte und regt es zur "
                "Überarbeitung und Selbstkontrolle an, ohne eine "
                "fertige Neufassung vorzugeben?"
            ),
        ),
    ),
)


@dataclass(frozen=True)
class StoredFeedbackEvaluation:
    """Eigenständige manuelle oder automatische Meta-Bewertung."""

    evaluation_id: str
    feedback_run_id: str
    created_at: datetime
    evaluation_type: str
    rubric_version: str
    ratings: tuple[FeedbackEvaluationRating, ...]
    evaluator_provider: str | None
    evaluator_model: str | None
    duration_ms: int | None
    queue_duration_ms: float | None
    execution_duration_ms: float | None
    provider_request_id: str | None

    @property
    def type_label(self) -> str:
        if self.evaluation_type == MANUAL_EVALUATION_TYPE:
            return "Manuelle Bewertung"

        return "Automatische Bewertung"


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
    evaluations: tuple[StoredFeedbackEvaluation, ...]

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

    @property
    def manual_evaluation_count(self) -> int:
        return sum(
            evaluation.evaluation_type == MANUAL_EVALUATION_TYPE
            for evaluation in self.evaluations
        )
