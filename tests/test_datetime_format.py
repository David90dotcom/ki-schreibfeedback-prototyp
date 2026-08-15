from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.datetime_format import format_datetime_german


class GermanDatetimeFormatTests(unittest.TestCase):
    def test_summer_timestamp_is_displayed_as_mesz(self) -> None:
        value = datetime(
            2026,
            8,
            15,
            10,
            56,
            tzinfo=timezone.utc,
        )

        self.assertEqual(
            format_datetime_german(value),
            "15.08.2026, 12:56 MESZ",
        )

    def test_winter_timestamp_is_displayed_as_mez(self) -> None:
        value = datetime(
            2026,
            1,
            15,
            10,
            56,
            tzinfo=timezone.utc,
        )

        self.assertEqual(
            format_datetime_german(value),
            "15.01.2026, 11:56 MEZ",
        )

    def test_naive_timestamp_is_interpreted_as_utc(self) -> None:
        value = datetime(2026, 8, 15, 10, 56)

        self.assertEqual(
            format_datetime_german(value),
            "15.08.2026, 12:56 MESZ",
        )

    def test_missing_timestamp_has_readable_fallback(self) -> None:
        self.assertEqual(
            format_datetime_german(None),
            "Nicht verfügbar",
        )
