# Bekannte Einschränkungen

## UI-005-01: Markdown wird als Rohtext dargestellt

| Feld | Bewertung |
|---|---|
| Betroffene Version | 0.5.0 |
| Bereich | Ergebnisdarstellung |
| Priorität | niedrig |
| Release-blockierend | nein |
| Status | vorgemerkt |

### Beobachtung

Das Modell strukturiert Antworten teilweise mit Markdown-Zeichen wie `###`, `**` und `---`. Die Webanwendung gibt das Feedback derzeit escaped innerhalb eines `<pre>`-Elements aus. Dadurch bleibt die Antwort sicher lesbar, die Markdown-Zeichen werden jedoch nicht typografisch formatiert.

### Auswirkung

Die fachliche Rückmeldung ist vollständig verfügbar. Betroffen ist ausschließlich die visuelle Darstellung; Erzeugung, Übertragung und Speicherung des Feedbacks funktionieren unverändert.

### Vorgesehene Verbesserung

In einer späteren Version soll ein sicherer Markdown-Renderer mit enger Element- und Attribut-Allowlist ergänzt werden. Modellinhalt darf dabei nicht ungeprüft als HTML in die Seite gelangen. Automatisierte Tests sollen insbesondere Überschriften, Hervorhebungen, Trennlinien, Links sowie die Abwehr von Script- und Event-Handler-Inhalten abdecken.
