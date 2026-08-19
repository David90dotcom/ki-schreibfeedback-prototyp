from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from time import perf_counter

from app.domain.criterion_status import CRITERION_STATUS_LABELS
from app.domain.rubric import FeedbackTask, RubricCriterion
from app.llm.base import LLMProvider, LLMResponse
from app.services.rubric_feedback_service import (
    CriterionFeedbackResult,
    RubricFeedbackError,
    RubricFeedbackResult,
    RubricFeedbackService,
    UNVERIFIED_CRITERION_FEEDBACK,
    UNVERIFIED_CRITERION_NEXT_STEP,
)
from app.services.student_feedback_sections import (
    StudentFeedbackSections,
    open_formulation_helps_for_criterion,
    unverified_student_feedback_sections,
)


TWO_PASS_FEEDBACK_MODE = "rubric_feedback_two_pass_experimental"
TWO_PASS_FEEDBACK_LABEL = "Experimentelles Zwei-Pass-Kriterienfeedback"
TWO_PASS_PIPELINE_VERSION = "grounded-two-pass-v3-four-part-feedback"
TWO_PASS_ANALYSIS_PROMPT_VERSION = "candidate-findings-v2"
TWO_PASS_REVIEW_PROMPT_VERSION = "restricted-review-v2"
TWO_PASS_EVIDENCE_VALIDATION_VERSION = (
    "typed-student-source-criterion-word-sequence-v2"
)
MAX_FINDINGS_PER_CRITERION = 3
MAX_STRENGTHS_PER_CRITERION = 1
MAX_IMPROVEMENTS_PER_CRITERION = 2
MAX_FINDING_TEXT_CHARS = 800
MAX_REVIEW_REASON_CHARS = 800
FINDING_ROLES = {"strength", "improvement"}
FINDING_KINDS = {
    "strength",
    "missing_requirement",
    "criterion_mismatch",
    "source_mismatch",
    "language_issue",
}
SOURCE_SCOPES = {
    "none",
    "task_material",
    "run_original_text",
}
REVIEW_VERDICTS = {"accept", "reject"}
TECHNICAL_STATUS_PATTERN = re.compile(
    r"(?<!\w)(?:met|mostly_met|partially_met|not_met|not_assessable)(?!\w)",
    flags=re.IGNORECASE,
)
TWO_PASS_PARTIAL_OVERALL_FEEDBACK = (
    "Behalte die belegten Stärken bei und beginne mit den genannten, "
    "in zwei Schritten geprüften Überarbeitungshinweisen."
)
TWO_PASS_STRENGTH_ONLY_OVERALL_FEEDBACK = (
    "Die Zweitprüfung hat keine sicher belegte Verbesserungsmöglichkeit "
    "übernommen. Behalte die genannten Stärken bei und kontrolliere "
    "deinen Text abschließend selbst."
)
TWO_PASS_EMPTY_OVERALL_FEEDBACK = (
    "In der experimentellen Zweitprüfung blieb kein ausreichend sicherer "
    "Befund übrig. Deshalb wurde bewusst kein inhaltliches Feedback "
    "übernommen."
)


@dataclass(frozen=True)
class CandidateFinding:
    finding_id: str
    criterion_reference: str
    role: str
    kind: str
    claim: str
    student_quote: str
    criterion_quote: str
    source_scope: str
    source_quote: str
    student_feedback: str
    next_step: str

    def review_payload(self) -> dict[str, str]:
        return {
            "finding_id": self.finding_id,
            "role": self.role,
            "kind": self.kind,
            "claim": self.claim,
            "student_quote": self.student_quote,
            "criterion_quote": self.criterion_quote,
            "source_scope": self.source_scope,
            "source_quote": self.source_quote,
            "student_feedback": self.student_feedback,
            "next_step": self.next_step,
        }


@dataclass(frozen=True)
class CandidateCriterion:
    criterion_reference: str
    findings: tuple[CandidateFinding, ...]


@dataclass(frozen=True)
class CandidateParseResult:
    criteria: tuple[CandidateCriterion, ...]
    raw_finding_count: int
    warnings: tuple[str, ...]

    @property
    def findings(self) -> tuple[CandidateFinding, ...]:
        return tuple(
            finding
            for criterion in self.criteria
            for finding in criterion.findings
        )


@dataclass(frozen=True)
class ReviewCriterion:
    criterion_reference: str
    status: str
    accepted_finding_ids: tuple[str, ...]


class TwoPassRubricFeedbackService:
    """Experimentelle Befund- und Prüfphase mit demselben Modell."""

    def __init__(
        self,
        *,
        providers: dict[str, LLMProvider],
        max_input_chars: int,
    ) -> None:
        self.providers = providers
        self.max_input_chars = max_input_chars

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
        cleaned_original_text = original_text.strip()

        self._validate_input(cleaned_text, task)
        provider = provider_override or self.providers.get(provider_key)

        if provider is None:
            raise ValueError(
                "Der ausgewählte Modellanbieter ist nicht bekannt."
            )

        total_started_at = perf_counter()
        analysis_started_at = perf_counter()
        analysis_response = await provider.generate(
            self._build_candidate_prompt(
                student_text=cleaned_text,
                task=task,
                original_text=cleaned_original_text,
            ),
            response_schema=self._build_candidate_schema(task),
            response_schema_name="rubric_candidate_findings",
        )
        analysis_duration_ms = self._elapsed_ms(analysis_started_at)
        candidate_result = self._parse_candidate_response(
            analysis_response.text,
            task=task,
            student_text=cleaned_text,
            original_text=cleaned_original_text,
            finish_reason=RubricFeedbackService._finish_reason(
                analysis_response.raw_metadata
            ),
        )

        if not candidate_result.findings:
            return self._safe_result(
                task=task,
                analysis_response=analysis_response,
                review_response=None,
                total_started_at=total_started_at,
                analysis_duration_ms=analysis_duration_ms,
                review_duration_ms=0,
                raw_finding_count=candidate_result.raw_finding_count,
                validated_candidate_count=0,
                pipeline_warnings=(
                    *candidate_result.warnings,
                    "Nach der technischen Belegprüfung blieb kein "
                    "Kandidatenbefund für die Zweitprüfung übrig.",
                ),
            )

        review_started_at = perf_counter()
        review_response = await provider.generate(
            self._build_review_prompt(
                student_text=cleaned_text,
                task=task,
                original_text=cleaned_original_text,
                candidate_result=candidate_result,
            ),
            response_schema=self._build_review_schema(
                task,
                candidate_result,
            ),
            response_schema_name="rubric_restricted_review",
        )
        review_duration_ms = self._elapsed_ms(review_started_at)
        self._validate_phase_identity(
            analysis_response,
            review_response,
        )

        try:
            review_criteria = self._parse_review_response(
                review_response.text,
                task=task,
                candidate_result=candidate_result,
                finish_reason=RubricFeedbackService._finish_reason(
                    review_response.raw_metadata
                ),
            )
        except RubricFeedbackError as exc:
            return self._safe_result(
                task=task,
                analysis_response=analysis_response,
                review_response=review_response,
                total_started_at=total_started_at,
                analysis_duration_ms=analysis_duration_ms,
                review_duration_ms=review_duration_ms,
                raw_finding_count=candidate_result.raw_finding_count,
                validated_candidate_count=len(
                    candidate_result.findings
                ),
                pipeline_warnings=(
                    *candidate_result.warnings,
                    "Die eingeschränkte Zweitprüfung war strukturell "
                    f"nicht auswertbar: {exc}",
                ),
            )

        return self._build_reviewed_result(
            task=task,
            candidate_result=candidate_result,
            review_criteria=review_criteria,
            analysis_response=analysis_response,
            review_response=review_response,
            total_started_at=total_started_at,
            analysis_duration_ms=analysis_duration_ms,
            review_duration_ms=review_duration_ms,
        )

    def _validate_input(
        self,
        student_text: str,
        task: FeedbackTask,
    ) -> None:
        if not student_text:
            raise ValueError("Bitte gib einen Text ein.")
        if len(student_text) > self.max_input_chars:
            raise ValueError(
                "Der Text ist zu lang. Erlaubt sind maximal "
                f"{self.max_input_chars} Zeichen."
            )
        if not task.rubric.criteria:
            raise ValueError(
                "Die ausgewählte Feedback-Vorlage enthält keine Kriterien."
            )

    @staticmethod
    def _build_candidate_prompt(
        *,
        student_text: str,
        task: FeedbackTask,
        original_text: str,
    ) -> str:
        criteria_by_reference = (
            RubricFeedbackService._criteria_by_reference(task)
        )
        analysis_input = {
            "task": {
                "title": task.title,
                "subject": task.subject,
                "grade_level": task.grade_level,
                "instructions": task.instructions,
                "material": task.material,
                "feedback": {
                    "title": task.rubric.title,
                    "criteria": [
                        {
                            "criterion_id": reference,
                            "title": criterion.title,
                            "text": criterion.text,
                        }
                        for reference, criterion
                        in criteria_by_reference.items()
                    ],
                },
            },
            "original_text_for_this_run": original_text or None,
            "student_text": student_text,
        }
        response_template = {
            "criteria": [
                {
                    "criterion_id": reference,
                    "findings": [
                        {
                            "role": "strength | improvement",
                            "kind": (
                                "strength | missing_requirement | "
                                "criterion_mismatch | source_mismatch | "
                                "language_issue"
                            ),
                            "claim": "technischer Befund",
                            "student_quote": (
                                "exakter Ausschnitt aus student_text"
                            ),
                            "criterion_quote": (
                                "exakter relevanter Kriterienausschnitt"
                            ),
                            "source_scope": (
                                "none | task_material | "
                                "run_original_text"
                            ),
                            "source_quote": (
                                "exakter Quellenausschnitt oder leer"
                            ),
                            "student_feedback": (
                                "beleggestützte schülergerechte Rückmeldung"
                            ),
                            "next_step": (
                                "ein sicherer konkreter Schritt oder leer"
                            ),
                        }
                    ],
                }
                for reference in criteria_by_reference
            ],
        }

        return f"""
Du führst die erste Phase eines experimentellen Zwei-Pass-Feedbackverfahrens
durch. Erzeuge noch kein fertiges Gesamtfeedback. Ermittle ausschließlich
prüfbare Kandidatenbefunde zu jedem vorgegebenen Kriterium. Ein zweiter
Modellaufruf darf diese Befunde später nur bestätigen oder verwerfen.

Trenne die Quellenrollen strikt:

- Nur student_text zeigt, was die Schülerin oder der Schüler geschrieben hat.
- task.instructions enthält den Arbeitsauftrag.
- task.feedback.criteria enthält Anforderungen und fachliche
  Erwartungspunkte, aber niemals bereits erbrachte Schülerleistungen.
- task.material und original_text_for_this_run sind fachliche Quellen, aber
  keine Bestandteile des Schülertexts.

Prüfe für jedes Kriterium den vollständigen student_text. Gib höchstens einen
belegten Stärkenbefund und höchstens zwei priorisierte Verbesserungsbefunde aus.
Erfinde weder eine Stärke noch ein Problem, nur um die Liste zu füllen. Wenn
nichts sicher belegt ist, bleibt findings leer. Vergib in dieser Phase noch
keinen Erfüllungsstatus.

Jeder Befund benötigt ein kurzes, unverändertes student_quote aus student_text.
Verwende keine Auslassungszeichen und korrigiere keine Schreibweise im Zitat.
Zusätzlich benötigt jeder Befund ein kurzes wörtliches criterion_quote aus
genau dem aktuell geprüften Kriterium. Es muss unmittelbar zeigen, warum der
Befund zu diesem Kriterium gehört. Wenn du diese Verbindung nicht sicher
herstellen kannst, gib den Befund nicht aus.

Ordne jeden Befund genau einer Art zu:

- strength: eine nachweisbare Stärke; role muss strength sein.
- missing_requirement: ein Bestandteil des Kriteriums fehlt nach Prüfung des
  vollständigen Schülertexts; criterion_quote muss die betreffende Anforderung
  wörtlich aus dem aktuellen Kriterium übernehmen.
- criterion_mismatch: eine Schüleraussage widerspricht einer im Kriterium
  ausdrücklich festgelegten fachlichen Angabe; criterion_quote ist Pflicht.
- source_mismatch: eine Schüleraussage widerspricht task.material oder
  original_text_for_this_run; source_scope und ein dort wörtlich vorkommendes
  source_quote sind Pflicht.
- language_issue: ein konkretes sprachliches Problem im student_quote; hierfür
  muss criterion_quote zeigen, dass sprachliche Qualität zu diesem Kriterium
  gehört; source_quote bleibt leer und source_scope ist none.

Für alle Arten außer strength muss role improvement sein. Bei
missing_requirement und criterion_mismatch ist source_scope none. Verwende
task_material oder run_original_text nur, wenn source_quote exakt aus dem
jeweiligen Feld stammt.

Formuliere student_feedback verständlich, konkret und beleggestützt für die
angegebene Klassenstufe. Es gibt dafür keine vorgegebene Satz- oder
Zeichenbegrenzung. Jeder Verbesserungsbefund benötigt außerdem einen konkreten
next_step, der logisch aus dem Befund folgt, selbstständig umsetzbar ist und
keine fertige Lösung vorgibt. Kannst du keinen solchen sicheren Schritt
formulieren, darfst du den Verbesserungsbefund nicht ausgeben. Bei einer Stärke
bleibt next_step leer. Verwende in diesen Feldern weder Markdown noch technische
Statuswerte. Schreibe keine vollständige Musterlösung.

Die Statuswerte bedeuten ausschließlich:

- met = erfüllt
- mostly_met = überwiegend erfüllt
- partially_met = teilweise erfüllt
- not_met = nicht erfüllt
- not_assessable = nicht beurteilbar

Antworte ausschließlich als gültiges JSON-Objekt ohne Markdown-Codeblock.
Jede vorgegebene criterion_id muss genau einmal vorkommen. Verwende genau diese
Struktur und füge keine Felder hinzu:

{json.dumps(response_template, ensure_ascii=False, indent=2)}

Eingabe:
<analysis_input>
{json.dumps(analysis_input, ensure_ascii=False, indent=2)}
</analysis_input>
""".strip()

    @staticmethod
    def _build_review_prompt(
        *,
        student_text: str,
        task: FeedbackTask,
        original_text: str,
        candidate_result: CandidateParseResult,
    ) -> str:
        criteria_by_reference = (
            RubricFeedbackService._criteria_by_reference(task)
        )
        candidates_by_reference = {
            criterion.criterion_reference: criterion
            for criterion in candidate_result.criteria
        }
        response_template = {
            "criteria": [
                {
                    "criterion_id": reference,
                    "status": (
                        "met | mostly_met | partially_met | not_met | "
                        "not_assessable"
                    ),
                    "decisions": [
                        {
                            "finding_id": finding.finding_id,
                            "verdict": "accept | reject",
                            "reason": "kurzer technischer Prüfgrund",
                        }
                        for finding in candidates_by_reference[
                            reference
                        ].findings
                    ],
                }
                for reference in criteria_by_reference
            ],
        }
        review_input = {
            "task": {
                "title": task.title,
                "subject": task.subject,
                "grade_level": task.grade_level,
                "instructions": task.instructions,
                "material": task.material,
                "criteria": [
                    {
                        "criterion_id": reference,
                        "title": criterion.title,
                        "text": criterion.text,
                    }
                    for reference, criterion
                    in criteria_by_reference.items()
                ],
            },
            "original_text_for_this_run": original_text or None,
            "student_text": student_text,
            "candidate_findings": [
                {
                    "criterion_id": reference,
                    "findings": [
                        finding.review_payload()
                        for finding in candidates_by_reference[
                            reference
                        ].findings
                    ],
                }
                for reference in criteria_by_reference
            ],
        }

        return f"""
Du führst die eingeschränkte Zweitprüfung eines Schreibfeedbacks durch. Prüfe
jeden technisch quellengeprüften Kandidaten noch einmal semantisch gegen den
vollständigen Schülertext, die Aufgabe, das jeweilige Kriterium und – sofern
angegeben – die fachliche Quelle. Die technische Prüfung beweist nur, dass ein
Zitat in der bezeichneten Quelle vorkommt. Sie beweist ausdrücklich nicht, dass
die Behauptung des Kandidaten fachlich richtig ist. Behandle daher jeden
Kandidaten als unbestätigte Hypothese.

Trenne die Quellenrollen erneut strikt: Nur student_text enthält die
Schülerleistung. Aufgabe und Kriterien enthalten Anforderungen. Material und
original_text_for_this_run sind fachliche Quellen, aber keine Schülerleistung.

Du darfst ausschließlich vorhandene finding_id-Werte bestätigen oder
verwerfen. Du darfst keine neuen Fehler, Stärken, schülergerichteten
Begründungen, Überarbeitungsvorschläge oder finding_id-Werte ergänzen. Das Feld
reason enthält lediglich den technischen Grund für accept oder reject und wird
nicht Teil des Schülerfeedbacks. Ein Befund wird nur bestätigt, wenn claim,
student_feedback und next_step vollständig durch seine angegebenen Belege und
den Gesamtkontext getragen werden. Bei Zweifeln wird er verworfen.

Prüfe besonders:

- Wurde ein angeblich fehlender Bestandteil nicht doch sinngemäß an anderer
  Stelle des vollständigen student_text erfüllt?
- Stützt das Schülerzitat wirklich den behaupteten Befund?
- Belegt criterion_quote unmittelbar, dass der Befund genau zum aktuellen
  Kriterium gehört, statt zu einem anderen Kriterium der Vorlage?
- Stützt criterion_quote oder source_quote die fachliche Beanstandung?
- Ist eine literarische Deutung wirklich unvereinbar mit dem Text oder nur
  eine vertretbare andere Lesart?
- Ist next_step logisch, sicher, adressatengerecht und ohne fertige Lösung?

Setze für jedes Kriterium anschließend einen Status ausschließlich anhand des
vollständigen Kontexts und der von dir bestätigten Befunde. Wenn der Status nicht
sicher bestimmbar ist, verwende not_assessable. Technische Statuswerte
erscheinen nur im Feld status. Gib zu jeder finding_id genau eine Entscheidung
accept oder reject und einen kurzen technischen Prüfgrund aus.

Bei einem mehrteiligen Kriterium ist met nur zulässig, wenn der vollständige
Schülertext wirklich alle verlangten Teilaspekte erfüllt. Eine einzelne
bestätigte Stärke reicht dafür nicht. Sobald du einen Verbesserungsbefund
bestätigst, darf der Status nicht met sein.

Antworte ausschließlich als gültiges JSON-Objekt ohne Markdown-Codeblock und
ohne zusätzliche Felder. Verwende genau diese Struktur:

{json.dumps(response_template, ensure_ascii=False, indent=2)}

Eingabe:
<review_input>
{json.dumps(review_input, ensure_ascii=False, indent=2)}
</review_input>
""".strip()

    @staticmethod
    def _build_candidate_schema(
        task: FeedbackTask,
    ) -> dict[str, object]:
        references = list(
            RubricFeedbackService._criteria_by_reference(task)
        )
        finding_schema = {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "enum": sorted(FINDING_ROLES),
                },
                "kind": {
                    "type": "string",
                    "enum": sorted(FINDING_KINDS),
                },
                "claim": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_FINDING_TEXT_CHARS,
                },
                "student_quote": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                },
                "criterion_quote": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                },
                "source_scope": {
                    "type": "string",
                    "enum": sorted(SOURCE_SCOPES),
                },
                "source_quote": {
                    "type": "string",
                    "maxLength": 500,
                },
                "student_feedback": {
                    "type": "string",
                    "minLength": 1,
                },
                "next_step": {
                    "type": "string",
                },
            },
            "required": [
                "role",
                "kind",
                "claim",
                "student_quote",
                "criterion_quote",
                "source_scope",
                "source_quote",
                "student_feedback",
                "next_step",
            ],
            "additionalProperties": False,
        }
        criterion_schema = {
            "type": "object",
            "properties": {
                "criterion_id": {
                    "type": "string",
                    "enum": references,
                },
                "findings": {
                    "type": "array",
                    "items": finding_schema,
                    "minItems": 0,
                    "maxItems": MAX_FINDINGS_PER_CRITERION,
                },
            },
            "required": [
                "criterion_id",
                "findings",
            ],
            "additionalProperties": False,
        }

        return {
            "type": "object",
            "properties": {
                "criteria": {
                    "type": "array",
                    "items": criterion_schema,
                    "minItems": len(references),
                    "maxItems": len(references),
                },
            },
            "required": ["criteria"],
            "additionalProperties": False,
        }

    @staticmethod
    def _build_review_schema(
        task: FeedbackTask,
        candidate_result: CandidateParseResult,
    ) -> dict[str, object]:
        references = list(
            RubricFeedbackService._criteria_by_reference(task)
        )
        finding_ids = [
            finding.finding_id
            for finding in candidate_result.findings
        ]
        decision_schema = {
            "type": "object",
            "properties": {
                "finding_id": {
                    "type": "string",
                    "enum": finding_ids,
                },
                "verdict": {
                    "type": "string",
                    "enum": sorted(REVIEW_VERDICTS),
                },
                "reason": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_REVIEW_REASON_CHARS,
                },
            },
            "required": ["finding_id", "verdict", "reason"],
            "additionalProperties": False,
        }
        criterion_schema = {
            "type": "object",
            "properties": {
                "criterion_id": {
                    "type": "string",
                    "enum": references,
                },
                "status": {
                    "type": "string",
                    "enum": list(CRITERION_STATUS_LABELS),
                },
                "decisions": {
                    "type": "array",
                    "items": decision_schema,
                    "minItems": 0,
                    "maxItems": len(finding_ids),
                },
            },
            "required": ["criterion_id", "status", "decisions"],
            "additionalProperties": False,
        }

        return {
            "type": "object",
            "properties": {
                "criteria": {
                    "type": "array",
                    "items": criterion_schema,
                    "minItems": len(references),
                    "maxItems": len(references),
                },
            },
            "required": ["criteria"],
            "additionalProperties": False,
        }

    def _parse_candidate_response(
        self,
        response_text: str,
        *,
        task: FeedbackTask,
        student_text: str,
        original_text: str,
        finish_reason: str | None,
    ) -> CandidateParseResult:
        payload = self._response_payload(
            response_text,
            finish_reason=finish_reason,
            phase_label="Befundphase",
        )
        raw_criteria = payload.get("criteria")

        if not isinstance(raw_criteria, list):
            raise RubricFeedbackError(
                "In der Befundphase fehlt die Kriterienliste."
            )

        expected = RubricFeedbackService._criteria_by_reference(task)
        parsed: dict[str, CandidateCriterion] = {}
        warnings: list[str] = []
        raw_finding_count = 0

        for raw_criterion in raw_criteria:
            if not isinstance(raw_criterion, dict):
                raise RubricFeedbackError(
                    "Ein Ergebnis der Befundphase besitzt ein "
                    "ungültiges Format."
                )

            reference = RubricFeedbackService._required_string(
                raw_criterion,
                "criterion_id",
            )

            if reference not in expected:
                raise RubricFeedbackError(
                    "Die Befundphase enthält ein unbekanntes Kriterium."
                )
            if reference in parsed:
                raise RubricFeedbackError(
                    "Die Befundphase enthält ein Kriterium mehrfach."
                )

            raw_findings = raw_criterion.get("findings")

            if not isinstance(raw_findings, list):
                raise RubricFeedbackError(
                    "In der Befundphase fehlt eine Befundliste."
                )
            if len(raw_findings) > MAX_FINDINGS_PER_CRITERION:
                raise RubricFeedbackError(
                    "Die Befundphase enthält zu viele Befunde zu einem "
                    "Kriterium."
                )

            criterion = expected[reference]
            criterion_title = self._criterion_title(criterion)
            parsed_findings: list[CandidateFinding] = []
            seen_findings: set[tuple[object, ...]] = set()
            strength_count = 0
            improvement_count = 0

            for position, raw_finding in enumerate(
                raw_findings,
                start=1,
            ):
                raw_finding_count += 1

                try:
                    finding = self._validated_candidate_finding(
                        raw_finding,
                        reference=reference,
                        position=position,
                        criterion=criterion,
                        student_text=student_text,
                        task_material=task.material,
                        original_text=original_text,
                    )
                except RubricFeedbackError as exc:
                    warnings.append(
                        f"{reference} – {criterion_title}: "
                        f"Kandidatenbefund {position} wurde technisch "
                        f"verworfen: {exc}"
                    )
                    continue

                duplicate_key = (
                    finding.role,
                    finding.kind,
                    finding.claim.casefold(),
                    RubricFeedbackService._evidence_tokens(
                        finding.student_quote
                    ),
                )

                if duplicate_key in seen_findings:
                    warnings.append(
                        f"{reference} – {criterion_title}: Ein doppelter "
                        "Kandidatenbefund wurde verworfen."
                    )
                    continue

                if finding.role == "strength":
                    strength_count += 1

                    if strength_count > MAX_STRENGTHS_PER_CRITERION:
                        warnings.append(
                            f"{reference} – {criterion_title}: Ein "
                            "zusätzlicher Stärkenbefund wurde wegen der "
                            "Begrenzung verworfen."
                        )
                        continue
                else:
                    improvement_count += 1

                    if (
                        improvement_count
                        > MAX_IMPROVEMENTS_PER_CRITERION
                    ):
                        warnings.append(
                            f"{reference} – {criterion_title}: Ein "
                            "zusätzlicher Verbesserungsbefund wurde wegen "
                            "der Begrenzung verworfen."
                        )
                        continue

                seen_findings.add(duplicate_key)
                parsed_findings.append(finding)

            parsed[reference] = CandidateCriterion(
                criterion_reference=reference,
                findings=tuple(parsed_findings),
            )

        if set(parsed) != set(expected):
            raise RubricFeedbackError(
                "Die Befundphase enthält nicht jedes Kriterium genau einmal."
            )

        return CandidateParseResult(
            criteria=tuple(parsed[reference] for reference in expected),
            raw_finding_count=raw_finding_count,
            warnings=tuple(warnings),
        )

    def _validated_candidate_finding(
        self,
        raw_finding: object,
        *,
        reference: str,
        position: int,
        criterion: RubricCriterion,
        student_text: str,
        task_material: str,
        original_text: str,
    ) -> CandidateFinding:
        if not isinstance(raw_finding, dict):
            raise RubricFeedbackError(
                "Ein Kandidatenbefund besitzt ein ungültiges Format."
            )

        role = RubricFeedbackService._required_string(
            raw_finding,
            "role",
        )
        kind = RubricFeedbackService._required_string(
            raw_finding,
            "kind",
        )
        claim = RubricFeedbackService._required_string(
            raw_finding,
            "claim",
        )
        student_quote = RubricFeedbackService._required_string(
            raw_finding,
            "student_quote",
        )
        student_feedback = RubricFeedbackService._required_string(
            raw_finding,
            "student_feedback",
        )
        criterion_quote = self._string_field(
            raw_finding,
            "criterion_quote",
        )
        source_scope = RubricFeedbackService._required_string(
            raw_finding,
            "source_scope",
        )
        source_quote = self._string_field(
            raw_finding,
            "source_quote",
        )
        next_step = self._string_field(raw_finding, "next_step")

        if role not in FINDING_ROLES or kind not in FINDING_KINDS:
            raise RubricFeedbackError(
                "Rolle oder Art des Kandidatenbefunds ist ungültig."
            )
        if source_scope not in SOURCE_SCOPES:
            raise RubricFeedbackError(
                "Die Quellenrolle des Kandidatenbefunds ist ungültig."
            )
        if kind == "strength" and role != "strength":
            raise RubricFeedbackError(
                "Ein Stärkenbefund besitzt die falsche Rolle."
            )
        if kind != "strength" and role != "improvement":
            raise RubricFeedbackError(
                "Ein Verbesserungsbefund besitzt die falsche Rolle."
            )
        if role == "improvement" and not next_step:
            raise RubricFeedbackError(
                "Für den Verbesserungsbefund fehlt ein sicherer "
                "Überarbeitungsschritt."
            )
        if role == "strength" and next_step:
            raise RubricFeedbackError(
                "Ein Stärkenbefund darf keinen Überarbeitungsschritt "
                "enthalten."
            )
        if TECHNICAL_STATUS_PATTERN.search(
            f"{student_feedback} {next_step}"
        ):
            raise RubricFeedbackError(
                "Der Kandidatenbefund enthält einen technischen "
                "Statuswert im Schülertext."
            )

        self._validated_quote(student_quote, student_text)
        if not criterion_quote:
            raise RubricFeedbackError(
                "Für den Befund fehlt die Verbindung zum Kriterium."
            )
        self._validated_quote(criterion_quote, criterion.text)

        if kind in {"missing_requirement", "criterion_mismatch"}:
            if source_scope != "none" or source_quote:
                raise RubricFeedbackError(
                    "Ein Kriterienbefund besitzt eine widersprüchliche "
                    "Quellenangabe."
                )
        elif kind == "source_mismatch":
            source_text = {
                "task_material": task_material,
                "run_original_text": original_text,
            }.get(source_scope)

            if not source_text or not source_quote:
                raise RubricFeedbackError(
                    "Für den Quellenwiderspruch fehlt ein verfügbarer "
                    "Originalbeleg."
                )

            self._validated_quote(source_quote, source_text)

        elif source_scope != "none" or source_quote:
            raise RubricFeedbackError(
                "Der Befund besitzt nicht benötigte Quellenangaben."
            )

        return CandidateFinding(
            finding_id=f"{reference}-F{position}",
            criterion_reference=reference,
            role=role,
            kind=kind,
            claim=claim,
            student_quote=student_quote,
            criterion_quote=criterion_quote,
            source_scope=source_scope,
            source_quote=source_quote,
            student_feedback=student_feedback,
            next_step=next_step,
        )

    @staticmethod
    def _validated_quote(quote: str, source_text: str) -> None:
        RubricFeedbackService._validated_evidence_quotes(
            {"evidence_quotes": [quote]},
            student_text=source_text,
            status="met",
        )

    def _parse_review_response(
        self,
        response_text: str,
        *,
        task: FeedbackTask,
        candidate_result: CandidateParseResult,
        finish_reason: str | None,
    ) -> tuple[ReviewCriterion, ...]:
        payload = self._response_payload(
            response_text,
            finish_reason=finish_reason,
            phase_label="Zweitprüfung",
        )
        raw_criteria = payload.get("criteria")

        if not isinstance(raw_criteria, list):
            raise RubricFeedbackError(
                "In der Zweitprüfung fehlt die Kriterienliste."
            )

        expected_criteria = (
            RubricFeedbackService._criteria_by_reference(task)
        )
        findings_by_id = {
            finding.finding_id: finding
            for finding in candidate_result.findings
        }
        parsed: dict[str, ReviewCriterion] = {}
        seen_finding_ids: set[str] = set()

        for raw_criterion in raw_criteria:
            if not isinstance(raw_criterion, dict):
                raise RubricFeedbackError(
                    "Ein Ergebnis der Zweitprüfung besitzt ein "
                    "ungültiges Format."
                )

            reference = RubricFeedbackService._required_string(
                raw_criterion,
                "criterion_id",
            )

            if reference not in expected_criteria:
                raise RubricFeedbackError(
                    "Die Zweitprüfung enthält ein unbekanntes Kriterium."
                )
            if reference in parsed:
                raise RubricFeedbackError(
                    "Die Zweitprüfung enthält ein Kriterium mehrfach."
                )

            status = RubricFeedbackService._required_string(
                raw_criterion,
                "status",
            )

            if status not in CRITERION_STATUS_LABELS:
                raise RubricFeedbackError(
                    "Die Zweitprüfung enthält einen ungültigen Status."
                )

            raw_decisions = raw_criterion.get("decisions")

            if not isinstance(raw_decisions, list):
                raise RubricFeedbackError(
                    "In der Zweitprüfung fehlt eine Entscheidungsliste."
                )

            accepted_ids: list[str] = []

            for raw_decision in raw_decisions:
                if not isinstance(raw_decision, dict):
                    raise RubricFeedbackError(
                        "Eine Prüfentscheidung besitzt ein ungültiges "
                        "Format."
                    )

                finding_id = RubricFeedbackService._required_string(
                    raw_decision,
                    "finding_id",
                )
                verdict = RubricFeedbackService._required_string(
                    raw_decision,
                    "verdict",
                )
                RubricFeedbackService._required_string(
                    raw_decision,
                    "reason",
                )

                finding = findings_by_id.get(finding_id)

                if finding is None:
                    raise RubricFeedbackError(
                        "Die Zweitprüfung hat einen neuen, unzulässigen "
                        "Befund ergänzt."
                    )
                if finding.criterion_reference != reference:
                    raise RubricFeedbackError(
                        "Die Zweitprüfung hat einen Befund dem falschen "
                        "Kriterium zugeordnet."
                    )
                if finding_id in seen_finding_ids:
                    raise RubricFeedbackError(
                        "Die Zweitprüfung hat einen Befund mehrfach "
                        "entschieden."
                    )
                if verdict not in REVIEW_VERDICTS:
                    raise RubricFeedbackError(
                        "Die Zweitprüfung enthält eine ungültige "
                        "Entscheidung."
                    )

                seen_finding_ids.add(finding_id)

                if verdict == "accept":
                    accepted_ids.append(finding_id)

            parsed[reference] = ReviewCriterion(
                criterion_reference=reference,
                status=status,
                accepted_finding_ids=tuple(accepted_ids),
            )

        if set(parsed) != set(expected_criteria):
            raise RubricFeedbackError(
                "Die Zweitprüfung enthält nicht jedes Kriterium genau "
                "einmal."
            )
        if seen_finding_ids != set(findings_by_id):
            raise RubricFeedbackError(
                "Die Zweitprüfung hat nicht jeden Kandidatenbefund genau "
                "einmal entschieden."
            )

        return tuple(
            parsed[reference]
            for reference in expected_criteria
        )

    def _build_reviewed_result(
        self,
        *,
        task: FeedbackTask,
        candidate_result: CandidateParseResult,
        review_criteria: tuple[ReviewCriterion, ...],
        analysis_response: LLMResponse,
        review_response: LLMResponse,
        total_started_at: float,
        analysis_duration_ms: int,
        review_duration_ms: int,
    ) -> RubricFeedbackResult:
        findings_by_id = {
            finding.finding_id: finding
            for finding in candidate_result.findings
        }
        expected = RubricFeedbackService._criteria_by_reference(task)
        review_by_reference = {
            item.criterion_reference: item
            for item in review_criteria
        }
        results: list[CriterionFeedbackResult] = []
        evidence_warnings: list[str] = []
        accepted_count = 0
        accepted_issue_count = 0
        accepted_strength_count = 0

        for reference, criterion in expected.items():
            review = review_by_reference[reference]
            accepted = tuple(
                findings_by_id[finding_id]
                for finding_id in review.accepted_finding_ids
            )
            accepted_count += len(accepted)
            strengths = tuple(
                finding
                for finding in accepted
                if finding.role == "strength"
            )
            improvements = tuple(
                finding
                for finding in accepted
                if finding.role == "improvement"
            )
            accepted_strength_count += len(strengths)
            accepted_issue_count += len(improvements)
            criterion_title = self._criterion_title(criterion)

            if not accepted:
                evidence_warnings.append(
                    f"{reference} – {criterion_title}: Nach technischer "
                    "Prüfung und Zweitprüfung blieb kein ausreichend "
                    "sicherer Befund übrig."
                )
                results.append(
                    self._unverified_criterion_result(criterion)
                )
                continue

            feedback_parts: list[str] = []

            if strengths:
                feedback_parts.append(
                    f"Stärke: {strengths[0].student_feedback}"
                )

            for position, finding in enumerate(improvements):
                prefix = "Verbesserung:" if position == 0 else "Außerdem:"
                feedback_parts.append(
                    f"{prefix} {finding.student_feedback}"
                )

            next_step = (
                improvements[0].next_step
                if improvements
                else ""
            )
            evidence_quotes = self._unique_quotes(
                finding.student_quote
                for finding in accepted
            )
            student_feedback_sections = StudentFeedbackSections(
                staerke=(
                    strengths[0].student_feedback
                    if strengths
                    else (
                        "Eine konkrete Stärke lässt sich aus den "
                        "bestätigten Befunden noch nicht sicher ableiten."
                    )
                ),
                rueckmeldung=(
                    " ".join(
                        finding.student_feedback
                        for finding in improvements
                    )
                    if improvements
                    else strengths[0].student_feedback
                ),
                naechster_schritt=(
                    next_step
                    or (
                        "Prüfe abschließend, ob du diese Stärke im "
                        "gesamten Text beibehältst."
                    )
                ),
                formulierungshilfen=(
                    open_formulation_helps_for_criterion(
                        criterion_title=criterion_title,
                        criterion_text=criterion.text,
                    )
                    if improvements
                    else ()
                ),
            )

            results.append(
                CriterionFeedbackResult(
                    criterion_id=criterion.criterion_id,
                    criterion_title=criterion_title,
                    criterion_text=criterion.text,
                    status=(
                        "mostly_met"
                        if improvements and review.status == "met"
                        else review.status
                    ),
                    status_label=CRITERION_STATUS_LABELS[
                        "mostly_met"
                        if improvements and review.status == "met"
                        else review.status
                    ],
                    feedback=" ".join(feedback_parts),
                    next_step=next_step,
                    evidence_quotes=evidence_quotes,
                    evidence_verified=True,
                    student_feedback_sections=(
                        student_feedback_sections
                    ),
                )
            )

        if accepted_issue_count:
            overall_feedback = TWO_PASS_PARTIAL_OVERALL_FEEDBACK
        elif accepted_strength_count:
            overall_feedback = TWO_PASS_STRENGTH_ONLY_OVERALL_FEEDBACK
        else:
            overall_feedback = TWO_PASS_EMPTY_OVERALL_FEEDBACK

        return self._result(
            task=task,
            criteria_feedback=tuple(results),
            overall_feedback=overall_feedback,
            evidence_warnings=tuple(evidence_warnings),
            pipeline_warnings=candidate_result.warnings,
            analysis_response=analysis_response,
            review_response=review_response,
            duration_ms=self._elapsed_ms(total_started_at),
            analysis_duration_ms=analysis_duration_ms,
            review_duration_ms=review_duration_ms,
            raw_finding_count=candidate_result.raw_finding_count,
            validated_candidate_count=len(candidate_result.findings),
            accepted_finding_count=accepted_count,
        )

    def _safe_result(
        self,
        *,
        task: FeedbackTask,
        analysis_response: LLMResponse,
        review_response: LLMResponse | None,
        total_started_at: float,
        analysis_duration_ms: int,
        review_duration_ms: int,
        raw_finding_count: int,
        validated_candidate_count: int,
        pipeline_warnings: tuple[str, ...],
    ) -> RubricFeedbackResult:
        criteria_feedback = tuple(
            self._unverified_criterion_result(criterion)
            for criterion in task.rubric.criteria
        )
        evidence_warnings = tuple(
            f"{reference} – {self._criterion_title(criterion)}: "
            "Es wurde bewusst kein ungeprüfter Modellbefund übernommen."
            for reference, criterion
            in RubricFeedbackService._criteria_by_reference(task).items()
        )

        return self._result(
            task=task,
            criteria_feedback=criteria_feedback,
            overall_feedback=TWO_PASS_EMPTY_OVERALL_FEEDBACK,
            evidence_warnings=evidence_warnings,
            pipeline_warnings=pipeline_warnings,
            analysis_response=analysis_response,
            review_response=review_response,
            duration_ms=self._elapsed_ms(total_started_at),
            analysis_duration_ms=analysis_duration_ms,
            review_duration_ms=review_duration_ms,
            raw_finding_count=raw_finding_count,
            validated_candidate_count=validated_candidate_count,
            accepted_finding_count=0,
        )

    def _result(
        self,
        *,
        task: FeedbackTask,
        criteria_feedback: tuple[CriterionFeedbackResult, ...],
        overall_feedback: str,
        evidence_warnings: tuple[str, ...],
        pipeline_warnings: tuple[str, ...],
        analysis_response: LLMResponse,
        review_response: LLMResponse | None,
        duration_ms: int,
        analysis_duration_ms: int,
        review_duration_ms: int,
        raw_finding_count: int,
        validated_candidate_count: int,
        accepted_finding_count: int,
    ) -> RubricFeedbackResult:
        final_response = review_response or analysis_response

        return RubricFeedbackResult(
            provider=final_response.provider,
            model=final_response.model,
            task_id=task.task_id,
            task_title=task.title,
            rubric_title=task.rubric.title,
            criteria_feedback=criteria_feedback,
            overall_feedback=overall_feedback,
            duration_ms=duration_ms,
            queue_duration_ms=self._sum_optional(
                analysis_response.queue_duration_ms,
                review_response.queue_duration_ms
                if review_response is not None
                else None,
            ),
            execution_duration_ms=self._sum_optional(
                analysis_response.execution_duration_ms,
                review_response.execution_duration_ms
                if review_response is not None
                else None,
            ),
            provider_request_id=final_response.provider_request_id,
            worker_id=final_response.worker_id,
            reasoning_effort=(
                RubricFeedbackService._reasoning_effort(
                    final_response.raw_metadata
                )
                or RubricFeedbackService._reasoning_effort(
                    analysis_response.raw_metadata
                )
            ),
            evidence_warnings=evidence_warnings,
            pipeline_mode=TWO_PASS_FEEDBACK_MODE,
            pipeline_label=TWO_PASS_FEEDBACK_LABEL,
            prompt_version=TWO_PASS_PIPELINE_VERSION,
            evidence_validation_version=(
                TWO_PASS_EVIDENCE_VALIDATION_VERSION
            ),
            analysis_prompt_version=TWO_PASS_ANALYSIS_PROMPT_VERSION,
            review_prompt_version=TWO_PASS_REVIEW_PROMPT_VERSION,
            analysis_duration_ms=analysis_duration_ms,
            review_duration_ms=review_duration_ms,
            candidate_finding_count=raw_finding_count,
            validated_candidate_count=validated_candidate_count,
            accepted_finding_count=accepted_finding_count,
            rejected_finding_count=max(
                0,
                raw_finding_count - accepted_finding_count,
            ),
            analysis_provider_request_id=(
                analysis_response.provider_request_id
            ),
            review_provider_request_id=(
                review_response.provider_request_id
                if review_response is not None
                else None
            ),
            pipeline_warnings=pipeline_warnings,
        )

    @staticmethod
    def _unverified_criterion_result(
        criterion: RubricCriterion,
    ) -> CriterionFeedbackResult:
        return CriterionFeedbackResult(
            criterion_id=criterion.criterion_id,
            criterion_title=(
                TwoPassRubricFeedbackService._criterion_title(
                    criterion
                )
            ),
            criterion_text=criterion.text,
            status="not_assessable",
            status_label=CRITERION_STATUS_LABELS[
                "not_assessable"
            ],
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
    def _criterion_title(criterion: RubricCriterion) -> str:
        return criterion.title or f"Kriterium {criterion.position + 1}"

    @staticmethod
    def _response_payload(
        response_text: str,
        *,
        finish_reason: str | None,
        phase_label: str,
    ) -> dict[str, object]:
        cleaned = RubricFeedbackService._remove_optional_code_fence(
            response_text
        )

        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            if finish_reason in {
                "length",
                "limit",
                "max_output_tokens",
                "max_tokens",
            }:
                raise RubricFeedbackError(
                    f"Die {phase_label} wurde am Ausgabelimit "
                    "abgeschnitten."
                ) from exc

            raise RubricFeedbackError(
                f"Die {phase_label} enthält kein gültiges JSON."
            ) from exc

        if not isinstance(payload, dict):
            raise RubricFeedbackError(
                f"Die {phase_label} besitzt nicht das erwartete Format."
            )

        return payload

    @staticmethod
    def _string_field(
        payload: dict[str, object],
        key: str,
    ) -> str:
        value = payload.get(key)

        if not isinstance(value, str):
            raise RubricFeedbackError(
                f"Im Kandidatenbefund fehlt das Textfeld '{key}'."
            )

        return value.strip()

    @staticmethod
    def _validate_phase_identity(
        analysis_response: LLMResponse,
        review_response: LLMResponse,
    ) -> None:
        if (
            analysis_response.provider != review_response.provider
            or analysis_response.model != review_response.model
        ):
            raise RubricFeedbackError(
                "Die beiden experimentellen Phasen wurden nicht mit "
                "demselben Modell ausgeführt."
            )

    @staticmethod
    def _unique_quotes(quotes: Iterable[str]) -> tuple[str, ...]:
        unique: list[str] = []
        seen: set[tuple[str, ...]] = set()

        for quote in quotes:
            tokens = RubricFeedbackService._evidence_tokens(quote)

            if tokens in seen:
                continue

            seen.add(tokens)
            unique.append(quote)

        return tuple(unique)

    @staticmethod
    def _sum_optional(
        *values: float | None,
    ) -> float | None:
        available = [value for value in values if value is not None]
        return sum(available) if available else None

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return int((perf_counter() - started_at) * 1000)
