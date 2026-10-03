"""Tests fuer das Blanko-Profil (bestehende fremde Instanz, ohne Generator-Vorgaben).

SICHERHEIT: Es geht KEIN Request ins Netz; requests ist gestubbt.

    .venv/Scripts/python tests/test_blank.py
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_CFG = tempfile.mkdtemp(prefix="portal-blank-")
os.environ["CONFIG_DIR"] = _CFG
os.environ["SITE_DIR"] = os.path.join(os.path.dirname(_HERE), "site")
os.environ["PORTAL_WATCHER"] = "0"
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "app"))

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


class Resp:
    def __init__(self, code, body=None):
        self.status_code = code
        self._body = body if body is not None else {}
        self.headers = {}
        self.content = b"{}"
        self.text = "{}"

    def json(self):
        return self._body


NET = []


def _stub(*a, **kw):
    NET.append(a)
    return Resp(200, {"count": 0, "results": [], "next": None})


A.requests.get = _stub
A.requests.post = _stub
A.requests.delete = _stub

PID = "9d5a263844e6fb1c"
PW = "geheim123"
A.app.config["TESTING"] = True


def reset(**flags):
    A.save_config({"admin_user": "admin", "admin_pw_hash": A.generate_password_hash(PW),
                   "paperless_url": "", "paperless_token": "", "secret": "s3cr3t",
                   "is_default_pw": False, "active_profile": PID})
    A._cfg0.clear()
    A._cfg0.update(A.load_config())
    prof = {"name": "Fremd", "paperless_url": "http://stub.invalid:8000", "paperless_token": "tok",
            "generator_config": None, "productive": False, "readonly": False, "color": ""}
    prof.update(flags)
    A.save_profiles({PID: prof})


def client():
    c = A.app.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
        s["sid"] = A._sessions_add()
        s["active_profile"] = PID
    return c


def origin(c, path, **kw):
    return c.post(path, headers={"Origin": "http://localhost"}, base_url="http://localhost", **kw)


print("Anlegen")
reset()
c = client()
origin(c, "/profiles", data={"name": "Neu-Blanko", "blank": "1"})
profs = A.load_profiles()
new = [p for p in profs.values() if p.get("name") == "Neu-Blanko"]
eq("Blanko-Profil angelegt", len(new), 1)
check("blank gesetzt", new[0].get("blank") is True)
check("und standardmäßig nur lesen", new[0].get("readonly") is True)
reset()
c = client()
origin(c, "/profiles", data={"name": "Normal"})
nrm = [p for p in A.load_profiles().values() if p.get("name") == "Normal"][0]
check("normales Profil bleibt ohne Blanko/readonly", not nrm.get("blank") and not nrm.get("readonly"))

print("Flags")
reset()
c = client()
origin(c, "/profiles/%s/flags" % PID, data={"blank": "1"})
p = A.load_profiles()[PID]
check("Einschalten setzt readonly automatisch", p["blank"] is True and p["readonly"] is True)
origin(c, "/profiles/%s/flags" % PID, data={"blank": "1"})
check("(readonly war nicht angehakt, aber Blanko war schon an -> frei schaltbar)",
      A.load_profiles()[PID]["readonly"] is False)
origin(c, "/profiles/%s/flags" % PID, data={})
check("Ausschalten möglich", not A.load_profiles()[PID].get("blank"))

print("Anwenden gesperrt")
reset(blank=True, readonly=False, generator_config={"tags": [{"name": "X"}]})
c = client()
page = c.get("/anwenden").data.decode("utf-8")
check("GET /anwenden zeigt Blanko-Sperre", "Blanko" in page)
NET.clear()
r = origin(c, "/anwenden", data={"password": PW, "item": ["tags|X"]})
eq("POST /anwenden leitet um", r.status_code, 302)
eq("und schreibt nichts", NET, [])

print("Drift")
reset(blank=True, generator_config={"tags": [{"name": "X"}]})
d = client().get("/dashboard").data.decode("utf-8")
check("Dashboard: kein Drift-Abgleich", "Drift · Config" not in d)
line = A._profile_digest_line("http://stub.invalid:8000", "tok", {"tags": [{"name": "X"}]}, True)
check("Digest nennt keine Drift", "Drift" not in line and "fehlen" not in line)

print("Profil-Liste / Proxy")
reset(blank=True, readonly=True)
c = client()
eq("profiles.json meldet active_blank", c.get("/portal/profiles.json").get_json()["active_blank"], True)
NET.clear()
r = c.post("/api/tags/", data="{}", headers={"Origin": "http://localhost"})
eq("Schreiben über den Proxy gesperrt (nur lesen)", r.status_code, 403)
eq("Proxy hat nichts weitergeleitet", NET, [])
r = c.delete("/api/documents/1/")
eq("Dokument-Löschung bleibt gesperrt", r.status_code, 403)

print("inject.js")
js = open(os.path.join(os.path.dirname(_HERE), "app", "inject.js"), encoding="utf-8").read()
check("Blanko-Modus im Generator vorhanden", "applyBlankMode" in js and "emptyGeneratorLists" in js)

print("\n%d Prüfungen, %d Fehler" % (_count[0], len(_fails)))
for f in _fails:
    print("  - " + f)
sys.exit(1 if _fails else 0)
