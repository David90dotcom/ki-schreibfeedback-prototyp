from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx


RUNPOD_QUEUE_API_BASE_URL = "https://api.runpod.ai/v2"
RUNPOD_MANAGEMENT_API_BASE_URL = "https://api.runpod.io/v2"
RUNPOD_GRAPHQL_API_URL = "https://api.runpod.io/graphql"

RUNPOD_ENDPOINT_STATUS_QUERY = """
query RunPodEndpointStatus($endpointId: String!) {
  myself {
    endpoint(id: $endpointId) {
      id
      gpuIds
      gpuCount
      idleTimeout
      executionTimeoutMs
      version
      workersMin
      workersMax
      workerState(input: {granularity: LIVE}) {
        time
        initializing
        idle
        running
        throttled
        unhealthy
      }
      pods {
        id
        desiredStatus
        uptimeSeconds
        version
        slsVersion
        lastStartedAt
        machine {
          gpuTypeId
          gpuDisplayName
          dataCenterId
        }
      }
    }
  }
}
"""

SUPPLY_LEVELS = ("NONE", "LOW", "MEDIUM", "HIGH")
SUPPLY_LABELS = {
    "HIGH": "Hoch (HIGH)",
    "MEDIUM": "Mittel (MEDIUM)",
    "LOW": "Gering (LOW)",
    "NONE": "Nicht verfügbar (NONE)",
    "UNAVAILABLE": "Nicht abrufbar",
}
SUPPLY_TONES = {
    "HIGH": "success",
    "MEDIUM": "warning",
    "LOW": "limited",
    "NONE": "error",
    "UNAVAILABLE": "neutral",
}


class RunPodStatusService:
    """Liest RunPod-Betriebsdaten, ohne einen Modelljob auszulösen."""

    def __init__(
        self,
        *,
        api_key: str | None,
        idle_timeout_seconds: int = 3600,
        supply_cache_seconds: float = 60.0,
        worker_cache_seconds: float = 10.0,
    ) -> None:
        self.api_key = api_key.strip() if api_key else None
        self.idle_timeout_seconds = int(idle_timeout_seconds)
        self.supply_cache_seconds = float(supply_cache_seconds)
        self.worker_cache_seconds = float(worker_cache_seconds)

        if not 1 <= self.idle_timeout_seconds <= 3600:
            raise ValueError(
                "Das RunPod-Warmhaltefenster muss zwischen "
                "1 und 3600 Sekunden liegen."
            )

        self._cache: dict[
            tuple[str, ...],
            tuple[float, dict[str, Any]],
        ] = {}
        self._last_successful_request: dict[str, datetime] = {}
        self._endpoint_idle_timeouts: dict[str, int] = {}

    def mark_success(
        self,
        endpoint_key: str,
        *,
        completed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Merkt den letzten Erfolg für eine vorsichtige Warmhalteprognose."""

        timestamp = completed_at or datetime.now(timezone.utc)

        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)

        timestamp = timestamp.astimezone(timezone.utc)
        self._last_successful_request[endpoint_key] = timestamp

        return self._warm_window(endpoint_key)

    async def snapshot(
        self,
        *,
        endpoint_key: str,
        endpoint_label: str,
        endpoint_id: str | None,
        gpu_type_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        """Erzeugt eine browsergeeignete, geheimnisfreie Momentaufnahme."""

        checked_at = datetime.now(timezone.utc)

        if not self.api_key or not endpoint_id:
            worker = self._unavailable_worker_status(
                "RunPod ist serverseitig nicht vollständig konfiguriert."
            )
            supply = self._unavailable_supply(
                "Die GPU-Verfügbarkeit ist ohne vollständige "
                "RunPod-Konfiguration nicht abrufbar."
            )
            technical = self._unavailable_technical_details(
                "Technische Workerdaten sind nicht abrufbar."
            )
            configuration = self._unavailable_configuration(
                "Die Endpoint-Konfiguration ist nicht abrufbar."
            )
        else:
            async with self._create_http_client() as client:
                health_result, configuration, technical = await asyncio.gather(
                    self._read_health(client, endpoint_id),
                    self._cached(
                        ("configuration", endpoint_id),
                        self.supply_cache_seconds,
                        lambda: self._read_endpoint_configuration(
                            client,
                            endpoint_id,
                        ),
                    ),
                    self._cached(
                        ("workers", endpoint_id),
                        self.worker_cache_seconds,
                        lambda: self._read_worker_details(
                            client,
                            endpoint_id,
                        ),
                    ),
                )

                should_try_graphql = (
                    (
                        not configuration.get("available")
                        and configuration.get("fallbackEligible")
                    )
                    or (
                        not technical.get("available")
                        and technical.get("fallbackEligible")
                    )
                )

                if should_try_graphql:
                    graphql_result = await self._cached(
                        ("graphql-endpoint", endpoint_id),
                        self.worker_cache_seconds,
                        lambda: self._read_graphql_endpoint(
                            client,
                            endpoint_id,
                        ),
                    )

                    if graphql_result.get("available"):
                        if (
                            not configuration.get("available")
                            and configuration.get("fallbackEligible")
                        ):
                            configuration = graphql_result[
                                "configuration"
                            ]

                        if (
                            not technical.get("available")
                            and technical.get("fallbackEligible")
                        ):
                            technical = graphql_result["technical"]

                if (
                    not technical.get("available")
                    and health_result.get("available")
                ):
                    technical = self._technical_from_health(
                        technical,
                        health_result,
                    )

                supply = await self._read_supply(
                    client,
                    configuration=configuration,
                    fallback_gpu_type_ids=gpu_type_ids,
                )

            worker = self._classify_worker_status(health_result)

            configured_idle_timeout = configuration.get(
                "idleTimeoutSeconds"
            )

            if (
                isinstance(configured_idle_timeout, int)
                and 1 <= configured_idle_timeout <= 3600
            ):
                self._endpoint_idle_timeouts[endpoint_key] = (
                    configured_idle_timeout
                )

        return {
            "endpoint": {
                "key": endpoint_key,
                "label": endpoint_label,
            },
            "checkedAt": checked_at.isoformat(),
            "worker": worker,
            "supply": supply,
            "warmWindow": self._warm_window(
                endpoint_key,
                configuration=configuration,
            ),
            "configuration": configuration,
            "technical": technical,
        }

    def _create_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            timeout=8.0,
            trust_env=False,
        )

    async def _cached(
        self,
        key: tuple[str, ...],
        ttl_seconds: float,
        loader: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        now = monotonic()
        cached = self._cache.get(key)

        if cached is not None and cached[0] > now:
            return cached[1]

        value = await loader()
        self._cache[key] = (now + ttl_seconds, value)
        return value

    async def _read_health(
        self,
        client: httpx.AsyncClient,
        endpoint_id: str,
    ) -> dict[str, Any]:
        encoded_id = quote(endpoint_id, safe="")
        status_code, payload, failure = await self._get_json(
            client,
            f"{RUNPOD_QUEUE_API_BASE_URL}/{encoded_id}/health",
        )

        if status_code != 200 or payload is None:
            return {
                "available": False,
                "message": self._failure_message(
                    "Der Workerstatus ist momentan nicht abrufbar.",
                    status_code,
                    failure,
                ),
            }

        raw_jobs = payload.get("jobs")
        raw_workers = payload.get("workers")

        if not isinstance(raw_jobs, dict):
            raw_jobs = {}
        if not isinstance(raw_workers, dict):
            raw_workers = {}

        return {
            "available": True,
            "jobs": {
                "completed": self._count(raw_jobs.get("completed")),
                "failed": self._count(raw_jobs.get("failed")),
                "inProgress": self._count(raw_jobs.get("inProgress")),
                "inQueue": self._count(raw_jobs.get("inQueue")),
                "retried": self._count(raw_jobs.get("retried")),
            },
            "workers": {
                "idle": self._count(raw_workers.get("idle")),
                "ready": self._count(raw_workers.get("ready")),
                "running": self._count(raw_workers.get("running")),
                "initializing": self._count(
                    raw_workers.get("initializing")
                ),
                "throttled": self._count(raw_workers.get("throttled")),
                "unhealthy": self._count(raw_workers.get("unhealthy")),
            },
        }

    async def _read_endpoint_configuration(
        self,
        client: httpx.AsyncClient,
        endpoint_id: str,
    ) -> dict[str, Any]:
        encoded_id = quote(endpoint_id, safe="")
        status_code, payload, failure = await self._get_json(
            client,
            (
                f"{RUNPOD_MANAGEMENT_API_BASE_URL}/serverless/"
                f"{encoded_id}"
            ),
        )

        if status_code != 200 or payload is None:
            permission_missing = status_code == 403
            message = (
                "Dem RunPod-API-Key fehlt die Leseberechtigung "
                "für die Endpoint-Konfiguration."
                if permission_missing
                else self._failure_message(
                    "Die Endpoint-Konfiguration ist momentan nicht abrufbar.",
                    status_code,
                    failure,
                )
            )
            return self._unavailable_configuration(
                message,
                permission_missing=permission_missing,
                fallback_eligible=self._fallback_eligible(
                    status_code,
                    failure,
                ),
            )

        gpu = payload.get("gpu")
        workers = payload.get("workers")
        scaling = payload.get("scaling")

        if not isinstance(gpu, dict):
            gpu = {}
        if not isinstance(workers, dict):
            workers = {}
        if not isinstance(scaling, dict):
            scaling = {}

        gpu_type_ids = self._safe_string_list(
            gpu.get(
                "typeIds",
                gpu.get("types", gpu.get("gpuTypeIds")),
            )
        )

        return {
            "available": True,
            "permissionMissing": False,
            "gpuPools": self._safe_string_list(gpu.get("pools")),
            "gpuTypeIds": gpu_type_ids,
            "gpuCount": self._count_or_none(gpu.get("count")),
            "idleTimeoutSeconds": self._count_or_none(
                scaling.get("idleTimeout")
            ),
            "executionTimeoutMs": self._count_or_none(
                payload.get("timeout")
            ),
            "minimumWorkers": self._count_or_none(
                workers.get("min")
            ),
            "maximumWorkers": self._count_or_none(
                workers.get("max")
            ),
            "flashboot": self._safe_string(payload.get("flashboot")),
            "source": "rest_v2",
            "message": (
                "Aktuelle, lesend aus RunPod abgerufene "
                "Endpoint-Konfiguration."
            ),
        }

    async def _read_supply(
        self,
        client: httpx.AsyncClient,
        *,
        configuration: dict[str, Any],
        fallback_gpu_type_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        configuration_available = bool(
            configuration.get("available")
        )
        gpu_pools = (
            set(configuration.get("gpuPools") or [])
            if configuration_available
            else set()
        )
        gpu_type_ids = (
            set(configuration.get("gpuTypeIds") or [])
            if configuration_available
            else set()
        )
        used_fallback = not configuration_available

        if not gpu_pools and not gpu_type_ids:
            gpu_type_ids = set(fallback_gpu_type_ids)
            used_fallback = True

        if not gpu_pools and not gpu_type_ids:
            return self._unavailable_supply(
                "RunPod meldet für diesen Endpoint keine auswertbare "
                "GPU-Konfiguration."
            )

        if gpu_pools:
            catalog = await self._cached(
                ("gpu-catalog",),
                self.supply_cache_seconds,
                lambda: self._read_gpu_catalog(client),
            )
        else:
            sorted_gpu_type_ids = tuple(sorted(gpu_type_ids))
            catalog = await self._cached(
                ("gpu-types", *sorted_gpu_type_ids),
                self.supply_cache_seconds,
                lambda: self._read_gpu_types(
                    client,
                    sorted_gpu_type_ids,
                ),
            )

        if not catalog.get("available"):
            return self._unavailable_supply(
                str(
                    catalog.get("message")
                    or "Der GPU-Katalog ist nicht abrufbar."
                ),
                permission_missing=bool(
                    catalog.get("permissionMissing")
                ),
            )

        matched_gpus = [
            gpu
            for gpu in catalog["gpus"]
            if (
                gpu.get("pool") in gpu_pools
                or gpu.get("id") in gpu_type_ids
            )
            and gpu.get("level") in SUPPLY_LEVELS
        ]

        if not matched_gpus:
            return self._unavailable_supply(
                "Der GPU-Katalog enthält keine Supply-Daten für "
                "die tatsächlich konfigurierte Endpoint-Auswahl."
            )

        level = max(
            (gpu["level"] for gpu in matched_gpus),
            key=SUPPLY_LEVELS.index,
        )

        return {
            "available": True,
            "level": level,
            "label": SUPPLY_LABELS[level],
            "tone": SUPPLY_TONES[level],
            "isPool": bool(gpu_pools) or len(matched_gpus) > 1,
            "gpuPools": sorted(gpu_pools),
            "gpuTypes": [
                {
                    "id": gpu["id"],
                    "pool": gpu["pool"],
                    "level": gpu["level"],
                    "label": SUPPLY_LABELS[gpu["level"]],
                }
                for gpu in matched_gpus
            ],
            "message": (
                "Momentaufnahme der allgemeinen RunPod-Kapazität "
                "für den tatsächlich konfigurierten GPU-Pool; keine "
                "Garantie für einen erfolgreichen Workerstart."
                if not used_fallback
                else (
                    "Momentaufnahme für die in der Web-App hinterlegte "
                    "GPU-Klasse; die tatsächliche Endpoint-Konfiguration "
                    "konnte nicht vollständig verifiziert werden. Keine "
                    "Garantie für einen erfolgreichen Workerstart."
                )
            ),
            "permissionMissing": False,
            "configurationVerified": not used_fallback,
        }

    async def _read_gpu_types(
        self,
        client: httpx.AsyncClient,
        gpu_type_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        results = await asyncio.gather(
            *(
                self._read_gpu_type(client, gpu_type_id)
                for gpu_type_id in gpu_type_ids
            )
        )
        gpus = [
            result["gpu"]
            for result in results
            if result.get("available") and result.get("gpu")
        ]

        if gpus:
            return {
                "available": True,
                "permissionMissing": False,
                "gpus": gpus,
                "message": "GPU-Klassen erfolgreich geladen.",
            }

        first_failure = next(
            (
                result
                for result in results
                if not result.get("available")
            ),
            {},
        )
        return {
            "available": False,
            "permissionMissing": bool(
                first_failure.get("permissionMissing")
            ),
            "gpus": [],
            "message": str(
                first_failure.get("message")
                or "Die GPU-Klassen sind nicht abrufbar."
            ),
        }

    async def _read_gpu_type(
        self,
        client: httpx.AsyncClient,
        gpu_type_id: str,
    ) -> dict[str, Any]:
        encoded_id = quote(gpu_type_id, safe="")
        status_code, payload, failure = await self._get_json(
            client,
            (
                f"{RUNPOD_MANAGEMENT_API_BASE_URL}/catalog/gpus/"
                f"{encoded_id}"
            ),
            params={
                "include": "AVAILABILITY",
                "product": "SERVERLESS",
                "count": "1",
            },
        )

        if status_code != 200 or payload is None:
            permission_missing = status_code == 403
            return {
                "available": False,
                "permissionMissing": permission_missing,
                "message": (
                    "Dem RunPod-API-Key fehlt die Leseberechtigung "
                    "für den GPU-Katalog."
                    if permission_missing
                    else self._failure_message(
                        "Die GPU-Klasse ist momentan nicht abrufbar.",
                        status_code,
                        failure,
                    )
                ),
            }

        raw_level = self._safe_string(payload.get("availability"))
        level = raw_level.upper() if raw_level else "UNAVAILABLE"
        gpu_id = self._safe_string(payload.get("id"))

        if gpu_id is None or level not in SUPPLY_LEVELS:
            return {
                "available": False,
                "permissionMissing": False,
                "message": (
                    "RunPod lieferte für diese GPU-Klasse keine "
                    "auswertbare Supply-Stufe."
                ),
            }

        return {
            "available": True,
            "permissionMissing": False,
            "gpu": {
                "id": gpu_id,
                "pool": self._safe_string(payload.get("pool")),
                "level": level,
            },
        }

    async def _read_gpu_catalog(
        self,
        client: httpx.AsyncClient,
    ) -> dict[str, Any]:
        status_code, payload, failure = await self._get_json(
            client,
            f"{RUNPOD_MANAGEMENT_API_BASE_URL}/catalog/gpus",
            params={
                "include": "AVAILABILITY",
                "product": "SERVERLESS",
                "count": "1",
            },
        )

        if status_code != 200 or payload is None:
            permission_missing = status_code == 403
            message = (
                "Dem RunPod-API-Key fehlt die Leseberechtigung "
                "für den GPU-Katalog."
                if permission_missing
                else self._failure_message(
                    "Der GPU-Katalog ist momentan nicht abrufbar.",
                    status_code,
                    failure,
                )
            )
            return {
                "available": False,
                "permissionMissing": permission_missing,
                "gpus": [],
                "message": message,
            }

        raw_gpus = payload.get("gpus")

        if not isinstance(raw_gpus, list):
            raw_gpus = []

        gpus: list[dict[str, str | None]] = []

        for raw_gpu in raw_gpus:
            if not isinstance(raw_gpu, dict):
                continue

            gpu_id = self._safe_string(raw_gpu.get("id"))
            raw_level = self._safe_string(raw_gpu.get("availability"))
            level = raw_level.upper() if raw_level else "UNAVAILABLE"

            if gpu_id is None or level not in SUPPLY_LEVELS:
                continue

            gpus.append(
                {
                    "id": gpu_id,
                    "pool": self._safe_string(raw_gpu.get("pool")),
                    "level": level,
                }
            )

        return {
            "available": True,
            "permissionMissing": False,
            "gpus": gpus,
            "message": "GPU-Katalog erfolgreich geladen.",
        }

    async def _read_worker_details(
        self,
        client: httpx.AsyncClient,
        endpoint_id: str,
    ) -> dict[str, Any]:
        encoded_id = quote(endpoint_id, safe="")
        status_code, payload, failure = await self._get_json(
            client,
            (
                f"{RUNPOD_MANAGEMENT_API_BASE_URL}/serverless/"
                f"{encoded_id}/workers"
            ),
        )

        if status_code != 200 or payload is None:
            permission_missing = status_code == 403
            message = (
                "Dem RunPod-API-Key fehlt die Leseberechtigung "
                "für technische Workerdaten."
                if permission_missing
                else self._failure_message(
                    "Technische Workerdaten sind momentan nicht abrufbar.",
                    status_code,
                    failure,
                )
            )
            return self._unavailable_technical_details(
                message,
                permission_missing=permission_missing,
                fallback_eligible=self._fallback_eligible(
                    status_code,
                    failure,
                ),
            )

        raw_workers = payload.get("workers")

        if not isinstance(raw_workers, list):
            raw_workers = []

        workers: list[dict[str, Any]] = []

        for raw_worker in raw_workers:
            if not isinstance(raw_worker, dict):
                continue

            workers.append(
                {
                    "id": self._safe_string(raw_worker.get("id")),
                    "status": self._safe_string(raw_worker.get("status")),
                    "gpuTypeId": self._safe_string(
                        raw_worker.get("gpuTypeId")
                    ),
                    "dataCenterId": self._safe_string(
                        raw_worker.get("dataCenterId")
                    ),
                    "version": self._count_or_none(
                        raw_worker.get("version")
                    ),
                    "uptimeSeconds": self._count_or_none(
                        raw_worker.get("uptimeSeconds")
                    ),
                    "startedAt": self._safe_string(
                        raw_worker.get("startedAt")
                    ),
                    "isStale": (
                        raw_worker.get("isStale")
                        if isinstance(raw_worker.get("isStale"), bool)
                        else None
                    ),
                }
            )

        return {
            "available": True,
            "permissionMissing": False,
            "aggregateAvailable": True,
            "source": "rest_v2",
            "endpointVersion": self._count_or_none(
                payload.get("endpointVersion")
            ),
            "counts": self._worker_counts(payload.get("summary")),
            "workers": workers,
            "message": (
                "Nur aktuell aktive Worker werden aufgeführt. "
                "Herunterskalierte Worker erscheinen hier nicht."
            ),
        }

    async def _read_graphql_endpoint(
        self,
        client: httpx.AsyncClient,
        endpoint_id: str,
    ) -> dict[str, Any]:
        status_code, payload, failure = await self._post_json(
            client,
            RUNPOD_GRAPHQL_API_URL,
            json_body={
                "query": RUNPOD_ENDPOINT_STATUS_QUERY,
                "variables": {"endpointId": endpoint_id},
            },
        )

        if status_code != 200 or payload is None:
            return {
                "available": False,
                "message": self._failure_message(
                    "Auch der offizielle GraphQL-Rückfall ist momentan "
                    "nicht abrufbar.",
                    status_code,
                    failure,
                ),
            }

        if isinstance(payload.get("errors"), list):
            return {
                "available": False,
                "message": (
                    "RunPod konnte die Endpoint-Daten auch über den "
                    "GraphQL-Rückfall nicht bereitstellen."
                ),
            }

        data = payload.get("data")
        myself = data.get("myself") if isinstance(data, dict) else None
        endpoint = (
            myself.get("endpoint")
            if isinstance(myself, dict)
            else None
        )

        if not isinstance(endpoint, dict):
            return {
                "available": False,
                "message": (
                    "RunPod meldete den ausgewählten Endpoint im "
                    "GraphQL-Rückfall nicht zurück."
                ),
            }

        returned_endpoint_id = self._safe_string(endpoint.get("id"))

        if returned_endpoint_id != endpoint_id:
            return {
                "available": False,
                "message": (
                    "RunPod lieferte im GraphQL-Rückfall keine eindeutig "
                    "zuordenbare Endpoint-Konfiguration."
                ),
            }

        endpoint_version = self._count_or_none(endpoint.get("version"))
        counts = self._latest_worker_counts(endpoint.get("workerState"))
        raw_pods = endpoint.get("pods")

        if not isinstance(raw_pods, list):
            raw_pods = []

        workers: list[dict[str, Any]] = []

        for raw_pod in raw_pods:
            if not isinstance(raw_pod, dict):
                continue

            machine = raw_pod.get("machine")

            if not isinstance(machine, dict):
                machine = {}

            worker_version = self._count_or_none(
                raw_pod.get("slsVersion")
            )

            if worker_version is None:
                worker_version = self._count_or_none(
                    raw_pod.get("version")
                )

            gpu_type_id = self._safe_string(
                machine.get("gpuTypeId")
            ) or self._safe_string(machine.get("gpuDisplayName"))

            workers.append(
                {
                    "id": self._safe_string(raw_pod.get("id")),
                    "status": self._safe_string(
                        raw_pod.get("desiredStatus")
                    ),
                    "gpuTypeId": gpu_type_id,
                    "dataCenterId": self._safe_string(
                        machine.get("dataCenterId")
                    ),
                    "version": worker_version,
                    "uptimeSeconds": self._count_or_none(
                        raw_pod.get("uptimeSeconds")
                    ),
                    "startedAt": self._safe_string(
                        raw_pod.get("lastStartedAt")
                    ),
                    "isStale": (
                        worker_version != endpoint_version
                        if worker_version is not None
                        and endpoint_version is not None
                        else None
                    ),
                }
            )

        if len(workers) == 1:
            aggregate_status = self._single_worker_status(counts)

            if aggregate_status:
                workers[0]["status"] = aggregate_status

        configuration = {
            "available": True,
            "permissionMissing": False,
            "fallbackEligible": False,
            "gpuPools": self._split_gpu_ids(endpoint.get("gpuIds")),
            "gpuTypeIds": [],
            "gpuCount": self._count_or_none(endpoint.get("gpuCount")),
            "idleTimeoutSeconds": self._count_or_none(
                endpoint.get("idleTimeout")
            ),
            "executionTimeoutMs": self._count_or_none(
                endpoint.get("executionTimeoutMs")
            ),
            "minimumWorkers": self._count_or_none(
                endpoint.get("workersMin")
            ),
            "maximumWorkers": self._count_or_none(
                endpoint.get("workersMax")
            ),
            "flashboot": None,
            "source": "graphql",
            "message": (
                "Endpoint-Konfiguration über den offiziellen "
                "GraphQL-Rückfall geladen, weil RunPods REST API v2 "
                "nicht verfügbar war."
            ),
        }
        technical = {
            "available": True,
            "permissionMissing": False,
            "fallbackEligible": False,
            "aggregateAvailable": True,
            "source": "graphql",
            "endpointVersion": endpoint_version,
            "counts": counts,
            "workers": workers,
            "message": (
                "Workerdetails über den offiziellen GraphQL-Rückfall "
                "geladen, weil RunPods REST API v2 nicht verfügbar war."
            ),
        }

        return {
            "available": True,
            "configuration": configuration,
            "technical": technical,
        }

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        params: dict[str, str] | None = None,
    ) -> tuple[int | None, dict[str, Any] | None, str | None]:
        try:
            response = await client.get(url, params=params)
        except httpx.TimeoutException:
            return None, None, "timeout"
        except httpx.RequestError:
            return None, None, "connection"

        if response.status_code != 200:
            return response.status_code, None, None

        try:
            payload = response.json()
        except ValueError:
            return response.status_code, None, "invalid_json"

        if not isinstance(payload, dict):
            return response.status_code, None, "invalid_json"

        return response.status_code, payload, None

    async def _post_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        json_body: dict[str, Any],
    ) -> tuple[int | None, dict[str, Any] | None, str | None]:
        try:
            response = await client.post(url, json=json_body)
        except httpx.TimeoutException:
            return None, None, "timeout"
        except httpx.RequestError:
            return None, None, "connection"

        if response.status_code != 200:
            return response.status_code, None, None

        try:
            payload = response.json()
        except ValueError:
            return response.status_code, None, "invalid_json"

        if not isinstance(payload, dict):
            return response.status_code, None, "invalid_json"

        return response.status_code, payload, None

    def _classify_worker_status(
        self,
        health: dict[str, Any],
    ) -> dict[str, Any]:
        if not health.get("available"):
            return self._unavailable_worker_status(
                str(
                    health.get("message")
                    or "Der Workerstatus ist nicht abrufbar."
                )
            )

        jobs = health["jobs"]
        workers = health["workers"]

        if jobs["inProgress"] > 0 or workers["running"] > 0:
            state = "processing"
            label = "Worker verarbeitet gerade eine Anfrage"
            tone = "info"
        elif workers["unhealthy"] > 0:
            state = "unhealthy"
            label = (
                "Workerstart fehlgeschlagen – Ersatz wird "
                "möglicherweise gestartet"
            )
            tone = "error"
        elif workers["initializing"] > 0:
            state = "initializing"
            label = "Worker wird gestartet – mehrere Minuten möglich"
            tone = "warning"
        elif workers["throttled"] > 0:
            state = "throttled"
            label = "GPU-Kapazität momentan eingeschränkt"
            tone = "limited"
        elif jobs["inQueue"] > 0:
            state = "queued"
            label = "Auftrag wartet auf einen Worker – Cold Start möglich"
            tone = "warning"
        elif workers["idle"] + workers["ready"] > 0:
            state = "warm"
            label = "Worker aktiv – kurze Wartezeit erwartet"
            tone = "success"
        else:
            state = "cold"
            label = "Cold Start erforderlich"
            tone = "neutral"

        return {
            "available": True,
            "state": state,
            "label": label,
            "tone": tone,
            "jobs": jobs,
            "counts": workers,
        }

    def _warm_window(
        self,
        endpoint_key: str,
        *,
        configuration: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        configured_timeout = (
            configuration.get("idleTimeoutSeconds")
            if configuration and configuration.get("available")
            else None
        )

        if (
            isinstance(configured_timeout, int)
            and 1 <= configured_timeout <= 3600
        ):
            idle_timeout_seconds = configured_timeout
            configuration_verified = True
        else:
            idle_timeout_seconds = self._endpoint_idle_timeouts.get(
                endpoint_key,
                self.idle_timeout_seconds,
            )
            configuration_verified = endpoint_key in (
                self._endpoint_idle_timeouts
            )

        last_success = self._last_successful_request.get(endpoint_key)
        estimated_until = (
            last_success + timedelta(seconds=idle_timeout_seconds)
            if last_success is not None
            else None
        )
        now = datetime.now(timezone.utc)

        return {
            "idleTimeoutSeconds": idle_timeout_seconds,
            "idleTimeoutMinutes": idle_timeout_seconds // 60,
            "configurationVerified": configuration_verified,
            "lastSuccessfulAt": (
                last_success.isoformat()
                if last_success is not None
                else None
            ),
            "estimatedUntil": (
                estimated_until.isoformat()
                if estimated_until is not None
                else None
            ),
            "estimateActive": bool(
                estimated_until is not None
                and estimated_until > now
            ),
            "message": (
                "Voraussichtliche Obergrenze nach der letzten "
                "erfolgreichen Anfrage; keine garantierte Reservierung."
                if configuration_verified
                else (
                    "Erwarteter Wert aus der Web-Konfiguration; die "
                    "RunPod-Einstellung konnte nicht verifiziert werden."
                )
            ),
        }

    def _technical_from_health(
        self,
        technical: dict[str, Any],
        health: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "available": False,
            "permissionMissing": bool(
                technical.get("permissionMissing")
            ),
            "fallbackEligible": False,
            "aggregateAvailable": True,
            "source": "health",
            "endpointVersion": None,
            "counts": self._worker_counts(health.get("workers")),
            "jobs": {
                key: self._count(value)
                for key, value in (health.get("jobs") or {}).items()
            },
            "workers": [],
            "message": (
                "Der aggregierte Workerstatus ist verfügbar. "
                "Worker-ID, tatsächliche GPU, Release und Rechenzentrum "
                "werden von RunPods Verwaltungs-API momentan nicht "
                "bereitgestellt."
            ),
            "diagnosticMessage": self._safe_string(
                technical.get("message")
            ),
        }

    def _latest_worker_counts(self, value: Any) -> dict[str, int]:
        if not isinstance(value, list):
            return self._worker_counts(None)

        states = [item for item in value if isinstance(item, dict)]

        if not states:
            return self._worker_counts(None)

        latest = max(
            states,
            key=lambda item: self._safe_string(item.get("time")) or "",
        )
        return self._worker_counts(latest)

    def _worker_counts(self, value: Any) -> dict[str, int]:
        source = value if isinstance(value, dict) else {}
        return {
            "idle": self._count(source.get("idle")),
            "ready": self._count(source.get("ready")),
            "running": self._count(source.get("running")),
            "initializing": self._count(source.get("initializing")),
            "throttled": self._count(source.get("throttled")),
            "unhealthy": self._count(source.get("unhealthy")),
        }

    @staticmethod
    def _single_worker_status(counts: dict[str, int]) -> str | None:
        for key, label in (
            ("unhealthy", "UNHEALTHY"),
            ("initializing", "INITIALIZING"),
            ("throttled", "THROTTLED"),
            ("running", "RUNNING"),
            ("idle", "IDLE"),
            ("ready", "READY"),
        ):
            if counts.get(key, 0) > 0:
                return label

        return None

    @classmethod
    def _split_gpu_ids(cls, value: Any) -> list[str]:
        if isinstance(value, list):
            return cls._safe_string_list(value)

        cleaned = cls._safe_string(value)

        if cleaned is None:
            return []

        values: list[str] = []

        for item in cleaned.replace(";", ",").split(","):
            gpu_id = item.strip()

            if gpu_id and gpu_id not in values:
                values.append(gpu_id)

        return values

    @staticmethod
    def _fallback_eligible(
        status_code: int | None,
        failure: str | None,
    ) -> bool:
        if failure in {"timeout", "connection", "invalid_json"}:
            return True

        return bool(
            status_code == 404
            or (
                status_code is not None
                and 500 <= status_code <= 599
            )
        )

    @staticmethod
    def _unavailable_worker_status(message: str) -> dict[str, Any]:
        return {
            "available": False,
            "state": "unavailable",
            "label": "Workerstatus nicht abrufbar",
            "tone": "neutral",
            "message": message,
            "jobs": {},
            "counts": {},
        }

    @staticmethod
    def _unavailable_supply(
        message: str,
        *,
        permission_missing: bool = False,
    ) -> dict[str, Any]:
        return {
            "available": False,
            "level": "UNAVAILABLE",
            "label": SUPPLY_LABELS["UNAVAILABLE"],
            "tone": SUPPLY_TONES["UNAVAILABLE"],
            "isPool": False,
            "gpuTypes": [],
            "message": message,
            "permissionMissing": permission_missing,
        }

    @staticmethod
    def _unavailable_technical_details(
        message: str,
        *,
        permission_missing: bool = False,
        fallback_eligible: bool = False,
    ) -> dict[str, Any]:
        return {
            "available": False,
            "permissionMissing": permission_missing,
            "fallbackEligible": fallback_eligible,
            "aggregateAvailable": False,
            "source": None,
            "endpointVersion": None,
            "counts": {},
            "workers": [],
            "message": message,
            "diagnosticMessage": None,
        }

    @staticmethod
    def _unavailable_configuration(
        message: str,
        *,
        permission_missing: bool = False,
        fallback_eligible: bool = False,
    ) -> dict[str, Any]:
        return {
            "available": False,
            "permissionMissing": permission_missing,
            "fallbackEligible": fallback_eligible,
            "gpuPools": [],
            "gpuTypeIds": [],
            "gpuCount": None,
            "idleTimeoutSeconds": None,
            "executionTimeoutMs": None,
            "minimumWorkers": None,
            "maximumWorkers": None,
            "flashboot": None,
            "source": None,
            "message": message,
        }

    @staticmethod
    def _failure_message(
        fallback: str,
        status_code: int | None,
        failure: str | None,
    ) -> str:
        if status_code is not None:
            return f"{fallback} RunPod antwortete mit HTTP {status_code}."
        if failure == "timeout":
            return f"{fallback} Die Statusabfrage lief in ein Zeitlimit."
        if failure == "connection":
            return f"{fallback} Die RunPod-API war nicht erreichbar."
        if failure == "invalid_json":
            return f"{fallback} RunPod lieferte kein gültiges JSON."
        return fallback

    @staticmethod
    def _count(value: Any) -> int:
        return value if isinstance(value, int) and value >= 0 else 0

    @staticmethod
    def _count_or_none(value: Any) -> int | None:
        return value if isinstance(value, int) and value >= 0 else None

    @staticmethod
    def _safe_string(value: Any) -> str | None:
        if not isinstance(value, str):
            return None

        cleaned = value.strip()
        return cleaned[:500] if cleaned else None

    @classmethod
    def _safe_string_list(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []

        cleaned_values: list[str] = []

        for item in value:
            cleaned = cls._safe_string(item)

            if cleaned and cleaned not in cleaned_values:
                cleaned_values.append(cleaned)

        return cleaned_values
