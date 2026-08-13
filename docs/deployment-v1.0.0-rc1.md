# Deployment-Ablauf für Version 1.0.0-rc1

Dieser Ablauf stellt zuerst einen neuen RunPod-Endpoint bereit und schaltet erst danach die Webanwendung auf DigitalOcean um. Der bisherige Endpoint bleibt bis zum erfolgreichen Ende-zu-Ende-Test unverändert und dient als schneller Rückkehrweg.

## 1. Zielkonfiguration

| Bereich | Wert |
|---|---|
| Web-App-Branch | `release/1.0.0-rc1` |
| Web-App-Image | `ki-schreibfeedback-web:1.0.0-rc1` |
| RunPod-Worker | `runpod/worker-v1-vllm:v2.24.0` |
| Serverless-Modell | `RedHatAI/Mistral-Small-3.2-24B-Instruct-2506-FP8` |
| Lokales Ollama-Modell | `mistral-small3.2:24b-instruct-2506-q8_0` |
| Bevorzugte Serverless-GPU | 48 GB VRAM; RTX 5090 mit 32 GB erst nach erfolgreichem Standardtest |
| Nicht geeignet | RTX 4090 mit 24 GB VRAM |

Die FP8-Variante umfasst ungefähr 25,8 GB Modellgewichte. Das offizielle BF16-/FP16-Modell benötigt ungefähr 55 GB GPU-Speicher und passt deshalb nicht in die vorhandene Einzel-GPU-Strategie mit 32 beziehungsweise 48 GB. Die Quantisierungsform wird in den Versuchsdaten dokumentiert: lokal Q8 über Ollama, Serverless FP8 über vLLM.

## 2. Neuen RunPod-Endpoint anlegen

Den bestehenden funktionierenden Endpoint nicht überschreiben. In RunPod einen neuen vLLM-Serverless-Endpoint mit folgenden Einstellungen anlegen:

```text
Container image: runpod/worker-v1-vllm:v2.24.0
MODEL_NAME: RedHatAI/Mistral-Small-3.2-24B-Instruct-2506-FP8
OPENAI_SERVED_MODEL_NAME_OVERRIDE: RedHatAI/Mistral-Small-3.2-24B-Instruct-2506-FP8
MAX_MODEL_LEN: 8192
GPU_MEMORY_UTILIZATION: 0.95
MAX_NUM_SEQS: 1
MAX_CONCURRENCY: 1
TOKENIZER_MODE: mistral
CONFIG_FORMAT: mistral
LOAD_FORMAT: mistral
```

`QUANTIZATION` nicht setzen. Das Modell bringt seine FP8-/Compressed-Tensors-Konfiguration selbst mit. Für den textbasierten Feedbackbetrieb sind Tool- und Bildoptionen nicht erforderlich.

Weitere Endpoint-Einstellungen:

```text
Minimum workers: 0
Maximum workers: 1
Execution timeout: 600 Sekunden
Idle timeout: 3600 Sekunden
FlashBoot: aktiviert, sofern verfügbar
GPU-Pool: L40, L40S oder RTX 6000 Ada mit jeweils 48 GB
```

Für wiederholbare Versuche sollte ein ausreichend großes Netzlaufwerk beziehungsweise ein Modellcache verwendet werden. Wegen der Modellgröße sind mindestens 40 GB freier Cache-Speicher, mit Reserve besser 50 bis 60 GB, vorzusehen.

## 3. RunPod vor der Umschaltung prüfen

1. Den neuen Endpoint starten.
2. In RunPod unter „Requests“ den Inhalt von `runpod_worker/test_input.json` absenden.
3. Prüfen, dass der Job `COMPLETED` erreicht und unter `choices[0].message.content` ein JSON-Objekt mit dem Feld `feedback` zurückkommt.
4. Endpoint-ID notieren.
5. Worker-Logs auf Speicherfehler, Modellabbruch und wiederholte Neustarts kontrollieren.

Erst nach diesem Test wird DigitalOcean umgestellt. Schlägt bereits der Start mit `CUDA out of memory` fehl, zunächst einen 48-GB-Pool verwenden und `MAX_MODEL_LEN` nicht erhöhen.

## 4. DigitalOcean vorbereiten

Auf dem Server in das Projektverzeichnis wechseln und den Release-Branch laden:

```bash
git fetch origin
git switch release/1.0.0-rc1
git pull --ff-only
git status --short
```

`git status --short` muss leer bleiben. Vor dem Container-Neubau eine SQLite-Sicherung innerhalb des persistenten Volumes anlegen:

```bash
docker compose exec -T web python -c 'import sqlite3; source=sqlite3.connect("/app/data/analysis_runs.sqlite3"); target=sqlite3.connect("/app/data/analysis_runs-pre-1.0.0-rc1.sqlite3"); source.backup(target); target.close(); source.close()'
```

Anschließend in der serverseitigen `.env` ausschließlich die Werte des neuen Endpoints eintragen:

```env
RUNPOD_ENDPOINT_ID=<neue Endpoint-ID>
RUNPOD_ENDPOINT_RTX4090_ID=
RUNPOD_DEFAULT_MODEL=RedHatAI/Mistral-Small-3.2-24B-Instruct-2506-FP8
RUNPOD_JOB_TIMEOUT_SECONDS=1200
RUNPOD_IDLE_TIMEOUT_SECONDS=3600
```

API-Key, Passwort-Hash und Sitzungs-Secret bleiben unverändert. Die `.env` darf weder angezeigt noch eingecheckt werden.

## 5. Webanwendung aktualisieren

Zuerst die Compose-Datei ohne Ausgabe der Secrets validieren, dann das Web-Image neu bauen und die Dienste aktualisieren:

```bash
docker compose config --quiet
docker compose build --pull web
docker compose up -d --remove-orphans
docker compose ps
docker compose logs --tail=100 web caddy
```

Der Login-Endpunkt muss über HTTPS erreichbar sein:

```bash
curl --fail --silent --show-error --output /dev/null https://llm-lernlabor.de/login
```

## 6. Ende-zu-Ende-Abnahme

In der produktiven Oberfläche nacheinander prüfen:

1. Anmeldung und Abmeldung.
2. Standard-Kriterienvorlage ist vorausgewählt.
3. OpenAI- und Mistral-Cloudauswahl werden angezeigt.
4. RunPod Standard startet die kriterienweise Analyse.
5. Alle Kriterienkarten erscheinen; ein unsicherer Einzelbefund wird höchstens als „Nicht beurteilbar“ ersetzt und bricht nicht den gesamten Lauf ab.
6. Eine einzelne Kriterienkarte lässt sich aktualisieren.
7. Feedbacklauf lässt sich für die Meta-Bewertung speichern.
8. Manuelle Bewertung und PDF-Export funktionieren.
9. Automatische Meta-Vorbewertung wird nur nach dem ausdrücklichen Cloud-Klick gestartet.
10. Daten und Standardvorlage bleiben nach einem Container-Neustart erhalten.

Zusätzlich Modellname, Gesamtdauer, Queue-Zeit und Ausführungszeit für die Bachelorarbeit notieren.

## 7. Rückkehrweg

Falls das neue Serverless-Modell nicht stabil läuft, bleibt die neue Web-App bestehen. In der DigitalOcean-`.env` werden lediglich die vorherige Endpoint-ID und der dazu passende frühere Modellname wiederhergestellt. Danach genügt:

```bash
docker compose up -d --force-recreate web
docker compose logs --tail=100 web
```

Die SQLite-Sicherung wird nur benötigt, falls unabhängig vom Modellwechsel ein Datenbankproblem festgestellt wird. Sie wird nicht vorsorglich über die aktuelle Datenbank kopiert.
