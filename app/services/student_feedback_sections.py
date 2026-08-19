from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_TITLE_HEADING_RULES = (
    (
        (
            "interpret",
            "deutung",
            "stilmittel",
            "sprachliche mittel",
            "wirkung",
            "bildlich",
            "lyrisch",
        ),
        "Deine Interpretation",
    ),
    (
        ("rechtschreib", "grammatik", "zeichensetz", "sprachrichtig"),
        "Deine sprachliche Richtigkeit",
    ),
    (("lesbar", "handschrift"), "Deine Lesbarkeit"),
    (
        ("aufbau", "struktur", "glieder", "einleitung", "hauptteil", "schluss"),
        "Dein Aufbau",
    ),
    (("argument", "begrund", "erorter"), "Deine Argumentation"),
    (("ausdruck", "formulier", "sprachstil"), "Dein Ausdruck"),
    (
        ("inhalt", "zusammenfass", "wiedergab", "nacherzahl"),
        "Deine inhaltliche Darstellung",
    ),
    (("beleg", "zitat"), "Deine Belegführung"),
)

_CONTEXT_HEADING_RULES = (
    (
        (
            "interpret",
            "deutung",
            "stilmittel",
            "sprachliche mittel",
            "wirkung",
            "bildlich",
            "metapher",
            "lyrisch",
        ),
        "Deine Interpretation",
    ),
    (
        ("rechtschreib", "grammatik", "zeichensetz", "sprachrichtig"),
        "Deine sprachliche Richtigkeit",
    ),
    (("lesbar", "handschrift"), "Deine Lesbarkeit"),
    (("argument", "begrund", "erorter"), "Deine Argumentation"),
    (
        ("inhalt", "zusammenfass", "wiedergab", "nacherzahl"),
        "Deine inhaltliche Darstellung",
    ),
    (("beleg", "zitat"), "Deine Belegführung"),
)


class StudentFeedbackSectionsError(ValueError):
    """Die Modellantwort enthält keine gültige Viererstruktur."""


@dataclass(frozen=True)
class StudentFeedbackSections:
    """Vier beleggebundene Teile eines kriterienbezogenen Feedbacks."""

    staerke: str
    rueckmeldung: str
    naechster_schritt: str
    formulierungshilfen: tuple[str, ...] = ()

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, object],
    ) -> StudentFeedbackSections:
        raw_formulation_helps = payload.get("formulierungshilfen")

        if not isinstance(raw_formulation_helps, list):
            raise StudentFeedbackSectionsError(
                "Das Feld 'formulierungshilfen' muss eine Liste sein."
            )

        formulation_helps: list[str] = []

        for raw_help in raw_formulation_helps:
            if not isinstance(raw_help, str) or not raw_help.strip():
                raise StudentFeedbackSectionsError(
                    "Eine Formulierungshilfe besitzt ein ungültiges Format."
                )
            formulation_helps.append(raw_help.strip())

        return cls(
            staerke=cls._required_text(payload, "staerke"),
            rueckmeldung=cls._required_text(payload, "rueckmeldung"),
            naechster_schritt=cls._required_text(
                payload,
                "naechster_schritt",
            ),
            formulierungshilfen=tuple(formulation_helps),
        )

    def as_markdown(
        self,
        *,
        criterion_title: str,
        criterion_text: str,
    ) -> str:
        """Formatiert die Teile in der verbindlichen sichtbaren Reihenfolge."""

        sections = [
            ("Das gelingt dir schon", self.staerke),
            (
                feedback_heading_for_criterion(
                    criterion_title=criterion_title,
                    criterion_text=criterion_text,
                ),
                self.rueckmeldung,
            ),
            ("Daran kannst du weiterarbeiten", self.naechster_schritt),
        ]

        if self.formulierungshilfen:
            formulation_help = "  \n".join(
                f"{index}. {help_text}"
                for index, help_text in enumerate(
                    self.formulierungshilfen,
                    start=1,
                )
            )
            sections.append(("Formulierungshilfe", formulation_help))

        return "\n\n".join(
            f"**{heading}:**  \n{text}"
            for heading, text in sections
        )

    @staticmethod
    def _required_text(
        payload: dict[str, object],
        key: str,
    ) -> str:
        value = payload.get(key)

        if not isinstance(value, str) or not value.strip():
            raise StudentFeedbackSectionsError(
                f"Das Feld '{key}' fehlt oder ist leer."
            )

        return value.strip()


def feedback_heading_for_criterion(
    *,
    criterion_title: str,
    criterion_text: str,
) -> str:
    """Wählt eine passende zweite Überschrift ohne Modellinterpretation."""

    normalized_title = _normalized(criterion_title)
    normalized_context = _normalized(f"{criterion_title} {criterion_text}")

    # Eine eindeutige Kriterienüberschrift hat Vorrang vor Begriffen aus der
    # Erläuterung. So wird etwa ein Ausdruckskriterium nicht allein deshalb
    # als Interpretation beschriftet, weil sein Text auch "Wirkung" nennt.
    title_heading = _heading_from_rules(
        normalized_title,
        _TITLE_HEADING_RULES,
    )
    if title_heading is not None:
        return title_heading

    context_heading = _heading_from_rules(
        normalized_context,
        _CONTEXT_HEADING_RULES,
    )
    if context_heading is not None:
        return context_heading

    return "Deine Bearbeitung"


def unverified_student_feedback_sections(
    *,
    explanation: str,
    next_step: str,
) -> StudentFeedbackSections:
    """Hält auch technische Sicherheitsfälle in der neuen Darstellung."""

    return StudentFeedbackSections(
        staerke=(
            "Eine konkrete Stärke lässt sich hier nicht sicher belegen, "
            "ohne etwas über deinen Text zu behaupten."
        ),
        rueckmeldung=explanation,
        naechster_schritt=next_step,
        formulierungshilfen=(),
    )


def open_formulation_helps_for_criterion(
    *,
    criterion_title: str,
    criterion_text: str,
) -> tuple[str, ...]:
    """Liefert nur einen offenen, inhaltlich nicht ergänzten Satzanfang."""

    heading = feedback_heading_for_criterion(
        criterion_title=criterion_title,
        criterion_text=criterion_text,
    )

    if heading == "Deine Interpretation":
        return ("Durch ... wird deutlich, dass ...",)
    if heading == "Deine Argumentation":
        return ("Das zeigt sich daran, dass ...",)

    return ()


def _normalized(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )
    return re.sub(r"\s+", " ", without_marks).strip()


def _contains_any(value: str, *needles: str) -> bool:
    return any(needle in value for needle in needles)


def _heading_from_rules(
    value: str,
    rules: tuple[tuple[tuple[str, ...], str], ...],
) -> str | None:
    for needles, heading in rules:
        if _contains_any(value, *needles):
            return heading

    return None
