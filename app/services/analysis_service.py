from __future__ import annotations

from pydantic import ValidationError

from app.domain.analysis import (
    AnalysisInput,
    AnalysisPayload,
)
from app.domain.model_catalog import ModelParameters
from app.llm.base import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
)
from app.llm.errors import ProviderStructuredOutputError
from app.services.metrics_service import (
    MetricsService,
    ModelExecutionResult,
)


ANALYSIS_INSTRUCTIONS = """
Du analysierst einen anonymisierten Schülertext im Fach Deutsch.

Arbeite ausschließlich mit der übermittelten Aufgabe, dem Schülertext
und den angegebenen Feedbackkriterien.

Für jedes Feedbackkriterium musst du genau ein Ergebnis erzeugen.

Beachte dabei:

1. Unterscheide klar zwischen erfüllten, teilweise erfüllten und nicht
   erfüllten Kriterien.
2. Benenne konkrete Stärken.
3. Benenne konkreten Überarbeitungsbedarf.
4. Formuliere handlungsorientierte Überarbeitungshinweise.
5. Belege Aussagen möglichst mit wörtlichen Textstellen aus dem
   Schülertext.
6. Erfinde keine Textbelege.
7. Formuliere verständlich, wertschätzend und lernförderlich.
8. Schreibe dem Schüler keine fertige Musterlösung.
9. Unterstütze eine eigenständige Überarbeitung.
10. Gib ausschließlich das verlangte strukturierte Datenformat zurück.

Jede criterion_id der Eingabe muss genau einmal in criteria_results
vorkommen. Füge keine eigenen Kriterien hinzu.
""".strip()


class AnalysisService:
    """Führt kriterienspezifische und gemessene Textanalysen aus."""

    def __init__(
        self,
        *,
        metrics_service: MetricsService,
        prompt_version: str,
        schema_version: str,
    ) -> None:
        self.metrics_service = metrics_service
        self.prompt_version = prompt_version
        self.schema_version = schema_version

    async def analyze(
        self,
        *,
        analysis_input: AnalysisInput,
        provider: ModelProvider,
        model_id: str,
        provider_model_name: str,
        parameters: ModelParameters,
        stream: bool = False,
    ) -> ModelExecutionResult[AnalysisPayload]:
        """
        Führt eine Analyse aus und liefert Ergebnis sowie Laufprotokoll.

        model_id ist die interne stabile ID aus config/models.yaml.
        provider_model_name ist der tatsächliche Modellname des Anbieters.
        """

        model_request = ModelRequest(
            model_name=provider_model_name,
            instructions=ANALYSIS_INSTRUCTIONS,
            input_text=self._build_model_input(analysis_input),
            parameters=parameters,
            response_schema=AnalysisPayload.model_json_schema(),
            response_schema_name="analysis_payload",
            stream=stream,
            metadata={
                "model_id": model_id,
                "prompt_version": self.prompt_version,
                "schema_version": self.schema_version,
            },
        )

        return await self.metrics_service.execute(
            provider=provider,
            request=model_request,
            model_id=model_id,
            prompt_version=self.prompt_version,
            schema_version=self.schema_version,
            validator=lambda response: self._validate_response(
                response=response,
                analysis_input=analysis_input,
            ),
        )

    def _build_model_input(
        self,
        analysis_input: AnalysisInput,
    ) -> str:
        serialized_input = analysis_input.model_dump_json(
            indent=2,
        )

        return (
            "Analysiere den folgenden Auftrag und den darin enthaltenen "
            "Schülertext anhand der angegebenen Kriterien.\n\n"
            f"{serialized_input}"
        )

    def _validate_response(
        self,
        *,
        response: ModelResponse,
        analysis_input: AnalysisInput,
    ) -> AnalysisPayload:
        json_text = self._remove_optional_code_fence(
            response.text
        )

        try:
            payload = AnalysisPayload.model_validate_json(
                json_text
            )
        except ValidationError as exc:
            raise ProviderStructuredOutputError(
                (
                    "Die Modellantwort entspricht nicht dem "
                    "vorgegebenen Analyseformat."
                ),
                provider_id=response.provider_id,
                model_name=response.actual_model_name,
                details={
                    "provider_request_id": (
                        response.provider_request_id
                    ),
                    "validation_errors": exc.errors(
                        include_input=False,
                        include_url=False,
                    ),
                },
            ) from exc

        self._validate_criterion_coverage(
            analysis_input=analysis_input,
            payload=payload,
            response=response,
        )

        return payload

    def _validate_criterion_coverage(
        self,
        *,
        analysis_input: AnalysisInput,
        payload: AnalysisPayload,
        response: ModelResponse,
    ) -> None:
        expected_ids = self._get_expected_criterion_ids(
            analysis_input
        )
        returned_ids = self._get_returned_criterion_ids(
            payload
        )

        duplicate_ids = sorted(
            criterion_id
            for criterion_id in set(returned_ids)
            if returned_ids.count(criterion_id) > 1
        )

        missing_ids = sorted(
            set(expected_ids) - set(returned_ids)
        )
        unexpected_ids = sorted(
            set(returned_ids) - set(expected_ids)
        )

        if duplicate_ids or missing_ids or unexpected_ids:
            raise ProviderStructuredOutputError(
                (
                    "Die Modellantwort enthält keine eindeutige "
                    "Rückmeldung für jedes angeforderte Kriterium."
                ),
                provider_id=response.provider_id,
                model_name=response.actual_model_name,
                details={
                    "provider_request_id": (
                        response.provider_request_id
                    ),
                    "duplicate_criterion_ids": duplicate_ids,
                    "missing_criterion_ids": missing_ids,
                    "unexpected_criterion_ids": unexpected_ids,
                },
            )

    @staticmethod
    def _get_expected_criterion_ids(
        analysis_input: AnalysisInput,
    ) -> list[str]:
        criterion_ids: list[str] = []

        for criterion in analysis_input.criteria:
            criterion_id = getattr(
                criterion,
                "criterion_id",
                None,
            )

            if criterion_id is None:
                criterion_id = getattr(
                    criterion,
                    "id",
                    None,
                )

            if not isinstance(criterion_id, str):
                raise ValueError(
                    "Ein Feedbackkriterium besitzt keine gültige ID."
                )

            criterion_ids.append(criterion_id)

        return criterion_ids

    @staticmethod
    def _get_returned_criterion_ids(
        payload: AnalysisPayload,
    ) -> list[str]:
        criterion_ids: list[str] = []

        for result in payload.criteria_results:
            criterion_id = getattr(
                result,
                "criterion_id",
                None,
            )

            if not isinstance(criterion_id, str):
                raise ValueError(
                    "Ein Analyseergebnis besitzt keine gültige "
                    "criterion_id."
                )

            criterion_ids.append(criterion_id)

        return criterion_ids

    @staticmethod
    def _remove_optional_code_fence(
        text: str,
    ) -> str:
        cleaned_text = text.strip()

        if not cleaned_text.startswith("```"):
            return cleaned_text

        lines = cleaned_text.splitlines()

        if len(lines) < 3:
            return cleaned_text

        if not lines[-1].strip().startswith("```"):
            return cleaned_text

        return "\n".join(lines[1:-1]).strip()