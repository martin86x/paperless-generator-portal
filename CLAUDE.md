# Paperless Generator Portal

**Was:** Self-hosted Flask-App, die den Paperless-ngx Setup Generator dauerhaft bereitstellt — mit Login, Profilen, Einstellungsmenü und API-Proxy (kein CORS in Paperless nötig).  
**Aktuelle Version:** steht in `app/VERSION` (einzige Quelle, nicht hier doppelt pflegen).  
**Sprache:** Deutsch — immer und überall.  
**Repo:** https://github.com/martin86x/paperless-generator-portal

Schwesterprojekt: `../Generator-Build` baut das Generator-HTML, das hier unter `site/index.html` liegt (siehe `../CLAUDE.md`).

## Aufbau

```
Generator-Portal/
├── app/
│   ├── app.py            ← gesamte Flask-App (Login, Profile, Proxy, Wächter, Update, Backup)
│   ├── inject.js         ← wird zur Laufzeit in das Generator-HTML eingefügt
│   ├── templates/        ← Jinja-Templates der Portal-Seiten
│   ├── requirements.txt  ← gepinnte Abhängigkeiten
│   └── VERSION           ← Portal-Release
├── site/index.html       ← Generator-Build (Artefakt aus ../Generator-Build/dist, NIE von Hand bearbeiten)
├── tests/                ← eigenständige Testskripte (kein pytest)
├── host-helper/          ← Cron-Skript auf dem LXC für 1-Klick-Update/Rollback
├── tools/portal-mcp/     ← Konnektor: lokaler MCP-Server für Claude Code (läuft auf dem PC)
├── Dockerfile, docker-compose.yml
├── proxmox-install.sh    ← legt LXC an, installiert Docker, startet den Container
└── update.ps1            ← holt ../Generator-Build/dist/index.html nach site/
```

- `/api/*` ist ein Reverse-Proxy an das konfigurierte Paperless; der Token wird serverseitig eingespritzt.
- Der Generator wird **nicht** verändert: die Vorkonfiguration wird nur in die HTTP-Antwort injiziert, `site/index.html` bleibt Byte für Byte identisch.
- Laufzeitdaten (Token, Passwort-Hash, Profile, Historie) liegen im Volume `config/` bzw. `$CONFIG_DIR` — nie committen, nie in Antworten zitieren.
- Der Container läuft mit `gunicorn -w 2 --preload`; der Wächter läuft nur in dem Worker, der die Dateisperre hält, Status wird über Dateien in `$CONFIG_DIR` geteilt.
- Update-Prüfung vergleicht `app/VERSION` mit dem Stand auf `main` bei GitHub — ein Push mit neuer VERSION wird also bei allen Installationen als Update angeboten.

## Tests

Einmalig ein venv anlegen (im System-Python fehlen Flask & Co.):

```powershell
python -m venv .venv
.venv\Scripts\pip install -r app\requirements.txt
```

Dann jedes Skript einzeln starten, kein Netzwerk nötig (`CONFIG_DIR` zeigt auf ein Temp-Verzeichnis):

```powershell
.venv\Scripts\python tests\test_auth.py
.venv\Scripts\python tests\test_proxy.py
# ebenso: test_apitoken, test_connector, test_apply, test_backup, test_escalation, test_metrics, test_report, test_restart, test_webhook, test_ics, test_notfall, test_blank, test_links, test_actions
```

Nach jeder Änderung an `app/app.py` die betroffenen Tests laufen lassen, vor einem Release alle.

## Lokal starten

Preview-Konfiguration `portal-dev` in `.claude/launch.json` (Port 8090, damit 8080 für den Generator frei bleibt). Sie setzt `CONFIG_DIR=.dev-config`, `SITE_DIR=site`, `PORTAL_WATCHER=0` und braucht das venv. Login beim ersten Start: `admin` / `admin`.

Mit Docker: `docker compose up -d --build` → `http://localhost:8080`.

## Regeln

- **`site/index.html` nie direkt bearbeiten** — Änderungen am Generator gehören nach `../Generator-Build/src/`, danach `update.ps1`.
- **Jede Portal-Änderung = neue Version** in `app/VERSION` (z. B. 1.11.4 → 1.11.5).
- Commit-Texte: `Portal v1.11.5: <Kurzbeschreibung>` für Portal-Code, `site: Generator v3.98 - <Kurzbeschreibung>` für einen neuen Generator-Build.
- Sicherheit nicht aufweichen: kein Docker-Socket im Container, CSRF-Origin-Check, Login-Rate-Limit und Token-Verschlüsselung at-rest bleiben erhalten.
- Abhängigkeiten in `app/requirements.txt` bleiben gepinnt; ein Update ist eine eigene, getestete Änderung.
- Bereichsregeln für `app/`, `tests/` und Deployment stehen in `.claude/rules/` und laden automatisch, sobald passende Dateien bearbeitet werden.
- Vor einem Portal-Release den Subagenten `portal-pruefer` (`.claude/agents/`) die Änderungen prüfen lassen; er liest nur.

## Arbeitsweise

- Direkt loslegen, keine Rückfragen
- Am Session-Ende: GitHub-Push vorschlagen (User muss „ja" sagen) — ein Push löst bei laufenden Installationen das Update-Angebot aus
