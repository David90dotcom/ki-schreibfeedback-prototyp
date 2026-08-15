import asyncio
import hashlib
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import (
    APP_MODE_LOCAL,
    APP_MODE_PRODUCTION,
    settings,
)
from app.domain.criterion_status import (
    criterion_status_display_label,
    criterion_status_display_text,
)
from app.domain.student_account import (
    IssuedStudentAccessCode,
    MAX_STUDENT_ACCOUNT_LABEL_CHARS,
    StudentAccount,
    StudentFeedbackConfiguration,
)
from app.domain.feedback_evaluation import (
    MANUAL_META_EVALUATION_RUBRIC,
    MAX_EVALUATION_NAME_CHARS,
    MAX_META_JUSTIFICATION_CHARS,
    META_EVALUATION_SCORE_OPTIONS,
)
from app.domain.rubric import FeedbackTask
from app.feedback_markdown import (
    render_feedback_inline_markdown,
    render_feedback_markdown,
)
from app.llm.base import LLMProvider
from app.llm.errors import (
    ProviderError,
    ProviderInvalidRequestError,
)
from app.llm.mistral_client import MistralProvider
from app.llm.ollama_client import OllamaProvider
from app.llm.openai_client import (
    OPENAI_REASONING_EFFORTS,
    OpenAIProvider,
)
from app.llm.openai_evaluation_client import (
    OPENAI_EVALUATION_REASONING_EFFORT,
    OPENAI_EVALUATION_REASONING_MODE,
    OPENAI_EVALUATION_REASONING_MODES,
    OpenAIAutomaticEvaluationProvider,
)
from app.llm.runpod_client import (
    RunPodJobStatusCallback,
    RunPodProvider,
)
from app.security import (
    LoginRateLimiter,
    authenticated_student_account_id,
    authenticated_student_access_version,
    authenticated_username,
    end_authenticated_session,
    get_or_create_csrf_token,
    is_valid_csrf_token,
    is_authenticated,
    start_authenticated_session,
    start_student_session,
    verify_credentials,
)
from app.services.automatic_feedback_evaluation_service import (
    AUTOMATIC_EVALUATION_PROMPT_VERSION,
    AutomaticFeedbackEvaluationService,
)
from app.services.criterion_wise_rubric_feedback_service import (
    CRITERION_REFRESH_OVERALL_FEEDBACK,
    CriterionWiseRubricFeedbackService,
)
from app.services.feedback_service import (
    FeedbackResult,
    FeedbackService,
)
from app.services.feedback_evaluation_exchange_service import (
    MAX_FEEDBACK_EVALUATION_IMPORT_BYTES,
    FeedbackEvaluationExchangeError,
    FeedbackEvaluationExchangeService,
)
from app.services.feedback_evaluation_pdf_service import (
    FeedbackEvaluationPdfError,
    FeedbackEvaluationPdfService,
)
from app.services.rubric_feedback_service import (
    RubricFeedbackResult,
    RubricFeedbackService,
)
from app.services.rubric_exchange_service import (
    MAX_RUBRIC_BUNDLE_BYTES,
    MAX_RUBRIC_BUNDLE_TASKS,
    MAX_RUBRIC_IMPORT_BYTES,
    RubricExchangeError,
    RubricExchangeService,
)
from app.services.runpod_job_store import (
    ACTIVE_RUNPOD_JOB_STATUSES,
    RunPodJobStore,
    RunPodJobStoreError,
    RunPodTrackedJob,
)
from app.services.runpod_status_service import RunPodStatusService
from app.services.student_account_store import (
    StudentAccountConflictError,
    StudentAccountNotFoundError,
    StudentAccountStore,
    StudentAccountStoreError,
)
from app.services.student_analysis_gate import (
    StudentAnalysisGate,
    StudentAnalysisInProgressError,
)
from app.services.task_store import (
    FeedbackEvaluationDeleteConflictError,
    FeedbackRunRefreshError,
    MAX_MATERIAL_CHARS,
    TaskNotFoundError,
    TaskStore,
    TaskStoreError,
)
from app.services.two_pass_rubric_feedback_service import (
    TwoPassRubricFeedbackService,
)


BASE_DIR = Path(__file__).resolve().parent
CUSTOM_MODEL_VALUE = "__custom__"
OLLAMA_FALLBACK_BASE_URL = "http://localhost:11434"
MAX_MODEL_NAME_CHARS = 200
RUBRIC_ANALYSIS_MODE_JOINT = "joint"
RUBRIC_ANALYSIS_MODE_CRITERION_WISE = "criterion_wise"
RUBRIC_ANALYSIS_MODE_TWO_PASS = "two_pass"
VALID_RUBRIC_ANALYSIS_MODES = {
    RUBRIC_ANALYSIS_MODE_JOINT,
    RUBRIC_ANALYSIS_MODE_CRITERION_WISE,
    RUBRIC_ANALYSIS_MODE_TWO_PASS,
}
SESSION_COOKIE_NAME = "ki-schreibfeedback-session"
RUNPOD_JOB_STATUS_REFRESH_TIMEOUT_SECONDS = 3.0
logger = logging.getLogger(__name__)


def _static_asset_version() -> str:
    """Erzeugt eine Cache-Kennung aus den ausgelieferten Frontend-Dateien."""

    digest = hashlib.sha256()

    for filename in (
        "app.js",
        "feedback_evaluations.js",
        "rubrics.js",
        "student_accounts.js",
        "student.js",
        "style.css",
    ):
        digest.update((BASE_DIR / "static" / filename).read_bytes())

    return digest.hexdigest()[:12]


STATIC_ASSET_VERSION = _static_asset_version()

OPENAI_MODEL_CATALOG = (
    ("gpt-5.6-luna", "GPT-5.6 Luna – günstig"),
    ("gpt-5.6-terra", "GPT-5.6 Terra – ausgewogen"),
    ("gpt-5.6-sol", "GPT-5.6 Sol – höchste Leistung"),
)

OPENAI_REASONING_EFFORT_OPTIONS = (
    ("", "Modellstandard"),
    ("none", "None – ohne zusätzlichen Denkaufwand"),
    ("low", "Low – geringer Denkaufwand"),
    ("medium", "Medium – ausgewogen"),
    ("high", "High – hoher Denkaufwand"),
    ("xhigh", "XHigh – sehr hoher Denkaufwand"),
    ("max", "Max – maximale Denktiefe"),
)

OPENAI_EVALUATION_REASONING_MODE_OPTIONS = (
    ("", "Standard – kein erzwungener Pro-Modus"),
    ("pro", "Pro – gründliche Prüfung"),
)

MISTRAL_MODEL_CATALOG = (
    ("mistral-small-latest", "Mistral Small – günstig"),
    ("mistral-medium-latest", "Mistral Medium – ausgewogen"),
    ("mistral-large-latest", "Mistral Large – höchste Leistung"),
)

STUDENT_PROVIDER_LABELS = {
    "mistral": "Mistral-Cloud-API",
    "openai": "OpenAI-Cloud-API",
}

STUDENT_FEEDBACK_SELECTION_SEPARATOR = "::"

RUNPOD_DEFAULT_ENDPOINT_KEY = "standard"

RUNPOD_ENDPOINT_CATALOG = (
    (
        RUNPOD_DEFAULT_ENDPOINT_KEY,
        "RunPod Standard – automatischer 48-GB-GPU-Pool",
        "runpod_endpoint_id",
        "RUNPOD_ENDPOINT_ID",
        (
            "NVIDIA L40",
            "NVIDIA L40S",
            "NVIDIA RTX 6000 Ada Generation",
        ),
    ),
)


def _format_duration_ms(value: float | int | None) -> str:
    """Formatiert technische Millisekunden prüferfreundlich."""

    if value is None:
        return "Nicht verfügbar"

    milliseconds = max(0.0, float(value))

    if milliseconds < 1000:
        return f"{milliseconds:.0f} ms"

    seconds = milliseconds / 1000

    if seconds < 60:
        return f"{seconds:.1f}".replace(".", ",") + " s"

    minutes = int(seconds // 60)
    remaining_seconds = seconds % 60
    formatted_seconds = (
        f"{remaining_seconds:.1f}".replace(".", ",")
    )
    return f"{minutes} min {formatted_seconds} s"


def _format_datetime_utc(value: datetime | None) -> str:
    if value is None:
        return "Nicht verfügbar"

    normalized = (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )
    return normalized.strftime("%d.%m.%Y, %H:%M UTC")


def _format_meta_score(value: float | int | None) -> str:
    if value is None:
        return "Nicht verfügbar"

    return f"{float(value):.1f}".replace(".", ",")


def _meta_score_hue(value: float | int | None) -> str:
    score = min(3.0, max(0.0, float(value or 0)))
    return f"{(score / 3) * 120:.1f}"


def _runtime_session_secret() -> str:
    """Verlangt außerhalb der lokalen Entwicklung ein festes Secret."""
    if settings.session_secret:
        return settings.session_secret

    if settings.app_mode == APP_MODE_LOCAL:
        return secrets.token_urlsafe(48)

    raise RuntimeError(
        "SESSION_SECRET muss für diesen Betriebsmodus "
        "konfiguriert sein."
    )


app = FastAPI(
    title=settings.app_name,
    docs_url=("/docs" if settings.api_docs_enabled else None),
    redoc_url=("/redoc" if settings.api_docs_enabled else None),
    openapi_url=(
        "/openapi.json"
        if settings.api_docs_enabled
        else None
    ),
)

runtime_session_secret = _runtime_session_secret()


app.add_middleware(
    SessionMiddleware,
    secret_key=runtime_session_secret,
    session_cookie=SESSION_COOKIE_NAME,
    max_age=settings.session_max_age_seconds,
    same_site="lax",
    https_only=settings.session_cookie_secure,
)

app.mount(
    "/static",
    StaticFiles(directory=BASE_DIR / "static"),
    name="static",
)

templates = Jinja2Templates(
    directory=BASE_DIR / "templates"
)
templates.env.filters[
    "feedback_markdown"
] = render_feedback_markdown
templates.env.filters[
    "feedback_inline_markdown"
] = render_feedback_inline_markdown
templates.env.filters["duration_ms"] = _format_duration_ms
templates.env.filters["datetime_utc"] = _format_datetime_utc
templates.env.filters["meta_score"] = _format_meta_score
templates.env.filters["meta_score_hue"] = _meta_score_hue
templates.env.filters[
    "criterion_status_display_label"
] = criterion_status_display_label
templates.env.filters[
    "criterion_status_display_text"
] = criterion_status_display_text
templates.env.globals[
    "static_asset_version"
] = STATIC_ASSET_VERSION


feedback_service = FeedbackService(
    providers={
        "ollama": OllamaProvider(
            base_url=settings.ollama_base_url,
            model_name=settings.ollama_model,
            request_timeout_seconds=(
                settings.ollama_request_timeout_seconds
            ),
        ),
        "openai": OpenAIProvider(
            api_key=settings.openai_api_key,
            model_name=settings.openai_model,
        ),
        "mistral": MistralProvider(
            api_key=settings.mistral_api_key,
            model_name=settings.mistral_model,
        ),
        "runpod": RunPodProvider(
            api_key=settings.runpod_api_key,
            endpoint_id=settings.runpod_endpoint_id,
            model_name=settings.runpod_model,
            job_timeout_seconds=(
                settings.runpod_job_timeout_seconds
            ),
            poll_interval_seconds=(
                settings.runpod_poll_interval_seconds
            ),
        ),
    },
    max_input_chars=settings.max_input_chars,
)

rubric_feedback_service = RubricFeedbackService(
    providers=feedback_service.providers,
    max_input_chars=settings.max_input_chars,
)

two_pass_rubric_feedback_service = TwoPassRubricFeedbackService(
    providers=feedback_service.providers,
    max_input_chars=settings.max_input_chars,
)

criterion_wise_rubric_feedback_service = (
    CriterionWiseRubricFeedbackService(
        providers=feedback_service.providers,
        max_input_chars=settings.max_input_chars,
    )
)

automatic_evaluation_provider = OpenAIAutomaticEvaluationProvider(
    api_key=settings.openai_api_key,
    model_name=settings.openai_evaluation_model,
)

automatic_feedback_evaluation_service = (
    AutomaticFeedbackEvaluationService(
        evaluator=automatic_evaluation_provider,
    )
)

feedback_evaluation_pdf_service = FeedbackEvaluationPdfService()

task_store = TaskStore(
    settings.analysis_database_path,
    max_criteria=settings.max_criteria,
    max_criterion_chars=settings.max_criterion_chars,
)

student_account_store = StudentAccountStore(
    settings.analysis_database_path,
    code_secret=runtime_session_secret,
)

student_analysis_gate = StudentAnalysisGate()

runpod_status_service = RunPodStatusService(
    api_key=settings.runpod_api_key,
    idle_timeout_seconds=settings.runpod_idle_timeout_seconds,
)

runpod_job_store = RunPodJobStore(
    settings.analysis_database_path
)

login_rate_limiter = LoginRateLimiter(
    max_attempts=settings.login_rate_limit_attempts,
    window_seconds=(
        settings.login_rate_limit_window_seconds
    ),
)

student_login_rate_limiter = LoginRateLimiter(
    max_attempts=settings.login_rate_limit_attempts,
    window_seconds=(
        settings.login_rate_limit_window_seconds
    ),
)


def _openai_model_options() -> list[tuple[str, str]]:
    """
    Zeigt das Modell aus der .env-Datei immer zuerst an,
    auch wenn es nicht im vordefinierten Katalog steht.
    """
    labels = dict(OPENAI_MODEL_CATALOG)
    configured_model = settings.openai_model

    options = [
        (
            configured_model,
            labels.get(configured_model, configured_model),
        )
    ]

    options.extend(
        (model_name, label)
        for model_name, label in OPENAI_MODEL_CATALOG
        if model_name != configured_model
    )

    return options


def _openai_evaluation_model_options() -> list[tuple[str, str]]:
    """Liefert die freigegebenen Modelle für die Meta-Bewertung."""

    labels = dict(OPENAI_MODEL_CATALOG)
    configured_model = settings.openai_evaluation_model
    options = [
        (
            configured_model,
            labels.get(configured_model, configured_model),
        )
    ]
    options.extend(
        (model_name, label)
        for model_name, label in OPENAI_MODEL_CATALOG
        if model_name != configured_model
    )
    return options


def _mistral_model_options() -> list[tuple[str, str]]:
    """
    Zeigt das Modell aus der .env-Datei immer zuerst an,
    auch wenn es nicht im vordefinierten Katalog steht.
    """
    labels = dict(MISTRAL_MODEL_CATALOG)
    configured_model = settings.mistral_model

    options = [
        (
            configured_model,
            labels.get(configured_model, configured_model),
        )
    ]

    options.extend(
        (model_name, label)
        for model_name, label in MISTRAL_MODEL_CATALOG
        if model_name != configured_model
    )

    return options


def _student_feedback_model_options(
    provider: str,
) -> list[tuple[str, str]]:
    """Liefert die fest freigegebenen Cloudmodelle der Schüleransicht."""

    if provider == "mistral":
        return _mistral_model_options()
    if provider == "openai":
        return _openai_model_options()

    return []


def _student_provider_is_configured(provider: str) -> bool:
    if provider == "mistral":
        return bool(settings.mistral_api_key)
    if provider == "openai":
        return bool(settings.openai_api_key)

    return False


def _default_student_feedback_configuration(
) -> StudentFeedbackConfiguration:
    provider = settings.student_feedback_provider
    model = (
        settings.mistral_model
        if provider == "mistral"
        else settings.openai_model
    )
    return StudentFeedbackConfiguration(
        provider=provider,
        model=model,
    )


def _validated_student_feedback_configuration(
    *,
    provider: str,
    model: str,
    require_configured_provider: bool,
) -> StudentFeedbackConfiguration:
    normalized_provider = provider.strip().lower()
    normalized_model = model.strip()
    allowed_models = {
        model_name
        for model_name, _label in _student_feedback_model_options(
            normalized_provider
        )
    }

    if normalized_model not in allowed_models:
        raise ValueError(
            "Bitte wähle eine freigegebene Kombination aus "
            "Cloudprovider und Modell."
        )

    if (
        require_configured_provider
        and not _student_provider_is_configured(normalized_provider)
    ):
        raise ValueError(
            f"{STUDENT_PROVIDER_LABELS[normalized_provider]} ist nicht "
            "vollständig konfiguriert. Prüfe den zugehörigen API-Key."
        )

    return StudentFeedbackConfiguration(
        provider=normalized_provider,
        model=normalized_model,
    )


def _student_feedback_configuration_from_selection(
    selection: str,
) -> StudentFeedbackConfiguration:
    provider, separator, model = selection.strip().partition(
        STUDENT_FEEDBACK_SELECTION_SEPARATOR
    )

    if not separator:
        raise ValueError(
            "Bitte wähle einen Modellanbieter und ein Modell."
        )

    return _validated_student_feedback_configuration(
        provider=provider,
        model=model,
        require_configured_provider=True,
    )


def _student_feedback_selection_value(
    configuration: StudentFeedbackConfiguration,
) -> str:
    return (
        f"{configuration.provider}"
        f"{STUDENT_FEEDBACK_SELECTION_SEPARATOR}"
        f"{configuration.model}"
    )


def _student_feedback_configuration_label(
    configuration: StudentFeedbackConfiguration,
) -> str:
    provider_label = STUDENT_PROVIDER_LABELS.get(
        configuration.provider,
        configuration.provider,
    )
    model_label = dict(
        _student_feedback_model_options(configuration.provider)
    ).get(configuration.model, configuration.model)
    return f"{provider_label} · {model_label}"


def _student_feedback_options() -> list[dict[str, object]]:
    options: list[dict[str, object]] = []

    for provider in ("mistral", "openai"):
        provider_configured = _student_provider_is_configured(provider)

        for model, model_label in _student_feedback_model_options(provider):
            configuration = StudentFeedbackConfiguration(
                provider=provider,
                model=model,
            )
            options.append(
                {
                    "value": _student_feedback_selection_value(
                        configuration
                    ),
                    "label": (
                        f"{STUDENT_PROVIDER_LABELS[provider]} · "
                        f"{model_label}"
                    ),
                    "configured": provider_configured,
                }
            )

    return options


async def _current_student_feedback_configuration(
) -> StudentFeedbackConfiguration:
    fallback = _default_student_feedback_configuration()
    stored = await task_store.get_student_feedback_configuration(
        fallback_provider=fallback.provider,
        fallback_model=fallback.model,
    )

    try:
        return _validated_student_feedback_configuration(
            provider=stored.provider,
            model=stored.model,
            require_configured_provider=False,
        )
    except ValueError:
        logger.warning(
            "Ungültige persistente Schülerfeedback-Konfiguration "
            "ignoriert (provider=%s, model=%s).",
            stored.provider,
            stored.model,
        )
        return fallback


def _student_provider_for_configuration(
    configuration: StudentFeedbackConfiguration,
) -> LLMProvider:
    validated = _validated_student_feedback_configuration(
        provider=configuration.provider,
        model=configuration.model,
        require_configured_provider=False,
    )

    if validated.provider == "mistral":
        return MistralProvider(
            api_key=settings.mistral_api_key,
            model_name=validated.model,
        )

    return OpenAIProvider(
        api_key=settings.openai_api_key,
        model_name=validated.model,
    )


def _ollama_available() -> bool:
    """Erlaubt Ollama außerhalb des Produktionsbetriebs."""
    return settings.app_mode != APP_MODE_PRODUCTION


def _provider_options() -> list[tuple[str, str]]:
    """Blendet lokale Provider im Produktionsbetrieb aus."""
    options = feedback_service.get_provider_options()

    if _ollama_available():
        return options

    return [
        option
        for option in options
        if option[0] != "ollama"
    ]


def _runpod_endpoint_options() -> list[dict[str, object]]:
    """Gibt nur sichere Auswahldaten an das Template weiter."""
    return [
        {
            "key": key,
            "label": label,
            "configured": bool(
                getattr(settings, settings_attribute)
            ),
        }
        for (
            key,
            label,
            settings_attribute,
            _environment_variable,
            _gpu_type_ids,
        ) in RUNPOD_ENDPOINT_CATALOG
    ]


def _selected_runpod_endpoint_key(
    raw_endpoint_key: str | None,
) -> str:
    """Gibt an das Template niemals unbekannte Browserwerte zurück."""
    endpoint_key = (raw_endpoint_key or "").strip().lower()
    allowed_keys = {
        option[0]
        for option in RUNPOD_ENDPOINT_CATALOG
    }

    if endpoint_key in allowed_keys:
        return endpoint_key

    return RUNPOD_DEFAULT_ENDPOINT_KEY


def _runpod_endpoint_label(
    endpoint_key: str,
) -> str:
    """Liefert nur das öffentliche Label eines bekannten Endpoints."""
    for (
        known_key,
        label,
        _settings_attribute,
        _environment_variable,
        _gpu_type_ids,
    ) in RUNPOD_ENDPOINT_CATALOG:
        if endpoint_key == known_key:
            return label

    raise ValueError(
        "Die ausgewählte RunPod-Hardwarekonfiguration "
        "ist nicht erlaubt."
    )


def _runpod_endpoint_definition(
    endpoint_key: str,
) -> tuple[str, str, str, str, tuple[str, ...]] | None:
    """Löst einen Browser-Key ausschließlich über die feste Allowlist auf."""

    normalized_key = endpoint_key.strip().lower()

    return next(
        (
            definition
            for definition in RUNPOD_ENDPOINT_CATALOG
            if definition[0] == normalized_key
        ),
        None,
    )


def _template_context(
    *,
    csrf_token: str,
    authenticated_user: str | None = None,
    selected_provider: str | None = None,
    student_text: str = "",
    ollama_base_url: str | None = None,
    selected_ollama_model: str | None = None,
    ollama_custom_model: str = "",
    selected_openai_model: str | None = None,
    openai_custom_model: str = "",
    selected_openai_reasoning_effort: str = "",
    openai_override_used: bool = False,
    selected_mistral_model: str | None = None,
    mistral_custom_model: str = "",
    mistral_override_used: bool = False,
    selected_runpod_endpoint: str | None = None,
    runpod_tracking_id: str | None = None,
    task_options: list[FeedbackTask] | None = None,
    selected_task_id: str = "",
    default_task_id: str = "",
    original_text: str = "",
    selected_rubric_analysis_mode: str | None = None,
    advanced_options: bool = False,
    result: FeedbackResult | RubricFeedbackResult | None = None,
    feedback_run_id: str | None = None,
    runpod_warm_window: dict[str, object] | None = None,
    storage_warning: str | None = None,
    error: str | None = None,
) -> dict[str, object]:
    provider_options = _provider_options()
    available_provider_keys = {
        provider_key
        for provider_key, _ in provider_options
    }

    if selected_provider not in available_provider_keys:
        selected_provider = (
            "openai"
            if "openai" in available_provider_keys
            else provider_options[0][0]
        )

    default_rubric_analysis_mode = (
        RUBRIC_ANALYSIS_MODE_CRITERION_WISE
        if selected_task_id
        else RUBRIC_ANALYSIS_MODE_JOINT
    )

    current_ollama_model = (
        selected_ollama_model
        or settings.ollama_model
    )

    ollama_model_options = [settings.ollama_model]

    if current_ollama_model not in {
        settings.ollama_model,
        CUSTOM_MODEL_VALUE,
    }:
        ollama_model_options.append(
            current_ollama_model
        )

    selected_runpod_endpoint_key = (
        _selected_runpod_endpoint_key(
            selected_runpod_endpoint
        )
    )

    result_endpoint_label = None

    if result is not None and result.provider == "runpod":
        result_endpoint_label = _runpod_endpoint_label(
            selected_runpod_endpoint_key
        )

    return {
        "app_name": settings.app_name,
        "authenticated_user": authenticated_user,
        "csrf_token": csrf_token,
        "provider_options": provider_options,
        "selected_provider": selected_provider,
        "student_text": student_text,
        "task_options": task_options or [],
        "selected_task_id": selected_task_id,
        "default_task_id": default_task_id,
        "original_text": original_text,
        "selected_rubric_analysis_mode": (
            selected_rubric_analysis_mode
            if selected_rubric_analysis_mode
            in VALID_RUBRIC_ANALYSIS_MODES
            else default_rubric_analysis_mode
        ),
        "advanced_options": advanced_options,
        "result": result,
        "feedback_run_id": feedback_run_id,
        "error": error,
        "storage_warning": storage_warning,
        "custom_model_value": CUSTOM_MODEL_VALUE,
        "browser_overrides_allowed": (
            settings.browser_overrides_allowed
        ),
        "ollama_available": _ollama_available(),
        "ollama_default_base_url": (
            settings.ollama_base_url
        ),
        "ollama_fallback_base_url": (
            OLLAMA_FALLBACK_BASE_URL
        ),
        "ollama_base_url": (
            (
                ollama_base_url
                or settings.ollama_base_url
            )
            if settings.browser_overrides_allowed
            else ""
        ),
        "ollama_default_model": settings.ollama_model,
        "ollama_model_options": ollama_model_options,
        "selected_ollama_model": (
            current_ollama_model
        ),
        "ollama_custom_model": ollama_custom_model,
        "openai_default_model": settings.openai_model,
        "openai_model_options": _openai_model_options(),
        "selected_openai_model": (
            selected_openai_model
            or settings.openai_model
        ),
        "openai_custom_model": openai_custom_model,
        "openai_reasoning_effort_options": (
            OPENAI_REASONING_EFFORT_OPTIONS
        ),
        "selected_openai_reasoning_effort": (
            selected_openai_reasoning_effort
        ),
        "openai_env_key_configured": bool(
            settings.openai_api_key
        ),
        "openai_override_used": openai_override_used,
        "mistral_default_model": settings.mistral_model,
        "mistral_model_options": _mistral_model_options(),
        "selected_mistral_model": (
            selected_mistral_model
            or settings.mistral_model
        ),
        "mistral_custom_model": mistral_custom_model,
        "mistral_env_key_configured": bool(
            settings.mistral_api_key
        ),
        "mistral_override_used": mistral_override_used,
        "runpod_endpoint_options": _runpod_endpoint_options(),
        "selected_runpod_endpoint": selected_runpod_endpoint_key,
        "runpod_tracking_id": (
            runpod_tracking_id or str(uuid4())
        ),
        "result_endpoint_label": result_endpoint_label,
        "runpod_warm_window": runpod_warm_window,
        "runpod_idle_timeout_minutes": (
            settings.runpod_idle_timeout_seconds // 60
        ),
    }


TASK_NOTICE_MESSAGES = {
    "created": "Aufgabe und Feedback wurden gespeichert.",
    "updated": "Aufgabe und Feedback wurden aktualisiert.",
    "duplicated": "Aufgabe und Feedback wurden dupliziert.",
    "deleted": "Die unbenutzte Feedback-Vorlage wurde gelöscht.",
    "archived": (
        "Die bereits verwendete Feedback-Vorlage wurde archiviert. "
        "Vorhandene Analyseergebnisse bleiben erhalten."
    ),
    "default": (
        "Die Aufgabe wurde als Standard-Kriterienvorlage festgelegt."
    ),
}

FEEDBACK_EVALUATION_NOTICES = {
    "saved": (
        "Der Feedbacklauf wurde für die spätere Bewertung gespeichert.",
        "success",
    ),
    "save-failed": (
        "Der Feedbacklauf konnte nicht für die Bewertung gespeichert werden.",
        "error",
    ),
    "evaluation-saved": (
        "Die manuelle Bewertung wurde als eigenständiger Datensatz "
        "gespeichert.",
        "success",
    ),
    "evaluation-failed": (
        "Die manuelle Bewertung konnte nicht gespeichert werden. Bitte "
        "prüfe alle vier Bewertungsstufen und Begründungen.",
        "error",
    ),
    "evaluation-deleted": (
        "Die ausgewählte Bewertung wurde gelöscht.",
        "success",
    ),
    "evaluation-delete-linked": (
        "Die KI-Vorbewertung ist noch mit einer manuellen Prüfung "
        "verknüpft. Lösche zuerst die verknüpfte manuelle Bewertung.",
        "error",
    ),
    "evaluation-delete-failed": (
        "Die ausgewählte Bewertung konnte nicht gelöscht werden.",
        "error",
    ),
    "feedback-run-removed": (
        "Der Feedbackbogen und alle zugehörigen Bewertungen wurden aus "
        "der Feedback-Bewertung entfernt. Der technische Feedbacklauf "
        "bleibt zur Nachvollziehbarkeit erhalten.",
        "success",
    ),
    "feedback-run-remove-failed": (
        "Der Feedbackbogen konnte nicht aus der Feedback-Bewertung "
        "entfernt werden.",
        "error",
    ),
    "automatic-evaluation-saved": (
        "Die automatische Vorbewertung wurde unverändert gespeichert. "
        "Du kannst sie nun im manuellen Formular prüfen und als getrennte "
        "Bewertung speichern.",
        "success",
    ),
    "automatic-evaluation-failed": (
        "Die automatische Vorbewertung konnte nicht abgeschlossen oder "
        "gespeichert werden. Es wurde kein unvollständiger Datensatz "
        "angelegt. Prüfe OPENAI_API_KEY, Modellzugriff und Verbindung. "
        "Die technische Ursache steht im Serverterminal.",
        "error",
    ),
    "evaluation-import-failed": (
        "Der Meta-Bewertungs-Import konnte nicht verarbeitet werden. "
        "Bitte wähle einen unveränderten JSON-Export dieser Anwendung.",
        "error",
    ),
}


def _task_notice_message(
    notice: str,
    imported_count: int,
) -> str | None:
    if notice == "imported" and imported_count > 0:
        if imported_count == 1:
            return "Eine Feedback-Vorlage wurde als neue Kopie importiert."

        return (
            f"{imported_count} Feedback-Vorlagen wurden als neue Kopien "
            "importiert."
        )

    return TASK_NOTICE_MESSAGES.get(notice)


def _feedback_evaluation_notice(
    notice: str,
    imported_run_count: int,
    imported_evaluation_count: int,
) -> tuple[str | None, str | None]:
    if (
        notice == "evaluation-imported"
        and imported_run_count > 0
    ):
        run_label = (
            "Feedbacklauf"
            if imported_run_count == 1
            else "Feedbackläufe"
        )
        if imported_evaluation_count > 0:
            evaluation_label = (
                "Meta-Bewertung"
                if imported_evaluation_count == 1
                else "Meta-Bewertungen"
            )
            evaluation_summary = (
                f"mit {imported_evaluation_count} {evaluation_label}"
            )
        else:
            evaluation_summary = "ohne vorhandene Meta-Bewertung"

        verb = "wurde" if imported_run_count == 1 else "wurden"
        copy_label = (
            "neue Kopie"
            if imported_run_count == 1
            else "neue Kopien"
        )

        return (
            f"{imported_run_count} {run_label} {evaluation_summary} "
            f"{verb} als {copy_label} importiert.",
            "success",
        )

    return FEEDBACK_EVALUATION_NOTICES.get(
        notice,
        (None, None),
    )


def _rubric_download_response(
    content: bytes,
    filename: str,
    *,
    media_type: str = "application/json",
) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"'
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _task_form_values(
    task: FeedbackTask | None = None,
) -> dict[str, object]:
    if task is None:
        return {
            "title": "",
            "subject": "Deutsch",
            "grade_level": "",
            "instructions": "",
            "material": "",
            "rubric_title": "",
            "criteria": [""],
            "criterion_titles": [""],
        }

    return {
        "title": task.title,
        "subject": task.subject,
        "grade_level": task.grade_level,
        "instructions": task.instructions,
        "material": task.material,
        "rubric_title": task.rubric.title,
        "criteria": [
            criterion.text
            for criterion in task.rubric.criteria
        ],
        "criterion_titles": [
            criterion.title
            for criterion in task.rubric.criteria
        ],
    }


def _submitted_task_form_values(
    *,
    title: str,
    subject: str,
    grade_level: str,
    instructions: str,
    material: str,
    rubric_title: str,
    criteria: list[str],
    criterion_titles: list[str] | None,
) -> dict[str, object]:
    criterion_values = criteria or [""]
    title_values = list(criterion_titles or ())

    if len(title_values) < len(criterion_values):
        title_values.extend(
            ""
            for _ in range(len(criterion_values) - len(title_values))
        )

    return {
        "title": title,
        "subject": subject,
        "grade_level": grade_level,
        "instructions": instructions,
        "material": material,
        "rubric_title": rubric_title,
        "criteria": criterion_values,
        "criterion_titles": title_values,
    }


def _task_form_context(
    *,
    request: Request,
    authenticated_user: str,
    task: FeedbackTask | None,
    values: dict[str, object],
    error: str | None = None,
) -> dict[str, object]:
    return {
        "app_name": settings.app_name,
        "authenticated_user": authenticated_user,
        "csrf_token": get_or_create_csrf_token(
            request.session
        ),
        "task": task,
        "values": values,
        "error": error,
        "max_criteria": task_store.max_criteria,
        "max_criterion_chars": task_store.max_criterion_chars,
    }


def _authenticated_user(request: Request) -> str | None:
    """Liefert nur das konfigurierte, gültig angemeldete Konto."""
    if not is_authenticated(
        request.session,
        settings.auth_username,
    ):
        return None

    return authenticated_username(request.session)


def _redirect_to_login() -> RedirectResponse:
    return RedirectResponse(
        url="/login",
        status_code=303,
    )


def _redirect_to_student_portal() -> RedirectResponse:
    return RedirectResponse(
        url="/schueler",
        status_code=303,
    )


async def _authenticated_student_account(
    request: Request,
) -> StudentAccount | None:
    """Prüft Konto-ID und aktuellen Aktivstatus jeder Schülersitzung."""

    account_id = authenticated_student_account_id(request.session)
    access_version = authenticated_student_access_version(request.session)

    if account_id is None or access_version is None:
        return None

    account = await student_account_store.get_active_account(account_id)

    if account is None or account.access_version != access_version:
        request.session.clear()
        return None

    return account


async def _student_accounts_response(
    *,
    request: Request,
    authenticated_user: str,
    issued_code: IssuedStudentAccessCode | None = None,
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    try:
        accounts = await student_account_store.list_accounts()
    except StudentAccountStoreError as exc:
        accounts = []
        error = error or str(exc)
        status_code = 500

    try:
        feedback_configuration = (
            await _current_student_feedback_configuration()
        )
    except TaskStoreError as exc:
        feedback_configuration = (
            _default_student_feedback_configuration()
        )
        error = error or str(exc)
        status_code = 500

    feedback_options = _student_feedback_options()

    return templates.TemplateResponse(
        name="student_accounts.html",
        request=request,
        context={
            "app_name": settings.app_name,
            "authenticated_user": authenticated_user,
            "csrf_token": get_or_create_csrf_token(request.session),
            "accounts": accounts,
            "issued_code": issued_code,
            "notice": notice,
            "error": error,
            "max_label_chars": MAX_STUDENT_ACCOUNT_LABEL_CHARS,
            "student_link": str(request.url_for("student_portal")),
            "student_feedback_options": feedback_options,
            "student_feedback_has_configured_option": any(
                bool(option["configured"])
                for option in feedback_options
            ),
            "selected_student_feedback_target": (
                _student_feedback_selection_value(
                    feedback_configuration
                )
            ),
            "student_feedback_configuration_label": (
                _student_feedback_configuration_label(
                    feedback_configuration
                )
            ),
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _student_login_context(
    request: Request,
    *,
    error: str | None = None,
) -> dict[str, object]:
    return {
        "app_name": settings.app_name,
        "authenticated_user": None,
        "csrf_token": get_or_create_csrf_token(request.session),
        "error": error,
    }


def _student_portal_context(
    *,
    request: Request,
    account: StudentAccount,
    tasks: list[FeedbackTask],
    selected_task_id: str = "",
    student_text: str = "",
    result: RubricFeedbackResult | None = None,
    error: str | None = None,
    storage_warning: str | None = None,
) -> dict[str, object]:
    return {
        "app_name": settings.app_name,
        "authenticated_user": None,
        "student_account": account,
        "csrf_token": get_or_create_csrf_token(request.session),
        "task_options": tasks,
        "selected_task_id": selected_task_id,
        "student_text": student_text,
        "result": result,
        "error": error,
        "storage_warning": storage_warning,
        "max_input_chars": settings.max_input_chars,
    }


def _login_client_key(request: Request) -> str:
    """Verwendet die von ASGI ermittelte Client-Adresse."""
    if request.client is None:
        return "unknown-client"

    return request.client.host


def _require_valid_csrf_token(
    request: Request,
    submitted_token: str,
) -> None:
    """Lehnt schreibende Browseranfragen ohne Sitzungstoken ab."""
    if is_valid_csrf_token(
        request.session,
        submitted_token,
    ):
        return

    raise HTTPException(
        status_code=403,
        detail="Ungültige oder fehlende Formularbestätigung.",
    )


def _validate_model_name(
    selected_model: str,
    custom_model: str,
    default: str,
) -> str:
    entered_custom_model = custom_model.strip()

    if entered_custom_model:
        model_name = entered_custom_model
    elif selected_model == CUSTOM_MODEL_VALUE:
        raise ValueError(
            "Bitte gib die gewünschte Modell-ID "
            "in das Freitextfeld ein."
        )
    else:
        model_name = (
            selected_model or default
        ).strip()

    if not model_name:
        raise ValueError(
            "Bitte wähle ein Modell oder gib "
            "eine Modell-ID ein."
        )

    if len(model_name) > MAX_MODEL_NAME_CHARS:
        raise ValueError(
            "Die Modell-ID darf höchstens "
            f"{MAX_MODEL_NAME_CHARS} Zeichen lang sein."
        )

    if any(
        character.isspace()
        for character in model_name
    ):
        raise ValueError(
            "Die Modell-ID darf keine Leerzeichen enthalten."
        )

    return model_name


def _validate_openai_reasoning_effort(
    reasoning_effort: str,
) -> str | None:
    normalized = reasoning_effort.strip().lower()

    if not normalized:
        return None

    if normalized not in OPENAI_REASONING_EFFORTS:
        raise ValueError(
            "Bitte wähle eine gültige OpenAI-Denktiefe."
        )

    return normalized


def _validate_openai_evaluation_model(model_name: str) -> str:
    normalized = _validate_model_name(
        model_name,
        "",
        settings.openai_evaluation_model,
    )
    allowed_models = {
        model
        for model, _label in _openai_evaluation_model_options()
    }

    if normalized not in allowed_models:
        raise ValueError(
            "Bitte wähle ein freigegebenes OpenAI-Bewertungsmodell."
        )

    return normalized


def _validate_openai_evaluation_reasoning_mode(
    reasoning_mode: str,
) -> str | None:
    normalized = reasoning_mode.strip().lower()

    if not normalized:
        return None

    if normalized not in OPENAI_EVALUATION_REASONING_MODES:
        raise ValueError(
            "Bitte wähle einen gültigen OpenAI-Denkmodus."
        )

    return normalized


def _validate_runpod_tracking_id(raw_tracking_id: str) -> str:
    """Akzeptiert ausschließlich kanonische UUIDs aus dem Analyseformular."""

    try:
        tracking_id = UUID(raw_tracking_id.strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError(
            "Die technische RunPod-Tracking-ID ist ungültig."
        ) from exc

    return str(tracking_id)


def _configured_runpod_endpoint(
    endpoint_key: str,
) -> tuple[str, str, str]:
    """Löst einen öffentlichen Key auf eine konfigurierte Endpoint-ID auf."""

    normalized_key = (
        endpoint_key.strip().lower()
        or RUNPOD_DEFAULT_ENDPOINT_KEY
    )
    definition = _runpod_endpoint_definition(normalized_key)

    if definition is None:
        raise ValueError(
            "Die ausgewählte RunPod-Hardwarekonfiguration "
            "ist nicht erlaubt."
        )

    (
        allowed_key,
        label,
        settings_attribute,
        environment_variable,
        _gpu_type_ids,
    ) = definition
    endpoint_id = getattr(settings, settings_attribute)

    if not endpoint_id:
        raise ValueError(
            f"{label} ist noch nicht konfiguriert. "
            f"Hinterlege {environment_variable} in der .env-Datei."
        )

    return allowed_key, label, endpoint_id


def _runpod_provider(
    *,
    endpoint_id: str,
    job_status_callback: RunPodJobStatusCallback | None = None,
) -> RunPodProvider:
    return RunPodProvider(
        api_key=settings.runpod_api_key,
        endpoint_id=endpoint_id,
        model_name=settings.runpod_model,
        job_timeout_seconds=(
            settings.runpod_job_timeout_seconds
        ),
        poll_interval_seconds=(
            settings.runpod_poll_interval_seconds
        ),
        job_status_callback=job_status_callback,
    )


def _runpod_job_status_callback(
    *,
    tracking_id: str,
    endpoint_key: str,
    endpoint_id: str,
) -> RunPodJobStatusCallback:
    async def record_job_status(
        job_id: str,
        status: str,
    ) -> None:
        await runpod_job_store.record_status(
            tracking_id=tracking_id,
            job_id=job_id,
            endpoint_key=endpoint_key,
            endpoint_id=endpoint_id,
            status=status,
        )

    return record_job_status


def _validate_ollama_base_url(
    raw_base_url: str,
) -> str:
    base_url = (
        raw_base_url
        or settings.ollama_base_url
    ).strip().rstrip("/")

    parsed = urlsplit(base_url)

    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
    ):
        raise ValueError(
            "Die Ollama-Adresse muss eine vollständige "
            "HTTP-Adresse sein, zum Beispiel "
            "http://localhost:11434."
        )

    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Die Ollama-Adresse darf keine Zugangsdaten, "
            "Abfrageparameter oder Fragmente enthalten."
        )

    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(
            "Die Ollama-Adresse enthält keinen gültigen Port."
        ) from exc

    return base_url


def _rubric_analysis_mode(
    raw_mode: str,
    *,
    legacy_two_pass: bool = False,
    default_mode: str = RUBRIC_ANALYSIS_MODE_JOINT,
) -> str:
    """Validiert den Modus und unterstützt den bisherigen Checkbox-POST."""
    normalized_mode = raw_mode.strip().lower()

    if not normalized_mode:
        return (
            RUBRIC_ANALYSIS_MODE_TWO_PASS
            if legacy_two_pass
            else default_mode
        )
    if normalized_mode not in VALID_RUBRIC_ANALYSIS_MODES:
        raise ValueError(
            "Das ausgewählte Analyseverfahren ist nicht gültig."
        )

    return normalized_mode


def _provider_for_request(
    *,
    provider_key: str,
    ollama_base_url: str,
    ollama_model: str,
    ollama_custom_model: str,
    openai_model: str,
    openai_custom_model: str,
    openai_api_key: str,
    runpod_endpoint: str,
    runpod_tracking_id: str,
    mistral_model: str = "",
    mistral_custom_model: str = "",
    mistral_api_key: str = "",
    openai_reasoning_effort: str = "",
) -> LLMProvider:
    if provider_key == "ollama":
        if not _ollama_available():
            raise ValueError(
                "Ollama ist im Produktionsbetrieb deaktiviert."
            )

        effective_base_url = (
            ollama_base_url.strip()
            or settings.ollama_base_url
            if settings.browser_overrides_allowed
            else settings.ollama_base_url
        )

        return OllamaProvider(
            base_url=_validate_ollama_base_url(
                effective_base_url
            ),
            model_name=_validate_model_name(
                ollama_model,
                ollama_custom_model,
                settings.ollama_model,
            ),
            request_timeout_seconds=(
                settings.ollama_request_timeout_seconds
            ),
        )

    if provider_key == "openai":
        api_key = settings.openai_api_key

        if settings.browser_overrides_allowed:
            api_key = (
                openai_api_key.strip()
                or api_key
            )

        if not api_key:
            error_message = (
                "Kein OpenAI-API-Key verfügbar. "
                "Hinterlege OPENAI_API_KEY in der "
                ".env-Datei."
            )

            if settings.browser_overrides_allowed:
                error_message = (
                    "Kein OpenAI-API-Key verfügbar. "
                    "Hinterlege OPENAI_API_KEY in der "
                    ".env-Datei oder gib im optionalen "
                    "Key-Feld einen Key für diesen Aufruf ein."
                )

            raise ValueError(error_message)

        return OpenAIProvider(
            api_key=api_key,
            model_name=_validate_model_name(
                openai_model,
                openai_custom_model,
                settings.openai_model,
            ),
            reasoning_effort=(
                _validate_openai_reasoning_effort(
                    openai_reasoning_effort
                )
            ),
        )

    if provider_key == "mistral":
        api_key = settings.mistral_api_key

        if settings.browser_overrides_allowed:
            api_key = (
                mistral_api_key.strip()
                or api_key
            )

        if not api_key:
            error_message = (
                "Kein Mistral-API-Key verfügbar. "
                "Hinterlege MISTRAL_API_KEY in der "
                ".env-Datei."
            )

            if settings.browser_overrides_allowed:
                error_message = (
                    "Kein Mistral-API-Key verfügbar. "
                    "Hinterlege MISTRAL_API_KEY in der "
                    ".env-Datei oder gib im optionalen "
                    "Key-Feld einen Key für diesen Aufruf ein."
                )

            raise ValueError(error_message)

        return MistralProvider(
            api_key=api_key,
            model_name=_validate_model_name(
                mistral_model,
                mistral_custom_model,
                settings.mistral_model,
            ),
        )

    if provider_key == "runpod":
        endpoint_key, _label, endpoint_id = (
            _configured_runpod_endpoint(runpod_endpoint)
        )
        tracking_id = _validate_runpod_tracking_id(
            runpod_tracking_id
        )

        return _runpod_provider(
            endpoint_id=endpoint_id,
            job_status_callback=_runpod_job_status_callback(
                tracking_id=tracking_id,
                endpoint_key=endpoint_key,
                endpoint_id=endpoint_id,
            ),
        )

    raise ValueError(
        "Der ausgewählte Modellanbieter ist nicht bekannt."
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
) -> Response:
    if _authenticated_user(request) is not None:
        return RedirectResponse(
            url="/",
            status_code=303,
        )

    return templates.TemplateResponse(
        name="login.html",
        request=request,
        context={
            "app_name": settings.app_name,
            "authenticated_user": None,
            "username": settings.auth_username,
            "csrf_token": get_or_create_csrf_token(
                request.session
            ),
            "error": None,
        },
    )


@app.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    username: str = Form(..., max_length=200),
    password: str = Form(..., max_length=512),
    csrf_token: str = Form("", max_length=256),
) -> Response:
    _require_valid_csrf_token(request, csrf_token)

    client_key = _login_client_key(request)
    retry_after = login_rate_limiter.retry_after_seconds(
        client_key
    )

    if retry_after is not None:
        return templates.TemplateResponse(
            name="login.html",
            request=request,
            context={
                "app_name": settings.app_name,
                "authenticated_user": None,
                "username": settings.auth_username,
                "csrf_token": get_or_create_csrf_token(
                    request.session
                ),
                "error": (
                    "Zu viele fehlgeschlagene "
                    "Anmeldeversuche. Bitte versuche es "
                    "später erneut."
                ),
            },
            status_code=429,
            headers={
                "Retry-After": str(retry_after),
            },
        )

    if verify_credentials(
        username=username.strip(),
        password=password,
        expected_username=settings.auth_username,
        password_hash=settings.auth_password_hash,
    ):
        login_rate_limiter.reset(client_key)
        start_authenticated_session(
            request.session,
            settings.auth_username,
        )

        return RedirectResponse(
            url="/",
            status_code=303,
        )

    login_rate_limiter.record_failure(client_key)

    return templates.TemplateResponse(
        name="login.html",
        request=request,
        context={
            "app_name": settings.app_name,
            "authenticated_user": None,
            "username": settings.auth_username,
            "csrf_token": get_or_create_csrf_token(
                request.session
            ),
            "error": "Benutzername oder Passwort ist falsch.",
        },
        status_code=401,
    )


@app.post("/logout")
async def logout(
    request: Request,
    csrf_token: str = Form("", max_length=256),
) -> RedirectResponse:
    _require_valid_csrf_token(request, csrf_token)
    end_authenticated_session(request.session)

    return _redirect_to_login()


@app.get("/schuelerzugange", response_class=HTMLResponse)
async def student_accounts_page(
    request: Request,
    notice: str = Query(default="", max_length=32),
) -> Response:
    authenticated_user = _authenticated_user(request)

    if authenticated_user is None:
        return _redirect_to_login()

    notice_messages = {
        "activated": "Das Schülerkonto wurde aktiviert.",
        "disabled": "Das Schülerkonto wurde deaktiviert.",
        "deleted": "Das Schülerkonto wurde gelöscht.",
        "feedback-target-updated": (
            "Provider und Modell für Schülerfeedback wurden gespeichert."
        ),
    }
    return await _student_accounts_response(
        request=request,
        authenticated_user=authenticated_user,
        notice=notice_messages.get(notice),
    )


@app.post(
    "/schuelerzugange/feedback-target",
    response_class=HTMLResponse,
)
async def set_student_feedback_target(
    request: Request,
    student_feedback_target: str = Form("", max_length=400),
    csrf_token: str = Form("", max_length=256),
) -> Response:
    authenticated_user = _authenticated_user(request)

    if authenticated_user is None:
        return _redirect_to_login()

    _require_valid_csrf_token(request, csrf_token)

    try:
        configuration = _student_feedback_configuration_from_selection(
            student_feedback_target
        )
        await task_store.set_student_feedback_configuration(
            provider=configuration.provider,
            model=configuration.model,
        )
    except ValueError as exc:
        return await _student_accounts_response(
            request=request,
            authenticated_user=authenticated_user,
            error=str(exc),
            status_code=422,
        )
    except TaskStoreError as exc:
        return await _student_accounts_response(
            request=request,
            authenticated_user=authenticated_user,
            error=str(exc),
            status_code=500,
        )

    return RedirectResponse(
        url="/schuelerzugange?notice=feedback-target-updated",
        status_code=303,
    )


@app.post("/schuelerzugange/new", response_class=HTMLResponse)
async def create_student_account(
    request: Request,
    label: str = Form("", max_length=MAX_STUDENT_ACCOUNT_LABEL_CHARS),
    csrf_token: str = Form("", max_length=256),
) -> Response:
    authenticated_user = _authenticated_user(request)

    if authenticated_user is None:
        return _redirect_to_login()

    _require_valid_csrf_token(request, csrf_token)

    try:
        issued_code = await student_account_store.create_account(label)
    except (ValueError, StudentAccountConflictError) as exc:
        return await _student_accounts_response(
            request=request,
            authenticated_user=authenticated_user,
            error=str(exc),
            status_code=422,
        )
    except StudentAccountStoreError as exc:
        return await _student_accounts_response(
            request=request,
            authenticated_user=authenticated_user,
            error=str(exc),
            status_code=500,
        )

    return await _student_accounts_response(
        request=request,
        authenticated_user=authenticated_user,
        issued_code=issued_code,
        notice=(
            "Das Schülerkonto wurde erstellt. Kopiere den Code jetzt; "
            "er wird nicht im Klartext gespeichert."
        ),
    )


@app.post(
    "/schuelerzugange/{account_id}/new-code",
    response_class=HTMLResponse,
)
async def issue_new_student_access_code(
    request: Request,
    account_id: str,
    csrf_token: str = Form("", max_length=256),
) -> Response:
    authenticated_user = _authenticated_user(request)

    if authenticated_user is None:
        return _redirect_to_login()

    _require_valid_csrf_token(request, csrf_token)

    try:
        issued_code = await student_account_store.issue_new_code(account_id)
    except StudentAccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StudentAccountStoreError as exc:
        return await _student_accounts_response(
            request=request,
            authenticated_user=authenticated_user,
            error=str(exc),
            status_code=500,
        )

    return await _student_accounts_response(
        request=request,
        authenticated_user=authenticated_user,
        issued_code=issued_code,
        notice=(
            "Der bisherige Code ist ab sofort ungültig. Kopiere den neuen "
            "Code jetzt; er wird nicht im Klartext gespeichert."
        ),
    )


@app.post("/schuelerzugange/{account_id}/status")
async def set_student_account_status(
    request: Request,
    account_id: str,
    active: bool = Form(...),
    csrf_token: str = Form("", max_length=256),
) -> RedirectResponse:
    if _authenticated_user(request) is None:
        return _redirect_to_login()

    _require_valid_csrf_token(request, csrf_token)

    try:
        await student_account_store.set_account_active(
            account_id,
            active=active,
        )
    except StudentAccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return RedirectResponse(
        url=(
            "/schuelerzugange?notice="
            f"{'activated' if active else 'disabled'}"
        ),
        status_code=303,
    )


@app.post("/schuelerzugange/{account_id}/delete")
async def delete_student_account(
    request: Request,
    account_id: str,
    csrf_token: str = Form("", max_length=256),
) -> RedirectResponse:
    if _authenticated_user(request) is None:
        return _redirect_to_login()

    _require_valid_csrf_token(request, csrf_token)

    try:
        await student_account_store.delete_account(account_id)
    except StudentAccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return RedirectResponse(
        url="/schuelerzugange?notice=deleted",
        status_code=303,
    )


@app.get("/schueler", response_class=HTMLResponse)
async def student_portal(request: Request) -> Response:
    try:
        account = await _authenticated_student_account(request)
    except StudentAccountStoreError:
        account = None

    if account is None:
        return templates.TemplateResponse(
            name="student_login.html",
            request=request,
            context=_student_login_context(request),
            headers={"Cache-Control": "no-store"},
        )

    try:
        tasks = await task_store.list_tasks()
        default_task_id = await task_store.get_default_feedback_task_id()
    except TaskStoreError:
        tasks = []
        default_task_id = None

    active_task_ids = {task.task_id for task in tasks}
    selected_task_id = (
        default_task_id
        if default_task_id in active_task_ids
        else tasks[0].task_id if tasks else ""
    )
    return templates.TemplateResponse(
        name="student_portal.html",
        request=request,
        context=_student_portal_context(
            request=request,
            account=account,
            tasks=tasks,
            selected_task_id=selected_task_id,
            error=(
                None
                if tasks
                else "Momentan ist keine Feedback-Vorlage verfügbar."
            ),
        ),
        headers={"Cache-Control": "no-store"},
    )


@app.post("/schueler/login", response_class=HTMLResponse)
async def student_login(
    request: Request,
    access_code: str = Form("", max_length=6),
    csrf_token: str = Form("", max_length=256),
) -> Response:
    _require_valid_csrf_token(request, csrf_token)
    client_key = _login_client_key(request)
    retry_after = student_login_rate_limiter.retry_after_seconds(client_key)

    if retry_after is not None:
        return templates.TemplateResponse(
            name="student_login.html",
            request=request,
            context=_student_login_context(
                request,
                error=(
                    "Zu viele fehlgeschlagene Anmeldeversuche. Bitte "
                    "versuche es später erneut."
                ),
            ),
            status_code=429,
            headers={
                "Retry-After": str(retry_after),
                "Cache-Control": "no-store",
            },
        )

    try:
        account = await student_account_store.authenticate_code(access_code)
    except StudentAccountStoreError:
        return templates.TemplateResponse(
            name="student_login.html",
            request=request,
            context=_student_login_context(
                request,
                error=(
                    "Der Schülerzugang kann momentan nicht geprüft werden. "
                    "Bitte versuche es später erneut."
                ),
            ),
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )

    if account is None:
        student_login_rate_limiter.record_failure(client_key)
        return templates.TemplateResponse(
            name="student_login.html",
            request=request,
            context=_student_login_context(
                request,
                error="Der sechsstellige Zugangscode ist ungültig.",
            ),
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )

    student_login_rate_limiter.reset(client_key)
    start_student_session(
        request.session,
        account.account_id,
        account.access_version,
    )
    return _redirect_to_student_portal()


@app.post("/schueler/logout")
async def student_logout(
    request: Request,
    csrf_token: str = Form("", max_length=256),
) -> RedirectResponse:
    _require_valid_csrf_token(request, csrf_token)
    end_authenticated_session(request.session)
    return _redirect_to_student_portal()


@app.post("/schueler/analyze", response_class=HTMLResponse)
async def analyze_student_text(
    request: Request,
    student_text: str = Form(...),
    task_id: str = Form(..., max_length=100),
    csrf_token: str = Form("", max_length=256),
) -> Response:
    try:
        account = await _authenticated_student_account(request)
    except StudentAccountStoreError:
        account = None

    if account is None:
        return _redirect_to_student_portal()

    _require_valid_csrf_token(request, csrf_token)
    result: RubricFeedbackResult | None = None
    error: str | None = None
    storage_warning: str | None = None
    selected_task_id = task_id.strip()
    feedback_configuration = (
        _default_student_feedback_configuration()
    )

    try:
        cleaned_text = student_text.strip()

        if not cleaned_text:
            raise ValueError("Bitte gib deinen Text ein.")

        if len(cleaned_text) > settings.max_input_chars:
            raise ValueError(
                "Dein Text ist zu lang. Erlaubt sind maximal "
                f"{settings.max_input_chars} Zeichen."
            )

        selected_task = await task_store.get_task(selected_task_id)

        if selected_task is None:
            raise ValueError(
                "Die ausgewählte Feedback-Vorlage ist nicht verfügbar."
            )

        feedback_configuration = (
            await _current_student_feedback_configuration()
        )
        provider_override = _student_provider_for_configuration(
            feedback_configuration
        )

        async with student_analysis_gate.reserve(account.account_id):
            result = await criterion_wise_rubric_feedback_service.analyze_text(
                student_text=cleaned_text,
                task=selected_task,
                provider_key=feedback_configuration.provider,
                provider_override=provider_override,
            )

            try:
                await task_store.save_feedback_run(
                    task=selected_task,
                    student_text=cleaned_text,
                    provider=result.provider,
                    model=result.model,
                    duration_ms=result.duration_ms,
                    feedback_payload=result.payload(),
                    provider_request_id=result.provider_request_id,
                    queue_duration_ms=result.queue_duration_ms,
                    execution_duration_ms=result.execution_duration_ms,
                    reasoning_effort=result.reasoning_effort,
                )
            except TaskStoreError:
                storage_warning = (
                    "Dein Feedback wurde erstellt, aber die technischen "
                    "Laufdaten konnten nicht gespeichert werden."
                )
    except (ValueError, StudentAnalysisInProgressError) as exc:
        error = str(exc)
    except Exception as exc:
        logger.error(
            "Schülerfeedback fehlgeschlagen "
            "(account_id=%s, provider=%s, task_id=%s, error_type=%s).",
            account.account_id,
            feedback_configuration.provider,
            selected_task_id,
            type(exc).__name__,
        )
        error = (
            "Das Feedback konnte momentan nicht erstellt werden. Bitte "
            "versuche es später erneut oder wende dich an deine Lehrkraft."
        )

    try:
        tasks = await task_store.list_tasks()
    except TaskStoreError:
        tasks = []

    context = _student_portal_context(
        request=request,
        account=account,
        tasks=tasks,
        selected_task_id=selected_task_id,
        student_text=student_text,
        result=result,
        error=error,
        storage_warning=storage_warning,
    )
    template_name = (
        "student_analysis_response.html"
        if request.headers.get("X-Requested-With") == "XMLHttpRequest"
        else "student_portal.html"
    )
    return templates.TemplateResponse(
        name=template_name,
        request=request,
        context=context,
        headers={
            "Cache-Control": "no-store",
            "X-Analysis-Outcome": "error" if error else "success",
        },
    )


@app.get("/feedback-evaluations", response_class=HTMLResponse)
async def feedback_evaluations_page(
    request: Request,
    notice: str = Query(default="", max_length=32),
    imported_run_count: int = Query(default=0, ge=0, le=1000),
    imported_evaluation_count: int = Query(
        default=0,
        ge=0,
        le=500_000,
    ),
    automatic_feedback_run_id: str = Query(
        default="",
        max_length=36,
    ),
    feedback_run_notice_id: str = Query(
        default="",
        max_length=36,
    ),
) -> Response:
    authenticated_user = _authenticated_user(request)

    if authenticated_user is None:
        return _redirect_to_login()

    error: str | None = None

    try:
        feedback_runs = (
            await task_store.list_feedback_runs_for_evaluation()
        )
    except TaskStoreError:
        feedback_runs = []
        error = (
            "Die gespeicherten Feedbackläufe konnten momentan nicht "
            "geladen werden."
        )

    notice_message, notice_tone = _feedback_evaluation_notice(
        notice,
        imported_run_count,
        imported_evaluation_count,
    )

    return templates.TemplateResponse(
        name="feedback_evaluations.html",
        request=request,
        context={
            "app_name": settings.app_name,
            "authenticated_user": authenticated_user,
            "csrf_token": get_or_create_csrf_token(
                request.session
            ),
            "feedback_runs": feedback_runs,
            "manual_meta_rubric": MANUAL_META_EVALUATION_RUBRIC,
            "meta_evaluation_score_options": (
                META_EVALUATION_SCORE_OPTIONS
            ),
            "max_meta_justification_chars": (
                MAX_META_JUSTIFICATION_CHARS
            ),
            "max_evaluation_name_chars": (
                MAX_EVALUATION_NAME_CHARS
            ),
            "automatic_evaluation_configured": (
                automatic_evaluation_provider.configured
            ),
            "automatic_evaluation_provider": (
                automatic_evaluation_provider.provider_name
            ),
            "automatic_evaluation_model": (
                automatic_evaluation_provider.model_name
            ),
            "automatic_evaluation_model_options": (
                _openai_evaluation_model_options()
            ),
            "automatic_evaluation_reasoning_mode": (
                OPENAI_EVALUATION_REASONING_MODE
            ),
            "automatic_evaluation_reasoning_mode_options": (
                OPENAI_EVALUATION_REASONING_MODE_OPTIONS
            ),
            "automatic_evaluation_reasoning_effort": (
                OPENAI_EVALUATION_REASONING_EFFORT
            ),
            "automatic_evaluation_reasoning_effort_options": (
                OPENAI_REASONING_EFFORT_OPTIONS
            ),
            "automatic_evaluation_prompt_version": (
                AUTOMATIC_EVALUATION_PROMPT_VERSION
            ),
            "notice_code": notice,
            "automatic_feedback_run_id": (
                automatic_feedback_run_id
            ),
            "feedback_run_notice_id": feedback_run_notice_id,
            "notice_message": notice_message,
            "notice_tone": notice_tone,
            "error": error,
        },
    )


@app.get("/feedback-evaluations/export-json")
async def export_feedback_evaluations_json(
    request: Request,
) -> Response:
    if _authenticated_user(request) is None:
        return _redirect_to_login()

    try:
        feedback_runs = await task_store.list_feedback_runs_for_evaluation(
            limit=1000
        )
        content = FeedbackEvaluationExchangeService.export_json(
            feedback_runs
        )
    except TaskStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        ) from exc
    except FeedbackEvaluationExchangeError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc

    return _rubric_download_response(
        content,
        "meta-bewertungen.json",
    )


@app.get("/feedback-evaluations/export-csv")
async def export_feedback_evaluations_csv(
    request: Request,
) -> Response:
    if _authenticated_user(request) is None:
        return _redirect_to_login()

    try:
        feedback_runs = await task_store.list_feedback_runs_for_evaluation(
            limit=1000
        )
        content = FeedbackEvaluationExchangeService.export_csv(
            feedback_runs
        )
    except TaskStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        ) from exc
    except FeedbackEvaluationExchangeError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc

    return _rubric_download_response(
        content,
        "meta-bewertungen.csv",
        media_type="text/csv",
    )


@app.post("/feedback-evaluations/import")
async def import_feedback_evaluations(
    request: Request,
    import_file: UploadFile = File(...),
    csrf_token: str = Form("", max_length=256),
) -> RedirectResponse:
    if _authenticated_user(request) is None:
        return _redirect_to_login()

    _require_valid_csrf_token(request, csrf_token)

    try:
        content = await import_file.read(
            MAX_FEEDBACK_EVALUATION_IMPORT_BYTES + 1
        )
    except OSError:
        logger.exception(
            "Eine Meta-Bewertungs-Importdatei konnte nicht gelesen werden."
        )
        return RedirectResponse(
            url=(
                "/feedback-evaluations?notice="
                "evaluation-import-failed"
            ),
            status_code=303,
        )
    finally:
        await import_file.close()

    try:
        feedback_runs = FeedbackEvaluationExchangeService.parse_import(
            content
        )
        imported_run_count, imported_evaluation_count = (
            await task_store.import_feedback_evaluation_runs(feedback_runs)
        )
    except FeedbackEvaluationExchangeError as exc:
        logger.warning("Meta-Bewertungs-Import abgelehnt: %s", exc)
        return RedirectResponse(
            url=(
                "/feedback-evaluations?notice="
                "evaluation-import-failed"
            ),
            status_code=303,
        )
    except (ValueError, TaskStoreError):
        logger.exception(
            "Meta-Bewertungen konnten nicht importiert werden."
        )
        return RedirectResponse(
            url=(
                "/feedback-evaluations?notice="
                "evaluation-import-failed"
            ),
            status_code=303,
        )

    return RedirectResponse(
        url=(
            "/feedback-evaluations?notice=evaluation-imported"
            f"&imported_run_count={imported_run_count}"
            f"&imported_evaluation_count={imported_evaluation_count}"
        ),
        status_code=303,
    )


@app.post(
    "/feedback-runs/{feedback_run_id}/criteria/"
    "{criterion_id}/refresh",
    response_class=HTMLResponse,
)
async def refresh_feedback_run_criterion(
    request: Request,
    feedback_run_id: UUID,
    criterion_id: str,
    student_text: str = Form(
        ...,
        max_length=settings.max_input_chars,
    ),
    provider: str = Form(..., max_length=32),
    csrf_token: str = Form("", max_length=256),
    ollama_base_url: str = Form(""),
    ollama_model: str = Form(""),
    ollama_custom_model: str = Form(""),
    openai_model: str = Form(""),
    openai_custom_model: str = Form(""),
    openai_reasoning_effort: str = Form("", max_length=16),
    openai_api_key: str = Form(""),
    mistral_model: str = Form(""),
    mistral_custom_model: str = Form(""),
    mistral_api_key: str = Form(""),
    runpod_endpoint: str = Form("", max_length=64),
    runpod_tracking_id: str = Form("", max_length=64),
) -> Response:
    if _authenticated_user(request) is None:
        return _redirect_to_login()

    _require_valid_csrf_token(request, csrf_token)

    refreshed_item = None
    criterion_position = 0
    refresh_count = 0
    error: str | None = None

    try:
        if not criterion_id or len(criterion_id) > 100:
            raise FeedbackRunRefreshError(
                "Das ausgewählte Feedback-Kriterium ist ungültig."
            )

        stored_run = await task_store.get_feedback_run_for_refresh(
            feedback_run_id=str(feedback_run_id),
            student_text=student_text,
        )

        if provider != stored_run.provider:
            raise FeedbackRunRefreshError(
                "Für die Einzelaktualisierung muss derselbe Anbieter wie "
                "beim ursprünglichen Feedbacklauf ausgewählt bleiben."
            )

        if provider == "runpod" and not runpod_tracking_id.strip():
            runpod_tracking_id = str(uuid4())

        provider_override = _provider_for_request(
            provider_key=provider,
            ollama_base_url=ollama_base_url,
            ollama_model=ollama_model,
            ollama_custom_model=ollama_custom_model,
            openai_model=openai_model,
            openai_custom_model=openai_custom_model,
            openai_reasoning_effort=openai_reasoning_effort,
            openai_api_key=openai_api_key,
            mistral_model=mistral_model,
            mistral_custom_model=mistral_custom_model,
            mistral_api_key=mistral_api_key,
            runpod_endpoint=runpod_endpoint,
            runpod_tracking_id=runpod_tracking_id,
        )
        selected_model = getattr(provider_override, "model_name", "")
        selected_reasoning_effort = getattr(
            provider_override,
            "reasoning_effort",
            None,
        )

        if (
            selected_model != stored_run.model
            or selected_reasoning_effort
            != stored_run.reasoning_effort
        ):
            raise FeedbackRunRefreshError(
                "Modell und Denktiefe müssen für die "
                "Einzelaktualisierung unverändert bleiben."
            )

        single_result = (
            await criterion_wise_rubric_feedback_service.analyze_criterion(
                student_text=student_text,
                task=stored_run.task,
                criterion_id=criterion_id,
                original_text=stored_run.original_text,
                provider_key=provider,
                provider_override=provider_override,
            )
        )
        refreshed_item = single_result.criteria_feedback[0]
        refresh_count = await task_store.update_feedback_run_criterion(
            feedback_run_id=str(feedback_run_id),
            student_text=student_text,
            provider=single_result.provider,
            model=single_result.model,
            reasoning_effort=single_result.reasoning_effort,
            criterion_payload=refreshed_item.payload(),
            overall_feedback=CRITERION_REFRESH_OVERALL_FEEDBACK,
            prompt_version=single_result.prompt_version,
            evidence_validation_version=(
                single_result.evidence_validation_version
            ),
            duration_ms=single_result.duration_ms,
            queue_duration_ms=single_result.queue_duration_ms,
            execution_duration_ms=(
                single_result.execution_duration_ms
            ),
            provider_request_id=single_result.provider_request_id,
            evidence_repair_attempts=tuple(
                item.payload()
                for item in single_result.evidence_repair_attempts
            ),
        )
        criterion_position = next(
            position
            for position, criterion in enumerate(
                stored_run.task.rubric.criteria,
                start=1,
            )
            if criterion.criterion_id == criterion_id
        )

        if single_result.provider == "runpod":
            endpoint_key = (
                runpod_endpoint.strip().lower()
                or RUNPOD_DEFAULT_ENDPOINT_KEY
            )
            runpod_status_service.mark_success(endpoint_key)

    except Exception as exc:
        logger.error(
            "Einzelnes Kriterienfeedback konnte nicht aktualisiert werden "
            "(feedback_run_id=%s, criterion_id=%s, provider=%s, "
            "error_type=%s).",
            feedback_run_id,
            criterion_id,
            provider,
            type(exc).__name__,
        )
        error = str(exc).strip() or (
            "Das einzelne Kriterium konnte nicht aktualisiert werden."
        )

    response = templates.TemplateResponse(
        name="criterion_refresh_response.html",
        request=request,
        context={
            "item": refreshed_item,
            "criterion_position": criterion_position,
            "feedback_run_id": str(feedback_run_id),
            "refresh_count": refresh_count,
            "overall_feedback": CRITERION_REFRESH_OVERALL_FEEDBACK,
            "refresh_notice": (
                "Dieses Kriterium wurde einzeln neu analysiert und im "
                "aktuellen Feedbacklauf ersetzt."
                if refreshed_item is not None
                else None
            ),
            "error": error,
        },
    )
    response.headers["X-Criterion-Refresh-Outcome"] = (
        "error" if error is not None else "success"
    )
    return response


@app.post("/feedback-runs/{feedback_run_id}/save")
async def save_feedback_run_for_evaluation(
    request: Request,
    feedback_run_id: UUID,
    student_text: str = Form(
        ...,
        max_length=settings.max_input_chars,
    ),
    csrf_token: str = Form("", max_length=256),
) -> RedirectResponse:
    if _authenticated_user(request) is None:
        return _redirect_to_login()

    _require_valid_csrf_token(request, csrf_token)

    try:
        await task_store.select_feedback_run_for_evaluation(
            feedback_run_id=str(feedback_run_id),
            student_text=student_text,
        )
        notice = "saved"
    except TaskStoreError:
        notice = "save-failed"

    return RedirectResponse(
        url=f"/feedback-evaluations?notice={notice}",
        status_code=303,
    )


@app.post(
    "/feedback-runs/{feedback_run_id}/automatic-evaluations"
)
async def create_automatic_feedback_evaluation(
    request: Request,
    feedback_run_id: UUID,
    evaluation_model: str = Form(
        "",
        max_length=MAX_MODEL_NAME_CHARS,
    ),
    evaluation_reasoning_mode: str = Form("", max_length=16),
    evaluation_reasoning_effort: str = Form(
        OPENAI_EVALUATION_REASONING_EFFORT,
        max_length=16,
    ),
    evaluation_name: str = Form(
        "",
        max_length=MAX_EVALUATION_NAME_CHARS,
    ),
    csrf_token: str = Form("", max_length=256),
) -> RedirectResponse:
    if _authenticated_user(request) is None:
        return _redirect_to_login()

    _require_valid_csrf_token(request, csrf_token)

    try:
        selected_model = _validate_openai_evaluation_model(
            evaluation_model
        )
        selected_reasoning_mode = (
            _validate_openai_evaluation_reasoning_mode(
                evaluation_reasoning_mode
            )
        )
        selected_reasoning_effort = (
            _validate_openai_reasoning_effort(
                evaluation_reasoning_effort
            )
        )
        stored_feedback_run = (
            await task_store.get_feedback_run_for_evaluation(
                str(feedback_run_id)
            )
        )
        result = await automatic_feedback_evaluation_service.evaluate(
            stored_feedback_run,
            model_name=selected_model,
            reasoning_mode=selected_reasoning_mode,
            reasoning_effort=selected_reasoning_effort,
        )
        await task_store.create_automatic_feedback_evaluation(
            feedback_run_id=str(feedback_run_id),
            scores={
                rating.criterion_key: rating.score
                for rating in result.ratings
            },
            justifications={
                rating.criterion_key: rating.justification
                for rating in result.ratings
            },
            evaluator_provider=result.provider,
            evaluator_model=result.model,
            evaluator_reasoning_mode=result.reasoning_mode,
            evaluator_reasoning_effort=result.reasoning_effort,
            evaluator_prompt_version=result.prompt_version,
            duration_ms=result.duration_ms,
            provider_request_id=result.provider_request_id,
            evaluation_name=evaluation_name,
        )
        notice = "automatic-evaluation-saved"
    except Exception:
        logger.exception(
            "Die automatische Feedbackvorbewertung ist fehlgeschlagen."
        )
        notice = "automatic-evaluation-failed"

    return RedirectResponse(
        url=(
            f"/feedback-evaluations?notice={notice}"
            f"&automatic_feedback_run_id={feedback_run_id}"
            f"#feedback-run-{feedback_run_id}"
        ),
        status_code=303,
    )


@app.post(
    "/feedback-runs/{feedback_run_id}/manual-evaluations"
)
async def create_manual_feedback_evaluation(
    request: Request,
    feedback_run_id: UUID,
    score_factual_correctness: int = Form(..., ge=0, le=3),
    justification_factual_correctness: str = Form(
        ...,
        min_length=1,
        max_length=MAX_META_JUSTIFICATION_CHARS,
    ),
    score_transparency_reasoning: int = Form(..., ge=0, le=3),
    justification_transparency_reasoning: str = Form(
        ...,
        min_length=1,
        max_length=MAX_META_JUSTIFICATION_CHARS,
    ),
    score_audience_context_fit: int = Form(..., ge=0, le=3),
    justification_audience_context_fit: str = Form(
        ...,
        min_length=1,
        max_length=MAX_META_JUSTIFICATION_CHARS,
    ),
    score_action_learning_activation: int = Form(
        ...,
        ge=0,
        le=3,
    ),
    justification_action_learning_activation: str = Form(
        ...,
        min_length=1,
        max_length=MAX_META_JUSTIFICATION_CHARS,
    ),
    source_evaluation_id: str = Form("", max_length=64),
    evaluation_name: str = Form(
        "",
        max_length=MAX_EVALUATION_NAME_CHARS,
    ),
    csrf_token: str = Form("", max_length=256),
) -> RedirectResponse:
    if _authenticated_user(request) is None:
        return _redirect_to_login()

    _require_valid_csrf_token(request, csrf_token)

    try:
        await task_store.create_manual_feedback_evaluation(
            feedback_run_id=str(feedback_run_id),
            scores={
                "factual_correctness": score_factual_correctness,
                "transparency_reasoning": score_transparency_reasoning,
                "audience_context_fit": score_audience_context_fit,
                "action_learning_activation": (
                    score_action_learning_activation
                ),
            },
            justifications={
                "factual_correctness": (
                    justification_factual_correctness
                ),
                "transparency_reasoning": (
                    justification_transparency_reasoning
                ),
                "audience_context_fit": (
                    justification_audience_context_fit
                ),
                "action_learning_activation": (
                    justification_action_learning_activation
                ),
            },
            source_evaluation_id=source_evaluation_id,
            evaluation_name=evaluation_name,
        )
        notice = "evaluation-saved"
    except TaskStoreError:
        notice = "evaluation-failed"

    return RedirectResponse(
        url=(
            f"/feedback-evaluations?notice={notice}"
            f"&feedback_run_notice_id={feedback_run_id}"
            f"#feedback-run-{feedback_run_id}"
        ),
        status_code=303,
    )


@app.post(
    "/feedback-runs/{feedback_run_id}/evaluations/"
    "{evaluation_id}/delete"
)
async def delete_feedback_evaluation(
    request: Request,
    feedback_run_id: UUID,
    evaluation_id: UUID,
    csrf_token: str = Form("", max_length=256),
) -> RedirectResponse:
    if _authenticated_user(request) is None:
        return _redirect_to_login()

    _require_valid_csrf_token(request, csrf_token)

    try:
        await task_store.delete_feedback_evaluation(
            feedback_run_id=str(feedback_run_id),
            evaluation_id=str(evaluation_id),
        )
        notice = "evaluation-deleted"
    except FeedbackEvaluationDeleteConflictError:
        notice = "evaluation-delete-linked"
    except TaskStoreError:
        logger.exception(
            "Eine gespeicherte Feedbackbewertung konnte nicht "
            "gelöscht werden."
        )
        notice = "evaluation-delete-failed"

    return RedirectResponse(
        url=(
            f"/feedback-evaluations?notice={notice}"
            f"&feedback_run_notice_id={feedback_run_id}"
            f"#feedback-run-{feedback_run_id}"
        ),
        status_code=303,
    )


@app.get(
    "/feedback-runs/{feedback_run_id}/evaluations/"
    "{evaluation_id}/pdf"
)
async def export_feedback_evaluation_pdf(
    request: Request,
    feedback_run_id: UUID,
    evaluation_id: UUID,
) -> Response:
    if _authenticated_user(request) is None:
        return _redirect_to_login()

    try:
        feedback_run = await task_store.get_feedback_run_for_evaluation(
            str(feedback_run_id)
        )
    except TaskStoreError as exc:
        raise HTTPException(
            status_code=404,
            detail={"message": "Der Feedbacklauf wurde nicht gefunden."},
        ) from exc

    evaluation = next(
        (
            item
            for item in feedback_run.evaluations
            if item.evaluation_id == str(evaluation_id)
        ),
        None,
    )

    if evaluation is None:
        raise HTTPException(
            status_code=404,
            detail={"message": "Die Bewertung wurde nicht gefunden."},
        )

    try:
        pdf = feedback_evaluation_pdf_service.render(
            feedback_run=feedback_run,
            evaluation=evaluation,
        )
    except FeedbackEvaluationPdfError as exc:
        logger.exception(
            "Eine gespeicherte Meta-Bewertung konnte nicht als PDF "
            "exportiert werden."
        )
        raise HTTPException(
            status_code=500,
            detail={
                "message": (
                    "Der PDF-Export konnte nicht erzeugt werden."
                )
            },
        ) from exc

    return Response(
        content=pdf.content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{pdf.filename}"'
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post(
    "/feedback-runs/{feedback_run_id}/remove-from-evaluation"
)
async def remove_feedback_run_from_evaluation(
    request: Request,
    feedback_run_id: UUID,
    csrf_token: str = Form("", max_length=256),
) -> RedirectResponse:
    if _authenticated_user(request) is None:
        return _redirect_to_login()

    _require_valid_csrf_token(request, csrf_token)

    try:
        await task_store.remove_feedback_run_from_evaluation(
            feedback_run_id=str(feedback_run_id),
        )
        return RedirectResponse(
            url=(
                "/feedback-evaluations?notice="
                "feedback-run-removed"
            ),
            status_code=303,
        )
    except TaskStoreError:
        logger.exception(
            "Ein Feedbackbogen konnte nicht aus der "
            "Feedback-Bewertung entfernt werden."
        )

    return RedirectResponse(
        url=(
            "/feedback-evaluations?notice="
            "feedback-run-remove-failed"
        ),
        status_code=303,
    )


@app.get("/tasks", response_class=HTMLResponse)
async def tasks_page(
    request: Request,
    notice: str = Query(default="", max_length=32),
    imported_count: int = Query(
        default=0,
        ge=0,
        le=MAX_RUBRIC_BUNDLE_TASKS,
    ),
) -> Response:
    authenticated_user = _authenticated_user(request)

    if authenticated_user is None:
        return _redirect_to_login()

    error: str | None = None

    try:
        stored_tasks = await task_store.list_tasks(
            include_archived=True
        )
        default_task_id = (
            await task_store.get_default_feedback_task_id()
        )
        tasks = [
            task
            for task in stored_tasks
            if task.archived_at is None
        ]
        export_available = bool(stored_tasks)
    except TaskStoreError as exc:
        tasks = []
        default_task_id = None
        export_available = False
        error = str(exc)

    return templates.TemplateResponse(
        name="tasks.html",
        request=request,
        context={
            "app_name": settings.app_name,
            "authenticated_user": authenticated_user,
            "csrf_token": get_or_create_csrf_token(
                request.session
            ),
            "tasks": tasks,
            "default_task_id": default_task_id,
            "export_available": export_available,
            "notice": _task_notice_message(
                notice,
                imported_count,
            ),
            "error": error,
        },
    )


@app.get("/tasks/export-all")
async def export_all_tasks(
    request: Request,
) -> Response:
    if _authenticated_user(request) is None:
        return _redirect_to_login()

    try:
        tasks = await task_store.list_tasks(include_archived=True)
    except TaskStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        ) from exc

    if not tasks:
        raise HTTPException(
            status_code=404,
            detail="Es sind keine Feedback-Vorlagen für den Export vorhanden.",
        )

    try:
        content = RubricExchangeService.export_collection_bundle(tasks)
    except RubricExchangeError as exc:
        raise HTTPException(
            status_code=413,
            detail=str(exc),
        ) from exc

    return _rubric_download_response(
        content,
        "feedback-gesamt.zip",
        media_type="application/zip",
    )


@app.get("/tasks/{task_id}/export")
async def export_task(
    request: Request,
    task_id: str,
) -> Response:
    if _authenticated_user(request) is None:
        return _redirect_to_login()

    try:
        task = await task_store.get_task(task_id)
    except TaskStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        ) from exc

    if task is None:
        raise HTTPException(
            status_code=404,
            detail="Die Aufgabe wurde nicht gefunden.",
        )

    return _rubric_download_response(
        RubricExchangeService.export_task(task),
        "feedback-einzeln.json",
    )


@app.post("/tasks/import", response_class=HTMLResponse)
async def import_tasks(
    request: Request,
    import_file: UploadFile = File(...),
    csrf_token: str = Form("", max_length=256),
) -> Response:
    authenticated_user = _authenticated_user(request)

    if authenticated_user is None:
        return _redirect_to_login()

    _require_valid_csrf_token(request, csrf_token)

    try:
        prefix = await import_file.read(4)
        maximum_bytes = (
            MAX_RUBRIC_BUNDLE_BYTES
            if RubricExchangeService.is_bundle_content(prefix)
            else MAX_RUBRIC_IMPORT_BYTES
        )
        content = prefix + await import_file.read(
            maximum_bytes - len(prefix) + 1
        )
    except OSError as exc:
        raise HTTPException(
            status_code=400,
            detail="Die Importdatei konnte nicht gelesen werden.",
        ) from exc
    finally:
        await import_file.close()

    try:
        drafts = RubricExchangeService.parse_import(content)
        imported_tasks = await task_store.create_tasks(drafts)
    except (RubricExchangeError, ValueError, TaskStoreError) as exc:
        try:
            stored_tasks = await task_store.list_tasks(
                include_archived=True
            )
            tasks = [
                task
                for task in stored_tasks
                if task.archived_at is None
            ]
            export_available = bool(stored_tasks)
        except TaskStoreError:
            tasks = []
            export_available = False

        return templates.TemplateResponse(
            name="tasks.html",
            request=request,
            context={
                "app_name": settings.app_name,
                "authenticated_user": authenticated_user,
                "csrf_token": get_or_create_csrf_token(
                    request.session
                ),
                "tasks": tasks,
                "export_available": export_available,
                "notice": None,
                "error": str(exc),
            },
            status_code=(
                422
                if isinstance(exc, (RubricExchangeError, ValueError))
                else 500
            ),
        )

    return RedirectResponse(
        url=(
            "/tasks?notice=imported&imported_count="
            f"{len(imported_tasks)}"
        ),
        status_code=303,
    )


@app.get("/tasks/new", response_class=HTMLResponse)
async def new_task_page(
    request: Request,
) -> Response:
    authenticated_user = _authenticated_user(request)

    if authenticated_user is None:
        return _redirect_to_login()

    return templates.TemplateResponse(
        name="task_form.html",
        request=request,
        context=_task_form_context(
            request=request,
            authenticated_user=authenticated_user,
            task=None,
            values=_task_form_values(),
        ),
    )


@app.post("/tasks/new", response_class=HTMLResponse)
async def create_task(
    request: Request,
    title: str = Form(""),
    subject: str = Form(""),
    grade_level: str = Form(""),
    instructions: str = Form(""),
    material: str = Form(""),
    rubric_title: str = Form(""),
    criteria: list[str] = Form(...),
    criterion_titles: list[str] | None = Form(None),
    csrf_token: str = Form("", max_length=256),
) -> Response:
    authenticated_user = _authenticated_user(request)

    if authenticated_user is None:
        return _redirect_to_login()

    _require_valid_csrf_token(request, csrf_token)
    values = _submitted_task_form_values(
        title=title,
        subject=subject,
        grade_level=grade_level,
        instructions=instructions,
        material=material,
        rubric_title=rubric_title,
        criteria=criteria,
        criterion_titles=criterion_titles,
    )

    try:
        await task_store.create_task(
            title=title,
            subject=subject,
            grade_level=grade_level,
            instructions=instructions,
            material=material,
            rubric_title=rubric_title,
            criteria=criteria,
            criterion_titles=criterion_titles,
        )
    except (ValueError, TaskStoreError) as exc:
        return templates.TemplateResponse(
            name="task_form.html",
            request=request,
            context=_task_form_context(
                request=request,
                authenticated_user=authenticated_user,
                task=None,
                values=values,
                error=str(exc),
            ),
            status_code=(
                422 if isinstance(exc, ValueError) else 500
            ),
        )

    return RedirectResponse(
        url="/tasks?notice=created",
        status_code=303,
    )


@app.get("/tasks/{task_id}/edit", response_class=HTMLResponse)
async def edit_task_page(
    request: Request,
    task_id: str,
) -> Response:
    authenticated_user = _authenticated_user(request)

    if authenticated_user is None:
        return _redirect_to_login()

    task = await task_store.get_task(task_id)

    if task is None:
        raise HTTPException(
            status_code=404,
            detail="Die Aufgabe wurde nicht gefunden.",
        )

    return templates.TemplateResponse(
        name="task_form.html",
        request=request,
        context=_task_form_context(
            request=request,
            authenticated_user=authenticated_user,
            task=task,
            values=_task_form_values(task),
        ),
    )


@app.post("/tasks/{task_id}/edit", response_class=HTMLResponse)
async def update_task(
    request: Request,
    task_id: str,
    title: str = Form(""),
    subject: str = Form(""),
    grade_level: str = Form(""),
    instructions: str = Form(""),
    material: str = Form(""),
    rubric_title: str = Form(""),
    criteria: list[str] = Form(...),
    criterion_titles: list[str] | None = Form(None),
    csrf_token: str = Form("", max_length=256),
) -> Response:
    authenticated_user = _authenticated_user(request)

    if authenticated_user is None:
        return _redirect_to_login()

    _require_valid_csrf_token(request, csrf_token)
    task = await task_store.get_task(task_id)

    if task is None:
        raise HTTPException(
            status_code=404,
            detail="Die Aufgabe wurde nicht gefunden.",
        )

    values = _submitted_task_form_values(
        title=title,
        subject=subject,
        grade_level=grade_level,
        instructions=instructions,
        material=material,
        rubric_title=rubric_title,
        criteria=criteria,
        criterion_titles=criterion_titles,
    )

    try:
        await task_store.update_task(
            task_id,
            title=title,
            subject=subject,
            grade_level=grade_level,
            instructions=instructions,
            material=material,
            rubric_title=rubric_title,
            criteria=criteria,
            criterion_titles=criterion_titles,
        )
    except (ValueError, TaskStoreError) as exc:
        return templates.TemplateResponse(
            name="task_form.html",
            request=request,
            context=_task_form_context(
                request=request,
                authenticated_user=authenticated_user,
                task=task,
                values=values,
                error=str(exc),
            ),
            status_code=(
                422 if isinstance(exc, ValueError) else 500
            ),
        )

    return RedirectResponse(
        url="/tasks?notice=updated",
        status_code=303,
    )


@app.post("/tasks/{task_id}/duplicate")
async def duplicate_task(
    request: Request,
    task_id: str,
    csrf_token: str = Form("", max_length=256),
) -> RedirectResponse:
    if _authenticated_user(request) is None:
        return _redirect_to_login()

    _require_valid_csrf_token(request, csrf_token)

    try:
        await task_store.duplicate_task(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc

    return RedirectResponse(
        url="/tasks?notice=duplicated",
        status_code=303,
    )


@app.post("/tasks/{task_id}/delete")
async def delete_task(
    request: Request,
    task_id: str,
    csrf_token: str = Form("", max_length=256),
) -> RedirectResponse:
    if _authenticated_user(request) is None:
        return _redirect_to_login()

    _require_valid_csrf_token(request, csrf_token)

    try:
        delete_result = await task_store.delete_task(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc

    return RedirectResponse(
        url=f"/tasks?notice={delete_result.action}",
        status_code=303,
    )


@app.post("/tasks/{task_id}/default")
async def set_default_feedback_task(
    request: Request,
    task_id: str,
    csrf_token: str = Form("", max_length=256),
) -> RedirectResponse:
    if _authenticated_user(request) is None:
        return _redirect_to_login()

    _require_valid_csrf_token(request, csrf_token)

    try:
        await task_store.set_default_feedback_task(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc

    return RedirectResponse(
        url="/tasks?notice=default",
        status_code=303,
    )


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
) -> Response:
    authenticated_user = _authenticated_user(request)

    if authenticated_user is None:
        return _redirect_to_login()

    storage_warning: str | None = None

    try:
        tasks = await task_store.list_tasks()
        default_task_id = (
            await task_store.get_default_feedback_task_id()
        )
    except TaskStoreError:
        tasks = []
        default_task_id = None
        storage_warning = (
            "Die Feedback-Vorlagen konnten momentan nicht geladen werden. "
            "Bitte versuche es nach einem Neustart erneut."
        )

    return templates.TemplateResponse(
        name="index.html",
        request=request,
        context=_template_context(
            csrf_token=get_or_create_csrf_token(
                request.session
            ),
            authenticated_user=authenticated_user,
            task_options=tasks,
            selected_task_id=default_task_id or "",
            default_task_id=default_task_id or "",
            storage_warning=storage_warning,
        ),
    )


@app.get("/api/ollama/models")
async def ollama_models(
    request: Request,
    base_url: str = Query(
        default=OLLAMA_FALLBACK_BASE_URL,
        max_length=2048,
    ),
) -> dict[str, object]:
    if _authenticated_user(request) is None:
        raise HTTPException(
            status_code=401,
            detail={"message": "Anmeldung erforderlich."},
        )

    if not _ollama_available():
        raise HTTPException(
            status_code=403,
            detail={
                "message": (
                    "Ollama ist im Produktionsbetrieb deaktiviert."
                )
            },
        )

    try:
        effective_base_url = (
            base_url
            if settings.browser_overrides_allowed
            else settings.ollama_base_url
        )
        validated_base_url = (
            _validate_ollama_base_url(effective_base_url)
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc)},
        ) from exc

    provider = OllamaProvider(
        base_url=validated_base_url,
        model_name=settings.ollama_model,
        request_timeout_seconds=(
            settings.ollama_request_timeout_seconds
        ),
    )

    try:
        models = await provider.discover_models()
    except (httpx.HTTPError, ValueError) as exc:
        location = (
            f" unter {validated_base_url}"
            if settings.browser_overrides_allowed
            else ""
        )
        raise HTTPException(
            status_code=502,
            detail={
                "message": (
                    f"Ollama ist{location} "
                    "nicht erreichbar. Prüfe, ob Ollama "
                    "läuft und korrekt konfiguriert ist."
                )
            },
        ) from exc

    return {
        "base_url": (
            validated_base_url
            if settings.browser_overrides_allowed
            else None
        ),
        "models": models,
        "default_model": settings.ollama_model,
        "message": (
            f"{len(models)} installierte "
            "Ollama-Modelle geladen."
        ),
    }


@app.get("/api/runpod/status")
async def runpod_status(
    request: Request,
    response: Response,
    endpoint_key: str = Query(
        default=RUNPOD_DEFAULT_ENDPOINT_KEY,
        max_length=64,
    ),
) -> dict[str, object]:
    """Liefert Status- und Supply-Daten ohne Inferenzauftrag."""

    if _authenticated_user(request) is None:
        raise HTTPException(
            status_code=401,
            detail={"message": "Anmeldung erforderlich."},
        )

    response.headers["Cache-Control"] = "no-store"

    definition = _runpod_endpoint_definition(endpoint_key)

    if definition is None:
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    "Die ausgewählte RunPod-Hardwarekonfiguration "
                    "ist nicht erlaubt."
                )
            },
        )

    (
        allowed_key,
        label,
        settings_attribute,
        environment_variable,
        gpu_type_ids,
    ) = definition
    endpoint_id = getattr(settings, settings_attribute)

    if not endpoint_id:
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    f"{label} ist noch nicht konfiguriert. "
                    f"Hinterlege {environment_variable} in der "
                    ".env-Datei."
                )
            },
        )

    return await runpod_status_service.snapshot(
        endpoint_key=allowed_key,
        endpoint_label=label,
        endpoint_id=endpoint_id,
        gpu_type_ids=gpu_type_ids,
    )


def _tracked_runpod_job_payload(
    job: RunPodTrackedJob,
    *,
    status_fresh: bool,
) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    age_seconds = max(
        0,
        int((now - job.created_at).total_seconds()),
    )

    return {
        "trackingId": job.tracking_id,
        "jobId": job.job_id,
        "endpointKey": job.endpoint_key,
        "status": job.status,
        "createdAt": job.created_at.isoformat(),
        "updatedAt": job.updated_at.isoformat(),
        "ageSeconds": age_seconds,
        "statusFresh": status_fresh,
    }


async def _refresh_tracked_runpod_job(
    job: RunPodTrackedJob,
) -> dict[str, object] | None:
    provider = _runpod_provider(endpoint_id=job.endpoint_id)

    try:
        payload = await asyncio.wait_for(
            provider.get_job_status(job.job_id),
            timeout=RUNPOD_JOB_STATUS_REFRESH_TIMEOUT_SECONDS,
        )
        raw_status = payload.get("status")

        if not isinstance(raw_status, str) or not raw_status.strip():
            return _tracked_runpod_job_payload(
                job,
                status_fresh=False,
            )

        status = raw_status.strip().upper()
        await runpod_job_store.update_known_job(
            endpoint_id=job.endpoint_id,
            job_id=job.job_id,
            status=status,
        )

        if status not in ACTIVE_RUNPOD_JOB_STATUSES:
            return None

        refreshed_job = RunPodTrackedJob(
            tracking_id=job.tracking_id,
            job_id=job.job_id,
            endpoint_key=job.endpoint_key,
            endpoint_id=job.endpoint_id,
            status=status,
            created_at=job.created_at,
            updated_at=datetime.now(timezone.utc),
        )
        return _tracked_runpod_job_payload(
            refreshed_job,
            status_fresh=True,
        )
    except ProviderError as exc:
        if exc.status_code == 404:
            await runpod_job_store.update_known_job(
                endpoint_id=job.endpoint_id,
                job_id=job.job_id,
                status="NOT_FOUND",
            )
            return None

        return _tracked_runpod_job_payload(
            job,
            status_fresh=False,
        )
    except asyncio.TimeoutError:
        return _tracked_runpod_job_payload(
            job,
            status_fresh=False,
        )


@app.get("/api/runpod/jobs")
async def runpod_jobs(
    request: Request,
    response: Response,
    endpoint_key: str = Query(
        default=RUNPOD_DEFAULT_ENDPOINT_KEY,
        max_length=64,
    ),
) -> dict[str, object]:
    """Liefert aktive, von dieser Web-App registrierte RunPod-Jobs."""

    if _authenticated_user(request) is None:
        raise HTTPException(
            status_code=401,
            detail={"message": "Anmeldung erforderlich."},
        )

    response.headers["Cache-Control"] = "no-store"

    try:
        allowed_key, label, _endpoint_id = (
            _configured_runpod_endpoint(endpoint_key)
        )
        tracked_jobs = await runpod_job_store.list_active(
            endpoint_key=allowed_key,
        )
        refreshed_jobs = await asyncio.gather(
            *(
                _refresh_tracked_runpod_job(job)
                for job in tracked_jobs
            )
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc)},
        ) from exc
    except RunPodJobStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail={"message": str(exc)},
        ) from exc

    return {
        "endpoint": {
            "key": allowed_key,
            "label": label,
        },
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "jobs": [
            job
            for job in refreshed_jobs
            if job is not None
        ],
    }


@app.post("/api/runpod/jobs/cancel")
async def cancel_runpod_job(
    request: Request,
    endpoint_key: str = Form(..., max_length=64),
    job_id: str = Form(..., max_length=200),
    csrf_token: str = Form("", max_length=256),
) -> dict[str, object]:
    """Bricht genau einen Job eines erlaubten RunPod-Endpoints ab."""

    if _authenticated_user(request) is None:
        raise HTTPException(
            status_code=401,
            detail={"message": "Anmeldung erforderlich."},
        )

    _require_valid_csrf_token(request, csrf_token)

    try:
        allowed_key, label, endpoint_id = (
            _configured_runpod_endpoint(endpoint_key)
        )
        provider = _runpod_provider(endpoint_id=endpoint_id)
        payload = await provider.cancel_job(job_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc)},
        ) from exc
    except ProviderError as exc:
        if isinstance(exc, ProviderInvalidRequestError):
            status_code = 422
        elif exc.status_code == 404:
            status_code = 404
        else:
            status_code = 502
        raise HTTPException(
            status_code=status_code,
            detail={"message": exc.message},
        ) from exc

    normalized_job_id = job_id.strip()
    status = str(payload.get("status") or "CANCELLED").strip().upper()

    try:
        await runpod_job_store.update_known_job(
            endpoint_id=endpoint_id,
            job_id=normalized_job_id,
            status=status,
        )
    except RunPodJobStoreError:
        pass

    return {
        "endpoint": {
            "key": allowed_key,
            "label": label,
        },
        "jobId": normalized_job_id,
        "status": status,
        "message": "Die RunPod-Anfrage wurde abgebrochen.",
    }


@app.post("/analyze", response_class=HTMLResponse)
async def analyze(
    request: Request,
    student_text: str = Form(...),
    provider: str = Form(...),
    task_id: str = Form("", max_length=100),
    original_text: str = Form(
        "",
        max_length=MAX_MATERIAL_CHARS,
    ),
    rubric_analysis_mode: str = Form("", max_length=32),
    two_pass_feedback: bool = Form(False),
    advanced_options: bool = Form(False),
    csrf_token: str = Form("", max_length=256),
    ollama_base_url: str = Form(""),
    ollama_model: str = Form(""),
    ollama_custom_model: str = Form(""),
    openai_model: str = Form(""),
    openai_custom_model: str = Form(""),
    openai_reasoning_effort: str = Form("", max_length=16),
    openai_api_key: str = Form(""),
    mistral_model: str = Form(""),
    mistral_custom_model: str = Form(""),
    mistral_api_key: str = Form(""),
    runpod_endpoint: str = Form("", max_length=64),
    runpod_tracking_id: str = Form("", max_length=64),
) -> Response:
    authenticated_user = _authenticated_user(request)

    if authenticated_user is None:
        return _redirect_to_login()

    _require_valid_csrf_token(request, csrf_token)

    result: FeedbackResult | RubricFeedbackResult | None = None
    feedback_run_id: str | None = None
    error: str | None = None
    storage_warning: str | None = None
    runpod_warm_window: dict[str, object] | None = None
    selected_task_id = task_id.strip()
    default_rubric_analysis_mode = (
        RUBRIC_ANALYSIS_MODE_CRITERION_WISE
        if selected_task_id
        else RUBRIC_ANALYSIS_MODE_JOINT
    )
    selected_rubric_analysis_mode = (
        RUBRIC_ANALYSIS_MODE_TWO_PASS
        if two_pass_feedback
        else default_rubric_analysis_mode
    )

    openai_override_used = (
        settings.browser_overrides_allowed
        and provider == "openai"
        and bool(openai_api_key.strip())
    )
    mistral_override_used = (
        settings.browser_overrides_allowed
        and provider == "mistral"
        and bool(mistral_api_key.strip())
    )

    try:
        selected_rubric_analysis_mode = _rubric_analysis_mode(
            rubric_analysis_mode,
            legacy_two_pass=two_pass_feedback,
            default_mode=default_rubric_analysis_mode,
        )

        if provider == "runpod" and not runpod_tracking_id.strip():
            runpod_tracking_id = str(uuid4())

        provider_override = _provider_for_request(
            provider_key=provider,
            ollama_base_url=ollama_base_url,
            ollama_model=ollama_model,
            ollama_custom_model=ollama_custom_model,
            openai_model=openai_model,
            openai_custom_model=openai_custom_model,
            openai_reasoning_effort=openai_reasoning_effort,
            openai_api_key=openai_api_key,
            mistral_model=mistral_model,
            mistral_custom_model=mistral_custom_model,
            mistral_api_key=mistral_api_key,
            runpod_endpoint=runpod_endpoint,
            runpod_tracking_id=runpod_tracking_id,
        )

        if selected_task_id:
            selected_task = await task_store.get_task(
                selected_task_id
            )

            if selected_task is None:
                raise ValueError(
                    "Die ausgewählte Aufgabe oder ihre Feedback-Vorlage "
                    "ist nicht mehr verfügbar."
                )

            selected_feedback_service = {
                RUBRIC_ANALYSIS_MODE_JOINT: rubric_feedback_service,
                RUBRIC_ANALYSIS_MODE_CRITERION_WISE: (
                    criterion_wise_rubric_feedback_service
                ),
                RUBRIC_ANALYSIS_MODE_TWO_PASS: (
                    two_pass_rubric_feedback_service
                ),
            }[selected_rubric_analysis_mode]
            result = await selected_feedback_service.analyze_text(
                student_text=student_text,
                task=selected_task,
                original_text=original_text,
                provider_key=provider,
                provider_override=provider_override,
            )

            try:
                feedback_run_id = await task_store.save_feedback_run(
                    task=selected_task,
                    student_text=student_text,
                    original_text=original_text,
                    provider=result.provider,
                    model=result.model,
                    duration_ms=result.duration_ms,
                    feedback_payload=result.payload(),
                    provider_request_id=(
                        result.provider_request_id
                    ),
                    queue_duration_ms=(
                        result.queue_duration_ms
                    ),
                    execution_duration_ms=(
                        result.execution_duration_ms
                    ),
                    reasoning_effort=result.reasoning_effort,
                )
            except TaskStoreError:
                storage_warning = (
                    "Das Feedback wurde erfolgreich erzeugt, konnte "
                    "aber nicht dauerhaft gespeichert werden."
                )
        else:
            if (
                selected_rubric_analysis_mode
                != RUBRIC_ANALYSIS_MODE_JOINT
            ):
                raise ValueError(
                    "Das ausgewählte Kriterien-Analyseverfahren benötigt "
                    "eine Aufgabe mit Feedback-Vorlage."
                )

            result = await feedback_service.analyze_text(
                student_text=student_text,
                provider_key=provider,
                provider_override=provider_override,
            )

            try:
                standard_task = (
                    await task_store.get_or_create_standard_feedback_task()
                )
                feedback_run_id = await task_store.save_feedback_run(
                    task=standard_task,
                    student_text=student_text,
                    provider=result.provider,
                    model=result.model,
                    duration_ms=result.duration_ms,
                    feedback_payload=result.payload(),
                    provider_request_id=result.provider_request_id,
                    queue_duration_ms=result.queue_duration_ms,
                    execution_duration_ms=result.execution_duration_ms,
                    reasoning_effort=result.reasoning_effort,
                )
            except TaskStoreError:
                storage_warning = (
                    "Das Feedback wurde erfolgreich erzeugt, konnte "
                    "aber nicht dauerhaft gespeichert werden."
                )

        if result.provider == "runpod":
            endpoint_key = (
                runpod_endpoint.strip().lower()
                or RUNPOD_DEFAULT_ENDPOINT_KEY
            )
            runpod_warm_window = (
                runpod_status_service.mark_success(endpoint_key)
            )
    except Exception as exc:
        provider_error_details = (
            exc.details
            if isinstance(exc, ProviderError)
            else {}
        )
        logger.error(
            "Feedbackanalyse fehlgeschlagen "
            "(provider=%s, task_selected=%s, rubric_mode=%s, "
            "error_type=%s, status_code=%s, operation=%s, "
            "request_id=%s, job_status=%s, "
            "provider_error=%s).",
            provider,
            bool(task_id.strip()),
            selected_rubric_analysis_mode,
            type(exc).__name__,
            (
                exc.status_code
                if isinstance(exc, ProviderError)
                else None
            ),
            provider_error_details.get("operation"),
            provider_error_details.get("request_id"),
            provider_error_details.get("job_status"),
            provider_error_details.get("response_error"),
        )
        error = str(exc).strip() or (
            "Die Feedbackanalyse ist mit einem internen Fehler "
            f"abgebrochen ({type(exc).__name__})."
        )

    try:
        task_options = await task_store.list_tasks()
        default_task_id = (
            await task_store.get_default_feedback_task_id()
        )
    except TaskStoreError:
        task_options = []
        default_task_id = None

        if error is None:
            storage_warning = (
                "Die Feedback-Vorlagen konnten momentan nicht geladen werden."
            )

    template_name = (
        "analysis_response.html"
        if request.headers.get("X-Requested-With")
        == "XMLHttpRequest"
        else "index.html"
    )

    analysis_outcome = (
        "error"
        if error is not None
        else "success"
        if result is not None
        else "empty"
    )

    return templates.TemplateResponse(
        name=template_name,
        request=request,
        context=_template_context(
            csrf_token=get_or_create_csrf_token(
                request.session
            ),
            authenticated_user=authenticated_user,
            selected_provider=provider,
            student_text=student_text,
            task_options=task_options,
            selected_task_id=task_id.strip(),
            default_task_id=default_task_id or "",
            original_text=original_text,
            selected_rubric_analysis_mode=(
                selected_rubric_analysis_mode
            ),
            advanced_options=advanced_options,
            ollama_base_url=ollama_base_url,
            selected_ollama_model=ollama_model,
            ollama_custom_model=ollama_custom_model,
            selected_openai_model=openai_model,
            openai_custom_model=openai_custom_model,
            selected_openai_reasoning_effort=(
                openai_reasoning_effort
            ),
            openai_override_used=openai_override_used,
            selected_mistral_model=mistral_model,
            mistral_custom_model=mistral_custom_model,
            mistral_override_used=mistral_override_used,
            selected_runpod_endpoint=runpod_endpoint,
            result=result,
            feedback_run_id=feedback_run_id,
            runpod_warm_window=runpod_warm_window,
            storage_warning=storage_warning,
            error=error,
        ),
        headers={
            "X-Analysis-Outcome": analysis_outcome,
        },
    )
