from __future__ import annotations

import json
import unittest

import httpx

from runpod_worker.worker import (
    WorkerSettings,
    process_job,
)


class RunPodWorkerTests(unittest.TestCase):
    MODEL_NAME = (
        "mistral-small3.2:"
        "24b-instruct-2506-q8_0"
    )

    def setUp(self) -> None:
        self.settings = WorkerSettings(
            model_name=self.MODEL_NAME,
            ollama_base_url=(
                "http://ollama.test"
            ),
            request_timeout_seconds=30.0,
            max_prompt_chars=1_000,
        )

    def test_process_job_forwards_input_and_maps_output(
        self,
    ) -> None:
        recorded: list[httpx.Request] = []

        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            recorded.append(request)

            return httpx.Response(
                200,
                json={
                    "model": self.MODEL_NAME,
                    "response": (
                        "Simuliertes Schreibfeedback"
                    ),
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 23,
                    "eval_count": 11,
                    "total_duration": 900_000_000,
                    "context": [1, 2, 3],
                },
            )

        with httpx.Client(
            base_url=(
                self.settings.ollama_base_url
            ),
            transport=httpx.MockTransport(
                handler
            ),
        ) as client:
            result = process_job(
                {
                    "id": "job-123",
                    "input": {
                        "model": self.MODEL_NAME,
                        "prompt": (
                            "Ein kurzer Beispieltext."
                        ),
                        "system": (
                            "Gib lernförderliches "
                            "Feedback."
                        ),
                        "stream": False,
                        "options": {
                            "temperature": 0.15,
                            "num_predict": 4000,
                            "seed": 7,
                        },
                        "format": {
                            "type": "object",
                            "properties": {
                                "feedback": {
                                    "type": "string",
                                },
                            },
                        },
                    },
                },
                settings=self.settings,
                client=client,
            )

        self.assertEqual(len(recorded), 1)
        self.assertEqual(
            recorded[0].url.path,
            "/api/generate",
        )

        payload = json.loads(
            recorded[0].content
        )

        self.assertEqual(
            payload["model"],
            self.MODEL_NAME,
        )
        self.assertEqual(
            payload["prompt"],
            "Ein kurzer Beispieltext.",
        )
        self.assertEqual(
            payload["system"],
            "Gib lernförderliches Feedback.",
        )
        self.assertFalse(payload["stream"])
        self.assertEqual(
            payload["keep_alive"],
            -1,
        )
        self.assertEqual(
            payload["options"]["seed"],
            7,
        )
        self.assertEqual(
            payload["format"]["type"],
            "object",
        )

        self.assertEqual(
            result["response"],
            "Simuliertes Schreibfeedback",
        )
        self.assertEqual(
            result["model"],
            self.MODEL_NAME,
        )
        self.assertEqual(
            result["prompt_eval_count"],
            23,
        )
        self.assertEqual(
            result["eval_count"],
            11,
        )
        self.assertNotIn(
            "context",
            result,
        )

    def test_other_model_is_rejected_without_request(
        self,
    ) -> None:
        calls = 0

        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            nonlocal calls
            calls += 1

            return httpx.Response(
                200,
                json={
                    "response": "nicht erwartet",
                },
            )

        with httpx.Client(
            base_url=(
                self.settings.ollama_base_url
            ),
            transport=httpx.MockTransport(
                handler
            ),
        ) as client:
            with self.assertRaisesRegex(
                ValueError,
                (
                    "ausschließlich das "
                    "konfigurierte Modell"
                ),
            ):
                process_job(
                    {
                        "input": {
                            "model": (
                                "anderes-modell"
                            ),
                            "prompt": "Test",
                            "stream": False,
                        },
                    },
                    settings=self.settings,
                    client=client,
                )

        self.assertEqual(calls, 0)

    def test_empty_prompt_is_rejected(
        self,
    ) -> None:
        with httpx.Client(
            base_url=(
                self.settings.ollama_base_url
            ),
            trust_env=False,
        ) as client:
            with self.assertRaisesRegex(
                ValueError,
                "nichtleerer Text",
            ):
                process_job(
                    {
                        "input": {
                            "model": (
                                self.MODEL_NAME
                            ),
                            "prompt": "   ",
                            "stream": False,
                        },
                    },
                    settings=self.settings,
                    client=client,
                )

    def test_ollama_http_error_becomes_worker_error(
        self,
    ) -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                500,
                json={
                    "error": "simuliert",
                },
            )

        with httpx.Client(
            base_url=(
                self.settings.ollama_base_url
            ),
            transport=httpx.MockTransport(
                handler
            ),
        ) as client:
            with self.assertRaisesRegex(
                RuntimeError,
                "HTTP-Status 500",
            ):
                process_job(
                    {
                        "input": {
                            "model": (
                                self.MODEL_NAME
                            ),
                            "prompt": "Test",
                            "stream": False,
                        },
                    },
                    settings=self.settings,
                    client=client,
                )


if __name__ == "__main__":
    unittest.main()
