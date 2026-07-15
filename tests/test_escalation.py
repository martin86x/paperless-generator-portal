"""Tests der Wächter-Eskalation (Z2) — Flapping-Schutz, Erinnerungs-Backoff, Entwarnung.

Keine Netzwerk-Zugriffe: Versand, Webhook und Protokoll werden gestubbt und nur
mitgeschrieben. Die Uhr ist gefälscht, damit Backoff-Abstände ohne Warten prüfbar sind.
CONFIG_DIR zeigt auf ein Temp-Verzeichnis, die Wächter-Schleife bleibt aus.

Braucht die Abhängigkeiten aus app/requirements.txt (Flask & Co.) — im System-Python
fehlen die, also einmalig ein venv anlegen:

    python -m venv .venv
    .venv/Scripts/pip install -r app/requirements.txt
    .venv/Scripts/python tests/test_escalation.py
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_CFG = tempfile.mkdtemp(prefix="portal-esc-")
os.environ["CONFIG_DIR"] = _CFG
os.environ["SITE_DIR"] = os.path.join(os.path.dirname(_HERE), "site")
os.environ["PORTAL_WATCHER"] = "0"   # Hintergrund-Schleife im Test nicht starten
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


class Clock:
    """Ersetzt das time-Modul in app.py: time() liefert die Testuhr, Rest bleibt echt."""

    def __init__(self):
        import time as _t
        self._t = _t
        self.now = 1_700_000_000.0

    def time(self):
        return self.now

    def tick(self, minutes):
        self.now += minutes * 60

    def __getattr__(self, k):
        return getattr(self._t, k)


CLOCK = Clock()
A.time = CLOCK

SENT = []      # (event, title, message, prio_bump)
HOOKS = []     # (event, status)
LOGS = []      # (level, text)


def _stub_dispatch(prof, event, title, message, only=None, bump=0):
    SENT.append({"event": event, "title": title, "msg": message, "bump": bump})
    return 0, []


def _stub_hook(kind, event, profile, status, detail, wc=None):
    HOOKS.append({"kind": kind, "event": event, "status": status})
    return True, "ok"


def _stub_log(kind, text, level="info", detail=""):
    LOGS.append({"level": level, "text": text})


A._dispatch_notification = _stub_dispatch
A._fire_webhook = _stub_hook
A._log_activity = _stub_log

PROF = {"name": "TESTINStanz"}
BAD = {"event": "downtime", "label": "Erreichbarkeit", "status": "bad", "detail": "Weg."}
OK = {"event": "downtime", "label": "Erreichbarkeit", "status": "ok", "detail": "Da."}
UNK = {"event": "downtime", "label": "Erreichbarkeit", "status": "unknown", "detail": "?"}


def wc(**over):
    """Wächter-Konfiguration aus den Defaults plus Abweichungen."""
    c = dict(A._WATCHER_DEFAULTS)
    c["checks"] = dict(A._WATCHER_DEFAULTS["checks"])
    c.update(over)
    return c


def reset():
    del SENT[:], HOOKS[:], LOGS[:]
    A._watcher_state["alerts_active"] = set()
    A._watcher_state["alert_meta"] = {}


def feed(c, conf, n=1):
    for _ in range(n):
        A._maybe_alert(PROF, "p1", c, conf)


def meta():
    return A._watcher_state["alert_meta"]["p1|downtime"]


def active():
    return ("p1", "downtime") in A._watcher_state["alerts_active"]


# ── 1. Flapping-Schutz ───────────────────────────────────────────────────────
print("Flapping-Schutz")
reset()
c = wc(fail_threshold=2)
feed(BAD, c)
eq("erster schlechter Lauf alarmiert NICHT", len(SENT), 0)
check("und gilt noch nicht als offen", not active())
eq("aber der Streak zählt", meta()["fail"], 1)
feed(BAD, c)
eq("zweiter schlechter Lauf alarmiert", len(SENT), 1)
check("jetzt offen", active())
eq("Webhook feuert mit", HOOKS[-1]["status"], "bad")
eq("Protokoll auf Fehler-Stufe", LOGS[-1]["level"], "err")

reset()
feed(BAD, wc(fail_threshold=1))
eq("Schwelle 1 alarmiert sofort", len(SENT), 1)

reset()
c = wc(fail_threshold=3)
feed(BAD, c)
feed(OK, c)
feed(BAD, c, 2)
eq("guter Lauf setzt den Streak zurück", len(SENT), 0)
eq("Streak zählt nach dem Reset neu", meta()["fail"], 2)

reset()
c = wc(fail_threshold=2)
feed(BAD, c)
feed(UNK, c)
eq("'unknown' alarmiert nicht", len(SENT), 0)
eq("'unknown' friert den Streak ein", meta()["fail"], 1)
feed(BAD, c)
eq("nach 'unknown' zählt der alte Streak weiter", len(SENT), 1)

reset()
feed(UNK, wc(fail_threshold=1))
eq("'unknown' allein löst nie aus", len(SENT), 0)


# ── 2. Erinnerungen + Backoff ────────────────────────────────────────────────
print("Erinnerungen + Backoff")
reset()
c = wc(fail_threshold=1, repeat_min=0)
feed(BAD, c)
CLOCK.tick(600)
feed(BAD, c, 5)
eq("repeat_min=0 -> keine Erinnerung, nur der Erstalarm", len(SENT), 1)

reset()
c = wc(fail_threshold=1, repeat_min=60, repeat_backoff=False, escalate_after=0)
feed(BAD, c)
CLOCK.tick(59)
feed(BAD, c)
eq("vor Ablauf des Abstands kommt nichts", len(SENT), 1)
CLOCK.tick(1)
feed(BAD, c)
eq("nach 60 min kommt die Erinnerung", len(SENT), 2)
check("Erinnerung ist als solche betitelt", "weiterhin offen" in SENT[-1]["title"])
check("Erinnerung nennt die Dauer", "Seit 1 h offen" in SENT[-1]["msg"])
eq("ohne Backoff kein lauter werden", SENT[-1]["bump"], 0)
# Seit v1.9.0 geht die Erinnerung auch an den Webhook — als eigene Art, damit die
# Gegenstelle sie vom Erst-Alarm unterscheiden kann (beide haben status 'bad'). OB sie
# rausgeht, entscheidet _fire_webhook selbst (Auswahl in der Konfig, hier gestubbt).
eq("Erinnerung geht an den Webhook", len(HOOKS), 2)
eq("und zwar als Art 'reminder'", HOOKS[-1]["kind"], "reminder")
eq("der Erst-Alarm war Art 'alarm'", HOOKS[0]["kind"], "alarm")
eq("beide tragen denselben status — nur kind trennt sie",
   [h["status"] for h in HOOKS], ["bad", "bad"])
CLOCK.tick(60)
feed(BAD, c)
eq("konstanter Abstand: nächste nach weiteren 60 min", len(SENT), 3)

reset()
c = wc(fail_threshold=1, repeat_min=60, repeat_backoff=True, escalate_after=0)
feed(BAD, c)
CLOCK.tick(60)
feed(BAD, c)
eq("1. Erinnerung nach 60 min", len(SENT), 2)
CLOCK.tick(60)
feed(BAD, c)
eq("Backoff: nach 60 min ist die 2. noch nicht fällig", len(SENT), 2)
CLOCK.tick(60)
feed(BAD, c)
eq("2. Erinnerung erst nach 120 min", len(SENT), 3)
CLOCK.tick(180)
feed(BAD, c)
eq("3. Erinnerung erst nach 240 min", len(SENT), 3)
CLOCK.tick(60)
feed(BAD, c)
eq("... und dann ja", len(SENT), 4)

print("Backoff-Rechnung")
c = wc(repeat_min=60, repeat_backoff=True)
eq("0 Erinnerungen -> 60 min", A._repeat_gap_min(c, 0), 60)
eq("1 Erinnerung -> 120 min", A._repeat_gap_min(c, 1), 120)
eq("4 Erinnerungen -> 960 min", A._repeat_gap_min(c, 4), 960)
eq("Deckel bei 24 h", A._repeat_gap_min(c, 9), 1440)
eq("Deckel hält auch bei absurden Zählern", A._repeat_gap_min(c, 9999), 1440)
eq("ohne Backoff konstant", A._repeat_gap_min(wc(repeat_min=90, repeat_backoff=False), 7), 90)


# ── 3. Prioritäts-Eskalation ─────────────────────────────────────────────────
print("Prioritäts-Eskalation")
reset()
c = wc(fail_threshold=1, repeat_min=60, repeat_backoff=False, escalate_after=2)
feed(BAD, c)
CLOCK.tick(60)
feed(BAD, c)
eq("1. Erinnerung noch normal laut", SENT[-1]["bump"], 0)
CLOCK.tick(60)
feed(BAD, c)
eq("ab der 2. Erinnerung eine Stufe lauter", SENT[-1]["bump"], 1)
CLOCK.tick(60)
feed(BAD, c)
eq("und danach ebenso", SENT[-1]["bump"], 1)

reset()
c = wc(fail_threshold=1, repeat_min=30, repeat_backoff=False, escalate_after=0)
feed(BAD, c)
for _ in range(4):
    CLOCK.tick(30)
    feed(BAD, c)
eq("escalate_after=0 hebt nie an", [s["bump"] for s in SENT[1:]], [0, 0, 0, 0])

print("Prioritäts-Deckel")
eq("Anheben endet bei 2 (Notfall)", A._clamp_prio(2 + 1, 2), 2)
eq("Absenken endet bei -2 (stumm)", A._clamp_prio(-2 - 1, -2), -2)


# ── 4. Entwarnung ────────────────────────────────────────────────────────────
print("Entwarnung")
reset()
c = wc(fail_threshold=1, ok_threshold=1, recovery_notify=True)
feed(BAD, c)
CLOCK.tick(90)
feed(OK, c)
eq("Entwarnung wird gemeldet", len(SENT), 2)
check("Entwarnung ist als behoben betitelt", "behoben" in SENT[-1]["title"])
check("Entwarnung nennt die Ausfalldauer", "nach 1 h" in SENT[-1]["msg"])
eq("Entwarnung geht leiser raus", SENT[-1]["bump"], -1)
check("nicht mehr offen", not active())
eq("Webhook entwarnt ebenfalls", HOOKS[-1]["status"], "ok")
eq("als Art 'recovery'", HOOKS[-1]["kind"], "recovery")
eq("Protokoll auf ok-Stufe", LOGS[-1]["level"], "ok")

reset()
c = wc(fail_threshold=1, ok_threshold=1, recovery_notify=False)
feed(BAD, c)
feed(OK, c)
eq("ohne recovery_notify kein Kanal-Versand", len(SENT), 1)
eq("Webhook entwarnt trotzdem", HOOKS[-1]["status"], "ok")
check("Alarm ist trotzdem geschlossen", not active())

reset()
c = wc(fail_threshold=1, ok_threshold=2)
feed(BAD, c)
feed(OK, c)
check("ein guter Lauf reicht bei Schwelle 2 nicht", active())
eq("und meldet noch nichts", len(SENT), 1)
feed(OK, c)
check("zweiter guter Lauf entwarnt", not active())

reset()
c = wc(fail_threshold=1, ok_threshold=2)
feed(BAD, c)
feed(OK, c)
feed(BAD, c)
eq("Rückfall vor der Entwarnung meldet nicht erneut", len(SENT), 1)
eq("und setzt den ok-Streak zurück", meta()["ok"], 0)
check("Alarm bleibt durchgehend offen", active())

reset()
c = wc(fail_threshold=1)
feed(OK, c, 3)
eq("gute Läufe ohne offenen Alarm melden nichts", len(SENT), 0)
eq("und feuern keinen Webhook", len(HOOKS), 0)

print("Zustand nach der Entwarnung")
reset()
c = wc(fail_threshold=2, repeat_min=60)
feed(BAD, c, 2)
CLOCK.tick(120)
feed(OK, c)
eq("Meta ist zurückgesetzt: reps", meta()["reps"], 0)
eq("Meta ist zurückgesetzt: since", meta()["since"], None)
eq("Meta ist zurückgesetzt: fail", meta()["fail"], 0)
del SENT[:]
feed(BAD, c)
eq("neuer Vorfall braucht wieder die volle Schwelle", len(SENT), 0)
feed(BAD, c)
eq("und alarmiert dann erneut", len(SENT), 1)


# ── 5. Bestandsalarm nach Update (Meta fehlt) ────────────────────────────────
print("Bestandsalarm nach Update")
reset()
c = wc(fail_threshold=1, repeat_min=60, repeat_backoff=False)
A._watcher_state["alerts_active"] = {("p1", "downtime")}   # wie aus watcher-state.json
feed(BAD, c)
eq("offener Altalarm erinnert nicht sofort", len(SENT), 0)
CLOCK.tick(30)
feed(BAD, c)
eq("und auch nicht vor Ablauf des Abstands", len(SENT), 0)
CLOCK.tick(30)
feed(BAD, c)
eq("erst nach dem regulären Abstand", len(SENT), 1)


# ── 6. Aufräumen ─────────────────────────────────────────────────────────────
print("Aufräumen")
reset()
c = wc(fail_threshold=1)
A._maybe_alert(PROF, "p1", BAD, c)
A._maybe_alert(PROF, "weg", BAD, c)
eq("beide Profile im Zustand", len(A._watcher_state["alert_meta"]), 2)
A._prune_alert_state({"p1"})
eq("entferntes Profil fliegt aus dem Meta-Zustand", list(A._watcher_state["alert_meta"]), ["p1|downtime"])
eq("und aus den offenen Alarmen", A._watcher_state["alerts_active"], {("p1", "downtime")})


# ── 7. Konfiguration: Normalisierung + Persistenz ────────────────────────────
print("Konfiguration")
eq("Standard: Flapping-Schutz an", A._WATCHER_DEFAULTS["fail_threshold"], 2)
eq("Standard: keine Erinnerungen", A._WATCHER_DEFAULTS["repeat_min"], 0)
eq("Standard: Entwarnung wird gemeldet", A._WATCHER_DEFAULTS["recovery_notify"], True)

with open(os.path.join(_CFG, "config.json"), "w", encoding="utf-8") as fh:
    json.dump({"watcher": {"fail_threshold": "99", "ok_threshold": "0",
                           "repeat_min": "-5", "escalate_after": "abc"}}, fh)
g = A._watcher_cfg()
eq("fail_threshold wird auf 10 gedeckelt", g["fail_threshold"], 10)
eq("ok_threshold mindestens 1", g["ok_threshold"], 1)
eq("repeat_min nie negativ", g["repeat_min"], 0)
eq("Unsinn fällt auf den Standard zurück", g["escalate_after"], 3)

with open(os.path.join(_CFG, "config.json"), "w", encoding="utf-8") as fh:
    json.dump({"watcher": {"repeat_min": "9999"}}, fh)
eq("repeat_min auf 24 h gedeckelt", A._watcher_cfg()["repeat_min"], A._ESC_MAX_GAP_MIN)

print("Persistenz des Zustands")
reset()
c = wc(fail_threshold=1, repeat_min=60)
feed(BAD, c)
A._save_watch_state()
with open(A.WATCH_STATE_PATH, encoding="utf-8") as fh:
    st = json.load(fh)
eq("alerts_active landet als String-Liste auf Platte", st["alerts_active"], ["p1|downtime"])
check("alert_meta ist JSON-fähig und mit dabei", "p1|downtime" in st["alert_meta"])
eq("Streak überlebt das Speichern", st["alert_meta"]["p1|downtime"]["fail"], 1)


# ── 8. Dauer-Formatierung ────────────────────────────────────────────────────
print("Dauer-Formatierung")
eq("Minuten", A._fmt_dur(45 * 60), "45 min")
eq("unter einer Minute", A._fmt_dur(9), "0 min")
eq("Stunden", A._fmt_dur(3 * 3600), "3 h")
eq("Stunden werden abgerundet", A._fmt_dur(3 * 3600 + 59 * 60), "3 h")
eq("Tage", A._fmt_dur(2 * 86400 + 5 * 3600), "2 d 5 h")
eq("None ist kein Absturz", A._fmt_dur(None), "0 min")
eq("negative Dauer ist kein Absturz", A._fmt_dur(-10), "0 min")

# _fmt_rel_ts muss auch nach vorn schauen: 'nächster Lauf' liegt immer in der Zukunft
# und stand vorher als 'vor -3540 s' in der Statuszeile.
print("Relative Zeitangaben")
now = CLOCK.now
eq("Vergangenheit: Sekunden", A._fmt_rel_ts(now - 30), "vor 30 s")
eq("Vergangenheit: Minuten", A._fmt_rel_ts(now - 600), "vor 10 min")
eq("Vergangenheit: Stunden", A._fmt_rel_ts(now - 7200), "vor 2 h")
eq("Zukunft: Sekunden", A._fmt_rel_ts(now + 30), "in 30 s")
eq("Zukunft: Minuten", A._fmt_rel_ts(now + 600), "in 10 min")
eq("Zukunft: Stunden", A._fmt_rel_ts(now + 7200), "in 2 h")
check("weit voraus -> absolutes Datum statt 'in 300 h'",
      "in " not in A._fmt_rel_ts(now + 40 * 86400))
check("weit zurück -> absolutes Datum", "vor " not in A._fmt_rel_ts(now - 40 * 86400))
eq("kein Zeitstempel", A._fmt_rel_ts(None), "—")
eq("0 gilt als 'kein Zeitstempel'", A._fmt_rel_ts(0), "—")


# ── Ergebnis ─────────────────────────────────────────────────────────────────
print("")
if _fails:
    print("%d von %d Prüfungen FEHLGESCHLAGEN:" % (len(_fails), _count[0]))
    for f in _fails:
        print("  - " + f)
    sys.exit(1)
print("Alle %d Prüfungen bestanden." % _count[0])
