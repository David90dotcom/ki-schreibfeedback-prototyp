from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main


class WebAuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        self.client.close()

    def _login(self) -> object:
        with patch.object(
            main,
            "verify_credentials",
            return_value=True,
        ):
            return self.client.post(
                "/login",
                data={
                    "username": main.settings.auth_username,
                    "password": "nur-fuer-den-integrationstest",
                },
                follow_redirects=False,
            )

    def test_login_page_is_public(self) -> None:
        response = self.client.get("/login")

        self.assertEqual(response.status_code, 200)
        self.assertIn('name="username"', response.text)
        self.assertIn('name="password"', response.text)

    def test_start_page_redirects_to_login(self) -> None:
        response = self.client.get(
            "/",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_analyze_redirects_to_login(self) -> None:
        response = self.client.post(
            "/analyze",
            data={
                "student_text": "Testtext",
                "provider": "ollama",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_api_rejects_unauthenticated_request(self) -> None:
        response = self.client.get("/api/ollama/models")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["detail"]["message"],
            "Anmeldung erforderlich.",
        )

    def test_wrong_credentials_are_rejected(self) -> None:
        with patch.object(
            main,
            "verify_credentials",
            return_value=False,
        ):
            response = self.client.post(
                "/login",
                data={
                    "username": "falsch",
                    "password": "falsch",
                },
            )

        self.assertEqual(response.status_code, 401)
        self.assertIn(
            "Benutzername oder Passwort ist falsch.",
            response.text,
        )
        self.assertNotIn('value="falsch"', response.text)

    def test_login_and_logout_control_the_session(self) -> None:
        login_response = self._login()

        self.assertEqual(login_response.status_code, 303)
        self.assertEqual(login_response.headers["location"], "/")

        session_cookie = login_response.headers["set-cookie"].lower()

        self.assertIn("httponly", session_cookie)
        self.assertIn("samesite=lax", session_cookie)
        self.assertIn(
            f"max-age={main.settings.session_max_age_seconds}",
            session_cookie,
        )

        if main.settings.session_cookie_secure:
            self.assertIn("secure", session_cookie)
        else:
            self.assertNotIn("secure", session_cookie)

        protected_response = self.client.get("/")

        self.assertEqual(protected_response.status_code, 200)
        self.assertIn("Angemeldet als", protected_response.text)

        logout_response = self.client.post(
            "/logout",
            follow_redirects=False,
        )

        self.assertEqual(logout_response.status_code, 303)
        self.assertEqual(
            logout_response.headers["location"],
            "/login",
        )

        protected_after_logout = self.client.get(
            "/",
            follow_redirects=False,
        )

        self.assertEqual(protected_after_logout.status_code, 303)


if __name__ == "__main__":
    unittest.main()
