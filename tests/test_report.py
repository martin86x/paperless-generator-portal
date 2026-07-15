"""Tests des Wochen-/Monatsreports (Z4) — Taktung, Ausfall-Erkennung, Auswertung, Text.

Kein Netzwerk: Versand und Protokoll sind gestubbt, _test_paperless/_api_count sind Fallen,
die den Test scheitern lassen — der Report muss ohne jede Instanz-Abfrage auskommen, das
ist seine zentrale Zusage. CONFIG_DIR zeigt auf ein Temp-Verzeichnis, die Wächter-Schleife
bleibt aus.

Braucht die Abhängigkeiten aus app/requirements.txt (Flask & Co.) — im System-Python
fehlen die, also einmalig ein venv anlegen:

    python -m venv .venv
    .venv/Scripts/pip install -r app/requirements.txt
    .venv/Scripts/python tests/test_report.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_CFG = tempfile.mkdtemp(prefix="portal-report-")
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
    """Ersetzt das time-Modul in app.py: time() liefert die Testuhr, Rest bleibt echt.
    Nötig, weil _send_report/_report_preview ihr Fenster aus time.time() bilden — ohne
    Shim läge die gefälschte Historie außerhalb und der Report wäre immer leer."""

    def __init__(self):
        import time as _t
        self._t = _t
        self.now = NOW

    def time(self):
        return self.now

    def __getattr__(self, k):
        return getattr(self._t, k)


CLOCK = Clock()
A.time = CLOCK


def _trap(*a, **k):
    raise AssertionError("Der Report hat einen Instanz-Aufruf ausgelöst!")


A._test_paperless = _trap
A._api_count = _trap
A._chk_downtime = _trap
A._log_activity = lambda *a, **k: None

SENT = []


def _stub_dispatch(prof, event, title, message, only=None, bump=0):
    SENT.append({"event": event, "title": title, "msg": message, "bump": bump})
    return 0, []


A._dispatch_notification = _stub_dispatch

DAY = 86400.0
P1, P2, P3 = "aaaa1111", "bbbb2222", "cccc3333"


def wc(**over):
    c = dict(A._WATCHER_DEFAULTS)
    c["checks"] = dict(A._WATCHER_DEFAULTS["checks"])
    c.update(over)
    return c


def write_profiles(**over):
    """P1: Kanal aktiv + Historie. P2: Historie, aber KEIN Kanal. P3: Kanal, keine Historie."""
    chan = {"ntfy": {"enabled": True, "server": "https://ntfy.sh", "topic": "t"}}
    profs = {
        P1: {"name": "Alpha", "paperless_url": "http://a:8000", "notifications": chan},
        P2: {"name": "Beta", "paperless_url": "http://b:8000"},
        P3: {"name": "Gamma", "paperless_url": "http://c:8000", "notifications": chan},
    }
    profs.update(over)
    with open(A.PROFILES_PATH, "w", encoding="utf-8") as fh:
        json.dump(profs, fh)


def write_watch(pid, rows):
    os.makedirs(A.WATCH_DIR, exist_ok=True)
    with open(A._watch_path(pid), "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def write_metrics(pid, rows):
    os.makedirs(A.METRICS_DIR, exist_ok=True)
    with open(A._metrics_path(pid), "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


A.init_config()

# ── 1. Ausfall-Erkennung ─────────────────────────────────────────────────────
print("Ausfall-Erkennung")


def rows(*states, start=0, step=3600):
    return [{"ts": start + i * step, "c": {"downtime": s}}
            for i, s in enumerate(states)]


eq("keine Läufe -> keine Strecke", A._outages([]), [])
eq("alles ok -> keine Strecke", A._outages(rows("ok", "ok")), [])
eq("ein bad, dann ok -> eine geschlossene Strecke",
   A._outages(rows("ok", "bad", "ok")), [(3600, 7200)])
eq("zwei bad hintereinander -> EINE Strecke, nicht zwei",
   A._outages(rows("ok", "bad", "bad", "ok")), [(3600, 10800)])
eq("noch offen -> Ende None", A._outages(rows("ok", "bad")), [(3600, None)])
eq("'unknown' zerteilt eine Strecke NICHT",
   A._outages(rows("bad", "unknown", "bad", "ok")), [(0, 10800)])
eq("'unknown' beendet auch keine offene Strecke",
   A._outages(rows("bad", "unknown")), [(0, None)])
eq("zwei getrennte Strecken",
   A._outages(rows("bad", "ok", "bad", "ok")), [(0, 3600), (7200, 10800)])
eq("anderes Event wird sauber getrennt",
   A._outages([{"ts": 0, "c": {"drift": "bad", "downtime": "ok"}}], "drift"), [(0, None)])

print("Längster Ausfall")
lo = A._longest_outage(rows("ok", "bad", "ok", "bad", "bad", "bad", "ok"), now=NOW)
eq("nimmt die längste Strecke, nicht die erste", lo["sec"], 3 * 3600)
eq("mit Startzeitpunkt", lo["start"], 3 * 3600)
check("ohne Ausfall -> None", A._longest_outage(rows("ok", "ok"), now=NOW) is None)
lo2 = A._longest_outage([{"ts": NOW - 7200, "c": {"downtime": "bad"}}], now=NOW)
eq("offene Strecke zählt bis jetzt", lo2["sec"], 7200)
check("und ist als offen markiert", lo2["end"] is None)

# ── 2. Auswertung ────────────────────────────────────────────────────────────
print("Auswertung")
write_profiles()
write_watch(P1, [
    {"ts": NOW - 20 * DAY, "c": {"downtime": "ok"}},          # außerhalb der Woche
    {"ts": NOW - 5 * DAY, "c": {"downtime": "ok", "drift": "ok"}},
    {"ts": NOW - 4 * DAY, "c": {"downtime": "bad", "drift": "ok"}},
    {"ts": NOW - 4 * DAY + 3600, "c": {"downtime": "ok", "drift": "bad"}},
    {"ts": NOW - 1 * DAY, "c": {"downtime": "unknown", "drift": "ok"}},
    {"ts": NOW - 3600, "c": {"downtime": "ok", "drift": "ok"}},
])
write_metrics(P1, [
    {"ts": NOW - 20 * DAY, "total": 1, "inbox": 99, "lat": 999},   # außerhalb
    {"ts": NOW - 6 * DAY, "total": 1000, "inbox": 11, "lat": 50},
    {"ts": NOW - 3 * DAY, "total": 1020, "inbox": 7, "lat": 300},
    {"ts": NOW - 3600, "total": 1042, "inbox": 3, "lat": 70},
])
st = A._report_stats(P1, NOW - 7 * DAY, NOW)
eq("nur Läufe im Fenster", st["runs"], 5)
eq("downtime ok-Läufe", st["checks"]["downtime"]["ok"], 3)
eq("downtime bad-Läufe", st["checks"]["downtime"]["bad"], 1)
eq("Uptime: 3 ok / 1 bad = 75 % ('unknown' zählt nicht)",
   st["checks"]["downtime"]["pct"], 75.0)
eq("drift auffällig gezählt", st["checks"]["drift"]["bad"], 1)
eq("Ausfalldauer aus den Zeitstempeln", st["outage"]["sec"], 3600)
eq("Dokumente: letzter Wert im Fenster", st["docs"]["last"], 1042)
eq("Wachstum gegen den ERSTEN Wert im Fenster, nicht gegen den ältesten überhaupt",
   st["docs"]["delta"], 42)
eq("Posteingang vorher", st["inbox"]["first"], 11)
eq("Posteingang jetzt", st["inbox"]["last"], 3)
eq("Latenz-Median (50/300/70 -> 70)", st["lat"]["med"], 70)
eq("Latenz-Max ignoriert den Wert außerhalb des Fensters", st["lat"]["max"], 300)

print("Auswertung: Randfälle")
st0 = A._report_stats("gibtsnicht", NOW - 7 * DAY, NOW)
eq("Profil ohne Historie -> 0 Läufe", st0["runs"], 0)
check("und keine Kennzahlen", st0["docs"] is None and st0["lat"] is None)
write_watch(P2, [{"ts": NOW - 2 * DAY, "c": {"downtime": "ok"}}])
st2 = A._report_stats(P2, NOW - 7 * DAY, NOW)
eq("Wächter-Historie ohne Kennzahl-Historie geht", st2["runs"], 1)
check("Kennzahlen dann leer statt Absturz", st2["docs"] is None)
eq("Median ungerade", A._median([3, 1, 2]), 2)
eq("Median gerade", A._median([1, 2, 3, 4]), 2.5)
check("Median leer -> None", A._median([]) is None)
eq("Tausendertrennung deutsch", A._fmt_int(1042), "1.042")
eq("Tausendertrennung groß", A._fmt_int(1234567), "1.234.567")

# ── 3. Reporttext ────────────────────────────────────────────────────────────
print("Reporttext")
TXT = A._report_text(7, st, NOW)
check("nennt den Zeitraum", "7 Tage" in TXT)
check("nennt die Zahl der Läufe", "5 Prüfläufe" in TXT)
check("Uptime ohne unnötige Nachkommastelle (75 statt 75.0)", "75 %" in TXT)
check("längster Ausfall mit Dauer", "Längster Ausfall: 1 h" in TXT)
check("Ausfall mit Datum", "ab " in TXT)
check("Drift-Auffälligkeit benannt", "Konfig-Drift: 1 auffällige Läufe" in TXT)
check("Dokumente mit Vorzeichen", "+42 im Zeitraum" in TXT)
check("Dokumente pro Tag", "6.0/Tag" in TXT)
check("Posteingang als abgebaut erkannt", "Posteingang: 3 (abgebaut, vorher 11)" in TXT)
check("Antwortzeit", "Ø 70 ms, max 300 ms" in TXT)
check("kein leerer Platzhalter im Text", "None" not in TXT)

print("Reporttext: Varianten")
clean = A._report_stats(P2, NOW - 7 * DAY, NOW)
T2 = A._report_text(7, clean, NOW)
check("ohne Ausfall steht das auch da", "Kein Ausfall im Zeitraum." in T2)
check("und keine Ausfall-Zeile", "Längster Ausfall" not in T2)
check("ohne Kennzahlen keine Dokument-Zeile", "Dokumente:" not in T2)
write_watch(P3, [{"ts": NOW - 2 * DAY, "c": {"downtime": "bad"}}])
T3 = A._report_text(7, A._report_stats(P3, NOW - 7 * DAY, NOW), NOW)
check("offener Ausfall wird als offen ausgewiesen", "noch offen" in T3)
write_metrics(P3, [{"ts": NOW - 5 * DAY, "total": 50, "inbox": 2, "lat": 10},
                   {"ts": NOW - DAY, "total": 40, "inbox": 5, "lat": 10}])
T4 = A._report_text(7, A._report_stats(P3, NOW - 7 * DAY, NOW), NOW)
check("Schrumpfen wird mit Minus gezeigt", "-10 im Zeitraum" in T4)
check("wachsender Posteingang benannt", "gewachsen, vorher 2" in T4)
write_metrics(P3, [{"ts": NOW - 5 * DAY, "total": 50, "inbox": 4, "lat": 10},
                   {"ts": NOW - DAY, "total": 50, "inbox": 4, "lat": 10}])
T5 = A._report_text(7, A._report_stats(P3, NOW - 7 * DAY, NOW), NOW)
check("unveränderter Posteingang benannt", "Posteingang: 4 (unverändert)" in T5)
write_watch(P3, [{"ts": NOW - 2 * DAY, "c": {"downtime": "unknown"}}])
T6 = A._report_text(7, A._report_stats(P3, NOW - 7 * DAY, NOW), NOW)
check("nur 'unknown' -> ehrlich 'keine verwertbaren Läufe'",
      "keine verwertbaren Läufe" in T6)

# ── 4. Empfänger-Auswahl ─────────────────────────────────────────────────────
print("Empfänger-Auswahl")
write_watch(P3, [])
del SENT[:]
n = A._send_report("week")
eq("nur Profile mit Kanal UND Historie", n, 1)
eq("und zwar Alpha", SENT[0]["title"], "Paperless-Report (7 Tage): Alpha")
eq("Ereignis 'report'", SENT[0]["event"], "report")
check("Beta hat Historie, aber keinen Kanal -> nichts",
      not any("Beta" in s["title"] for s in SENT))
check("Gamma hat Kanal, aber keine Läufe -> kein Report voller Striche",
      not any("Gamma" in s["title"] for s in SENT))
eq("Standard-Priorität ist leise", A._NOTIFY_DEFAULT_PRIO["report"], -1)
check("'report' ist ein konfigurierbares Ereignis",
      "report" in dict(A._NOTIFY_EVENTS))
check("bestehende Profile bekommen die Prio automatisch",
      A._notif_of({})["priorities"]["report"] == -1)

del SENT[:]
A._send_report("month")
check("Monatsreport nennt 30 Tage", "30 Tage" in SENT[0]["title"])
prev = A._report_preview("week", NOW)
eq("Vorschau zeigt beide Profile mit Historie (auch das ohne Kanal)", len(prev), 2)
eq("Vorschau alphabetisch", [p["name"] for p in prev], ["Alpha", "Beta"])
check("Vorschau markiert fehlenden Kanal",
      [p["channels"] for p in prev] == [True, False])
del SENT[:]
eq("Vorschau versendet nichts", len(SENT), 0)

# ── 5. Taktung ───────────────────────────────────────────────────────────────
print("Taktung")
MON = datetime(2026, 7, 13, 8, 0)      # ein Montag
TUE = datetime(2026, 7, 14, 8, 0)
eq("Montag ist Wochentag 0", MON.weekday(), 0)
c = wc(report_enabled=True, report_period="week", report_weekday=0, report_hour=8)
check("Montag 8 Uhr -> fällig", A._report_due(MON, c, None))
check("Dienstag 8 Uhr -> nicht fällig", not A._report_due(TUE, c, None))
check("Montag 9 Uhr -> nicht fällig",
      not A._report_due(datetime(2026, 7, 13, 9, 0), c, None))
check("schon heute gesendet -> nicht nochmal", not A._report_due(MON, c, "2026-07-13"))
check("gestern gesendet -> heute wieder", A._report_due(MON, c, "2026-07-06"))
check("abgeschaltet -> nie", not A._report_due(MON, wc(**dict(c, report_enabled=False)), None))

cm = wc(report_enabled=True, report_period="month", report_day=13, report_hour=8)
check("Monatsmodus: am 13. fällig", A._report_due(MON, cm, None))
check("Monatsmodus: am 14. nicht", not A._report_due(TUE, cm, None))
check("Monatsmodus ignoriert den Wochentag",
      A._report_due(datetime(2026, 8, 13, 8, 0), cm, None))
check("Wochenmodus ignoriert den Kalendertag",
      A._report_due(datetime(2026, 7, 20, 8, 0), c, None))

print("Taktung: Konfig-Grenzen")
def cfg_of(**w):
    cf = A.load_config()
    cf["watcher"] = w
    A.save_config(cf)
    return A._watcher_cfg()


eq("Tag 31 wird auf 28 gedeckelt (Februar hätte den Report verschluckt)",
   cfg_of(report_day=31)["report_day"], 28)
eq("Tag 0 -> 1", cfg_of(report_day=0)["report_day"], 1)
eq("Wochentag 9 -> 6", cfg_of(report_weekday=9)["report_weekday"], 6)
eq("Stunde 99 -> 23", cfg_of(report_hour=99)["report_hour"], 23)
eq("unbekannter Zeitraum -> week", cfg_of(report_period="quartal")["report_period"], "week")
eq("Müll im Tag-Feld -> Standard", cfg_of(report_day="abc")["report_day"], 1)
eq("Standard ist aus", cfg_of()["report_enabled"], False)
eq("Standard-Zeitraum Woche", cfg_of()["report_period"], "week")
eq("alte config.json ohne report-Felder lädt", cfg_of(enabled=True)["report_day"], 1)
eq("7 Tage bei week", A._report_days("week"), 7)
eq("30 Tage bei month", A._report_days("month"), 30)
eq("unbekannt -> 7", A._report_days("quatsch"), 7)

# ── 6. Zustand ───────────────────────────────────────────────────────────────
print("Zustand")
A._watcher_state["last_report"] = "2026-07-13"
A._watcher_state["owner"] = True
A._save_watch_state()
with open(A.WATCH_STATE_PATH, encoding="utf-8") as fh:
    disk = json.load(fh)
eq("last_report wird gespiegelt", disk["last_report"], "2026-07-13")
A._watcher_state["owner"] = False
A._watcher_state["last_run"] = None
eq("und vom Nicht-Owner gelesen", A._load_watch_state()["last_report"], "2026-07-13")
check("alte watcher-state.json ohne last_report lädt", True)

print("\n%d Prüfungen, %d Fehler" % (_count[0], len(_fails)))
for f in _fails:
    print("  - " + f)
sys.exit(1 if _fails else 0)
