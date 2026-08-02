from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from app import main
from app.services.feedback_service import FeedbackResult


class BrowserModelSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(main.app)

    def test_start_page_contains_defaults_and_custom_fields(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="ollama-base-url"', response.text)
        self.assertIn('id="ollama-model"', response.text)
        self.assertIn('id="openai-model"', response.text)
        self.assertIn(main.settings.ollama_base_url, response.text)
        self.assertIn(main.settings.openai_model, response.text)
        self.assertIn("Andere Modell-ID", response.text)

    def test_custom_ollama_settings_apply_only_to_request(self) -> None:
        captured: dict[str, str] = {}

        async def fake_analyze_text(**kwargs: object) -> FeedbackResult:
            provider = kwargs["provider_override"]
            captured["base_url"] = provider.base_url  # type: ignore[attr-defined]
            captured["model"] = provider.model_name  # type: ignore[attr-defined]

            return FeedbackResult(
                provider="ollama",
                model=provider.model_name,  # type: ignore[attr-defined]
                feedback="Lokales Testfeedback",
                duration_ms=123,
            )

        with patch.object(
            main.feedback_service,
            "analyze_text",
            new=AsyncMock(side_effect=fake_analyze_text),
        ):
            response = self.client.post(
                "/analyze",
                data={
                    "student_text": "Ein kurzer Beispieltext.",
                    "provider": "ollama",
                    "ollama_base_url": "http://127.0.0.1:11500/",
                    "ollama_model": main.CUSTOM_MODEL_VALUE,
                    "ollama_custom_model": "llama4:latest",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["base_url"], "http://127.0.0.1:11500")
        self.assertEqual(captured["model"], "llama4:latest")
        self.assertIn("Lokales Testfeedback", response.text)
        self.assertIn("llama4:latest", response.text)
        self.assertIn("123 ms", response.text)

    def test_selected_openai_model_is_used(self) -> None:
        captured: dict[str, str] = {}

        async def fake_analyze_text(**kwargs: object) -> FeedbackResult:
            provider = kwargs["provider_override"]
            captured["model"] = provider.model_name  # type: ignore[attr-defined]

            return FeedbackResult(
                provider="openai",
                model=provider.model_name,  # type: ignore[attr-defined]
                feedback="Cloud-Testfeedback",
                duration_ms=45,
            )

        with patch.object(
            main.feedback_service,
            "analyze_text",
            new=AsyncMock(side_effect=fake_analyze_text),
        ):
            response = self.client.post(
                "/analyze",
                data={
                    "student_text": "Ein kurzer Beispieltext.",
                    "provider": "openai",
                    "openai_model": "gpt-5.6-terra",
                    "openai_api_key": "test-api-key",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["model"], "gpt-5.6-terra")
        self.assertIn("Cloud-Testfeedback", response.text)
        self.assertIn("gpt-5.6-terra", response.text)

    def test_invalid_ollama_url_returns_understandable_error(self) -> None:
        response = self.client.post(
            "/analyze",
            data={
                "student_text": "Ein kurzer Beispieltext.",
                "provider": "ollama",
                "ollama_base_url": "localhost:11434",
                "ollama_model": main.settings.ollama_model,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "muss eine vollständige HTTP-Adresse sein",
            response.text,
        )

    def test_empty_custom_model_returns_understandable_error(self) -> None:
        response = self.client.post(
            "/analyze",
            data={
                "student_text": "Ein kurzer Beispieltext.",
                "provider": "openai",
                "openai_model": main.CUSTOM_MODEL_VALUE,
                "openai_custom_model": "",
                "openai_api_key": "test-api-key",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Bitte gib die gewünschte Modell-ID "
            "in das Freitextfeld ein.",
            response.text,
        )

    def test_ollama_model_discovery_returns_models(self) -> None:
        discovered_models = [
            "gemma3:12b",
            "qwen3:30b",
        ]

        with patch.object(
            main.OllamaProvider,
            "discover_models",
            new=AsyncMock(return_value=discovered_models),
        ):
            response = self.client.get(
                "/api/ollama/models",
                params={
                    "base_url": "http://localhost:11434/",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            payload["models"],
            discovered_models,
        )
        self.assertEqual(
            payload["base_url"],
            "http://localhost:11434",
        )

    def test_ollama_discovery_connection_error_is_understandable(self) -> None:
        error = httpx.ConnectError(
            "Ollama ist nicht erreichbar."
        )

        with patch.object(
            main.OllamaProvider,
            "discover_models",
            new=AsyncMock(side_effect=error),
        ):
            response = self.client.get(
                "/api/ollama/models",
                params={
                    "base_url": "http://localhost:11434",
                },
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn(
            "nicht erreichbar",
            response.json()["detail"]["message"],
        )


if __name__ == "__main__":
    unittest.main()