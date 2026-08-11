from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from app.domain.rubric import FeedbackTask, Rubric, RubricCriterion
from app.services.rubric_exchange_service import (
    MAX_RUBRIC_IMPORT_BYTES,
    RUBRIC_EXPORT_FORMAT,
    RUBRIC_EXPORT_VERSION,
    RubricExchangeError,
    RubricExchangeService,
)


class RubricExchangeServiceTests(unittest.TestCase):
    def _task(self) -> FeedbackTask:
        timestamp = datetime.now(timezone.utc)
        return FeedbackTask(
            task_id="interne-aufgaben-id",
            title="Gedichtinterpretation Klasse 8",
            subject="Deutsch",
            grade_level="8",
            instructions="Interpretiere das Gedicht.",
            material="Ein kurzes Beispielgedicht.",
            rubric=Rubric(
                rubric_id="interne-bogen-id",
                title="Grundanforderungen Gedichtinterpretation",
                criteria=(
                    RubricCriterion(
                        criterion_id="interne-kriterium-id-1",
                        text="Einleitung mit Titel und Autor",
                        position=0,
                    ),
                    RubricCriterion(
                        criterion_id="interne-kriterium-id-2",
                        text="Sprachliche Bilder erläutern",
                        position=1,
                    ),
                ),
            ),
            created_at=timestamp,
            updated_at=timestamp,
        )

    def test_single_export_round_trip_omits_internal_ids(self) -> None:
        source = self._task()

        content = RubricExchangeService.export_task(source)
        decoded = content.decode("utf-8")
        document = json.loads(decoded)

        self.assertEqual(document["format"], RUBRIC_EXPORT_FORMAT)
        self.assertEqual(
            document["format_version"],
            RUBRIC_EXPORT_VERSION,
        )
        self.assertEqual(document["export_type"], "single")
        self.assertNotIn(source.task_id, decoded)
        self.assertNotIn(source.rubric.rubric_id, decoded)
        self.assertNotIn(
            source.rubric.criteria[0].criterion_id,
            decoded,
        )

        drafts = RubricExchangeService.parse_import(content)

        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].title, source.title)
        self.assertEqual(drafts[0].material, source.material)
        self.assertEqual(drafts[0].rubric_title, source.rubric.title)
        self.assertEqual(
            drafts[0].criteria,
            tuple(item.text for item in source.rubric.criteria),
        )

    def test_collection_marks_archived_sources_but_imports_content(self) -> None:
        active = self._task()
        archived = replace(
            self._task(),
            task_id="archivierte-aufgaben-id",
            archived_at=datetime.now(timezone.utc),
        )

        content = RubricExchangeService.export_collection(
            (active, archived)
        )
        document = json.loads(content)

        self.assertEqual(document["export_type"], "collection")
        self.assertFalse(document["tasks"][0]["source_archived"])
        self.assertTrue(document["tasks"][1]["source_archived"])
        self.assertEqual(
            len(RubricExchangeService.parse_import(content)),
            2,
        )

    def test_import_accepts_utf8_byte_order_mark(self) -> None:
        content = RubricExchangeService.export_task(self._task())

        drafts = RubricExchangeService.parse_import(
            b"\xef\xbb\xbf" + content
        )

        self.assertEqual(drafts[0].subject, "Deutsch")

    def test_import_rejects_wrong_version_and_duplicate_keys(self) -> None:
        invalid_documents = (
            json.dumps(
                {
                    "format": RUBRIC_EXPORT_FORMAT,
                    "format_version": 999,
                    "export_type": "single",
                    "tasks": [],
                }
            ).encode("utf-8"),
            (
                '{"format":"ki-schreibfeedback-rubrics",'
                '"format":"ki-schreibfeedback-rubrics",'
                '"format_version":1,"export_type":"single",'
                '"tasks":[]}'
            ).encode("utf-8"),
        )

        for content in invalid_documents:
            with self.subTest(content=content):
                with self.assertRaises(RubricExchangeError):
                    RubricExchangeService.parse_import(content)

    def test_import_rejects_oversized_or_structurally_invalid_files(
        self,
    ) -> None:
        invalid_structure = json.dumps(
            {
                "format": RUBRIC_EXPORT_FORMAT,
                "format_version": RUBRIC_EXPORT_VERSION,
                "export_type": "single",
                "tasks": [
                    {
                        "title": "Aufgabe",
                        "subject": "Deutsch",
                        "grade_level": "8",
                        "instructions": "Bearbeite die Aufgabe.",
                        "material": "",
                        "rubric": {
                            "title": "Feedback",
                            "criteria": ["Kein Kriterienobjekt"],
                        },
                    }
                ],
            }
        ).encode("utf-8")

        with self.assertRaises(RubricExchangeError):
            RubricExchangeService.parse_import(invalid_structure)

        with self.assertRaisesRegex(RubricExchangeError, "5 MiB"):
            RubricExchangeService.parse_import(
                b"x" * (MAX_RUBRIC_IMPORT_BYTES + 1)
            )
