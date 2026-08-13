from dataclasses import dataclass
from time import perf_counter

from app.llm.base import LLMProvider


STANDARD_FEEDBACK_MODE = "standard_without_feedback_template"
STANDARD_FEEDBACK_PROMPT_VERSION = "standard-feedback-v3"
STANDARD_FEEDBACK_STUDENT_TEXT_PLACEHOLDER = "{{STUDENT_TEXT}}"
STANDARD_FEEDBACK_CLOUD_SYSTEM_PROMPT = (
    "Du bist ein hilfreiches Assistenzsystem für lernförderliches "
    "Schreibfeedback im Deutschunterricht."
)
STANDARD_FEEDBACK_PROMPT_TEMPLATE = f"""
Du analysierst einen abgetippten, anonymisierten Schülertext im Fach Deutsch.

Ziel:
Gib lernförderliches Schreibfeedback. Das Feedback soll verständlich, konkret,
wertschätzend und überarbeitungsorientiert sein.

Formuliere so, dass Schülerinnen und Schüler das Feedback leicht verstehen.
Verwende klare, nicht unnötig schwierige Sprache und erkläre unvermeidbare
Fachbegriffe kurz.

Bleibe nicht bei pauschalen Aussagen, sondern gib konkrete Hinweise, wie der
Text verbessert werden kann.

Strukturiere deine Antwort mit folgenden Überschriften:

1. Gesamteindruck
2. Stärken des Textes
3. Verbesserungsmöglichkeiten
4. Konkrete Überarbeitungshinweise
5. Kurzes motivierendes Fazit

Für Abschnitt 4 gilt: Verwende keine Tabellen und keine senkrechten Striche als
Spaltentrenner. Formatiere die Vorschläge als übersichtliche nummerierte Liste.
Nutze für jeden Vorschlag genau dieses Schema und trenne die drei Angaben jeweils
durch eine Leerzeile:

1. **Original:** kurzer Ausschnitt aus dem Schülertext

   **Mögliche Überarbeitung:** konkreter Verbesserungsvorschlag

   **Begründung:** kurze, verständliche Erklärung

Wähle die wichtigsten Vorschläge aus, statt den gesamten Schülertext neu zu
formulieren.

Schülertext:
\"\"\"
{STANDARD_FEEDBACK_STUDENT_TEXT_PLACEHOLDER}
\"\"\"
""".strip()


@dataclass(frozen=True)
class FeedbackResult:
    provider: str
    model: str
    feedback: str
    duration_ms: int
    queue_duration_ms: float | None = None
    execution_duration_ms: float | None = None
    provider_request_id: str | None = None
    worker_id: str | None = None
    reasoning_effort: str | None = None
    prompt_version: str = STANDARD_FEEDBACK_PROMPT_VERSION
    prompt_template: str = STANDARD_FEEDBACK_PROMPT_TEMPLATE

    def payload(self) -> dict[str, object]:
        """Erzeugt den speicherbaren Snapshot des freien Feedbacks."""

        system_prompt = (
            STANDARD_FEEDBACK_CLOUD_SYSTEM_PROMPT
            if self.provider in {"openai", "mistral"}
            else None
        )

        return {
            "criteria": [],
            "overall_feedback": self.feedback,
            "generation_context": {
                "mode": STANDARD_FEEDBACK_MODE,
                "label": "Kontextarmes Standardfeedback",
                "prompt_version": self.prompt_version,
                "system_prompt": system_prompt,
                "prompt_template": self.prompt_template,
            },
        }


class FeedbackService:
    def __init__(
        self,
        providers: dict[str, LLMProvider],
        max_input_chars: int,
    ) -> None:
        self.providers = providers
        self.max_input_chars = max_input_chars

    def get_provider_options(self) -> list[tuple[str, str]]:
        return [
            ("ollama", "Lokal: Ollama"),
            ("openai", "Cloud: OpenAI API"),
            ("mistral", "Cloud: Mistral API"),
            ("runpod", "Cloud: RunPod Serverless"),
        ]

    async def analyze_text(
        self,
        student_text: str,
        provider_key: str,
        provider_override: LLMProvider | None = None,
    ) -> FeedbackResult:
        cleaned_text = student_text.strip()

        if not cleaned_text:
            raise ValueError("Bitte gib einen Text ein.")

        if len(cleaned_text) > self.max_input_chars:
            raise ValueError(
                "Der Text ist zu lang. Erlaubt sind maximal "
                f"{self.max_input_chars} Zeichen."
            )

        provider = (
            provider_override
            or self.providers.get(provider_key)
        )

        if provider is None:
            raise ValueError(
                "Der ausgewählte Modellanbieter ist nicht bekannt."
            )

        prompt = self._build_feedback_prompt(cleaned_text)

        start_time = perf_counter()
        response = await provider.generate(prompt)
        duration_ms = int(
            (perf_counter() - start_time) * 1000
        )

        return FeedbackResult(
            provider=response.provider,
            model=response.model,
            feedback=response.text,
            duration_ms=duration_ms,
            queue_duration_ms=response.queue_duration_ms,
            execution_duration_ms=response.execution_duration_ms,
            provider_request_id=response.provider_request_id,
            worker_id=response.worker_id,
            reasoning_effort=self._reasoning_effort(
                response.raw_metadata
            ),
        )

    @staticmethod
    def _reasoning_effort(
        raw_metadata: dict[str, object],
    ) -> str | None:
        value = raw_metadata.get("reasoning_effort")

        if not isinstance(value, str) or not value.strip():
            return None

        return value.strip().lower()

    def _build_feedback_prompt(
        self,
        student_text: str,
    ) -> str:
        return STANDARD_FEEDBACK_PROMPT_TEMPLATE.replace(
            STANDARD_FEEDBACK_STUDENT_TEXT_PLACEHOLDER,
            student_text,
        )
