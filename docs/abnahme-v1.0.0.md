# Abnahmeprotokoll – Version 1.0.0

## Ziel und Abgrenzung

Version 1.0.0 ist der eingefrorene Abschlussstand des
KI-Schreibfeedback-Prototyps. Das Artefakt ist eine Lern- und
Vergleichsplattform für lokale und cloudbasierte Sprachmodelle. Es unterstützt
die Untersuchung von Feedbackqualität, Laufzeit, Providerverhalten und
didaktischer Nutzbarkeit, ist aber keine autonome Benotungsplattform.

## Release-Stand

| Bereich | Festlegung |
|---|---|
| Hauptbranch | `main` |
| Release-Tag | `v1.0.0` |
| Vorgänger | `v1.0.0-rc4` |
| Web-Image | `ki-schreibfeedback-web:1.0.0` |
| OpenAI-Standard | `gpt-5.6-terra`, mittlerer Reasoning-Aufwand |
| Lokales Standardmodell | `mistral-small3.2:24b-instruct-2506-q8_0` |
| RunPod-Modell | `RedHatAI/Mistral-Small-3.2-24B-Instruct-2506-FP8` |
| RunPod-Betrieb | ein automatischer 48-GB-GPU-Pool, höchstens ein Worker |

Der annotierte Tag `v1.0.0` wird nach Veröffentlichung nicht verschoben. Eine
spätere Fehlerkorrektur erhält eine neue Patchversion, beispielsweise
`v1.0.1`.

## Abgenommener Funktionsumfang

- geschützter Prüferbereich mit lokaler oder cloudbasierter Modellwahl
- verwaltbare Aufgaben und geordnete Feedback-Kriterien
- kriterienweise Standardanalyse sowie optionale Vergleichsmodi
- technische Herkunftsprüfung wörtlicher Schülertextbelege
- formative Kriterienstatus ohne Benotungsanspruch
- gezielte Aktualisierung einzelner Kriterienkarten
- pseudonyme Schülerzugänge mit sechsstelligen Codes und zentraler Modellwahl
- Speicherung ausgewählter Feedbackläufe für die Meta-Ebene
- manuelle und optionale automatische Meta-Bewertung
- robuste Browser-Server-Synchronisierung langer automatischer Bewertungen
- JSON-Import und -Export vollständiger Feedback- und Meta-Datensätze
- CSV-Export numerischer Auswertungsdaten sowie PDF-Einzelexport
- produktives Docker-Compose-Deployment hinter Caddy mit HTTP/1.1 und HTTP/2

## Automatisierte Prüfung

Die vollständige Test-Suite und Architekturprüfung werden unmittelbar vor dem
Release mit lokaler Anwendungskonfiguration ausgeführt:

```powershell
$env:APP_MODE = "local"
& ".\.venv\Scripts\python.exe" -m pytest -q
& ".\.venv\Scripts\python.exe" scripts\check_architecture.py
& ".\.venv\Scripts\python.exe" -m json.tool runpod_worker\test_input.json > $null
git diff --check
```

Erwartetes Ergebnis des Release-Stands:

- 237 Tests bestanden
- 57 Subtests bestanden
- Architekturprüfung mit acht Prüfschritten erfolgreich
- vier Provider und acht Modelle validiert
- keine Modell-API während der automatisierten Prüfung aufgerufen
- JSON-Testeingabe des RunPod-Workers syntaktisch gültig

## Manuelle Freigabe

Vor der Kennzeichnung wurden Anmeldung, Navigation, Textanalyse,
Aufgabenverwaltung, Schüleransicht, gespeicherte Feedbacks und Meta-Bewertung
im Browser geprüft. Der zuvor beobachtete Fall, dass eine automatische
Meta-Bewertung serverseitig gespeichert wurde, während die Browseranzeige
weiter wartete, wird durch eine authentifizierte Statusabfrage erkannt. Die
fertige Bewertung wird anschließend automatisch geöffnet; ein erneuter
Modellaufruf ist nicht erforderlich.

Die produktive Instanz wird nach dem Tag anhand des
[Deployment-Ablaufs](deployment-v1.0.0.md) auf exakt diesen Stand gebracht.
Dabei werden SQLite-Daten vor dem Neubau gesichert, Containerzustand und
HTTPS-Erreichbarkeit geprüft und anschließend die zentralen Browserwege als
Smoke-Test ausgeführt.

## Bekannte Grenzen

Die technische Belegprüfung beweist nur die Herkunft eines Textausschnitts,
nicht die logische Richtigkeit jeder daraus abgeleiteten Modellbewertung.
Modellfeedback und automatische Meta-Vorbewertungen erfordern daher weiterhin
menschliche Prüfung. RunPod-Queue und Kaltstart hängen von externer
GPU-Verfügbarkeit ab. Die produktive Architektur ist als Einzelinstanz mit
SQLite ausgelegt und nicht horizontal skaliert. Weitere Einzelheiten stehen
in den [bekannten Einschränkungen](known-issues.md).

## Freigabeentscheidung

Der dokumentierte Funktionsumfang ist für den prototypischen Forschungszweck
vollständig. Zusätzliche Optimierungen an Prompts, Modellen oder Oberfläche
gehören nicht mehr zur Abnahme von Version 1.0.0. Notwendige Fehlerkorrekturen
werden nachvollziehbar als nachfolgende Patchversion veröffentlicht.
