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

    async def test_http_500_uses_graphql_fallback_for_real_details(
        self,
    ) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)

            if request.url.host == "api.runpod.ai":
                return httpx.Response(
                    200,
                    json={
                        "jobs": {"inQueue": 0, "inProgress": 0},
                        "workers": {"idle": 1},
                    },
                )

            if request.method == "POST" and request.url.path == "/graphql":
                self.assertEqual(
                    request.url.params["api_key"],
                    self.API_KEY,
                )
                body = json.loads(request.content)
                self.assertEqual(
                    body["variables"]["endpointId"],
                    self.ENDPOINT_ID,
                )
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "myself": {
                                "endpoint": {
                                    "id": self.ENDPOINT_ID,
                                    "gpuIds": "ADA_24",
                                    "gpuCount": 1,
                                    "idleTimeout": 3600,
                                    "executionTimeoutMs": 600000,
                                    "version": 20,
                                    "workersMin": 0,
                                    "workersMax": 1,
                                    "workerState": [
                                        {
                                            "time": "2026-08-09T19:15:00Z",
                                            "initializing": 0,
                                            "idle": 1,
                                            "running": 0,
                                            "throttled": 0,
                                            "unhealthy": 0,
                                        }
                                    ],
                                    "pods": [
                                        {
                                            "id": "graphql-worker-1",
                                            "desiredStatus": "RUNNING",
                                            "uptimeSeconds": 412,
                                            "version": 20,
                                            "slsVersion": 20,
                                            "lastStartedAt": (
                                                "2026-08-09T19:08:08Z"
                                            ),
                                            "machine": {
                                                "gpuTypeId": self.GPU_TYPE_ID,
                                                "gpuDisplayName": "RTX 4090",
                                                "dataCenterId": "EU-RO-1",
                                            },
                                        }
                                    ],
                                }
                            }
                        }
                    },
                )

            if request.url.path.endswith("/catalog/gpus"):
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

            if request.url.path.startswith("/v2/serverless/"):
                return httpx.Response(500, json={"detail": "beta failure"})

            return httpx.Response(404)

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={
                "Authorization": f"Bearer {self.API_KEY}",
            },
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

        self.assertEqual(snapshot["worker"]["state"], "warm")
        self.assertEqual(snapshot["supply"]["level"], "HIGH")
        self.assertTrue(snapshot["supply"]["configurationVerified"])
        self.assertEqual(snapshot["configuration"]["source"], "graphql")
        self.assertEqual(
            snapshot["configuration"]["idleTimeoutSeconds"],
            3600,
        )
        self.assertEqual(snapshot["technical"]["source"], "graphql")
        self.assertEqual(snapshot["technical"]["endpointVersion"], 20)
        self.assertEqual(
            snapshot["technical"]["workers"][0]["gpuTypeId"],
            self.GPU_TYPE_ID,
        )
        self.assertEqual(
            snapshot["technical"]["workers"][0]["status"],
            "IDLE",
        )
        self.assertNotIn(
            self.ENDPOINT_ID,
            json.dumps(snapshot),
        )
        self.assertNotIn(self.API_KEY, json.dumps(snapshot))
        self.assertEqual(
            sum(request.method == "POST" for request in requests),
            1,
        )

    async def test_double_management_failure_keeps_supply_and_health(
        self,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.runpod.ai":
                return httpx.Response(
                    200,
                    json={
                        "jobs": {"inQueue": 0, "inProgress": 0},
                        "workers": {"throttled": 1},
                    },
                )

            if request.method == "POST" and request.url.path == "/graphql":
                return httpx.Response(500)

            if request.url.path.startswith("/v2/serverless/"):
                return httpx.Response(500)

            if request.url.path.endswith("/v2/catalog/gpus"):
                return httpx.Response(
                    200,
                    json={
                        "gpus": [
                            {
                                "id": self.GPU_TYPE_ID,
                                "pool": "ADA_24",
                                "availability": "MEDIUM",
                            }
                        ]
                    },
                )

            return httpx.Response(404)

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

        self.assertEqual(snapshot["worker"]["state"], "throttled")
        self.assertEqual(snapshot["supply"]["level"], "MEDIUM")
        self.assertFalse(snapshot["supply"]["configurationVerified"])
        self.assertFalse(snapshot["configuration"]["available"])
        self.assertFalse(snapshot["technical"]["available"])
        self.assertTrue(snapshot["technical"]["aggregateAvailable"])
        self.assertEqual(snapshot["technical"]["source"], "health")
        self.assertEqual(snapshot["technical"]["counts"]["throttled"], 1)
        self.assertIn(
            "GraphQL-Rückfall",
            snapshot["configuration"]["message"],
        )
        self.assertIn(
            "GraphQL-Rückfall",
            snapshot["technical"]["diagnosticMessage"],
        )


if __name__ == "__main__":
    unittest.main()
