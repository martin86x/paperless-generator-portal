"""Tests der Webhook-Auswahl (v1.9.0) — welche Ereignisarten rausgehen, Payload, Kompatibilität.

Kein Netzwerk: requests.post ist ersetzt und protokolliert nur, was gesendet WÜRDE.
CONFIG_DIR zeigt auf ein Temp-Verzeichnis, die Wächter-Schleife bleibt aus.

Braucht die Abhängigkeiten aus app/requirements.txt (Flask & Co.) — im System-Python
fehlen die, also einmalig ein venv anlegen:

    python -m venv .venv
    .venv/Scripts/pip install -r app/requirements.txt
    .venv/Scripts/python tests/test_webhook.py
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_CFG = tempfile.mkdtemp(prefix="portal-webhook-")
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


NOW = 1_700_000_000.0


class Clock:
    def __init__(self):
        import time as _t
        self._t = _t
        self.now = NOW

    def time(self):
        return self.now

    def __getattr__(self, k):
        return getattr(self._t, k)


A.time = Clock()

POSTS = []       # alles, was der Webhook rausschicken würde


class FakeResp:
    status_code = 200
    text = "ok"


def _fake_post(url, **kw):
    POSTS.append({"url": url, "json": kw.get("json")})
    return FakeResp()


A.requests.post = _fake_post
A.requests.get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("kein GET erwartet"))
A._log_activity = lambda *a, **k: None
A._dispatch_notification = lambda *a, **k: (0, [])

URL = "https://n8n.example.com/webhook/paperless"


def set_hook(enabled=True, url=URL, **kinds):
    cfg = A.init_config()
    cfg["is_default_pw"] = False
    cfg["webhook"] = {"enabled": enabled, "url": url}
    if kinds:
        cfg["webhook"]["kinds"] = kinds
    A.save_config(cfg)


def reset():
    del POSTS[:]
    A._watcher_state["alerts_active"] = set()
    A._watcher_state["alert_meta"] = {}


def wc(**over):
    c = dict(A._WATCHER_DEFAULTS)
    c["checks"] = dict(A._WATCHER_DEFAULTS["checks"])
    c.update(over)
    return c


PROF = {"name": "Alpha"}
BAD = {"event": "downtime", "label": "Erreichbarkeit", "status": "bad", "detail": "Weg."}
OK = {"event": "downtime", "label": "Erreichbarkeit", "status": "ok", "detail": "Da."}

# ── 1. Kompatibilität mit alter Konfig ───────────────────────────────────────
print("Kompatibilität")
set_hook()   # ohne 'kinds' — so sieht jede config.json vor v1.9.0 aus
c = A._webhook_cfg()
eq("alte Konfig: Alarm an (wie bisher)", c["kinds"]["alarm"], True)
eq("alte Konfig: Entwarnung an (wie bisher)", c["kinds"]["recovery"], True)
eq("alte Konfig: Erinnerung aus (war bisher nie dabei)", c["kinds"]["reminder"], False)
eq("alte Konfig: Digest aus", c["kinds"]["digest"], False)
eq("alte Konfig: Report aus", c["kinds"]["report"], False)
cfg = A.load_config()
cfg["webhook"] = {"enabled": True, "url": URL, "kinds": "kaputt"}
A.save_config(cfg)
eq("Müll in 'kinds' -> Standardwerte statt Absturz",
   A._webhook_cfg()["kinds"]["alarm"], True)
set_hook()

# ── 2. Auswahl greift ────────────────────────────────────────────────────────
print("Auswahl")
reset()
set_hook(alarm=True, reminder=False, recovery=False, digest=False, report=False)
ok, detail = A._fire_webhook("alarm", "downtime", "Alpha", "bad", "x")
eq("gewählte Art geht raus", ok, True)
eq("und landet auf der URL", POSTS[-1]["url"], URL)
ok, detail = A._fire_webhook("recovery", "downtime", "Alpha", "ok", "x")
eq("abgewählte Art geht NICHT raus", ok, False)
check("mit sprechendem Grund", "abgewählt" in detail)
eq("und es wurde nichts gesendet", len(POSTS), 1)
ok, _ = A._fire_webhook("test", "test", "Portal", "test", "x")
eq("Testknopf feuert IMMER, unabhängig von der Auswahl", ok, True)

reset()
set_hook(enabled=False, alarm=True)
ok, detail = A._fire_webhook("alarm", "downtime", "Alpha", "bad", "x")
eq("Webhook aus -> nichts, auch wenn die Art gewählt ist", ok, False)
eq("auch der Testknopf nicht", A._fire_webhook("test", "test", "P", "test", "x")[0], False)
eq("wirklich nichts gesendet", len(POSTS), 0)
set_hook(alarm=True)
reset()
A._fire_webhook("alarm", "downtime", "Alpha", "bad", "x")
eq("ohne URL geht nichts", len(POSTS), 1)
set_hook(url="", alarm=True)
reset()
eq("leere URL -> kein Versand", A._fire_webhook("alarm", "downtime", "A", "bad", "x")[0], False)
eq("und nichts gesendet", len(POSTS), 0)

# ── 3. Payload ───────────────────────────────────────────────────────────────
print("Payload")
reset()
set_hook(alarm=True)
A._fire_webhook("alarm", "downtime", "Meine Instanz", "bad", "Nicht erreichbar.")
p = POSTS[-1]["json"]
eq("source", p["source"], "paperless-generator-portal")
eq("kind sagt WAS passiert ist", p["kind"], "alarm")
eq("event sagt WELCHER Check", p["event"], "downtime")
eq("profile", p["profile"], "Meine Instanz")
eq("status", p["status"], "bad")
eq("detail", p["detail"], "Nicht erreichbar.")
eq("portal_version", p["portal_version"], A.PORTAL_VERSION)
check("ts im ISO-Format", "T" in p["ts"] and len(p["ts"]) == 19)
eq("keine unerwarteten Felder",
   sorted(p), ["detail", "event", "kind", "portal_version", "profile", "source", "status", "ts"])
check("Payload ist JSON-fähig", json.dumps(p) and True)

# ── 4. Alarm / Erinnerung / Entwarnung über _maybe_alert ─────────────────────
print("Eskalations-Flanken")
reset()
set_hook(alarm=True, reminder=True, recovery=True)
w = wc(fail_threshold=1, ok_threshold=1, repeat_min=60, recovery_notify=False)
A._maybe_alert(PROF, "p1", BAD, w)
eq("Alarm feuert", len(POSTS), 1)
eq("als 'alarm'", POSTS[-1]["json"]["kind"], "alarm")
A._maybe_alert(PROF, "p1", BAD, w)
eq("vor Ablauf des Abstands keine Erinnerung", len(POSTS), 1)
A.time.now += 3600
A._maybe_alert(PROF, "p1", BAD, w)
eq("nach 60 min feuert die Erinnerung", len(POSTS), 2)
eq("als 'reminder'", POSTS[-1]["json"]["kind"], "reminder")
eq("mit demselben status wie der Alarm — kind trennt sie",
   POSTS[-1]["json"]["status"], "bad")
check("Erinnerung nennt die Dauer im detail", "offen" in POSTS[-1]["json"]["detail"])
A._maybe_alert(PROF, "p1", OK, w)
eq("Entwarnung feuert", len(POSTS), 3)
eq("als 'recovery'", POSTS[-1]["json"]["kind"], "recovery")
eq("Entwarnung geht raus, obwohl recovery_notify aus ist "
   "(die Kanäle schweigen, n8n sieht beide Flanken)", POSTS[-1]["json"]["status"], "ok")

print("Eskalations-Flanken: abgewählt")
reset()
set_hook(alarm=True, reminder=False, recovery=True)
w = wc(fail_threshold=1, ok_threshold=1, repeat_min=60)
A._maybe_alert(PROF, "p1", BAD, w)
A.time.now += 3600
A._maybe_alert(PROF, "p1", BAD, w)
eq("Erinnerung abgewählt -> nur der Alarm ging raus", len(POSTS), 1)
eq("Kanäle bekommen die Erinnerung trotzdem (Auswahl gilt nur für den Webhook)",
   POSTS[-1]["json"]["kind"], "alarm")
A._maybe_alert(PROF, "p1", OK, w)
eq("Entwarnung gewählt -> geht raus", POSTS[-1]["json"]["kind"], "recovery")

reset()
set_hook(alarm=False, recovery=False, reminder=False)
w = wc(fail_threshold=1, ok_threshold=1)
A._maybe_alert(PROF, "p1", BAD, w)
A._maybe_alert(PROF, "p1", OK, w)
eq("alles abgewählt -> gar kein Versand", len(POSTS), 0)

print("Flapping-Schutz gilt auch für den Webhook")
reset()
set_hook(alarm=True)
w = wc(fail_threshold=3)
A._maybe_alert(PROF, "p1", BAD, w)
eq("erster schlechter Lauf feuert nicht", len(POSTS), 0)
A._maybe_alert(PROF, "p1", BAD, w)
eq("zweiter auch nicht", len(POSTS), 0)
A._maybe_alert(PROF, "p1", BAD, w)
eq("erst der dritte (fail_threshold) feuert", len(POSTS), 1)

# ── 5. Digest / Report — auch OHNE Benachrichtigungs-Kanal ───────────────────
print("Digest & Report")
CHAN = {"ntfy": {"enabled": True, "server": "https://ntfy.sh", "topic": "t"}}
with open(A.PROFILES_PATH, "w", encoding="utf-8") as fh:
    json.dump({"p1": {"name": "MitKanal", "paperless_url": "http://a:8000",
                      "notifications": CHAN},
               "p2": {"name": "OhneKanal", "paperless_url": "http://b:8000"}}, fh)
A._profile_digest_line = lambda url, token, gc: "✓ 5 Dokumente"

reset()
set_hook(digest=True)
n = A._send_digest()
eq("Digest erreicht BEIDE Profile — auch das ohne Kanal (n8n allein genügt)", n, 2)
eq("zwei Webhook-Aufrufe", len(POSTS), 2)
eq("als 'digest'", POSTS[-1]["json"]["kind"], "digest")
eq("status 'info'", POSTS[-1]["json"]["status"], "info")
eq("Profilname dabei", sorted(p["json"]["profile"] for p in POSTS), ["MitKanal", "OhneKanal"])

reset()
set_hook(digest=False)
n = A._send_digest()
eq("Digest abgewählt -> nur das Profil mit Kanal wird bedient", n, 1)
eq("und nichts an den Webhook", len(POSTS), 0)

print("Report")
os.makedirs(A.WATCH_DIR, exist_ok=True)
os.makedirs(A.METRICS_DIR, exist_ok=True)
for pid in ("p1", "p2"):
    with open(A._watch_path(pid), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": int(NOW - 86400), "c": {"downtime": "ok"}}) + "\n")
reset()
set_hook(report=True)
n = A._send_report("week")
eq("Report erreicht ebenfalls beide Profile", n, 2)
eq("als 'report'", POSTS[-1]["json"]["kind"], "report")
eq("status 'info'", POSTS[-1]["json"]["status"], "info")
check("Reporttext im detail", "Rückblick über 7 Tage" in POSTS[-1]["json"]["detail"])
reset()
set_hook(report=False)
n = A._send_report("week")
eq("Report abgewählt -> nur Kanal-Profil", n, 1)
eq("nichts an den Webhook", len(POSTS), 0)

# ── 6. Arten-Liste ───────────────────────────────────────────────────────────
print("Arten-Liste")
kinds = [k for k, _ in A._WEBHOOK_KINDS]
eq("fünf konfigurierbare Arten", kinds,
   ["alarm", "reminder", "recovery", "digest", "report"])
eq("jede Art hat einen Standardwert", sorted(A._WEBHOOK_KIND_DEFAULTS), sorted(kinds))
check("'update' wird nicht angeboten — es wird nirgends gefeuert", "update" not in kinds)
check("'error' wird nicht angeboten — es wird nirgends gefeuert", "error" not in kinds)
check("jede Art hat eine Beschriftung", all(lbl for _, lbl in A._WEBHOOK_KINDS))
set_hook(alarm=True)
check("_webhook_wants: gewählte Art", A._webhook_wants("alarm"))
check("_webhook_wants: abgewählte Art", not A._webhook_wants("digest"))
check("_webhook_wants: 'test' immer", A._webhook_wants("test"))
set_hook(enabled=False)
check("_webhook_wants: abgeschaltet -> nie", not A._webhook_wants("alarm"))

print("\n%d Prüfungen, %d Fehler" % (_count[0], len(_fails)))
for f in _fails:
    print("  - " + f)
sys.exit(1 if _fails else 0)
