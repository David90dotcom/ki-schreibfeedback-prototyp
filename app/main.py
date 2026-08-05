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

from app.config import APP_MODE_LOCAL, settings
from app.llm.base import LLMProvider
from app.llm.ollama_client import OllamaProvider
from app.llm.openai_client import OpenAIProvider
from app.llm.runpod_client import RunPodProvider
from app.security import (
    authenticated_username,
    end_authenticated_session,
    is_authenticated,
    start_authenticated_session,
    verify_credentials,
)
from app.services.feedback_service import (
    FeedbackResult,
    FeedbackService,
)


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


def _template_context(
    *,
    authenticated_user: str | None = None,
    selected_provider: str = "ollama",
    student_text: str = "",
    ollama_base_url: str | None = None,
    selected_ollama_model: str | None = None,
    ollama_custom_model: str = "",
    selected_openai_model: str | None = None,
    openai_custom_model: str = "",
    openai_override_used: bool = False,
    result: FeedbackResult | None = None,
    error: str | None = None,
) -> dict[str, object]:
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

    return {
        "app_name": settings.app_name,
        "authenticated_user": authenticated_user,
        "provider_options": (
            feedback_service.get_provider_options()
        ),
        "selected_provider": selected_provider,
        "student_text": student_text,
        "result": result,
        "error": error,
        "custom_model_value": CUSTOM_MODEL_VALUE,
        "ollama_default_base_url": (
            settings.ollama_base_url
        ),
        "ollama_fallback_base_url": (
            OLLAMA_FALLBACK_BASE_URL
        ),
        "ollama_base_url": (
            ollama_base_url
            or settings.ollama_base_url
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
) -> LLMProvider:
    if provider_key == "ollama":
        return OllamaProvider(
            base_url=_validate_ollama_base_url(
                ollama_base_url
            ),
            model_name=_validate_model_name(
                ollama_model,
                ollama_custom_model,
                settings.ollama_model,
            ),
        )

    if provider_key == "openai":
        api_key = (
            openai_api_key.strip()
            or settings.openai_api_key
        )

        if not api_key:
            raise ValueError(
                "Kein OpenAI-API-Key verfügbar. "
                "Hinterlege OPENAI_API_KEY in der "
                ".env-Datei oder gib im optionalen "
                "Key-Feld einen Key für diesen Aufruf ein."
            )

        return OpenAIProvider(
            api_key=api_key,
            model_name=_validate_model_name(
                openai_model,
                openai_custom_model,
                settings.openai_model,
            ),
        )

    if provider_key == "runpod":
        return RunPodProvider(
            api_key=settings.runpod_api_key,
            endpoint_id=settings.runpod_endpoint_id,
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
            "error": None,
        },
    )


@app.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    username: str = Form(..., max_length=200),
    password: str = Form(..., max_length=512),
) -> Response:
    if verify_credentials(
        username=username.strip(),
        password=password,
        expected_username=settings.auth_username,
        password_hash=settings.auth_password_hash,
    ):
        start_authenticated_session(
            request.session,
            settings.auth_username,
        )

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
            "error": "Benutzername oder Passwort ist falsch.",
        },
        status_code=401,
    )


@app.post("/logout")
async def logout(
    request: Request,
) -> RedirectResponse:
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

    try:
        validated_base_url = (
            _validate_ollama_base_url(base_url)
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
        raise HTTPException(
            status_code=502,
            detail={
                "message": (
                    f"Ollama ist unter {validated_base_url} "
                    "nicht erreichbar. Prüfe, ob Ollama "
                    "läuft und die Adresse stimmt."
                )
            },
        ) from exc

    return {
        "base_url": validated_base_url,
        "models": models,
        "default_model": settings.ollama_model,
        "message": (
            f"{len(models)} installierte "
            "Ollama-Modelle geladen."
        ),
    }


@app.post("/analyze", response_class=HTMLResponse)
async def analyze(
    request: Request,
    student_text: str = Form(...),
    provider: str = Form(...),
    ollama_base_url: str = Form(""),
    ollama_model: str = Form(""),
    ollama_custom_model: str = Form(""),
    openai_model: str = Form(""),
    openai_custom_model: str = Form(""),
    openai_api_key: str = Form(""),
) -> Response:
    authenticated_user = _authenticated_user(request)

    if authenticated_user is None:
        return _redirect_to_login()

    result: FeedbackResult | None = None
    error: str | None = None

    openai_override_used = (
        provider == "openai"
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
        )

        result = await feedback_service.analyze_text(
            student_text=student_text,
            provider_key=provider,
            provider_override=provider_override,
        )
    except Exception as exc:
        error = str(exc)

    return templates.TemplateResponse(
        name="index.html",
        request=request,
        context=_template_context(
            authenticated_user=authenticated_user,
            selected_provider=provider,
            student_text=student_text,
            ollama_base_url=ollama_base_url,
            selected_ollama_model=ollama_model,
            ollama_custom_model=ollama_custom_model,
            selected_openai_model=openai_model,
            openai_custom_model=openai_custom_model,
            openai_override_used=openai_override_used,
            result=result,
            error=error,
        ),
    )
