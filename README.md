# KI-Schreibfeedback-Prototyp

Minimaler Webapp-Prototyp zur vergleichenden Nutzung eines lokal betriebenen Sprachmodells über Ollama und eines cloudbasierten Sprachmodells über die OpenAI API.

## Funktionen in Version 0.1

- Eingabe eines anonymisierten, abgetippten Beispieltextes
- Auswahl zwischen lokalem Modell und Cloud-Modell
- Generierung von lernförderlichem Schreibfeedback
- Anzeige von Anbieter, Modell und Antwortzeit
- Einfache serverseitige HTML-Oberfläche

## Installation

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt