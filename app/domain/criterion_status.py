from __future__ import annotations

from enum import Enum


class CriterionStatus(str, Enum):
    """Mögliche Erfüllungsstände eines Feedbackkriteriums."""

    MET = "met"
    MOSTLY_MET = "mostly_met"
    PARTIALLY_MET = "partially_met"
    NOT_MET = "not_met"
    NOT_ASSESSABLE = "not_assessable"


CRITERION_STATUS_LABELS = {
    CriterionStatus.MET.value: "Erfüllt",
    CriterionStatus.MOSTLY_MET.value: "Überwiegend erfüllt",
    CriterionStatus.PARTIALLY_MET.value: "Teilweise erfüllt",
    CriterionStatus.NOT_MET.value: "Nicht erfüllt",
    CriterionStatus.NOT_ASSESSABLE.value: "Nicht beurteilbar",
}


CRITERION_STATUS_DISPLAY_LABELS = {
    CriterionStatus.MET.value: "Klar erkennbar",
    CriterionStatus.MOSTLY_MET.value: "Weitgehend erkennbar",
    CriterionStatus.PARTIALLY_MET.value: "Teilweise erkennbar",
    CriterionStatus.NOT_MET.value: "Noch nicht erkennbar",
    CriterionStatus.NOT_ASSESSABLE.value: "Keine sichere Einordnung",
}


def criterion_status_label(value: object) -> str:
    """Übersetzt einen gespeicherten Status ohne Rohcode-Leckage."""

    if not isinstance(value, str):
        return "Nicht verfügbar"

    return CRITERION_STATUS_LABELS.get(
        value,
        "Unbekannter Erfüllungsstand",
    )


def criterion_status_display_label(value: object) -> str:
    """Liefert die formative Bezeichnung für die sichtbare Oberfläche."""

    if not isinstance(value, str):
        return "Keine Statusangabe"

    return CRITERION_STATUS_DISPLAY_LABELS.get(
        value,
        "Unbekannte Rückmeldestufe",
    )


def criterion_status_display_text(value: object) -> str:
    """Übersetzt Statusbezeichnungen in bereits gespeicherten Anzeigetexten."""

    if not isinstance(value, str):
        return ""

    display_text = value

    for status, internal_label in CRITERION_STATUS_LABELS.items():
        display_text = display_text.replace(
            internal_label,
            CRITERION_STATUS_DISPLAY_LABELS[status],
        )

    return display_text
