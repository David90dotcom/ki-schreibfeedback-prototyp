# RunPod-Transparenz in Version 0.6

## Ziel

Die Web-App soll Prüfern verständlich zeigen, ob für das ausgewählte RunPod-Ziel ein Warmstart erwartet wird, ob ein Worker gerade initialisiert oder fehlschlägt und welcher Anteil der Gesamtdauer auf Bereitstellung beziehungsweise Modellverarbeitung entfällt. Die Anzeige löst keine zusätzliche Inferenz aus.

## Angezeigte Ebenen

1. **Allgemeine GPU-Verfügbarkeit:** Die RunPod-Katalog-API liefert `HIGH`, `MEDIUM`, `LOW` oder `NONE`. Diese Angabe ist eine Momentaufnahme und keine Startgarantie.
2. **Konkreter Endpointzustand:** Die Queue-Health-API liefert unter anderem freie, startende, laufende, gedrosselte und fehlerhafte Worker sowie wartende und laufende Jobs.
3. **Messwerte des abgeschlossenen Jobs:** `delayTime` beschreibt die Warte-/Bereitstellungszeit; `executionTime` beschreibt die eigentliche Worker-Ausführung. Die App misst zusätzlich ihre Gesamtdauer.

Während einer RunPod-Anfrage sendet der Browser das Analyseformular asynchron ab und fragt parallel alle drei Sekunden den geschützten Statusendpunkt der Web-App ab. Dadurch bleiben Zeitmesser und Zustandsmeldung auch während eines langen Cold Starts sichtbar. Es wird keine Prozentzahl angezeigt, weil RunPod keinen belastbaren prozentualen Fortschritt liefert.

Die Health-Daten gelten immer aggregiert für den gesamten Endpoint. Insbesondere bedeutet ein `running`-Worker nur, dass der Worker-Container läuft. Erst ein Jobstatus `IN_PROGRESS` bedeutet, dass ein Job von einem Worker aufgenommen wurde. Solange gleichzeitig Jobs unter `inQueue` gemeldet werden, behauptet die Oberfläche deshalb keine Zuordnung des laufenden Workers zum Browserauftrag.

## Warmhaltefenster

- RunPod-`Idle timeout`: `3600 s` bei jedem verwendeten Endpoint
- Web-App-Dokumentationswert: `RUNPOD_IDLE_TIMEOUT_SECONDS=3600`
- Web-App-Joblimit: `RUNPOD_JOB_TIMEOUT_SECONDS=1200`
- RunPod-`Execution timeout`: weiterhin `600 s`

Nach einer erfolgreichen Anfrage berechnet die Web-App aus Abschlusszeit plus Idle-Timeout eine ungefähre Warmhalteobergrenze. Die Formulierungen „voraussichtlich“ und „etwa“ sind absichtlich: Ein Worker kann wegen Absturz, Releasewechsel oder Infrastrukturmaßnahmen früher verschwinden. Der aktuelle Health-Status hat deshalb Vorrang vor der Zeitschätzung.

## Berechtigungen und Datenschutz

Alle RunPod-Aufrufe erfolgen serverseitig. API-Key und Endpoint-IDs werden nicht an den Browser übertragen.

- Queue-Health: erforderlich für den sichtbaren Worker-/Jobstatus
- GPU-Katalog: erforderlich für Supply
- Serverless-Worker-Lesezugriff: erforderlich für GPU-Typ, Worker-ID, Release, Rechenzentrum und Laufzeit

Antwortet eine Management-API mit `403`, bleiben Analyse und Health-Status nutzbar. Die betroffene Zusatzinformation erscheint als „Nicht abrufbar“. Eine tatsächlich verwendete GPU wird nur dann behauptet, wenn RunPod eine Job-zu-Worker-ID liefert und dieser Worker in den technischen Daten eindeutig gefunden wird. Sonst listet die Anwendung lediglich die aktuell aktiven Worker auf.

## Akzeptanzkriterien

- Statusroute ist nur nach Anmeldung erreichbar.
- Browserwerte werden ausschließlich über die feste Endpoint-Allowlist aufgelöst.
- API-Key und Endpoint-IDs erscheinen weder in HTML noch in JSON.
- Supply- und Workerfehler beeinträchtigen die Modellanfrage nicht.
- Live-Anzeige verwendet echte aggregierte Endpointzustände plus Zeitmesser, keine erfundene Prozentzahl oder Jobzuordnung.
- Ergebnis trennt Gesamt-, Warte-/Bereitstellungs- und KI-Verarbeitungszeit.
- Warmhalteangabe enthält ausdrücklich keine Reservierungsgarantie.
- Technische Details sind eingeklappt und bleiben bei fehlender Berechtigung ehrlich unvollständig.
