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
