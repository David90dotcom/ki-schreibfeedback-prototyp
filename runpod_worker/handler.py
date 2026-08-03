from __future__ import annotations

import runpod

from runpod_worker.worker import handler


if __name__ == "__main__":
    runpod.serverless.start(
        {
            "handler": handler,
        }
    )