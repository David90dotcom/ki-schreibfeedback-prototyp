from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI, OpenAIError


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE_PATH = PROJECT_ROOT / ".env"

EXPECTED_RESPONSE = "OpenAI-Verbindung erfolgreich"


def main() -> int:
    load_dotenv(dotenv_path=ENV_FILE_PATH, override=False)

    api_key = _get_optional_environment_value("OPENAI_API_KEY")
    model = _get_first_nonempty_environment_value(
        "OPENAI_DEFAULT_MODEL",
        "OPENAI_MODEL",
        default="gpt-5.6-luna",
    )

    if not api_key:
        print(
            "Fehler: OPENAI_API_KEY wurde nicht gefunden. "
            "Prüfe die lokale .env-Datei.",
            file=sys.stderr,
        )
        return 1

    client = OpenAI(
        api_key=api_key,
        timeout=60.0,
        max_retries=1,
    )

    print(f"Teste OpenAI-Modell: {model}")
    print("Der API-Key wird nicht ausgegeben.")
    print("Die Testantwort wird nicht dauerhaft gespeichert.")

    started_at = time.perf_counter()

    try:
        response = client.responses.create(
            model=model,
            instructions=(
                "Du bist ein technischer Verbindungstest. "
                "Befolge die Nutzereingabe exakt."
            ),
            input=(
                "Antworte ausschließlich mit: "
                "OpenAI-Verbindung erfolgreich"
            ),
            reasoning={
                "effort": "none",
            },
            max_output_tokens=30,
            store=False,
        )
    except OpenAIError as exc:
        print(
            f"OpenAI-Fehler ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            f"Unerwarteter Fehler ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 3

    duration_ms = round(
        (time.perf_counter() - started_at) * 1000,
        2,
    )

    output_text = response.output_text.strip()

    usage = (
        response.usage.model_dump(mode="json")
        if response.usage is not None
        else {}
    )

    print("\nAntwort:")
    print(output_text)

    print("\nMesswerte:")
    print(f"Angefragtes Modell: {model}")
    print(f"Tatsächliches Modell: {response.model}")
    print(f"Status: {response.status}")
    print(f"Laufzeit: {duration_ms} ms")
    print(f"Response-ID: {response.id}")
    print(f"Tokenwerte: {usage}")

    if output_text != EXPECTED_RESPONSE:
        print(
            "\nDie API-Verbindung war erfolgreich, aber die Antwort "
            "entspricht nicht exakt dem erwarteten Testtext.",
            file=sys.stderr,
        )
        return 4

    print("\nOpenAI-Verbindungstest erfolgreich.")
    return 0


def _get_optional_environment_value(
    variable_name: str,
) -> str | None:
    value = os.getenv(variable_name)

    if value is None:
        return None

    normalized_value = value.strip()
    return normalized_value or None


def _get_first_nonempty_environment_value(
    *variable_names: str,
    default: str,
) -> str:
    for variable_name in variable_names:
        value = os.getenv(variable_name)

        if value is not None and value.strip():
            return value.strip()

    return default


if __name__ == "__main__":
    raise SystemExit(main())
