"""Tests fuer Voll-Backup/Restore + Profil-Export/Import — Rundlauf, Secrets, Pfad-Riegel.

SICHERHEIT: Es geht KEIN Request ins Netz. Geprueft wird ausschliesslich, was auf Platte
landet und was der Export/Import damit macht. Alle Schreibpfade zeigen in einen Temp-Dir.

Der Kern-Anspruch dieser Datei: der Export entschluesselt Tokens BEWUSST, damit ein Backup
auf einer anderen Instanz (mit anderem 'secret') einspielbar bleibt. Genau dieser Rundlauf
wird hier zum ersten Mal wirklich nachgestellt — inkl. Instanz-Wechsel.

    python -m venv .venv
    .venv/Scripts/pip install -r app/requirements.txt
    .venv/Scripts/python tests/test_backup.py
"""
import base64
import hashlib
import io
import json
import os
import sys
import tempfile
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_CFG = tempfile.mkdtemp(prefix="portal-backup-")
os.environ["CONFIG_DIR"] = _CFG
os.environ["SITE_DIR"] = os.path.join(os.path.dirname(_HERE), "site")
os.environ["PORTAL_WATCHER"] = "0"
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "app"))

from cryptography.fernet import Fernet  # noqa: E402

import app as A  # noqa: E402


# ── Testgeruest ──────────────────────────────────────────────────────────────
_fails = []
_count = [0]


def check(name, cond):
    _count[0] += 1
    if not cond:
        _fails.append(name)
        print("  FAIL  " + name)


def eq(name, got, want):
    check("%s (erwartet %r, war %r)" % (name, want, got), got == want)


PID = "9d5a263844e6fb1c"
TOKEN = "382810ecd86741419d580e029d121b7a5537f602"
PW_HASH = A.generate_password_hash("geheim123")


def _fernet_with(secret):
    """Fernet einer FREMDEN Instanz — fuer Backups, die anderswo entstanden sind."""
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def _enc_with(secret, tok):
    return A._ENC_PREFIX + _fernet_with(secret).encrypt(tok.encode()).decode()


def base_config(secret):
    return {"admin_user": "admin", "admin_pw_hash": PW_HASH, "paperless_url": "",
            "paperless_token": "", "secret": secret, "is_default_pw": False,
            "active_profile": PID}


def reset(secret="secret-der-laufenden-instanz"):
    """Instanz auf einen definierten Stand bringen: Config + ein Profil mit Token."""
    A.save_config(base_config(secret))
    A._cfg0.clear()
    A._cfg0.update(base_config(secret))
    A.save_profiles({PID: {"name": "TESTINStanz", "paperless_url": "http://192.168.10.200:8000",
                           "paperless_token": TOKEN, "generator_config": None,
                           "productive": False, "readonly": False, "color": "",
                           "notifications": {"pushover": {"token": "pushtok", "user": "pushuser"},
                                             "email": {"password": "smtppw"}}}})


A.app.config["TESTING"] = True


def client():
    c = A.app.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
        s["active_profile"] = PID
    return c


# ── 1. Verschluesselung at-rest ──────────────────────────────────────────────
print("Verschlüsselung at-rest")
reset()
raw = json.load(open(A.PROFILES_PATH, encoding="utf-8"))
check("der Token liegt NICHT im Klartext auf Platte",
      raw[PID]["paperless_token"].startswith(A._ENC_PREFIX))
check("der Klartext-Token taucht nirgends in der Datei auf",
      TOKEN not in open(A.PROFILES_PATH, encoding="utf-8").read())
eq("und ist mit dem Instanz-secret wieder lesbar", A._dec(raw[PID]["paperless_token"]), TOKEN)
check("Pushover-Token ebenfalls verschlüsselt",
      raw[PID]["notifications"]["pushover"]["token"].startswith(A._ENC_PREFIX))
check("SMTP-Passwort ebenfalls verschlüsselt",
      raw[PID]["notifications"]["email"]["password"].startswith(A._ENC_PREFIX))

# Idempotenz: zweites Speichern darf nicht doppelt verschlüsseln
A.save_profiles(A.load_profiles())
eq("zweimal speichern verschlüsselt nicht doppelt",
   A._dec(json.load(open(A.PROFILES_PATH, encoding="utf-8"))[PID]["paperless_token"]), TOKEN)


# ── 2. Profil-Export ─────────────────────────────────────────────────────────
print("Profil-Export")
reset()
r = client().get("/profiles/export")
eq("Export liefert 200", r.status_code, 200)
exp = json.loads(r.data.decode("utf-8"))
eq("Token wird ENTSCHLÜSSELT exportiert (Zweck: auf anderer Instanz einspielbar)",
   exp[PID]["paperless_token"], TOKEN)
eq("Pushover-Token ebenso", exp[PID]["notifications"]["pushover"]["token"], "pushtok")
eq("SMTP-Passwort ebenso", exp[PID]["notifications"]["email"]["password"], "smtppw")
check("der Export lässt die Profile auf Platte verschlüsselt (keine Rück-Wirkung)",
      json.load(open(A.PROFILES_PATH, encoding="utf-8"))[PID]["paperless_token"]
      .startswith(A._ENC_PREFIX))


# ── 3. Rundlauf: Export -> Import auf ANDERER Instanz (anderer secret) ───────
print("Rundlauf über Instanz-Grenze")
reset("secret-instanz-A")
dump = json.loads(client().get("/profiles/export").data.decode("utf-8"))

# Instanz B: anderer secret, fremdes Platzhalter-Profil (sonst greift das Setup-Gate,
# siehe Block 11 — der Import wuerde auf /wizard umgeleitet).
A.save_config(base_config("secret-instanz-B"))
A._cfg0.clear()
A._cfg0.update(base_config("secret-instanz-B"))
A.save_profiles({PID: {"name": "Platzhalter", "paperless_url": "http://alt",
                       "paperless_token": "alterToken"}})

r = client().post("/profiles/import", data={
    "file": (io.BytesIO(json.dumps(dump).encode("utf-8")), "profiles.json")},
    content_type="multipart/form-data")
eq("Import leitet zurück (302)", r.status_code, 302)
check("kein Fehler in der Rückmeldung", "err=" not in r.headers.get("Location", ""))
prof_b = A.load_profiles().get(PID) or {}
eq("das Profil ist auf Instanz B angekommen", prof_b.get("name"), "TESTINStanz")
eq("und der Token ist dort BENUTZBAR (das ist der beworbene Zweck)",
   A._dec(prof_b.get("paperless_token", "")), TOKEN)
check("und liegt auf B mit B's secret verschlüsselt (nicht im Klartext)",
      prof_b.get("paperless_token", "").startswith(A._ENC_PREFIX))
eq("Benachrichtigungs-Geheimnisse überleben den Umzug ebenfalls",
   A._dec(prof_b["notifications"]["pushover"]["token"]), "pushtok")


# ── 4. Export bei gewechseltem secret ────────────────────────────────────────
# Wenn 'secret' nicht zu den gespeicherten Werten passt, sind die Tokens nicht
# entschlüsselbar. _dec() antwortet mit "" — der Export darf daraus kein scheinbar
# gültiges Backup mit leeren Tokens bauen, OHNE es zu sagen.
print("Export bei nicht entschlüsselbaren Tokens")
reset("secret-original")
cfg = base_config("secret-voellig-anders")       # Profile bleiben mit dem alten verschlüsselt
A.save_config(cfg)
A._cfg0.clear()
A._cfg0.update(cfg)
dump2 = json.loads(client().get("/profiles/export").data.decode("utf-8"))
eq("der Token ist erwartungsgemäss nicht zu retten", dump2[PID]["paperless_token"], "")
warn = [e for e in A._read_activity(20) if e.get("kind") == "export" and e.get("level") == "warn"]
check("aber der Verlust steht als Warnung im Protokoll (statt still zu passieren)", bool(warn))
if warn:
    check("und die Warnung benennt die betroffenen Zugangsdaten",
          "Paperless-Token" in (warn[0].get("detail") or ""))
    # Token + Pushover-Token + Pushover-User + SMTP-Passwort = 4
    check("alle vier Geheimnisse werden gezählt", "4 Zugangsdaten" in warn[0]["msg"])
    check("und die Warnung sagt, was zu tun ist",
          "neu gesetzt werden" in (warn[0].get("detail") or ""))


# ── 5. Voll-Backup: ZIP-Inhalt ───────────────────────────────────────────────
print("Voll-Backup (ZIP)")
reset()
A._log_activity("test", "Marker fürs Backup")
r = client().get("/verwaltung/config-backup")
eq("Backup liefert 200", r.status_code, 200)
eq("als ZIP", r.mimetype, "application/zip")
zf = zipfile.ZipFile(io.BytesIO(r.data))
names = set(zf.namelist())
check("config.json ist enthalten", "config.json" in names)
check("profiles.json ist enthalten", "profiles.json" in names)
check("das Protokoll ist enthalten", "activity.log" in names)
check("watcher.lock ist NICHT enthalten (transient)", "watcher.lock" not in names)
check("das ZIP enthält den Token nicht im Klartext",
      TOKEN.encode() not in zf.read("profiles.json"))
check("aber config.json enthält das secret (vertraulich!)",
      b"secret" in zf.read("config.json"))


# ── 6. Voll-Restore: Rundlauf ────────────────────────────────────────────────
print("Voll-Restore")
reset("secret-vor-dem-restore")
backup = client().get("/verwaltung/config-backup").data     # Backup DIESER Instanz
A.save_profiles({PID: {"name": "Platzhalter", "paperless_url": "http://alt",
                       "paperless_token": "alterToken"}})    # Stand verfaelschen
r = client().post("/verwaltung/config-restore", data={
    "file": (io.BytesIO(backup), "backup.zip")}, content_type="multipart/form-data")
eq("Restore leitet zurück (302)", r.status_code, 302)
check("ohne Fehler", "err=" not in r.headers.get("Location", ""))
eq("die Profile sind zurück", A.load_profiles().get(PID, {}).get("name"), "TESTINStanz")
eq("und der Token ist wieder lesbar (gleiches secret)",
   A._dec(A.load_profiles()[PID]["paperless_token"]), TOKEN)


# ── 7. Voll-Restore eines FREMDEN Backups (anderer secret) ───────────────────
# Das ist der Disaster-Recovery-Fall: Container neu, Backup von der alten Instanz.
# Im ZIP stecken config.json (fremder secret) UND die damit verschlüsselten Profile.
print("Voll-Restore eines fremden Backups")
reset("secret-der-laufenden-instanz")
FREMD = "secret-der-alten-instanz"
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("config.json", json.dumps(base_config(FREMD)))
    z.writestr("profiles.json", json.dumps({PID: {
        "name": "Aus dem Backup", "paperless_url": "http://192.168.10.200:8000",
        "paperless_token": _enc_with(FREMD, TOKEN), "generator_config": None}}))
r = client().post("/verwaltung/config-restore", data={
    "file": (io.BytesIO(buf.getvalue()),"fremd.zip")}, content_type="multipart/form-data")
eq("Restore nimmt das fremde Backup an", r.status_code, 302)
eq("die Datei auf Platte trägt jetzt den fremden secret",
   A.load_config()["secret"], FREMD)
eq("das Profil aus dem Backup ist da", A.load_profiles()[PID]["name"], "Aus dem Backup")
eq("und sein Token ist SOFORT lesbar — ohne Neustart (der secret kommt von Platte, "
   "nicht aus einem Boot-Snapshot)",
   A._dec(A.load_profiles()[PID]["paperless_token"]), TOKEN)
# Gegenprobe: der Boot-Snapshot ist bewusst NICHT mehr massgeblich
eq("_cfg0 hält noch den alten secret — und das stört nicht mehr",
   A._cfg0["secret"], "secret-der-laufenden-instanz")
eq("der Schlüssel folgt der Datei", A._current_secret(), FREMD)


# ── 8. Pfad-Riegel: Restore-ZIP mit Traversal ────────────────────────────────
print("Restore: Pfad-Riegel")
reset()
outside = os.path.join(os.path.dirname(os.path.abspath(_CFG)), "portal-pwned.txt")
if os.path.exists(outside):
    os.remove(outside)
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("../portal-pwned.txt", "pwned")
    z.writestr("harmlos.txt", "ok")
client().post("/verwaltung/config-restore", data={
    "file": (io.BytesIO(buf.getvalue()),"evil.zip")}, content_type="multipart/form-data")
check("ein ZIP-Eintrag mit ../ schreibt NICHT ausserhalb von CONFIG_DIR",
      not os.path.exists(outside))
check("harmlose Einträge werden trotzdem eingespielt",
      os.path.exists(os.path.join(_CFG, "harmlos.txt")))


# ── 9. Import: Eingabe-Pruefung ──────────────────────────────────────────────
print("Import: Eingabe-Prüfung")
reset()


def imp(payload):
    body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    if isinstance(body, str):
        body = body.encode("utf-8")
    return client().post("/profiles/import", data={"file": (io.BytesIO(body), "p.json")},
                         content_type="multipart/form-data")


check("kaputtes JSON wird abgelehnt", "err=" in imp(b"{nicht json").headers.get("Location", ""))
check("eine leere Datei wird abgelehnt", "err=" in imp({}).headers.get("Location", ""))
check("eine Liste statt Objekt wird abgelehnt", "err=" in imp([1, 2]).headers.get("Location", ""))
check("Profil ohne 'name' wird abgelehnt",
      "err=" in imp({"abc": {"paperless_url": "x"}}).headers.get("Location", ""))
eq("und der bestehende Stand bleibt dabei unangetastet",
   A.load_profiles()[PID]["name"], "TESTINStanz")


# ── 10. Pfad-Riegel: Profil-ID aus der Import-Datei ──────────────────────────
# Die Schlüssel der Import-JSON werden zu Profil-IDs. Diese IDs landen ungefiltert in
# Dateipfaden (_watch_path/_metrics_path/_history_dir/_save_undo). Eine untergeschobene
# Backup-Datei koennte damit ausserhalb von CONFIG_DIR schreiben.
print("Import: Profil-ID als Pfad")
reset()
for evil in ("../../portal-evil", "C:/portal-abs", "/tmp/portal-abs", "..", "a/b",
             "a\\b", "x" * 65, "", "pu.nkt"):
    r = imp({evil: {"name": "Böse", "paperless_url": "http://x", "paperless_token": "t"}})
    check("Profil-ID %r wird abgelehnt" % evil, "err=" in r.headers.get("Location", ""))
    check("Profil-ID %r landet nicht in den Profilen" % evil, evil not in A.load_profiles())
eq("der bestehende Stand überlebt die Angriffsversuche",
   A.load_profiles()[PID]["name"], "TESTINStanz")
check("eine normale hex-ID geht weiterhin durch", A._valid_pid(A._new_profile_id()))

# Zweite Linie: ein Voll-Restore schreibt profiles.json DIREKT ins Volume, am Import vorbei.
print("Restore: Profil-ID als Pfad")
reset()
with open(A.PROFILES_PATH, "w", encoding="utf-8") as fh:
    json.dump({"../../portal-evil": {"name": "Böse"}, PID: {"name": "Gut"}}, fh)
profs = A.load_profiles()
check("eine am Import vorbei eingeschleuste Pfad-ID wird beim Lesen verworfen",
      "../../portal-evil" not in profs)
eq("das harmlose Profil daneben bleibt erhalten", profs.get(PID, {}).get("name"), "Gut")
warn = [e for e in A._read_activity(20) if e.get("level") == "warn" and "Profil-ID" in e.get("msg", "")]
check("und der Vorfall steht im Protokoll", bool(warn))
try:
    A.save_profiles({"../../x": {"name": "Böse"}})
    check("save_profiles verweigert eine Pfad-ID", False)
except ValueError:
    check("save_profiles verweigert eine Pfad-ID", True)

# ── 11. Datenrettung auf einer frischen Instanz ──────────────────────────────
# Der Disaster-Recovery-Fall: neuer Container, Backup einspielen. Solange kein Profil mit
# URL+Token existiert, leitet require_setup JEDEN Seiten-Request auf /wizard — und der
# Wizard bietet KEINEN Import an. Genau dann, wenn man Backup/Restore braucht, sind sie zu.
print("Datenrettung auf frischer Instanz")
reset()
A.save_profiles({})          # frischer Container: noch kein Profil
c = client()
for name, r in (("Voll-Backup", c.get("/verwaltung/config-backup")),
                ("Profil-Export", c.get("/profiles/export")),
                ("Voll-Restore", c.post("/verwaltung/config-restore", data={
                    "file": (io.BytesIO(b"PK"), "b.zip")}, content_type="multipart/form-data")),
                ("Profil-Import", c.post("/profiles/import", data={
                    "file": (io.BytesIO(json.dumps({PID: {"name": "X"}}).encode()), "p.json")},
                    content_type="multipart/form-data"))):
    loc = r.headers.get("Location", "")
    # Das Setup-Gate erkennt man an einer STUMMEN Umleitung auf /wizard. Antwortet der
    # Endpunkt dagegen mit msg=/err=, hat er gearbeitet — dann ist er erreichbar.
    gated = r.status_code == 302 and "wizard" in loc and "msg=" not in loc and "err=" not in loc
    check("%s ist ohne eingerichtetes Profil erreichbar (war: %s %s)" % (name, r.status_code, loc),
          not gated)
eq("und der Import legt das Profil wirklich an",
   A.load_profiles().get(PID, {}).get("name"), "X")


# ── 12. Der Rettungsweg als Ganzes ───────────────────────────────────────────
# Routen offen reicht nicht — der Nutzer muss den Weg auch FINDEN und beschreiten können.
print("Rettungsweg über den Wizard")
reset()
A.save_config(dict(base_config("secret-frisch"), is_default_pw=True))   # frisch: Wizard-Pflicht
A._cfg0.clear()
A._cfg0.update(A.load_config())
A.save_profiles({PID: {"name": "Leer", "paperless_url": "", "paperless_token": ""}})
c = client()
page = c.get("/wizard").data.decode("utf-8")
check("der Wizard bietet einen Weg für ein Voll-Backup an", "/verwaltung/config-restore" in page)
check("und einen für den Profil-Import", "/profiles/import" in page)

# Profil-Import auf der frischen Instanz -> Verbindung ist da, nur das Passwort fehlt noch
dump3 = {PID: {"name": "Aus Backup", "paperless_url": "http://192.168.10.200:8000",
               "paperless_token": TOKEN}}
r = c.post("/profiles/import", data={
    "file": (io.BytesIO(json.dumps(dump3).encode()), "p.json")},
    content_type="multipart/form-data")
loc = r.headers.get("Location", "")
check("nach dem Import führt der Weg zurück zum Wizard (Passwort fehlt noch)", "wizard" in loc)
check("und die Erfolgsmeldung geht dabei NICHT verloren", "msg=" in loc)
page = c.get(loc).data.decode("utf-8")
check("der Wizard zeigt die übernommene Verbindung", "Verbindung übernommen" in page)
check("und verlangt den Token nicht erneut", 'name="paperless_token" required' not in page)
eq("der importierte Token liegt verschlüsselt vor",
   A._dec(A.load_profiles()[PID]["paperless_token"]), TOKEN)

# Wizard abschliessen: nur Passwort, Verbindungsfelder leer -> bestehende wird uebernommen
_probe = {}
A._test_paperless = lambda url, tok: (_probe.update({"url": url, "tok": tok}), 200)[1]
r = c.post("/wizard", data={"new": "neuesPW1", "repeat": "neuesPW1",
                            "paperless_url": "", "paperless_token": ""})
eq("die Einrichtung geht mit leeren Verbindungsfeldern durch", r.status_code, 302)
eq("und prüft dabei den importierten Token live", _probe.get("tok"), TOKEN)
eq("gegen die importierte URL", _probe.get("url"), "http://192.168.10.200:8000")
check("die Einrichtung gilt jetzt als abgeschlossen", not A.load_config()["is_default_pw"])
eq("der Token blieb dabei unangetastet",
   A._dec(A.load_profiles()[PID]["paperless_token"]), TOKEN)

print("\n%d Prüfungen, %d Fehler" % (_count[0], len(_fails)))
for f in _fails:
    print("  - " + f)
sys.exit(1 if _fails else 0)
