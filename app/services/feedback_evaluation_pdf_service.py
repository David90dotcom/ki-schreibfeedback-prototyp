from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.domain.feedback_evaluation import (
    FeedbackEvaluationRating,
    StoredFeedbackEvaluation,
    StoredFeedbackRun,
)


_FONT_DIRECTORY = Path(reportlab.__file__).resolve().parent / "fonts"
_FONT_REGULAR = "MetaVera"
_FONT_BOLD = "MetaVeraBold"
_FONT_ITALIC = "MetaVeraItalic"
_FONT_BOLD_ITALIC = "MetaVeraBoldItalic"

pdfmetrics.registerFont(
    TTFont(_FONT_REGULAR, _FONT_DIRECTORY / "Vera.ttf")
)
pdfmetrics.registerFont(
    TTFont(_FONT_BOLD, _FONT_DIRECTORY / "VeraBd.ttf")
)
pdfmetrics.registerFont(
    TTFont(_FONT_ITALIC, _FONT_DIRECTORY / "VeraIt.ttf")
)
pdfmetrics.registerFont(
    TTFont(_FONT_BOLD_ITALIC, _FONT_DIRECTORY / "VeraBI.ttf")
)
pdfmetrics.registerFontFamily(
    _FONT_REGULAR,
    normal=_FONT_REGULAR,
    bold=_FONT_BOLD,
    italic=_FONT_ITALIC,
    boldItalic=_FONT_BOLD_ITALIC,
)

_PRIMARY = colors.HexColor("#5925DC")
_PRIMARY_DARK = colors.HexColor("#3E1C96")
_TEXT = colors.HexColor("#101828")
_MUTED = colors.HexColor("#667085")
_BORDER = colors.HexColor("#D0D5DD")
_SURFACE = colors.HexColor("#F9FAFB")
_INFO_SURFACE = colors.HexColor("#F4F3FF")

_SCORE_COLORS = {
    0: (
        colors.HexColor("#B42318"),
        colors.HexColor("#FFF1F1"),
        colors.HexColor("#FDA29B"),
    ),
    1: (
        colors.HexColor("#92400E"),
        colors.HexColor("#FFFBEB"),
        colors.HexColor("#F5C451"),
    ),
    2: (
        colors.HexColor("#3F6212"),
        colors.HexColor("#F7FEE7"),
        colors.HexColor("#A3E635"),
    ),
    3: (
        colors.HexColor("#067647"),
        colors.HexColor("#ECFDF3"),
        colors.HexColor("#6CE9A6"),
    ),
}


class FeedbackEvaluationPdfError(RuntimeError):
    """Der PDF-Export einer Meta-Bewertung ist fehlgeschlagen."""


@dataclass(frozen=True)
class FeedbackEvaluationPdf:
    content: bytes
    filename: str


def _paragraph_text(value: object) -> str:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return escape(text).replace("\n", "<br/>")


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return _normalize_datetime(value).strftime("%d.%m.%Y, %H:%M UTC")


def _format_duration(value: float | int | None) -> str:
    if value is None:
        return "Nicht verfügbar"

    milliseconds = max(0.0, float(value))

    if milliseconds < 1000:
        return f"{milliseconds:.0f} ms"

    seconds = milliseconds / 1000

    if seconds < 60:
        return f"{seconds:.1f}".replace(".", ",") + " s"

    minutes = int(seconds // 60)
    remaining_seconds = seconds % 60
    formatted_seconds = f"{remaining_seconds:.1f}".replace(".", ",")
    return f"{minutes} min {formatted_seconds} s"


def _format_score(value: float | None) -> str:
    if value is None:
        return "Nicht verfügbar"

    return f"{value:.1f}".replace(".", ",")


def _filename_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_value).strip("-")
    return (slug.lower() or "bewertung")[:48].rstrip("-")


class FeedbackEvaluationPdfService:
    """Erzeugt einen lokalen, druckfertigen Einzelbewertungs-Export."""

    def render(
        self,
        *,
        feedback_run: StoredFeedbackRun,
        evaluation: StoredFeedbackEvaluation,
    ) -> FeedbackEvaluationPdf:
        if evaluation.feedback_run_id != feedback_run.feedback_run_id:
            raise FeedbackEvaluationPdfError(
                "Die Bewertung gehört nicht zum angegebenen Feedbacklauf."
            )

        buffer = BytesIO()
        display_name = evaluation.evaluation_name or evaluation.type_label
        filename = (
            "meta-bewertung-"
            f"{_normalize_datetime(evaluation.created_at):%Y%m%d-%H%M}-"
            f"{_filename_slug(display_name)}-"
            f"{evaluation.evaluation_id[:8]}.pdf"
        )

        try:
            document = SimpleDocTemplate(
                buffer,
                pagesize=A4,
                rightMargin=18 * mm,
                leftMargin=18 * mm,
                topMargin=18 * mm,
                bottomMargin=22 * mm,
                title=f"Meta-Bewertung - {display_name}",
                author="KI-Schreibfeedback-Prototyp",
                subject=(
                    "Einzelexport einer gespeicherten Meta-Bewertung"
                ),
            )
            styles = self._styles()
            story = self._story(
                feedback_run=feedback_run,
                evaluation=evaluation,
                display_name=display_name,
                styles=styles,
            )

            def draw_footer(canvas, doc) -> None:
                canvas.saveState()
                canvas.setStrokeColor(_BORDER)
                canvas.setLineWidth(0.5)
                canvas.line(
                    18 * mm,
                    16 * mm,
                    A4[0] - 18 * mm,
                    16 * mm,
                )
                canvas.setFillColor(_MUTED)
                canvas.setFont(_FONT_REGULAR, 7.5)
                canvas.drawString(
                    18 * mm,
                    11 * mm,
                    "KI-Schreibfeedback - Meta-Bewertung",
                )
                canvas.drawRightString(
                    A4[0] - 18 * mm,
                    11 * mm,
                    f"Seite {doc.page}",
                )
                canvas.restoreState()

            document.build(
                story,
                onFirstPage=draw_footer,
                onLaterPages=draw_footer,
            )
        except Exception as exc:
            raise FeedbackEvaluationPdfError(
                "Die Meta-Bewertung konnte nicht als PDF erzeugt werden."
            ) from exc

        return FeedbackEvaluationPdf(
            content=buffer.getvalue(),
            filename=filename,
        )

    @staticmethod
    def _styles() -> dict[str, ParagraphStyle]:
        base = getSampleStyleSheet()
        return {
            "title": ParagraphStyle(
                "MetaTitle",
                parent=base["Title"],
                fontName=_FONT_BOLD,
                fontSize=19,
                leading=23,
                textColor=_PRIMARY_DARK,
                alignment=TA_CENTER,
                spaceAfter=6,
            ),
            "subtitle": ParagraphStyle(
                "MetaSubtitle",
                parent=base["Normal"],
                fontName=_FONT_REGULAR,
                fontSize=10.5,
                leading=15,
                textColor=_MUTED,
                alignment=TA_CENTER,
                spaceAfter=14,
            ),
            "section": ParagraphStyle(
                "MetaSection",
                parent=base["Heading2"],
                fontName=_FONT_BOLD,
                fontSize=12.5,
                leading=16,
                textColor=_PRIMARY_DARK,
                spaceBefore=14,
                spaceAfter=7,
                keepWithNext=True,
            ),
            "body": ParagraphStyle(
                "MetaBody",
                parent=base["BodyText"],
                fontName=_FONT_REGULAR,
                fontSize=9.2,
                leading=13.5,
                textColor=_TEXT,
                splitLongWords=True,
            ),
            "small": ParagraphStyle(
                "MetaSmall",
                parent=base["BodyText"],
                fontName=_FONT_REGULAR,
                fontSize=7.8,
                leading=11,
                textColor=_MUTED,
                splitLongWords=True,
            ),
            "label": ParagraphStyle(
                "MetaLabel",
                parent=base["BodyText"],
                fontName=_FONT_BOLD,
                fontSize=8.2,
                leading=11,
                textColor=_MUTED,
            ),
            "rating_title": ParagraphStyle(
                "MetaRatingTitle",
                parent=base["Heading3"],
                fontName=_FONT_BOLD,
                fontSize=10.3,
                leading=14,
                textColor=_TEXT,
            ),
            "score": ParagraphStyle(
                "MetaScore",
                parent=base["BodyText"],
                fontName=_FONT_BOLD,
                fontSize=8.4,
                leading=11,
                alignment=TA_CENTER,
            ),
        }

    def _story(
        self,
        *,
        feedback_run: StoredFeedbackRun,
        evaluation: StoredFeedbackEvaluation,
        display_name: str,
        styles: dict[str, ParagraphStyle],
    ) -> list[object]:
        story: list[object] = [
            Paragraph(
                "Meta-Bewertung des KI-Schreibfeedbacks",
                styles["title"],
            ),
            Paragraph(_paragraph_text(display_name), styles["subtitle"]),
            self._summary_box(evaluation, styles),
            Paragraph("Bewertung", styles["section"]),
            self._metadata_table(
                [
                    ("Bewertungsart", evaluation.type_label),
                    ("Optionaler Name", evaluation.evaluation_name or "-"),
                    ("Erstellt", _format_datetime(evaluation.created_at)),
                    ("Bewertungsbogen", evaluation.rubric_version),
                    (
                        "Ausgangsbewertung",
                        evaluation.source_evaluation_id or "-",
                    ),
                ],
                styles,
            ),
            Paragraph("Bewerteter Feedbacklauf", styles["section"]),
            self._metadata_table(
                [
                    ("Aufgabe", feedback_run.task_title),
                    ("Feedback-Vorlage", feedback_run.rubric_title),
                    ("Feedbackart", feedback_run.feedback_mode_label),
                    *(
                        [
                            (
                                "Erzeugungsprompt",
                                feedback_run.generation_prompt_version
                                or "Nicht gespeichert",
                            )
                        ]
                        if feedback_run.is_standard_feedback
                        else []
                    ),
                    ("Feedbackanbieter", feedback_run.provider),
                    ("Feedbackmodell", feedback_run.model),
                    (
                        "Denktiefe",
                        feedback_run.reasoning_effort or "Nicht gespeichert",
                    ),
                    (
                        "Feedback erstellt",
                        _format_datetime(feedback_run.created_at),
                    ),
                    (
                        "Feedbackdauer",
                        _format_duration(feedback_run.duration_ms),
                    ),
                ],
                styles,
            ),
        ]

        if evaluation.evaluator_provider:
            story.extend(
                [
                    Paragraph(
                        "Automatische Vorbewertung",
                        styles["section"],
                    ),
                    self._metadata_table(
                        [
                            (
                                "Bewertungsanbieter",
                                evaluation.evaluator_provider,
                            ),
                            (
                                "Bewertungsmodell",
                                evaluation.evaluator_model
                                or "Nicht verfügbar",
                            ),
                            (
                                "Denkmodus",
                                evaluation.evaluator_reasoning_mode
                                or "Standard",
                            ),
                            (
                                "Denkaufwand",
                                evaluation.evaluator_reasoning_effort
                                or "Modellstandard",
                            ),
                            (
                                "Prompt-Version",
                                evaluation.evaluator_prompt_version
                                or "Nicht verfügbar",
                            ),
                            (
                                "Bewertungsdauer",
                                _format_duration(evaluation.duration_ms),
                            ),
                            (
                                "Request-ID",
                                evaluation.provider_request_id
                                or "Nicht verfügbar",
                            ),
                        ],
                        styles,
                    ),
                ]
            )

        for position, rating in enumerate(evaluation.ratings, start=1):
            story.extend(
                self._rating_block(
                    position=position,
                    rating=rating,
                    styles=styles,
                )
            )

        story.extend(
            [
                Paragraph("Technische Zuordnung", styles["section"]),
                self._metadata_table(
                    [
                        ("Bewertungs-ID", evaluation.evaluation_id),
                        ("Feedbacklauf-ID", feedback_run.feedback_run_id),
                    ],
                    styles,
                ),
                Spacer(1, 8),
                Table(
                    [
                        [
                            Paragraph(
                                "Dieser Export enthält die gespeicherte "
                                "Meta-Bewertung und ihre technische "
                                "Zuordnung. Der anonymisierte Schülertext "
                                "und der vollständige Originaltext sind "
                                "nicht Bestandteil des PDFs.",
                                styles["small"],
                            )
                        ]
                    ],
                    colWidths=[174 * mm],
                    style=TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, -1), _INFO_SURFACE),
                            ("BOX", (0, 0), (-1, -1), 0.6, _PRIMARY),
                            ("LEFTPADDING", (0, 0), (-1, -1), 9),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                            ("TOPPADDING", (0, 0), (-1, -1), 8),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                        ]
                    ),
                ),
            ]
        )
        return story

    @staticmethod
    def _summary_box(
        evaluation: StoredFeedbackEvaluation,
        styles: dict[str, ParagraphStyle],
    ) -> Table:
        average = _format_score(evaluation.average_score)
        return Table(
            [
                [
                    Paragraph(
                        "Arithmetischer Mittelwert dieser Bewertung",
                        styles["label"],
                    ),
                    Paragraph(
                        f"{average} / 3",
                        styles["rating_title"],
                    ),
                ],
                [
                    Paragraph(
                        "Rein berechneter Orientierungswert - keine "
                        "separat gespeicherte Gesamtnote.",
                        styles["small"],
                    ),
                    "",
                ],
            ],
            colWidths=[130 * mm, 44 * mm],
            style=TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), _INFO_SURFACE),
                    ("BOX", (0, 0), (-1, -1), 0.8, _PRIMARY),
                    ("SPAN", (0, 1), (1, 1)),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ]
            ),
        )

    @staticmethod
    def _metadata_table(
        rows: list[tuple[str, object]],
        styles: dict[str, ParagraphStyle],
    ) -> Table:
        table_rows = [
            [
                Paragraph(_paragraph_text(label), styles["label"]),
                Paragraph(_paragraph_text(value), styles["body"]),
            ]
            for label, value in rows
        ]
        return Table(
            table_rows,
            colWidths=[45 * mm, 129 * mm],
            repeatRows=0,
            style=TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), _SURFACE),
                    ("BOX", (0, 0), (-1, -1), 0.5, _BORDER),
                    ("INNERGRID", (0, 0), (-1, -1), 0.3, _BORDER),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            ),
        )

    @staticmethod
    def _rating_block(
        *,
        position: int,
        rating: FeedbackEvaluationRating,
        styles: dict[str, ParagraphStyle],
    ) -> list[object]:
        score_text, score_background, score_border = _SCORE_COLORS[
            rating.score
        ]
        score_style = ParagraphStyle(
            f"MetaScore{rating.score}",
            parent=styles["score"],
            textColor=score_text,
        )
        header = Table(
            [
                [
                    Paragraph(
                        f"{position}. {_paragraph_text(rating.criterion_title)}",
                        styles["rating_title"],
                    ),
                    Paragraph(
                        f"{rating.score}/3 - "
                        f"{_paragraph_text(rating.rating_label)}",
                        score_style,
                    ),
                ]
            ],
            colWidths=[125 * mm, 49 * mm],
            style=TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("BACKGROUND", (1, 0), (1, 0), score_background),
                    ("BOX", (1, 0), (1, 0), 0.7, score_border),
                    ("LEFTPADDING", (0, 0), (0, 0), 0),
                    ("RIGHTPADDING", (0, 0), (0, 0), 8),
                    ("TOPPADDING", (0, 0), (0, 0), 5),
                    ("BOTTOMPADDING", (0, 0), (0, 0), 5),
                    ("LEFTPADDING", (1, 0), (1, 0), 6),
                    ("RIGHTPADDING", (1, 0), (1, 0), 6),
                    ("TOPPADDING", (1, 0), (1, 0), 5),
                    ("BOTTOMPADDING", (1, 0), (1, 0), 5),
                ]
            ),
        )
        block: list[object] = [
            Spacer(1, 5),
            header,
            Spacer(1, 4),
            Paragraph(
                "Prüffrage: "
                f"{_paragraph_text(rating.criterion_question)}",
                styles["small"],
            ),
            Spacer(1, 4),
            Paragraph(
                "<b>Begründung:</b> "
                f"{_paragraph_text(rating.justification)}",
                styles["body"],
            ),
            Spacer(1, 8),
        ]

        if position == 1:
            block.insert(
                0,
                Paragraph("Kriterienbewertungen", styles["section"]),
            )

        return [KeepTogether(block)]
