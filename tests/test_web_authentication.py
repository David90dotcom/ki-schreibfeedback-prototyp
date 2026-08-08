from __future__ import annotations

import re
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from httpx import Response

from app import main
from app.security import LoginRateLimiter


def _csrf_token_from(response: Response) -> str:
    match = re.search(
        r'name="csrf_token"\s+value="([^"]+)"',
        response.text,
    )

    if match is None:
        raise AssertionError("CSRF-Token fehlt in der Antwort.")

    return match.group(1)


class WebAuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rate_limiter_patcher = patch.object(
            main,
            "login_rate_limiter",
            LoginRateLimiter(
                max_attempts=(
                    main.settings.login_rate_limit_attempts
                ),
                window_seconds=(
                    main.settings.login_rate_limit_window_seconds
                ),
            ),
        )
        self.rate_limiter_patcher.start()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        self.client.close()
        self.rate_limiter_patcher.stop()

    def _login(self) -> Response:
        csrf_token = _csrf_token_from(
            self.client.get("/login")
        )

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
                    "csrf_token": csrf_token,
                },
                follow_redirects=False,
            )

    def test_login_page_is_public(self) -> None:
        response = self.client.get("/login")

        self.assertEqual(response.status_code, 200)
        self.assertIn('name="username"', response.text)
        self.assertIn('name="password"', response.text)
        self.assertIn('name="csrf_token"', response.text)

        username_input_match = re.search(
            r'<input[^>]*name="username"[^>]*>',
            response.text,
            re.DOTALL,
        )

        self.assertIsNotNone(username_input_match)
        username_input = (
            username_input_match.group(0)
            if username_input_match is not None
            else ""
        )
        self.assertNotIn("value=", username_input)

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
        csrf_token = _csrf_token_from(
            self.client.get("/login")
        )

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
                    "csrf_token": csrf_token,
                },
            )

        self.assertEqual(response.status_code, 401)
        self.assertIn(
            "Benutzername oder Passwort ist falsch.",
            response.text,
        )
        self.assertNotIn('value="falsch"', response.text)

    def test_login_rejects_missing_or_invalid_csrf_token(
        self,
    ) -> None:
        self.client.get("/login")

        with patch.object(
            main,
            "verify_credentials",
            return_value=True,
        ) as verify_mock:
            missing_response = self.client.post(
                "/login",
                data={
                    "username": main.settings.auth_username,
                    "password": "testwert",
                },
            )
            invalid_response = self.client.post(
                "/login",
                data={
                    "username": main.settings.auth_username,
                    "password": "testwert",
                    "csrf_token": "falsches-token",
                },
            )

        self.assertEqual(missing_response.status_code, 403)
        self.assertEqual(invalid_response.status_code, 403)
        verify_mock.assert_not_called()

    def test_repeated_wrong_credentials_are_rate_limited(
        self,
    ) -> None:
        limiter = LoginRateLimiter(
            max_attempts=2,
            window_seconds=60,
        )
        csrf_token = _csrf_token_from(
            self.client.get("/login")
        )

        with (
            patch.object(
                main,
                "login_rate_limiter",
                limiter,
            ),
            patch.object(
                main,
                "verify_credentials",
                return_value=False,
            ) as verify_mock,
        ):
            first_response = self.client.post(
                "/login",
                data={
                    "username": "falsch",
                    "password": "falsch",
                    "csrf_token": csrf_token,
                },
            )
            second_response = self.client.post(
                "/login",
                data={
                    "username": "falsch",
                    "password": "falsch",
                    "csrf_token": csrf_token,
                },
            )
            blocked_response = self.client.post(
                "/login",
                data={
                    "username": main.settings.auth_username,
                    "password": "richtig",
                    "csrf_token": csrf_token,
                },
            )

        self.assertEqual(first_response.status_code, 401)
        self.assertEqual(second_response.status_code, 401)
        self.assertEqual(blocked_response.status_code, 429)
        self.assertEqual(
            blocked_response.headers["retry-after"],
            "60",
        )
        self.assertIn(
            "Zu viele fehlgeschlagene Anmeldeversuche.",
            blocked_response.text,
        )
        self.assertEqual(verify_mock.call_count, 2)

    def test_successful_login_resets_failed_attempts(
        self,
    ) -> None:
        limiter = LoginRateLimiter(
            max_attempts=2,
            window_seconds=60,
        )

        csrf_token = _csrf_token_from(
            self.client.get("/login")
        )

        with (
            patch.object(
                main,
                "login_rate_limiter",
                limiter,
            ),
            patch.object(
                main,
                "verify_credentials",
                side_effect=(False, True, False, False),
            ) as verify_mock,
        ):
            first_response = self.client.post(
                "/login",
                data={
                    "username": main.settings.auth_username,
                    "password": "testwert",
                    "csrf_token": csrf_token,
                },
                follow_redirects=False,
            )
            successful_response = self.client.post(
                "/login",
                data={
                    "username": main.settings.auth_username,
                    "password": "testwert",
                    "csrf_token": csrf_token,
                },
                follow_redirects=False,
            )

            authenticated_csrf_token = _csrf_token_from(
                self.client.get("/")
            )

            third_response = self.client.post(
                "/login",
                data={
                    "username": main.settings.auth_username,
                    "password": "testwert",
                    "csrf_token": authenticated_csrf_token,
                },
                follow_redirects=False,
            )
            fourth_response = self.client.post(
                "/login",
                data={
                    "username": main.settings.auth_username,
                    "password": "testwert",
                    "csrf_token": authenticated_csrf_token,
                },
                follow_redirects=False,
            )

        self.assertEqual(
            [
                first_response.status_code,
                successful_response.status_code,
                third_response.status_code,
                fourth_response.status_code,
            ],
            [401, 303, 401, 401],
        )
        self.assertEqual(verify_mock.call_count, 4)

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
            data={
                "csrf_token": _csrf_token_from(
                    protected_response
                ),
            },
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

    def test_authenticated_posts_require_csrf_token(self) -> None:
        self._login()

        with patch.object(
            main.feedback_service,
            "analyze_text",
            new=AsyncMock(),
        ) as analyze_mock:
            analyze_response = self.client.post(
                "/analyze",
                data={
                    "student_text": "Testtext",
                    "provider": "ollama",
                },
            )

        logout_response = self.client.post("/logout")

        self.assertEqual(analyze_response.status_code, 403)
        self.assertEqual(logout_response.status_code, 403)
        analyze_mock.assert_not_awaited()
        self.assertEqual(self.client.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
