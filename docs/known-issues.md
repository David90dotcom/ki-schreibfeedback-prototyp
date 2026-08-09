# Bekannte Einschränkungen

## UI-005-01: Markdown wird als Rohtext dargestellt

| Feld | Bewertung |
|---|---|
| Betroffene Version | 0.5.0 |
| Bereich | Ergebnisdarstellung |
| Priorität | niedrig |
| Release-blockierend | nein |
| Status | in Version 0.6 behoben |

### Beobachtung

Das Modell strukturiert Antworten teilweise mit Markdown-Zeichen wie `###`, `**` und `---`. Die Webanwendung gibt das Feedback derzeit escaped innerhalb eines `<pre>`-Elements aus. Dadurch bleibt die Antwort sicher lesbar, die Markdown-Zeichen werden jedoch nicht typografisch formatiert.

### Auswirkung

Die fachliche Rückmeldung ist vollständig verfügbar. Betroffen ist ausschließlich die visuelle Darstellung; Erzeugung, Übertragung und Speicherung des Feedbacks funktionieren unverändert.

### Vorgesehene Verbesserung

Version 0.6 verwendet einen sicheren Markdown-Renderer mit enger Element- und Attribut-Allowlist. HTML, aktive Links, Bilder und Code-Markup werden nicht aktiviert; automatisierte Sicherheitstests decken diese Fälle ab.

## RUNPOD-006-01: Hostabhängige Cold-Start-Abstürze

| Feld | Bewertung |
|---|---|
| Betroffene Version | 0.6 |
| Bereich | RunPod Serverless / vLLM |
| Priorität | mittel |
| Release-blockierend | nein |
| Status | transparent gemacht, extern verbleibend |

### Beobachtung

Einzelne neu bereitgestellte Worker können beim vLLM-/Triton-Warm-up mit einem CUDA-Fehler abbrechen. RunPod kann anschließend einen Ersatzworker starten; ein bereits erfolgreich gestarteter warmer Worker verarbeitet weitere Anfragen zuverlässig und deutlich schneller.

### Behandlung in der Anwendung

Die Anwendung verlängert ihr eigenes Queue-/Cold-Start-Limit auf 1200 Sekunden, zeigt Endpointstatus und laufende Wartezeit und trennt nach Erfolg `delayTime` von `executionTime`. Das RunPod-Idle-Timeout wird für den Prüfungsbetrieb auf 3600 Sekunden festgelegt. Diese Maßnahmen verbessern Transparenz und Nutzbarkeit, beseitigen aber keinen externen CUDA-/Hostfehler.

## RUNPOD-006-02: Fremde oder ältere Queue-Jobs sind nicht automatisch auflistbar

| Feld | Bewertung |
|---|---|
| Betroffene Version | 0.6 |
| Bereich | RunPod Serverless / Auftragsverwaltung |
| Priorität | niedrig |
| Release-blockierend | nein |
| Status | durch manuellen Einzelabbruch behandelt |

### Beobachtung

RunPods dokumentierte Queue-API liefert über `/health` nur aggregierte Anzahlen. Sie stellt keine öffentliche Operation bereit, mit der die Web-App sämtliche einzelnen Job-IDs eines Endpoints nachträglich abrufen kann.

### Behandlung in der Anwendung

Neue Aufträge werden unmittelbar nach `/run` technisch und ohne Schülertext persistent registriert und können automatisch aufgelistet werden. Für Jobs, die vor dieser Funktion oder außerhalb der Web-App entstanden sind, kann die Request-ID manuell eingegeben und über `/cancel/{job_id}` einzeln abgebrochen werden. `/purge-queue` wird nicht angeboten, weil die Operation alle wartenden Jobs des Endpoints betreffen würde.
