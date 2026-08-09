# Abnahmeprotokoll – Version 0.6.0

## Freigabegegenstand

Version 0.6.0 ist der neue stabile Standard des KI-Schreibfeedback-Prototyps. Sie erweitert den produktiven Stand 0.5.0 um vier auswählbare RunPod-Inferenzziele, sichere Markdown-Darstellung, nachvollziehbare Worker- und Jobzustände, getrennte Zeitmessung sowie den gezielten Einzelabbruch wartender oder laufender Jobs.

| Merkmal | Abgenommener Stand |
|---|---|
| Datum | 9. August 2026 |
| Funktional geprüfter Commit | `c3f9c85` |
| Release-Branch | `fix/0.6-clean-readiness-gui` |
| Zielbranch | `main` |
| Release-Tag | `v0.6.0` |
| Vorgänger | `v0.5.0` |
| Produktionspfad | `/home/deploy/ki-schreibfeedback-prototyp` |
| Webanwendung | FastAPI im Docker-Container |
| HTTPS und Reverse Proxy | Caddy 2.11.4 |
| Modellbetrieb | RunPod Serverless mit vLLM |
| Modell | `mistralai/Ministral-3-14B-Instruct-2512` |

Der funktional geprüfte Commit enthält den vollständigen Anwendungscode. Der nachgelagerte Release-Commit verändert nur diese Dokumentation und die Versionskennzeichnung im README.

## Automatisierte Verifikation

| Prüfschritt | Ergebnis |
|---|---|
| Pytest-Testlauf | 69 Tests und 8 Subtests erfolgreich |
| Python-Kompilierung | erfolgreich |
| JavaScript-Syntaxprüfung | erfolgreich |
| Git-Diff-Prüfung | erfolgreich |
| Externe Modellaufrufe während der Tests | keine |

Die Regressionstests decken insbesondere Authentifizierung, Sitzungen, CSRF-Schutz, Produktionsbeschränkungen, RunPod-Client, Status-Fallbacks, individuelle Jobzustände, persistente technische Jobregistrierung und den gezielten Einzelabbruch ab.

## Manuelle Ende-zu-Ende-Abnahme

Die folgenden Abläufe wurden am 9. August 2026 erfolgreich im Browser gegen echte RunPod-Endpunkte geprüft:

1. Die Betriebsbereitschaft zeigt den Zeitpunkt und die aggregierten Workerzahlen ohne eine falsche Zuordnung zum gerade abgesendeten Job.
2. Ein Queue-Job bleibt orange; Grün wird erst bei tatsächlicher Verarbeitung angezeigt.
3. Die Verwaltung aktiver Anfragen ersetzt den Ladeplatzhalter zuverlässig durch lokal registrierte Jobs.
4. Eine registrierte Request-ID wird mit ihrem letzten Status und einem gezielten Abbruchknopf angezeigt.
5. Eine ältere Request-ID kann manuell und ohne Beenden des Workers abgebrochen werden.
6. Eine reale Anfrage über das Inferenzziel „RTX 6000 Ada – 48 GB“ wurde vollständig verarbeitet.
7. Das Ergebnis zeigt Anbieter, Modell, GPU-Endpunkt, Gesamtzeit, Warte-/Bereitstellungszeit, KI-Verarbeitungszeit, RunPod-Job-ID und Worker-ID.
8. Das erzeugte Schreibfeedback wurde vollständig und sicher formatiert dargestellt.

## Release- und Rollback-Regel

Der annotierte Tag `v0.6.0` wird nach der Freigabe nicht verschoben oder überschrieben. Jede Entwicklung für Version 0.7 beginnt von diesem Tag. Dadurch bleibt Version 0.6.0 unabhängig vom späteren Stand von `main` reproduzierbar.

Für ein Deployment oder einen Rollback auf dem DigitalOcean-Server wird ausschließlich der Tag verwendet:

```bash
cd /home/deploy/ki-schreibfeedback-prototyp
git fetch --tags origin
git switch --detach v0.6.0
docker compose build --pull web
docker compose up -d --remove-orphans
docker compose ps
```

Die serverseitige `.env` und die Docker-Volumes werden dabei nicht ersetzt. Vor einem späteren Versionswechsel ist die vorhandene `.env` separat zu sichern, ohne ihren Inhalt in Terminalausgaben oder Git zu übernehmen.

## Abnahmeentscheidung

Die funktionalen, sicherheitsbezogenen und betrieblichen Anforderungen an Version 0.6.0 sind erfüllt. Dieser Stand ist als neuer stabiler Standard freigegeben. Bei Fehlern in Version 0.7 erfolgt der Rückweg ausschließlich über den unveränderlichen Tag `v0.6.0`.

Die verbleibenden externen Einschränkungen des RunPod-Cold-Starts und der nachträglichen Auflistung fremder Queue-Jobs sind in [known-issues.md](known-issues.md) dokumentiert und durch transparente Zustände beziehungsweise den manuellen Einzelabbruch behandelt.
