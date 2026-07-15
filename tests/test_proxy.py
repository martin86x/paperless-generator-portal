"""Tests des /api/-Reverse-Proxy — Lösch-Riegel, Token-Einspritzung, Ziel-URL, readonly.

SICHERHEIT: Der Upstream ist gestubbt. Es geht KEIN Request ins Netz — insbesondere wird
niemals wirklich etwas gelöscht. Geprüft wird ausschliesslich, was der Proxy weiterreichen
WÜRDE.

Braucht die Abhängigkeiten aus app/requirements.txt (Flask & Co.) — im System-Python
fehlen die, also einmalig ein venv anlegen:

    python -m venv .venv
    .venv/Scripts/pip install -r app/requirements.txt
    .venv/Scripts/python tests/test_proxy.py
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_CFG = tempfile.mkdtemp(prefix="portal-proxy-")
os.environ["CONFIG_DIR"] = _CFG
os.environ["SITE_DIR"] = os.path.join(os.path.dirname(_HERE), "site")
os.environ["PORTAL_WATCHER"] = "0"
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "app"))

import app as A  # noqa: E402


# ── Testgerüst ───────────────────────────────────────────────────────────────
_fails = []
_count = [0]


def check(name, cond):
    _count[0] += 1
    if not cond:
        _fails.append(name)
        print("  FAIL  " + name)


def eq(name, got, want):
    check("%s (erwartet %r, war %r)" % (name, want, got), got == want)


FWD = []          # was der Proxy an den Upstream geschickt HÄTTE


class FakeUp:
    status_code = 200
    content = b'{"ok":true}'
    headers = {"Content-Type": "application/json"}


def _fake_request(method, url, **kw):
    FWD.append({"method": method, "url": url, "headers": kw.get("headers") or {},
                "params": kw.get("params"), "data": kw.get("data"),
                "allow_redirects": kw.get("allow_redirects")})
    return FakeUp()


A.requests.request = _fake_request
A._log_activity = lambda *a, **k: None

PID = "p1"
TOKEN = "geheimes-paperless-token"
BASE = "http://192.168.10.200:8000"
cfg = A.init_config()
cfg["is_default_pw"] = False
cfg["active_profile"] = PID
A.save_config(cfg)
A._cfg0 = cfg
A.save_profiles({PID: {"name": "Alpha", "paperless_url": BASE,
                       "paperless_token": TOKEN, "generator_config": {}}})
A.app.config["TESTING"] = True
C = A.app.test_client()
with C.session_transaction() as s:
    s["logged_in"] = True
    s["active_profile"] = PID


def call(method, path, **kw):
    del FWD[:]
    r = C.open(path, method=method, **kw)
    return r.status_code, (FWD[0] if FWD else None)


def set_flag(**flags):
    profs = A.load_profiles()
    profs[PID].update(flags)
    A.save_profiles(profs)


# ── 1. Lösch-Riegel: DELETE ──────────────────────────────────────────────────
print("Lösch-Riegel: DELETE")
code, fwd = call("DELETE", "/api/documents/5/")
eq("DELETE auf ein Dokument -> 403", code, 403)
check("und NICHTS wurde weitergereicht", fwd is None)
eq("DELETE ohne Schrägstrich am Ende ebenso", call("DELETE", "/api/documents/5")[0], 403)
eq("DELETE auf die Liste ebenso", call("DELETE", "/api/documents/")[0], 403)
code, fwd = call("DELETE", "/api/tags/5/")
eq("DELETE auf einen Tag ist erlaubt (nur Dokumente sind gesperrt)", code, 200)
check("und geht durch", fwd is not None)

# ── 2. Lösch-Riegel: bulk_edit ───────────────────────────────────────────────
print("Lösch-Riegel: bulk_edit")
BULK = "/api/documents/bulk_edit/"


def bulk(body, ct="application/json"):
    if ct == "application/json":
        return call("POST", BULK, data=json.dumps(body), content_type=ct)
    return call("POST", BULK, data=body, content_type=ct)


eq("method 'delete' -> 403", bulk({"documents": [1], "method": "delete"})[0], 403)
eq("method 'delete_documents' -> 403",
   bulk({"documents": [1], "method": "delete_documents"})[0], 403)
eq("Grossschreibung 'DELETE' -> 403", bulk({"documents": [1], "method": "DELETE"})[0], 403)
eq("gemischt 'DeLeTe' -> 403", bulk({"documents": [1], "method": "DeLeTe"})[0], 403)
eq("ohne Schrägstrich am Ende -> 403",
   call("POST", "/api/documents/bulk_edit", data=json.dumps({"method": "delete"}),
        content_type="application/json")[0], 403)
code, fwd = bulk({"documents": [1], "method": "modify_tags",
                  "parameters": {"add_tags": [2], "remove_tags": []}})
eq("modify_tags ist erlaubt", code, 200)
check("und geht durch", fwd is not None)

print("Lösch-Riegel: Umgehungsversuche")
# Der Proxy reicht den Rumpf unverändert weiter — der Riegel muss also denselben
# Byte-Strom beurteilen wie Paperless, nicht nur sauber deklariertes JSON.
eq("Leerzeichen im Verfahren (' delete') wird geblockt",
   bulk({"documents": [1], "method": " delete"})[0], 403)
eq("Tabs/Umbrüche im Verfahren ebenso",
   bulk({"documents": [1], "method": "\tdelete\n"})[0], 403)
eq("formular-kodiert wird geblockt",
   call("POST", BULK, data="documents=1&method=delete",
        content_type="application/x-www-form-urlencoded")[0], 403)
eq("formular-kodiert mit Leerzeichen ebenso",
   call("POST", BULK, data="documents=1&method=%20delete",
        content_type="application/x-www-form-urlencoded")[0], 403)
eq("JSON-Rumpf, als text/plain deklariert, wird geblockt",
   call("POST", BULK, data=json.dumps({"documents": [1], "method": "delete"}),
        content_type="text/plain")[0], 403)
eq("JSON-Rumpf ganz ohne Content-Type wird geblockt",
   call("POST", BULK, data=json.dumps({"documents": [1], "method": "delete"}))[0], 403)
eq("delete_documents formular-kodiert ebenso",
   call("POST", BULK, data="method=delete_documents",
        content_type="application/x-www-form-urlencoded")[0], 403)

print("Lösch-Riegel: fail-closed")
eq("unlesbarer Rumpf -> gesperrt, nicht durchgewinkt",
   call("POST", BULK, data="\x00\xff kein sinnvoller rumpf",
        content_type="application/json")[0], 403)
eq("leerer Rumpf -> gesperrt", call("POST", BULK, data="")[0], 403)
eq("Rumpf ohne 'method' -> gesperrt",
   bulk({"documents": [1], "parameters": {}})[0], 403)
eq("JSON-Liste statt Objekt -> gesperrt",
   call("POST", BULK, data="[1,2,3]", content_type="application/json")[0], 403)

print("Lösch-Riegel: erlaubte Verfahren kommen weiterhin durch")
for meth in ("modify_tags", "set_correspondent", "set_document_type", "set_storage_path",
             "add_tag", "remove_tag", "merge"):
    code, fwd = bulk({"documents": [1], "method": meth, "parameters": {}})
    eq("  %s ist erlaubt" % meth, code, 200)
code, fwd = bulk({"documents": [1], "method": "modify_tags",
                  "parameters": {"add_tags": [2], "remove_tags": []}})
check("der Rumpf wird unverändert weitergereicht",
      fwd and b"modify_tags" in (fwd["data"] if isinstance(fwd["data"], bytes)
                                 else fwd["data"].encode()))

# ── 3. Token-Einspritzung ────────────────────────────────────────────────────
print("Token")
code, fwd = call("GET", "/api/documents/?page_size=1")
eq("normaler GET geht durch", code, 200)
eq("Token wird serverseitig eingespritzt", fwd["headers"].get("Authorization"), "Token " + TOKEN)
code, fwd = call("GET", "/api/documents/", headers={"Authorization": "Token BOESE"})
eq("mitgeschicktes Authorization wird ERSETZT, nicht übernommen",
   fwd["headers"].get("Authorization"), "Token " + TOKEN)
code, fwd = call("GET", "/api/documents/", headers={"Cookie": "session=abc"})
check("Cookie wird nicht weitergereicht (Portal-Sitzung leckt nicht nach Paperless)",
      not any(k.lower() == "cookie" for k in fwd["headers"]))
check("Host-Header wird nicht weitergereicht",
      not any(k.lower() == "host" for k in fwd["headers"]))
eq("Accept-Encoding wird auf sicher Dekodierbares gesetzt",
   fwd["headers"].get("Accept-Encoding"), "gzip, deflate")
r = C.get("/api/documents/")
check("Token taucht NICHT in der Antwort an den Browser auf",
      TOKEN.encode() not in r.get_data())

# ── 4. Ziel-URL — kein Ausbruch auf fremde Hosts ─────────────────────────────
print("Ziel-URL")
code, fwd = call("GET", "/api/documents/")
eq("Ziel ist die konfigurierte Instanz", fwd["url"], BASE + "/api/documents/")
check("Redirects werden NICHT verfolgt (sonst Umleitung auf fremden Host)",
      fwd["allow_redirects"] is False)
for evil, label in [
    ("/api/../../etc/passwd", "Pfad-Ausbruch mit .."),
    ("/api/..%2f..%2fetc", "Pfad-Ausbruch URL-kodiert"),
    ("/api/@evil.example.com/x", "@-Trick"),
    ("/api//evil.example.com/x", "doppelter Schrägstrich"),
    ("/api/http://evil.example.com/", "absolute URL im Pfad"),
]:
    code, fwd = call("GET", evil)
    url = (fwd or {}).get("url") or ""
    escaped = bool(url) and not url.startswith(BASE + "/api")
    check("%s bricht nicht aus (%s)" % (label, url[:60] or "gar nicht weitergereicht"),
          not escaped)

# ── 5. readonly-Profil ───────────────────────────────────────────────────────
print("readonly-Profil")
set_flag(readonly=True)
eq("POST auf ein readonly-Profil -> 403", call("POST", "/api/tags/")[0], 403)
eq("PUT ebenso", call("PUT", "/api/tags/1/")[0], 403)
eq("PATCH ebenso", call("PATCH", "/api/tags/1/")[0], 403)
eq("DELETE ebenso", call("DELETE", "/api/tags/1/")[0], 403)
code, fwd = call("GET", "/api/documents/")
eq("GET bleibt erlaubt", code, 200)
check("und geht durch", fwd is not None)
set_flag(readonly=False)
eq("ohne readonly ist POST wieder erlaubt", call("POST", "/api/tags/")[0], 200)

# ── 6. Fehlende Konfiguration ────────────────────────────────────────────────
print("Fehlende Konfiguration")
profs = A.load_profiles()
profs[PID]["paperless_url"] = ""
A.save_profiles(profs)
code, fwd = call("GET", "/api/documents/")
eq("ohne Instanz-URL -> 503 statt Absturz", code, 503)
check("und nichts wird weitergereicht", fwd is None)

print("\n%d Prüfungen, %d Fehler" % (_count[0], len(_fails)))
for f in _fails:
    print("  - " + f)
sys.exit(1 if _fails else 0)
