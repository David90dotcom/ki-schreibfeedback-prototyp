from __future__ import annotations

import re
from enum import Enum

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


INTERNAL_ID_PATTERN = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$"
)

IDENTIFIER_PATTERN = INTERNAL_ID_PATTERN


class DomainModel(BaseModel):
    """Gemeinsame Grundlage der unveränderlichen Domänenmodelle."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class ProcessingLocation(str, Enum):
    """Grundsätzlicher Ort der Modellverarbeitung."""

    LOCAL = "local"
    CLOUD = "cloud"


class ModelRole(str, Enum):
    """Vorgesehene fachliche Rolle eines Modells."""

    FEEDBACK = "feedback"
    COMPARISON = "comparison"
    BOTH = "both"


class ReasoningEffort(str, Enum):
    """Providerübergreifend abbildbare Reasoning-Stufen."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class ModelCapabilities(DomainModel):
    """
    Technische Fähigkeiten eines konkreten Modells.

    Ein nicht unterstützter Messwert wird später als nicht verfügbar
    behandelt und nicht als null oder als gemessener Wert ausgegeben.
    """

    streaming: bool = False

    structured_output: bool = False
    native_json_schema: bool = False

    reasoning: bool = False
    reasoning_efforts: frozenset[
        ReasoningEffort
    ] = Field(default_factory=frozenset)

    input_token_usage: bool = False
    output_token_usage: bool = False
    reasoning_token_usage: bool = False
    cached_input_token_usage: bool = False
    cache_write_token_usage: bool = False

    provider_timing: bool = False
    time_to_first_token: bool = False

    cost_tracking: bool = False

    @model_validator(mode="after")
    def validate_dependencies(
        self,
    ) -> ModelCapabilities:
        if (
            self.native_json_schema
            and not self.structured_output
        ):
            raise ValueError(
                (
                    "native_json_schema setzt "
                    "structured_output voraus."
                )
            )

        if (
            self.reasoning_efforts
            and not self.reasoning
        ):
            raise ValueError(
                (
                    "reasoning_efforts dürfen nur bei aktivierter "
                    "Reasoning-Fähigkeit angegeben werden."
                )
            )

        if (
            self.reasoning_token_usage
            and not self.reasoning
        ):
            raise ValueError(
                (
                    "reasoning_token_usage setzt reasoning voraus."
                )
            )

        if (
            self.reasoning_token_usage
            and not self.output_token_usage
        ):
            raise ValueError(
                (
                    "reasoning_token_usage setzt "
                    "output_token_usage voraus."
                )
            )

        if (
            self.cached_input_token_usage
            and not self.input_token_usage
        ):
            raise ValueError(
                (
                    "cached_input_token_usage setzt "
                    "input_token_usage voraus."
                )
            )

        if (
            self.cache_write_token_usage
            and not self.input_token_usage
        ):
            raise ValueError(
                (
                    "cache_write_token_usage setzt "
                    "input_token_usage voraus."
                )
            )

        if (
            self.time_to_first_token
            and not self.streaming
        ):
            raise ValueError(
                (
                    "time_to_first_token setzt Streaming voraus."
                )
            )

        return self


class ProviderCapabilities(DomainModel):
    """Fähigkeiten eines Provider-Adapters."""

    health_check: bool = False
    model_discovery: bool = False

    supported_model_capabilities: frozenset[
        str
    ] = Field(default_factory=frozenset)

    @field_validator(
        "supported_model_capabilities"
    )
    @classmethod
    def validate_capability_names(
        cls,
        capability_names: frozenset[str],
    ) -> frozenset[str]:
        known_capability_names = set(
            ModelCapabilities.model_fields
        )

        unknown_capability_names = (
            set(capability_names)
            - known_capability_names
        )

        if unknown_capability_names:
            formatted_names = ", ".join(
                sorted(unknown_capability_names)
            )

            raise ValueError(
                (
                    "Unbekannte Modell-Capabilities: "
                    f"{formatted_names}"
                )
            )

        return capability_names


class ModelParameters(DomainModel):
    """Providerübergreifend nutzbare Modellparameter."""

    temperature: float | None = Field(
        default=None,
        ge=0,
        le=2,
    )
    max_output_tokens: int | None = Field(
        default=None,
        ge=1,
    )
    reasoning_effort: ReasoningEffort | None = None
    seed: int | None = None


class ProviderDefinition(DomainModel):
    """Katalogeintrag eines Modellanbieters."""

    id: str = Field(
        min_length=1,
        pattern=IDENTIFIER_PATTERN,
    )
    display_name: str = Field(min_length=1)

    processing_location: ProcessingLocation
    enabled: bool = True

    capabilities: ProviderCapabilities = Field(
        default_factory=ProviderCapabilities
    )


class ModelDefinition(DomainModel):
    """Katalogeintrag eines konkreten Modells."""

    id: str = Field(
        min_length=1,
        pattern=IDENTIFIER_PATTERN,
    )
    provider_id: str = Field(
        min_length=1,
        pattern=IDENTIFIER_PATTERN,
    )

    provider_model_name: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    description: str = Field(min_length=1)

    role: ModelRole = ModelRole.FEEDBACK
    enabled: bool = True

    capabilities: ModelCapabilities = Field(
        default_factory=ModelCapabilities
    )
    default_parameters: ModelParameters = Field(
        default_factory=ModelParameters
    )

    @model_validator(mode="after")
    def validate_parameter_capabilities(
        self,
    ) -> ModelDefinition:
        reasoning_effort = (
            self.default_parameters.reasoning_effort
        )

        if reasoning_effort is None:
            return self

        if not self.capabilities.reasoning:
            raise ValueError(
                (
                    "reasoning_effort wurde konfiguriert, obwohl "
                    "das Modell kein Reasoning unterstützt."
                )
            )

        supported_efforts = (
            self.capabilities.reasoning_efforts
        )

        if (
            supported_efforts
            and reasoning_effort not in supported_efforts
        ):
            raise ValueError(
                (
                    "Der konfigurierte reasoning_effort wird von "
                    f"Modell '{self.id}' nicht unterstützt."
                )
            )

        return self
