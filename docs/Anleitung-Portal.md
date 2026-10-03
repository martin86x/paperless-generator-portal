# Anleitung: Paperless Generator Portal

Stand: Portal v1.14.1 · für Anwender (kein Entwickler-Wissen nötig)

Diese Anleitung beschreibt nur, was es im Portal wirklich gibt. Stellen, bei denen ich mir nach dem Lesen des Codes nicht sicher bin, sind mit **(unsicher)** oder **Hinweis** gekennzeichnet. Zugangsdaten, Tokens und IP-Adressen stehen hier bewusst nicht drin; wo sie gebraucht werden, steht ein Platzhalter wie `<Portal-Adresse>`.

---

## 1. Was ist das Portal?

Das Portal ist eine kleine Web-Anwendung, die im Heimnetz läuft (als Container auf dem Proxmox-Server). Sie macht drei Dinge:

1. Sie stellt den **Paperless-Generator** dauerhaft bereit, mit Anmeldung. Der Generator baut dir die Einrichtung für Paperless-ngx (Tags, Dokumenttypen, Felder, Korrespondenten, Speicherpfade, Arbeitsabläufe).
2. Sie verwaltet **Profile**: ein Profil ist eine Paperless-Instanz (Adresse, Zugangs-Token, gespeicherte Generator-Einstellungen).
3. Sie **überwacht** deine Instanzen (Wächter), meldet Probleme aufs Handy, liefert einen **Fristen-Kalender** und eine druckbare **Notfall-Mappe**.

Der Generator redet nie direkt mit Paperless, sondern über das Portal. Das Portal setzt dabei das gespeicherte Token selbst ein. Deshalb braucht Paperless keine CORS-Einstellung.

**Aufruf im Browser:** `http://<Portal-Adresse>:8080`

---

## 2. Erste Einrichtung und Anmeldung

### Anmelden

1. Portal-Adresse im Browser öffnen.
2. Benutzername und Passwort eingeben. Beim allerersten Start gilt der Standard-Zugang aus der README des Projekts (Benutzer `admin`, Passwort steht auch auf der Anmeldeseite als Hinweis). Er wird in der Einrichtung sofort ersetzt.
3. Die Anmeldung gilt 8 Stunden, danach musst du dich neu anmelden.

**Schutz vor Raten:** Nach 5 Fehlversuchen von derselben Adresse innerhalb von 5 Minuten sperrt das Portal die Anmeldung kurz („Zu viele Fehlversuche, bitte einige Minuten warten“).

### Einrichtungs-Assistent (nur beim ersten Mal)

Solange die Einrichtung nicht fertig ist, kommst du nicht in den Generator. Der Assistent hat drei Schritte:

1. **Neues Passwort** setzen (ersetzt den Standard-Zugang).
2. **Erste Paperless-Instanz** anlegen: Name, Paperless-Adresse, API-Token. Das Token holst du in Paperless unter *Einstellungen → Mein Profil → API-Token*. Es wird beim Speichern sofort geprüft und verschlüsselt abgelegt.
3. **Optional:** E-Mail-Adresse für Frist-Erinnerungen und die Container-ID des Portals (nur die Nummer, wird fürs Update gebraucht).

Auf der Assistenten-Seite gibt es unten auch „Schon ein Backup?“: Hier kannst du ein Voll-Backup (ZIP) oder nur Profile (JSON) einspielen, statt alles neu einzurichten (Umzug, neuer Container).

### Passwort vergessen

Auf der Anmeldeseite auf „Passwort vergessen? Recovery-Code verwenden“ klicken. Jeder der 10 Recovery-Codes funktioniert genau einmal. Danach sofort ein neues Passwort setzen. Wo du Codes erzeugst, steht in Abschnitt 8.

---

## 3. Die Generator-Seite (Startseite)

Nach der Anmeldung siehst du den Generator mit einer festen **Kopfleiste** oben. Von links nach rechts:

| Element | Bedeutung |
|---|---|
| **Profil-Auswahl** | Wechselt das aktive Profil (die aktive Paperless-Instanz). Bei ungespeicherten Änderungen fragt das Portal vorher nach. |
| **💾 Profil speichern** | Speichert den Generator-Stand ins aktive Profil. Der vorige Stand landet automatisch in der Versionshistorie. Wird orange, wenn es Ungespeichertes gibt. |
| **● ungespeichert** | Erscheint, solange es Änderungen gibt, die noch nicht gespeichert sind. |
| **Health-Ampel** | 🟢 alles gut, 🟡 Hinweise, 🔴 kritisch. Ein Klick springt zum Ergebnis und prüft neu. |
| **🚀 Alle Tools** | Lässt alle lesenden Diagnose-Werkzeuge einmal laufen. Es wird nichts in der Instanz geändert. |
| **Konnektor-Lampe und zwei Schalter** | Siehe Abschnitt 4. |
| **Update-Hinweis** | Erscheint nur, wenn es eine neuere Portal-Version gibt. |
| **⚙ Verwaltung** | Öffnet die Verwaltung (Abschnitt 5). |
| **Logout** | Abmelden. |

Wenn das aktive Profil als **Produktivsystem** markiert ist, steht darüber ein farbiger Warnbalken („PRODUKTIV: … Änderungen wirken auf das Live-System“, bei „nur lesen“ mit Zusatz).

**Die Abschnitte des Generators** (Seitenleiste): 01 Verbindung & Start, 02 Was soll angelegt werden?, 03 Tags, 04 Dokumenttypen, 05 Benutzerdefinierte Felder, 06 Arbeitsabläufe, 07 Speicherpfade, 08 Korrespondenten, dazu „Konfiguration speichern/laden“ und Changelog, 09 Skript generieren, 10 Direkt-Ausführung, 11 Generiertes Bash-Skript, 12 So wendest du das Skript an, 13 Werkzeuge.

> **Wichtig:** Der Abschnitt **01 Verbindung** im Generator ist im Portal-Betrieb wirkungslos. Das Portal verwirft ein dort eingetipptes Token und nimmt das aus dem Profil. Den API-Token immer im **Profil** eintragen (Verwaltung → Profile → Verbindung). Eine Adresse im Token-Feld führt zu rotem Status oder HTTP 401.

Die E-Mail-Adresse für Frist-Erinnerungen ist beim Speichern eines Profils Pflicht. Sie wird für die Frist-Arbeitsabläufe gebraucht.

*Was jeder Generator-Abschnitt im Einzelnen tut, steht in der Generator-Hilfe („So wendest du das Skript an“, Abschnitt 12) und ist nicht Teil dieser Anleitung.*

---

## 4. Konnektor, die zwei Schalter und die Status-Lampe

**Was ist der Konnektor?** Eine Brücke zwischen **Claude Code auf deinem Windows-PC** und dem Portal. Mit ihm kann Claude Code den Portal-Status abfragen, ein Update anstoßen und (nur wenn du es erlaubst) Paperless über das Portal lesen und ändern.

Der Konnektor arbeitet mit einem eigenen **Portal-Token** (Abschnitt 8, „API-Zugang“). Ohne dieses Token zeigt die Kopfleiste „Konnektor: kein Token“.

### Die Status-Lampe

| Lampe | Text | Bedeutung |
|---|---|---|
| grau | „Konnektor: kein Token“ | Es gibt noch kein Portal-Token. |
| grau | „Konnektor gesperrt“ | Token da, aber Schalter „Konnektor“ steht auf AUS. Aufrufe werden abgewiesen. |
| grün | „Konnektor verbunden“ | Der Konnektor hat sich in den letzten 3 Minuten gemeldet (er meldet sich alle 45 Sekunden, solange Claude Code läuft). |
| rot | „Konnektor getrennt“ | Token da und erlaubt, aber seit über 3 Minuten kein Lebenszeichen. Meist ist Claude Code nicht offen. Mit der Maus über die Lampe fahren zeigt „zuletzt gemeldet“. |

### Die zwei Schalter

1. **Konnektor: AN/AUS.** Lässt das Token überhaupt zu. Bei AUS wird jeder Aufruf mit dem Token abgewiesen. Ausschalten nimmt auch den Paperless-Zugriff weg.
2. **Paperless: AN/AUS.** Erlaubt dem Konnektor, Paperless über das Portal zu **lesen und zu ändern**. Nur klickbar, wenn „Konnektor“ AN ist. Beim Einschalten kommt eine Rückfrage.

Merksatz: **Standardmäßig soll beides aus sein. Paperless nur einschalten, wenn du Claude gerade etwas in Paperless tun lassen willst, und danach wieder aus.**

Weitere Regeln:

- Beide Schalter lassen sich **nur in deinem angemeldeten Browser** umlegen, ohne Passwortabfrage. Das Token selbst kann sie nicht umlegen.
- Jede Änderung steht im **Protokoll** (Verwaltung → System).
- Ein neu erzeugtes oder widerrufenes Token setzt „Paperless“ auf AUS.
- **Dokumente löschen bleibt immer gesperrt**, auch bei Paperless: AN (Abschnitt 8).
- Mit dem Token sind außerdem gesperrt: Passwort ändern, Recovery-Codes, Token erzeugen/widerrufen, Voll-Backup und Wiederherstellen, Profile exportieren/importieren/anlegen/wechseln/löschen, Paperless-Adresse und „nur lesen“ ändern, „Anwenden/Rückgängig“, Kalender-Einstellungen. Benachrichtigungen und Wächter darf es **nur ansehen**, nicht speichern.

Die Einrichtung des Konnektors auf dem PC (Token-Datei, `claude mcp add`) steht in `tools/portal-mcp/README.md` im Portal-Ordner.

---

## 5. Die Verwaltung

Aufruf: **⚙ Verwaltung** in der Kopfleiste (oder `http://<Portal-Adresse>:8080/verwaltung`). Oben gibt es sieben Reiter. Der gewählte Reiter steht in der Adresse (`?tab=…`), du kannst also Lesezeichen setzen.

### 5.1 Überblick

- Kopfkarte mit aktivem Profil, Verbindungsstatus (grün/rot) und Schnellknöpfen: **Generator öffnen**, **Konfiguration anwenden**, **Paperless öffnen**, **Benachrichtigungen**, **Wächter**, **Profile sichern**.
- **Kennzahlen-Kacheln** der aktiven Instanz: Dokumente, Posteingang, ohne Typ, ohne Korrespondent.
- **Statuswand:** eine Ampel pro Profil (grün/rot/grau) mit Dokumentzahl und Erreichbarkeit. Sie zeigt den **zuletzt vom Wächter erfassten** Stand, ohne die Instanzen neu abzufragen. Ist der Wächter aus, bleiben die Ampeln grau.
- Fußzeile: Portal-Version, Größe von `/config`, **letzte Sicherung** (grün unter 7 Tage, gelb unter 30, rot älter oder nie), Wächter-Status und Link „Auf Updates prüfen“.

**Konfiguration anwenden** (Knopf nur sichtbar, wenn das Profil eine gespeicherte Konfiguration hat und nicht „nur lesen“ ist): legt die **fehlenden** Einträge aus dem Profil in der Instanz an. Nur Angehaktes wird geschrieben, Dokumente werden nie verändert. Ablauf: Einträge ankreuzen, Passwort bestätigen, „Ausgewählte anwenden“. Vorher wird der Ist-Stand als Snapshot gesichert (zum Nachschlagen, **kein** Ein-Klick-Restore). „Rückgängig“ entfernt wieder, was das Portal selbst angelegt hat.

### 5.2 Profile

Ein Profil bündelt **Verbindung** (Adresse + Token) und **Generator-Konfiguration**. Genau ein Profil ist **aktiv**: Es bestimmt, womit der Generator arbeitet und wohin das Portal alle Paperless-Aufrufe schickt.

Je Profil:

- **▶ Aktivieren & in den Generator** (bei nicht aktiven Profilen).
- **Verbindung:** Paperless-Adresse, API-Token (leer lassen = behalten), Erinnerungs-E-Mail.
- **Umbenennen:** nur der Anzeigename.
- **Sicherheits-Einstellungen:**
  - *Produktivsystem*: dauerhafter Warnbalken (Farbe frei wählbar).
  - *Nur lesen*: das Portal blockt **jeden** Schreibzugriff (POST/PUT/PATCH/DELETE) auf diese Instanz. Das ist der härteste Schutz.
- **Profil löschen:** entfernt das Profil samt gespeicherter Konfiguration. Dokumente in Paperless bleiben unberührt.
- **Überwachung (Wächter):** Instanz überwachen ja/nein und welche Checks laufen (Erreichbarkeit/Token, Konfig-Drift, ASN-Lücken, Duplikate). Überschreibt die globale Vorgabe.
- **Versionshistorie:** der jeweils vorige Stand bei „Profil speichern“. „Vergleichen“ zeigt, was ein Wiederherstellen ändern würde; einzelne Kategorien sind wählbar. Der aktuelle Stand wird vorher automatisch gesichert.
- **Instanz-Snapshots:** Belege, die beim „Anwenden“ entstehen (zum Nachschlagen).

Oben: **⬇ Alle Profile sichern** (JSON mit allen Profilen und entschlüsselten Tokens, also vertraulich behandeln) und **⬆ Wiederherstellen** (ersetzt alle Profile, vorher wird automatisch gesichert). Unten: **Neues Profil** anlegen.

### 5.3 Auswertung

Zwei Teile untereinander:

- **Kennzahlen:** Live-Zahlen jeder Instanz (Dokumente, Posteingang, ohne Typ, ohne Korrespondent) und **Drift**: Vergleich der gespeicherten Konfiguration mit der echten Instanz („+3 nicht angelegt“ oder „2 extra in Instanz“).
- **Trends:** Kurven über Zeit (Gesamtzahl, Aufräum-Werte, Antwortzeit) mit Zeitraumwahl. Die Daten sammelt der Wächter (höchstens etwa einmal pro Stunde). Ist der Wächter aus, hilft „Jetzt erfassen“. Eine Kurve braucht mindestens 2 Messpunkte.

### 5.4 Werkzeuge

Rein lesende Prüfungen der **aktiven** Instanz. Jeder Punkt verlinkt in die passende gefilterte Paperless-Ansicht:

- **Aufräumen:** ohne Dokumenttyp, ohne Korrespondent, ohne ASN-Nummer, im Posteingang.
- **ASN-Lücken:** fehlende Nummern in der ASN-Folge und die nächste freie ASN.
- **Mögliche Duplikate:** gleiche Titel (Heuristik, keine Garantie).

### 5.5 Wächter

Der Wächter prüft regelmäßig und **rein lesend** alle Profile mit hinterlegter Instanz und meldet Auffälligkeiten über deine Benachrichtigungskanäle. Er schreibt nie in eine Instanz.

Oben stehen Zustand (aktiv/aus), letzter und nächster Lauf, offene Auffälligkeiten und die Liste „in Beobachtung“. Die Einstellungen sind unter **⚙ Einstellungen** eingeklappt:

- **Wächter aktiv** und **Intervall** (Standard 60 Minuten, mindestens 5).
- **Checks (globale Vorgabe):** Erreichbarkeit/Token, Konfig-Drift, Fehlgeschlagener Einzug, ASN-Lücken (ab wie vielen Lücken melden), Duplikate (standardmäßig aus, kann bei vielen Dokumenten dauern).
- **Eskalation:** Alarm erst nach X schlechten Läufen in Folge (Standard 2, schützt vor Fehlalarm), Entwarnung erst nach X guten Läufen, Erinnerung alle X Minuten (0 = keine), Abstand verdoppeln, Priorität anheben ab der X. Erinnerung, Entwarnung über die Kanäle melden.
- **Tages-Digest und Heartbeat:** täglich eine Kurzmeldung je Profil (damit du weißt, dass die Überwachung läuft); Heartbeat-URL für einen externen Dienst wie healthchecks.io, der Alarm schlägt, wenn das Portal selbst tot ist.
- **Wochen-/Monatsreport:** Rückblick (Uptime, längster Ausfall, Dokument-Wachstum), mit Vorschau und „Report jetzt senden“.
- **Webhook / n8n** (Abschnitt 7).
- **Prometheus /metrics:** Wächter-Stand für Grafana & Co., geschützt durch ein eigenes Token.
- **Fristen-Kalender** (Abschnitt 7).

Mit **Speichern** werden die Einstellungen übernommen, **Jetzt prüfen** speichert und startet sofort einen Lauf. Darunter stehen der **letzte Prüflauf** je Profil und der **Verlauf** (Streifen grün/rot/grau, wählbar 7 Tage, 30 Tage, alles).

> **Hinweis:** Tages-Digest und Report hängen an der Wächter-Schleife. Ist „Wächter aktiv“ aus, kommen sie nicht von selbst (die Knöpfe „jetzt senden“ funktionieren trotzdem).

### 5.6 Benachrichtigungen

Kanäle für das **aktive** Profil. Drei stehen zur Wahl, jeder mit eigenem Test-Knopf (speichert und sendet sofort):

- **Pushover:** API-Token (App) und User-Key.
- **ntfy:** Server (z. B. der öffentliche ntfy-Dienst oder dein eigener) und Topic. Wer das Topic kennt, sieht die Meldungen, also ein schwer erratbares Topic wählen.
- **E-Mail (SMTP):** Host, Port, STARTTLS (Port 465 = implizites SSL), Benutzer, Passwort, Absender, Empfänger.

Gesetzte Passwörter und Schlüssel bleiben erhalten, wenn das Feld beim Speichern leer ist.

Darunter die **Priorität je Ereignis** (−2 Stumm bis 2 Notfall): Instanz nicht erreichbar, Drift, fehlgeschlagene Verarbeitung, ASN-Lücken, Duplikate, Digest, Report, Portal-Update verfügbar, Fehler im Portal/Wächter. Die Meldungen kommen vom Wächter.

### 5.7 System

Ein Reiter mit drei Abschnitten untereinander:

- **Konto:** Passwort ändern, Recovery-Codes, API-Zugang (Abschnitt 8).
- **Version:** Versionsstand, Update, Voll-Backup (Abschnitte 9 und 10).
- **Protokoll:** die letzten Aktionen im Portal (neueste zuerst), auch geblockte Schreibversuche und das Umlegen der Schalter. Mit Suche, Filter nach Art und Stufe und Log-Download.

---

## 6. Notfall-Mappe drucken

Eine Seite zum Ausdrucken oder als PDF-Speichern, falls du oder jemand anderes im Notfall an die Dokumentenverwaltung kommen muss.

1. Im Browser `http://<Portal-Adresse>:8080/notfall` öffnen (angemeldet). **(unsicher)**: Ich habe in der Verwaltung keinen Knopf dafür gefunden, nur einen Link in der Unter-Navigation einzelner Seiten. Das Lesezeichen auf die Adresse ist der sichere Weg.
2. Auf **Drucken / als PDF speichern** klicken. Beim Drucken verschwinden Navigation und Knöpfe, die Seite wird schwarz auf weiß.

**Was draufsteht:** Stand und Portal-Version, Datum des letzten Portal-Backups, Anzahl übriger Recovery-Codes, je Instanz die Adresse, Speicherpfade, überwachte Fristen, Dokumenttypen und wichtige Korrespondenten (aus den im Portal gespeicherten Angaben, es wird nichts bei Paperless abgefragt).

**Was nicht draufsteht:** Passwörter, Tokens, Recovery-Codes, Dokumentinhalte. Leere Linien zum **Handschriftlich-Ausfüllen** sind vorgesehen für: Passwort-Manager/Tresor, wer helfen kann (Name, Telefon), wo der Admin-Login liegt, Versicherungen, Bank, Steuerberater/Anwalt, Sonstiges.

Tipp: nach größeren Änderungen am Profil neu drucken und die alte Mappe wegwerfen.


---

## 7. Benachrichtigungen, n8n-Webhook und Fristen-Kalender

### 7.1 Webhook an n8n einrichten

Der Wächter kann bei Ereignissen ein JSON per POST an eine Adresse schicken, zum Beispiel an einen n8n-Webhook-Knoten.

**In n8n:**

1. Neuen Workflow mit einem **Webhook-Knoten** anlegen, **HTTP Method = POST**. (Bei GET antwortet n8n mit 404 „not registered for POST requests“.)
2. Die **Test-URL** (`/webhook-test/…`) lauscht nur, solange der Workflow im Editor offen ist und du „Listen for Test Event“ geklickt hast. Für den Dauerbetrieb den Workflow **aktivieren** und die **Production-URL** (`/webhook/…`) verwenden.

**Im Portal:** Verwaltung → Wächter → ⚙ Einstellungen → **Webhook / n8n**:

1. „Webhook aktiv“ anhaken.
2. Webhook-URL eintragen.
3. Auswählen, was rausgehen soll: Alarm, Erinnerung, Entwarnung, Täglicher Digest, Wochen-/Monatsreport. (Vorgabe: Alarm und Entwarnung an.)
4. Speichern, dann **Webhook testen**. Darunter steht die **letzte Zustellung** (Zeit, Art, ✓ oder ✗ mit Detail).

**Aufbau der Nachricht** (bei jeder Art gleich):

```json
{
  "source": "paperless-generator-portal",
  "portal_version": "…",
  "kind": "alarm",
  "event": "downtime",
  "profile": "Name des Profils",
  "status": "bad",
  "detail": "Text mit Einzelheiten",
  "ts": "2026-07-15T14:03:11"
}
```

- `kind`: alarm, reminder, recovery, digest, report oder test. In n8n darauf verzweigen (`{{ $json.kind }}`), denn Alarm und Erinnerung haben beide `status: "bad"`.
- `event`: welcher Check betroffen ist (downtime, drift, task_fail, asn_gap, duplicate; bei Digest/Report/Test entsprechend).
- `status`: bad, ok, info oder test.

Der Alarm kommt erst nach der eingestellten Zahl schlechter Läufe (Eskalation). Erinnerungen kommen nur, wenn ein Erinnerungsabstand gesetzt ist.

### 7.2 Fristen-Kalender abonnieren

Der Kalender zeigt Fristen als **Ganztagstermine** in Handy- oder Desktop-Kalender. Quelle sind die Datumsfelder der Frist-Erinnerungen im **aktiven** Profil. Er liest nur, in Paperless wird nichts geändert. Jeder Termin enthält Feldname, Dokumenttitel und einen Link. Der Feed ist 15 Minuten zwischengespeichert, Termine reichen etwa 30 Tage zurück und etwa 400 Tage voraus.

**Einschalten:** Verwaltung → Wächter → ⚙ Einstellungen → **Fristen-Kalender** → „Kalender-Abo aktiv“ anhaken → **Speichern**. Danach steht die **Abo-URL** da (mit geheimem Token).

**Im Kalender eintragen:**

- Google Kalender: „Per URL hinzufügen“.
- iPhone: „Abonnierter Kalender“ (Einstellungen → Kalender → Accounts → Account hinzufügen → Andere).
- Anderer Kalender: eine Funktion wie „Kalender per Internetadresse abonnieren“.

**Wichtig:**

- Die URL hängt **nicht** am Portal-Login. Wer sie kennt, sieht die Dokumenttitel deiner Fristen. Nicht weitergeben.
- Mit **Token neu erzeugen** wird die alte URL ungültig; dann die Abo-URL im Kalender ersetzen.
- Mit `&profil=…` am Ende der URL lässt sich ein anderes Profil wählen (das Token gilt für alle Profile).
- Bei `http://` läuft sie unverschlüsselt durch dein Netz. Im Heimnetz vertretbar.
- Ist der Kalender aus oder das Token falsch, antwortet die Adresse mit „Not Found“ (404).
- **Hinweis (allgemein, nicht aus dem Code):** Manche Kalender-Dienste rufen Abos von ihren eigenen Servern ab. Eine Heimnetz-Adresse kann dann nicht funktionieren. Das iPhone im WLAN oder ein Desktop-Kalender im Heimnetz holt sie direkt.

---

## 8. Sicherheit: Konto, Token, Recovery-Codes

Alles unter **Verwaltung → System → Konto**.

- **Passwort ändern:** aktuelles Passwort, neues Passwort, Wiederholung.
- **Recovery-Codes:** Passwort zur Bestätigung eingeben, dann „10 Recovery-Codes erzeugen“. Die Codes werden **nur ein einziges Mal** angezeigt. Sofort kopieren oder als .txt speichern und sicher aufbewahren (Passwort-Manager oder ausgedruckt). Jeder Code gilt einmal. Neue Codes erzeugen macht alle alten ungültig. Das Konto zeigt, wie viele Codes noch gültig sind.
- **API-Zugang (Portal-Token):** Passwort bestätigen, „Token erzeugen“. Auch dieses Token wird **nur einmal** angezeigt. Danach siehst du nur die letzten Zeichen, Erzeugungsdatum und „zuletzt genutzt“. **Token widerrufen** sperrt es sofort. Das Token gilt nur fürs Portal, nie für Paperless direkt (siehe Abschnitt 4 für die Grenzen).
- **Paperless-Tokens** der Profile werden im Portal verschlüsselt gespeichert. Wichtig: Beim Voll-Backup stecken die Schlüssel mit im ZIP, deshalb das ZIP vertraulich behandeln.
- Das Portal ist für das **Heimnetz** gedacht. Es gehört nicht ohne Weiteres ins offene Internet.

### Löschen in Paperless ist immer gesperrt

Das Portal blockt über seinen Paperless-Zugang jede Aktion, die Dokumente löschen würde, auch wenn Konnektor und Paperless-Schalter an sind. Gesperrt sind:

- das Löschen eines einzelnen Dokuments,
- Massen-Löschen,
- Zusammenführen, Teilen und PDF-Bearbeiten, wenn dabei die Originale gelöscht würden (oder die Einstellung nicht eindeutig lesbar ist),
- das endgültige Leeren des Papierkorbs (nur „Wiederherstellen“ aus dem Papierkorb bleibt möglich).

Die Antwort des Portals lautet dann „Gesperrt: Dokument-Loeschung ist im Portal nicht erlaubt“ (HTTP 403). Löschen musst du direkt in Paperless selbst, mit deinem eigenen Zugang.

Ist ein Profil auf **Nur lesen** gestellt, sind außerdem **alle** Schreibzugriffe auf diese Instanz gesperrt.

---

## 9. Sicherung und Wiederherstellung

Es gibt zwei Arten, und beide sind wichtig:

| Was | Wo | Inhalt |
|---|---|---|
| **Voll-Backup (ZIP)** | Verwaltung → System → Version → *Portal-Sicherung (komplett)* → „Voll-Backup herunterladen“ | Alles aus `/config`: Profile, Benachrichtigungen, Wächter-Einstellungen, Historie, Protokoll, **und den Zugang** (Passwort-Hash, Schlüssel, verschlüsselte Tokens). |
| **Profile (JSON)** | Verwaltung → Profile → „⬇ Alle Profile sichern“ | Nur die Profile mit Token (entschlüsselt, also auf anderem Host einspielbar). |

**Wann:** vor jedem Update und vor einem Umzug. Die Überblick-Seite zeigt, wie alt die letzte Sicherung ist (grün unter 7 Tage).

**Einspielen:**

- **Voll-Backup:** Verwaltung → System → Version → „Backup wiederherstellen (ZIP)“ → „Einspielen“. Das überschreibt die aktuelle Konfiguration, auch das Passwort. Danach mit dem Passwort **von damals** neu anmelden. Ein Neustart kann nötig sein.
- **Nur Profile:** Verwaltung → Profile → „⬆ Wiederherstellen“. Ersetzt alle Profile (der aktuelle Stand wird vorher gesichert).
- Auf einem **frischen Container** ohne Einrichtung geht beides direkt auf der Seite des Einrichtungs-Assistenten („Schon ein Backup?“).

Das Portal legt außerdem selbst vor jedem Schreiben der Profile ein rotierendes Backup an und hält die Versionshistorie je Profil. Das ersetzt aber keine Sicherung, die du außerhalb des Containers aufbewahrst.

Das ZIP und die Profil-JSON enthalten Geheimnisse. Nicht per E-Mail verschicken und nicht in eine öffentliche Ablage legen.

---

## 10. Update einspielen

Unter **Verwaltung → System → Version** (Link „Auf Updates prüfen“ auch im Überblick). Dort steht die installierte Version, der Abgleich mit GitHub („Update verfügbar“ oder „Aktuell“) und der neueste Commit. In der Generator-Kopfleiste erscheint ein Hinweis, sobald eine neuere Version vorliegt.

**Vorher:** Voll-Backup herunterladen (Abschnitt 9).

### Weg A: 1-Klick-Update (wenn eingerichtet)

Voraussetzung ist der **Host-Helper**, ein kleines Cron-Skript auf dem Container, das einmalig eingerichtet wird (Anleitung in `host-helper/README.md`). Ist er aktiv, steht im Abschnitt „1-Klick-Update“ **„Helper aktiv“** und es gibt zwei Knöpfe:

1. **⟳ Jetzt aktualisieren** (Rückfrage bestätigen). Das Portal legt eine Anforderung ab, der Helper führt sie innerhalb von etwa einer Minute aus und baut den Container neu.
2. **↩ Rollback:** zurück auf den vorherigen Stand, falls nach dem Update etwas nicht stimmt.

Darunter steht das Ergebnis der letzten Aktion mit Commit und Zeit. „Anforderung liegt vor“ heißt: der Helper hat sie noch nicht abgeholt.

Das ist bewusst so gebaut: Das Portal selbst bekommt keine Docker-Rechte auf dem Server.

### Weg B: Befehl auf dem Proxmox-Server

Ohne Helper zeigt der Abschnitt „Aktualisieren“ einen **fertigen Befehl** zum Kopieren:

1. Im Abschnitt „Container“ die Container-ID (nur die Nummer, sichtbar mit `pct list` auf dem Proxmox-Server) eintragen und **Speichern**. Sie wird in den Befehl eingesetzt.
2. **Befehl kopieren** und in der **Proxmox-Host-Shell** ausführen. Er holt den neuesten Stand von GitHub und baut den Container neu.

Profile und Einstellungen im `/config`-Volume überstehen das Update.

### Nach dem Update

Seite neu laden. Unter Version prüfen, ob die neue Versionsnummer steht und „Aktuell“ angezeigt wird. Der Konnektor kann den Update-Stand auch abfragen und (bei Helper) ein Update anfordern.

---

## 11. Typische Probleme

| Problem | Ursache und Lösung |
|---|---|
| **Roter Status / HTTP 401** beim Profil | Token falsch, abgelaufen oder eine Adresse statt eines Tokens eingetragen. Neues Token in Paperless holen und bei Verwaltung → Profile → *Verbindung* eintragen. Im Generator-Abschnitt 01 hilft nichts. |
| Generator-Schalter oder Token-Feld in Abschnitt 01 „tut nichts“ | Gewollt: Im Portal zählt nur das Token aus dem Profil. |
| **„Zu viele Fehlversuche“** bei der Anmeldung | Einige Minuten warten, dann erneut versuchen. |
| **Passwort vergessen** | Recovery-Code auf der Anmeldeseite. Keine Codes mehr: nur noch ein Voll-Backup einspielen. Deshalb die Codes vorab erzeugen und sichern. |
| **Lampe grau, „kein Token“** | Noch kein Portal-Token erzeugt (System → Konto → API-Zugang). |
| **Lampe grau, „gesperrt“** | Schalter „Konnektor“ steht auf AUS. Anklicken zum Zulassen. |
| **Lampe rot, „getrennt“** | Claude Code läuft nicht oder erreicht das Portal nicht. Claude Code starten bzw. neu starten, Adresse und Token-Datei prüfen (siehe `tools/portal-mcp/README.md`). |
| **Paperless-Schalter lässt sich nicht klicken** | Er geht nur bei Konnektor AN und vorhandenem Token. |
| **Statuswand und Trends bleiben grau/leer** | Der Wächter ist aus oder hat noch nicht gelaufen. Wächter einschalten, „Jetzt prüfen“ oder bei Trends „Jetzt erfassen“. |
| Keine Meldungen aufs Handy | Kanal unter Benachrichtigungen aktiv? Test-Knopf probiert? Beachten: Der Alarm kommt erst nach mehreren schlechten Läufen (Standard 2, bei 60 Minuten Intervall also frühestens nach etwa einer Stunde). Auf 1 stellen, wenn es sofort sein soll. |
| **Digest/Report kommt nicht** | Wächter aus (die Zeitpläne hängen an ihm). |
| **n8n: „webhook is not registered for POST“** | Im n8n-Knoten HTTP Method auf POST stellen. Test-URL nur bei offenem Editor mit „Listen for Test Event“, sonst Workflow aktivieren und Production-URL nehmen. |
| **Kalender-Abo liefert „Not Found“** | Kalender-Abo ausgeschaltet oder Token falsch/erneuert. Abo-URL im Wächter-Reiter neu kopieren. „Paperless nicht erreichbar“ (502): Instanz prüfen. |
| **Löschen geht nicht („Gesperrt“, 403)** | Gewollt, siehe Abschnitt 8. In Paperless direkt löschen. |
| **Schreiben geht nicht („nur lesen“, 403)** | Das Profil ist auf „nur lesen“ gestellt. Verwaltung → Profile → Sicherheits-Einstellungen. |
| **Ungespeicherte Änderungen** | „● ungespeichert“ in der Kopfleiste: **💾 Profil speichern** klicken, bevor du das Profil wechselst oder den Tab schließt. |
| **1-Klick-Update: „meldet sich nicht mehr“** | Der Helper läuft nicht. Auf dem Container prüfen (Skript ausführbar, Cron-Eintrag mit `bash` davor), Details in `host-helper/README.md`. Eine liegengebliebene Anforderung wird danach nachgeholt. |
| **Nach Voll-Restore Anmeldung klappt nicht** | Es gilt wieder das Passwort aus dem Backup. Mit dem damaligen Passwort anmelden. |

---

## Kurz-Übersicht: wo finde ich was?

| Ich will … | Dann … |
|---|---|
| Instanz wechseln | Generator-Kopfleiste → Profil-Auswahl |
| Token einer Instanz ändern | Verwaltung → Profile → *Verbindung · Umbenennen · Löschen* |
| Claude Zugriff auf Paperless geben | Generator-Kopfleiste → Konnektor AN, dann Paperless AN (danach wieder AUS) |
| Handy-Meldungen einrichten | Verwaltung → Benachrichtigungen |
| n8n anbinden / Fristen-Kalender | Verwaltung → Wächter → ⚙ Einstellungen |
| Papier-Notfallplan | `/notfall` im Browser, Drucken |
| Backup ziehen | Verwaltung → System → Version → Portal-Sicherung |
| Update | Verwaltung → System → Version |
| Passwort / Recovery-Codes / Portal-Token | Verwaltung → System → Konto |
| Nachsehen, was passiert ist | Verwaltung → System → Protokoll |
