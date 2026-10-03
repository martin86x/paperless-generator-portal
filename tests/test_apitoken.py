"""Tests des Portal-API-Tokens — Erzeugen, Widerrufen, Rechte, Hash-Ablage.

Kein Netzwerk: der Paperless-Proxy-Upstream ist gestubbt (jeder Aufruf wuerde
mitgeschrieben), CONFIG_DIR zeigt auf ein Temp-Verzeichnis.

    .venv\\Scripts\\python tests\\test_apitoken.py
"""
import json
import os
import re
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_CFG = tempfile.mkdtemp(prefix="portal-apitoken-")
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


FWD = []
A.requests.request = lambda method, url, **kw: FWD.append((method, url)) or None
A._test_paperless = lambda url, tok: 200
A._api_count = lambda *a, **k: None
LOG = []
A._log_activity = lambda kind, msg, **k: LOG.append((kind, msg))

PW = "richtiges-passwort"
cfg = A.init_config()
cfg["is_default_pw"] = False
cfg["admin_pw_hash"] = A.generate_password_hash(PW)
cfg["active_profile"] = "p1"
A.save_config(cfg)
A._cfg0 = cfg
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


# ── 1. Erzeugen ──────────────────────────────────────────────────────────────
print("Erzeugen")
b = browser()
r = b.post("/verwaltung/api-token/generate", data={"current": "falsch"}, headers=H)
check("falsches Passwort -> kein Token", "api_token" not in A.load_config())
r = b.post("/verwaltung/api-token/generate", data={"current": PW}, headers=H)
eq("richtiges Passwort -> Seite mit Token", r.status_code, 200)
m = re.search(r'id="api-token"[^>]*>(pgp_[A-Za-z0-9_-]+)<', r.data.decode("utf-8"))
check("Token wird einmal angezeigt", m is not None)
TOK = m.group(1) if m else ""
raw = open(A.CONFIG_PATH, encoding="utf-8").read()
check("Token liegt NICHT im Klartext in config.json", TOK and TOK not in raw)
eq("gespeichert ist der SHA-256-Hash", A.load_config()["api_token"]["hash"], A._api_token_hash(TOK))
page = b.get("/settings").data.decode("utf-8")
check("danach zeigt die Konto-Seite nur noch 'Aktiv'", "Aktiv" in page and TOK not in page)

# ── 2. Zugriff ───────────────────────────────────────────────────────────────
print("Zugriff mit Token")
A._login_fails_reset()
c = A.app.test_client()
eq("Verwaltung mit Token -> 200", c.get("/verwaltung/overview", headers=bearer(TOK)).status_code, 200)
eq("Profil-Liste mit Token -> 200", c.get("/portal/profiles.json", headers=bearer(TOK)).status_code, 200)
check("es entsteht KEIN Anmelde-Cookie", c.get_cookie("session") is None)
eq("ohne Token -> Login", A.app.test_client().get("/verwaltung/overview").status_code, 302)
eq("Token per URL zählt nicht", A.app.test_client().get(
    "/verwaltung/overview?token=" + TOK).status_code, 302)
r = c.post("/profiles", data={"name": "Neu per Token"}, headers=bearer(TOK))
eq("Schreiben im Portal erlaubt (Profil anlegen)", r.status_code, 302)
check("Profil wurde angelegt", any(p.get("name") == "Neu per Token" for p in A.load_profiles().values()))
check("auch nach dem Profilwechsel kein Cookie", c.get_cookie("session") is None)
_cfg = A.load_config(); _cfg["active_profile"] = "p1"; A.save_config(_cfg)  # Setup-Gate
check("und es steht im Protokoll", any(k == "api" and "POST /profiles" in m_ for k, m_ in LOG))
check("zuletzt genutzt wird mitgeschrieben", A._read_json_dict(A.API_TOKEN_USED_PATH).get("ts"))

print("Gesperrt mit Token")
for meth, path in [("GET", "/api/documents/"), ("POST", "/api/tags/"), ("GET", "/api/")]:
    del FWD[:]
    eq("%s %s -> 403" % (meth, path), c.open(path, method=meth, headers=bearer(TOK)).status_code, 403)
    check("  und nichts ging an Paperless", not FWD)
for path, data in [("/settings", {"current": PW, "new": "x1234", "repeat": "x1234"}),
                   ("/verwaltung/recovery/generate", {"current": PW}),
                   ("/verwaltung/api-token/generate", {"current": PW}),
                   ("/verwaltung/api-token/revoke", {}),
                   ("/verwaltung/config-restore", {}),
                   ("/anwenden", {"password": PW}),
                   ("/anwenden/undo", {"password": PW}),
                   ("/wizard", {"current": PW, "new": "x1234", "repeat": "x1234"})]:
    eq("POST %s -> 403" % path, c.post(path, data=data, headers=bearer(TOK)).status_code, 403)
check("Passwort unverändert", A.check_password_hash(A.load_config()["admin_pw_hash"], PW))
check("Token unverändert", A.load_config().get("api_token", {}).get("hash") == A._api_token_hash(TOK))

print("Falsches Token")
A._login_fails_reset()
eq("falsches Token -> 401", c.get("/verwaltung/overview", headers=bearer("pgp_falsch")).status_code, 401)
for _ in range(A.LOGIN_MAX):
    c.get("/verwaltung/overview", headers=bearer("pgp_falsch"))
eq("nach %d Fehlversuchen gesperrt, auch für das richtige" % A.LOGIN_MAX,
   c.get("/verwaltung/overview", headers=bearer(TOK)).status_code, 429)
A._login_fails_reset()

# ── 3. Widerrufen / neu erzeugen ─────────────────────────────────────────────
print("Widerrufen")
b = browser()
r = b.post("/verwaltung/api-token/generate", data={"current": PW}, headers=H)
TOK2 = re.search(r'(pgp_[A-Za-z0-9_-]+)<', r.data.decode("utf-8")).group(1)
eq("neues Token macht das alte ungültig",
   c.get("/verwaltung/overview", headers=bearer(TOK)).status_code, 401)
eq("das neue gilt", c.get("/verwaltung/overview", headers=bearer(TOK2)).status_code, 200)
A._login_fails_reset()
b.post("/verwaltung/api-token/revoke", headers=H)
eq("nach Widerruf -> 401", c.get("/verwaltung/overview", headers=bearer(TOK2)).status_code, 401)
check("kein Token mehr in config.json", "api_token" not in A.load_config())

print("\n%d Prüfungen, %d Fehler" % (_count[0], len(_fails)))
for f in _fails:
    print("  - " + f)
sys.exit(1 if _fails else 0)
