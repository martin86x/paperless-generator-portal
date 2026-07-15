"""Tests des Prometheus-Endpunkts (Z3) — Auth-Gate, Exposition, Format, Scrape-Kosten.

Kein Netzwerk: _test_paperless/_api_count/requests werden durch Fallen ersetzt, die den
Test scheitern lassen, falls ein Scrape sie doch anfasst — genau das ist die zentrale
Zusage des Endpunkts. CONFIG_DIR zeigt auf ein Temp-Verzeichnis, die Wächter-Schleife
bleibt aus.

Braucht die Abhängigkeiten aus app/requirements.txt (Flask & Co.) — im System-Python
fehlen die, also einmalig ein venv anlegen:

    python -m venv .venv
    .venv/Scripts/pip install -r app/requirements.txt
    .venv/Scripts/python tests/test_metrics.py
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_CFG = tempfile.mkdtemp(prefix="portal-metrics-")
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


class NetTrap(Exception):
    pass


def _trap(*a, **k):
    raise NetTrap("Ein Scrape hat einen Netz-Aufruf ausgelöst!")


A._test_paperless = _trap
A._api_count = _trap
A._fetch_latest_version = _trap
A.requests.get = _trap
A.requests.post = _trap
A._log_activity = lambda *a, **k: None

TOK = "TestToken_1234567890"
P1, P2 = "aaaa1111", "bbbb2222"
NASTY = 'Zu "Hause"\\Keller'   # Profilname mit " und \ — muss escaped rauskommen


def write_cfg(enabled=True, token=TOK):
    cfg = A.init_config()
    cfg["is_default_pw"] = False
    cfg["metrics"] = {"enabled": enabled, "token": token}
    cfg["watcher"] = {"enabled": True, "interval_min": 30, "fail_threshold": 3}
    A.save_config(cfg)


def write_profiles():
    with open(A.PROFILES_PATH, "w", encoding="utf-8") as fh:
        json.dump({
            P1: {"name": "TESTINStanz", "paperless_url": "http://192.168.10.200:8000"},
            P2: {"name": NASTY, "paperless_url": "http://x:8000",
                 "watch": {"enabled": False}},
        }, fh)


def write_history():
    os.makedirs(A.WATCH_DIR, exist_ok=True)
    rows = [{"ts": 1700000000, "c": {"downtime": "ok", "drift": "ok"}},
            {"ts": 1700003600, "c": {"downtime": "bad", "drift": "ok"}},
            {"ts": 1700007200, "c": {"downtime": "unknown", "drift": "ok"}},
            {"ts": 1700010800, "c": {"downtime": "ok", "drift": "ok"}}]
    with open(A._watch_path(P1), "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    # P2 hat Historie, wird aber nicht mehr überwacht — realistisch (nachträglich
    # abgeschaltet) und der einzige Weg, das Label-Escaping wirklich durch die
    # fertige Exposition zu jagen.
    with open(A._watch_path(P2), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": 1700000000, "c": {"downtime": "ok"}}) + "\n")
    os.makedirs(A.METRICS_DIR, exist_ok=True)
    with open(A._metrics_path(P1), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": 1699000000, "total": 5, "inbox": 1,
                             "no_type": 0, "no_corr": 2, "lat": 250}) + "\n")
        fh.write(json.dumps({"ts": 1700010800, "total": 9, "inbox": 3,
                             "no_type": 1, "no_corr": 4, "lat": 120}) + "\n")


def set_state():
    A._watcher_state.update({
        "owner": True,
        "last_run": 1700010800, "next_run": 1700012600,
        "last_heartbeat": 1700010801, "last_error": None,
        "results": {P1: {"name": "TESTINStanz", "ts": 1700010800, "checks": [
            {"event": "downtime", "label": "Erreichbarkeit", "status": "ok", "detail": "Da."},
            {"event": "drift", "label": "Konfig-Drift", "status": "bad", "detail": "3 fehlen."},
            {"event": "duplicate", "label": "Duplikate", "status": "unknown", "detail": "?"},
        ]}},
        "alerts_active": {(P1, "drift")},
        "alert_meta": {"%s|drift" % P1: {"fail": 3, "ok": 0, "since": 1700000000,
                                         "last": 1700010800, "reps": 2},
                       "%s|downtime" % P1: {"fail": 1, "ok": 0, "since": 1700010800,
                                            "last": None, "reps": 0}},
    })


write_cfg()
write_profiles()
write_history()
set_state()
A.app.config["TESTING"] = True
CLIENT = A.app.test_client()


def parse(text):
    """Exposition -> {name: [(labels_dict, wert)]} + Reihenfolge/Typen für Formatprüfungen."""
    out, types, order = {}, {}, []
    for ln in text.splitlines():
        if ln.startswith("# TYPE "):
            _, _, rest = ln.partition("# TYPE ")
            n, _, t = rest.partition(" ")
            types[n] = t
            order.append(n)
            continue
        if ln.startswith("#") or not ln.strip():
            continue
        head, _, val = ln.rpartition(" ")
        name, _, lbl = head.partition("{")
        labels = {}
        if lbl:
            for part in lbl.rstrip("}").split('",'):
                k, _, v = part.partition('=')
                labels[k.strip()] = v.strip().strip('"')
        out.setdefault(name, []).append((labels, float(val)))
    return out, types, order


def body():
    with CLIENT as c:
        with c.session_transaction() as s:
            s.clear()
        r = c.get("/metrics", headers={"Authorization": "Bearer " + TOK})
        eq("Scrape liefert 200", r.status_code, 200)
        return r.get_data(as_text=True)


# ── 1. Auth-Gate ─────────────────────────────────────────────────────────────
print("Auth-Gate")
with CLIENT as c:
    r = c.get("/metrics")
    eq("ohne Token -> 401", r.status_code, 401)
    check("mit WWW-Authenticate: Bearer",
          "Bearer" in (r.headers.get("WWW-Authenticate") or ""))
    eq("falsches Token -> 401",
       c.get("/metrics", headers={"Authorization": "Bearer falsch"}).status_code, 401)
    eq("Token ohne 'Bearer '-Präfix -> 401",
       c.get("/metrics", headers={"Authorization": TOK}).status_code, 401)
    eq("Basic-Auth-Header -> 401",
       c.get("/metrics", headers={"Authorization": "Basic " + TOK}).status_code, 401)
    eq("nicht-ASCII im Header -> 401 statt TypeError",
       c.get("/metrics", headers={"Authorization": "Bearer schlüssel"}).status_code, 401)
    eq("Präfix des Tokens reicht nicht",
       c.get("/metrics", headers={"Authorization": "Bearer " + TOK[:-1]}).status_code, 401)
    eq("richtiges Token -> 200",
       c.get("/metrics", headers={"Authorization": "Bearer " + TOK}).status_code, 200)
    eq("Groß-/Kleinschreibung von 'bearer' egal",
       c.get("/metrics", headers={"Authorization": "bEaReR " + TOK}).status_code, 200)

with CLIENT as c:
    with c.session_transaction() as s:
        s["logged_in"] = True
    eq("eingeloggt ohne Token -> 200", c.get("/metrics").status_code, 200)

write_cfg(enabled=False)
with CLIENT as c:
    with c.session_transaction() as s:
        s.clear()
    eq("abgeschaltet -> 404 (verrät die Existenz nicht)",
       c.get("/metrics", headers={"Authorization": "Bearer " + TOK}).status_code, 404)
write_cfg(enabled=True, token="")
eq("aktiv aber ohne Token -> 404 statt ungeschützt",
   CLIENT.get("/metrics").status_code, 404)
write_cfg()

# ── 2. Format ────────────────────────────────────────────────────────────────
print("Format")
TEXT = body()
M, TYPES, ORDER = parse(TEXT)
check("endet mit genau einem Newline", TEXT.endswith("\n") and not TEXT.endswith("\n\n"))
eq("kein Metrik-Name doppelt deklariert", len(ORDER), len(set(ORDER)))
for n in M:
    check("Sample '%s' hat einen TYPE-Block" % n, n in TYPES)
for n, t in TYPES.items():
    check("TYPE '%s' ist ein bekannter Typ" % n, t in ("gauge", "counter"))
    check("TYPE '%s' hat mindestens ein Sample" % n, n in M)
check("Content-Type ist die Prometheus-Exposition",
      "version=0.0.4" in CLIENT.get(
          "/metrics", headers={"Authorization": "Bearer " + TOK}).headers["Content-Type"])

# ── 3. Label-Escaping ────────────────────────────────────────────────────────
print("Label-Escaping")
eq('Anführungszeichen escaped', A._prom_lbl('a"b'), 'a\\"b')
eq("Backslash escaped", A._prom_lbl("a\\b"), "a\\\\b")
eq("Zeilenumbruch escaped", A._prom_lbl("a\nb"), "a\\nb")
eq("Backslash zuerst — kein doppeltes Escapen", A._prom_lbl('\\"'), '\\\\\\"')
check("der fiese Profilname steht escaped in der Exposition",
      'profile="Zu \\"Hause\\"\\\\Keller"' in TEXT)

# ── 4. Zahlen ────────────────────────────────────────────────────────────────
print("Zahlen")
eq("True -> 1", A._prom_num(True), "1")
eq("False -> 0", A._prom_num(False), "0")
eq("int bleibt int (keine .0)", A._prom_num(1700010800), "1700010800")
eq("float gerundet", A._prom_num(0.123456789), "0.1235")
check("Zeitstempel nicht in Exponentialschreibweise", "e+" not in TEXT)

# ── 5. Inhalt: Portal-Ebene ──────────────────────────────────────────────────
print("Portal-Ebene")
eq("info trägt die Version", M["paperless_portal_info"][0][0]["version"], A.PORTAL_VERSION)
eq("Wächter als aktiv gemeldet", M["paperless_portal_watcher_enabled"][0][1], 1.0)
eq("Intervall in Sekunden (30 min)",
   M["paperless_portal_watcher_interval_seconds"][0][1], 1800.0)
eq("letzter Lauf", M["paperless_portal_watcher_last_run_timestamp_seconds"][0][1], 1700010800.0)
eq("nächster Lauf", M["paperless_portal_watcher_next_run_timestamp_seconds"][0][1], 1700012600.0)
eq("kein Fehler", M["paperless_portal_watcher_error"][0][1], 0.0)
eq("ein offener Alarm", M["paperless_portal_alerts_active"][0][1], 1.0)
eq("zwei Profile", M["paperless_portal_profiles_total"][0][1], 2.0)
eq("davon eins überwacht (P2 ist abgeschaltet)",
   M["paperless_portal_profiles_watched"][0][1], 1.0)
check("Scrape-Dauer dabei", "paperless_portal_scrape_duration_seconds" in M)
check("Update-Metrik fehlt ohne Cache", "paperless_portal_update_available" not in M)

with open(A.UPDATE_CHECK_CACHE, "w", encoding="utf-8") as fh:
    json.dump({"latest": "99.0.0", "checked_at": 1700010800, "error": None}, fh)
M2, _, _ = parse(body())
eq("Update-Metrik kommt aus dem Cache", M2["paperless_portal_update_available"][0][1], 1.0)
eq("und trägt die Version als Label",
   M2["paperless_portal_update_available"][0][0]["latest"], "99.0.0")
os.remove(A.UPDATE_CHECK_CACHE)

# ── 6. Inhalt: Checks ────────────────────────────────────────────────────────
print("Checks")


def sample(m, **want):
    for labels, val in M.get(m, []):
        if all(labels.get(k) == v for k, v in want.items()):
            return val
    return None


eq("Instanz erreichbar", sample("paperless_portal_instance_up", id=P1), 1.0)
eq("downtime ok", sample("paperless_portal_check_ok", id=P1, check="downtime"), 1.0)
eq("drift auffällig", sample("paperless_portal_check_ok", id=P1, check="drift"), 0.0)
check("'unknown' liefert KEIN Sample (Lücke statt falscher 0)",
      sample("paperless_portal_check_ok", id=P1, check="duplicate") is None)
eq("Profil-Label statt nur ID",
   [l for l, _ in M["paperless_portal_check_ok"]][0]["profile"], "TESTINStanz")
eq("Zeitpunkt des Prüflaufs",
   sample("paperless_portal_check_last_run_timestamp_seconds", id=P1), 1700010800.0)
eq("Uptime: 2 ok / 1 bad -> 0.667 ('unknown' zählt nicht mit)",
   sample("paperless_portal_check_uptime_ratio", id=P1, check="downtime"), 0.667)
eq("drift war immer ok -> 1.0",
   sample("paperless_portal_check_uptime_ratio", id=P1, check="drift"), 1.0)
eq("letzter schlechter Lauf",
   sample("paperless_portal_check_last_bad_timestamp_seconds", id=P1, check="downtime"), 1700003600.0)
check("ohne Schlecht-Lauf kein last_bad",
      sample("paperless_portal_check_last_bad_timestamp_seconds", id=P1, check="drift") is None)
eq("Zahl der Läufe", sample("paperless_portal_check_runs", id=P1), 4.0)
eq("die Zeile mit dem fiesen Profilnamen bleibt lesbar (Wert am Ende intakt)",
   sample("paperless_portal_check_uptime_ratio", id=P2, check="downtime"), 1.0)

# ── 7. Inhalt: Eskalation ────────────────────────────────────────────────────
print("Eskalation")
eq("Alarmschwelle exportiert", M["paperless_portal_alert_threshold"][0][1], 3.0)
eq("drift ist offen", sample("paperless_portal_alert_active", id=P1, check="drift"), 1.0)
eq("downtime ist nur in Beobachtung",
   sample("paperless_portal_alert_active", id=P1, check="downtime"), 0.0)
eq("Fehl-Streak des Beobachtungs-Kandidaten sichtbar",
   sample("paperless_portal_fail_streak", id=P1, check="downtime"), 1.0)
eq("Streak des offenen Alarms", sample("paperless_portal_fail_streak", id=P1, check="drift"), 3.0)
eq("Alarm offen seit", sample("paperless_portal_alert_since_timestamp_seconds", id=P1, check="drift"), 1700000000.0)
eq("zwei Erinnerungen raus", sample("paperless_portal_alert_repeats", id=P1, check="drift"), 2.0)
check("für den Nicht-Alarm kein 'since'",
      sample("paperless_portal_alert_since_timestamp_seconds", id=P1, check="downtime") is None)

# ── 8. Inhalt: Kennzahlen ────────────────────────────────────────────────────
print("Kennzahlen")
eq("Dokumente aus der JÜNGSTEN Zeile", sample("paperless_portal_documents", id=P1), 9.0)
eq("Posteingang", sample("paperless_portal_documents_inbox", id=P1), 3.0)
eq("ohne Typ", sample("paperless_portal_documents_without_type", id=P1), 1.0)
eq("ohne Korrespondent", sample("paperless_portal_documents_without_correspondent", id=P1), 4.0)
eq("Latenz in Sekunden (120 ms)", sample("paperless_portal_api_latency_seconds", id=P1), 0.12)
eq("Erhebungs-Zeitstempel mitgeliefert (Werte sind älter als der Scrape)",
   sample("paperless_portal_documents_timestamp_seconds", id=P1), 1700010800.0)
check("Profil ohne Kennzahl-Historie taucht dort nicht auf",
      sample("paperless_portal_documents", id=P2) is None)

# ── 9. Scrape-Kosten ─────────────────────────────────────────────────────────
print("Scrape-Kosten")
check("kein Netz-Aufruf je Scrape (sonst wäre die NetTrap geflogen)", True)
A._watch_stats_cache.clear()
body()
c1 = dict(A._watch_stats_cache)
reads = [0]
_orig_read = A._read_watch


def _counting_read(pid, limit=A.WATCH_MAX):
    reads[0] += 1
    return _orig_read(pid, limit)


A._read_watch = _counting_read
body()
eq("zweiter Scrape parst die Historie NICHT neu (mtime-Cache)", reads[0], 0)
check("Cache-Inhalt unverändert", dict(A._watch_stats_cache) == c1)
os.utime(A._watch_path(P1), (1700020000, 1700020000))
body()
eq("nach Änderung der Datei wird neu geparst", reads[0], 1)
A._read_watch = _orig_read
check("fehlende Historie wirft nicht",
      A._watch_stats("gibtsnicht") == {"ratio": {}, "last_bad": {}, "runs": 0})

# ── 10. Token-Hygiene ────────────────────────────────────────────────────────
print("Token-Hygiene")
eq("Umbruch fliegt raus", A._metrics_token("abc\ndef"), "abcdef")
eq("Umlaute fliegen raus (compare_digest verträgt kein Nicht-ASCII)",
   A._metrics_token("schlüssel"), "schlssel")
eq("URL-sichere Zeichen bleiben", A._metrics_token("a-b_c1"), "a-b_c1")
eq("leer bleibt leer", A._metrics_token(""), "")
eq("None -> leer", A._metrics_token(None), "")
eq("auf 80 Zeichen gedeckelt", len(A._metrics_token("x" * 200)), 80)
check("erzeugtes Token übersteht die Hygiene unverändert",
      A._metrics_token(A.secrets.token_urlsafe(32)) != "")

# ── 11. Leerer Zustand ───────────────────────────────────────────────────────
print("Leerer Zustand")
A._watcher_state.update({"results": {}, "alerts_active": set(), "alert_meta": {},
                         "last_run": None, "next_run": None, "last_heartbeat": None})
os.remove(A.PROFILES_PATH)
A._watch_stats_cache.clear()
TEXT0 = body()
M0, TYPES0, _ = parse(TEXT0)
check("frische Installation liefert trotzdem eine gültige Exposition", "paperless_portal_info" in M0)
check("ohne Läufe kein last_run-Sample",
      "paperless_portal_watcher_last_run_timestamp_seconds" not in M0)
check("und keine Instanz-Metriken", "paperless_portal_instance_up" not in M0)
for n in M0:
    check("Sample '%s' hat auch leer einen TYPE-Block" % n, n in TYPES0)

print("\n%d Prüfungen, %d Fehler" % (_count[0], len(_fails)))
for f in _fails:
    print("  - " + f)
sys.exit(1 if _fails else 0)
