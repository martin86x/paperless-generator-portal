"""Tests der Notfall-Mappe (/notfall) und der Gesundheitszeilen im Report.

Kein Netzwerk (requests wird zur Falle). Prüft vor allem: keine Zugangsdaten auf der Seite,
Login nötig, keine Abfrage an Paperless.
"""
import json
import os
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["CONFIG_DIR"] = tempfile.mkdtemp(prefix="portal-notfall-")
os.environ["SITE_DIR"] = os.path.join(os.path.dirname(_HERE), "site")
os.environ["PORTAL_WATCHER"] = "0"
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "app"))

import app as A  # noqa: E402

_fails, _count = [], [0]


def check(name, cond):
    _count[0] += 1
    if not cond:
        _fails.append(name)
        print("  FAIL  " + name)


def _trap(*a, **k):
    raise AssertionError("Netz-Aufruf in der Notfall-Mappe!")


A.requests.get = _trap
A.requests.post = _trap
A._log_activity = lambda *a, **k: None

SECRET = "GEHEIMES_PAPERLESS_TOKEN_123"
cfg = A.init_config()
cfg["is_default_pw"] = False
A.save_config(cfg)
with open(A.PROFILES_PATH, "w", encoding="utf-8") as fh:
    json.dump({"aaaa1111": {
        "name": "Zuhause", "paperless_url": "http://192.168.10.200:8000", "paperless_token": SECRET,
        "generator_config": {
            "storagePaths": [{"name": "Rechnungen", "path": "Finanzen/{{ correspondent }}"}],
            "types": [{"name": "Vertrag"}, {"name": "Rechnung"}],
            "correspondents": [{"name": "<b>Bank</b>"}],
            "fristConfigs": [{"label": "Vertrag", "doctype": "Vertrag", "dateField": "Vertragsende"}]}}}, fh)

c = A.app.test_client()
r = c.get("/notfall")
check("ohne Login keine Seite", r.status_code in (301, 302, 401))

A._logged_in = lambda: True
A._setup_complete = lambda: True
r = c.get("/notfall")
html = r.get_data(as_text=True)
check("Seite 200", r.status_code == 200)
check("Instanz und Adresse", "Zuhause" in html and "http://192.168.10.200:8000" in html)
check("Ablageort, Typ, Frist erscheinen", "Finanzen/" in html and "Rechnung" in html and "Vertragsende" in html)
check("kein Token auf der Seite", SECRET not in html)
check("HTML in Namen wird escaped", "<b>Bank</b>" not in html and "&lt;b&gt;Bank" in html)
check("Backup 'noch nie'", "noch nie erstellt" in html)

# Gesundheitszeilen
L = A._health_lines()
check("ohne Backup: Hinweis", L[0] == "Letztes Backup: noch nie erstellt")
with open(A.LAST_BACKUP_PATH, "w", encoding="utf-8") as fh:
    json.dump({"ts": int(time.time()) - 3 * 86400}, fh)
check("Backup vor 3 Tagen", A._health_lines()[0] == "Letztes Backup: vor 3 Tag(en)")
with open(A.LAST_BACKUP_PATH, "w", encoding="utf-8") as fh:
    json.dump({"ts": int(time.time()) - 40 * 86400}, fh)
check("Backup älter als 30 Tage warnt", "neues Backup empfohlen" in A._health_lines()[0])
A._prom_update_cache = lambda: {"latest": "99.0.0"}
check("Update verfügbar", "Update verfügbar: 99.0.0" in A._health_lines()[1])
A._prom_update_cache = lambda: None
check("kein Update bekannt", "kein Update bekannt" in A._health_lines()[1])

print("\n%d Prüfungen, %d Fehler" % (_count[0], len(_fails)))
for f in _fails:
    print("  - " + f)
sys.exit(1 if _fails else 0)
