from __future__ import annotations

import hashlib
import io
import json
import unittest
import warnings
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from app.domain.rubric import FeedbackTask, Rubric, RubricCriterion
from app.services import rubric_exchange_service as exchange
from app.services.rubric_exchange_service import (
    MAX_RUBRIC_BUNDLE_PARTS,
    MAX_RUBRIC_BUNDLE_TASKS,
    MAX_RUBRIC_IMPORT_BYTES,
    MAX_RUBRIC_IMPORT_TASKS,
    RUBRIC_BUNDLE_FORMAT,
    RUBRIC_BUNDLE_MANIFEST_NAME,
    RubricExchangeError,
    RubricExchangeService,
)


class RubricExchangeBundleTests(unittest.TestCase):
    @staticmethod
    def _task(
        position: int,
        *,
        maximum_payload: bool = False,
    ) -> FeedbackTask:
        timestamp = datetime(2026, 8, 11, tzinfo=timezone.utc)
        criterion_count = 30 if maximum_payload else 1
        criterion_text = (
            "K" * 1500
            if maximum_payload
            else f"Gültiges Kriterium {position}"
        )

        return FeedbackTask(
            task_id=f"interne-aufgaben-id-{position}",
            title=f"Aufgabe {position}",
            subject="Deutsch",
            grade_level="8",
            instructions=(
                "A" * 12000
                if maximum_payload
                else "Bearbeite die Aufgabe."
            ),
            material=("M" * 30000 if maximum_payload else ""),
            rubric=Rubric(
                rubric_id=f"interne-bogen-id-{position}",
                title=f"Feedback {position}",
                criteria=tuple(
                    RubricCriterion(
                        criterion_id=(
                            f"interne-kriterium-id-{position}-{index}"
                        ),
                        text=criterion_text,
                        position=index,
                    )
                    for index in range(criterion_count)
                ),
            ),
            created_at=timestamp,
            updated_at=timestamp,
        )

    @staticmethod
    def _members(bundle: bytes) -> dict[str, bytes]:
        with zipfile.ZipFile(io.BytesIO(bundle), mode="r") as archive:
            return {
                info.filename: archive.read(info)
                for info in archive.infolist()
            }

    @staticmethod
    def _archive(
        members: dict[str, bytes],
        *,
        compression: int = zipfile.ZIP_STORED,
    ) -> bytes:
        buffer = io.BytesIO()

        with zipfile.ZipFile(
            buffer,
            mode="w",
            compression=compression,
            allowZip64=False,
        ) as archive:
            for name, content in members.items():
                archive.writestr(
                    name,
                    content,
                    compress_type=compression,
                )

        return buffer.getvalue()

    @staticmethod
    def _json_bytes(value: object) -> bytes:
        return (
            json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")

    def test_201_small_tasks_are_split_and_round_trip(self) -> None:
        tasks = tuple(self._task(position) for position in range(201))

        bundle = RubricExchangeService.export_collection_bundle(tasks)
        drafts = RubricExchangeService.parse_import(bundle)
        members = self._members(bundle)
        manifest = json.loads(members[RUBRIC_BUNDLE_MANIFEST_NAME])

        self.assertTrue(bundle.startswith(b"PK\x03\x04"))
        self.assertEqual(len(drafts), 201)
        self.assertEqual(
            [draft.title for draft in drafts],
            [task.title for task in tasks],
        )
        self.assertEqual(manifest["total_task_count"], 201)
        self.assertEqual(len(manifest["parts"]), 2)
        self.assertEqual(
            [part["task_count"] for part in manifest["parts"]],
            [200, 1],
        )

    def test_more_than_five_mib_of_valid_tasks_round_trips(self) -> None:
        tasks = tuple(
            self._task(position, maximum_payload=True)
            for position in range(60)
        )

        with self.assertRaisesRegex(RubricExchangeError, "5 MiB"):
            RubricExchangeService.export_collection(tasks)

        bundle = RubricExchangeService.export_collection_bundle(tasks)
        drafts = RubricExchangeService.parse_import(bundle)

        self.assertGreater(len(bundle), MAX_RUBRIC_IMPORT_BYTES)
        self.assertEqual(len(drafts), 60)
        self.assertEqual(len(drafts[0].instructions), 12000)
        self.assertEqual(len(drafts[0].material), 30000)
        self.assertEqual(len(drafts[0].criteria), 30)
        self.assertEqual(len(drafts[0].criteria[0]), 1500)

    def test_manifest_parts_obey_limits_and_omit_internal_ids(self) -> None:
        tasks = tuple(self._task(position) for position in range(201))
        bundle = RubricExchangeService.export_collection_bundle(tasks)
        members = self._members(bundle)
        manifest = json.loads(members[RUBRIC_BUNDLE_MANIFEST_NAME])

        self.assertEqual(manifest["format"], RUBRIC_BUNDLE_FORMAT)
        self.assertEqual(manifest["format_version"], 1)
        self.assertEqual(manifest["export_type"], "collection")
        self.assertLessEqual(
            len(manifest["parts"]),
            MAX_RUBRIC_BUNDLE_PARTS,
        )

        exported_content = b""

        for part in manifest["parts"]:
            content = members[part["name"]]
            document = json.loads(content)
            exported_content += content

            self.assertLessEqual(len(content), MAX_RUBRIC_IMPORT_BYTES)
            self.assertLessEqual(
                part["task_count"],
                MAX_RUBRIC_IMPORT_TASKS,
            )
            self.assertEqual(part["size_bytes"], len(content))
            self.assertEqual(
                part["sha256"],
                hashlib.sha256(content).hexdigest(),
            )
            self.assertEqual(
                part["task_count"],
                len(document["tasks"]),
            )

        for task in tasks:
            self.assertNotIn(task.task_id.encode("utf-8"), exported_content)
            self.assertNotIn(
                task.rubric.rubric_id.encode("utf-8"),
                exported_content,
            )
            self.assertNotIn(
                task.rubric.criteria[0].criterion_id.encode("utf-8"),
                exported_content,
            )

    def test_unicode_and_archived_source_marker_round_trip(self) -> None:
        base = self._task(1)
        source = replace(
            base,
            title="Résumé – Gedicht über Großstadt 🌆",
            material="Ä, Ö, Ü, ß · 日本語",
            rubric=replace(
                base.rubric,
                title="Prüfbogen № 1",
                criteria=(
                    replace(
                        base.rubric.criteria[0],
                        title="Schluss: Titelbezug",
                        text="Erkläre die Wirkung von ‚vorbei‘.",
                    ),
                ),
            ),
            archived_at=base.updated_at,
        )

        bundle = RubricExchangeService.export_collection_bundle((source,))
        draft = RubricExchangeService.parse_import(bundle)[0]
        members = self._members(bundle)
        manifest = json.loads(members[RUBRIC_BUNDLE_MANIFEST_NAME])
        part = json.loads(members[manifest["parts"][0]["name"]])

        self.assertEqual(draft.title, source.title)
        self.assertEqual(draft.material, source.material)
        self.assertEqual(draft.rubric_title, source.rubric.title)
        self.assertEqual(draft.criteria[0], source.rubric.criteria[0].text)
        self.assertEqual(
            draft.criterion_titles[0],
            source.rubric.criteria[0].title,
        )
        self.assertTrue(part["tasks"][0]["source_archived"])

    def test_changed_part_is_rejected_by_checksum(self) -> None:
        bundle = RubricExchangeService.export_collection_bundle(
            (self._task(1),)
        )
        members = self._members(bundle)
        manifest = json.loads(members[RUBRIC_BUNDLE_MANIFEST_NAME])
        part_name = manifest["parts"][0]["name"]
        members[part_name] = members[part_name].replace(
            b"Aufgabe 1",
            b"Aufgabe X",
            1,
        )

        with self.assertRaisesRegex(
            RubricExchangeError,
            "beschädigt|verändert",
        ):
            RubricExchangeService.parse_import(self._archive(members))

    def test_inconsistent_manifest_is_rejected(self) -> None:
        bundle = RubricExchangeService.export_collection_bundle(
            (self._task(1), self._task(2))
        )
        members = self._members(bundle)
        manifest = json.loads(members[RUBRIC_BUNDLE_MANIFEST_NAME])
        manifest["total_task_count"] += 1
        members[RUBRIC_BUNDLE_MANIFEST_NAME] = self._json_bytes(manifest)

        with self.assertRaisesRegex(
            RubricExchangeError,
            "Mengenangaben",
        ):
            RubricExchangeService.parse_import(self._archive(members))

    def test_unexpected_and_compressed_members_are_rejected(self) -> None:
        bundle = RubricExchangeService.export_collection_bundle(
            (self._task(1),)
        )
        members = self._members(bundle)

        unexpected_members = dict(members)
        unexpected_members["../fremde-datei.json"] = b"{}"

        invalid_bundles = (
            self._archive(unexpected_members),
            self._archive(members, compression=zipfile.ZIP_DEFLATED),
        )

        for invalid_bundle in invalid_bundles:
            with self.subTest(size=len(invalid_bundle)):
                with self.assertRaises(RubricExchangeError):
                    RubricExchangeService.parse_import(invalid_bundle)

    def test_duplicate_member_names_are_rejected(self) -> None:
        bundle = RubricExchangeService.export_collection_bundle(
            (self._task(1),)
        )
        members = self._members(bundle)
        duplicate_name = next(
            name
            for name in members
            if name != RUBRIC_BUNDLE_MANIFEST_NAME
        )
        buffer = io.BytesIO()

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)

            with zipfile.ZipFile(buffer, mode="w") as archive:
                for name, content in members.items():
                    archive.writestr(name, content)

                archive.writestr(duplicate_name, members[duplicate_name])

        with self.assertRaisesRegex(RubricExchangeError, "doppelt"):
            RubricExchangeService.parse_import(buffer.getvalue())

    def test_bundle_size_is_checked_before_archive_parsing(self) -> None:
        with patch.object(exchange, "MAX_RUBRIC_BUNDLE_BYTES", 8):
            with self.assertRaisesRegex(
                RubricExchangeError,
                "höchstens 64 MiB",
            ):
                RubricExchangeService.parse_import(
                    b"PK\x03\x04" + b"x" * 5
                )

    def test_export_never_returns_a_bundle_above_its_import_limit(
        self,
    ) -> None:
        tasks = tuple(self._task(position) for position in range(201))
        valid_bundle = RubricExchangeService.export_collection_bundle(tasks)

        with patch.object(
            exchange,
            "MAX_RUBRIC_BUNDLE_BYTES",
            len(valid_bundle) - 1,
        ):
            with self.assertRaisesRegex(
                RubricExchangeError,
                "Paketgröße",
            ):
                RubricExchangeService.export_collection_bundle(tasks)

    def test_export_rejects_empty_excessive_or_single_oversized_input(
        self,
    ) -> None:
        small_task = self._task(1)
        oversized_task = FeedbackTask(
            task_id="oversized-task",
            title=small_task.title,
            subject=small_task.subject,
            grade_level=small_task.grade_level,
            instructions=small_task.instructions,
            material="M" * (MAX_RUBRIC_IMPORT_BYTES + 1),
            rubric=small_task.rubric,
            created_at=small_task.created_at,
            updated_at=small_task.updated_at,
        )

        invalid_inputs = (
            (),
            (small_task,) * (MAX_RUBRIC_BUNDLE_TASKS + 1),
            (oversized_task,),
        )

        for tasks in invalid_inputs:
            with self.subTest(task_count=len(tasks)):
                with self.assertRaises(RubricExchangeError):
                    RubricExchangeService.export_collection_bundle(tasks)


if __name__ == "__main__":
    unittest.main()
