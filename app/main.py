import secrets
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import (
    FastAPI,
    Form,
    HTTPException,
    Query,
    Request,
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
from app.feedback_markdown import render_feedback_markdown
from app.llm.base import LLMProvider
from app.llm.ollama_client import OllamaProvider
from app.llm.openai_client import OpenAIProvider
from app.llm.runpod_client import RunPodProvider
from app.security import (
    LoginRateLimiter,
    authenticated_username,
    end_authenticated_session,
    get_or_create_csrf_token,
    is_valid_csrf_token,
    is_authenticated,
    start_authenticated_session,
    verify_credentials,
)
from app.services.feedback_service import (
    FeedbackResult,
    FeedbackService,
)
from app.services.runpod_status_service import RunPodStatusService


BASE_DIR = Path(__file__).resolve().parent
CUSTOM_MODEL_VALUE = "__custom__"
OLLAMA_FALLBACK_BASE_URL = "http://localhost:11434"
MAX_MODEL_NAME_CHARS = 200
SESSION_COOKIE_NAME = "ki-schreibfeedback-session"

OPENAI_MODEL_CATALOG = (
    ("gpt-5.6-luna", "GPT-5.6 Luna – günstig"),
    ("gpt-5.6-terra", "GPT-5.6 Terra – ausgewogen"),
    ("gpt-5.6-sol", "GPT-5.6 Sol – höchste Leistung"),
)

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
    (
        "rtx4090_24gb",
        "RTX 4090 – 24 GB",
        "runpod_endpoint_rtx4090_id",
        "RUNPOD_ENDPOINT_RTX4090_ID",
        ("NVIDIA GeForce RTX 4090",),
    ),
    (
        "rtx5090_32gb",
        "RTX 5090 – 32 GB",
        "runpod_endpoint_rtx5090_id",
        "RUNPOD_ENDPOINT_RTX5090_ID",
        ("NVIDIA GeForce RTX 5090",),
    ),
    (
        "rtx6000ada_48gb",
        "RTX 6000 Ada – 48 GB",
        "runpod_endpoint_rtx6000_ada_id",
        "RUNPOD_ENDPOINT_RTX6000_ADA_ID",
        ("NVIDIA RTX 6000 Ada Generation",),
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

app.add_middleware(
    SessionMiddleware,
    secret_key=_runtime_session_secret(),
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
templates.env.filters["duration_ms"] = _format_duration_ms


feedback_service = FeedbackService(
    providers={
        "ollama": OllamaProvider(
            base_url=settings.ollama_base_url,
            model_name=settings.ollama_model,
        ),
        "openai": OpenAIProvider(
            api_key=settings.openai_api_key,
            model_name=settings.openai_model,
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

runpod_status_service = RunPodStatusService(
    api_key=settings.runpod_api_key,
    idle_timeout_seconds=settings.runpod_idle_timeout_seconds,
)

login_rate_limiter = LoginRateLimiter(
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
    openai_override_used: bool = False,
    selected_runpod_endpoint: str | None = None,
    result: FeedbackResult | None = None,
    runpod_warm_window: dict[str, object] | None = None,
    error: str | None = None,
) -> dict[str, object]:
    provider_options = _provider_options()
    available_provider_keys = {
        provider_key
        for provider_key, _ in provider_options
    }

    if selected_provider not in available_provider_keys:
        selected_provider = (
            "runpod"
            if "runpod" in available_provider_keys
            else provider_options[0][0]
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
        "result": result,
        "error": error,
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
        "openai_env_key_configured": bool(
            settings.openai_api_key
        ),
        "openai_override_used": openai_override_used,
        "runpod_endpoint_options": _runpod_endpoint_options(),
        "selected_runpod_endpoint": selected_runpod_endpoint_key,
        "result_endpoint_label": result_endpoint_label,
        "runpod_warm_window": runpod_warm_window,
        "runpod_idle_timeout_minutes": (
            settings.runpod_idle_timeout_seconds // 60
        ),
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
) -> LLMProvider:
    if provider_key == "ollama":
        if not _ollama_available():
            raise ValueError(
                "Ollama ist im Produktionsbetrieb deaktiviert."
            )

        effective_base_url = (
            ollama_base_url
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
        )

    if provider_key == "runpod":
        endpoint_key = (
            runpod_endpoint.strip().lower()
            or RUNPOD_DEFAULT_ENDPOINT_KEY
        )

        definition = _runpod_endpoint_definition(endpoint_key)

        if definition is None:
            raise ValueError(
                "Die ausgewählte RunPod-Hardwarekonfiguration "
                "ist nicht erlaubt."
            )

        (
            _allowed_key,
            label,
            settings_attribute,
            environment_variable,
            _gpu_type_ids,
        ) = definition
        endpoint_id = getattr(settings, settings_attribute)

        if not endpoint_id:
            raise ValueError(
                f"{label} ist noch nicht konfiguriert. "
                f"Hinterlege {environment_variable} in der "
                ".env-Datei."
            )

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


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
) -> Response:
    authenticated_user = _authenticated_user(request)

    if authenticated_user is None:
        return _redirect_to_login()

    return templates.TemplateResponse(
        name="index.html",
        request=request,
        context=_template_context(
            csrf_token=get_or_create_csrf_token(
                request.session
            ),
            authenticated_user=authenticated_user,
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


@app.post("/analyze", response_class=HTMLResponse)
async def analyze(
    request: Request,
    student_text: str = Form(...),
    provider: str = Form(...),
    csrf_token: str = Form("", max_length=256),
    ollama_base_url: str = Form(""),
    ollama_model: str = Form(""),
    ollama_custom_model: str = Form(""),
    openai_model: str = Form(""),
    openai_custom_model: str = Form(""),
    openai_api_key: str = Form(""),
    runpod_endpoint: str = Form("", max_length=64),
) -> Response:
    authenticated_user = _authenticated_user(request)

    if authenticated_user is None:
        return _redirect_to_login()

    _require_valid_csrf_token(request, csrf_token)

    result: FeedbackResult | None = None
    error: str | None = None
    runpod_warm_window: dict[str, object] | None = None

    openai_override_used = (
        settings.browser_overrides_allowed
        and provider == "openai"
        and bool(openai_api_key.strip())
    )

    try:
        provider_override = _provider_for_request(
            provider_key=provider,
            ollama_base_url=ollama_base_url,
            ollama_model=ollama_model,
            ollama_custom_model=ollama_custom_model,
            openai_model=openai_model,
            openai_custom_model=openai_custom_model,
            openai_api_key=openai_api_key,
            runpod_endpoint=runpod_endpoint,
        )

        result = await feedback_service.analyze_text(
            student_text=student_text,
            provider_key=provider,
            provider_override=provider_override,
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
        error = str(exc)

    template_name = (
        "analysis_response.html"
        if request.headers.get("X-Requested-With")
        == "XMLHttpRequest"
        else "index.html"
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
            ollama_base_url=ollama_base_url,
            selected_ollama_model=ollama_model,
            ollama_custom_model=ollama_custom_model,
            selected_openai_model=openai_model,
            openai_custom_model=openai_custom_model,
            openai_override_used=openai_override_used,
            selected_runpod_endpoint=runpod_endpoint,
            result=result,
            runpod_warm_window=runpod_warm_window,
            error=error,
        ),
    )
