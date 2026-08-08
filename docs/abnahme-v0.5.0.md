# Abnahmeprotokoll – Version 0.5.0

## Freigabegegenstand

Version 0.5.0 des KI-Schreibfeedback-Prototyps umfasst die geschützte Produktivbereitstellung der FastAPI-Webanwendung auf DigitalOcean und die Modellinferenz über einen RunPod-Serverless-Endpoint mit vLLM.

| Merkmal | Abgenommener Stand |
|---|---|
| Datum | 8. August 2026 |
| Funktional geprüfter Commit | `ef6748a` |
| Release-Branch | `feature/0.5-deployment` |
| Zielbranch | `main` |
| Release-Tag | `v0.5.0` |
| Webanwendung | FastAPI im Docker-Container |
| HTTPS und Reverse Proxy | Caddy 2.11.4 |
| Modellbetrieb | RunPod Serverless mit vLLM |
| Modell | `mistralai/Ministral-3-14B-Instruct-2512` |

Die nach dem funktional geprüften Commit ergänzten Release-Dateien verändern ausschließlich die Dokumentation.

## Automatisierte Verifikation

| Prüfschritt | Ergebnis |
|---|---|
| Pytest-Testlauf | 38 Tests und 3 Subtests erfolgreich |
| Architekturprüfung | erfolgreich; keine Modell-API aufgerufen |
| Validierung der RunPod-Testeingabe | gültiges JSON |
| Regressionsschwerpunkte | Authentifizierung, Sitzung, CSRF, Login-Begrenzung, Provider-Auswahl, Produktionsbeschränkungen, RunPod-Client und Worker |

Eine Deprecation-Warnung aus der Kombination von Starlette-TestClient und `httpx` ist bekannt. Sie beeinflusst die Funktion oder Abnahme von Version 0.5.0 nicht und kann bei einer späteren Aktualisierung der Testabhängigkeiten behoben werden.

## Produktive Ende-zu-Ende-Abnahme

Folgende Prüfschritte wurden auf der öffentlich erreichbaren Produktionsumgebung erfolgreich durchgeführt:

1. Die Hauptdomain liefert die Loginseite per HTTPS mit HTTP 200.
2. Die `www`-Subdomain leitet permanent auf die Hauptdomain weiter.
3. Caddy akzeptiert die produktive Konfiguration.
4. Web- und Caddy-Container laufen; der Webcontainer meldet `healthy`.
5. Der Argon2-Passworthash und die RunPod-Modell-ID kommen unverändert im Webcontainer an.
6. Anmeldung und geschützte Startseite funktionieren im Browser.
7. Der lokale Ollama-Provider ist im Produktionsmodus nicht verfügbar.
8. Eine reale Texteingabe wird über die Webanwendung an RunPod übertragen.
9. RunPod verarbeitet den Auftrag mit vLLM und liefert vollständiges Schreibfeedback an die Webanwendung zurück.
10. Die Webanwendung zeigt Anbieter, tatsächlich verwendetes Modell und Gesamtdauer an.
11. Ein Scale-to-zero-Kaltstart wurde über eine neue Worker-Instanz und eine anschließend erfolgreich abgeschlossene Webanfrage nachgewiesen.

Ein erfolgreicher Ende-zu-Ende-Lauf benötigte in der Webanwendung 49,803 Sekunden. RunPod wies dafür 0,96 Sekunden Queue-Zeit und 47,85 Sekunden Ausführungszeit aus. Dieser Messwert dokumentiert einen erfolgreichen Lauf; die Kaltstartprüfung wurde separat anhand des neu gestarteten Workers nachgewiesen.

## Abnahmeentscheidung

Die für Version 0.5.0 vorgesehenen funktionalen, sicherheitsbezogenen und betrieblichen Anforderungen sind erfüllt. Der Stand ist für die Zusammenführung in `main` und die Kennzeichnung mit `v0.5.0` freigegeben.

Die rohe Anzeige von Markdown-Steuerzeichen in Modellantworten ist als nicht blockierende UI-Abweichung akzeptiert und in [known-issues.md](known-issues.md) dokumentiert.

## Nachgelagerte Betriebsprüfung

Nach einem kontrollierten Neustart des DigitalOcean-Servers ist ein kurzer Smoke-Test auszuführen:

- Docker-Dienst aktiv;
- Web- und Caddy-Container automatisch gestartet;
- Webcontainer `healthy`;
- Loginseite über HTTPS erreichbar;
- Anmeldung und eine einzelne RunPod-Anfrage erfolgreich.

Diese Prüfung bestätigt das Wiederanlaufverhalten, verändert aber nicht die funktionale Abnahme des Release-Stands.
