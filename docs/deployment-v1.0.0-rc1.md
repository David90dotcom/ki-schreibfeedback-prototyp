# Deployment-Ablauf für Version 1.0.0-rc1

Dieser Ablauf verwendet ausschließlich den vorhandenen automatischen 48-GB-GPU-Pool. Dedizierte RTX-4090-, RTX-5090- und RTX-6000-Ada-Endpunkte werden nach dem Leeren ihrer Queues und dem Beenden aller Worker gelöscht. Damit verbleibt genau ein ergänzender RunPod-Providerweg.

## 1. Zielkonfiguration

| Bereich | Wert |
|---|---|
| Web-App-Branch | `release/1.0.0-rc1` |
| Web-App-Image | `ki-schreibfeedback-web:1.0.0-rc1` |
| RunPod-Worker | `runpod/worker-v1-vllm:v2.24.0` |
| Serverless-Modell | `RedHatAI/Mistral-Small-3.2-24B-Instruct-2506-FP8` |
| Lokales Ollama-Modell | `mistral-small3.2:24b-instruct-2506-q8_0` |
| Serverless-Endpoint | automatischer Pool kompatibler 48-GB-GPUs |
| Dedizierte Endpoints | nicht Bestandteil der endgültigen Anwendung |
| Sicherheitsgrenze | 15 Minuten Job-TTL, 10 Minuten Ausführung, 5 Sekunden Idle, höchstens ein Worker |
| Schüleransicht | `/schueler`, Prüferwahl aus freigegebenen Mistral-/OpenAI-Modellen, sechsstellige Einmalcodes |

Die FP8-Variante umfasst ungefähr 25,8 GB Modellgewichte. Das offizielle BF16-/FP16-Modell benötigt ungefähr 55 GB GPU-Speicher und passt deshalb nicht in die vorhandene Einzel-GPU-Strategie mit 32 beziehungsweise 48 GB. Die Quantisierungsform wird in den Versuchsdaten dokumentiert: lokal Q8 über Ollama, Serverless FP8 über vLLM.

## 2. Bestehende RunPod-Endpoints aktualisieren

Nur der automatische Pool behält seine ID. Sein Template verwendet:

```text
Container image: runpod/worker-v1-vllm:v2.24.0
MODEL_NAME: RedHatAI/Mistral-Small-3.2-24B-Instruct-2506-FP8
MAX_MODEL_LEN: 8192
GPU_MEMORY_UTILIZATION: 0.90
MAX_NUM_SEQS: 1
MAX_CONCURRENCY: 1
TOKENIZER_MODE: mistral
CONFIG_FORMAT: mistral
LOAD_FORMAT: mistral
RUNPOD_INIT_TIMEOUT: 800
VLLM_STARTUP_TIMEOUT: 1200
```

`OPENAI_SERVED_MODEL_NAME_OVERRIDE` und `QUANTIZATION` nicht setzen. Der von der Web-App gesendete Modellname entspricht bereits `MODEL_NAME`; das FP8-Format ist im Modell hinterlegt.

Weitere Endpoint-Einstellungen:

```text
Minimum workers: 0
Maximum workers: 1
Execution timeout: 600 Sekunden
Idle timeout: 5 Sekunden
FlashBoot: aktiviert, sofern verfügbar
Container disk: 60 GB
GPU-Pool: L40, L40S und RTX 6000 Ada mit jeweils 48 GB
```

Der automatische Pool erhöht die Chance, dass Prüfer trotz schwankender GPU-Supply einen Worker erhalten. Welche GPU einen Auftrag tatsächlich übernimmt, wird zusammen mit den Laufzeitwerten dokumentiert. Jeder App-Auftrag setzt zusätzlich `policy.ttl=900000` und `policy.executionTimeout=600000`. Für den Prüferzugang bleibt `Minimum workers = 0` und `Maximum workers = 1`; dadurch startet kein dauerhaft aktiver Worker und es kann zugleich niemals mehr als ein Worker abrechnen. Die Schüleransicht verwendet RunPod nicht.

## 3. RunPod vor der Umschaltung prüfen

1. `Maximum workers` unmittelbar vor dem beaufsichtigten Test von `0` auf `1` setzen.
2. Beim automatischen Pool unter „Requests“ den Inhalt von `runpod_worker/test_input.json` absenden.
3. Prüfen, dass der Job `COMPLETED` erreicht und unter `choices[0].message.content` ein JSON-Objekt mit dem Feld `feedback` zurückkommt.
4. Queue-Zeit, tatsächliche GPU, Ausführungszeit und Worker-Logs dokumentieren.
5. Nach dem Test die Queue kontrollieren; `Maximum workers = 1` bleibt für den Prüferzugang bestehen, `Minimum workers = 0` verhindert einen dauerhaft aktiven Worker.
6. Nach dem Idle Timeout prüfen, dass kein Worker `Running` oder `Initializing` bleibt.

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
sudo docker compose exec -T web python -c 'import sqlite3; source=sqlite3.connect("/app/data/analysis_runs.sqlite3"); target=sqlite3.connect("/app/data/analysis_runs-pre-1.0.0-rc1.sqlite3"); source.backup(target); target.close(); source.close()'
```

Anschließend in der serverseitigen `.env` ausschließlich die ID des Pools eintragen:

```env
RUNPOD_ENDPOINT_ID=<Endpoint-ID des automatischen Pools>
RUNPOD_DEFAULT_MODEL=RedHatAI/Mistral-Small-3.2-24B-Instruct-2506-FP8
RUNPOD_JOB_TIMEOUT_SECONDS=900
RUNPOD_IDLE_TIMEOUT_SECONDS=5
STUDENT_FEEDBACK_PROVIDER=mistral
```

`STUDENT_FEEDBACK_PROVIDER` legt nur die Erstkonfiguration fest. Eine spätere
Prüferauswahl von Provider und Modell wird persistent in SQLite gespeichert.
API-Key, Passwort-Hash und Sitzungs-Secret bleiben unverändert. Die `.env` darf
weder angezeigt noch eingecheckt werden.

## 5. Webanwendung aktualisieren

Zuerst die Compose-Datei ohne Ausgabe der Secrets validieren, dann das Web-Image neu bauen und die Dienste aktualisieren:

```bash
sudo docker compose config --quiet
sudo docker compose build --pull web
sudo docker compose up -d --remove-orphans
sudo docker compose ps
sudo docker compose logs --tail=100 web caddy
```

Der Login-Endpunkt muss über HTTPS erreichbar sein:

```bash
curl --fail --silent --show-error --output /dev/null https://llm-lernlabor.de/login
```

Der Reverse-Proxy akzeptiert für HTTPS ausschließlich HTTP/1.1 und HTTP/2.
HTTP/3 bleibt bewusst deaktiviert, weil die in einem Test beobachtete
QUIC-Verbindung einen noch aktiven, länger als 60 Sekunden wartenden
RunPod-Aufruf mit `504 timeout: no recent network activity` vom Browser
getrennt hat. Die Modellanfrage lief dabei bei RunPod weiter. Die
TCP-basierte HTTP/2-Verbindung vermeidet diesen verwaisten Browserzustand.
Nach einer Änderung muss die aktive Caddy-Konfiguration validiert werden:

```bash
sudo docker compose exec -T caddy \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
curl --http2 --silent --show-error --output /dev/null \
  --write-out 'HTTP-Version: %{http_version}\n' \
  https://llm-lernlabor.de/login
```

## 6. Ende-zu-Ende-Abnahme

In der produktiven Oberfläche nacheinander prüfen:

1. Anmeldung und Abmeldung.
2. Standard-Kriterienvorlage ist vorausgewählt.
3. OpenAI- und Mistral-Cloudauswahl werden angezeigt.
4. RunPod zeigt im Standardmodus automatisch die aktuelle Supply-Momentaufnahme des GPU-Pools.
5. RunPod Standard startet die kriterienweise Analyse über den automatischen Pool.
6. Auch im Experimentmodus wird keine dedizierte GPU-Auswahl angeboten.
7. Alle Kriterienkarten erscheinen; ein unsicherer Einzelbefund wird höchstens als „Nicht beurteilbar“ ersetzt und bricht nicht den gesamten Lauf ab.
8. Eine einzelne Kriterienkarte lässt sich aktualisieren.
9. Feedbacklauf lässt sich für die Meta-Bewertung speichern.
10. Manuelle Bewertung und PDF-Export funktionieren.
11. Automatische Meta-Vorbewertung wird nur nach dem ausdrücklichen Cloud-Klick gestartet.
12. Daten und Standardvorlage bleiben nach einem Container-Neustart erhalten.
13. Unter „Schülerzugänge“ lässt sich ein pseudonymes Konto erstellen; der sechsstellige Code erscheint genau einmal.
14. Der Code öffnet `/schueler`, dort werden nur aktive Vorlagen, Texteingabe und Schülerfeedback angezeigt.
15. Unter „Schülerzugänge“ lässt sich eine konfigurierte Mistral- oder OpenAI-Modellvariante als zentrale Schülerkonfiguration speichern.
16. Eine Schüleranalyse verwendet exakt diese Prüferauswahl; Provider-, Modell- und Forschungsoptionen fehlen in der Schüleransicht vollständig.
17. Der JSON-Export der Meta-Bewertungen lässt sich in einer zweiten Installation importieren, ohne vorhandene Datensätze zu überschreiben.
18. Der CSV-Export enthält Laufzeiten und numerische Kriterienwerte, aber keine Schülertexte, Feedbacktexte oder Begründungen.
17. Nach der Deaktivierung verliert auch eine bereits angemeldete Schülersitzung bei der nächsten Anfrage den Zugriff.

Zusätzlich Modellname, Gesamtdauer, Queue-Zeit und Ausführungszeit für die Bachelorarbeit notieren.

## 7. Rückkehrweg

Falls das neue Serverless-Modell nicht stabil läuft, bleibt die neue Web-App bestehen. In der DigitalOcean-`.env` werden lediglich die vorherige Endpoint-ID und der dazu passende frühere Modellname wiederhergestellt. Danach genügt:

```bash
sudo docker compose up -d --force-recreate web
sudo docker compose logs --tail=100 web
```

Die SQLite-Sicherung wird nur benötigt, falls unabhängig vom Modellwechsel ein Datenbankproblem festgestellt wird. Sie wird nicht vorsorglich über die aktuelle Datenbank kopiert.
