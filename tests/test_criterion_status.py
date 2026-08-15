from __future__ import annotations

import unittest

from app.domain.criterion_status import (
    CRITERION_STATUS_LABELS,
    criterion_status_display_label,
    criterion_status_display_text,
)


class CriterionStatusTests(unittest.TestCase):
    def test_formative_display_labels_do_not_replace_internal_labels(
        self,
    ) -> None:
        expected_display_labels = {
            "met": "Klar erkennbar",
            "mostly_met": "Weitgehend erkennbar",
            "partially_met": "Teilweise erkennbar",
            "not_met": "Noch nicht erkennbar",
            "not_assessable": "Keine sichere Einordnung",
        }

        for status, expected_label in expected_display_labels.items():
            with self.subTest(status=status):
                self.assertEqual(
                    criterion_status_display_label(status),
                    expected_label,
                )

        self.assertEqual(CRITERION_STATUS_LABELS["met"], "Erfüllt")
        self.assertEqual(
            CRITERION_STATUS_LABELS["not_assessable"],
            "Nicht beurteilbar",
        )
        self.assertEqual(
            criterion_status_display_text(
                "Überwiegend erfüllt; ein Punkt ist „Nicht beurteilbar“."
            ),
            "Weitgehend erkennbar; ein Punkt ist „Keine sichere "
            "Einordnung“.",
        )


if __name__ == "__main__":
    unittest.main()
