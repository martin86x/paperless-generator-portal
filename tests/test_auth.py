"""Tests der Zugangs-Schicht — Login-Rate-Limit, Recovery-Codes, CSRF, Sitzungs-Cookie,
Token-Verschlüsselung at-rest.

Kein Netzwerk. CONFIG_DIR zeigt auf ein Temp-Verzeichnis.

Braucht die Abhängigkeiten aus app/requirements.txt (Flask & Co.) — im System-Python
fehlen die, also einmalig ein venv anlegen:

    python -m venv .venv
    .venv/Scripts/pip install -r app/requirements.txt
    .venv/Scripts/python tests/test_auth.py
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_CFG = tempfile.mkdtemp(prefix="portal-auth-")
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


A._log_activity = lambda *a, **k: None
PW = "richtiges-passwort"
cfg = A.init_config()
cfg["is_default_pw"] = False
cfg["admin_pw_hash"] = A.generate_password_hash(PW)
cfg["active_profile"] = "p1"
A.save_config(cfg)
A._cfg0 = cfg
# Zweites Gate: ohne aktives Profil MIT URL+Token schickt require_setup jede Seite auf
# /wizard — dann prüft man versehentlich den Wizard statt der Zugangs-Schicht.
A.save_profiles({"p1": {"name": "Alpha", "paperless_url": "http://x:8000",
                        "paperless_token": "abcdef0123456789abcdef0123456789abcdef01"}})
A.app.config["TESTING"] = True


def client():
    return A.app.test_client()


def login(c, pw=PW, user="admin", ip="10.0.0.1"):
    return c.post("/login", data={"username": user, "password": pw},
                  environ_base={"REMOTE_ADDR": ip})


def reset_limit():
    A._login_fails_reset()


def set_fails(ip, ts):
    with A._locked_json(A.LOGIN_FAILS_PATH) as f:
        f[ip] = ts


# ── 1. Login-Rate-Limit ──────────────────────────────────────────────────────
print("Login-Rate-Limit")
reset_limit()
c = client()
codes = [login(c, pw="falsch").status_code for _ in range(A.LOGIN_MAX)]
eq("die ersten %d Fehlversuche geben 200 (Formular mit Fehler)" % A.LOGIN_MAX,
   set(codes), {200})
eq("der %d. Versuch wird geblockt" % (A.LOGIN_MAX + 1), login(c, pw="falsch").status_code, 429)
eq("auch mit RICHTIGEM Passwort bleibt gesperrt (kein Umgehen durch Erraten)",
   login(c).status_code, 429)
check("und es entsteht KEINE Sitzung",
      client().get("/verwaltung/overview").status_code == 302)

print("Rate-Limit: Abgrenzung")
reset_limit()
c = client()
for _ in range(A.LOGIN_MAX):
    login(c, pw="falsch", ip="10.0.0.1")
eq("andere IP ist nicht mitgesperrt", login(client(), pw="falsch", ip="10.0.0.2").status_code, 200)
eq("die gesperrte IP bleibt gesperrt", login(c, pw="falsch", ip="10.0.0.1").status_code, 429)

reset_limit()
c = client()
for _ in range(A.LOGIN_MAX - 1):
    login(c, pw="falsch")
r = login(c)
eq("ein Erfolg VOR der Sperre klappt", r.status_code, 302)
eq("und setzt den Zähler zurück", len(A._login_fails_load().get("10.0.0.1", [])), 0)

print("Rate-Limit: Zeitfenster")
reset_limit()
set_fails("10.0.0.9", [A.time.time() - A.LOGIN_WINDOW - 1] * 10)
check("Fehlversuche älter als das Fenster zählen nicht mehr",
      not A._login_blocked("10.0.0.9"))
eq("und werden dabei aufgeräumt", A._login_fails_load().get("10.0.0.9"), None)
set_fails("10.0.0.8", [A.time.time()] * A.LOGIN_MAX)
check("frische Fehlversuche sperren", A._login_blocked("10.0.0.8"))

# ── 2. Anmeldung ─────────────────────────────────────────────────────────────
print("Anmeldung")
reset_limit()
eq("falscher Benutzername -> kein Login", login(client(), user="root").status_code, 200)
reset_limit()
eq("leeres Passwort -> kein Login", login(client(), pw="").status_code, 200)
reset_limit()
r = login(client())
eq("richtige Daten -> Weiterleitung", r.status_code, 302)
check("und die Sitzung greift", not r.headers["Location"].endswith("/login"))
reset_limit()
eq("Leerzeichen um den Benutzernamen (Autofill) -> trotzdem Login",
   login(client(), user=" admin ").status_code, 302)

print("Login-Seite für Passwort-Manager")
reset_limit()
page = client().get("/login").get_data(as_text=True)
check("Formular schaltet Autofill nicht ab", 'method="post" autocomplete="off"' not in page)
check("Benutzerfeld ist als username markiert", 'autocomplete="username"' in page)
check("Passwortfeld ist als current-password markiert", 'autocomplete="current-password"' in page)
page = client().get("/login/recovery").get_data(as_text=True)
check("Recovery: Benutzerfeld als username markiert", 'autocomplete="username"' in page)

print("Grund des Fehlversuchs im Protokoll")
_logged = []
_orig_log = A._log_activity
A._log_activity = lambda *a, **k: _logged.append(k.get("detail") or "")
for _u, _p, _why in (("root", PW, "Benutzer unbekannt"), ("admin", "", "Passwort leer"),
                     ("admin", "x9Q!zz", "Passwort falsch")):
    reset_limit()
    del _logged[:]
    login(client(), user=_u, pw=_p)
    check("Protokoll nennt '%s'" % _why, any(_why in d for d in _logged))
    check("Protokoll enthält kein Passwort (%s)" % _why,
          not any(_p and _p in d for d in _logged))
reset_limit()
del _logged[:]
login(client(), user="GeheimesPasswort", pw="")
check("fremder Benutzername (evtl. Passwort) steht nicht im Protokoll",
      _logged and not any("GeheimesPasswort" in d for d in _logged))
A._log_activity = _orig_log
reset_limit()

print("Abmelden")
c = client()
login(c)
eq("eingeloggt: Cockpit erreichbar", c.get("/verwaltung/overview").status_code, 200)
c.get("/logout")
eq("nach dem Abmelden wieder gesperrt", c.get("/verwaltung/overview").status_code, 302)

print("Sitzungen widerrufen")
H0 = {"Origin": "http://localhost"}


def cookie_of(c):
    return c.get_cookie("session").value


def with_cookie(val):
    c = client()
    c.set_cookie("session", val)
    return c


reset_limit()
c = client()
login(c)
stolen = cookie_of(c)
eq("kopiertes Cookie funktioniert, solange die Sitzung läuft",
   with_cookie(stolen).get("/verwaltung/overview").status_code, 200)
c.get("/logout")
eq("nach dem Logout ist auch das kopierte Cookie ungültig",
   with_cookie(stolen).get("/verwaltung/overview").status_code, 302)

forged = client()
with forged.session_transaction() as s:
    s["logged_in"] = True          # Cookie ohne gültige Sitzungs-ID (z. B. vor dem Update)
eq("Cookie ohne Sitzungs-ID reicht nicht", forged.get("/verwaltung/overview").status_code, 302)

reset_limit()
a, b = client(), client()
login(a)
login(b)
other = cookie_of(b)
r = a.post("/settings", data={"current": PW, "new": "zwischen1", "repeat": "zwischen1"},
           headers=H0)
check("Passwortwechsel klappt", "msg=" in (r.headers.get("Location") or ""))
eq("die eigene Sitzung bleibt", a.get("/verwaltung/overview").status_code, 200)
eq("andere Sitzungen sind beendet",
   with_cookie(other).get("/verwaltung/overview").status_code, 302)
a.post("/settings", data={"current": "zwischen1", "new": PW, "repeat": PW}, headers=H0)

print("Sitzungen: abgelaufene IDs")
with A._locked_json(A.SESSIONS_PATH) as ss:
    ss["uralt"] = A.time.time() - A.SESSION_MAX_AGE - 1
old = client()
with old.session_transaction() as s:
    s["logged_in"] = True
    s["sid"] = "uralt"
eq("Sitzung über der Höchstdauer gilt nicht", old.get("/verwaltung/overview").status_code, 302)
A._sessions_add()
check("und wird beim nächsten Anmelden aufgeräumt", "uralt" not in A._read_json_dict(A.SESSIONS_PATH))

print("Wizard: Passwort nur mit aktuellem Passwort")
reset_limit()
c = client()
login(c)
_real_test = A._test_paperless
A._test_paperless = lambda url, tok: 200
page = c.get("/wizard").data.decode("utf-8")
check("Wizard fragt das aktuelle Passwort ab", 'name="current"' in page)
r = c.post("/wizard", data={"new": "uebernahme", "repeat": "uebernahme"}, headers=H0)
eq("ohne aktuelles Passwort -> abgelehnt", r.status_code, 200)
check("und das Passwort ist unverändert",
      A.check_password_hash(A.load_config()["admin_pw_hash"], PW))
r = c.post("/wizard", data={"current": "falsch", "new": "uebernahme", "repeat": "uebernahme"},
           headers=H0)
check("mit falschem aktuellem Passwort ebenso",
      A.check_password_hash(A.load_config()["admin_pw_hash"], PW))
r = c.post("/wizard", data={"current": PW, "new": "zwischen2", "repeat": "zwischen2"},
           headers=H0)
eq("mit richtigem aktuellem Passwort -> durch", r.status_code, 302)
check("Passwort geändert", A.check_password_hash(A.load_config()["admin_pw_hash"], "zwischen2"))
c.post("/settings", data={"current": "zwischen2", "new": PW, "repeat": PW}, headers=H0)
A._test_paperless = _real_test

# ── 3. Recovery-Codes ────────────────────────────────────────────────────────
print("Recovery-Codes")
cfg = A.load_config()
eq("frische Installation hat KEINE Codes", A._recovery_remaining(cfg), 0)
codes = A._gen_recovery_codes()
eq("es werden %d Codes erzeugt" % A.RECOVERY_CODE_COUNT, len(codes), A.RECOVERY_CODE_COUNT)
eq("alle verschieden", len(set(codes)), A.RECOVERY_CODE_COUNT)
check("Format xxxx-xxxx-xxxx", all(len(x) == 14 and x.count("-") == 2 for x in codes))
A._set_recovery_codes(cfg, codes)
A.save_config(cfg)
raw = open(A.CONFIG_PATH, encoding="utf-8").read()
check("KEIN Code liegt im Klartext in der config.json",
      not any(c_ in raw for c_ in codes))
check("auch nicht ohne Bindestriche",
      not any(A._norm_recovery(c_) in raw for c_ in codes))
eq("gespeichert sind %d Hashes" % A.RECOVERY_CODE_COUNT,
   A._recovery_remaining(A.load_config()), A.RECOVERY_CODE_COUNT)

print("Recovery: Einlösen")
reset_limit()
c = client()
r = c.post("/login/recovery", data={"username": "admin", "code": codes[0]},
           environ_base={"REMOTE_ADDR": "10.0.0.1"})
eq("gültiger Code meldet an", r.status_code, 302)
eq("und der Code ist verbraucht", A._recovery_remaining(A.load_config()),
   A.RECOVERY_CODE_COUNT - 1)
eq("die Sitzung greift", c.get("/verwaltung/overview").status_code, 200)
check("Konto-Seite fragt nach Recovery-Login nicht nach dem alten Passwort",
      'name="current" required' not in c.get("/settings").data.decode("utf-8")
      and "Recovery-Code angemeldet" in c.get("/settings").data.decode("utf-8"))
r = c.post("/settings", data={"new": "nachrecovery", "repeat": "nachrecovery"},
           headers={"Origin": "http://localhost"})
check("neues Passwort ohne das vergessene alte setzbar",
      A.check_password_hash(A.load_config()["admin_pw_hash"], "nachrecovery"))
r = c.post("/settings", data={"new": "nochmal1", "repeat": "nochmal1"},
           headers={"Origin": "http://localhost"})
check("aber nur EINMAL — danach wieder mit aktuellem Passwort",
      A.check_password_hash(A.load_config()["admin_pw_hash"], "nachrecovery"))
c.post("/settings", data={"current": "nachrecovery", "new": PW, "repeat": PW},
       headers={"Origin": "http://localhost"})
reset_limit()
r = client().post("/login/recovery", data={"username": "admin", "code": codes[0]},
                  environ_base={"REMOTE_ADDR": "10.0.0.1"})
eq("derselbe Code ein ZWEITES Mal wird abgelehnt (Einmal-Code)", r.status_code, 200)

print("Recovery: Ablehnen")
for label, data in [
    ("falscher Code", {"username": "admin", "code": "aaaa-bbbb-cccc"}),
    ("leerer Code", {"username": "admin", "code": ""}),
    ("nur Leerzeichen", {"username": "admin", "code": "   "}),
    ("falscher Benutzer", {"username": "root", "code": codes[1]}),
]:
    reset_limit()
    r = client().post("/login/recovery", data=data, environ_base={"REMOTE_ADDR": "10.0.0.1"})
    eq("  %s -> kein Login" % label, r.status_code, 200)
eq("und kein Code wurde dabei verbraucht", A._recovery_remaining(A.load_config()),
   A.RECOVERY_CODE_COUNT - 1)

print("Recovery: Schreibweise")
cfg = A.load_config()
eq("Bindestriche egal", A._norm_recovery("ab12-cd34-ef56"), "ab12cd34ef56")
eq("Grossschreibung egal", A._norm_recovery("AB12-CD34-EF56"), "ab12cd34ef56")
eq("Leerzeichen egal", A._norm_recovery(" ab12 cd34 ef56 "), "ab12cd34ef56")
check("ein Code wird auch in anderer Schreibweise angenommen",
      A._consume_recovery_code(cfg, codes[1].upper().replace("-", " ")))

print("Recovery: Rate-Limit greift auch hier")
reset_limit()
c = client()
for _ in range(A.LOGIN_MAX):
    c.post("/login/recovery", data={"username": "admin", "code": "falsch"},
           environ_base={"REMOTE_ADDR": "10.0.0.7"})
eq("nach %d Fehlversuchen gesperrt" % A.LOGIN_MAX,
   c.post("/login/recovery", data={"username": "admin", "code": "falsch"},
          environ_base={"REMOTE_ADDR": "10.0.0.7"}).status_code, 429)
eq("Login und Recovery teilen denselben Zähler (kein Umweg)",
   login(c, pw="falsch", ip="10.0.0.7").status_code, 429)

# ── 4. CSRF ──────────────────────────────────────────────────────────────────
print("CSRF")
c = client()
reset_limit()
login(c)
H = "http://localhost"
eq("fremder Origin -> 403",
   c.post("/settings", headers={"Origin": "http://evil.example.com"}).status_code, 403)
eq("fremder Referer -> 403",
   c.post("/settings", headers={"Referer": "http://evil.example.com/x"}).status_code, 403)
check("eigener Origin -> kein CSRF-Block",
      c.post("/settings", headers={"Origin": H}).status_code != 403)
check("Origin schlägt Referer (eigener Origin, fremder Referer)",
      c.post("/settings", headers={"Origin": H,
                                   "Referer": "http://evil.example.com/"}).status_code != 403)
check("ohne beide Header -> durchgelassen (der Angriffsvektor sendet einen Origin)",
      c.post("/settings").status_code != 403)
eq("Müll im Origin -> 403", c.post("/settings", headers={"Origin": "::::"}).status_code, 403)
check("GET wird nicht geprüft", c.get("/verwaltung/overview").status_code == 200)
eq("der /api-Pfad ist ausgenommen (Generator feuert dort legitim Bursts) — "
   "Schutz kommt dort vom SameSite-Cookie",
   c.post("/api/tags/", headers={"Origin": "http://evil.example.com"}).status_code != 403, True)

print("Sitzungs-Cookie")
eq("HttpOnly gesetzt", A.app.config["SESSION_COOKIE_HTTPONLY"], True)
eq("SameSite=Lax — bremst genau die Cross-Site-Posts, die /api nicht prüft",
   A.app.config["SESSION_COOKIE_SAMESITE"], "Lax")
r = login(client())
sc = r.headers.get("Set-Cookie", "")
check("das echte Cookie trägt HttpOnly", "HttpOnly" in sc)
check("das echte Cookie trägt SameSite=Lax", "SameSite=Lax" in sc)
check("Sitzung läuft ab (nicht unbegrenzt)",
      A.app.config["PERMANENT_SESSION_LIFETIME"].total_seconds() > 0)

# ── 5. Token-Verschlüsselung at-rest ─────────────────────────────────────────
print("Token-Verschlüsselung")
TOK = "abcdef0123456789abcdef0123456789abcdef01"
A.save_profiles({"p1": {"name": "Alpha", "paperless_url": "http://x:8000",
                        "paperless_token": TOK}})
raw = open(A.PROFILES_PATH, encoding="utf-8").read()
check("das Token liegt NICHT im Klartext in profiles.json", TOK not in raw)
check("es ist als verschlüsselt markiert", A._ENC_PREFIX in raw)
eq("und lässt sich zurückholen", A._dec(A.load_profiles()["p1"]["paperless_token"]), TOK)
eq("Verschlüsseln ist idempotent (kein Doppel-Enc beim zweiten Speichern)",
   A._dec(A._enc(A._enc(TOK))), TOK)
eq("leeres Token bleibt leer", A._enc(""), "")
eq("Entschlüsseln von Klartext gibt Klartext (Altbestand vor der Verschlüsselung)",
   A._dec("nackt"), "nackt")
eq("kaputtes Chiffrat -> leer statt Absturz", A._dec(A._ENC_PREFIX + "quatsch"), "")
A.save_profiles(A.load_profiles())
eq("mehrfaches Speichern beschädigt das Token nicht",
   A._dec(A.load_profiles()["p1"]["paperless_token"]), TOK)
raw2 = open(A.PROFILES_PATH, encoding="utf-8").read()
check("und es bleibt verschlüsselt", TOK not in raw2)

print("Notifikations-Secrets")
A.save_profiles({"p1": {"name": "Alpha", "paperless_url": "http://x:8000",
                        "paperless_token": TOK,
                        "notifications": {"pushover": {"enabled": True, "token": "po-geheim",
                                                       "user": "user-geheim"},
                                          "email": {"enabled": True, "password": "mail-geheim"}}}})
raw = open(A.PROFILES_PATH, encoding="utf-8").read()
for secret in ("po-geheim", "user-geheim", "mail-geheim"):
    check("  '%s' liegt nicht im Klartext auf Platte" % secret, secret not in raw)

# ── 6. Reverse-Proxy-Betrieb (TRUST_PROXY) ──────────────────────────────────
# Wird beim Import ausgewertet -> die Gegenprobe braucht einen eigenen Prozess.
print("Reverse-Proxy: Standard (TRUST_PROXY aus)")
reset_limit()
c = client()
for _ in range(A.LOGIN_MAX):
    c.post("/login", data={"username": "admin", "password": "falsch"},
           headers={"X-Forwarded-For": "203.0.113.99"},
           environ_base={"REMOTE_ADDR": "203.0.113.5"})
eq("X-Forwarded-For wird NICHT geglaubt — der Angreifer kann sich keine frische IP "
   "erfinden und das Limit umgehen",
   c.post("/login", data={"username": "admin", "password": "falsch"},
          headers={"X-Forwarded-For": "10.1.1.1"},
          environ_base={"REMOTE_ADDR": "203.0.113.5"}).status_code, 429)
eq("gezählt wird der echte Peer", sorted(A._login_fails_load()), ["203.0.113.5"])
check("ohne TRUST_PROXY hängt kein ProxyFix in der Kette",
      A.app.wsgi_app.__class__.__name__ != "ProxyFix")

print("Reverse-Proxy: TRUST_PROXY=1 (eigener Prozess)")
import subprocess  # noqa: E402

_PROBE = r'''
import os, sys, tempfile
os.environ.update({"CONFIG_DIR": tempfile.mkdtemp(), "SITE_DIR": sys.argv[1],
                   "PORTAL_WATCHER": "0", "TRUST_PROXY": "1"})
sys.path.insert(0, sys.argv[2])
import app as A
A._log_activity = lambda *a, **k: None
cfg = A.init_config(); cfg["is_default_pw"] = False
cfg["admin_pw_hash"] = A.generate_password_hash("pw"); A.save_config(cfg); A._cfg0 = cfg
A.app.config["TESTING"] = True
c = A.app.test_client()
for _ in range(A.LOGIN_MAX):
    c.post("/login", data={"username": "admin", "password": "falsch"},
           headers={"X-Forwarded-For": "203.0.113.99"},
           environ_base={"REMOTE_ADDR": "172.17.0.1"})
r = c.post("/login", data={"username": "admin", "password": "pw"},
           headers={"X-Forwarded-For": "192.168.10.23"},
           environ_base={"REMOTE_ADDR": "172.17.0.1"})
print(r.status_code, sorted(A._login_fails_load()), A.app.wsgi_app.__class__.__name__)
'''
out = subprocess.run([sys.executable, "-c", _PROBE, os.environ["SITE_DIR"],
                      os.path.join(os.path.dirname(_HERE), "app")],
                     capture_output=True, text=True)
res = (out.stdout or "").strip().split()
check("Sonde lief (%s)" % ((out.stderr or "")[-120:] or "ok"), len(res) >= 3)
if len(res) >= 3:
    eq("hinter dem Proxy sperren fremde Fehlversuche NICHT den eigenen Login", res[0], "302")
    eq("gezählt wird die echte Client-IP aus X-Forwarded-For", res[1], "['203.0.113.99']")
    eq("ProxyFix hängt in der Kette", res[2], "ProxyFix")

print("\n%d Prüfungen, %d Fehler" % (_count[0], len(_fails)))
for f in _fails:
    print("  - " + f)
sys.exit(1 if _fails else 0)
