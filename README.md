# KI-Schreibfeedback-Prototyp 0.2

Webapp-Prototyp zur vergleichenden Nutzung eines lokal betriebenen Sprachmodells über Ollama und eines cloudbasierten Modells über die OpenAI API.

## Funktionsumfang des 0.2-Cuts

- Eingabe eines anonymisierten, abgetippten Beispieltexts
- Auswahl zwischen Ollama und OpenAI im Browser
- konfigurierbares Standardmodell für beide Anbieter
- dynamisches Laden der lokal installierten Ollama-Modelle
- optionale Modell-ID für künftige oder nicht aufgelistete Modelle
- optional änderbare Ollama-API-Adresse
- Anzeige von Anbieter, tatsächlich verwendetem Modell und Gesamtdauer
- verständliche Fehler bei fehlendem OpenAI-Key oder nicht erreichbarem Ollama

Die bereits vorbereitete Registry-, Modellkatalog-, Metrik- und SQLite-Architektur bleibt als Grundlage für spätere Ausbaustufen erhalten.

## Installation unter Windows

```powershell
cd C:\Users\music\Documents\VisualStudioCodeProjects\ki-schreibfeedback-prototyp-0.2
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Trage anschließend ausschließlich in der lokalen `.env` deinen OpenAI-Key ein:

```env
OPENAI_API_KEY=dein_api_key
```

Die `.env` wird nicht in Git eingecheckt.

## Anwendung starten

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --reload
```

Danach im Browser öffnen: <http://127.0.0.1:8000>

Für lokale Analysen muss Ollama laufen. Die Standardadresse ist `http://localhost:11434`; sie kann unter „Erweiterte Ollama-Einstellungen“ für den aktuellen Aufruf geändert werden.

## Modelle im Browser auswählen

### Ollama

1. „Lokal: Ollama“ auswählen.
2. Optional die API-Adresse ändern.
3. „Verbindung prüfen / Modelle laden“ anklicken.
4. Ein installiertes Modell auswählen oder „Andere Modell-ID …“ verwenden.

### OpenAI

Das in `.env` konfigurierte Modell ist vorausgewählt. Bekannte Modelle können direkt gewählt werden. Über „Andere Modell-ID …“ lässt sich eine zukünftige Modell-ID eintragen.

Browserwerte gelten immer nur für den aktuellen Aufruf. Sie verändern weder `.env` noch den Modellkatalog. Der OpenAI-Key wird nicht an den Browser übertragen.

Die frei änderbare Ollama-Adresse ist für den lokalen Prototyp vorgesehen. Vor einer öffentlichen Bereitstellung wären mindestens Authentifizierung und eine Allowlist zulässiger Ollama-Server erforderlich.

## Tests

Die automatisierten Browserweg-Tests führen keine echten Modellanfragen aus:

```powershell
python -m unittest discover -s tests -v
python scripts\check_architecture.py
```
