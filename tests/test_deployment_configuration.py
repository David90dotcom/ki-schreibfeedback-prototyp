from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]


class DeploymentConfigurationTests(unittest.TestCase):
    def test_caddy_uses_http1_and_http2_without_http3(self) -> None:
        caddyfile = (PROJECT_ROOT / "Caddyfile").read_text(
            encoding="utf-8"
        )

        self.assertIn("servers :443", caddyfile)
        self.assertIn("protocols h1 h2", caddyfile)
        self.assertNotIn("protocols h1 h2 h3", caddyfile)

    def test_compose_does_not_publish_https_udp_port(self) -> None:
        compose = (PROJECT_ROOT / "compose.yaml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn('"443:443/udp"', compose)


if __name__ == "__main__":
    unittest.main()
