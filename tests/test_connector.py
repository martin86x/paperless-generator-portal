"""Tests Konnektor: Paperless-Schalter, Lampe, Status, Update per Token, MCP-Server.

Kein Netzwerk nach aussen: der Paperless-Upstream ist gestubbt, das Portal laeuft fuer den
MCP-Teil lokal auf 127.0.0.1 (Zufallsport), CONFIG_DIR zeigt auf ein Temp-Verzeichnis.

    .venv\\Scripts\\python tests\\test_connector.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_CFG = tempfile.mkdtemp(prefix="portal-connector-")
os.environ["CONFIG_DIR"] = _CFG
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


class _Up:
    status_code = 200
    headers = {"Content-Type": "application/json"}
    content = b'{"results": []}'


FWD = []
A.requests.request = lambda method, url, **kw: FWD.append((method, url)) or _Up()
A._test_paperless = lambda url, tok: 200
A._api_count = lambda *a, **k: None
A._fetch_latest_version = lambda: None  # kein GitHub
LOG = []
A._log_activity = lambda kind, msg, **k: LOG.append((kind, msg))

PW = "richtiges-passwort"
cfg = A.init_config()
cfg["is_default_pw"] = False
cfg["admin_pw_hash"] = A.generate_password_hash(PW)
cfg["active_profile"] = "p1"
A.save_config(cfg)
A.save_profiles({"p1": {"name": "Alpha", "paperless_url": "http://x:8000",
                        "paperless_token": "platzhalter-token"}})
A.app.config["TESTING"] = True
H = {"Origin": "http://localhost"}


def browser():
    c = A.app.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
        s["sid"] = A._sessions_add()
    return c


def bearer(tok):
    return {"Authorization": "Bearer " + tok}


def toggle(c, on, headers=H):
    return c.post("/portal/connector/paperless", json={"on": on}, headers=headers)


b = browser()
c = A.app.test_client()

# ── 1. Ohne Token ────────────────────────────────────────────────────────────
print("Ohne Token")
d = b.get("/portal/connector.json").get_json()
eq("kein Token", d["token"], False)
eq("Lampe aus", d["online"], False)
eq("Schalter aus", d["paperless"], False)
eq("Einschalten ohne Token -> 409", toggle(b, True).status_code, 409)
eq("connector.json ohne Login -> 401", A.app.test_client().get("/portal/connector.json").status_code, 401)

r = b.post("/verwaltung/api-token/generate", data={"current": PW}, headers=H)
TOK = re.search(r'(pgp_[A-Za-z0-9_-]+)<', r.data.decode("utf-8")).group(1)

# ── 2. Schalter AUS ──────────────────────────────────────────────────────────
print("Schalter AUS")
d = b.get("/portal/connector.json").get_json()
check("Token da, Lampe noch aus", d["token"] and not d["online"])
del FWD[:]
eq("Token auf /api -> 403", c.get("/api/documents/", headers=bearer(TOK)).status_code, 403)
check("  nichts ging an Paperless", not FWD)
d = b.get("/portal/connector.json").get_json()
eq("nach Token-Nutzung Lampe an", d["online"], True)

# ── 3. Schalter nur mit Sitzung ──────────────────────────────────────────────
print("Schalter umlegen")
eq("Token darf den Schalter nicht umlegen", toggle(c, True, bearer(TOK)).status_code, 403)
eq("  Schalter weiter aus", A.load_config().get("api_token_paperless", False), False)
eq("fremder Origin -> 403", toggle(b, True, {"Origin": "http://boese.example"}).status_code, 403)
eq("kaputter Rumpf -> 400", b.post("/portal/connector/paperless", json={"on": "ja"},
                                    headers=H).status_code, 400)
del LOG[:]
r = toggle(b, True)
eq("Sitzung schaltet ein (ohne Passwort)", r.status_code, 200)
eq("  Antwort zeigt AN", r.get_json()["paperless"], True)
check("  steht im Protokoll", any("Paperless-Zugriff" in m and "EIN" in m for _, m in LOG))

# ── 4. Schalter AN ───────────────────────────────────────────────────────────
print("Schalter AN")
del FWD[:]
eq("Token liest Paperless", c.get("/api/documents/?page_size=1", headers=bearer(TOK)).status_code, 200)
check("  ging an das Profil-Paperless", FWD and FWD[-1][1].startswith("http://x:8000/api/documents/"))
eq("Token legt Tag an", c.post("/api/tags/", json={"name": "t"}, headers=bearer(TOK)).status_code, 200)
check("  Schreiben wird protokolliert", any("POST /api/tags/" in m for _, m in LOG))
check("kein Cookie für den Token-Client", c.get_cookie("session") is None)

print("Löschen bleibt gesperrt")
for meth, path, body in [
        ("DELETE", "/api/documents/5/", None),
        ("POST", "/api/documents/bulk_edit/", {"documents": [5], "method": "delete"}),
        ("POST", "/api/documents/bulk_edit/", {"documents": [5], "method": "delete_pages",
                                               "parameters": {"pages": [1]}}),
        ("POST", "/api/documents/bulk_edit/", {"documents": [5, 6], "method": "merge",
                                               "parameters": {"delete_originals": True}}),
        ("POST", "/api/trash/", {"action": "empty"})]:
    del FWD[:]
    eq("%s %s %s -> 403" % (meth, path, (body or {}).get("method") or (body or {}).get("action") or ""),
       c.open(path, method=meth, json=body, headers=bearer(TOK)).status_code, 403)
    check("  nichts ging an Paperless", not FWD)

print("Nur-lesen-Profil")
profs = A.load_profiles(); profs["p1"]["readonly"] = True; A.save_profiles(profs)
eq("Schreiben -> 403", c.post("/api/tags/", json={"name": "t"}, headers=bearer(TOK)).status_code, 403)
eq("Lesen geht", c.get("/api/tags/", headers=bearer(TOK)).status_code, 200)
profs["p1"]["readonly"] = False; A.save_profiles(profs)

print("Gesperrte Portal-Wege bleiben gesperrt")
_before = json.dumps(A.load_profiles(), sort_keys=True)
for meth, path in [("POST", "/settings"), ("POST", "/wizard"),
                   ("GET", "/logout"),
                   ("POST", "/verwaltung/recovery/generate"),
                   ("POST", "/verwaltung/api-token/generate"),
                   ("POST", "/verwaltung/api-token/revoke"),
                   ("POST", "/verwaltung/config-restore"),
                   ("POST", "/anwenden"), ("POST", "/anwenden/undo"),
                   ("GET", "/profiles/export"), ("GET", "/verwaltung/config-backup"),
                   ("POST", "/profiles/import"), ("POST", "/profiles/p1/connection"),
                   ("POST", "/profiles/p1/flags"), ("POST", "/profiles/p1/activate"),
                   ("POST", "/profiles/p1/delete"),
                   ("POST", "/verwaltung/benachrichtigungen"), ("POST", "/profiles"),
                   ("POST", "/verwaltung/waechter")]:
    r = c.open(path, method=meth, data={"current": PW, "paperless_url": "http://fremd:1"},
               headers=bearer(TOK))
    eq("%s %s -> 403" % (meth, path), r.status_code, 403)
    check("  kein Paperless-Token in der Antwort", b"platzhalter" not in r.data)
eq("Profile unverändert", json.dumps(A.load_profiles(), sort_keys=True), _before)
check("Wächter-Einstellungen unverändert", "webhook_url" not in json.dumps(A.load_config().get("watcher") or {}))
eq("Benachrichtigungen lesen geht", c.get("/verwaltung/benachrichtigungen",
                                          headers=bearer(TOK)).status_code, 200)

# ── 5. Status + Update per Token ─────────────────────────────────────────────
print("Status und Update")
r = c.get("/portal/status.json", headers=bearer(TOK))
eq("status.json -> 200", r.status_code, 200)
st = r.get_json()
eq("Version", st["version"], A.PORTAL_VERSION)
eq("Schalter im Status", st["connector"]["paperless"], True)
eq("aktives Profil", st["active_profile"]["name"], "Alpha")
check("keine Paperless-URL/Token im Status", "x:8000" not in r.data.decode() and
      "platzhalter" not in r.data.decode())
r = c.post("/verwaltung/update/trigger", data={"action": "rollback"}, headers=bearer(TOK))
eq("Update-Anforderung per Token -> JSON 200", r.status_code, 200)
eq("  action", r.get_json().get("action"), "rollback")
eq("  Anforderungsdatei", json.load(open(A.UPDATE_REQUEST, encoding="utf-8"))["action"], "rollback")
os.remove(A.UPDATE_REQUEST)
r = b.post("/verwaltung/update/trigger", data={"action": "update"}, headers=H)
eq("im Browser weiter Redirect", r.status_code, 302)
os.remove(A.UPDATE_REQUEST)

# ── 6. Ausschalten, Token neu ────────────────────────────────────────────────
print("Ausschalten")
toggle(b, False)
eq("nach AUS wieder 403", c.get("/api/documents/", headers=bearer(TOK)).status_code, 403)
toggle(b, True)
r = b.post("/verwaltung/api-token/generate", data={"current": PW}, headers=H)
TOK = re.search(r'(pgp_[A-Za-z0-9_-]+)<', r.data.decode("utf-8")).group(1)
eq("neues Token startet mit Schalter AUS", A.load_config().get("api_token_paperless"), False)
toggle(b, True)
b.post("/verwaltung/api-token/revoke", headers=H)
eq("Widerruf setzt Schalter AUS", A.load_config().get("api_token_paperless"), False)
r = b.post("/verwaltung/api-token/generate", data={"current": PW}, headers=H)
TOK = re.search(r'(pgp_[A-Za-z0-9_-]+)<', r.data.decode("utf-8")).group(1)

print("Schalter Konnektor")


def enable(c, on, headers=H):
    return c.post("/portal/connector/enabled", json={"on": on}, headers=headers)


check("Standard: Konnektor AN (auch ohne Schlüssel in config.json)",
      "api_token_enabled" not in A.load_config() and b.get("/portal/connector.json").get_json()["enabled"])
eq("Token darf den Konnektor-Schalter nicht umlegen", enable(c, False, bearer(TOK)).status_code, 403)
eq("fremder Origin -> 403", enable(b, False, {"Origin": "http://boese.example"}).status_code, 403)
eq("kaputter Rumpf -> 400", b.post("/portal/connector/enabled", json={}, headers=H).status_code, 400)
toggle(b, True)
del LOG[:]
r = enable(b, False)
eq("Sitzung sperrt den Konnektor (ohne Passwort)", r.status_code, 200)
eq("  Antwort zeigt AUS", r.get_json()["enabled"], False)
eq("  Paperless ist mit aus", A.load_config().get("api_token_paperless"), False)
check("  steht im Protokoll", any(m == "Konnektor AUS" for _, m in LOG))
del FWD[:]
for meth, path in [("GET", "/portal/status.json"), ("GET", "/portal/connector.json"),
                   ("GET", "/verwaltung/overview"), ("POST", "/verwaltung/update/trigger"),
                   ("GET", "/api/documents/")]:
    eq("gesperrt: %s %s -> 403" % (meth, path),
       c.open(path, method=meth, data={"action": "update"}, headers=bearer(TOK)).status_code, 403)
check("  keine Update-Anforderung", not os.path.exists(A.UPDATE_REQUEST))
check("  nichts ging an Paperless", not FWD)
eq("falsches Token weiter 401", c.get("/portal/status.json", headers=bearer("pgp_falsch")).status_code, 401)
A._login_fails_reset()
eq("Paperless einschalten bei Konnektor AUS -> 409", toggle(b, True).status_code, 409)
eq("im Browser geht alles weiter", b.get("/portal/connector.json").status_code, 200)
r = enable(b, True)
check("wieder AN, Paperless bleibt AUS", r.get_json()["enabled"] and not r.get_json()["paperless"])
eq("Token wieder zugelassen", c.get("/portal/status.json", headers=bearer(TOK)).status_code, 200)
eq("Paperless weiter 403 bis zum Einschalten", c.get("/api/documents/", headers=bearer(TOK)).status_code, 403)

print("Host-Helper")
st = c.get("/portal/status.json", headers=bearer(TOK)).get_json()["update_helper"]
check("ohne Lebenszeichen nicht aktiv", not st["alive"] and st["last_seen"] is None)
open(A.UPDATE_HELPER_ALIVE, "w").close()
eq("frisches Lebenszeichen -> aktiv", c.get("/portal/status.json", headers=bearer(TOK)).get_json()
   ["update_helper"]["alive"], True)
old = time.time() - 80 * 86400
os.utime(A.UPDATE_HELPER_ALIVE, (old, old))
st = c.get("/portal/status.json", headers=bearer(TOK)).get_json()["update_helper"]
check("altes Lebenszeichen -> nicht aktiv, mit Datum", not st["alive"] and st["last_seen"])


def _no_net(*a, **k):
    raise A.requests.RequestException("kein Netz im Test")


A.requests.get = _no_net
page = b.get("/update").data.decode("utf-8")
check("Version-Seite meldet stehenden Helper", "meldet sich nicht mehr" in page)
os.remove(A.UPDATE_HELPER_ALIVE)

print("Lampe")
used = A._read_json_dict(A.API_TOKEN_USED_PATH)
with open(A.API_TOKEN_USED_PATH, "w", encoding="utf-8") as fh:
    json.dump({"ts": int(time.time()) - A.CONNECTOR_ONLINE_SECS - 5, "ip": "?"}, fh)
eq("alte Meldung -> Lampe aus", b.get("/portal/connector.json").get_json()["online"], False)

print("inject.js")
js = b.get("/portal/inject.js").data.decode("utf-8")
check("beide Schalter + Lampe im Generator",
      "plx-conn-toggle" in js and "plx-conn-enable" in js and "plx-conn-lamp" in js)
check("Text nur per textContent", "innerHTML = d." not in js)

# ── 7. MCP-Server ────────────────────────────────────────────────────────────
print("MCP-Server")
from werkzeug.serving import make_server  # noqa: E402
import logging  # noqa: E402

logging.getLogger("werkzeug").setLevel(logging.ERROR)

srv = make_server("127.0.0.1", 0, A.app, threaded=True)
threading.Thread(target=srv.serve_forever, daemon=True).start()
tokfile = os.path.join(_CFG, "token-datei")
with open(tokfile, "w", encoding="utf-8") as fh:
    fh.write(TOK + "\n")
env = dict(os.environ, PORTAL_URL="http://127.0.0.1:%d" % srv.server_port, PORTAL_TOKEN_FILE=tokfile)
os.remove(A.API_TOKEN_USED_PATH)
p = subprocess.Popen([sys.executable, os.path.join(_ROOT, "tools", "portal-mcp", "portal_mcp.py")],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)


def rpc(mid, method, params=None):
    msg = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        msg["params"] = params
    p.stdin.write((json.dumps(msg) + "\n").encode())
    p.stdin.flush()
    return json.loads(p.stdout.readline())


def call(mid, name, args=None):
    res = rpc(mid, "tools/call", {"name": name, "arguments": args or {}})["result"]
    return res["content"][0]["text"], res["isError"]


r = rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "test", "version": "0"}})
eq("initialize", r["result"]["serverInfo"]["name"], "paperless-portal")
p.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'); p.stdin.flush()
names = [t["name"] for t in rpc(2, "tools/list")["result"]["tools"]]
eq("Werkzeuge", names, ["portal_status", "portal_update", "portal_request", "paperless_request"])
check("Herzschlag beim Start -> Lampe an", b.get("/portal/connector.json").get_json()["online"])
txt, err = call(3, "portal_status")
check("portal_status ok", not err and '"version"' in txt)
check("Token nie in der Ausgabe", TOK not in txt)
txt, err = call(4, "paperless_request", {"path": "documents/"})
check("Paperless bei Schalter AUS -> Fehler 403", err and "403" in txt)
toggle(b, True)
del FWD[:]
txt, err = call(5, "paperless_request", {"path": "/api/documents/", "query": {"page_size": 1}})
check("Paperless bei Schalter AN -> 200", not err and txt.startswith("HTTP 200"))
check("  ging an Paperless", FWD)
del FWD[:]
txt, err = call(6, "paperless_request", {"method": "DELETE", "path": "documents/5/"})
check("Löschen über MCP -> 403", err and "403" in txt and not FWD)
txt, err = call(7, "portal_request", {"path": "/api/documents/"})
check("portal_request lässt /api nicht durch", err)
txt, err = call(8, "portal_request", {"method": "POST", "path": "/portal/connector/paperless",
                                      "json": {"on": False}})
check("Schalter über MCP -> 403", err and "403" in txt)
eq("  Schalter weiter AN", A.load_config().get("api_token_paperless"), True)
txt, err = call(9, "portal_update", {"action": "update"})
check("portal_update ok", not err and json.loads(txt.split("\n\n", 1)[1]).get("ok") is True)
os.remove(A.UPDATE_REQUEST)
eq("unbekannte Methode -> Fehler", rpc(10, "foo/bar").get("error", {}).get("code"), -32601)
p.stdin.close()
p.wait(timeout=10)
eq("beendet sich bei Ende der Eingabe", p.returncode, 0)
srv.shutdown()

print("\n%d Prüfungen, %d Fehler" % (_count[0], len(_fails)))
for f in _fails:
    print("  - " + f)
sys.exit(1 if _fails else 0)
