"""Tests des Fristen-Kalenders (/fristen.ics): Token-Gate, ICS-Format, Filter, Zwischenspeicher.

Kein Netzwerk: requests.get wird durch eine Attrappe ersetzt, die Paperless nachstellt.
Start wie die anderen Tests (venv mit Abhängigkeiten aus app/requirements.txt).
"""
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["CONFIG_DIR"] = tempfile.mkdtemp(prefix="portal-ics-")
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


A._log_activity = lambda *a, **k: None
TOK = "IcsToken_123456"
PID = "aaaa1111"
today = date.today()
iso = lambda d: d.isoformat()  # noqa: E731
CALLS = []


class Resp:
    status_code = 200

    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


def fake_get(url, params=None, **kw):
    CALLS.append((url, params, kw))
    assert kw.get("allow_redirects") is False
    if "/api/custom_fields/" in url:
        return Resp({"results": [{"id": 7, "name": (params or {}).get("name__iexact")}]})
    return Resp({"next": None, "results": [
        {"id": 1, "title": "Rechnung; Strom, Okt", "custom_fields": [{"field": 7, "value": iso(today + timedelta(days=5))}]},
        {"id": 2, "title": "Zu alt", "custom_fields": [{"field": 7, "value": iso(today - timedelta(days=200))}]},
        {"id": 3, "title": "Anderes Feld", "custom_fields": [{"field": 9, "value": iso(today + timedelta(days=3))}]},
        {"id": 4, "title": "Kein Datum", "custom_fields": [{"field": 7, "value": None}]},
        {"id": 5, "title": "Ä" * 60, "custom_fields": [{"field": 7, "value": iso(today + timedelta(days=1))}]},
    ]})


A.requests.get = fake_get
A.requests.post = lambda *a, **k: (_ for _ in ()).throw(AssertionError("kein POST erlaubt"))

cfg = A.init_config()
cfg["is_default_pw"] = False
cfg["ics"] = {"enabled": True, "token": TOK}
cfg["active_profile"] = PID
A.save_config(cfg)
with open(A.PROFILES_PATH, "w", encoding="utf-8") as fh:
    json.dump({PID: {"name": "Zuhause", "paperless_url": "http://192.168.10.200:8000",
                     "generator_config": {"fristConfigs": [{"dateField": "Fälligkeitsdatum"},
                                                           {"dateField": "fälligkeitsdatum"}, {}]}}}, fh)

c = A.app.test_client()
check("ohne Token 404", c.get("/fristen.ics").status_code == 404)
check("falsches Token 404", c.get("/fristen.ics?token=nope").status_code == 404)
check("Token mit Umlaut wirft nicht (404)", c.get("/fristen.ics?token=%C3%A4").status_code == 404)
check("ohne Netz-Aufruf bei falschem Token", not CALLS)

r = c.get("/fristen.ics?token=" + TOK)
check("mit Token 200", r.status_code == 200)
check("Content-Type calendar", r.mimetype == "text/calendar")
txt = r.get_data(as_text=True)
check("CRLF-Zeilenenden", "\r\n" in txt and txt.endswith("END:VCALENDAR\r\n"))
check("zwei Termine (alt, fremdes Feld und leer fehlen)", txt.count("BEGIN:VEVENT") == 2)
BS = chr(92)
check("Komma und Semikolon escaped", ("Rechnung" + BS + "; Strom" + BS + ", Okt") in txt)
check("Ganztag mit Endtag", "DTSTART;VALUE=DATE:" + (today + timedelta(days=5)).strftime("%Y%m%d") in txt
      and "DTEND;VALUE=DATE:" + (today + timedelta(days=6)).strftime("%Y%m%d") in txt)
check("Link auf das Dokument", "http://192.168.10.200:8000/documents/1/details" in txt)
check("keine Zeile über 75 Oktette",
      all(len(l.encode("utf-8")) <= 75 for l in txt.split("\r\n")))
check("Datumsfeld nur einmal abgefragt (Duplikat, leer ignoriert)",
      sum(1 for u, _, _ in CALLS if "/api/custom_fields/" in u) == 1)
check("nur GET-Abfragen auf Paperless", all("/api/" in u for u, _, _ in CALLS))

n = len(CALLS)
c.get("/fristen.ics?token=" + TOK)
check("zweiter Abruf kommt aus dem Zwischenspeicher", len(CALLS) == n)

# Aus -> 404, Token neu -> alte URL ungültig
cfg = A.load_config()
cfg["ics"] = {"enabled": False, "token": TOK}
A.save_config(cfg)
check("ausgeschaltet 404", c.get("/fristen.ics?token=" + TOK).status_code == 404)

# Einstellungsroute (mit Login-Sitzung)
with c.session_transaction() as s:
    s["logged_in"] = True
    s["sid"] = "x"
A._logged_in = lambda: True
A._setup_complete = lambda: True
r = c.post("/verwaltung/kalender", data={"ics_enabled": "1"})
check("Einschalten speichert Zustand und behält Token", A._ics_cfg()["enabled"] and A._ics_cfg()["token"] == TOK)
old = A._ics_cfg()["token"]
c.post("/verwaltung/kalender", data={"ics_enabled": "1", "action": "ics_token_new"})
check("Token neu erzeugt", A._ics_cfg()["token"] != old)
check("alte URL ungültig", c.get("/fristen.ics?token=" + old).status_code == 404)

# Paperless nicht erreichbar -> 502 ohne alten Stand
A._ics_cache.clear()
A.requests.get = lambda *a, **k: (_ for _ in ()).throw(A.requests.ConnectionError("down"))
check("Paperless down 502", c.get("/fristen.ics?token=" + A._ics_cfg()["token"]).status_code == 502)

check("next auf fremden Host verworfen",
      A._ics_same_origin("http://evil.example/api/documents/?page=2", "https://p.lan") is None)
check("next http auf https angeglichen",
      A._ics_same_origin("http://p.lan/api/documents/?page=2", "https://p.lan") == "https://p.lan/api/documents/?page=2")
check("API-Token darf Kalender nicht umstellen", "ics_settings" in A._API_TOKEN_FORBIDDEN)

print("\n%d Prüfungen, %d Fehler" % (_count[0], len(_fails)))
for f in _fails:
    print("  - " + f)
sys.exit(1 if _fails else 0)
