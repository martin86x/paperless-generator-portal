# Portal-Konnektor (MCP-Server für Claude Code)

Lokaler MCP-Server, mit dem Claude Code das Generator-Portal steuert. Läuft auf dem eigenen PC, nicht im Container. Nur Python-Standardbibliothek, keine Installation nötig.

## Werkzeuge

| Werkzeug | Zweck |
|---|---|
| `portal_status` | Version, Update verfügbar, Host-Helper, Wächter, aktives Profil, Lampe, Paperless-Schalter |
| `portal_update` | 1-Klick-Update oder Rollback anfordern (`action`: `update` / `rollback`) |
| `portal_request` | beliebiger Portal-Aufruf außer `/api` |
| `paperless_request` | Paperless-API über den Portal-Proxy, nur wenn der Schalter auf AN steht |

Mit dem Token immer gesperrt: Passwort, Recovery-Codes, Token selbst, Voll-Restore, Config-Backup, Profil-Export/-Import, Paperless-URL und „nur lesen“ ändern, Profil anlegen, wechseln oder löschen, Benachrichtigungen und Wächter speichern, Anwenden/Rückgängig, beide Schalter. Dokumente löschen ist auch bei Schalter AN gesperrt; ein Profil auf „nur lesen“ sperrt alle Schreibzugriffe.

## Einrichtung (Windows)

1. Im Portal unter **Verwaltung → Konto → API-Zugang** ein Token erzeugen.
2. Token in eine Datei im Benutzerordner legen (PowerShell):

   ```powershell
   Set-Content -NoNewline -Path "$env:USERPROFILE\.portal-token" -Value "<token>"
   ```

3. Konnektor in Claude Code registrieren (gilt für alle Projekte):

   ```powershell
   claude mcp add --env PORTAL_URL=http://<portal-ip>:8080 --transport stdio --scope user paperless-portal -- python "<Repo-Pfad>\tools\portal-mcp\portal_mcp.py"
   ```

   `--env` darf nicht direkt vor dem Namen stehen, deshalb `--transport stdio` dazwischen.

4. Claude Code neu starten, mit `claude mcp list` prüfen.

Optional: `PORTAL_TOKEN_FILE` zeigt auf eine andere Token-Datei.

## Anzeige im Generator

In der Kopfzeile des Generators:

- **Lampe**: grün = der Konnektor hat sich in den letzten 3 Minuten gemeldet (er meldet sich alle 45 s, solange Claude Code läuft), rot = getrennt, grau = gesperrt oder kein Token angelegt.
- **Konnektor: AN/AUS**: lässt das Token überhaupt zu. Bei AUS wird jeder Aufruf mit dem Token abgewiesen (403). Ausschalten nimmt auch den Paperless-Zugriff weg.
- **Paperless: AN/AUS**: gibt dem Token den Zugriff auf Paperless, nur bei Konnektor AN.

Beide Schalter lassen sich nur im eingeloggten Browser umlegen, ohne Passwort, und jede Änderung steht im Protokoll. Das Token selbst kann keinen der beiden umlegen. Ein neues oder widerrufenes Token setzt Paperless auf AUS.
