from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from app.domain.feedback_evaluation import (
    AUTOMATIC_EVALUATION_TYPE,
    MANUAL_EVALUATION_TYPE,
    MANUAL_META_EVALUATION_RUBRIC,
    MAX_EVALUATION_NAME_CHARS,
    StoredFeedbackEvaluation,
    StoredFeedbackRun,
)


FEEDBACK_EVALUATION_EXPORT_FORMAT = (
    "ki-schreibfeedback-meta-evaluations"
)
FEEDBACK_EVALUATION_EXPORT_VERSION = 1
MAX_FEEDBACK_EVALUATION_IMPORT_BYTES = 16 * 1024 * 1024
MAX_FEEDBACK_EVALUATION_IMPORT_RUNS = 1000
MAX_EVALUATIONS_PER_RUN = 1000


class FeedbackEvaluationExchangeError(ValueError):
    """Der Meta-Bewertungs-Import besitzt kein gültiges Format."""


class FeedbackEvaluationExchangeService:
    """Exportiert Meta-Bewertungen portabel oder als Zahlentabelle."""

    @classmethod
    def export_json(
        cls,
        feedback_runs: Sequence[StoredFeedbackRun],
    ) -> bytes:
        runs = tuple(
            feedback_run
            for feedback_run in feedback_runs
            if feedback_run.evaluations
        )

        document = {
            "format": FEEDBACK_EVALUATION_EXPORT_FORMAT,
            "format_version": FEEDBACK_EVALUATION_EXPORT_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "feedback_run_count": len(runs),
            "evaluation_count": sum(
                len(feedback_run.evaluations)
                for feedback_run in runs
            ),
            "feedback_runs": [
                cls._serialize_feedback_run(feedback_run)
                for feedback_run in runs
            ],
        }
        content = (
            json.dumps(
                document,
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")

        if len(content) > MAX_FEEDBACK_EVALUATION_IMPORT_BYTES:
            raise FeedbackEvaluationExchangeError(
                "Der Meta-Bewertungs-Export ist größer als 16 MiB."
            )

        return content

    @classmethod
    def export_csv(
        cls,
        feedback_runs: Sequence[StoredFeedbackRun],
    ) -> bytes:
        rows = [
            cls._csv_row(feedback_run, evaluation)
            for feedback_run in feedback_runs
            for evaluation in feedback_run.evaluations
        ]

        fieldnames = (
            "feedback_run_id",
            "evaluation_id",
            "feedback_created_at",
            "evaluation_created_at",
            "task_title",
            "rubric_title",
            "feedback_provider",
            "feedback_model",
            "feedback_reasoning_effort",
            "feedback_duration_ms",
            "feedback_queue_duration_ms",
            "feedback_execution_duration_ms",
            "feedback_criterion_count",
            "evaluation_type",
            "evaluation_name",
            "evaluator_provider",
            "evaluator_model",
            "evaluation_duration_ms",
            "evaluation_queue_duration_ms",
            "evaluation_execution_duration_ms",
            "rubric_version",
            *(
                f"score_{criterion.key}"
                for criterion in MANUAL_META_EVALUATION_RUBRIC.criteria
            ),
            "average_score",
        )
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(
            stream,
            fieldnames=fieldnames,
            delimiter=";",
            lineterminator="\r\n",
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)

        # UTF-8 mit BOM wird von deutschsprachigem Excel direkt erkannt.
        return stream.getvalue().encode("utf-8-sig")

    @classmethod
    def parse_import(
        cls,
        content: bytes,
    ) -> tuple[StoredFeedbackRun, ...]:
        if not content:
            raise FeedbackEvaluationExchangeError(
                "Die ausgewählte Importdatei ist leer."
            )
        if len(content) > MAX_FEEDBACK_EVALUATION_IMPORT_BYTES:
            raise FeedbackEvaluationExchangeError(
                "Die Importdatei darf höchstens 16 MiB groß sein."
            )

        try:
            decoded = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise FeedbackEvaluationExchangeError(
                "Die Importdatei muss als UTF-8-JSON gespeichert sein."
            ) from exc

        try:
            document = json.loads(
                decoded,
                object_pairs_hook=cls._object_without_duplicate_keys,
                parse_constant=cls._reject_nonstandard_json_constant,
            )
        except FeedbackEvaluationExchangeError:
            raise
        except (json.JSONDecodeError, RecursionError) as exc:
            raise FeedbackEvaluationExchangeError(
                "Die ausgewählte Datei enthält kein gültiges JSON."
            ) from exc

        if not isinstance(document, Mapping):
            raise FeedbackEvaluationExchangeError(
                "Die Importdatei muss ein JSON-Objekt enthalten."
            )
        if document.get("format") != FEEDBACK_EVALUATION_EXPORT_FORMAT:
            raise FeedbackEvaluationExchangeError(
                "Die Datei ist kein Meta-Bewertungs-Export dieser Anwendung."
            )
        if (
            document.get("format_version")
            != FEEDBACK_EVALUATION_EXPORT_VERSION
        ):
            raise FeedbackEvaluationExchangeError(
                "Die Formatversion der Importdatei wird nicht unterstützt."
            )

        raw_runs = document.get("feedback_runs")

        if not isinstance(raw_runs, list) or not raw_runs:
            raise FeedbackEvaluationExchangeError(
                "Die Importdatei enthält keine Meta-Bewertungen."
            )
        if len(raw_runs) > MAX_FEEDBACK_EVALUATION_IMPORT_RUNS:
            raise FeedbackEvaluationExchangeError(
                "Eine Importdatei darf höchstens "
                f"{MAX_FEEDBACK_EVALUATION_IMPORT_RUNS} Feedbackläufe "
                "enthalten."
            )

        runs = tuple(
            cls._parse_feedback_run(raw_run, position)
            for position, raw_run in enumerate(raw_runs, start=1)
        )
        source_run_ids = [run.feedback_run_id for run in runs]

        if len(source_run_ids) != len(set(source_run_ids)):
            raise FeedbackEvaluationExchangeError(
                "Die Importdatei enthält Feedbacklauf-IDs doppelt."
            )

        return runs

    @classmethod
    def _serialize_feedback_run(
        cls,
        feedback_run: StoredFeedbackRun,
    ) -> dict[str, object]:
        return {
            "feedback_run_id": feedback_run.feedback_run_id,
            "task_id": feedback_run.task_id,
            "rubric_id": feedback_run.rubric_id,
            "created_at": feedback_run.created_at.isoformat(),
            "selected_for_evaluation_at": (
                feedback_run.selected_for_evaluation_at.isoformat()
            ),
            "provider": feedback_run.provider,
            "model": feedback_run.model,
            "reasoning_effort": feedback_run.reasoning_effort,
            "duration_ms": feedback_run.duration_ms,
            "queue_duration_ms": feedback_run.queue_duration_ms,
            "execution_duration_ms": feedback_run.execution_duration_ms,
            "provider_request_id": feedback_run.provider_request_id,
            "student_text": feedback_run.student_text,
            "original_text": feedback_run.original_text,
            "task_snapshot": feedback_run.task_snapshot,
            "feedback_payload": feedback_run.feedback_payload,
            "evaluations": [
                cls._serialize_evaluation(evaluation)
                for evaluation in feedback_run.evaluations
            ],
        }

    @staticmethod
    def _serialize_evaluation(
        evaluation: StoredFeedbackEvaluation,
    ) -> dict[str, object]:
        return {
            "evaluation_id": evaluation.evaluation_id,
            "created_at": evaluation.created_at.isoformat(),
            "evaluation_type": evaluation.evaluation_type,
            "evaluation_name": evaluation.evaluation_name,
            "rubric_version": evaluation.rubric_version,
            "evaluator_provider": evaluation.evaluator_provider,
            "evaluator_model": evaluation.evaluator_model,
            "evaluator_prompt_version": (
                evaluation.evaluator_prompt_version
            ),
            "source_evaluation_id": evaluation.source_evaluation_id,
            "duration_ms": evaluation.duration_ms,
            "queue_duration_ms": evaluation.queue_duration_ms,
            "execution_duration_ms": evaluation.execution_duration_ms,
            "provider_request_id": evaluation.provider_request_id,
            "ratings": [
                {
                    "criterion_key": rating.criterion_key,
                    "score": rating.score,
                    "justification": rating.justification,
                }
                for rating in evaluation.ratings
            ],
        }

    @classmethod
    def _parse_feedback_run(
        cls,
        value: object,
        position: int,
    ) -> StoredFeedbackRun:
        data = cls._mapping(
            value,
            f"Feedbacklauf {position}",
        )
        source_run_id = cls._required_text(
            data.get("feedback_run_id"),
            f"Feedbacklauf {position}: feedback_run_id",
            200,
        )
        task_snapshot = cls._mapping(
            data.get("task_snapshot"),
            f"Feedbacklauf {position}: task_snapshot",
        )
        feedback_payload = cls._mapping(
            data.get("feedback_payload"),
            f"Feedbacklauf {position}: feedback_payload",
        )
        raw_evaluations = data.get("evaluations")

        if not isinstance(raw_evaluations, list) or not raw_evaluations:
            raise FeedbackEvaluationExchangeError(
                f"Feedbacklauf {position} enthält keine Meta-Bewertung."
            )
        if len(raw_evaluations) > MAX_EVALUATIONS_PER_RUN:
            raise FeedbackEvaluationExchangeError(
                f"Feedbacklauf {position} enthält zu viele Bewertungen."
            )

        evaluations = tuple(
            cls._parse_evaluation(
                item,
                source_run_id=source_run_id,
                run_position=position,
                evaluation_position=evaluation_position,
            )
            for evaluation_position, item in enumerate(
                raw_evaluations,
                start=1,
            )
        )
        evaluation_by_id = {
            evaluation.evaluation_id: evaluation
            for evaluation in evaluations
        }

        if len(evaluation_by_id) != len(evaluations):
            raise FeedbackEvaluationExchangeError(
                f"Feedbacklauf {position} enthält Bewertungs-IDs doppelt."
            )

        for evaluation in evaluations:
            if evaluation.source_evaluation_id is None:
                continue

            source = evaluation_by_id.get(
                evaluation.source_evaluation_id
            )

            if (
                source is None
                or source.evaluation_type != AUTOMATIC_EVALUATION_TYPE
            ):
                raise FeedbackEvaluationExchangeError(
                    f"Feedbacklauf {position} enthält eine ungültige "
                    "Verknüpfung zu einer KI-Vorbewertung."
                )

        return StoredFeedbackRun(
            feedback_run_id=source_run_id,
            task_id=cls._required_text(
                data.get("task_id"),
                f"Feedbacklauf {position}: task_id",
                200,
            ),
            rubric_id=cls._required_text(
                data.get("rubric_id"),
                f"Feedbacklauf {position}: rubric_id",
                200,
            ),
            created_at=cls._datetime(
                data.get("created_at"),
                f"Feedbacklauf {position}: created_at",
            ),
            selected_for_evaluation_at=cls._datetime(
                data.get("selected_for_evaluation_at"),
                (
                    f"Feedbacklauf {position}: "
                    "selected_for_evaluation_at"
                ),
            ),
            provider=cls._required_text(
                data.get("provider"),
                f"Feedbacklauf {position}: provider",
                100,
            ),
            model=cls._required_text(
                data.get("model"),
                f"Feedbacklauf {position}: model",
                300,
            ),
            reasoning_effort=cls._optional_text(
                data.get("reasoning_effort"),
                f"Feedbacklauf {position}: reasoning_effort",
                100,
            ),
            duration_ms=cls._non_negative_int(
                data.get("duration_ms"),
                f"Feedbacklauf {position}: duration_ms",
            ),
            queue_duration_ms=cls._optional_non_negative_number(
                data.get("queue_duration_ms"),
                f"Feedbacklauf {position}: queue_duration_ms",
            ),
            execution_duration_ms=cls._optional_non_negative_number(
                data.get("execution_duration_ms"),
                f"Feedbacklauf {position}: execution_duration_ms",
            ),
            provider_request_id=cls._optional_text(
                data.get("provider_request_id"),
                f"Feedbacklauf {position}: provider_request_id",
                500,
            ),
            student_text=cls._required_text(
                data.get("student_text"),
                f"Feedbacklauf {position}: student_text",
                200_000,
            ),
            original_text=cls._optional_text(
                data.get("original_text"),
                f"Feedbacklauf {position}: original_text",
                200_000,
            ),
            task_snapshot=dict(task_snapshot),
            feedback_payload=dict(feedback_payload),
            evaluations=evaluations,
        )

    @classmethod
    def _parse_evaluation(
        cls,
        value: object,
        *,
        source_run_id: str,
        run_position: int,
        evaluation_position: int,
    ) -> StoredFeedbackEvaluation:
        label = (
            f"Feedbacklauf {run_position}, Bewertung "
            f"{evaluation_position}"
        )
        data = cls._mapping(value, label)
        evaluation_type = cls._required_text(
            data.get("evaluation_type"),
            f"{label}: evaluation_type",
            32,
        )

        if evaluation_type not in {
            MANUAL_EVALUATION_TYPE,
            AUTOMATIC_EVALUATION_TYPE,
        }:
            raise FeedbackEvaluationExchangeError(
                f"{label} besitzt einen ungültigen Bewertungstyp."
            )
        if data.get("rubric_version") != (
            MANUAL_META_EVALUATION_RUBRIC.version
        ):
            raise FeedbackEvaluationExchangeError(
                f"{label} verwendet einen nicht unterstützten "
                "Meta-Bewertungsbogen."
            )

        raw_ratings = data.get("ratings")

        if not isinstance(raw_ratings, list):
            raise FeedbackEvaluationExchangeError(
                f"{label} enthält keine gültigen Einzelbewertungen."
            )

        scores: dict[str, int] = {}
        justifications: dict[str, str] = {}

        for raw_rating in raw_ratings:
            rating = cls._mapping(raw_rating, f"{label}: rating")
            criterion_key = cls._required_text(
                rating.get("criterion_key"),
                f"{label}: criterion_key",
                100,
            )

            if criterion_key in scores:
                raise FeedbackEvaluationExchangeError(
                    f"{label} enthält ein Kriterium doppelt."
                )

            scores[criterion_key] = cls._score(
                rating.get("score"),
                f"{label}: score",
            )
            justifications[criterion_key] = cls._required_text(
                rating.get("justification"),
                f"{label}: justification",
                2000,
            )

        try:
            ratings = MANUAL_META_EVALUATION_RUBRIC.build_ratings(
                scores=scores,
                justifications=justifications,
            )
        except ValueError as exc:
            raise FeedbackEvaluationExchangeError(
                f"{label}: {exc}"
            ) from exc

        evaluator_provider = cls._optional_text(
            data.get("evaluator_provider"),
            f"{label}: evaluator_provider",
            100,
        )
        evaluator_model = cls._optional_text(
            data.get("evaluator_model"),
            f"{label}: evaluator_model",
            300,
        )
        evaluator_prompt_version = cls._optional_text(
            data.get("evaluator_prompt_version"),
            f"{label}: evaluator_prompt_version",
            200,
        )

        if evaluation_type == AUTOMATIC_EVALUATION_TYPE and not all(
            (
                evaluator_provider,
                evaluator_model,
                evaluator_prompt_version,
            )
        ):
            raise FeedbackEvaluationExchangeError(
                f"{label} enthält keine vollständige Modellangabe."
            )

        return StoredFeedbackEvaluation(
            evaluation_id=cls._required_text(
                data.get("evaluation_id"),
                f"{label}: evaluation_id",
                200,
            ),
            feedback_run_id=source_run_id,
            created_at=cls._datetime(
                data.get("created_at"),
                f"{label}: created_at",
            ),
            evaluation_type=evaluation_type,
            evaluation_name=cls._optional_text(
                data.get("evaluation_name"),
                f"{label}: evaluation_name",
                MAX_EVALUATION_NAME_CHARS,
            ),
            rubric_version=MANUAL_META_EVALUATION_RUBRIC.version,
            ratings=ratings,
            evaluator_provider=evaluator_provider,
            evaluator_model=evaluator_model,
            evaluator_prompt_version=evaluator_prompt_version,
            source_evaluation_id=cls._optional_text(
                data.get("source_evaluation_id"),
                f"{label}: source_evaluation_id",
                200,
            ),
            duration_ms=cls._optional_non_negative_int(
                data.get("duration_ms"),
                f"{label}: duration_ms",
            ),
            queue_duration_ms=cls._optional_non_negative_number(
                data.get("queue_duration_ms"),
                f"{label}: queue_duration_ms",
            ),
            execution_duration_ms=cls._optional_non_negative_number(
                data.get("execution_duration_ms"),
                f"{label}: execution_duration_ms",
            ),
            provider_request_id=cls._optional_text(
                data.get("provider_request_id"),
                f"{label}: provider_request_id",
                500,
            ),
        )

    @staticmethod
    def _csv_row(
        feedback_run: StoredFeedbackRun,
        evaluation: StoredFeedbackEvaluation,
    ) -> dict[str, object]:
        row: dict[str, object] = {
            "feedback_run_id": feedback_run.feedback_run_id,
            "evaluation_id": evaluation.evaluation_id,
            "feedback_created_at": feedback_run.created_at.isoformat(),
            "evaluation_created_at": evaluation.created_at.isoformat(),
            "task_title": FeedbackEvaluationExchangeService._csv_safe_text(
                feedback_run.task_title
            ),
            "rubric_title": FeedbackEvaluationExchangeService._csv_safe_text(
                feedback_run.rubric_title
            ),
            "feedback_provider": (
                FeedbackEvaluationExchangeService._csv_safe_text(
                    feedback_run.provider
                )
            ),
            "feedback_model": (
                FeedbackEvaluationExchangeService._csv_safe_text(
                    feedback_run.model
                )
            ),
            "feedback_reasoning_effort": (
                FeedbackEvaluationExchangeService._csv_safe_text(
                    feedback_run.reasoning_effort or ""
                )
            ),
            "feedback_duration_ms": feedback_run.duration_ms,
            "feedback_queue_duration_ms": (
                feedback_run.queue_duration_ms
                if feedback_run.queue_duration_ms is not None
                else ""
            ),
            "feedback_execution_duration_ms": (
                feedback_run.execution_duration_ms
                if feedback_run.execution_duration_ms is not None
                else ""
            ),
            "feedback_criterion_count": feedback_run.criterion_count,
            "evaluation_type": (
                FeedbackEvaluationExchangeService._csv_safe_text(
                    evaluation.evaluation_type
                )
            ),
            "evaluation_name": (
                FeedbackEvaluationExchangeService._csv_safe_text(
                    evaluation.evaluation_name or ""
                )
            ),
            "evaluator_provider": (
                FeedbackEvaluationExchangeService._csv_safe_text(
                    evaluation.evaluator_provider or ""
                )
            ),
            "evaluator_model": (
                FeedbackEvaluationExchangeService._csv_safe_text(
                    evaluation.evaluator_model or ""
                )
            ),
            "evaluation_duration_ms": (
                evaluation.duration_ms
                if evaluation.duration_ms is not None
                else ""
            ),
            "evaluation_queue_duration_ms": (
                evaluation.queue_duration_ms
                if evaluation.queue_duration_ms is not None
                else ""
            ),
            "evaluation_execution_duration_ms": (
                evaluation.execution_duration_ms
                if evaluation.execution_duration_ms is not None
                else ""
            ),
            "rubric_version": (
                FeedbackEvaluationExchangeService._csv_safe_text(
                    evaluation.rubric_version
                )
            ),
            "average_score": (
                round(evaluation.average_score, 3)
                if evaluation.average_score is not None
                else ""
            ),
        }
        row.update(
            {
                f"score_{rating.criterion_key}": rating.score
                for rating in evaluation.ratings
            }
        )
        return row

    @staticmethod
    def _object_without_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}

        for key, value in pairs:
            if key in result:
                raise FeedbackEvaluationExchangeError(
                    f'Das JSON-Feld "{key}" ist doppelt vorhanden.'
                )
            result[key] = value

        return result

    @staticmethod
    def _reject_nonstandard_json_constant(value: str) -> object:
        raise FeedbackEvaluationExchangeError(
            f'Der nicht standardisierte JSON-Wert "{value}" ist ungültig.'
        )

    @staticmethod
    def _csv_safe_text(value: str) -> str:
        if value.startswith(("=", "+", "-", "@", "\t", "\r")):
            return f"'{value}"
        return value

    @staticmethod
    def _mapping(value: object, label: str) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            raise FeedbackEvaluationExchangeError(
                f"{label} muss ein JSON-Objekt sein."
            )
        return value

    @staticmethod
    def _required_text(
        value: object,
        label: str,
        maximum_length: int,
    ) -> str:
        if not isinstance(value, str) or not value.strip():
            raise FeedbackEvaluationExchangeError(
                f"{label} muss Text enthalten."
            )

        normalized = value.strip()

        if len(normalized) > maximum_length:
            raise FeedbackEvaluationExchangeError(
                f"{label} ist zu lang."
            )

        return normalized

    @classmethod
    def _optional_text(
        cls,
        value: object,
        label: str,
        maximum_length: int,
    ) -> str | None:
        if value is None:
            return None
        if value == "":
            return None

        return cls._required_text(value, label, maximum_length)

    @staticmethod
    def _datetime(value: object, label: str) -> datetime:
        if not isinstance(value, str):
            raise FeedbackEvaluationExchangeError(
                f"{label} muss ein ISO-Zeitstempel sein."
            )

        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise FeedbackEvaluationExchangeError(
                f"{label} muss ein ISO-Zeitstempel sein."
            ) from exc

        if parsed.tzinfo is None:
            raise FeedbackEvaluationExchangeError(
                f"{label} muss eine Zeitzone enthalten."
            )

        return parsed

    @staticmethod
    def _non_negative_int(value: object, label: str) -> int:
        if type(value) is not int or value < 0:
            raise FeedbackEvaluationExchangeError(
                f"{label} muss eine nichtnegative Ganzzahl sein."
            )
        return value

    @classmethod
    def _optional_non_negative_int(
        cls,
        value: object,
        label: str,
    ) -> int | None:
        if value is None:
            return None
        return cls._non_negative_int(value, label)

    @staticmethod
    def _optional_non_negative_number(
        value: object,
        label: str,
    ) -> float | None:
        if value is None:
            return None
        if type(value) not in {int, float}:
            raise FeedbackEvaluationExchangeError(
                f"{label} muss eine nichtnegative Zahl sein."
            )

        number = float(value)

        if not math.isfinite(number) or number < 0:
            raise FeedbackEvaluationExchangeError(
                f"{label} muss eine nichtnegative Zahl sein."
            )

        return number

    @staticmethod
    def _score(value: object, label: str) -> int:
        if type(value) is not int or value not in {0, 1, 2, 3}:
            raise FeedbackEvaluationExchangeError(
                f"{label} muss zwischen 0 und 3 liegen."
            )
        return value
