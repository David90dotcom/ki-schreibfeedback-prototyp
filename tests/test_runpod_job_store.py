from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.runpod_job_store import (
    RunPodJobStore,
    RunPodJobStoreError,
)


class RunPodJobStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "analysis.sqlite3"
        )
        self.store = RunPodJobStore(self.database_path)

    async def asyncTearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def test_active_job_survives_new_store_instance(self) -> None:
        await self.store.record_status(
            tracking_id="cf91e87c-c6d4-45b0-b917-69be47f053dd",
            job_id="job-active-e1",
            endpoint_key="rtx4090_24gb",
            endpoint_id="internal-endpoint-id",
            status="IN_QUEUE",
        )

        reopened_store = RunPodJobStore(self.database_path)
        jobs = await reopened_store.list_active(
            endpoint_key="rtx4090_24gb"
        )

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, "job-active-e1")
        self.assertEqual(jobs[0].status, "IN_QUEUE")
        self.assertEqual(
            jobs[0].endpoint_id,
            "internal-endpoint-id",
        )

    async def test_terminal_status_removes_job_from_active_list(
        self,
    ) -> None:
        await self.store.record_status(
            tracking_id="26df4ce5-ec20-44da-902d-4a258c72ce82",
            job_id="job-completed-e1",
            endpoint_key="rtx5090_32gb",
            endpoint_id="endpoint-5090",
            status="IN_PROGRESS",
        )

        updated = await self.store.update_known_job(
            endpoint_id="endpoint-5090",
            job_id="job-completed-e1",
            status="COMPLETED",
        )
        await self.store.record_status(
            tracking_id="26df4ce5-ec20-44da-902d-4a258c72ce82",
            job_id="job-completed-e1",
            endpoint_key="rtx5090_32gb",
            endpoint_id="endpoint-5090",
            status="IN_QUEUE",
        )
        jobs = await self.store.list_active(
            endpoint_key="rtx5090_32gb"
        )

        self.assertTrue(updated)
        self.assertEqual(jobs, [])

    async def test_tracking_id_cannot_be_reassigned(self) -> None:
        tracking_id = "fc305f3a-3f8b-43e8-a271-8e9f898e6865"
        await self.store.record_status(
            tracking_id=tracking_id,
            job_id="first-job-e1",
            endpoint_key="standard",
            endpoint_id="endpoint-standard",
            status="IN_QUEUE",
        )

        with self.assertRaises(RunPodJobStoreError):
            await self.store.record_status(
                tracking_id=tracking_id,
                job_id="different-job-e1",
                endpoint_key="standard",
                endpoint_id="endpoint-standard",
                status="IN_QUEUE",
            )

    async def test_tracking_id_advances_after_terminal_job(self) -> None:
        tracking_id = "8d659c48-16e3-4624-9a44-f5728bc6333c"
        await self.store.record_status(
            tracking_id=tracking_id,
            job_id="first-job-e1",
            endpoint_key="standard",
            endpoint_id="endpoint-standard",
            status="IN_PROGRESS",
        )
        await self.store.update_known_job(
            endpoint_id="endpoint-standard",
            job_id="first-job-e1",
            status="COMPLETED",
        )

        await self.store.record_status(
            tracking_id=tracking_id,
            job_id="second-job-e1",
            endpoint_key="standard",
            endpoint_id="endpoint-standard",
            status="IN_QUEUE",
        )
        jobs = await self.store.list_active(endpoint_key="standard")

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].tracking_id, tracking_id)
        self.assertEqual(jobs[0].job_id, "second-job-e1")
        self.assertEqual(jobs[0].status, "IN_QUEUE")
