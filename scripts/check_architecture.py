from __future__ import annotations

import asyncio
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODEL_CATALOG_PATH = PROJECT_ROOT / "config" / "models.yaml"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


async def run_checks() -> None:
    print("Prüfe die neue Version-0.2-Architektur.")
    print("Es werden keine Modell-APIs aufgerufen.\n")

    print("[1/7] Python-Importe")

    from app.config import settings
    from app.domain.analysis import AnalysisPayload
    from app.domain.model_catalog import (
        ModelDefinition,
        ProviderDefinition,
    )
    from app.llm.catalog import load_model_registry
    from app.llm.provider_factory import (
        create_default_provider_factory,
    )
    from app.services.analysis_run_repository import (
        AnalysisRunRepository,
    )
    from app.services.analysis_run_store import (
        AnalysisRunStore,
    )
    from app.services.analysis_service import AnalysisService
    from app.services.metrics_service import MetricsService

    _ = AnalysisService
    _ = MetricsService

    print("      OK")

    print("[2/7] YAML-Datei laden")

    if not MODEL_CATALOG_PATH.is_file():
        raise FileNotFoundError(
            (
                "Der Modellkatalog wurde nicht gefunden: "
                f"{MODEL_CATALOG_PATH}"
            )
        )

    with MODEL_CATALOG_PATH.open(
        "r",
        encoding="utf-8",
    ) as catalog_file:
        catalog_data = yaml.safe_load(catalog_file)

    if not isinstance(catalog_data, dict):
        raise ValueError(
            "config/models.yaml muss ein YAML-Objekt enthalten."
        )

    print("      OK")

    print("[3/7] Provider und Modelle validieren")

    raw_providers = catalog_data.get("providers")
    raw_models = catalog_data.get("models")

    if not isinstance(raw_providers, list):
        raise ValueError(
            "Der YAML-Eintrag 'providers' muss eine Liste sein."
        )

    if not isinstance(raw_models, list):
        raise ValueError(
            "Der YAML-Eintrag 'models' muss eine Liste sein."
        )

    providers = [
        ProviderDefinition.model_validate(raw_provider)
        for raw_provider in raw_providers
    ]
    models = [
        ModelDefinition.model_validate(raw_model)
        for raw_model in raw_models
    ]

    provider_ids = {
        provider.id
        for provider in providers
    }

    for model in models:
        if model.provider_id not in provider_ids:
            raise ValueError(
                (
                    f"Modell '{model.id}' verweist auf den "
                    "unbekannten Provider "
                    f"'{model.provider_id}'."
                )
            )

    print(
        f"      OK: {len(providers)} Provider, "
        f"{len(models)} Modelle"
    )

    print("[4/7] ModelRegistry aufbauen")

    registry = load_model_registry(
        MODEL_CATALOG_PATH
    )

    if registry is None:
        raise RuntimeError(
            "load_model_registry() hat keine Registry geliefert."
        )

    print("      OK")

    print("[5/7] Strukturiertes Analyseformat erzeugen")

    analysis_schema: dict[str, Any] = (
        AnalysisPayload.model_json_schema()
    )

    if analysis_schema.get("type") != "object":
        raise ValueError(
            "AnalysisPayload erzeugt kein JSON-Objektschema."
        )

    print("      OK")

    print("[6/7] Provider-Factory konfigurieren")

    provider_factory = create_default_provider_factory(
        settings
    )

    availability = (
        provider_factory.list_availability()
    )

    if not availability:
        raise RuntimeError(
            "Es wurden keine Provider registriert."
        )

    for provider_status in availability:
        status = (
            "konfiguriert"
            if provider_status.configured
            else "nicht konfiguriert"
        )

        print(
            f"      {provider_status.provider_id}: {status}"
        )

    await provider_factory.close_all()

    print("[7/7] Austauschbaren Analysespeicher prüfen")

    with tempfile.TemporaryDirectory() as temporary_directory:
        database_path = (
            Path(temporary_directory)
            / "architecture-test.sqlite3"
        )

        run_store = AnalysisRunStore(database_path)

        if not isinstance(
            run_store,
            AnalysisRunRepository,
        ):
            raise TypeError(
                (
                    "AnalysisRunStore erfüllt die allgemeine "
                    "AnalysisRunRepository-Schnittstelle nicht."
                )
            )

        await run_store.initialize()

        if not database_path.is_file():
            raise RuntimeError(
                "Die Testdatenbank wurde nicht angelegt."
            )

    print("      OK")

    print(
        "\nArchitekturprüfung erfolgreich. "
        "Es wurden keine Modellanfragen ausgeführt."
    )


def main() -> int:
    try:
        asyncio.run(run_checks())
        return 0

    except ValidationError as exc:
        print(
            "\nValidierungsfehler:",
            file=sys.stderr,
        )
        print(
            exc,
            file=sys.stderr,
        )
        return 2

    except Exception:
        print(
            "\nArchitekturprüfung fehlgeschlagen:",
            file=sys.stderr,
        )
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())