from __future__ import annotations

from collections.abc import Iterable
from time import perf_counter

from app.domain.criterion_status import CRITERION_STATUS_LABELS
from app.domain.rubric import FeedbackTask, Rubric, RubricCriterion
from app.llm.base import LLMProvider
from app.services.rubric_feedback_service import (
    EVIDENCE_VALIDATION_VERSION,
    RUBRIC_FEEDBACK_PROMPT_VERSION,
    UNVERIFIED_CRITERION_FEEDBACK,
    UNVERIFIED_CRITERION_NEXT_STEP,
    CriterionFeedbackResult,
    RubricFeedbackError,
    RubricFeedbackResult,
    RubricFeedbackService,
)
from app.services.student_feedback_sections import (
    unverified_student_feedback_sections,
)


CRITERION_WISE_FEEDBACK_MODE = "rubric_feedback_criterion_wise"
CRITERION_WISE_FEEDBACK_LABEL = "Kriterienweise Einzelanalyse"
CRITERION_WISE_PIPELINE_VERSION = (
    "criterion-wise-rubric-feedback-v2-evidence-repair"
)
CRITERION_WISE_OVERALL_FEEDBACK = (
    "Die Rückmeldungen wurden für jedes Kriterium getrennt erzeugt und "
    "anschließend technisch zusammengeführt. Bearbeite zuerst die konkret "
    "genannten Überarbeitungsschritte."
)
CRITERION_WISE_PARTIAL_OVERALL_FEEDBACK = (
    "Mindestens ein getrennter Kriterienaufruf war nicht zuverlässig "
    "auswertbar. Nutze die übrigen belegten Rückmeldungen und prüfe die als "
    "„Nicht beurteilbar“ markierten Kriterien zusätzlich selbst."
)
CRITERION_REFRESH_OVERALL_FEEDBACK = (
    "Mindestens ein Einzelfeedback wurde anschließend gezielt neu erzeugt. "
    "Die jeweils sichtbaren Kriterienkarten enthalten den aktuellen Stand; "
    "die frühere Gesamtzusammenfassung wurde deshalb neutral ersetzt."
)


class CriterionWiseRubricFeedbackService:
    """Erzeugt nacheinander genau einen Modellaufruf je Kriterium."""

    def __init__(
        self,
        *,
        providers: dict[str, LLMProvider],
        max_input_chars: int,
    ) -> None:
        self.providers = providers
        self.max_input_chars = max_input_chars
        self._single_criterion_service = RubricFeedbackService(
            providers=providers,
            max_input_chars=max_input_chars,
        )

    async def analyze_criterion(
        self,
        *,
        student_text: str,
        task: FeedbackTask,
        criterion_id: str,
        original_text: str = "",
        provider_key: str,
        provider_override: LLMProvider | None = None,
    ) -> RubricFeedbackResult:
        """Analysiert genau ein vorhandenes Kriterium in einem Aufruf."""

        cleaned_text = student_text.strip()

        if not cleaned_text:
            raise ValueError("Bitte gib einen Text ein.")
        if len(cleaned_text) > self.max_input_chars:
            raise ValueError(
                "Der Text ist zu lang. Erlaubt sind maximal "
                f"{self.max_input_chars} Zeichen."
            )

        criterion = next(
            (
                item
                for item in task.rubric.criteria
                if item.criterion_id == criterion_id
            ),
            None,
        )

        if criterion is None:
            raise ValueError(
                "Das ausgewählte Feedback-Kriterium ist nicht mehr "
                "verfügbar."
            )

        provider = provider_override or self.providers.get(provider_key)

        if provider is None:
            raise ValueError(
                "Der ausgewählte Modellanbieter ist nicht bekannt."
            )

        return await self._single_criterion_service.analyze_text(
            student_text=cleaned_text,
            task=self._single_criterion_task(task, criterion),
            original_text=original_text,
            provider_key=provider_key,
            provider_override=provider,
        )

    async def analyze_text(
        self,
        *,
        student_text: str,
        task: FeedbackTask,
        original_text: str = "",
        provider_key: str,
        provider_override: LLMProvider | None = None,
    ) -> RubricFeedbackResult:
        cleaned_text = student_text.strip()

        if not cleaned_text:
            raise ValueError("Bitte gib einen Text ein.")
        if len(cleaned_text) > self.max_input_chars:
            raise ValueError(
                "Der Text ist zu lang. Erlaubt sind maximal "
                f"{self.max_input_chars} Zeichen."
            )
        if not task.rubric.criteria:
            raise ValueError(
                "Die ausgewählte Feedback-Vorlage enthält keine Kriterien."
            )

        provider = provider_override or self.providers.get(provider_key)

        if provider is None:
            raise ValueError(
                "Der ausgewählte Modellanbieter ist nicht bekannt."
            )

        total_started_at = perf_counter()
        results: list[CriterionFeedbackResult] = []
        evidence_warnings: list[str] = []
        criterion_durations_ms: list[int] = []
        request_ids: list[str | None] = []
        successful_results: list[RubricFeedbackResult] = []

        for index, criterion in enumerate(
            task.rubric.criteria,
            start=1,
        ):
            criterion_started_at = perf_counter()

            try:
                single_result = (
                    await self._single_criterion_service.analyze_text(
                        student_text=cleaned_text,
                        task=self._single_criterion_task(task, criterion),
                        original_text=original_text,
                        provider_key=provider_key,
                        provider_override=provider,
                    )
                )
            except RubricFeedbackError as exc:
                criterion_durations_ms.append(
                    self._elapsed_ms(criterion_started_at)
                )
                request_ids.append(None)
                results.append(self._unverified_result(criterion))
                evidence_warnings.append(
                    self._warning(
                        index,
                        criterion,
                        "Der getrennte Modellaufruf war strukturell "
                        f"nicht auswertbar: {exc}",
                    )
                )
                continue

            criterion_durations_ms.append(single_result.duration_ms)
            request_ids.append(single_result.provider_request_id)
            successful_results.append(single_result)
            criterion_result = single_result.criteria_feedback[0]
            results.append(criterion_result)

            for warning in single_result.evidence_warnings:
                details = warning.partition(": ")[2] or warning
                evidence_warnings.append(
                    self._warning(index, criterion, details)
                )

        last_success = successful_results[-1] if successful_results else None
        provider_name = (
            last_success.provider
            if last_success is not None
            else getattr(provider, "provider_name", provider_key)
        )
        model_name = (
            last_success.model
            if last_success is not None
            else getattr(provider, "model_name", "Unbekanntes Modell")
        )
        has_unassessable_criterion = any(
            item.status == "not_assessable"
            for item in results
        )

        return RubricFeedbackResult(
            provider=provider_name,
            model=model_name,
            task_id=task.task_id,
            task_title=task.title,
            rubric_title=task.rubric.title,
            criteria_feedback=tuple(results),
            overall_feedback=(
                CRITERION_WISE_PARTIAL_OVERALL_FEEDBACK
                if evidence_warnings or has_unassessable_criterion
                else CRITERION_WISE_OVERALL_FEEDBACK
            ),
            duration_ms=self._elapsed_ms(total_started_at),
            queue_duration_ms=self._sum_optional(
                item.queue_duration_ms for item in successful_results
            ),
            execution_duration_ms=self._sum_optional(
                item.execution_duration_ms for item in successful_results
            ),
            provider_request_id=(
                last_success.provider_request_id
                if last_success is not None
                else None
            ),
            worker_id=(
                last_success.worker_id
                if last_success is not None
                else None
            ),
            reasoning_effort=next(
                (
                    item.reasoning_effort
                    for item in successful_results
                    if item.reasoning_effort
                ),
                None,
            ),
            evidence_warnings=tuple(evidence_warnings),
            pipeline_mode=CRITERION_WISE_FEEDBACK_MODE,
            pipeline_label=CRITERION_WISE_FEEDBACK_LABEL,
            prompt_version=CRITERION_WISE_PIPELINE_VERSION,
            evidence_validation_version=EVIDENCE_VALIDATION_VERSION,
            criterion_prompt_version=RUBRIC_FEEDBACK_PROMPT_VERSION,
            criterion_request_count=len(task.rubric.criteria),
            criterion_request_durations_ms=tuple(
                criterion_durations_ms
            ),
            criterion_provider_request_ids=tuple(request_ids),
            evidence_repair_attempts=tuple(
                attempt
                for item in successful_results
                for attempt in item.evidence_repair_attempts
            ),
        )

    @staticmethod
    def _single_criterion_task(
        task: FeedbackTask,
        criterion: RubricCriterion,
    ) -> FeedbackTask:
        return FeedbackTask(
            task_id=task.task_id,
            title=task.title,
            subject=task.subject,
            grade_level=task.grade_level,
            instructions=task.instructions,
            material=task.material,
            rubric=Rubric(
                rubric_id=task.rubric.rubric_id,
                title=task.rubric.title,
                criteria=(criterion,),
            ),
            created_at=task.created_at,
            updated_at=task.updated_at,
            archived_at=task.archived_at,
        )

    @staticmethod
    def _unverified_result(
        criterion: RubricCriterion,
    ) -> CriterionFeedbackResult:
        return CriterionFeedbackResult(
            criterion_id=criterion.criterion_id,
            criterion_title=(
                criterion.title
                or f"Kriterium {criterion.position + 1}"
            ),
            criterion_text=criterion.text,
            status="not_assessable",
            status_label=CRITERION_STATUS_LABELS["not_assessable"],
            feedback=UNVERIFIED_CRITERION_FEEDBACK,
            next_step=UNVERIFIED_CRITERION_NEXT_STEP,
            evidence_quotes=(),
            evidence_verified=False,
            student_feedback_sections=(
                unverified_student_feedback_sections(
                    explanation=UNVERIFIED_CRITERION_FEEDBACK,
                    next_step=UNVERIFIED_CRITERION_NEXT_STEP,
                )
            ),
        )

    @staticmethod
    def _warning(
        index: int,
        criterion: RubricCriterion,
        details: str,
    ) -> str:
        title = criterion.title or f"Kriterium {index}"
        return f"K{index} – {title}: {details}"

    @staticmethod
    def _sum_optional(
        values: Iterable[float | None],
    ) -> float | None:
        available = [value for value in values if value is not None]
        return sum(available) if available else None

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return int((perf_counter() - started_at) * 1000)
