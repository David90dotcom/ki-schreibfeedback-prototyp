from dataclasses import dataclass
from time import perf_counter

from app.llm.base import LLMProvider


@dataclass(frozen=True)
class FeedbackResult:
    provider: str
    model: str
    feedback: str
    duration_ms: int


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
        )

    def _build_feedback_prompt(
        self,
        student_text: str,
    ) -> str:
        return f"""
Du analysierst einen abgetippten, anonymisierten Schülertext im Fach Deutsch.

Ziel:
Gib lernförderliches Schreibfeedback. Das Feedback soll verständlich, konkret,
wertschätzend und überarbeitungsorientiert sein.

Bitte bewerte nicht nur pauschal, sondern gib konkrete Hinweise, wie der Text
verbessert werden kann.

Strukturiere deine Antwort mit folgenden Überschriften:

1. Gesamteindruck
2. Stärken des Textes
3. Verbesserungsmöglichkeiten
4. Konkrete Überarbeitungshinweise
5. Kurzes motivierendes Fazit

Schülertext:
\"\"\"
{student_text}
\"\"\"
""".strip()