"""Funktionstest der Schreib-/Sende-Aktionen im Portal (alles lokal).

SICHERHEIT: Es geht KEIN Request ins Netz. requests ist gestubbt, die Config liegt in einem
Temp-Verzeichnis. Geprueft wird, ob die Knoepfe das tun, was sie versprechen, und ob der
Nutzer eine Rueckmeldung bekommt (Weiterleitung mit msg/err).

    .venv/Scripts/python tests/test_actions.py
"""
import json
import os
import re
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
os.environ["CONFIG_DIR"] = tempfile.mkdtemp(prefix="portal-actions-")
os.environ["SITE_DIR"] = os.path.join(_ROOT, "site")
os.environ["PORTAL_WATCHER"] = "0"
sys.path.insert(0, os.path.join(_ROOT, "app"))

import app as A  # noqa: E402

_fails = []
_count = [0]


def check(name, cond):
    _count[0] += 1
    if not cond:
        _fails.append(name)
        print("  FAIL  " + name)


def eq(name, got, want):
    check("%s (erwartet %r, war %r)" % (name, want, got), got == want)


NET = []
NET_FAIL = [False]


class Resp:
    def __init__(self, code=200):
        self.status_code = code
        self.headers = {}
        self.content = b"{}"
        self.text = "{}"

    def json(self):
        return {"count": 0, "results": [], "next": None}


def _net(*a, **kw):
    NET.append((a, kw))
    return Resp(500 if NET_FAIL[0] else 200)


A.requests.get = _net
A.requests.post = _net
A.requests.delete = _net
A.app.config["TESTING"] = True

P1, P2 = "9d5a263844e6fb1c", "1a2b3c4d5e6f7a8b"
H = {"Origin": "http://localhost"}


def reset():
    A.save_config({"admin_user": "admin", "admin_pw_hash": A.generate_password_hash("pw12345"),
                   "paperless_url": "", "paperless_token": "", "secret": "s3", "is_default_pw": False,
                   "active_profile": P1})
    A._cfg0.clear()
    A._cfg0.update(A.load_config())
    A.save_profiles({
        P1: {"name": "Eins", "paperless_url": "http://stub.invalid:8000", "paperless_token": "tok",
             "generator_config": {"types": [{"name": "A"}]}, "productive": False, "readonly": False, "color": ""},
        P2: {"name": "Zwei", "paperless_url": "http://stub2.invalid:8000", "paperless_token": "tok2",
             "generator_config": None, "productive": False, "readonly": False, "color": ""},
    })
    NET.clear()
    NET_FAIL[0] = False


def client():
    c = A.app.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
        s["sid"] = A._sessions_add()
        s["active_profile"] = P1
    return c


def post(c, path, **data):
    return c.post(path, data=data, headers=H, base_url="http://localhost")


def msg_of(r):
    loc = r.headers.get("Location", "")
    return loc, ("msg=" in loc), ("err=" in loc)


# ── Profile ──────────────────────────────────────────────────────────────────
print("Profile: umbenennen, Flags, Verbindung, Wächter, Aktivieren, Löschen")
reset()
c = client()
post(c, "/profiles/%s/rename" % P2, name="Neu benannt")
eq("umbenennen", A.load_profiles()[P2]["name"], "Neu benannt")
r = post(c, "/profiles/%s/flags" % P2, productive="1", readonly="1", color="#123456")
p = A.load_profiles()[P2]
check("Flags gespeichert", p["productive"] and p["readonly"] and p["color"] == "#123456")
check("Flags: Rückmeldung msg", msg_of(r)[1])
r = post(c, "/profiles/%s/connection" % P2, paperless_url="http://neu.invalid:8000/", paperless_token="neutok",
         notify_email="a@b.de")
p = A.load_profiles()[P2]
eq("Verbindung: URL ohne Schrägstrich", p["paperless_url"], "http://neu.invalid:8000")
eq("Verbindung: Mail in der Config", p["generator_config"]["notifyEmail"], "a@b.de")
check("Verbindung: Rückmeldung msg", msg_of(r)[1])
r = post(c, "/profiles/%s/activate" % P2)
r = c.get("/portal/profiles.json")
eq("Aktivieren: Profil ist aktiv", r.get_json()["active"], P2)
r = post(c, "/profiles/%s/delete" % P2)
check("Löschen: Profil weg", P2 not in A.load_profiles())
check("Löschen: Rückmeldung msg", msg_of(r)[1])
r = post(c, "/profiles/%s/delete" % P1)
check("letztes Profil lässt sich nicht löschen", P1 in A.load_profiles() and msg_of(r)[2])
r = post(c, "/profiles/nichtda/delete")
check("unbekanntes Profil: Fehlermeldung statt Absturz", msg_of(r)[2])
check("Löschen von Profilen berührt Paperless nicht", NET == [])

print("Konfigurations-Verlauf: speichern, Diff, Wiederherstellen")
reset()
c = client()
for types in ([{"name": "A"}], [{"name": "A"}, {"name": "B"}]):
    r = c.post("/portal/config", json={"types": types, "url": "x", "token": "y"}, headers=H,
               base_url="http://localhost")
    check("Speichern: ok", r.status_code == 200 and r.get_json()["ok"])
gc = A.load_profiles()[P1]["generator_config"]
check("url/token werden nicht gespeichert", "url" not in gc and "token" not in gc)
hist = A._list_history(P1)
check("Verlauf hat Einträge", len(hist) >= 1)
r = c.get("/profiles/%s/history/%s/diff?embed=1" % (P1, hist[0]))
eq("Diff-Ansicht öffnet", r.status_code, 200)
r = post(c, "/profiles/%s/history/%s/restore" % (P1, hist[0]))
check("Wiederherstellen: Rückmeldung msg", msg_of(r)[1])
check("Wiederherstellen: Stand zurück", len(A.load_profiles()[P1]["generator_config"]["types"]) == 1)
r = post(c, "/profiles/%s/history/%s/restore" % (P1, "19990101-000000"))
check("Wiederherstellen unbekannter Stand: Fehlermeldung", msg_of(r)[2])
r = c.get("/profiles/%s/snapshots" % P1)
eq("Snapshots-Liste öffnet", r.status_code, 200)

print("Benachrichtigungen: speichern, Test senden (gestubbt)")
reset()
c = client()
r = post(c, "/verwaltung/benachrichtigungen", action="save", ntfy_enabled="1", ntfy_topic="mein-test",
         ntfy_server="https://ntfy.invalid")
check("Speichern: Rückmeldung msg", msg_of(r)[1])
eq("ntfy gespeichert", A.load_profiles()[P1]["notifications"]["ntfy"]["topic"], "mein-test")
NET.clear()
r = post(c, "/verwaltung/benachrichtigungen", action="test_ntfy", ntfy_enabled="1", ntfy_topic="mein-test",
         ntfy_server="https://ntfy.invalid")
check("Test senden: Rückmeldung 'gesendet'", "gesendet" in A.unquote(r.headers["Location"]) if hasattr(A, "unquote")
      else "gesendet" in __import__("urllib.parse").parse.unquote(r.headers["Location"]))
check("Test senden: genau ein Aufruf an den ntfy-Server", len(NET) == 1 and "ntfy.invalid" in NET[0][0][0])
NET_FAIL[0] = True
r = post(c, "/verwaltung/benachrichtigungen", action="test_ntfy", ntfy_enabled="1", ntfy_topic="mein-test",
         ntfy_server="https://ntfy.invalid")
check("Test senden bei Fehler: Fehlermeldung", msg_of(r)[2])
NET_FAIL[0] = False
r = post(c, "/verwaltung/benachrichtigungen", action="test_pushover")
check("Test für nicht aktivierten Kanal: Fehlermeldung", msg_of(r)[2])

print("Wächter: speichern, Prüflauf, Digest, Webhook (gestubbt)")
reset()
c = client()
r = post(c, "/verwaltung/waechter", action="save", enabled="1", interval_min="30", chk_downtime="1")
check("Speichern: Rückmeldung msg", msg_of(r)[1])
eq("Intervall gespeichert", A._watcher_cfg()["interval_min"], 30)
r = post(c, "/verwaltung/waechter", action="run_now", enabled="1", interval_min="30", chk_downtime="1")
check("Prüflauf: Rückmeldung msg", msg_of(r)[1])
r = post(c, "/verwaltung/waechter", action="digest_now", enabled="1", interval_min="30")
check("Digest: Rückmeldung", msg_of(r)[1] or msg_of(r)[2])
r = post(c, "/verwaltung/waechter", action="webhook_now", webhook_enabled="1", webhook_url="https://hook.invalid/x")
check("Webhook-Test: Rückmeldung", msg_of(r)[1] or msg_of(r)[2])
r = post(c, "/verwaltung/waechter", action="metrics_token_new", metrics_enabled="1")
check("Metrik-Token: Rückmeldung", msg_of(r)[1] or msg_of(r)[2])

print("Update-Knöpfe (schreiben nur die Anforderungsdatei in die Temp-Config)")
reset()
c = client()
r = post(c, "/verwaltung/update/trigger", action="update")
req = json.load(open(A.UPDATE_REQUEST, encoding="utf-8"))
eq("Update angefordert", req["action"], "update")
check("Update: Rückmeldung msg", msg_of(r)[1])
r = post(c, "/verwaltung/update/trigger", action="rollback")
eq("Rollback angefordert", json.load(open(A.UPDATE_REQUEST, encoding="utf-8"))["action"], "rollback")
r = post(c, "/update", lxc_id="230")
eq("Container-ID gespeichert", A.load_config().get("lxc_id"), "230")
r = post(c, "/update", lxc_id="abc")
check("Container-ID ungültig: Fehlermeldung", msg_of(r)[2])
check("Update-Knöpfe berühren das Netz nicht", NET == [])

print("Einrichtung: Status wandert mit den Aktionen")
reset()
c = client()
page = c.get("/verwaltung/einrichtung?embed=1").data.decode("utf-8")
m = re.search(r"(\d+) von (\d+) Pflichtschritten", page)
before = int(m.group(1))
post(c, "/verwaltung/waechter", action="save", enabled="1", interval_min="30", chk_downtime="1")
post(c, "/verwaltung/benachrichtigungen", action="save", ntfy_enabled="1", ntfy_topic="t")
page = c.get("/verwaltung/einrichtung?embed=1").data.decode("utf-8")
after = int(re.search(r"(\d+) von (\d+) Pflichtschritten", page).group(1))
check("Wächter + Kanal erledigen zwei Schritte mehr (%d -> %d)" % (before, after), after >= before + 2)

print("\n%d Prüfungen, %d Fehler" % (_count[0], len(_fails)))
for f in _fails:
    print("  - " + f)
sys.exit(1 if _fails else 0)
