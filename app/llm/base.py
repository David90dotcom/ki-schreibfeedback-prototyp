from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LLMResponse:
    provider: str
    model: str
    text: str


class LLMProvider(Protocol):
    provider_name: str
    model_name: str

    async def generate(self, prompt: str) -> LLMResponse:
        ...