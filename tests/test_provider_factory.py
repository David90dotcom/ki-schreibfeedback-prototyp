from __future__ import annotations

import unittest

from app.config import Settings
from app.llm.provider_factory import create_default_provider_factory


class ProviderFactoryConfigurationTests(unittest.TestCase):
    def test_factory_uses_single_runpod_endpoint(self) -> None:
        factory = create_default_provider_factory(
            Settings(
                runpod_api_key="test-runpod-key",
                runpod_endpoint_id="test-standard-endpoint",
            )
        )

        availability = factory.get_availability("runpod")
        provider = factory.get("runpod")

        self.assertTrue(availability.configured)
        self.assertEqual(
            provider.endpoint_id,
            "test-standard-endpoint",
        )


if __name__ == "__main__":
    unittest.main()
