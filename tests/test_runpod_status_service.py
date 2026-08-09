from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import httpx

from app.services.runpod_status_service import RunPodStatusService


class RunPodStatusServiceTests(unittest.IsolatedAsyncioTestCase):
    API_KEY = "status-test-api-key"
    ENDPOINT_ID = "status-test-endpoint"
    GPU_TYPE_ID = "NVIDIA GeForce RTX 4090"

    async def test_snapshot_combines_health_supply_and_worker_details(
        self,
    ) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)

            if request.url.host == "api.runpod.ai":
                return httpx.Response(
                    200,
                    json={
                        "jobs": {
                            "completed": 1,
                            "failed": 0,
                            "inProgress": 0,
                            "inQueue": 0,
                            "retried": 0,
                        },
                        "workers": {
                            "idle": 1,
                            "ready": 0,
                            "running": 0,
                            "initializing": 0,
                            "throttled": 0,
                            "unhealthy": 0,
                        },
                    },
                )

            if request.url.path.endswith(
                f"/serverless/{self.ENDPOINT_ID}"
            ):
                return httpx.Response(
                    200,
                    json={
                        "gpu": {
                            "pools": ["ADA_24"],
                            "count": 1,
                        },
                        "workers": {"min": 0, "max": 1},
                        "scaling": {"idleTimeout": 600},
                        "timeout": 600000,
                        "flashboot": "FLASHBOOT",
                    },
                )

            if request.url.path.endswith("/catalog/gpus"):
                self.assertEqual(
                    request.url.params["product"],
                    "SERVERLESS",
                )
                self.assertEqual(
                    request.url.params["include"],
                    "AVAILABILITY",
                )
                return httpx.Response(
                    200,
                    json={
                        "gpus": [
                            {
                                "id": self.GPU_TYPE_ID,
                                "pool": "ADA_24",
                                "availability": "HIGH",
                            }
                        ]
                    },
                )

            if request.url.path.endswith("/workers"):
                return httpx.Response(
                    200,
                    json={
                        "endpointVersion": 20,
                        "summary": {"idle": 1, "total": 1},
                        "workers": [
                            {
                                "id": "worker-123",
                                "status": "IDLE",
                                "isStale": False,
                                "version": 20,
                                "gpuCount": 1,
                                "uptimeSeconds": 321,
                                "gpuTypeId": self.GPU_TYPE_ID,
                                "dataCenterId": "EU-RO-1",
                                "startedAt": (
                                    "2026-08-09T12:00:00Z"
                                ),
                            }
                        ],
                    },
                )

            return httpx.Response(404)

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={
                "Authorization": f"Bearer {self.API_KEY}",
            },
        )
        service = RunPodStatusService(
            api_key=self.API_KEY,
            idle_timeout_seconds=3600,
        )
        service.mark_success(
            "rtx4090_24gb",
            completed_at=datetime(
                2026,
                8,
                9,
                12,
                0,
                tzinfo=timezone.utc,
            ),
        )

        with patch.object(
            service,
            "_create_http_client",
            return_value=client,
        ):
            snapshot = await service.snapshot(
                endpoint_key="rtx4090_24gb",
                endpoint_label="RTX 4090 – 24 GB",
                endpoint_id=self.ENDPOINT_ID,
                gpu_type_ids=(self.GPU_TYPE_ID,),
            )

        self.assertEqual(snapshot["worker"]["state"], "warm")
        self.assertEqual(snapshot["supply"]["level"], "HIGH")
        self.assertEqual(
            snapshot["technical"]["endpointVersion"],
            20,
        )
        self.assertEqual(
            snapshot["configuration"]["idleTimeoutSeconds"],
            600,
        )
        self.assertEqual(
            snapshot["technical"]["workers"][0]["gpuTypeId"],
            self.GPU_TYPE_ID,
        )
        self.assertEqual(
            snapshot["warmWindow"]["estimatedUntil"],
            "2026-08-09T12:10:00+00:00",
        )
        self.assertEqual(
            snapshot["warmWindow"]["idleTimeoutMinutes"],
            10,
        )
        self.assertTrue(
            snapshot["warmWindow"]["configurationVerified"]
        )

        serialized = json.dumps(snapshot)
        self.assertNotIn(self.ENDPOINT_ID, serialized)

        self.assertEqual(len(requests), 4)
        for request in requests:
            self.assertEqual(
                request.headers["Authorization"],
                f"Bearer {self.API_KEY}",
            )

    async def test_missing_management_permission_is_transparent(
        self,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.runpod.ai":
                return httpx.Response(
                    200,
                    json={
                        "jobs": {"inQueue": 1},
                        "workers": {"initializing": 1},
                    },
                )

            return httpx.Response(
                403,
                json={"detail": "access denied"},
            )

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        service = RunPodStatusService(api_key=self.API_KEY)

        with patch.object(
            service,
            "_create_http_client",
            return_value=client,
        ):
            snapshot = await service.snapshot(
                endpoint_key="rtx4090_24gb",
                endpoint_label="RTX 4090 – 24 GB",
                endpoint_id=self.ENDPOINT_ID,
                gpu_type_ids=(self.GPU_TYPE_ID,),
            )

        self.assertEqual(
            snapshot["worker"]["state"],
            "initializing",
        )
        self.assertFalse(snapshot["supply"]["available"])
        self.assertTrue(snapshot["supply"]["permissionMissing"])
        self.assertFalse(snapshot["technical"]["available"])
        self.assertTrue(
            snapshot["technical"]["permissionMissing"]
        )


if __name__ == "__main__":
    unittest.main()
