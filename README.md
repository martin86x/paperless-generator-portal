# Paperless Generator Portal

Self-hosted Web-App, die den **Paperless-ngx Setup Generator** dauerhaft bereitstellt —
mit Login, Einstellungsmenü und automatischem API-Proxy (kein CORS-Gefummel mehr).

Deploy in **einem** Befehl auf einem Proxmox-Host: legt einen LXC an, installiert Docker
und startet den Container.

---

## Schnellstart (Proxmox-Host-Shell)

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/martin86x/paperless-generator-portal/main/proxmox-install.sh)"
```

Nach dem Durchlauf zeigt das Skript die URL an. Dann:

1. `http://<lxc-ip>:8080` öffnen → **Login** mit `admin` / `admin`
2. **Passwort ändern** (Einstellungen)
3. In den **Einstellungen** die **Paperless-URL** (z. B. `http://192.168.10.200:8000`)
   und den **API-Token** eintragen → speichern
4. Fertig — der Generator ist oben im Menü erreichbar und bereits verbunden.

---

## Wie es funktioniert

```
Browser ──▶ Flask (Port 8080)
             ├─ /login, /logout   Login-Gate (Session-Cookie)
             ├─ /settings         Paperless-URL + Token, Passwort ändern
             ├─ /                 Generator (unverändert) + Vorkonfig-Zeile
             └─ /api/*  ──▶  Proxy ──▶  Paperless (Token wird eingespritzt)
```

* Der Generator ruft `/api/...` **same-origin** auf → der Flask-Proxy leitet an das
  konfigurierte Paperless weiter und spritzt den gespeicherten Token ein.
  **Dadurch entfällt jede CORS-Konfiguration in Paperless.**
* Der **klassische Generator bleibt unverändert** — die Verbindungs-Vorbelegung wird nur
  zur Laufzeit in die ausgelieferte HTML-Seite injiziert.

---

## Manueller Betrieb (ohne Proxmox-Skript)

In diesem Ordner:

```bash
docker compose up -d --build
```

Läuft dann auf `http://localhost:8080`. Die Konfiguration (URL, Token, Passwort-Hash)
liegt im Volume `./config/config.json`.

## Generator-HTML aktualisieren

Nach einem neuen Generator-Build (`python build.py` im `Generator-Build`-Projekt):

```powershell
.\update.ps1        # kopiert dist/index.html -> site/ und baut das Image neu
```

---

## Sicherheitshinweis

Der Paperless-Token wird serverseitig im `config`-Volume gespeichert (Klartext), damit der
Proxy ihn einspritzen kann. Gedacht für den Betrieb im eigenen **Heimnetz**. Das Standard-
Passwort (`admin`) unbedingt nach dem ersten Login ändern.

## Lizenz

CC BY-NC 4.0 — siehe [LICENSE](LICENSE).
