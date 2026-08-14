from __future__ import annotations

import unittest

from pwdlib import PasswordHash

from app.security import (
    AUTHENTICATED_STUDENT_ACCESS_VERSION_SESSION_KEY,
    AUTHENTICATED_STUDENT_SESSION_KEY,
    AUTHENTICATED_USER_SESSION_KEY,
    CSRF_TOKEN_SESSION_KEY,
    LoginRateLimiter,
    authenticated_student_account_id,
    authenticated_student_access_version,
    authenticated_username,
    end_authenticated_session,
    get_or_create_csrf_token,
    is_valid_csrf_token,
    is_authenticated,
    start_authenticated_session,
    start_student_session,
    verify_credentials,
)


class SecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.username = "pruefer"
        cls.password = "correct-horse-battery-staple-test"
        cls.password_hash = PasswordHash.recommended().hash(
            cls.password
        )

    def test_correct_credentials_are_accepted(self) -> None:
        self.assertTrue(
            verify_credentials(
                username=self.username,
                password=self.password,
                expected_username=self.username,
                password_hash=self.password_hash,
            )
        )

    def test_wrong_credentials_are_rejected(self) -> None:
        self.assertFalse(
            verify_credentials(
                username="andere-person",
                password=self.password,
                expected_username=self.username,
                password_hash=self.password_hash,
            )
        )
        self.assertFalse(
            verify_credentials(
                username=self.username,
                password="falsches-passwort",
                expected_username=self.username,
                password_hash=self.password_hash,
            )
        )

    def test_missing_or_invalid_hash_is_rejected(self) -> None:
        for invalid_hash in (None, "", "kein-gueltiger-hash"):
            with self.subTest(password_hash=invalid_hash):
                self.assertFalse(
                    verify_credentials(
                        username=self.username,
                        password=self.password,
                        expected_username=self.username,
                        password_hash=invalid_hash,
                    )
                )

    def test_session_lifecycle(self) -> None:
        session: dict[str, object] = {
            "alter_wert": "wird entfernt"
        }

        start_authenticated_session(session, self.username)

        self.assertEqual(
            session,
            {
                AUTHENTICATED_USER_SESSION_KEY: self.username,
            },
        )
        self.assertEqual(
            authenticated_username(session),
            self.username,
        )
        self.assertTrue(
            is_authenticated(session, self.username)
        )
        self.assertFalse(
            is_authenticated(session, "andere-person")
        )

        end_authenticated_session(session)

        self.assertEqual(session, {})
        self.assertIsNone(authenticated_username(session))

    def test_student_session_is_separate_from_admin_session(self) -> None:
        session: dict[str, object] = {
            AUTHENTICATED_USER_SESSION_KEY: self.username,
        }

        start_student_session(session, "student-account-1", 3)

        self.assertEqual(
            session,
            {
                AUTHENTICATED_STUDENT_SESSION_KEY: "student-account-1",
                AUTHENTICATED_STUDENT_ACCESS_VERSION_SESSION_KEY: 3,
            },
        )
        self.assertEqual(
            authenticated_student_account_id(session),
            "student-account-1",
        )
        self.assertEqual(
            authenticated_student_access_version(session),
            3,
        )
        self.assertIsNone(authenticated_username(session))
        self.assertFalse(is_authenticated(session, self.username))

    def test_csrf_token_is_stored_and_verified(self) -> None:
        session: dict[str, object] = {}

        token = get_or_create_csrf_token(session)

        self.assertEqual(
            session[CSRF_TOKEN_SESSION_KEY],
            token,
        )
        self.assertEqual(
            get_or_create_csrf_token(session),
            token,
        )
        self.assertTrue(
            is_valid_csrf_token(session, token)
        )
        self.assertFalse(
            is_valid_csrf_token(session, "falsches-token")
        )
        self.assertFalse(
            is_valid_csrf_token(session, None)
        )

    def test_rate_limiter_blocks_after_maximum(self) -> None:
        now = [100.0]
        limiter = LoginRateLimiter(
            max_attempts=2,
            window_seconds=60,
            clock=lambda: now[0],
        )

        self.assertIsNone(
            limiter.retry_after_seconds("client-a")
        )

        limiter.record_failure("client-a")
        self.assertIsNone(
            limiter.retry_after_seconds("client-a")
        )

        limiter.record_failure("client-a")
        self.assertEqual(
            limiter.retry_after_seconds("client-a"),
            60,
        )

        self.assertIsNone(
            limiter.retry_after_seconds("client-b")
        )

    def test_rate_limiter_releases_after_window(self) -> None:
        now = [100.0]
        limiter = LoginRateLimiter(
            max_attempts=1,
            window_seconds=60,
            clock=lambda: now[0],
        )

        limiter.record_failure("client-a")
        now[0] += 30

        self.assertEqual(
            limiter.retry_after_seconds("client-a"),
            30,
        )

        now[0] += 30

        self.assertIsNone(
            limiter.retry_after_seconds("client-a")
        )

    def test_rate_limiter_resets_after_success(self) -> None:
        limiter = LoginRateLimiter(
            max_attempts=1,
            window_seconds=60,
        )

        limiter.record_failure("client-a")
        self.assertIsNotNone(
            limiter.retry_after_seconds("client-a")
        )

        limiter.reset("client-a")

        self.assertIsNone(
            limiter.retry_after_seconds("client-a")
        )


if __name__ == "__main__":
    unittest.main()
    authenticated_student_account_id,
    authenticated_student_access_version,
