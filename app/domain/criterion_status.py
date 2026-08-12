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


def criterion_status_label(value: object) -> str:
    """Übersetzt einen gespeicherten Status ohne Rohcode-Leckage."""

    if not isinstance(value, str):
        return "Nicht verfügbar"

    return CRITERION_STATUS_LABELS.get(
        value,
        "Unbekannter Erfüllungsstand",
    )
