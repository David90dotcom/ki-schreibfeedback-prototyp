from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.services.student_account_store import (
    StudentAccountConflictError,
    StudentAccountNotFoundError,
    StudentAccountStore,
)
from app.services.student_analysis_gate import (
    StudentAnalysisGate,
    StudentAnalysisInProgressError,
)


class StudentAccountStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "analysis.sqlite3"
        )
        self.codes = iter(("012345", "654321", "111222"))
        self.store = StudentAccountStore(
            self.database_path,
            code_secret="festes-test-secret",
            code_factory=lambda: next(self.codes),
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_creates_pseudonymous_account_without_plaintext_code(self) -> None:
        issued = asyncio.run(
            self.store.create_account("  Testperson   01  ")
        )

        self.assertEqual(issued.account.label, "Testperson 01")
        self.assertEqual(issued.access_code, "012345")
        self.assertTrue(issued.account.is_active)

        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT label, code_digest FROM student_accounts"
            ).fetchone()

        self.assertEqual(row[0], "Testperson 01")
        self.assertNotEqual(row[1], issued.access_code)
        self.assertNotIn(issued.access_code, row[1])
        self.assertEqual(len(row[1]), 64)

    def test_authenticates_only_exact_active_six_digit_code(self) -> None:
        issued = asyncio.run(self.store.create_account("Testperson 02"))

        for invalid_code in (
            "",
            "12345",
            "1234567",
            "abcdef",
            "１２３４５６",
            "999999",
        ):
            with self.subTest(invalid_code=invalid_code):
                self.assertIsNone(
                    asyncio.run(
                        self.store.authenticate_code(invalid_code)
                    )
                )

        account = asyncio.run(
            self.store.authenticate_code(issued.access_code)
        )

        self.assertIsNotNone(account)
        self.assertEqual(account.account_id, issued.account.account_id)
        self.assertIsNotNone(account.last_login_at)

    def test_deactivation_rotation_and_deletion_invalidate_access(self) -> None:
        issued = asyncio.run(self.store.create_account("Testperson 03"))
        account_id = issued.account.account_id

        disabled = asyncio.run(
            self.store.set_account_active(account_id, active=False)
        )
        self.assertFalse(disabled.is_active)
        self.assertEqual(
            disabled.access_version,
            issued.account.access_version + 1,
        )
        self.assertIsNone(
            asyncio.run(self.store.authenticate_code(issued.access_code))
        )

        enabled = asyncio.run(
            self.store.set_account_active(account_id, active=True)
        )
        self.assertTrue(enabled.is_active)

        replacement = asyncio.run(self.store.issue_new_code(account_id))
        self.assertEqual(replacement.access_code, "654321")
        self.assertIsNone(
            asyncio.run(self.store.authenticate_code(issued.access_code))
        )
        self.assertIsNotNone(
            asyncio.run(
                self.store.authenticate_code(replacement.access_code)
            )
        )

        asyncio.run(self.store.delete_account(account_id))
        self.assertIsNone(
            asyncio.run(
                self.store.authenticate_code(replacement.access_code)
            )
        )
        self.assertEqual(asyncio.run(self.store.list_accounts()), [])

        with self.assertRaises(StudentAccountNotFoundError):
            asyncio.run(self.store.delete_account(account_id))

    def test_labels_are_validated_and_unique_case_insensitively(self) -> None:
        for invalid_label in ("", "   ", "x" * 81):
            with self.subTest(invalid_label=invalid_label):
                with self.assertRaises(ValueError):
                    asyncio.run(self.store.create_account(invalid_label))

        asyncio.run(self.store.create_account("Testperson Alpha"))

        with self.assertRaises(StudentAccountConflictError):
            asyncio.run(self.store.create_account("testperson alpha"))


class StudentAnalysisGateTests(unittest.TestCase):
    def test_rejects_parallel_run_and_releases_after_completion(self) -> None:
        async def scenario() -> None:
            gate = StudentAnalysisGate()

            async with gate.reserve("account-1"):
                with self.assertRaises(StudentAnalysisInProgressError):
                    async with gate.reserve("account-1"):
                        pass

                async with gate.reserve("account-2"):
                    pass

            async with gate.reserve("account-1"):
                pass

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
