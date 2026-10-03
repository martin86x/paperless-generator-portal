---
paths:
  - "app/app.py"
  - "app/inject.js"
  - "app/templates/**"
---

# Portal-Code (app/)

- Jede Änderung hier ist ein Portal-Release: `app/VERSION` hochzählen, Commit `Portal vX.Y.Z: <Kurzbeschreibung>`.
- Nach Änderungen an `app.py` die betroffenen Skripte unter `tests/` laufen lassen (`.venv\Scripts\python tests\test_<bereich>.py`), vor einem Release alle.
- Sicherheits-Invarianten bleiben erhalten: Login-Gate (`require_login`) vor allem außer `PUBLIC_ENDPOINTS`, CSRF-Origin-Check für zustandsändernde Requests (nur `/api` ist ausgenommen), Login-Rate-Limit, Recovery-Codes nur als Hash, Token-Verschlüsselung at-rest, kein Token im ausgelieferten HTML oder in Logs.
- Ein neuer Endpunkt kommt nur dann in `PUBLIC_ENDPOINTS`, wenn er sich selbst schützt (wie `metrics_endpoint` per Bearer-Token). `TRUST_PROXY` bleibt opt-in.
- `/api/*` leitet nur an das konfigurierte Paperless des aktiven Profils weiter; das Ziel darf nie aus der Anfrage stammen.
- Der Container läuft mit zwei gunicorn-Workern: Zustand, den beide sehen müssen, gehört in Dateien unter `$CONFIG_DIR`, nicht in Modul-Variablen. Der Wächter läuft nur im Worker mit der Dateisperre.
- `fcntl` fehlt unter Windows — Code muss ohne die Sperre importierbar bleiben (lokale Tests).
- Der Generator (`site/index.html`) wird nie verändert; Anpassungen laufen ausschließlich über die Laufzeit-Injektion (`inject.js`).
- Texte in der Oberfläche sind deutsch.
