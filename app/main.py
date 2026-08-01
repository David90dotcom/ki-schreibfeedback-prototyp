from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import (
    FastAPI,
    Form,
    HTTPException,
    Request,
)
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.domain.analysis import (
    AnalysisInput,
    AnalysisRequest,
    AnalysisResult,
)
from app.domain.metrics import ErrorType
from app.domain.model_catalog import ModelParameters
from app.llm.catalog import load_model_registry
from app.llm.errors import ProviderError
from app.llm.openai_client import OpenAIProvider
from app.llm.provider_factory import (
    ProviderFactoryError,
    create_default_provider_factory,
)
from app.services.analysis_run_store import (
    AnalysisRunStore,
    AnalysisRunStoreError,
)
from app.services.analysis_service import AnalysisService
from app.services.feedback_service import (
    FeedbackResult,
    FeedbackService,
)
from app.services.metrics_service import MetricsService


LOGGER = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent


def resolve_project_path(
    configured_path: str | Path | None,
    fallback: Path,
) -> Path:
    if configured_path is None:
        return fallback

    path = Path(configured_path)

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    return path.resolve()


MODEL_CATALOG_PATH = resolve_project_path(
    getattr(
        settings,
        "model_catalog_path",
        None,
    ),
    PROJECT_ROOT / "config" / "models.yaml",
)

ANALYSIS_DATABASE_PATH = resolve_project_path(
    getattr(
        settings,
        "metrics_database_path",
        None,
    ),
    PROJECT_ROOT
    / "data"
    / "analysis_runs.sqlite3",
)

PERSIST_ANALYSIS_RUNS = bool(
    getattr(
        settings,
        "persist_analysis_runs",
        getattr(
            settings,
            "persist_metrics",
            True,
        ),
    )
)

PROMPT_VERSION = str(
    getattr(
        settings,
        "prompt_version",
        "feedback-prompt-v1",
    )
)

SCHEMA_VERSION = str(
    getattr(
        settings,
        "analysis_schema_version",
        getattr(
            settings,
            "schema_version",
            "analysis-schema-v1",
        ),
    )
)


model_registry = load_model_registry(
    MODEL_CATALOG_PATH
)

provider_factory = create_default_provider_factory(
    settings
)

metrics_service = MetricsService()

analysis_service = AnalysisService(
    metrics_service=metrics_service,
    prompt_version=PROMPT_VERSION,
    schema_version=SCHEMA_VERSION,
)

analysis_run_store = AnalysisRunStore(
    ANALYSIS_DATABASE_PATH
)


def build_legacy_providers() -> dict[str, Any]:
    """
    Stellt die bisherigen Provider für FeedbackService bereit.

    Wenn OpenAI nicht konfiguriert ist, bleibt der Provider trotzdem
    vorhanden und liefert erst bei seiner Auswahl eine verständliche
    Fehlermeldung.
    """

    providers: dict[str, Any] = {
        "ollama": provider_factory.get("ollama"),
    }

    openai_availability = (
        provider_factory.get_availability(
            "openai"
        )
    )

    if openai_availability.selectable:
        providers["openai"] = (
            provider_factory.get("openai")
        )
    else:
        providers["openai"] = OpenAIProvider(
            api_key=settings.openai_api_key,
            model_name=settings.openai_model,
        )

    return providers


feedback_service = FeedbackService(
    providers=build_legacy_providers(),
    max_input_chars=settings.max_input_chars,
)


@asynccontextmanager
async def lifespan(
    _: FastAPI,
) -> AsyncIterator[None]:
    if PERSIST_ANALYSIS_RUNS:
        await analysis_run_store.initialize()

    yield

    try:
        await provider_factory.close_all()
    except ProviderFactoryError:
        LOGGER.exception(
            "Provider konnten beim Herunterfahren nicht vollständig geschlossen werden."
        )


app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
)

app.mount(
    "/static",
    StaticFiles(directory=BASE_DIR / "static"),
    name="static",
)

templates = Jinja2Templates(
    directory=BASE_DIR / "templates"
)


@app.get(
    "/",
    response_class=HTMLResponse,
)
async def index(
    request: Request,
) -> HTMLResponse:
    """Bisherige Benutzeroberfläche der Version 0.1."""

    return templates.TemplateResponse(
        name="index.html",
        request=request,
        context={
            "app_name": settings.app_name,
            "provider_options": (
                feedback_service
                .get_provider_options()
            ),
            "selected_provider": "ollama",
            "student_text": "",
            "result": None,
            "error": None,
        },
    )


@app.post(
    "/analyze",
    response_class=HTMLResponse,
)
async def analyze(
    request: Request,
    student_text: str = Form(...),
    provider: str = Form(...),
) -> HTMLResponse:
    """Bisheriger Analyseweg der Version 0.1."""

    result: FeedbackResult | None = None
    error: str | None = None

    try:
        result = await feedback_service.analyze_text(
            student_text=student_text,
            provider_key=provider,
        )
    except Exception as exc:
        error = str(exc)

    return templates.TemplateResponse(
        name="index.html",
        request=request,
        context={
            "app_name": settings.app_name,
            "provider_options": (
                feedback_service
                .get_provider_options()
            ),
            "selected_provider": provider,
            "student_text": student_text,
            "result": result,
            "error": error,
        },
    )


@app.get("/api/models")
async def list_models() -> dict[str, Any]:
    """
    Liefert alle aktivierten und lokal konfigurierten Modelloptionen.

    Geheimnisse und API-Schlüssel werden nicht ausgegeben.
    """

    model_options: list[dict[str, Any]] = []

    for model in model_registry.list_models(
        enabled_only=True
    ):
        provider = model_registry.require_provider(
            model.provider_id
        )

        availability = (
            provider_factory.get_availability(
                provider.id
            )
        )

        model_options.append(
            {
                "model_id": model.id,
                "display_name": model.display_name,
                "description": model.description,
                "provider": {
                    "provider_id": provider.id,
                    "display_name": (
                        provider.display_name
                    ),
                    "processing_location": (
                        provider
                        .processing_location
                        .value
                    ),
                },
                "capabilities": (
                    model.capabilities.model_dump(
                        mode="json"
                    )
                ),
                "default_parameters": (
                    model
                    .default_parameters
                    .model_dump(
                        mode="json"
                    )
                ),
                "selectable": (
                    availability.selectable
                ),
                "availability_message": (
                    availability.message
                ),
            }
        )

    return {
        "models": model_options,
    }


@app.post(
    "/api/analysis",
    response_model=AnalysisResult,
)
async def analyze_api(
    analysis_request: AnalysisRequest,
) -> AnalysisResult:
    """Neuer strukturierter Analyse-Endpunkt der Version 0.2."""

    analysis_input = prepare_analysis_input(
        analysis_request.analysis_input
    )

    try:
        resolved_model = model_registry.resolve(
            analysis_request.model_id,
            require_enabled=True,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error_type": "model_not_found",
                "message": str(exc),
            },
        ) from exc

    availability = (
        provider_factory.get_availability(
            resolved_model.provider.id
        )
    )

    if not availability.selectable:
        raise HTTPException(
            status_code=503,
            detail={
                "error_type": (
                    "provider_unavailable"
                ),
                "message": availability.message,
            },
        )

    validate_model_for_analysis(
        analysis_request=analysis_request,
        capabilities=(
            resolved_model.model.capabilities
        ),
    )

    try:
        provider = provider_factory.get(
            resolved_model.provider.id
        )
    except ProviderFactoryError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error_type": (
                    "provider_unavailable"
                ),
                "message": str(exc),
            },
        ) from exc

    parameters = merge_model_parameters(
        defaults=(
            resolved_model
            .model
            .default_parameters
        ),
        overrides=analysis_request.parameters,
    )

    execution = await analysis_service.analyze(
        analysis_input=analysis_input,
        provider=provider,
        model_id=resolved_model.model.id,
        provider_model_name=(
            resolved_model
            .model
            .provider_model_name
        ),
        parameters=parameters,
        stream=analysis_request.stream,
    )

    if PERSIST_ANALYSIS_RUNS:
        try:
            await analysis_run_store.save(
                execution.run_record
            )
        except AnalysisRunStoreError as exc:
            raise HTTPException(
                status_code=500,
                detail={
                    "error_type": (
                        "metrics_persistence"
                    ),
                    "message": (
                        "Der Analyselauf konnte nicht "
                        "gespeichert werden."
                    ),
                },
            ) from exc

    if execution.error is not None:
        raise provider_http_exception(
            error=execution.error,
            run_id=str(
                execution.run_record.run_id
            ),
        )

    feedback = execution.require_output()

    return AnalysisResult(
        submission_id=(
            analysis_input
            .submission
            .submission_id
        ),
        feedback=feedback,
        run_record=execution.run_record,
    )


@app.get(
    "/api/analysis-runs",
)
async def list_analysis_runs(
    limit: int = 100,
    provider_id: str | None = None,
    model_id: str | None = None,
    success: bool | None = None,
) -> dict[str, Any]:
    """Liefert technische Laufdaten für spätere Vergleiche."""

    if not PERSIST_ANALYSIS_RUNS:
        raise HTTPException(
            status_code=503,
            detail={
                "error_type": (
                    "metrics_persistence_disabled"
                ),
                "message": (
                    "Die Speicherung technischer "
                    "Messwerte ist deaktiviert."
                ),
            },
        )

    try:
        records = await analysis_run_store.list_runs(
            limit=limit,
            provider_id=provider_id,
            model_id=model_id,
            success=success,
        )
    except (
        AnalysisRunStoreError,
        ValueError,
    ) as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error_type": "invalid_request",
                "message": str(exc),
            },
        ) from exc

    return {
        "runs": [
            record.model_dump(mode="json")
            for record in records
        ],
    }


def prepare_analysis_input(
    analysis_input: AnalysisInput,
) -> AnalysisInput:
    """Prüft Eingabegrenzen und entfernt deaktivierte Kriterien."""

    student_text = (
        analysis_input.submission.text.strip()
    )

    if not student_text:
        raise HTTPException(
            status_code=422,
            detail={
                "error_type": "invalid_request",
                "message": (
                    "Der Schülertext darf nicht leer sein."
                ),
            },
        )

    if len(student_text) > settings.max_input_chars:
        raise HTTPException(
            status_code=422,
            detail={
                "error_type": "invalid_request",
                "message": (
                    "Der Schülertext ist zu lang. "
                    "Erlaubt sind maximal "
                    f"{settings.max_input_chars} Zeichen."
                ),
            },
        )

    enabled_criteria = tuple(
        criterion
        for criterion in analysis_input.criteria
        if criterion.enabled
    )

    max_criteria = int(
        getattr(
            settings,
            "max_criteria",
            20,
        )
    )

    if len(enabled_criteria) > max_criteria:
        raise HTTPException(
            status_code=422,
            detail={
                "error_type": "invalid_request",
                "message": (
                    "Es dürfen maximal "
                    f"{max_criteria} Kriterien "
                    "analysiert werden."
                ),
            },
        )

    return analysis_input.model_copy(
        update={
            "submission": (
                analysis_input
                .submission
                .model_copy(
                    update={
                        "text": student_text,
                    }
                )
            ),
            "criteria": enabled_criteria,
        }
    )


def merge_model_parameters(
    *,
    defaults: ModelParameters,
    overrides: ModelParameters | None,
) -> ModelParameters:
    """Verbindet Katalogvorgaben mit expliziten Requestparametern."""

    if overrides is None:
        return defaults

    merged_data = defaults.model_dump(
        mode="python"
    )
    merged_data.update(
        overrides.model_dump(
            mode="python",
            exclude_unset=True,
        )
    )

    return ModelParameters.model_validate(
        merged_data
    )


def validate_model_for_analysis(
    *,
    analysis_request: AnalysisRequest,
    capabilities: Any,
) -> None:
    """Prüft die für den Analyseweg notwendigen Fähigkeiten."""

    if not capabilities.structured_output:
        raise HTTPException(
            status_code=422,
            detail={
                "error_type": (
                    "unsupported_capability"
                ),
                "message": (
                    "Das ausgewählte Modell unterstützt "
                    "keine strukturierten Ausgaben."
                ),
            },
        )

    if (
        analysis_request.stream
        and not capabilities.streaming
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "error_type": (
                    "unsupported_capability"
                ),
                "message": (
                    "Das ausgewählte Modell unterstützt "
                    "kein Streaming."
                ),
            },
        )


def provider_http_exception(
    *,
    error: ProviderError,
    run_id: str,
) -> HTTPException:
    """Übersetzt Providerfehler in passende HTTP-Statuscodes."""

    status_codes = {
        ErrorType.INVALID_REQUEST: 422,
        ErrorType.RATE_LIMIT: 429,
        ErrorType.TIMEOUT: 504,
        ErrorType.CONNECTION: 503,
        ErrorType.PROVIDER_UNAVAILABLE: 503,
        ErrorType.AUTHENTICATION: 503,
        ErrorType.AUTHORIZATION: 503,
        ErrorType.MODEL_NOT_FOUND: 503,
        ErrorType.INVALID_RESPONSE: 502,
        ErrorType.STRUCTURED_OUTPUT: 502,
        ErrorType.CANCELLED: 499,
        ErrorType.UNKNOWN: 502,
    }

    return HTTPException(
        status_code=status_codes.get(
            error.error_type,
            502,
        ),
        detail={
            "run_id": run_id,
            "error_type": error.error_type.value,
            "message": error.message,
            "retryable": error.retryable,
        },
    )