from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from app.domain.rubric import FeedbackTask, FeedbackTaskDraft


RUBRIC_EXPORT_FORMAT = "ki-schreibfeedback-rubrics"
RUBRIC_EXPORT_VERSION = 1
RUBRIC_EXPORT_TYPE_SINGLE = "single"
RUBRIC_EXPORT_TYPE_COLLECTION = "collection"
MAX_RUBRIC_IMPORT_BYTES = 5 * 1024 * 1024
MAX_RUBRIC_IMPORT_TASKS = 200
RUBRIC_BUNDLE_FORMAT = "ki-schreibfeedback-rubric-bundle"
RUBRIC_BUNDLE_VERSION = 1
RUBRIC_BUNDLE_MANIFEST_NAME = "manifest.json"
MAX_RUBRIC_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_RUBRIC_BUNDLE_PARTS = 100
MAX_RUBRIC_BUNDLE_MANIFEST_BYTES = 64 * 1024
MAX_RUBRIC_BUNDLE_TASKS = 5000

_BUNDLE_SIGNATURES = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
)


class RubricExchangeError(ValueError):
    """Die importierte JSON-Datei besitzt kein gültiges Austauschformat."""


class RubricExchangeService:
    """Erzeugt und validiert portable Bewertungsbogen-Exporte."""

    @classmethod
    def export_task(cls, task: FeedbackTask) -> bytes:
        content = cls._export_document(
            export_type=RUBRIC_EXPORT_TYPE_SINGLE,
            tasks=(task,),
        )
        cls._validate_export_part(content, task_count=1)
        return content

    @classmethod
    def export_collection(
        cls,
        tasks: Sequence[FeedbackTask],
    ) -> bytes:
        content = cls._export_document(
            export_type=RUBRIC_EXPORT_TYPE_COLLECTION,
            tasks=tasks,
        )

        cls._validate_export_part(
            content,
            task_count=len(tasks),
        )
        return content

    @classmethod
    def export_collection_bundle(
        cls,
        tasks: Sequence[FeedbackTask],
    ) -> bytes:
        """Erzeugt ein rundreisefestes ZIP-Paket aus begrenzten JSON-Teilen."""

        task_tuple = tuple(tasks)

        if not task_tuple:
            raise RubricExchangeError(
                "Es sind keine Bewertungsbögen für den Export vorhanden."
            )
        if len(task_tuple) > MAX_RUBRIC_BUNDLE_TASKS:
            raise RubricExchangeError(
                "Ein Gesamtexport darf höchstens "
                f"{MAX_RUBRIC_BUNDLE_TASKS} Bewertungsbögen enthalten."
            )

        exported_at = datetime.now(timezone.utc).isoformat()
        parts = cls._collection_parts(
            task_tuple,
            exported_at=exported_at,
        )

        if len(parts) > MAX_RUBRIC_BUNDLE_PARTS:
            raise RubricExchangeError(
                "Die Bewertungsbögen benötigen zu viele Exportteile."
            )

        manifest_parts: list[dict[str, object]] = []

        for position, (part_tasks, content) in enumerate(
            parts,
            start=1,
        ):
            manifest_parts.append(
                {
                    "name": cls._bundle_part_name(position),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                    "task_count": len(part_tasks),
                }
            )

        manifest = cls._json_bytes(
            {
                "format": RUBRIC_BUNDLE_FORMAT,
                "format_version": RUBRIC_BUNDLE_VERSION,
                "export_type": RUBRIC_EXPORT_TYPE_COLLECTION,
                "exported_at": exported_at,
                "total_task_count": len(task_tuple),
                "parts": manifest_parts,
            }
        )

        if len(manifest) > MAX_RUBRIC_BUNDLE_MANIFEST_BYTES:
            raise RubricExchangeError(
                "Das Inhaltsverzeichnis des Gesamtexports ist zu groß."
            )

        archive_buffer = io.BytesIO()

        try:
            with zipfile.ZipFile(
                archive_buffer,
                mode="w",
                compression=zipfile.ZIP_STORED,
                allowZip64=False,
            ) as archive:
                cls._write_bundle_member(
                    archive,
                    RUBRIC_BUNDLE_MANIFEST_NAME,
                    manifest,
                )

                for part, (_part_tasks, content) in zip(
                    manifest_parts,
                    parts,
                    strict=True,
                ):
                    cls._write_bundle_member(
                        archive,
                        str(part["name"]),
                        content,
                    )
        except (OSError, ValueError, zipfile.LargeZipFile) as exc:
            raise RubricExchangeError(
                "Der Gesamtexport konnte nicht als Paket erstellt werden."
            ) from exc

        bundle = archive_buffer.getvalue()

        if len(bundle) > MAX_RUBRIC_BUNDLE_BYTES:
            raise RubricExchangeError(
                "Der Gesamtexport überschreitet die sichere Paketgröße "
                "von 64 MiB."
            )

        return bundle

    @staticmethod
    def is_bundle_content(content: bytes) -> bool:
        return any(
            content.startswith(signature)
            for signature in _BUNDLE_SIGNATURES
        )

    @classmethod
    def parse_import(
        cls,
        content: bytes,
    ) -> tuple[FeedbackTaskDraft, ...]:
        if cls.is_bundle_content(content):
            return cls._parse_bundle(content)

        return cls._parse_json_document(content)

    @classmethod
    def _parse_json_document(
        cls,
        content: bytes,
        *,
        required_export_type: str | None = None,
    ) -> tuple[FeedbackTaskDraft, ...]:
        if not content:
            raise RubricExchangeError(
                "Die ausgewählte Importdatei ist leer."
            )
        if len(content) > MAX_RUBRIC_IMPORT_BYTES:
            raise RubricExchangeError(
                "Die Importdatei darf höchstens 5 MiB groß sein."
            )

        try:
            decoded = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise RubricExchangeError(
                "Die Importdatei muss als UTF-8-JSON gespeichert sein."
            ) from exc

        try:
            document = json.loads(
                decoded,
                object_pairs_hook=cls._object_without_duplicate_keys,
            )
        except RubricExchangeError:
            raise
        except (json.JSONDecodeError, RecursionError) as exc:
            raise RubricExchangeError(
                "Die ausgewählte Datei enthält kein gültiges JSON."
            ) from exc

        if not isinstance(document, Mapping):
            raise RubricExchangeError(
                "Die Importdatei muss ein JSON-Objekt enthalten."
            )
        if document.get("format") != RUBRIC_EXPORT_FORMAT:
            raise RubricExchangeError(
                "Die Datei ist kein Bewertungsbogen-Export dieser Anwendung."
            )

        format_version = document.get("format_version")

        if (
            type(format_version) is not int
            or format_version != RUBRIC_EXPORT_VERSION
        ):
            raise RubricExchangeError(
                "Die Formatversion der Importdatei wird nicht unterstützt."
            )

        export_type = document.get("export_type")

        if export_type not in {
            RUBRIC_EXPORT_TYPE_SINGLE,
            RUBRIC_EXPORT_TYPE_COLLECTION,
        }:
            raise RubricExchangeError(
                "Der Exporttyp der Importdatei ist ungültig."
            )
        if (
            required_export_type is not None
            and export_type != required_export_type
        ):
            raise RubricExchangeError(
                "Ein Teil des Gesamtpakets besitzt den falschen Exporttyp."
            )

        tasks = document.get("tasks")

        if not isinstance(tasks, list) or not tasks:
            raise RubricExchangeError(
                "Die Importdatei enthält keine Bewertungsbögen."
            )
        if len(tasks) > MAX_RUBRIC_IMPORT_TASKS:
            raise RubricExchangeError(
                "Eine Importdatei darf höchstens "
                f"{MAX_RUBRIC_IMPORT_TASKS} Bewertungsbögen enthalten."
            )
        if (
            export_type == RUBRIC_EXPORT_TYPE_SINGLE
            and len(tasks) != 1
        ):
            raise RubricExchangeError(
                "Ein Einzelexport muss genau einen Bewertungsbogen enthalten."
            )

        return tuple(
            cls._parse_task(task, position)
            for position, task in enumerate(tasks, start=1)
        )

    @classmethod
    def _parse_bundle(
        cls,
        content: bytes,
    ) -> tuple[FeedbackTaskDraft, ...]:
        if len(content) > MAX_RUBRIC_BUNDLE_BYTES:
            raise RubricExchangeError(
                "Ein Gesamtpaket darf höchstens 64 MiB groß sein."
            )

        try:
            with zipfile.ZipFile(io.BytesIO(content), mode="r") as archive:
                infos = archive.infolist()
                names = [info.filename for info in infos]

                if len(names) != len(set(names)):
                    raise RubricExchangeError(
                        "Das Gesamtpaket enthält Dateinamen doppelt."
                    )
                if len(infos) > MAX_RUBRIC_BUNDLE_PARTS + 1:
                    raise RubricExchangeError(
                        "Das Gesamtpaket enthält zu viele Dateien."
                    )

                info_by_name = {
                    info.filename: info
                    for info in infos
                }
                manifest_info = info_by_name.get(
                    RUBRIC_BUNDLE_MANIFEST_NAME
                )

                if manifest_info is None:
                    raise RubricExchangeError(
                        "Das Gesamtpaket enthält kein Inhaltsverzeichnis."
                    )

                cls._validate_bundle_member(
                    manifest_info,
                    maximum_size=MAX_RUBRIC_BUNDLE_MANIFEST_BYTES,
                )
                manifest_content = archive.read(manifest_info)
                manifest = cls._load_json_mapping(
                    manifest_content,
                    invalid_message=(
                        "Das Inhaltsverzeichnis des Gesamtpakets ist "
                        "ungültig."
                    ),
                )
                part_descriptions, total_task_count = (
                    cls._parse_bundle_manifest(manifest)
                )
                expected_names = {
                    RUBRIC_BUNDLE_MANIFEST_NAME,
                    *(
                        str(part["name"])
                        for part in part_descriptions
                    ),
                }

                if set(names) != expected_names:
                    raise RubricExchangeError(
                        "Das Gesamtpaket enthält unerwartete oder fehlende "
                        "Dateien."
                    )

                drafts: list[FeedbackTaskDraft] = []
                total_part_bytes = 0

                for part in part_descriptions:
                    part_name = str(part["name"])
                    part_info = info_by_name[part_name]
                    expected_size = int(part["size_bytes"])
                    cls._validate_bundle_member(
                        part_info,
                        maximum_size=MAX_RUBRIC_IMPORT_BYTES,
                    )

                    if part_info.file_size != expected_size:
                        raise RubricExchangeError(
                            f"Der Exportteil '{part_name}' besitzt eine "
                            "unerwartete Größe."
                        )

                    total_part_bytes += part_info.file_size

                    if total_part_bytes > MAX_RUBRIC_BUNDLE_BYTES:
                        raise RubricExchangeError(
                            "Die entpackten Exportteile sind insgesamt zu "
                            "groß."
                        )

                    part_content = archive.read(part_info)
                    actual_digest = hashlib.sha256(
                        part_content
                    ).hexdigest()

                    if actual_digest != part["sha256"]:
                        raise RubricExchangeError(
                            f"Der Exportteil '{part_name}' ist beschädigt "
                            "oder wurde verändert."
                        )

                    part_drafts = cls._parse_json_document(
                        part_content,
                        required_export_type=(
                            RUBRIC_EXPORT_TYPE_COLLECTION
                        ),
                    )

                    if len(part_drafts) != part["task_count"]:
                        raise RubricExchangeError(
                            f"Die Mengenangabe für '{part_name}' stimmt "
                            "nicht."
                        )

                    drafts.extend(part_drafts)

                if len(drafts) != total_task_count:
                    raise RubricExchangeError(
                        "Die Gesamtanzahl der Bewertungsbögen stimmt nicht."
                    )

                return tuple(drafts)

        except RubricExchangeError:
            raise
        except (
            OSError,
            RuntimeError,
            NotImplementedError,
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
        ) as exc:
            raise RubricExchangeError(
                "Die ausgewählte Datei ist kein gültiges Gesamtpaket."
            ) from exc

    @classmethod
    def _parse_bundle_manifest(
        cls,
        manifest: Mapping[object, object],
    ) -> tuple[list[Mapping[object, object]], int]:
        if manifest.get("format") != RUBRIC_BUNDLE_FORMAT:
            raise RubricExchangeError(
                "Die Datei ist kein Bewertungsbogen-Gesamtpaket dieser "
                "Anwendung."
            )

        version = manifest.get("format_version")

        if type(version) is not int or version != RUBRIC_BUNDLE_VERSION:
            raise RubricExchangeError(
                "Die Formatversion des Gesamtpakets wird nicht unterstützt."
            )
        if manifest.get("export_type") != RUBRIC_EXPORT_TYPE_COLLECTION:
            raise RubricExchangeError(
                "Der Exporttyp des Gesamtpakets ist ungültig."
            )

        total_task_count = manifest.get("total_task_count")

        if (
            type(total_task_count) is not int
            or total_task_count < 1
            or total_task_count > MAX_RUBRIC_BUNDLE_TASKS
        ):
            raise RubricExchangeError(
                "Die Gesamtanzahl der Bewertungsbögen ist ungültig."
            )

        parts = manifest.get("parts")

        if (
            not isinstance(parts, list)
            or not parts
            or len(parts) > MAX_RUBRIC_BUNDLE_PARTS
        ):
            raise RubricExchangeError(
                "Das Gesamtpaket enthält keine gültige Teileliste."
            )

        parsed_parts: list[Mapping[object, object]] = []
        described_task_count = 0

        for position, part in enumerate(parts, start=1):
            if not isinstance(part, Mapping):
                raise RubricExchangeError(
                    f"Exportteil {position} ist kein JSON-Objekt."
                )

            expected_name = cls._bundle_part_name(position)
            name = part.get("name")
            digest = part.get("sha256")
            size_bytes = part.get("size_bytes")
            task_count = part.get("task_count")

            if name != expected_name:
                raise RubricExchangeError(
                    f"Exportteil {position} besitzt einen ungültigen Namen."
                )
            if (
                not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise RubricExchangeError(
                    f"Exportteil {position} besitzt keine gültige Prüfsumme."
                )
            if (
                type(size_bytes) is not int
                or size_bytes < 1
                or size_bytes > MAX_RUBRIC_IMPORT_BYTES
            ):
                raise RubricExchangeError(
                    f"Exportteil {position} besitzt eine ungültige Größe."
                )
            if (
                type(task_count) is not int
                or task_count < 1
                or task_count > MAX_RUBRIC_IMPORT_TASKS
            ):
                raise RubricExchangeError(
                    f"Exportteil {position} besitzt eine ungültige Anzahl."
                )

            described_task_count += task_count
            parsed_parts.append(part)

        if described_task_count != total_task_count:
            raise RubricExchangeError(
                "Die Mengenangaben im Gesamtpaket widersprechen sich."
            )

        return parsed_parts, total_task_count

    @classmethod
    def _collection_parts(
        cls,
        tasks: tuple[FeedbackTask, ...],
        *,
        exported_at: str,
    ) -> tuple[tuple[tuple[FeedbackTask, ...], bytes], ...]:
        parts: list[tuple[tuple[FeedbackTask, ...], bytes]] = []

        for start in range(0, len(tasks), MAX_RUBRIC_IMPORT_TASKS):
            cls._append_sized_collection_parts(
                parts,
                tasks[start:start + MAX_RUBRIC_IMPORT_TASKS],
                exported_at=exported_at,
            )

        return tuple(parts)

    @classmethod
    def _append_sized_collection_parts(
        cls,
        target: list[tuple[tuple[FeedbackTask, ...], bytes]],
        tasks: tuple[FeedbackTask, ...],
        *,
        exported_at: str,
    ) -> None:
        content = cls._export_document(
            export_type=RUBRIC_EXPORT_TYPE_COLLECTION,
            tasks=tasks,
            exported_at=exported_at,
        )

        if len(content) <= MAX_RUBRIC_IMPORT_BYTES:
            target.append((tasks, content))
            return

        if len(tasks) == 1:
            raise RubricExchangeError(
                "Ein einzelner Bewertungsbogen überschreitet die sichere "
                "Größe eines Exportteils."
            )

        middle = len(tasks) // 2
        cls._append_sized_collection_parts(
            target,
            tasks[:middle],
            exported_at=exported_at,
        )
        cls._append_sized_collection_parts(
            target,
            tasks[middle:],
            exported_at=exported_at,
        )

    @staticmethod
    def _bundle_part_name(position: int) -> str:
        return f"parts/bewertungsboegen-{position:04d}.json"

    @staticmethod
    def _write_bundle_member(
        archive: zipfile.ZipFile,
        name: str,
        content: bytes,
    ) -> None:
        info = zipfile.ZipInfo(
            filename=name,
            date_time=(1980, 1, 1, 0, 0, 0),
        )
        info.compress_type = zipfile.ZIP_STORED
        info.create_system = 3
        info.external_attr = 0o100600 << 16
        archive.writestr(info, content)

    @staticmethod
    def _validate_bundle_member(
        info: zipfile.ZipInfo,
        *,
        maximum_size: int,
    ) -> None:
        if info.is_dir() or info.file_size < 1:
            raise RubricExchangeError(
                f"Die Paketdatei '{info.filename}' ist leer oder ungültig."
            )
        if info.file_size > maximum_size:
            raise RubricExchangeError(
                f"Die Paketdatei '{info.filename}' ist zu groß."
            )
        if info.flag_bits & 0x1:
            raise RubricExchangeError(
                "Verschlüsselte Dateien werden im Gesamtpaket nicht "
                "unterstützt."
            )
        if info.compress_type != zipfile.ZIP_STORED:
            raise RubricExchangeError(
                "Komprimierte Fremdpakete werden aus Sicherheitsgründen "
                "nicht importiert."
            )
        if info.compress_size != info.file_size:
            raise RubricExchangeError(
                f"Die Größenangaben für '{info.filename}' sind ungültig."
            )

    @classmethod
    def _load_json_mapping(
        cls,
        content: bytes,
        *,
        invalid_message: str,
    ) -> Mapping[object, object]:
        try:
            decoded = content.decode("utf-8-sig")
            value = json.loads(
                decoded,
                object_pairs_hook=cls._object_without_duplicate_keys,
            )
        except RubricExchangeError:
            raise
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
        ) as exc:
            raise RubricExchangeError(invalid_message) from exc

        if not isinstance(value, Mapping):
            raise RubricExchangeError(invalid_message)

        return value

    @staticmethod
    def _validate_export_part(
        content: bytes,
        *,
        task_count: int,
    ) -> None:
        if task_count < 1:
            raise RubricExchangeError(
                "Es sind keine Bewertungsbögen für den Export vorhanden."
            )
        if task_count > MAX_RUBRIC_IMPORT_TASKS:
            raise RubricExchangeError(
                "Ein einzelner JSON-Export darf höchstens "
                f"{MAX_RUBRIC_IMPORT_TASKS} Bewertungsbögen enthalten. "
                "Verwende für größere Bestände das Gesamtpaket."
            )
        if len(content) > MAX_RUBRIC_IMPORT_BYTES:
            raise RubricExchangeError(
                "Ein einzelner JSON-Export darf höchstens 5 MiB groß sein. "
                "Verwende für größere Bestände das Gesamtpaket."
            )

    @classmethod
    def _export_document(
        cls,
        *,
        export_type: str,
        tasks: Sequence[FeedbackTask],
        exported_at: str | None = None,
    ) -> bytes:
        document = {
            "format": RUBRIC_EXPORT_FORMAT,
            "format_version": RUBRIC_EXPORT_VERSION,
            "export_type": export_type,
            "exported_at": (
                exported_at
                or datetime.now(timezone.utc).isoformat()
            ),
            "tasks": [
                cls._serialize_task(task)
                for task in tasks
            ],
        }
        return cls._json_bytes(document)

    @staticmethod
    def _json_bytes(document: Mapping[str, object]) -> bytes:
        return (
            json.dumps(
                document,
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")

    @staticmethod
    def _serialize_task(task: FeedbackTask) -> dict[str, object]:
        return {
            "title": task.title,
            "subject": task.subject,
            "grade_level": task.grade_level,
            "instructions": task.instructions,
            "material": task.material,
            "source_archived": task.archived_at is not None,
            "rubric": {
                "title": task.rubric.title,
                "criteria": [
                    {"text": criterion.text}
                    for criterion in task.rubric.criteria
                ],
            },
        }

    @classmethod
    def _parse_task(
        cls,
        value: object,
        position: int,
    ) -> FeedbackTaskDraft:
        if not isinstance(value, Mapping):
            raise RubricExchangeError(
                f"Bewertungsbogen {position} ist kein JSON-Objekt."
            )

        rubric = value.get("rubric")

        if not isinstance(rubric, Mapping):
            raise RubricExchangeError(
                f"Bewertungsbogen {position} enthält keinen gültigen Bogen."
            )

        criteria = rubric.get("criteria")

        if not isinstance(criteria, list):
            raise RubricExchangeError(
                f"Bewertungsbogen {position} enthält keine Kriterienliste."
            )

        criterion_texts: list[str] = []

        for criterion_position, criterion in enumerate(
            criteria,
            start=1,
        ):
            if not isinstance(criterion, Mapping):
                raise RubricExchangeError(
                    "Kriterium "
                    f"{criterion_position} in Bewertungsbogen {position} "
                    "ist kein JSON-Objekt."
                )

            criterion_texts.append(
                cls._required_string(
                    criterion,
                    "text",
                    "Kriterium "
                    f"{criterion_position} in Bewertungsbogen {position}",
                )
            )

        source_archived = value.get("source_archived", False)

        if type(source_archived) is not bool:
            raise RubricExchangeError(
                f"Bewertungsbogen {position} enthält einen ungültigen "
                "Archivstatus."
            )

        return FeedbackTaskDraft(
            title=cls._required_string(
                value,
                "title",
                f"Bewertungsbogen {position}",
            ),
            subject=cls._optional_string(
                value,
                "subject",
                f"Bewertungsbogen {position}",
            ),
            grade_level=cls._optional_string(
                value,
                "grade_level",
                f"Bewertungsbogen {position}",
            ),
            instructions=cls._required_string(
                value,
                "instructions",
                f"Bewertungsbogen {position}",
            ),
            material=cls._optional_string(
                value,
                "material",
                f"Bewertungsbogen {position}",
            ),
            rubric_title=cls._required_string(
                rubric,
                "title",
                f"Bewertungsbogen {position}",
            ),
            criteria=tuple(criterion_texts),
        )

    @staticmethod
    def _required_string(
        value: Mapping[object, object],
        key: str,
        context: str,
    ) -> str:
        field_value = value.get(key)

        if not isinstance(field_value, str):
            raise RubricExchangeError(
                f"{context}: Das Feld '{key}' fehlt oder ist kein Text."
            )

        return field_value

    @staticmethod
    def _optional_string(
        value: Mapping[object, object],
        key: str,
        context: str,
    ) -> str:
        field_value = value.get(key, "")

        if not isinstance(field_value, str):
            raise RubricExchangeError(
                f"{context}: Das Feld '{key}' muss ein Text sein."
            )

        return field_value

    @staticmethod
    def _object_without_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}

        for key, value in pairs:
            if key in result:
                raise RubricExchangeError(
                    f"Die Importdatei enthält das Feld '{key}' doppelt."
                )

            result[key] = value

        return result
