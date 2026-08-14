from __future__ import annotations

import asyncio
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from httpx import Response

from app import main
from app.services.runpod_job_store import RunPodJobStore


def _csrf_token_from(response: Response) -> str:
    match = re.search(
        r'name="csrf_token"\s+value="([^"]+)"',
        response.text,
    )

    if match is None:
        raise AssertionError("CSRF-Token fehlt in der Antwort.")

    return match.group(1)


class RunPodJobManagementTests(unittest.TestCase):
    ENDPOINT_ID = "internal-endpoint-id-must-stay-secret"
    ENDPOINT_KEY = "standard"
    TRACKING_ID = "fead037f-17ba-4cce-955b-cf594212b43d"
    JOB_ID = "1ce415e5-e43f-4905-9b03-f6c17afb3b77-e1"

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = RunPodJobStore(
            Path(self.temporary_directory.name) / "analysis.sqlite3"
        )
        self.runpod_settings = replace(
            main.settings,
            runpod_api_key="test-api-key",
            runpod_endpoint_id=self.ENDPOINT_ID,
        )
        self.client = TestClient(main.app)
        csrf_token = _csrf_token_from(self.client.get("/login"))

        with patch.object(
            main,
            "verify_credentials",
            return_value=True,
        ):
            login_response = self.client.post(
                "/login",
                data={
                    "username": main.settings.auth_username,
                    "password": "integrationstest",
                    "csrf_token": csrf_token,
                },
                follow_redirects=False,
            )

        self.assertEqual(login_response.status_code, 303)
        self.csrf_token = _csrf_token_from(self.client.get("/"))

    def tearDown(self) -> None:
        self.client.close()
        self.temporary_directory.cleanup()

    def _record_active_job(self, status: str = "IN_QUEUE") -> None:
        asyncio.run(
            self.store.record_status(
                tracking_id=self.TRACKING_ID,
                job_id=self.JOB_ID,
                endpoint_key=self.ENDPOINT_KEY,
                endpoint_id=self.ENDPOINT_ID,
                status=status,
            )
        )

    def test_lists_registered_active_job_without_endpoint_id(self) -> None:
        self._record_active_job()

        with (
            patch.object(main, "settings", self.runpod_settings),
            patch.object(main, "runpod_job_store", self.store),
            patch.object(
                main.RunPodProvider,
                "get_job_status",
                new=AsyncMock(
                    return_value={
                        "id": self.JOB_ID,
                        "status": "IN_QUEUE",
                    }
                ),
            ),
        ):
            response = self.client.get(
                "/api/runpod/jobs",
                params={"endpoint_key": self.ENDPOINT_KEY},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        payload = response.json()
        self.assertEqual(len(payload["jobs"]), 1)
        self.assertEqual(payload["jobs"][0]["jobId"], self.JOB_ID)
        self.assertEqual(
            payload["jobs"][0]["trackingId"],
            self.TRACKING_ID,
        )
        self.assertEqual(payload["jobs"][0]["status"], "IN_QUEUE")
        self.assertNotIn(self.ENDPOINT_ID, response.text)
        self.assertNotIn("test-api-key", response.text)

    def test_runpod_provider_wires_submission_to_persistent_store(
        self,
    ) -> None:
        with (
            patch.object(main, "settings", self.runpod_settings),
            patch.object(main, "runpod_job_store", self.store),
        ):
            provider = main._provider_for_request(
                provider_key="runpod",
                ollama_base_url="",
                ollama_model="",
                ollama_custom_model="",
                openai_model="",
                openai_custom_model="",
                openai_api_key="",
                runpod_endpoint=self.ENDPOINT_KEY,
                runpod_tracking_id=self.TRACKING_ID,
            )
            callback = provider.job_status_callback

            self.assertIsNotNone(callback)

            if callback is not None:
                asyncio.run(callback(self.JOB_ID, "IN_QUEUE"))

        jobs = asyncio.run(
            self.store.list_active(endpoint_key=self.ENDPOINT_KEY)
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].tracking_id, self.TRACKING_ID)
        self.assertEqual(jobs[0].job_id, self.JOB_ID)

    def test_status_timeout_still_lists_locally_stored_job(self) -> None:
        self._record_active_job()

        async def delayed_status(_job_id: str) -> dict[str, str]:
            await asyncio.sleep(0.1)
            return {
                "id": self.JOB_ID,
                "status": "IN_PROGRESS",
            }

        with (
            patch.object(main, "settings", self.runpod_settings),
            patch.object(main, "runpod_job_store", self.store),
            patch.object(
                main,
                "RUNPOD_JOB_STATUS_REFRESH_TIMEOUT_SECONDS",
                0.01,
            ),
            patch.object(
                main.RunPodProvider,
                "get_job_status",
                new=AsyncMock(side_effect=delayed_status),
            ),
        ):
            response = self.client.get(
                "/api/runpod/jobs",
                params={"endpoint_key": self.ENDPOINT_KEY},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["jobs"]), 1)
        self.assertEqual(payload["jobs"][0]["jobId"], self.JOB_ID)
        self.assertEqual(payload["jobs"][0]["status"], "IN_QUEUE")
        self.assertFalse(payload["jobs"][0]["statusFresh"])

    def test_completed_job_disappears_from_active_list(self) -> None:
        self._record_active_job(status="IN_PROGRESS")

        with (
            patch.object(main, "settings", self.runpod_settings),
            patch.object(main, "runpod_job_store", self.store),
            patch.object(
                main.RunPodProvider,
                "get_job_status",
                new=AsyncMock(
                    return_value={
                        "id": self.JOB_ID,
                        "status": "COMPLETED",
                    }
                ),
            ),
        ):
            response = self.client.get(
                "/api/runpod/jobs",
                params={"endpoint_key": self.ENDPOINT_KEY},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["jobs"], [])
        self.assertEqual(
            asyncio.run(
                self.store.list_active(endpoint_key=self.ENDPOINT_KEY)
            ),
            [],
        )

    def test_cancel_aborts_only_submitted_job(self) -> None:
        self._record_active_job()
        cancel_mock = AsyncMock(
            return_value={
                "id": self.JOB_ID,
                "status": "CANCELLED",
            }
        )

        with (
            patch.object(main, "settings", self.runpod_settings),
            patch.object(main, "runpod_job_store", self.store),
            patch.object(
                main.RunPodProvider,
                "cancel_job",
                new=cancel_mock,
            ),
        ):
            response = self.client.post(
                "/api/runpod/jobs/cancel",
                data={
                    "endpoint_key": self.ENDPOINT_KEY,
                    "job_id": self.JOB_ID,
                    "csrf_token": self.csrf_token,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "CANCELLED")
        self.assertEqual(response.json()["jobId"], self.JOB_ID)
        self.assertNotIn(self.ENDPOINT_ID, response.text)
        cancel_mock.assert_awaited_once_with(self.JOB_ID)
        self.assertEqual(
            asyncio.run(
                self.store.list_active(endpoint_key=self.ENDPOINT_KEY)
            ),
            [],
        )

    def test_manual_legacy_job_can_be_cancelled(self) -> None:
        legacy_job_id = "fbf40f59-a0fd-4225-ae9b-b37f9818ac5a-e1"

        with (
            patch.object(main, "settings", self.runpod_settings),
            patch.object(main, "runpod_job_store", self.store),
            patch.object(
                main.RunPodProvider,
                "cancel_job",
                new=AsyncMock(
                    return_value={
                        "id": legacy_job_id,
                        "status": "CANCELLED",
                    }
                ),
            ) as cancel_mock,
        ):
            response = self.client.post(
                "/api/runpod/jobs/cancel",
                data={
                    "endpoint_key": self.ENDPOINT_KEY,
                    "job_id": legacy_job_id,
                    "csrf_token": self.csrf_token,
                },
            )

        self.assertEqual(response.status_code, 200)
        cancel_mock.assert_awaited_once_with(legacy_job_id)

    def test_cancel_rejects_invalid_csrf_and_job_id(self) -> None:
        with (
            patch.object(main, "settings", self.runpod_settings),
            patch.object(main, "runpod_job_store", self.store),
        ):
            invalid_csrf = self.client.post(
                "/api/runpod/jobs/cancel",
                data={
                    "endpoint_key": self.ENDPOINT_KEY,
                    "job_id": self.JOB_ID,
                    "csrf_token": "invalid",
                },
            )
            invalid_job_id = self.client.post(
                "/api/runpod/jobs/cancel",
                data={
                    "endpoint_key": self.ENDPOINT_KEY,
                    "job_id": "../../purge-queue",
                    "csrf_token": self.csrf_token,
                },
            )

        self.assertEqual(invalid_csrf.status_code, 403)
        self.assertEqual(invalid_job_id.status_code, 422)
        self.assertIn("kein gültiges Format", invalid_job_id.text)

    def test_job_management_requires_login(self) -> None:
        anonymous_client = TestClient(main.app)

        try:
            list_response = anonymous_client.get(
                "/api/runpod/jobs",
                params={"endpoint_key": self.ENDPOINT_KEY},
            )
            cancel_response = anonymous_client.post(
                "/api/runpod/jobs/cancel",
                data={
                    "endpoint_key": self.ENDPOINT_KEY,
                    "job_id": self.JOB_ID,
                    "csrf_token": "unavailable",
                },
            )
        finally:
            anonymous_client.close()

        self.assertEqual(list_response.status_code, 401)
        self.assertEqual(cancel_response.status_code, 401)
