from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.llm.ollama_client import OllamaProvider
from app.llm.openai_client import OpenAIProvider
from app.services.feedback_service import FeedbackResult, FeedbackService


BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title=settings.app_name)

app.mount(
    "/static",
    StaticFiles(directory=BASE_DIR / "static"),
    name="static",
)

templates = Jinja2Templates(directory=BASE_DIR / "templates")


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
    },
    max_input_chars=settings.max_input_chars,
)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        name="index.html",
        request=request,
        context={
            "app_name": settings.app_name,
            "provider_options": feedback_service.get_provider_options(),
            "selected_provider": "ollama",
            "student_text": "",
            "result": None,
            "error": None,
        },
    )


@app.post("/analyze", response_class=HTMLResponse)
async def analyze(
    request: Request,
    student_text: str = Form(...),
    provider: str = Form(...),
) -> HTMLResponse:
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
            "provider_options": feedback_service.get_provider_options(),
            "selected_provider": provider,
            "student_text": student_text,
            "result": result,
            "error": error,
        },
    )