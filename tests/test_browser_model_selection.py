from __future__ import annotations

import re
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from httpx import Response

from app import main
from app.services.feedback_service import FeedbackResult


def _csrf_token_from(response: Response) -> str:
    match = re.search(
        r'name="csrf_token"\s+value="([^"]+)"',
        response.text,
    )

    if match is None:
        raise AssertionError("CSRF-Token fehlt in der Antwort.")

    return match.group(1)


class BrowserModelSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(main.app)
        login_csrf_token = _csrf_token_from(
            cls.client.get("/login")
        )

        with patch.object(
            main,
            "verify_credentials",
            return_value=True,
        ):
            response = cls.client.post(
                "/login",
                data={
                    "username": main.settings.auth_username,
                    "password": "nur-fuer-den-integrationstest",
                    "csrf_token": login_csrf_token,
                },
                follow_redirects=False,
            )

        if response.status_code != 303:
            raise AssertionError(
                "Die Testanmeldung ist fehlgeschlagen."
            )

        cls.csrf_token = _csrf_token_from(
            cls.client.get("/")
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def test_start_page_contains_defaults_and_custom_fields(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="ollama-base-url"', response.text)
        self.assertIn('id="ollama-model"', response.text)
        self.assertIn('id="openai-model"', response.text)
        self.assertIn('id="runpod-endpoint"', response.text)
        self.assertIn('value="runpod"', response.text)
        self.assertIn('value="standard"', response.text)
        self.assertIn('value="rtx4090_24gb"', response.text)
        self.assertIn('value="rtx5090_32gb"', response.text)
        self.assertIn('value="rtx6000ada_48gb"', response.text)
        self.assertIn(main.settings.ollama_base_url, response.text)
        self.assertIn(main.settings.openai_model, response.text)
        self.assertIn("Cloud: RunPod Serverless", response.text)
        self.assertIn("Andere Modell-ID", response.text)

    def test_production_page_hides_ollama_and_override_fields(self) -> None:
        production_settings = replace(
            main.settings,
            app_mode="production",
        )

        with patch.object(
            main,
            "settings",
            production_settings,
        ):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            'value="ollama"',
            response.text,
        )
        self.assertNotIn(
            'id="ollama-settings"',
            response.text,
        )
        self.assertNotIn(
            'id="ollama-base-url"',
            response.text,
        )
        self.assertNotIn(
            'name="ollama_base_url"',
            response.text,
        )
        self.assertNotIn(
            'id="openai-api-key"',
            response.text,
        )
        self.assertNotIn(
            'name="openai_api_key"',
            response.text,
        )
        self.assertNotIn(
            production_settings.ollama_base_url,
            response.text,
        )
        self.assertIn(
            'id="openai-model"',
            response.text,
        )
        self.assertRegex(
            response.text,
            r'value="runpod"\s+selected',
        )

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
                    "csrf_token": self.csrf_token,
                    "ollama_base_url": "http://127.0.0.1:11500/",
                    "ollama_model": main.CUSTOM_MODEL_VALUE,
                    "ollama_custom_model": "llama4:latest",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            captured["base_url"],
            "http://127.0.0.1:11500",
        )
        self.assertEqual(captured["model"], "llama4:latest")
        self.assertIn("Lokales Testfeedback", response.text)
        self.assertIn("llama4:latest", response.text)
        self.assertIn("123 ms", response.text)

    def test_production_rejects_ollama_analysis(self) -> None:
        production_settings = replace(
            main.settings,
            app_mode="production",
            ollama_base_url="http://127.0.0.1:11434",
        )

        with (
            patch.object(
                main,
                "settings",
                production_settings,
            ),
            patch.object(
                main,
                "OllamaProvider",
            ) as provider_class,
            patch.object(
                main.feedback_service,
                "analyze_text",
                new=AsyncMock(),
            ) as analyze_mock,
        ):
            response = self.client.post(
                "/analyze",
                data={
                    "student_text": "Ein kurzer Beispieltext.",
                    "provider": "ollama",
                    "csrf_token": self.csrf_token,
                    "ollama_base_url": "https://manipulated.example",
                    "ollama_model": production_settings.ollama_model,
                },
            )

        self.assertEqual(response.status_code, 200)
        provider_class.assert_not_called()
        analyze_mock.assert_not_awaited()
        self.assertIn(
            "Ollama ist im Produktionsbetrieb deaktiviert.",
            response.text,
        )
        self.assertNotIn(
            "manipulated.example",
            response.text,
        )

    def test_selected_openai_model_is_used(self) -> None:
        captured: dict[str, str] = {}

        async def fake_analyze_text(**kwargs: object) -> FeedbackResult:
            provider = kwargs["provider_override"]

            if not isinstance(provider, main.OpenAIProvider):
                raise AssertionError(
                    "Es wurde kein OpenAIProvider übergeben."
                )

            captured["provider_key"] = str(kwargs["provider_key"])
            captured["provider_name"] = provider.provider_name
            captured["model"] = provider.model_name

            return FeedbackResult(
                provider="openai",
                model=provider.model_name,
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
                    "csrf_token": self.csrf_token,
                    "openai_model": "gpt-5.6-terra",
                    "openai_api_key": "test-api-key",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["provider_key"], "openai")
        self.assertEqual(captured["provider_name"], "openai")
        self.assertEqual(captured["model"], "gpt-5.6-terra")
        self.assertIn("Cloud-Testfeedback", response.text)
        self.assertIn("gpt-5.6-terra", response.text)

    def test_production_ignores_browser_openai_api_key(self) -> None:
        production_settings = replace(
            main.settings,
            app_mode="production",
            openai_api_key="configured-production-key",
        )

        async def fake_analyze_text(**kwargs: object) -> FeedbackResult:
            return FeedbackResult(
                provider="openai",
                model="gpt-5.6-terra",
                feedback="Produktions-Cloudfeedback",
                duration_ms=52,
            )

        with (
            patch.object(
                main,
                "settings",
                production_settings,
            ),
            patch.object(
                main,
                "OpenAIProvider",
            ) as provider_class,
            patch.object(
                main.feedback_service,
                "analyze_text",
                new=AsyncMock(side_effect=fake_analyze_text),
            ),
        ):
            response = self.client.post(
                "/analyze",
                data={
                    "student_text": "Ein kurzer Beispieltext.",
                    "provider": "openai",
                    "csrf_token": self.csrf_token,
                    "openai_model": "gpt-5.6-terra",
                    "openai_api_key": "browser-manipulated-key",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider_class.call_count, 1)
        self.assertEqual(
            provider_class.call_args.kwargs["api_key"],
            "configured-production-key",
        )
        self.assertNotIn(
            "browser-manipulated-key",
            response.text,
        )
        self.assertNotIn(
            "configured-production-key",
            response.text,
        )

    def test_selected_runpod_provider_is_used(self) -> None:
        captured: dict[str, str] = {}
        standard_endpoint_id = "standard-endpoint-test-only"
        runpod_settings = replace(
            main.settings,
            runpod_endpoint_id=standard_endpoint_id,
        )

        async def fake_analyze_text(**kwargs: object) -> FeedbackResult:
            provider = kwargs["provider_override"]

            if not isinstance(provider, main.RunPodProvider):
                raise AssertionError(
                    "Es wurde kein RunPodProvider übergeben."
                )

            captured["provider_key"] = str(kwargs["provider_key"])
            captured["provider_id"] = provider.provider_id
            captured["model"] = provider.model_name
            captured["endpoint_id"] = provider.endpoint_id

            return FeedbackResult(
                provider="runpod",
                model=provider.model_name,
                feedback="RunPod-Testfeedback",
                duration_ms=67,
            )

        with (
            patch.object(
                main,
                "settings",
                runpod_settings,
            ),
            patch.object(
                main.feedback_service,
                "analyze_text",
                new=AsyncMock(side_effect=fake_analyze_text),
            ),
        ):
            response = self.client.post(
                "/analyze",
                data={
                    "student_text": "Ein kurzer Beispieltext.",
                    "provider": "runpod",
                    "csrf_token": self.csrf_token,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["provider_key"], "runpod")
        self.assertEqual(captured["provider_id"], "runpod")
        self.assertEqual(
            captured["endpoint_id"],
            standard_endpoint_id,
        )
        self.assertEqual(
            captured["model"],
            main.settings.runpod_model,
        )
        self.assertIn("RunPod-Testfeedback", response.text)
        self.assertIn(main.settings.runpod_model, response.text)
        self.assertIn("67 ms", response.text)
        self.assertNotIn(standard_endpoint_id, response.text)

    def test_fixed_runpod_endpoints_are_mapped_server_side(self) -> None:
        endpoint_mapping = {
            "rtx4090_24gb": "endpoint-4090-test-only",
            "rtx5090_32gb": "endpoint-5090-test-only",
            "rtx6000ada_48gb": "endpoint-6000ada-test-only",
        }
        runpod_settings = replace(
            main.settings,
            runpod_endpoint_rtx4090_id=(
                endpoint_mapping["rtx4090_24gb"]
            ),
            runpod_endpoint_rtx5090_id=(
                endpoint_mapping["rtx5090_32gb"]
            ),
            runpod_endpoint_rtx6000_ada_id=(
                endpoint_mapping["rtx6000ada_48gb"]
            ),
        )

        for endpoint_key, expected_endpoint_id in (
            endpoint_mapping.items()
        ):
            with self.subTest(endpoint_key=endpoint_key):
                captured: dict[str, str | None] = {}

                async def fake_analyze_text(
                    **kwargs: object,
                ) -> FeedbackResult:
                    provider = kwargs["provider_override"]
                    captured["endpoint_id"] = (
                        provider.endpoint_id  # type: ignore[attr-defined]
                    )

                    return FeedbackResult(
                        provider="runpod",
                        model=runpod_settings.runpod_model,
                        feedback="Zuordnungstest",
                        duration_ms=1,
                    )

                with (
                    patch.object(
                        main,
                        "settings",
                        runpod_settings,
                    ),
                    patch.object(
                        main.feedback_service,
                        "analyze_text",
                        new=AsyncMock(
                            side_effect=fake_analyze_text
                        ),
                    ),
                ):
                    response = self.client.post(
                        "/analyze",
                        data={
                            "student_text": "Zuordnungstest.",
                            "provider": "runpod",
                            "runpod_endpoint": endpoint_key,
                            "csrf_token": self.csrf_token,
                        },
                    )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    captured["endpoint_id"],
                    expected_endpoint_id,
                )

                for endpoint_id in endpoint_mapping.values():
                    self.assertNotIn(endpoint_id, response.text)

    def test_unknown_runpod_endpoint_is_rejected(self) -> None:
        with patch.object(
            main.feedback_service,
            "analyze_text",
            new=AsyncMock(),
        ) as analyze_mock:
            response = self.client.post(
                "/analyze",
                data={
                    "student_text": "Ein kurzer Beispieltext.",
                    "provider": "runpod",
                    "runpod_endpoint": "attacker-endpoint-id",
                    "csrf_token": self.csrf_token,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "RunPod-Hardwarekonfiguration ist nicht erlaubt",
            response.text,
        )
        self.assertNotIn("attacker-endpoint-id", response.text)
        analyze_mock.assert_not_awaited()

    def test_invalid_ollama_url_returns_understandable_error(self) -> None:
        response = self.client.post(
            "/analyze",
            data={
                "student_text": "Ein kurzer Beispieltext.",
                "provider": "ollama",
                "csrf_token": self.csrf_token,
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
                "csrf_token": self.csrf_token,
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

    def test_production_rejects_ollama_discovery(self) -> None:
        production_settings = replace(
            main.settings,
            app_mode="production",
            ollama_base_url="http://127.0.0.1:11434",
        )

        with (
            patch.object(
                main,
                "settings",
                production_settings,
            ),
            patch.object(
                main,
                "OllamaProvider",
            ) as provider_class,
        ):
            response = self.client.get(
                "/api/ollama/models",
                params={
                    "base_url": "https://manipulated.example",
                },
            )

        self.assertEqual(response.status_code, 403)
        provider_class.assert_not_called()
        self.assertEqual(
            response.json()["detail"]["message"],
            "Ollama ist im Produktionsbetrieb deaktiviert.",
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
