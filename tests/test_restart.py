"""Tests des Neustart-Verhaltens (v1.9.1) — _restore_watch_state.

Hintergrund: Bis v1.9.0 lud kein Neustart den gespiegelten Zustand zurück. Folge: ein
Deploy in der Digest-/Report-Stunde schickte beides ein zweites Mal, und die komplette
Eskalation (offene Alarme, Streaks, Erinnerungstakt) fing wieder bei null an — ein längst
gemeldeter Alarm galt erneut als neu.

Kein Netzwerk. CONFIG_DIR zeigt auf ein Temp-Verzeichnis.

Braucht die Abhängigkeiten aus app/requirements.txt (Flask & Co.) — im System-Python
fehlen die, also einmalig ein venv anlegen:

    python -m venv .venv
    .venv/Scripts/pip install -r app/requirements.txt
    .venv/Scripts/python tests/test_restart.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_CFG = tempfile.mkdtemp(prefix="portal-restart-")
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
A.init_config()

FRESH = {"owner": False, "last_run": None, "next_run": None, "results": {},
         "alerts_active": set(), "alert_meta": {}, "last_error": None,
         "last_metrics": None, "last_digest": None, "last_report": None,
         "last_heartbeat": None, "last_webhook": None}


def running_state():
    """So sieht der Zustand aus, während das Portal läuft."""
    A._watcher_state.update({
        "owner": True,
        "last_run": 1700000000, "next_run": 1700003600,
        "last_digest": "2026-07-15", "last_report": "2026-07-15",
        "last_heartbeat": 1700000001, "last_metrics": 1700000002,
        "last_webhook": {"ts": 1700000003, "ok": True, "detail": "HTTP 200",
                         "event": "downtime", "kind": "alarm"},
        "results": {"p1": {"name": "Alpha", "ts": 1700000000, "checks": []}},
        "alerts_active": {("p1", "downtime")},
        "alert_meta": {"p1|downtime": {"fail": 3, "ok": 0, "since": 1699990000,
                                       "last": 1699999000, "reps": 2},
                       "p1|drift": {"fail": 1, "ok": 0, "since": None, "last": None,
                                    "reps": 0}},
        "last_error": "Irgendein alter Fehler",
    })
    A._save_watch_state()


def restart():
    """Neustart simulieren: Speicher auf Werkszustand, dann wie before_request booten."""
    A._watcher_state.update({k: (set() if k == "alerts_active" else
                                 {} if k in ("results", "alert_meta") else v)
                             for k, v in FRESH.items()})
    A._watcher_started = False
    A._restore_watch_state()


# ── 1. Ohne Wiederherstellung wäre alles weg ─────────────────────────────────
print("Ausgangslage")
running_state()
disk = json.load(open(A.WATCH_STATE_PATH, encoding="utf-8"))
eq("der Spiegel hat den Digest-Merker", disk["last_digest"], "2026-07-15")
eq("der Spiegel hat den offenen Alarm", disk["alerts_active"], ["p1|downtime"])
check("der Spiegel hat die Streaks", "p1|downtime" in disk["alert_meta"])

# ── 2. Nach dem Neustart ist alles wieder da ─────────────────────────────────
print("Neustart")
restart()
eq("last_digest überlebt", A._watcher_state["last_digest"], "2026-07-15")
eq("last_report überlebt", A._watcher_state["last_report"], "2026-07-15")
eq("last_run überlebt", A._watcher_state["last_run"], 1700000000)
eq("last_heartbeat überlebt", A._watcher_state["last_heartbeat"], 1700000001)
eq("last_metrics überlebt (sonst Extra-Datenpunkt je Deploy)",
   A._watcher_state["last_metrics"], 1700000002)
eq("last_webhook überlebt", A._watcher_state["last_webhook"]["kind"], "alarm")
eq("results überleben (sonst zeigt die UI bis zum ersten Lauf Leere)",
   list(A._watcher_state["results"]), ["p1"])
eq("der offene Alarm überlebt — als Tupel-Menge, nicht als Strings",
   A._watcher_state["alerts_active"], {("p1", "downtime")})
eq("die Streaks überleben", A._watcher_state["alert_meta"]["p1|downtime"]["fail"], 3)
eq("inklusive Erinnerungszähler", A._watcher_state["alert_meta"]["p1|downtime"]["reps"], 2)
eq("auch der Beobachtungs-Kandidat", A._watcher_state["alert_meta"]["p1|drift"]["fail"], 1)

print("Neustart: was bewusst NICHT überlebt")
check("next_run bleibt leer -> nach dem Neustart wird sofort geprüft",
      A._watcher_state["next_run"] is None)
check("last_error lebt nicht wieder auf", A._watcher_state["last_error"] is None)
check("owner wird nicht aus dem Spiegel geerbt", not A._watcher_state["owner"])

# ── 3. Die eigentliche Folge: keine Dubletten ────────────────────────────────
print("Keine Dubletten nach einem Deploy")
lt = datetime(2026, 7, 15, 8, 0)      # Digest-Stunde, Report-Tag
wc = dict(A._WATCHER_DEFAULTS)
wc.update({"enabled": True, "digest_enabled": True, "digest_hour": 8,
           "report_enabled": True, "report_period": "month", "report_day": 15,
           "report_hour": 8})
restart()
today = lt.strftime("%Y-%m-%d")
check("Digest wird NICHT ein zweites Mal fällig",
      not (lt.hour == wc["digest_hour"] and A._watcher_state.get("last_digest") != today))
check("Report wird NICHT ein zweites Mal fällig",
      not A._report_due(lt, wc, A._watcher_state.get("last_report")))
check("am nächsten Monat aber schon wieder",
      A._report_due(datetime(2026, 8, 15, 8, 0), wc, A._watcher_state.get("last_report")))

print("Keine Dubletten bei den Alarmen")
SENT = []
A._dispatch_notification = lambda *a, **k: (SENT.append(a[1]), (0, []))[1]
A._fire_webhook = lambda *a, **k: (True, "ok")
restart()
c = dict(A._WATCHER_DEFAULTS)
c["checks"] = dict(A._WATCHER_DEFAULTS["checks"])
c.update({"fail_threshold": 2, "repeat_min": 0})
BAD = {"event": "downtime", "label": "Erreichbarkeit", "status": "bad", "detail": "Weg."}
A._maybe_alert({"name": "Alpha"}, "p1", BAD, c)
eq("der schon gemeldete Alarm wird nach dem Neustart NICHT erneut gemeldet", len(SENT), 0)
check("und bleibt offen", ("p1", "downtime") in A._watcher_state["alerts_active"])

print("Beobachtungs-Streak läuft weiter, statt neu zu zählen")
restart()
del SENT[:]
DRIFT = {"event": "drift", "label": "Konfig-Drift", "status": "bad", "detail": "3 fehlen."}
A._maybe_alert({"name": "Alpha"}, "p1", DRIFT, c)
eq("Streak stand auf 1, ein weiterer schlechter Lauf reißt die Schwelle 2 -> Alarm",
   len(SENT), 1)
eq("und zwar für drift", SENT[0], "drift")

# ── 4. Randfälle ─────────────────────────────────────────────────────────────
print("Randfälle")
os.remove(A.WATCH_STATE_PATH)
restart()
check("ohne Spiegel-Datei kein Absturz (Erstinstallation)",
      A._watcher_state["last_digest"] is None)
eq("und leerer Alarm-Zustand", A._watcher_state["alerts_active"], set())
with open(A.WATCH_STATE_PATH, "w", encoding="utf-8") as fh:
    fh.write("{kaputt")
restart()
check("kaputte Spiegel-Datei kein Absturz", A._watcher_state["last_digest"] is None)
with open(A.WATCH_STATE_PATH, "w", encoding="utf-8") as fh:
    json.dump({"last_digest": "2026-07-01"}, fh)   # alter Spiegel, viele Felder fehlen
restart()
eq("alter Spiegel ohne die neuen Felder lädt", A._watcher_state["last_digest"], "2026-07-01")
check("fehlende Felder bleiben leer statt zu krachen",
      A._watcher_state["last_report"] is None and A._watcher_state["alert_meta"] == {})
eq("und alerts_active ist eine Menge", A._watcher_state["alerts_active"], set())

print("Rundlauf: speichern -> neu starten -> speichern verliert nichts")
running_state()
restart()
A._save_watch_state()
d2 = json.load(open(A.WATCH_STATE_PATH, encoding="utf-8"))
eq("Digest-Merker steht nach dem Neustart-Speichern noch im Spiegel",
   d2["last_digest"], "2026-07-15")
eq("offener Alarm ebenfalls", d2["alerts_active"], ["p1|downtime"])
check("Streaks ebenfalls", "p1|downtime" in d2["alert_meta"])
eq("results ebenfalls — der Nicht-Owner überschreibt sie nicht mehr mit Leere",
   list(d2["results"]), ["p1"])

print("\n%d Prüfungen, %d Fehler" % (_count[0], len(_fails)))
for f in _fails:
    print("  - " + f)
sys.exit(1 if _fails else 0)
