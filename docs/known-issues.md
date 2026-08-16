# Bekannte Einschränkungen von Version 1.0.0

Die folgenden Punkte sind für den Forschungsprototyp bekannt und nicht
release-blockierend. Sie werden bei der Interpretation der Evaluation
berücksichtigt.

## Fachliche Grenzen der Modellantworten

Sprachmodelle erzeugen probabilistische Ausgaben. Die technische Belegprüfung
kontrolliert, ob ein angeführter Ausschnitt tatsächlich im Schülertext
vorkommt. Sie kann jedoch nicht deterministisch nachweisen, dass die daraus
abgeleitete fachliche Bewertung logisch richtig ist. Besonders bei guten
Texten kann künstlicher Verbesserungsdruck zu überkritischen oder
halluzinierten Hinweisen führen. Bei unzureichender Bewertungsgrundlage ist
deshalb die neutrale Stufe „Keine sichere Einordnung“ vorgesehen.

Die Plattform ist keine autonome Benotungsinstanz. Feedback, Statusstufen und
automatische Meta-Vorbewertungen müssen fachlich durch eine Lehrkraft geprüft
werden.

## Externe Provider und RunPod-Kaltstarts

Verfügbarkeit, Antwortzeit und Modellverhalten externer APIs liegen nicht
vollständig unter Kontrolle der Anwendung. Beim RunPod-Serverless-Endpoint
können Queue- und Kaltstartzeiten stark schwanken oder einzelne Worker während
der Initialisierung ausfallen. Version 1.0.0 begrenzt den Betrieb auf einen
automatischen 48-GB-GPU-Pool, höchstens einen Worker, 15 Minuten Job-TTL und
fünf Sekunden Idle-Timeout. Die Anwendung zeigt den Status an und versucht
überlange Aufträge gezielt abzubrechen, kann externe Infrastrukturfehler aber
nicht verhindern.

## Prototypischer Einzelinstanzbetrieb

Die produktive Bereitstellung verwendet eine einzelne Webinstanz und eine
lokale SQLite-Datenbank. Die Begrenzung fehlgeschlagener Loginversuche und die
Sperre paralleler Schülerläufe besitzen teilweise prozesslokalen Zustand. Eine
horizontale Skalierung auf mehrere Webinstanzen würde dafür einen gemeinsam
genutzten Zustandsdienst und eine dafür ausgelegte Datenbank erfordern.

Sitzungen laufen nach dem konfigurierten Zeitraum ab. Noch nicht abgesendete
Formulare werden ausschließlich im Browser gehalten und nach Navigation,
Neuladen oder erneuter Anmeldung nicht automatisch wiederhergestellt.

## Datenschutz und Versuchsdaten

Für Untersuchungen dürfen nur erfundene oder vollständig anonymisierte Texte
verwendet werden. Bei Auswahl eines Cloudproviders werden die für den Aufruf
benötigten Inhalte an den jeweiligen Anbieter übertragen. Die Anwendung
reduziert und trennt gespeicherte Daten, ersetzt aber keine schulische
Datenschutzprüfung, Auftragsverarbeitungsvereinbarung oder Rechtsgrundlage.

## Historische Einschränkungen

Frühere Releases mit mehreren dedizierten RunPod-Endpunkten und HTTP/3 sind
nicht der aktuelle Betriebsstand. Version 1.0.0 verwendet einen einzelnen
automatischen Pool und beschränkt Caddy auf HTTP/1.1 und HTTP/2. Historische
Details bleiben über die älteren Release-Tags und Abnahmeprotokolle
reproduzierbar.
