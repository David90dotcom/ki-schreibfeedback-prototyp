from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from app.domain.metrics import AnalysisRunRecord
from app.domain.model_catalog import (
    DomainModel,
    ModelParameters,
)


class CriterionStatus(str, Enum):
    """Mögliche Erfüllungsstände eines Feedbackkriteriums."""

    MET = "met"
    PARTIALLY_MET = "partially_met"
    NOT_MET = "not_met"
    NOT_ASSESSABLE = "not_assessable"


class EvidenceType(str, Enum):
    """Funktion eines Textbelegs innerhalb der Rückmeldung."""

    STRENGTH = "strength"
    REVISION_NEED = "revision_need"


class ComparisonExecution(str, Enum):
    """Ausführungsart eines Modellvergleichs."""

    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"


class TaskDefinition(DomainModel):
    """Schreibaufgabe, auf die sich das Feedback bezieht."""

    task_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    description: str | None = None


class FeedbackCriterion(DomainModel):
    """Ein bearbeitbares Kriterium für die Schreibanalyse."""

    criterion_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    enabled: bool = True
    order: int = Field(default=0, ge=0)


class Submission(DomainModel):
    """Anonymisierter Schülertext."""

    submission_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class AnalysisInput(DomainModel):
    """Vollständige fachliche Eingabe für eine Analyse."""

    task: TaskDefinition
    submission: Submission
    criteria: tuple[FeedbackCriterion, ...] = Field(
        min_length=1
    )

    @model_validator(mode="after")
    def validate_criteria(self) -> AnalysisInput:
        criterion_ids = [
            criterion.criterion_id
            for criterion in self.criteria
        ]

        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError(
                "criterion_id muss innerhalb einer Analyse eindeutig sein."
            )

        enabled_criteria = [
            criterion
            for criterion in self.criteria
            if criterion.enabled
        ]

        if not enabled_criteria:
            raise ValueError(
                "Mindestens ein Feedbackkriterium muss aktiviert sein."
            )

        return self


class AnalysisRequest(DomainModel):
    """API- und serviceübergreifender Analyseauftrag."""

    model_id: str = Field(min_length=1)
    analysis_input: AnalysisInput
    parameters: ModelParameters | None = None
    stream: bool = False


class CriterionEvidence(DomainModel):
    """Wörtlicher Beleg aus dem Schülertext."""

    quote: str = Field(min_length=1)
    type: EvidenceType


class CriterionFeedback(DomainModel):
    """Standardisierte Rückmeldung zu einem Kriterium."""

    criterion_id: str = Field(min_length=1)
    criterion_title: str = Field(min_length=1)
    status: CriterionStatus

    strengths: tuple[str, ...]
    revision_needs: tuple[str, ...]
    revision_advice: tuple[str, ...]
    evidence: tuple[CriterionEvidence, ...]

    @model_validator(mode="after")
    def validate_feedback_content(
        self,
    ) -> CriterionFeedback:
        if (
            not self.strengths
            and not self.revision_needs
            and not self.revision_advice
        ):
            raise ValueError(
                (
                    "Ein Kriterienergebnis benötigt mindestens eine "
                    "inhaltliche Rückmeldung."
                )
            )

        return self


class AnalysisPayload(DomainModel):
    """Providerunabhängige fachliche Modellantwort."""

    criteria_results: tuple[
        CriterionFeedback,
        ...
    ] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_results(
        self,
    ) -> AnalysisPayload:
        criterion_ids = [
            result.criterion_id
            for result in self.criteria_results
        ]

        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError(
                (
                    "Jede criterion_id darf in criteria_results "
                    "nur einmal vorkommen."
                )
            )

        return self


class AnalysisResult(DomainModel):
    """Vollständiges Ergebnis eines Analysevorgangs."""

    analysis_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(
            timezone.utc
        )
    )

    submission_id: str = Field(min_length=1)
    feedback: AnalysisPayload
    run_record: AnalysisRunRecord


class ComparisonProfile(DomainModel):
    """Wiederverwendbare Konfiguration eines Modellvergleichs."""

    profile_id: str = Field(min_length=1)
    title: str = Field(min_length=1)

    model_ids: tuple[str, ...] = Field(
        min_length=2
    )
    execution: ComparisonExecution = (
        ComparisonExecution.SEQUENTIAL
    )

    @model_validator(mode="after")
    def validate_models(
        self,
    ) -> ComparisonProfile:
        if len(self.model_ids) != len(
            set(self.model_ids)
        ):
            raise ValueError(
                (
                    "Ein Modell darf in einem Vergleichsprofil "
                    "nur einmal vorkommen."
                )
            )

        return self


class ComparisonRequest(DomainModel):
    """Auftrag für die Analyse mit mehreren Modellen."""

    analysis_input: AnalysisInput
    profile: ComparisonProfile


class ComparisonResult(DomainModel):
    """Zusammengehörige Ergebnisse eines Modellvergleichs."""

    comparison_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(
            timezone.utc
        )
    )

    submission_id: str = Field(min_length=1)
    execution: ComparisonExecution

    results: tuple[AnalysisResult, ...] = Field(
        min_length=2
    )

    @model_validator(mode="after")
    def validate_submission_ids(
        self,
    ) -> ComparisonResult:
        invalid_results = [
            result
            for result in self.results
            if result.submission_id != self.submission_id
        ]

        if invalid_results:
            raise ValueError(
                (
                    "Alle Vergleichsergebnisse müssen zum selben "
                    "Schülertext gehören."
                )
            )

        return self