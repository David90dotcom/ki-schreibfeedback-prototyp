# RunPod-Transparenz in Version 0.6

## Ziel

Die Web-App soll Prüfern verständlich zeigen, ob für das ausgewählte RunPod-Ziel Worker-Kapazität vorhanden ist, ob ein Worker gerade initialisiert oder fehlschlägt und welcher Anteil der Gesamtdauer auf Bereitstellung beziehungsweise Modellverarbeitung entfällt. Die Anzeige löst keine zusätzliche Inferenz aus.

## Angezeigte Ebenen

1. **Kompakter Endpointzustand:** Die Oberfläche zeigt Worker-Kapazität, Zeitpunkt und die aggregierten Anzahlen für idle, bereit, laufend, startend, gedrosselt und fehlerhaft.
2. **Individueller Auftragszustand:** Nach der Rückgabe der Job-ID wird der konkrete Browserauftrag als `IN_QUEUE`, `IN_PROGRESS` beziehungsweise `RUNNING` verfolgt. Die Ladeanzeige wechselt erst bei bestätigter Verarbeitung von Orange auf Grün.
3. **Messwerte des abgeschlossenen Jobs:** `delayTime` beschreibt die Warte-/Bereitstellungszeit; `executionTime` beschreibt die eigentliche Worker-Ausführung. Die App misst zusätzlich ihre Gesamtdauer.

Supply-, Warmhalte- und technische Verwaltungsdaten werden weiterhin serverseitig erhoben, in Version 0.6 aber vorübergehend nicht in der regulären GUI dargestellt. Fehler dieser Zusatzabfragen erzeugen deshalb keine langen Diagnoseblöcke in der Oberfläche.

Während einer RunPod-Anfrage sendet der Browser das Analyseformular asynchron ab und fragt parallel alle drei Sekunden die geschützten Endpoint- und Jobstatusrouten der Web-App ab. Dadurch bleiben Zeitmesser, individuelle Zustandsmeldung und Abbruchknopf auch während eines langen Cold Starts verfügbar. Es wird keine Prozentzahl angezeigt, weil RunPod keinen belastbaren prozentualen Fortschritt liefert.

Die Health-Daten gelten immer aggregiert für den gesamten Endpoint. Insbesondere bedeutet ein `running`-Worker nur, dass der Worker-Container läuft. Erst der individuelle Jobstatus `IN_PROGRESS` beziehungsweise `RUNNING` bedeutet, dass genau dieser Auftrag von einem Worker aufgenommen wurde. `IN_QUEUE` bleibt auch bei einem bereits laufenden Worker eine wartende Anfrage.

## Auftragsverwaltung und Einzelabbruch

Sobald RunPod nach `/run` eine Job-ID zurückgibt, speichert die Web-App ausschließlich folgende technische Zuordnung in der bereits persistent eingebundenen SQLite-Datei:

- zufällige Tracking-ID des Browserlaufs,
- RunPod-Job-ID,
- interner Endpoint-Schlüssel und Endpoint-ID,
- Status sowie Erstellungs- und Aktualisierungszeit.

Schülertext, Prompt und API-Key werden nicht in dieser Jobtabelle gespeichert. Aktive, von der Web-App registrierte Jobs bleiben dadurch auch nach einem Browser- oder Serverneustart auffindbar. Die Oberfläche kann jeden Eintrag einzeln über RunPods dokumentierten Endpunkt `POST /cancel/{job_id}` abbrechen. Der Worker-Container selbst wird dabei nicht gelöscht.

RunPods öffentliche Queue-API bietet keine Operation zum Auflisten sämtlicher einzelner Job-IDs. Für Aufträge, die vor dieser Funktion oder direkt in der RunPod-Konsole gestartet wurden, enthält die eingeklappte Verwaltung daher ein manuelles Request-ID-Feld. Ein pauschaler `/purge-queue`-Knopf ist absichtlich nicht vorhanden, weil er alle wartenden Jobs des ausgewählten Endpoints entfernen würde.

## Warmhaltefenster

- RunPod-`Idle timeout`: `3600 s` bei jedem verwendeten Endpoint
- Web-App-Dokumentationswert: `RUNPOD_IDLE_TIMEOUT_SECONDS=3600`
- Web-App-Joblimit: `RUNPOD_JOB_TIMEOUT_SECONDS=1200`
- RunPod-`Execution timeout`: weiterhin `600 s`

Die Warmhalteberechnung bleibt intern erhalten, wird wegen ihrer begrenzten Aussagekraft in der bereinigten 0.6-Oberfläche jedoch nicht angezeigt. Ein Worker kann wegen Absturz, Releasewechsel oder Infrastrukturmaßnahmen jederzeit früher verschwinden.

## Berechtigungen und Datenschutz

Alle RunPod-Aufrufe erfolgen serverseitig. API-Key und Endpoint-IDs werden nicht an den Browser übertragen.

- Queue-Health: erforderlich für den sichtbaren Worker-/Jobstatus
- GPU-Katalog: erforderlich für Supply
- Serverless-Worker-Lesezugriff: erforderlich für GPU-Typ, Worker-ID, Release, Rechenzentrum und Laufzeit

Antwortet eine Management-API mit `403`, bleiben Analyse, individueller Jobstatus und Einzelabbruch nutzbar. Die nicht belastbaren Verwaltungsinformationen bleiben in der bereinigten GUI ausgeblendet.

Die Auftragsverwaltung ist nur nach Anmeldung erreichbar. Schreibende Abbruchaufrufe benötigen zusätzlich das sitzungsgebundene CSRF-Token. Der Browser darf nur einen öffentlichen Endpoint-Schlüssel aus der festen Allowlist senden; die tatsächliche Endpoint-ID und der API-Key werden ausschließlich serverseitig ergänzt. Job-IDs werden auf ein enges Zeichen- und Längenformat begrenzt, bevor sie in einen RunPod-Pfad eingesetzt werden.

## Akzeptanzkriterien

- Statusroute ist nur nach Anmeldung erreichbar.
- Browserwerte werden ausschließlich über die feste Endpoint-Allowlist aufgelöst.
- API-Key und Endpoint-IDs erscheinen weder in HTML noch in JSON.
- Supply- und Workerfehler beeinträchtigen die Modellanfrage nicht.
- Live-Anzeige verwendet für die konkrete Anfrage deren gespeicherten Jobstatus, keine erfundene Prozentzahl oder Zuordnung aus aggregierten Workerwerten.
- Ergebnis trennt Gesamt-, Warte-/Bereitstellungs- und KI-Verarbeitungszeit.
- Aktive, von der App registrierte Jobs bleiben in SQLite erhalten und lassen sich einzeln abbrechen.
- Altjobs lassen sich nach manueller Eingabe der Request-ID abbrechen.
- Es gibt keinen pauschalen Queue-Löschknopf.
- Supply-, Warmhalte- und technische Verwaltungsdetails bleiben in der bereinigten GUI ausgeblendet.
