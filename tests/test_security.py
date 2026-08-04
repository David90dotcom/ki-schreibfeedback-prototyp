from __future__ import annotations

import unittest

from pwdlib import PasswordHash

from app.security import (
    AUTHENTICATED_USER_SESSION_KEY,
    authenticated_username,
    end_authenticated_session,
    is_authenticated,
    start_authenticated_session,
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


if __name__ == "__main__":
    unittest.main()
    