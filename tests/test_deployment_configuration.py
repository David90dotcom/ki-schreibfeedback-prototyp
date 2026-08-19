from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import (
    OPENAI_DEFAULT_MODEL,
    _configured_student_feedback_provider,
)


PROJECT_ROOT = Path(__file__).parents[1]


class DeploymentConfigurationTests(unittest.TestCase):
    def test_caddy_uses_http1_and_http2_without_http3(self) -> None:
        caddyfile = (PROJECT_ROOT / "Caddyfile").read_text(
            encoding="utf-8"
        )

        self.assertIn("servers :443", caddyfile)
        self.assertIn("protocols h1 h2", caddyfile)
        self.assertNotIn("protocols h1 h2 h3", caddyfile)

    def test_release_configuration_is_consistent_and_disables_http3(
        self,
    ) -> None:
        compose = (PROJECT_ROOT / "compose.yaml").read_text(
            encoding="utf-8"
        )
        main_module = (PROJECT_ROOT / "app" / "main.py").read_text(
            encoding="utf-8"
        )
        readme = (PROJECT_ROOT / "README.md").read_text(
            encoding="utf-8"
        )

        self.assertNotIn('"443:443/udp"', compose)
        self.assertIn(
            "image: ki-schreibfeedback-web:1.0.0",
            compose,
        )
        self.assertIn('APP_VERSION = "1.0.0"', main_module)
        self.assertIn(
            "# KI-Schreibfeedback-Prototyp 1.0.0",
            readme,
        )

    def test_example_environment_uses_safe_student_provider(self) -> None:
        example_environment = (PROJECT_ROOT / ".env.example").read_text(
            encoding="utf-8"
        )

        self.assertEqual(OPENAI_DEFAULT_MODEL, "gpt-5.6-luna")
        self.assertIn(
            "STUDENT_FEEDBACK_PROVIDER=mistral",
            example_environment,
        )
        self.assertNotIn(
            "STUDENT_FEEDBACK_PROVIDER=runpod",
            example_environment,
        )
        self.assertIn(
            "OPENAI_DEFAULT_MODEL=gpt-5.6-luna",
            example_environment,
        )
        self.assertNotIn(
            "OPENAI_DEFAULT_MODEL=gpt-5.6-terra",
            example_environment,
        )
        self.assertIn(
            "OPENAI_EVALUATION_MODEL=gpt-5.6-luna",
            example_environment,
        )
        self.assertNotIn(
            "OPENAI_EVALUATION_MODEL=gpt-5.6-terra",
            example_environment,
        )

    def test_student_portal_rejects_local_and_runpod_providers(self) -> None:
        for provider in ("ollama", "runpod", "unbekannt"):
            with (
                self.subTest(provider=provider),
                patch.dict(
                    os.environ,
                    {"STUDENT_FEEDBACK_PROVIDER": provider},
                ),
                self.assertRaises(ValueError),
            ):
                _configured_student_feedback_provider()

        for provider in ("mistral", "openai"):
            with (
                self.subTest(provider=provider),
                patch.dict(
                    os.environ,
                    {"STUDENT_FEEDBACK_PROVIDER": provider},
                ),
            ):
                self.assertEqual(
                    _configured_student_feedback_provider(),
                    provider,
                )


if __name__ == "__main__":
    unittest.main()
