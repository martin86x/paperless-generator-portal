"""
paperless-generator-portal — Flask-Wrapper um den Paperless-ngx Setup Generator.

Aufgaben:
  * Login-Gate (Session-Cookie), Default admin/admin, Passwort im Menue aenderbar
  * Einstellungsmenue: Paperless-URL + Token (serverseitig gespeichert)
  * /api/*  -> Reverse-Proxy an das konfigurierte Paperless, Token wird eingespritzt
  * /       -> liefert den UNVERAENDERTEN Generator (site/index.html) + eine injizierte
               Vorkonfig-Zeile, damit der Generator same-origin ueber /api/ arbeitet.

Der Generator selbst wird nicht veraendert: die Zeile wird nur zur Laufzeit in den
HTTP-Response eingefuegt (die Datei auf der Platte bleibt Byte-fuer-Byte identisch).
"""
import base64
import hashlib
import io
import json
import os
import secrets
import threading
import time
import zipfile
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlparse

try:
    import fcntl  # POSIX-Dateisperre fuer den Single-Worker-Waechter (fehlt unter Windows/Tests)
except ImportError:  # pragma: no cover - nur relevant im lokalen Windows-Test
    fcntl = None

import requests
from cryptography.fernet import Fernet, InvalidToken
from flask import (Flask, Response, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
PROFILES_PATH = os.path.join(CONFIG_DIR, "profiles.json")
HISTORY_DIR = os.path.join(CONFIG_DIR, "history")
HISTORY_MAX = 20
ACTIVITY_PATH = os.path.join(CONFIG_DIR, "activity.log")
UNDO_DIR = os.path.join(CONFIG_DIR, "undo")
SNAP_DIR = os.path.join(CONFIG_DIR, "instance-snapshots")
WATCHER_LOCK_PATH = os.path.join(CONFIG_DIR, "watcher.lock")
METRICS_DIR = os.path.join(CONFIG_DIR, "history-metrics")
METRICS_MAX = 2000  # gekappte Historie je Profil (JSONL-Zeilen)
WATCH_DIR = os.path.join(CONFIG_DIR, "history-watch")   # Waechter-Historie je Profil (JSONL)
WATCH_MAX = 2000
# Waechter-Laufzeitzustand auf Platte: die Schleife laeuft nur im Worker mit der Sperre,
# die UI-Anfrage landet aber irgendwo (gunicorn -w 2). Ohne diese Datei zeigt der
# Nicht-Owner-Worker gar keinen Status. Owner schreibt, alle lesen.
WATCH_STATE_PATH = os.path.join(CONFIG_DIR, "watcher-state.json")
LAST_BACKUP_PATH = os.path.join(CONFIG_DIR, "last-backup.json")  # Zeitstempel des letzten Voll-Backups
# 1-Klick-Update (P7) — SICHER ohne Docker-Socket: das Portal legt nur eine Anforderung
# im /config-Volume ab; ein Host-Helper (Cron auf dem LXC) fuehrt den Rebuild aus.
UPDATE_REQUEST = os.path.join(CONFIG_DIR, "update-request.json")
UPDATE_STATUS = os.path.join(CONFIG_DIR, "update-status.json")
UPDATE_HELPER_ALIVE = os.path.join(CONFIG_DIR, "update-helper.alive")
SITE_DIR = os.environ.get("SITE_DIR", os.path.join(os.path.dirname(__file__), "site"))

DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASS = "admin"

def _read_version():
    """Portal-Release aus app/VERSION lesen (Fallback '1.0')."""
    try:
        with open(os.path.join(os.path.dirname(__file__), "VERSION"), encoding="utf-8") as fh:
            return fh.read().strip() or "1.0"
    except OSError:
        return "1.0"


def _build_stamp():
    """Deploy-Commit (sha + Datum), beim Rebuild nach $CONFIG_DIR/build_stamp.txt geschrieben.
    Datei-Format: '<sha> <YYYY-MM-DD>'. Fehlt sie -> None. Zeigt an, welcher Commit wirklich
    laeuft (der Container hat kein git; der Rebuild-Befehl/Host-Helper legt den Stamp ab)."""
    try:
        with open(os.path.join(CONFIG_DIR, "build_stamp.txt"), encoding="utf-8") as fh:
            parts = fh.read().strip().split()
        if parts:
            return {"sha": parts[0], "date": parts[1] if len(parts) > 1 else ""}
    except OSError:
        pass
    return None


PORTAL_VERSION = _read_version()             # Portal-Release (app/VERSION)
GITHUB_REPO = "martin86x/paperless-generator-portal"

# Automatische Update-Pruefung: die installierte PORTAL_VERSION gegen app/VERSION auf main
# vergleichen. Ergebnis wird im /config-Volume gecacht (TTL), damit GitHub bei jedem Seiten-
# aufruf NICHT neu gefragt wird (unauth. Rate-Limit = 60/h) und der Check restart-fest ist.
UPDATE_CHECK_CACHE = os.path.join(CONFIG_DIR, "update-check.json")
UPDATE_CHECK_TTL = 6 * 3600                  # hoechstens alle 6 h wirklich bei GitHub nachsehen


def _ver_tuple(s):
    """'1.2.0' -> (1, 2, 0); nicht-numerische Teile -> 0. Fuer den Versionsvergleich."""
    out = []
    for part in str(s or "").strip().split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out) or (0,)


def _fetch_latest_version():
    """app/VERSION vom main-Branch holen (roh ueber raw.githubusercontent -> kein API-Rate-Limit).
    Rueckgabe: Versions-String oder None bei Fehler/unplausibler Antwort."""
    try:
        r = requests.get(
            "https://raw.githubusercontent.com/%s/main/app/VERSION" % GITHUB_REPO,
            timeout=8)
        if r.status_code == 200:
            v = (r.text or "").strip()
            # Plausibilitaet: nur Ziffern/Punkte, sonst ist es kein VERSION-Inhalt (z. B. 404-Seite)
            if v and len(v) <= 20 and all(c.isdigit() or c == "." for c in v):
                return v
    except requests.RequestException:
        pass
    return None


def _update_result(latest, checked_at, error, stale):
    installed = PORTAL_VERSION
    avail = bool(latest) and _ver_tuple(latest) > _ver_tuple(installed)
    return {"installed": installed, "latest": latest, "update_available": avail,
            "checked_at": int(checked_at or 0), "error": error, "stale": bool(stale),
            "repo": GITHUB_REPO}


def check_for_update(force=False):
    """Versions-Check gegen GitHub mit Datei-Cache (TTL). Gibt IMMER ein Dict zurueck
    (siehe _update_result). force=True umgeht den Cache. Bei GitHub-Fehler wird ein
    vorhandener alter Cache als 'stale' zurueckgegeben, statt hart zu scheitern."""
    now = int(time.time())
    try:
        with open(UPDATE_CHECK_CACHE, encoding="utf-8") as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        cached = None
    if cached and not force and (now - int(cached.get("checked_at", 0))) < UPDATE_CHECK_TTL:
        return _update_result(cached.get("latest"), cached.get("checked_at"),
                              cached.get("error"), stale=False)
    latest = _fetch_latest_version()
    if latest is None:
        if cached and cached.get("latest"):
            return _update_result(cached.get("latest"), cached.get("checked_at"),
                                  "GitHub nicht erreichbar", stale=True)
        return _update_result(None, now, "GitHub nicht erreichbar", stale=False)
    try:
        with open(UPDATE_CHECK_CACHE, "w", encoding="utf-8") as fh:
            json.dump({"latest": latest, "checked_at": now, "error": None}, fh)
    except OSError:
        pass
    return _update_result(latest, now, None, stale=False)

# Hop-by-hop-Header + solche, die requests bereits aufloest (Content-Encoding/-Length),
# duerfen nicht 1:1 an den Browser durchgereicht werden.
EXCLUDED_RESP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "content-encoding", "content-length",
}
PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")

# Vorkonfig-Injektion (vor </head>):
#   1. Synchron im <head>: beide localStorage-Keys auf same-origin patchen, BEVOR die
#      Generator-Skripte sie lesen (sonst schreibt die letzte Sitzung eine direkte URL zurueck
#      -> CORS). 2. Externe /portal/inject.js laedt die restliche Logik (Nav/Profil-Dropdown/
#      Speichern/Dirty/Sektion-01/E-Mail-Pflichtfeld/Profil-Config laden) — als echte JS-Datei
#      wartbar und einzeln testbar (app/inject.js).
INJECT = (
    "<script>(function(){var o=location.origin;"
    "try{localStorage.setItem('plx_conn_preset',JSON.stringify({url:o,token:''}));}catch(e){}"
    "try{var K='paperless_gen_cfg_v2',r=localStorage.getItem(K);"
    "if(r){var c=JSON.parse(r);c.url=o;c.token='';localStorage.setItem(K,JSON.stringify(c));}}catch(e){}"
    "})();</script>"
    "<script src='/portal/inject.js'></script>"
)


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, CONFIG_PATH)


def _log_activity(kind, message, level="info", detail=None):
    """Append-only Aktivitaetsprotokoll (wann/was/Ergebnis) in /config/activity.log.

    level: info|ok|warn|err (fuer Einfaerbung im Protokoll). detail: optionaler
    Langtext (aus-/einklappbar). Rueckwaertskompatibel — Altzeilen ohne level/detail
    werden als 'info' ohne Detail angezeigt."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        rec = {"ts": datetime.now().isoformat(timespec="seconds"),
               "kind": kind, "msg": message, "level": level}
        if detail:
            rec["detail"] = detail
        with open(ACTIVITY_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _read_activity(limit=150):
    try:
        with open(ACTIVITY_PATH, encoding="utf-8") as fh:
            lines = fh.readlines()[-limit:]
    except FileNotFoundError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def init_config():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        save_config({
            "admin_user": DEFAULT_ADMIN_USER,
            "admin_pw_hash": generate_password_hash(DEFAULT_ADMIN_PASS),
            "paperless_url": "",
            "paperless_token": "",
            "secret": secrets.token_hex(32),
            "is_default_pw": True,
        })
    return load_config()


# ── Token-Verschluesselung at-rest ───────────────────────────────────────────
# Tokens liegen in profiles.json (+ Backups) nicht mehr im Klartext. Schluessel wird
# aus 'secret' abgeleitet. HINWEIS: aendert sich 'secret', sind gespeicherte Tokens nicht
# mehr entschluesselbar. Der Export (E1) entschluesselt bewusst -> Backup bleibt auf einer
# anderen Instanz (anderer secret) einspielbar.
_ENC_PREFIX = "enc:"


def _fernet():
    key = base64.urlsafe_b64encode(hashlib.sha256(_cfg0["secret"].encode()).digest())
    return Fernet(key)


def _enc(tok):
    if not tok or tok.startswith(_ENC_PREFIX):
        return tok
    return _ENC_PREFIX + _fernet().encrypt(tok.encode()).decode()


def _dec(val):
    if not val or not val.startswith(_ENC_PREFIX):
        return val or ""
    try:
        return _fernet().decrypt(val[len(_ENC_PREFIX):].encode()).decode()
    except InvalidToken:
        return ""


# Geheime Felder eines Profils (werden verschluesselt abgelegt): Paperless-Token +
# Benachrichtigungs-Zugangsdaten (Pushover-Token/User-Key, SMTP-Passwort).
_NOTIFY_SECRETS = [("pushover", "token"), ("pushover", "user"), ("email", "password")]


def _enc_profile_secrets(p):
    """Alle Secrets eines Profils idempotent verschluesseln (bereits verschluesselte bleiben)."""
    if not isinstance(p, dict):
        return
    if p.get("paperless_token"):
        p["paperless_token"] = _enc(p["paperless_token"])
    n = p.get("notifications")
    if isinstance(n, dict):
        for ch, field in _NOTIFY_SECRETS:
            c = n.get(ch)
            if isinstance(c, dict) and c.get(field):
                c[field] = _enc(c[field])


# ─────────────────────────────────────────────────────────────────────────────
# PROFILE — mehrere Paperless-Instanzen, je eigene Verbindung + Generator-Config
# profiles.json: { "<id>": {name, paperless_url, paperless_token, generator_config} }
# Das AKTIVE Profil steht in der Server-Session (kein Tab-uebergreifender Konflikt);
# config.json haelt zusaetzlich das zuletzt genutzte als persistenten Default.
# ─────────────────────────────────────────────────────────────────────────────
def load_profiles():
    try:
        with open(PROFILES_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}


def save_profiles(profs):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    # Secrets verschluesselt ablegen (idempotent: bereits verschluesselte bleiben).
    for p in profs.values():
        _enc_profile_secrets(p)
    # Auto-Backup: vorige Version rotierend sichern (letzte 5), damit nie Profile verloren gehen.
    if os.path.exists(PROFILES_PATH):
        try:
            for i in range(4, 0, -1):
                older = "%s.bak.%d" % (PROFILES_PATH, i)
                newer = "%s.bak.%d" % (PROFILES_PATH, i + 1)
                if os.path.exists(older):
                    os.replace(older, newer)
            import shutil
            shutil.copy2(PROFILES_PATH, "%s.bak.1" % PROFILES_PATH)
        except OSError:
            pass
    tmp = PROFILES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(profs, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, PROFILES_PATH)


def _new_profile_id():
    return secrets.token_hex(8)


def _strip_conn(gen_cfg):
    """url/token aus dem Generator-Snapshot entfernen — die echte Verbindung liegt
    getrennt im Profil (im Portal ist url=origin, token=leer)."""
    if isinstance(gen_cfg, dict):
        gen_cfg = dict(gen_cfg)
        gen_cfg.pop("url", None)
        gen_cfg.pop("token", None)
    return gen_cfg


def init_profiles():
    """Nicht-destruktive Migration: gibt es noch keine profiles.json, wird die bestehende
    Einzel-Verbindung aus config.json in ein Default-Profil 'Standard' gewandelt.
    config.json behaelt paperless_url/token als Rueckweg."""
    profs = load_profiles()
    if not profs:
        cfg = load_config()
        pid = _new_profile_id()
        profs = {pid: {
            "name": "Standard",
            "paperless_url": cfg.get("paperless_url", ""),
            "paperless_token": cfg.get("paperless_token", ""),
            "generator_config": None,
            "productive": False,
            "readonly": False,
            "color": "",
        }}
        save_profiles(profs)
        if not cfg.get("active_profile"):
            cfg["active_profile"] = pid
            save_config(cfg)
    return profs


def _active_id():
    """Aktives Profil: Session bevorzugt, sonst persistenter Default, sonst erstes Profil."""
    profs = load_profiles()
    aid = session.get("active_profile") or load_config().get("active_profile")
    if aid not in profs:
        aid = next(iter(profs), None)
    return aid


def active_profile():
    profs = load_profiles()
    aid = _active_id()
    return profs.get(aid, {}) if aid else {}


def set_active_profile(pid):
    profs = load_profiles()
    if pid in profs:
        session["active_profile"] = pid
        cfg = load_config()
        cfg["active_profile"] = pid
        save_config(cfg)
        return True
    return False


# ─── Config-Historie pro Profil (bei jedem Speichern wird der vorige Stand gesichert) ──
def _history_dir(pid):
    return os.path.join(HISTORY_DIR, pid)


def _snapshot_history(pid, gen_cfg):
    """Vorige generator_config als Zeitstempel-Snapshot ablegen (gekappt auf HISTORY_MAX)."""
    if not gen_cfg:
        return
    d = _history_dir(pid)
    os.makedirs(d, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    path = os.path.join(d, ts + ".json")
    i = 1
    while os.path.exists(path):
        path = os.path.join(d, "%s-%d.json" % (ts, i))
        i += 1
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(gen_cfg, fh, ensure_ascii=False)
    files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    while len(files) > HISTORY_MAX:
        try:
            os.remove(os.path.join(d, files.pop(0)))
        except OSError:
            break


def _list_history(pid):
    try:
        return sorted((f[:-5] for f in os.listdir(_history_dir(pid)) if f.endswith(".json")),
                      reverse=True)
    except FileNotFoundError:
        return []


def _fmt_ts(ts):
    """20260711T093015 -> 11.07.2026 09:30:15 (Anzeige)."""
    base = ts.split("-")[0]
    try:
        d = datetime.strptime(base, "%Y%m%dT%H%M%S")
        return d.strftime("%d.%m.%Y %H:%M:%S")
    except ValueError:
        return ts


def build_index_html():
    """Generator-HTML einlesen und die Vorkonfig-Zeile vor </head> einfuegen."""
    with open(os.path.join(SITE_DIR, "index.html"), encoding="utf-8") as fh:
        html = fh.read()
    idx = html.lower().find("</head>")
    if idx != -1:
        return html[:idx] + INJECT + html[idx:]
    return INJECT + html


_cfg0 = init_config()
init_profiles()
INDEX_HTML = build_index_html()
with open(os.path.join(os.path.dirname(__file__), "inject.js"), encoding="utf-8") as _fh:
    INJECT_JS = _fh.read()

app = Flask(__name__)
app.secret_key = _cfg0["secret"]
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",          # bremst Cross-Site-Requests -> CSRF-Grundschutz
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    SESSION_REFRESH_EACH_REQUEST=True,       # gleitend: aktive Nutzung haelt die Session am Leben
)

# ── Betrieb hinter einem Reverse-Proxy (opt-in) ──────────────────────────────
# Ohne das ist request.remote_addr hinter einem Proxy dessen IP — ALLE Clients landen im
# selben Rate-Limit-Topf, und fuenf Fehlversuche eines Fremden sperren den rechtmaessigen
# Nutzer mit aus. Mit TRUST_PROXY=1 zaehlt stattdessen X-Forwarded-For.
# BEWUSST opt-in und standardmaessig AUS: steht kein Proxy davor, darf X-Forwarded-For
# NICHT geglaubt werden — ein Angreifer erfindet sonst je Versuch eine neue IP und das
# Rate-Limit ist wirkungslos. Nur einschalten, wenn wirklich ein Proxy davorsteht, der
# den Header selbst setzt (und einen mitgeschickten ueberschreibt).
if os.environ.get("TRUST_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# metrics_endpoint gehoert hierher, weil Prometheus keine Session hat: require_login wuerde
# den Scrape auf /login umleiten. Der Endpunkt schuetzt sich selbst per Bearer-Token.
PUBLIC_ENDPOINTS = {"login", "login_recovery", "healthz", "static", "metrics_endpoint"}

# ── Login-Rate-Limit (in-memory, pro IP) ──────────────────────────────────────
_login_fails = {}
LOGIN_MAX = 5
LOGIN_WINDOW = 300  # Sekunden


# ── Recovery-Codes (Passwort-Rückweg ohne E-Mail) ────────────────────────────
# 10 Einmal-Codes; nur ihre Hashes liegen in config.json. Login per Code verbraucht
# den getroffenen Code. Neu erzeugen (eingeloggt) macht alle alten ungültig.
RECOVERY_CODE_COUNT = 10


def _norm_recovery(code):
    return (code or "").replace("-", "").replace(" ", "").strip().lower()


def _gen_recovery_codes():
    """Lesbare Einmal-Codes im Format xxxx-xxxx-xxxx (Hex). Rueckgabe: Klartext-Liste."""
    out = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = secrets.token_hex(6)  # 12 Hex-Zeichen
        out.append("%s-%s-%s" % (raw[0:4], raw[4:8], raw[8:12]))
    return out


def _set_recovery_codes(cfg, codes):
    cfg["recovery_codes"] = [generate_password_hash(_norm_recovery(c)) for c in codes]
    cfg["recovery_generated_at"] = datetime.now().isoformat(timespec="seconds")


def _recovery_remaining(cfg=None):
    return len((cfg or load_config()).get("recovery_codes") or [])


def _consume_recovery_code(cfg, code):
    """Code gegen gespeicherte Hashes pruefen; bei Treffer Hash entfernen -> True."""
    norm = _norm_recovery(code)
    if not norm:
        return False
    hashes = cfg.get("recovery_codes") or []
    for i, h in enumerate(hashes):
        if check_password_hash(h, norm):
            hashes.pop(i)
            cfg["recovery_codes"] = hashes
            return True
    return False


def _login_blocked(ip):
    now = time.time()
    fails = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_WINDOW]
    _login_fails[ip] = fails
    return len(fails) >= LOGIN_MAX


def _login_note_fail(ip):
    _login_fails.setdefault(ip, []).append(time.time())


def _same_host(url_value):
    try:
        return urlparse(url_value).netloc == request.host
    except ValueError:
        return False


@app.before_request
def csrf_origin_check():
    """CSRF-Grundschutz per Origin/Referer-Abgleich fuer zustandsaendernde Requests.
    Der /api/-Proxy ist ausgenommen: der Generator feuert dort legitim viele POSTs/Bursts
    (Direkt-Lauf) — die duerfen NICHT geblockt werden."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and not request.path.startswith("/api"):
        origin = request.headers.get("Origin")
        referer = request.headers.get("Referer")
        if origin is not None:
            if not _same_host(origin):
                return Response("CSRF: Origin stimmt nicht", status=403)
        elif referer is not None:
            if not _same_host(referer):
                return Response("CSRF: Referer stimmt nicht", status=403)
        # Fehlen beide Header -> durchlassen (der Angriffs-Vektor sendet einen fremden Origin).
    return None


@app.context_processor
def _inject_active_profile():
    """Aktives Profil (Name/Produktiv/Farbe) allen Templates bereitstellen — fuer den Warnbalken."""
    try:
        if not session.get("logged_in"):
            return {"active_prof": None}
        p = active_profile()
        return {"active_prof": {
            "name": p.get("name") or "",
            "productive": bool(p.get("productive")),
            "readonly": bool(p.get("readonly")),
            "color": p.get("color") or "",
        }}
    except Exception:
        return {"active_prof": None}


@app.context_processor
def _inject_layout():
    """`?embed=1` -> Seite ohne Kopf/Subnav rendern (Fragment fuer die Verwaltungs-Shell)."""
    embed = bool(request.args.get("embed"))
    return {"embed": embed, "layout": "_embed.html" if embed else "base.html"}


@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if not session.get("logged_in"):
        # Fetch-Endpunkte (Proxy + portal-interne API) -> 401 statt Redirect,
        # damit das injizierte JS im Generator es sauber behandeln kann.
        if request.path.startswith("/api") or request.path.startswith("/portal"):
            return Response("Unauthorized", status=401)
        return redirect(url_for("login"))
    return None


def _setup_complete():
    """Setup gilt als abgeschlossen, wenn das Default-Passwort ersetzt wurde UND das aktive
    Profil eine Verbindung (URL+Token) hat. Sonst -> gefuehrtes Onboarding erzwingen."""
    try:
        if load_config().get("is_default_pw"):
            return False
        p = active_profile()
        return bool(p.get("paperless_url") and p.get("paperless_token"))
    except (OSError, ValueError):
        return False


@app.before_request
def require_setup():
    """Solange das Onboarding nicht abgeschlossen ist, jeden Seiten-GET auf /wizard leiten.
    Ausgenommen: Wizard selbst, Login/Logout, static, healthz sowie /api + /portal (eigene
    401-Behandlung / vom injizierten JS genutzt)."""
    if request.endpoint in ("wizard", "login", "logout", "healthz", "static", "metrics_endpoint"):
        return None
    if request.path.startswith("/api") or request.path.startswith("/portal"):
        return None
    if not session.get("logged_in"):
        return None  # require_login kuemmert sich
    if not _setup_complete():
        return redirect(url_for("wizard"))
    return None


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if _login_blocked(ip):
            error = "Zu viele Fehlversuche. Bitte einige Minuten warten."
            return render_template("login.html", error=error), 429
        cfg = load_config()
        user = request.form.get("username", "")
        pw = request.form.get("password", "")
        if user == cfg.get("admin_user") and check_password_hash(cfg["admin_pw_hash"], pw):
            _login_fails.pop(ip, None)
            session.permanent = True
            session["logged_in"] = True
            _log_activity("login", "Anmeldung erfolgreich", level="ok", detail="IP %s" % ip)
            # Erst-Einrichtung (Default-Passwort aktiv) -> gefuehrter Wizard.
            if cfg.get("is_default_pw"):
                return redirect(url_for("wizard"))
            return redirect(url_for("index"))
        _login_note_fail(ip)
        _log_activity("login", "Fehlgeschlagene Anmeldung", level="warn",
                      detail="Benutzer '%s', IP %s" % (user, ip))
        error = "Falscher Benutzername oder falsches Passwort."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/login/recovery", methods=["GET", "POST"])
def login_recovery():
    """Anmeldung mit einem Recovery-Code, wenn das Passwort vergessen wurde. Der getroffene
    Code wird verbraucht; danach soll der Nutzer im Konto ein neues Passwort setzen."""
    error = None
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if _login_blocked(ip):
            return render_template("login_recovery.html",
                                   error="Zu viele Fehlversuche. Bitte einige Minuten warten."), 429
        cfg = load_config()
        user = request.form.get("username", "")
        code = request.form.get("code", "")
        if user == cfg.get("admin_user") and _consume_recovery_code(cfg, code):
            save_config(cfg)
            _login_fails.pop(ip, None)
            session.permanent = True
            session["logged_in"] = True
            remaining = _recovery_remaining(cfg)
            _log_activity("recovery", "Anmeldung per Recovery-Code", level="warn",
                          detail="IP %s · verbleibende Codes: %d" % (ip, remaining))
            return redirect(url_for("verwaltung", tab="konto",
                                    msg="Mit Recovery-Code angemeldet — bitte jetzt ein neues "
                                        "Passwort setzen. (%d Codes übrig)" % remaining))
        _login_note_fail(ip)
        _log_activity("recovery", "Recovery-Code abgelehnt", level="warn",
                      detail="Benutzer '%s', IP %s" % (user, ip))
        error = "Benutzername oder Code ist falsch."
    return render_template("login_recovery.html", error=error)


def _test_paperless(url, token):
    """Token direkt gegen Paperless testen. Rueckgabe: HTTP-Status (int) oder None (nicht erreichbar).

    Nutzt denselben Endpunkt wie der Generator (/api/documents/) — /api/ (root) liefert bei
    Paperless ein 302 und wuerde einen gueltigen Token faelschlich abweisen.
    """
    try:
        r = requests.get(
            url.rstrip("/") + "/api/documents/?page_size=1",
            headers={"Authorization": "Token " + token} if token else {},
            timeout=8,
            allow_redirects=False,
        )
        return r.status_code
    except requests.RequestException:
        return None


def _api_count(url, token, query, timeout=5):
    """count-Feld eines Paperless-Listen-Endpunkts holen (oder None wenn nicht erreichbar)."""
    try:
        r = requests.get(
            url.rstrip("/") + "/api/" + query,
            headers={"Authorization": "Token " + token} if token else {},
            timeout=timeout, allow_redirects=False,
        )
        if r.status_code == 200:
            return r.json().get("count")
    except (requests.RequestException, ValueError):
        pass
    return None


def _instance_info(url, token):
    """Paperless-Version + Anzahl offener Verarbeitungs-Tasks (best-effort, read-only)."""
    hdr = {"Authorization": "Token " + token} if token else {}
    ver = None
    try:
        r = requests.get(url.rstrip("/") + "/api/status/", headers=hdr, timeout=5, allow_redirects=False)
        if r.status_code == 200:
            ver = (r.json() or {}).get("pngx_version")
        if ver is None:
            ver = r.headers.get("X-Version")
    except (requests.RequestException, ValueError):
        pass
    open_tasks = None
    try:
        r = requests.get(url.rstrip("/") + "/api/tasks/", headers=hdr, timeout=5, allow_redirects=False)
        if r.status_code == 200:
            data = r.json()
            rows = data.get("results", data) if isinstance(data, dict) else data
            if isinstance(rows, list):
                open_tasks = sum(1 for t in rows if str(t.get("status", "")).upper() in ("PENDING", "STARTED", "RETRY"))
    except (requests.RequestException, ValueError):
        pass
    return {"version": ver, "open_tasks": open_tasks}


# Kennzahlen + Drift-Kategorien fuer das Dashboard (E3)
_DRIFT_CATS = [
    ("Tags", "tags", "tags/"),
    ("Typen", "types", "document_types/"),
    ("Felder", "fields", "custom_fields/"),
    ("Korrespondenten", "correspondents", "correspondents/"),
    ("Speicherpfade", "storagePaths", "storage_paths/"),
]


def _entry_on(e):
    """Editor-Eintrag aktiv? (Generator setzt enabled:false auf deaktivierten Einträgen.)
    Fehlendes Feld = aktiv (rückwärtskompatibel zu Configs ohne das Flag)."""
    return not (isinstance(e, dict) and e.get("enabled") is False)


def _count_active(gc, key):
    """Anzahl aktiver Top-Level-Einträge einer Kategorie (deaktivierte zählen nicht mit —
    sie werden auch nicht angelegt, sonst würde Drift fälschlich 'fehlt' melden)."""
    return sum(1 for e in (gc.get(key) or []) if _entry_on(e))


def _connection_status(cfg):
    """Live-Status der gespeicherten Paperless-Verbindung -> (kind, text) fuer die UI.

    kind: 'ok' (gruen) | 'err' (rot) | 'warn' (rot, noch nichts konfiguriert).
    """
    url = cfg.get("paperless_url")
    token = cfg.get("paperless_token") or ""
    if not url:
        return ("warn", "Noch keine Paperless-URL gesetzt.")
    if "://" in token:
        return ("err", "Im Token-Feld steht eine URL statt eines Tokens. Bitte den API-Token "
                       "aus Paperless (Mein Profil → API-Token) eintragen.")
    code = _test_paperless(url, token)
    if code == 200:
        return ("ok", "Verbindung aktiv – Paperless antwortet und der Token ist gültig.")
    if code in (401, 403):
        return ("err", "Token ungültig (HTTP %d). In Paperless unter Mein Profil → API-Token "
                       "neu erzeugen und hier eintragen." % code)
    if code is None:
        return ("err", "Paperless nicht erreichbar unter %s – URL/Netzwerk prüfen." % url)
    return ("err", "Unerwartete Antwort von Paperless: HTTP %d." % code)


@app.route("/wizard", methods=["GET", "POST"])
def wizard():
    """Gefuehrte Erst-Einrichtung: Passwort setzen + erste Instanz (mit Live-Token-Test)."""
    cfg = load_config()
    profs = load_profiles()
    aid = _active_id()
    prof = profs.get(aid, {})
    err = None
    if request.method == "POST":
        new = request.form.get("new", "")
        rep = request.form.get("repeat", "")
        name = request.form.get("name", "").strip() or (prof.get("name") or "Standard")
        url = request.form.get("paperless_url", "").strip().rstrip("/")
        tok = request.form.get("paperless_token", "").strip()
        lxc = request.form.get("lxc_id", "").strip()
        notify_email = request.form.get("notify_email", "").strip()
        if len(new) < 4:
            err = "Neues Passwort muss mindestens 4 Zeichen haben."
        elif new != rep:
            err = "Die Passwörter stimmen nicht überein."
        elif not url:
            err = "Bitte die Paperless-URL angeben."
        elif not tok:
            err = "Bitte den API-Token angeben."
        else:
            code = _test_paperless(url, tok)
            if code == 200:
                cfg["admin_pw_hash"] = generate_password_hash(new)
                cfg["is_default_pw"] = False
                if lxc.isdigit():
                    cfg["lxc_id"] = lxc          # Portal-Container fuer 1-Klick-Update (global)
                save_config(cfg)
                if aid in profs:
                    profs[aid]["name"] = name
                    profs[aid]["paperless_url"] = url
                    profs[aid]["paperless_token"] = tok
                    if notify_email:
                        gc = profs[aid].get("generator_config") or {}
                        gc["notifyEmail"] = notify_email   # Frist-Workflows/Erinnerungen
                        profs[aid]["generator_config"] = gc
                    save_profiles(profs)
                return redirect(url_for("index"))
            elif code in (401, 403):
                err = ("Token ungültig (HTTP %d) – in Paperless unter Mein Profil → "
                       "API-Token neu erzeugen." % code)
            elif code is None:
                err = "Paperless nicht erreichbar unter %s – URL prüfen." % url
            else:
                err = "Unerwartete Antwort von Paperless: HTTP %d." % code
    return render_template("wizard.html", name=(prof.get("name") or "Standard"),
                           url=prof.get("paperless_url", ""), err=err,
                           lxc_id=str(cfg.get("lxc_id") or ""),
                           notify_email=(prof.get("generator_config") or {}).get("notifyEmail", ""))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    cfg = load_config()
    if request.method == "POST":
        # Nur noch Passwort — Paperless-Verbindungen laufen ueber Profile (/profiles).
        def _back(**kw):
            return redirect(url_for("verwaltung", tab="konto", **kw))
        new = request.form.get("new", "")
        cur = request.form.get("current", "")
        rep = request.form.get("repeat", "")
        if not new:
            return _back(err="Bitte ein neues Passwort eingeben.")
        if not check_password_hash(cfg["admin_pw_hash"], cur):
            return _back(err="Aktuelles Passwort ist falsch.")
        if len(new) < 4:
            return _back(err="Neues Passwort muss mindestens 4 Zeichen haben.")
        if new != rep:
            return _back(err="Die neuen Passwörter stimmen nicht überein.")
        cfg["admin_pw_hash"] = generate_password_hash(new)
        cfg["is_default_pw"] = False
        save_config(cfg)
        return _back(msg="Passwort geändert.")
    return render_template("settings.html", is_default_pw=cfg.get("is_default_pw", False),
                           recovery_remaining=_recovery_remaining(cfg),
                           recovery_at=cfg.get("recovery_generated_at"),
                           msg=request.args.get("msg"), err=request.args.get("err"))


@app.route("/verwaltung/recovery/generate", methods=["POST"])
def recovery_generate():
    """10 neue Recovery-Codes erzeugen (Re-Auth mit aktuellem Passwort). Klartext wird EINMALIG
    angezeigt (danach nur noch Hashes gespeichert); alle vorher erzeugten Codes werden ungültig."""
    cfg = load_config()
    if not check_password_hash(cfg["admin_pw_hash"], request.form.get("current", "")):
        return redirect(url_for("verwaltung", tab="konto",
                                err="Aktuelles Passwort ist falsch — keine Codes erzeugt."))
    codes = _gen_recovery_codes()
    _set_recovery_codes(cfg, codes)
    save_config(cfg)
    _log_activity("recovery", "Recovery-Codes neu erzeugt (%d)" % len(codes), level="warn",
                  detail="Alle vorher erzeugten Codes sind jetzt ungültig.")
    # Klartext nur dieses eine Mal — Seite direkt rendern (kein Redirect, sonst weg).
    return render_template("settings.html", is_default_pw=cfg.get("is_default_pw", False),
                           recovery_remaining=len(codes),
                           recovery_at=cfg.get("recovery_generated_at"),
                           new_codes=codes,
                           msg="10 Recovery-Codes erzeugt — jetzt sichern! Sie werden nur "
                               "dieses eine Mal angezeigt.", err=None)


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


# ─────────────────────────────────────────────────────────────────────────────
# PROFIL-VERWALTUNG
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/profiles")
def profiles():
    profs = load_profiles()
    aid = _active_id()
    gwc = _watcher_cfg()
    items = []
    for pid, p in profs.items():
        kind, text = _connection_status({
            "paperless_url": p.get("paperless_url"),
            "paperless_token": _dec(p.get("paperless_token")),
        })
        n = _notif_of(p)
        notif_bits = []
        if n["pushover"]["enabled"]:
            notif_bits.append("Pushover")
        if n["ntfy"]["enabled"]:
            notif_bits.append("ntfy" + (" (%s)" % n["ntfy"]["topic"] if n["ntfy"]["topic"] else ""))
        if n["email"]["enabled"]:
            notif_bits.append("E-Mail" + (" → %s" % n["email"]["to"] if n["email"].get("to") else ""))
        pw = _profile_watch(p, gwc)
        items.append({
            "id": pid,
            "name": p.get("name") or "(ohne Name)",
            "url": p.get("paperless_url") or "",
            "has_token": bool(p.get("paperless_token")),
            "has_config": p.get("generator_config") is not None,
            "notify_email": (p.get("generator_config") or {}).get("notifyEmail", ""),
            "active": pid == aid,
            "conn_kind": kind, "conn_text": text,
            "productive": bool(p.get("productive")),
            "readonly": bool(p.get("readonly")),
            "color": p.get("color") or "",
            "notif": ", ".join(notif_bits),
            "watch": pw,
            "history": [{"ts": ts, "label": _fmt_ts(ts)} for ts in _list_history(pid)],
        })
    items.sort(key=lambda x: x["name"].lower())
    return render_template("profiles.html", profiles=items,
                           msg=request.args.get("msg"), err=request.args.get("err"))


@app.route("/update", methods=["GET", "POST"])
def update_page():
    """Versions-Anzeige + Update-Pruefung gegen GitHub + fertiger Rebuild-Befehl.

    Die Container-ID (LXC/CTID) wird gespeichert (config.json['lxc_id']) und in den
    kompletten `pct exec …`-Befehl eingesetzt, sodass er ohne Nacharbeit kopiert werden
    kann. Ein echter 1-Klick-Self-Rebuild ist bewusst NICHT umgesetzt: der Container
    (python:slim) hat kein git/docker und das Repo liegt auf dem LXC-Host — ein
    Self-Rebuild braeuchte den Docker-Socket (root-aequivalent). (Self-Rebuild -> Roadmap.)
    """
    cfg = load_config()
    if request.method == "POST":
        lxc = request.form.get("lxc_id", "").strip()
        cfg["lxc_id"] = lxc if lxc.isdigit() else ""   # nur eine Ziffern-CTID zulassen
        save_config(cfg)
        if lxc and not lxc.isdigit():
            return redirect(url_for("verwaltung", tab="version", err="Container-ID muss eine Zahl sein (z. B. 230)."))
        return redirect(url_for("verwaltung", tab="version", msg="Container-ID gespeichert." if lxc else "Container-ID entfernt."))

    lxc_id = str(cfg.get("lxc_id") or "").strip()
    latest = None
    gh_err = None
    try:
        r = requests.get("https://api.github.com/repos/%s/commits/main" % GITHUB_REPO,
                         headers={"Accept": "application/vnd.github+json"}, timeout=8)
        if r.status_code == 200:
            j = r.json()
            latest = {
                "sha": j["sha"][:7],
                "date": (j["commit"]["committer"]["date"] or "")[:10],
                "message": (j["commit"]["message"] or "").splitlines()[0],
                "url": j.get("html_url", ""),
            }
        else:
            gh_err = "GitHub antwortete mit HTTP %d." % r.status_code
    except (requests.RequestException, ValueError, KeyError):
        gh_err = "GitHub nicht erreichbar."
    # Der „innere" Befehl laeuft direkt auf der LXC-Shell; der volle wickelt ihn in
    # `pct exec <CTID> -- bash -c '…'` fuer die Proxmox-Host-Shell.
    # Nach dem Reset (vor dem Build) den laufenden Commit als Stamp ins config-Volume
    # schreiben — so zeigt der Version-Reiter, WELCHER Commit wirklich deployt ist.
    # WICHTIG: printf mit DOPPELTEN Quotes ("%s %s"), NICHT einfachen. full_cmd wickelt
    # inner_cmd in `bash -c '…'` (einfache Quotes) — einfache Quotes hier drin wuerden das
    # aeussere Quoting vorzeitig beenden und der kopierte Befehl liefe nicht durch.
    stamp_cmd = ("printf \"%s %s\" \"$(git rev-parse --short HEAD)\" "
                 "\"$(git show -s --format=%cd --date=short)\" > config/build_stamp.txt")
    inner_cmd = ("cd /opt/paperless-generator-portal && git fetch origin main && "
                 "git reset --hard origin/main && " + stamp_cmd + " && "
                 "docker compose up -d --build")
    full_cmd = "pct exec %s -- bash -c '%s'" % (lxc_id or "<CTID>", inner_cmd)
    upd_status = None
    try:
        with open(UPDATE_STATUS, encoding="utf-8") as fh:
            upd_status = json.load(fh)
    except (OSError, ValueError):
        pass
    oneclick = {
        "helper": os.path.exists(UPDATE_HELPER_ALIVE),
        "pending": os.path.exists(UPDATE_REQUEST),
        "status": upd_status,
    }
    # Version-Seite: FRISCH pruefen (force), sonst widerspricht der gecachte Banner dem
    # live abgerufenen GitHub-Commit-Block darunter. Nutzt raw.githubusercontent -> kein
    # API-Rate-Limit. Der leichte Kopfzeilen-Poller (inject.js) bleibt weiterhin gecacht.
    upd = check_for_update(force=True)
    return render_template("update.html", version=PORTAL_VERSION, latest=latest,
                           gh_err=gh_err, inner_cmd=inner_cmd, full_cmd=full_cmd,
                           lxc_id=lxc_id, repo=GITHUB_REPO, oneclick=oneclick,
                           build_stamp=_build_stamp(), upd=upd,
                           msg=request.args.get("msg"), err=request.args.get("err"))


@app.route("/portal/update-check.json")
def portal_update_check():
    """Leichter Endpoint fuer das injizierte JS (inject.js): liefert den gecachten
    Versions-Abgleich, damit die Kopfzeile proaktiv 'Update verfuegbar' anzeigen kann.
    ?force=1 erzwingt eine frische Abfrage (Cache umgehen)."""
    return jsonify(check_for_update(force=request.args.get("force") == "1"))


@app.route("/verwaltung/update/trigger", methods=["POST"])
def update_trigger():
    """1-Klick-Update/Rollback anfordern (SICHER): nur eine Anforderungsdatei ins
    /config-Volume schreiben. Der Host-Helper (Cron auf dem LXC) fuehrt sie aus."""
    action = "rollback" if request.form.get("action") == "rollback" else "update"
    try:
        with open(UPDATE_REQUEST, "w", encoding="utf-8") as fh:
            json.dump({"action": action, "ts": datetime.now().isoformat(timespec="seconds")}, fh)
    except OSError as exc:
        return redirect(url_for("verwaltung", tab="version",
                                err="Konnte Anforderung nicht schreiben: %s" % exc))
    _log_activity("update", "1-Klick-%s angefordert" % ("Rollback" if action == "rollback" else "Update"))
    return redirect(url_for("verwaltung", tab="version",
                            msg="%s angefordert — der Host-Helper führt es in Kürze aus."
                                % ("Rollback" if action == "rollback" else "Update")))


_BACKUP_SKIP = {"watcher.lock"}  # transiente Dateien nicht mitsichern


@app.route("/verwaltung/config-backup")
def config_backup():
    """Komplettes /config als ZIP herunterladen (Profile, Einstellungen, Historie, Metriken,
    Protokoll). ACHTUNG: enthaelt config.json mit secret+Passwort-Hash und die
    verschluesselten Tokens -> vertraulich behandeln."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(CONFIG_DIR):
            for f in files:
                if f in _BACKUP_SKIP or f.endswith(".tmp"):
                    continue
                full = os.path.join(root, f)
                arc = os.path.relpath(full, CONFIG_DIR)
                try:
                    zf.write(full, arc)
                except OSError:
                    pass
    buf.seek(0)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    _mark_backup()
    _log_activity("backup", "Voll-Backup heruntergeladen")
    return Response(buf.read(), mimetype="application/zip",
                    headers={"Content-Disposition": "attachment; filename=portal-config-%s.zip" % ts})


@app.route("/verwaltung/config-restore", methods=["POST"])
def config_restore():
    """Voll-Backup einspielen: ZIP nach /config entpacken (ueberschreibt). Mit
    Pfad-Traversal-Schutz. Danach ist ein Neustart/Reload noetig (evtl. neuer secret ->
    Sessions ungueltig -> Neu-Login)."""
    file = request.files.get("file")
    if not file or not file.filename:
        return redirect(url_for("verwaltung", tab="version", err="Keine Datei ausgewählt."))
    try:
        data = file.read()
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError):
        return redirect(url_for("verwaltung", tab="version", err="Keine gültige ZIP-Datei."))
    base = os.path.abspath(CONFIG_DIR)
    restored = 0
    for name in zf.namelist():
        if name.endswith("/") or name in _BACKUP_SKIP:
            continue
        dest = os.path.abspath(os.path.join(CONFIG_DIR, name))
        # Pfad-Traversal-Schutz: Ziel MUSS unter CONFIG_DIR liegen
        if not (dest == base or dest.startswith(base + os.sep)):
            continue
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(name) as src, open(dest, "wb") as out:
                out.write(src.read())
            restored += 1
        except OSError:
            pass
    _log_activity("restore", "Voll-Backup eingespielt (%d Dateien)" % restored)
    return redirect(url_for("verwaltung", tab="version",
                            msg="Backup eingespielt (%d Dateien). Bitte neu anmelden, falls die Sitzung endet." % restored))


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f TB" % n


# Reiter der Verwaltungs-Shell: (id, Label, Endpunkt des Fragments). Reihenfolge = Anzeige.
_VERW_TABS = [
    ("overview", "Überblick", "verwaltung_overview"),
    ("profiles", "Profile", "profiles"),
    ("kennzahlen", "Kennzahlen", "dashboard"),
    ("trends", "Trends", "trends"),
    ("werkzeuge", "Werkzeuge", "werkzeuge"),
    ("waechter", "Wächter", "waechter"),
    ("benachrichtigungen", "Benachrichtigungen", "notifications"),
    ("konto", "Konto", "settings"),
    ("version", "Version", "update_page"),
    ("protokoll", "Protokoll", "protokoll"),
]


@app.route("/verwaltung")
def verwaltung():
    """Verwaltungs-Shell: EINE Seite mit In-Page-Reitern. Jeder Reiter laedt sein Fragment
    lazy per fetch(<route>?embed=1). Live-Daten also erst beim Oeffnen des Reiters."""
    active = request.args.get("tab", "overview")
    if active not in {t[0] for t in _VERW_TABS}:
        active = "overview"
    msg, err = request.args.get("msg"), request.args.get("err")
    tabs = []
    for tid, label, ep in _VERW_TABS:
        args = {"embed": 1}
        if tid == active and msg:
            args["msg"] = msg
        if tid == active and err:
            args["err"] = err
        tabs.append({"id": tid, "label": label, "src": url_for(ep, **args)})
    return render_template("verwaltung_shell.html", tabs=tabs, active=active)


_WALL_ORDER = {"bad": 0, "unknown": 1, "ok": 2}


def _status_wall():
    """Ampel-Grid aller Instanzen — ausschliesslich aus bereits erfassten Daten
    (Waechter-Spiegel + Historie + Kennzahlen), BEWUSST ohne Live-Abfragen: sonst kostet
    jede tote Instanz 5 s Timeout und das Cockpit steht bei fuenf Profilen eine halbe
    Minute. Der Live-Blick auf die AKTIVE Instanz steht ohnehin oben auf der Seite,
    der volle Live-Vergleich im Dashboard."""
    st = _load_watch_state()
    res = st.get("results") or {}
    cutoff = time.time() - 30 * 86400
    try:
        profs = load_profiles()
    except (OSError, ValueError):
        return []
    aid = _active_id()
    tiles = []
    for pid, p in profs.items():
        checks = (res.get(pid) or {}).get("checks") or []
        problems = [c for c in checks if c.get("status") == "bad"]
        if not checks:
            level = "unknown"
        elif problems:
            level = "bad"
        elif any(c.get("status") == "ok" for c in checks):
            level = "ok"
        else:
            level = "unknown"  # ausschliesslich 'unknown' -> keine Aussage moeglich
        hist = [r for r in _read_watch(pid) if r.get("ts", 0) >= cutoff]
        last = (_read_metrics(pid, limit=1) or [None])[-1]
        tiles.append({
            "id": pid, "name": p.get("name") or "(ohne Name)", "active": pid == aid,
            "productive": bool(p.get("productive")), "level": level,
            "has_url": bool(p.get("paperless_url")),
            "problems": [{"label": c.get("label"), "detail": c.get("detail")} for c in problems],
            "checked": _fmt_rel_ts((res.get(pid) or {}).get("ts")) if checks else None,
            "uptime": _uptime_pct(hist, "downtime"),
            "total": (last or {}).get("total"),
        })
    # Auffaelliges zuerst — die Wand soll Probleme zeigen, nicht Alphabet.
    tiles.sort(key=lambda t: (_WALL_ORDER.get(t["level"], 1), t["name"].lower()))
    return tiles


@app.route("/verwaltung/overview")
def verwaltung_overview():
    """Cockpit-Inhalt (Fragment): Status des aktiven Profils, Kennzahlen, Profilliste, Selbststatus."""
    profs = load_profiles()
    aid = _active_id()
    act = profs.get(aid, {})
    url = act.get("paperless_url")
    token = _dec(act.get("paperless_token"))
    kind, text = _connection_status({"paperless_url": url, "paperless_token": token})
    stats = None
    if url and kind == "ok":
        stats = {
            "total": _api_count(url, token, "documents/?page_size=1"),
            "inbox": _api_count(url, token, "documents/?is_in_inbox=true&page_size=1"),
            "no_type": _api_count(url, token, "documents/?document_type__isnull=true&page_size=1"),
            "no_corr": _api_count(url, token, "documents/?correspondent__isnull=true&page_size=1"),
        }
    bts = _last_backup_ts()
    if bts:
        last_backup = _fmt_rel_ts(bts)
        _age_d = (int(time.time()) - bts) / 86400.0
        backup_level = "ok" if _age_d < 7 else ("warn" if _age_d < 30 else "err")
    else:
        last_backup = "noch nie"
        backup_level = "warn"
    _wc = _watcher_cfg()
    _st = _load_watch_state()  # nicht _watcher_state: der ist im fremden Worker leer
    _stamp = _build_stamp()
    portal = {"version": PORTAL_VERSION,
              "build": ("%s%s" % (_stamp["sha"], " · " + _stamp["date"] if _stamp["date"] else "")) if _stamp else "",
              "config_size": _fmt_size(_dir_size(CONFIG_DIR)),
              "last_backup": last_backup, "backup_level": backup_level,
              "watcher_on": _wc["enabled"], "watcher_last": _fmt_rel_ts(_st.get("last_run")),
              "watcher_alerts": len(_st.get("alerts_active") or ())}
    return render_template(
        "cockpit.html",
        active={"id": aid, "name": act.get("name") or "", "url": url or "",
                "productive": bool(act.get("productive")), "readonly": bool(act.get("readonly")),
                "conn_kind": kind, "conn_text": text,
                "has_config": act.get("generator_config") is not None},
        stats=stats, wall=_status_wall(), watcher_on=_wc["enabled"], portal=portal)


@app.route("/protokoll")
def protokoll():
    if request.args.get("download"):
        try:
            with open(ACTIVITY_PATH, encoding="utf-8") as fh:
                data = fh.read()
        except FileNotFoundError:
            data = ""
        return Response(data, mimetype="text/plain; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=activity.log"})
    entries = _read_activity(500)
    kinds = sorted({e.get("kind", "") for e in entries if e.get("kind")})
    return render_template("protokoll.html", entries=entries, kinds=kinds)


@app.route("/dashboard")
def dashboard():
    """Live-Kennzahlen + Drift (Config vs. Instanz) pro Profil."""
    profs = load_profiles()
    aid = _active_id()
    cards = []
    for pid, p in profs.items():
        url = p.get("paperless_url")
        token = _dec(p.get("paperless_token"))
        card = {"id": pid, "name": p.get("name") or "(ohne Name)", "active": pid == aid,
                "url": url or "", "online": False, "stats": None, "drift": None, "info": None,
                "has_config": p.get("generator_config") is not None}
        if url:
            total = _api_count(url, token, "documents/?page_size=1")
            if total is not None:
                card["online"] = True
                card["info"] = _instance_info(url, token)
                card["stats"] = {
                    "total": total,
                    "inbox": _api_count(url, token, "documents/?is_in_inbox=true&page_size=1"),
                    "no_type": _api_count(url, token, "documents/?document_type__isnull=true&page_size=1"),
                    "no_corr": _api_count(url, token, "documents/?correspondent__isnull=true&page_size=1"),
                }
                gc = p.get("generator_config") or {}
                if card["has_config"]:
                    drift = []
                    for label, key, ep in _DRIFT_CATS:
                        cfg_n = _count_active(gc, key)
                        inst_n = _api_count(url, token, ep + "?page_size=1")
                        drift.append({"label": label, "cfg": cfg_n, "inst": inst_n,
                                      "diff": (cfg_n - inst_n) if inst_n is not None else None})
                    card["drift"] = drift
        cards.append(card)
    cards.sort(key=lambda c: c["name"].lower())
    return render_template("dashboard.html", cards=cards)


# ─────────────────────────────────────────────────────────────────────────────
# BENACHRICHTIGUNGEN — Pushover / ntfy / E-Mail, je Profil konfigurierbar.
# Prioritaet wird auf der Pushover-Skala −2…2 je Ereignis gepflegt und fuer ntfy
# abgeleitet. Versand nur auf explizite Aktion (Testknopf) bzw. spaeter durch den
# Waechter (P4); das Portal schreibt damit nichts in die Instanz.
# ─────────────────────────────────────────────────────────────────────────────
_NOTIFY_EVENTS = [
    ("downtime", "Instanz nicht erreichbar / Token ungültig"),
    ("drift", "Konfigurations-Drift (fehlende Einträge)"),
    ("task_fail", "Fehlgeschlagene Verarbeitung (Consumer)"),
    ("asn_gap", "ASN-Lücken"),
    ("duplicate", "Duplikate gefunden"),
    ("digest", "Täglicher Status-Digest"),
    ("report", "Wochen-/Monatsreport"),
    ("update", "Portal-Update verfügbar"),
    ("error", "Fehler im Portal / Wächter"),
]
_NOTIFY_DEFAULT_PRIO = {"downtime": 1, "drift": 0, "task_fail": 1, "asn_gap": -1,
                        "duplicate": -1, "digest": -1, "report": -1, "update": 0, "error": 1}
_PRIO_LABELS = {-2: "−2 Stumm", -1: "−1 Leise", 0: "0 Normal", 1: "1 Hoch", 2: "2 Notfall"}
# Pushover-Prioritaet −2…2  ->  ntfy-Prioritaet 1…5.
_NTFY_PRIO = {-2: "1", -1: "2", 0: "3", 1: "4", 2: "5"}


def _clamp_prio(val, default=0):
    try:
        return max(-2, min(2, int(val)))
    except (TypeError, ValueError):
        return default


def _notif_of(prof):
    """Benachrichtigungs-Konfig eines Profils mit gefuellten Defaults (fuer Anzeige/Versand)."""
    n = prof.get("notifications") if isinstance(prof.get("notifications"), dict) else {}
    prios = n.get("priorities") if isinstance(n.get("priorities"), dict) else {}
    return {
        "pushover": {"enabled": bool((n.get("pushover") or {}).get("enabled")),
                     "token": (n.get("pushover") or {}).get("token") or "",
                     "user": (n.get("pushover") or {}).get("user") or ""},
        "ntfy": {"enabled": bool((n.get("ntfy") or {}).get("enabled")),
                 "server": (n.get("ntfy") or {}).get("server") or "https://ntfy.sh",
                 "topic": (n.get("ntfy") or {}).get("topic") or ""},
        "email": {"enabled": bool((n.get("email") or {}).get("enabled")),
                  "host": (n.get("email") or {}).get("host") or "",
                  "port": (n.get("email") or {}).get("port") or 587,
                  "user": (n.get("email") or {}).get("user") or "",
                  "password": (n.get("email") or {}).get("password") or "",
                  "from": (n.get("email") or {}).get("from") or "",
                  "to": (n.get("email") or {}).get("to") or "",
                  "tls": (n.get("email") or {}).get("tls", True)},
        "priorities": {ev: _clamp_prio(prios.get(ev, _NOTIFY_DEFAULT_PRIO[ev]),
                                       _NOTIFY_DEFAULT_PRIO[ev]) for ev, _ in _NOTIFY_EVENTS},
    }


def _hdr_safe(text):
    """HTTP-Header sind latin-1 — nicht darstellbare Zeichen (z. B. Emoji) entfernen."""
    return (text or "").encode("latin-1", "ignore").decode("latin-1")


def _notify_pushover(c, title, message, priority):
    if not c.get("token") or not c.get("user"):
        return False, "Token oder User-Key fehlt"
    data = {"token": c["token"], "user": c["user"],
            "title": (title or "")[:250], "message": (message or "")[:1024],
            "priority": priority}
    if priority >= 2:  # Notfall-Prioritaet verlangt retry/expire
        data.update({"retry": 60, "expire": 3600})
    try:
        r = requests.post("https://api.pushover.net/1/messages.json", data=data, timeout=10)
        if r.status_code == 200:
            return True, "gesendet"
        return False, "HTTP %d: %s" % (r.status_code, r.text[:160])
    except requests.RequestException as exc:
        return False, str(exc)


def _notify_ntfy(c, title, message, priority):
    topic = (c.get("topic") or "").strip()
    if not topic:
        return False, "Topic fehlt"
    server = (c.get("server") or "https://ntfy.sh").rstrip("/")
    try:
        r = requests.post(server + "/" + topic,
                          data=(message or "").encode("utf-8"),
                          headers={"Title": _hdr_safe(title),
                                   "Priority": _NTFY_PRIO.get(priority, "3")},
                          timeout=10)
        if r.status_code in (200, 201):
            return True, "gesendet"
        return False, "HTTP %d" % r.status_code
    except requests.RequestException as exc:
        return False, str(exc)


def _notify_email(c, subject, body):
    import smtplib
    from email.message import EmailMessage
    host = (c.get("host") or "").strip()
    to = (c.get("to") or "").strip()
    if not host or not to:
        return False, "SMTP-Host oder Empfänger fehlt"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = c.get("from") or c.get("user") or "paperless-portal@localhost"
    msg["To"] = to
    msg.set_content(body)
    try:
        port = int(c.get("port") or 587)
    except (TypeError, ValueError):
        port = 587
    try:
        if port == 465:
            srv = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            srv = smtplib.SMTP(host, port, timeout=15)
            if c.get("tls", True):
                srv.starttls()
        try:
            if c.get("user"):
                srv.login(c["user"], c.get("password") or "")
            srv.send_message(msg)
        finally:
            srv.quit()
        return True, "gesendet"
    except Exception as exc:  # smtplib wirft diverse Fehlerklassen
        return False, str(exc)


def _dispatch_notification(prof, event, title, message, only=None, bump=0):
    """An alle aktivierten (bzw. mit ``only`` gewaehlten) Kanaele senden.
    Rueckgabe: (priority, [(kanal, ok, detail), ...]). Entschluesselt Secrets hier zentral.
    ``bump`` verschiebt die Prioritaet dieses einen Versands (Eskalation +1, Entwarnung −1)."""
    n = _notif_of(prof)
    prio = _clamp_prio(n["priorities"].get(event, _NOTIFY_DEFAULT_PRIO.get(event, 0)))
    if bump:
        prio = _clamp_prio(prio + bump, prio)
    results = []
    if (only in (None, "pushover")) and n["pushover"]["enabled"]:
        c = {"token": _dec(n["pushover"]["token"]), "user": _dec(n["pushover"]["user"])}
        ok, detail = _notify_pushover(c, title, message, prio)
        results.append(("Pushover", ok, detail))
    if (only in (None, "ntfy")) and n["ntfy"]["enabled"]:
        ok, detail = _notify_ntfy(n["ntfy"], title, message, prio)
        results.append(("ntfy", ok, detail))
    if (only in (None, "email")) and n["email"]["enabled"]:
        c = dict(n["email"])
        c["password"] = _dec(n["email"]["password"])
        ok, detail = _notify_email(c, title, message)
        results.append(("E-Mail", ok, detail))
    return prio, results


@app.route("/verwaltung/benachrichtigungen", methods=["GET", "POST"])
def notifications():
    profs = load_profiles()
    aid = _active_id()
    if not aid or aid not in profs:
        return redirect(url_for("verwaltung", tab="profiles", err="Kein aktives Profil."))
    prof = profs[aid]

    if request.method == "POST":
        f = request.form
        action = f.get("action", "save")
        cur = _notif_of(prof)  # bestehende (ggf. verschluesselte) Secrets als Ausgangswert
        n = {
            "pushover": {
                "enabled": bool(f.get("pushover_enabled")),
                # Secrets: leeres Feld = bestehenden Wert behalten (write-only-UI)
                "token": f.get("pushover_token", "").strip() or cur["pushover"]["token"],
                "user": f.get("pushover_user", "").strip() or cur["pushover"]["user"],
            },
            "ntfy": {
                "enabled": bool(f.get("ntfy_enabled")),
                "server": f.get("ntfy_server", "").strip() or "https://ntfy.sh",
                "topic": f.get("ntfy_topic", "").strip(),
            },
            "email": {
                "enabled": bool(f.get("email_enabled")),
                "host": f.get("email_host", "").strip(),
                "port": _clamp_port(f.get("email_port")),
                "user": f.get("email_user", "").strip(),
                "password": f.get("email_password", "") or cur["email"]["password"],
                "from": f.get("email_from", "").strip(),
                "to": f.get("email_to", "").strip(),
                "tls": bool(f.get("email_tls")),
            },
            "priorities": {ev: _clamp_prio(f.get("prio_" + ev), _NOTIFY_DEFAULT_PRIO[ev])
                           for ev, _ in _NOTIFY_EVENTS},
        }
        profs[aid]["notifications"] = n
        save_profiles(profs)  # verschluesselt Secrets at-rest
        if action.startswith("test_"):
            channel = action[len("test_"):]
            title = "Paperless Portal — Testbenachrichtigung"
            body = ("Test von Profil %s. Wenn du das liest, funktioniert der Kanal."
                    % (prof.get("name") or "?"))
            _prio, res = _dispatch_notification(profs[aid], "update", title, body, only=channel)
            if not res:
                return redirect(url_for("verwaltung", tab="benachrichtigungen", err="Kanal ist nicht aktiviert."))
            name, ok, detail = res[0]
            _log_activity("notify", "Test %s (%s): %s" % (name, prof.get("name"),
                                                          "ok" if ok else detail))
            if ok:
                return redirect(url_for("verwaltung", tab="benachrichtigungen", msg="%s: Testnachricht gesendet." % name))
            return redirect(url_for("verwaltung", tab="benachrichtigungen", err="%s fehlgeschlagen: %s" % (name, detail)))
        return redirect(url_for("verwaltung", tab="benachrichtigungen", msg="Benachrichtigungen gespeichert."))

    view = _notif_of(prof)
    return render_template(
        "notifications.html", prof_name=prof.get("name") or "",
        n=view,
        # Secrets nie im Klartext ins Formular — nur „gesetzt/leer" anzeigen.
        has_pushover_token=bool(view["pushover"]["token"]),
        has_pushover_user=bool(view["pushover"]["user"]),
        has_email_password=bool(view["email"]["password"]),
        events=_NOTIFY_EVENTS, prio_labels=_PRIO_LABELS,
        msg=request.args.get("msg"), err=request.args.get("err"))


def _clamp_port(val):
    try:
        return max(1, min(65535, int(str(val).strip())))
    except (TypeError, ValueError):
        return 587


# ─────────────────────────────────────────────────────────────────────────────
# WÄCHTER (P4) — periodische, STRIKT READ-ONLY Checks je Profil. Bei Auffaelligkeit
# Benachrichtigung ueber die P3-Kanaele. Kein Instanz-Schreibvorgang, niemals.
# Genau EIN Worker fuehrt die Schleife aus (POSIX-Dateisperre in /config, da
# gunicorn mit -w 2 --preload laeuft). Zeitplan + Checks konfigurierbar.
# ─────────────────────────────────────────────────────────────────────────────
_WATCHER_DEFAULTS = {
    "enabled": False,
    "interval_min": 60,
    "checks": {"downtime": True, "drift": True, "task_fail": True,
               "asn_gap": False, "duplicate": False},
    "asn_gap_threshold": 1,
    "digest_enabled": False,
    "digest_hour": 8,          # Ortszeit-Stunde fuer den taeglichen Status-Digest
    "heartbeat_url": "",       # Dead-Man's-Switch: URL wird nach jedem Zyklus gepingt
    # Eskalation (Z2)
    "fail_threshold": 2,       # so viele schlechte Laeufe in Folge -> erst dann Alarm
    "ok_threshold": 1,         # so viele gute Laeufe in Folge -> erst dann Entwarnung
    "repeat_min": 0,           # Erinnerungsabstand in Minuten (0 = keine Erinnerung)
    "repeat_backoff": True,    # Abstand je Erinnerung verdoppeln (gedeckelt auf 24 h)
    "escalate_after": 3,       # ab der N-ten Erinnerung Prioritaet +1 (0 = nie)
    "recovery_notify": True,   # Entwarnung auch ueber die Kanaele melden
    # Wochen-/Monatsreport (Z4)
    "report_enabled": False,
    "report_period": "week",   # 'week' = letzte 7 Tage, 'month' = letzte 30 Tage
    "report_weekday": 0,       # bei 'week': 0 = Montag … 6 = Sonntag
    "report_day": 1,           # bei 'month': Kalendertag
    "report_hour": 8,          # Ortszeit-Stunde
}
# Report-Tag nie ueber 28: der 29./30./31. faellt in kurzen Monaten aus, im Februar
# jedes Jahr — der Report bliebe dann still aus, und still ausbleiben ist das Schlimmste,
# was eine Ueberwachung tun kann.
_REPORT_MAX_DAY = 28
_ESC_MAX_GAP_MIN = 1440   # Erinnerungsabstand nie groesser als 24 h
_ESC_MAX_DOUBLE = 12      # Backoff-Verdopplungen deckeln (2**12 * base reicht immer)
# Laufzeit-Zustand des Waechters (nur im Prozess, der die Sperre haelt). Fuer die UI +
# Alarm-Entprellung (nur bei Zustandswechsel ok->schlecht senden, nicht jede Runde).
_watcher_state = {
    "owner": False,      # haelt dieser Prozess die Sperre / fuehrt die Schleife aus?
    "last_run": None,    # Unix-ts des letzten Prueflaufs
    "next_run": None,    # Unix-ts des naechsten faelligen Laufs
    "results": {},       # pid -> {name, ts, checks:[{event,label,status,detail}]}
    "alerts_active": set(),   # {(pid, event)} — aktuell gemeldete Auffaelligkeiten
    # 'pid|event' -> {fail, ok, since, last, reps} — Streaks + Erinnerungstakt der
    # Eskalation. Getrennt von alerts_active, weil hier auch Kandidaten stehen, die
    # die Alarmschwelle noch nicht gerissen haben.
    "alert_meta": {},
    "last_error": None,
    "last_metrics": None,  # Unix-ts der letzten Kennzahl-Erfassung (Trends, gedrosselt)
    "last_digest": None,   # 'YYYY-MM-DD' des zuletzt gesendeten Tages-Digests
    "last_report": None,   # 'YYYY-MM-DD' des zuletzt gesendeten Wochen-/Monatsreports
    "last_heartbeat": None,  # Unix-ts des letzten Heartbeat-Pings
    "last_webhook": None,  # {ts, ok, detail, event} — letzte Webhook-Zustellung (Diagnose)
}
_watcher_started = False
_watcher_lock_fh = None


def _watch_int(val, default):
    try:
        return int(str(val).strip())
    except (TypeError, ValueError, AttributeError):
        return default


def _watcher_cfg():
    """Normalisierte Waechter-Konfiguration (global, in config.json unter 'watcher')."""
    w = {}
    try:
        w = load_config().get("watcher") or {}
    except (OSError, ValueError):
        pass
    ch = w.get("checks") or {}
    return {
        "enabled": bool(w.get("enabled", False)),
        "interval_min": max(5, _watch_int(w.get("interval_min"), 60)),
        "checks": {k: bool(ch.get(k, _WATCHER_DEFAULTS["checks"][k]))
                   for k in _WATCHER_DEFAULTS["checks"]},
        "asn_gap_threshold": max(1, _watch_int(w.get("asn_gap_threshold"), 1)),
        "digest_enabled": bool(w.get("digest_enabled", False)),
        "digest_hour": max(0, min(23, _watch_int(w.get("digest_hour"), 8))),
        "heartbeat_url": (w.get("heartbeat_url") or "").strip(),
        "fail_threshold": max(1, min(10, _watch_int(w.get("fail_threshold"), 2))),
        "ok_threshold": max(1, min(10, _watch_int(w.get("ok_threshold"), 1))),
        "repeat_min": max(0, min(_ESC_MAX_GAP_MIN, _watch_int(w.get("repeat_min"), 0))),
        "repeat_backoff": bool(w.get("repeat_backoff", True)),
        "escalate_after": max(0, min(20, _watch_int(w.get("escalate_after"), 3))),
        "recovery_notify": bool(w.get("recovery_notify", True)),
        "report_enabled": bool(w.get("report_enabled", False)),
        "report_period": (w.get("report_period") if w.get("report_period") in _REPORT_PERIODS
                          else "week"),
        "report_weekday": max(0, min(6, _watch_int(w.get("report_weekday"), 0))),
        "report_day": max(1, min(_REPORT_MAX_DAY, _watch_int(w.get("report_day"), 1))),
        "report_hour": max(0, min(23, _watch_int(w.get("report_hour"), 8))),
    }


def _chk_downtime(url, token):
    """Erreichbarkeit + Token-Gueltigkeit (deckt Token-Ablauf/-Widerruf via 401/403)."""
    code = _test_paperless(url, token)
    if code == 200:
        return {"event": "downtime", "label": "Erreichbarkeit", "status": "ok",
                "detail": "Erreichbar, Token gültig."}
    if code is None:
        return {"event": "downtime", "label": "Erreichbarkeit", "status": "bad",
                "detail": "Paperless nicht erreichbar unter %s." % url}
    if code in (401, 403):
        return {"event": "downtime", "label": "Erreichbarkeit", "status": "bad",
                "detail": "Token ungültig (HTTP %d) — evtl. abgelaufen/widerrufen." % code}
    return {"event": "downtime", "label": "Erreichbarkeit", "status": "bad",
            "detail": "Unerwartete Antwort HTTP %d." % code}


def _chk_drift(url, token, gc):
    """Config vs. Instanz: fehlende Eintraege (Config hat mehr als die Instanz)."""
    if not gc:
        return {"event": "drift", "label": "Konfig-Drift", "status": "unknown",
                "detail": "Keine gespeicherte Profil-Config."}
    missing, parts, unreachable = 0, [], False
    for label, key, ep in _DRIFT_CATS:
        cfg_n = len(gc.get(key) or [])
        inst_n = _api_count(url, token, ep + "?page_size=1")
        if inst_n is None:
            unreachable = True
            continue
        if cfg_n - inst_n > 0:
            missing += cfg_n - inst_n
            parts.append("%s: %d fehlen" % (label, cfg_n - inst_n))
    if not parts and unreachable:
        return {"event": "drift", "label": "Konfig-Drift", "status": "unknown",
                "detail": "Instanz nicht erreichbar."}
    if missing > 0:
        return {"event": "drift", "label": "Konfig-Drift", "status": "bad",
                "detail": "; ".join(parts)}
    return {"event": "drift", "label": "Konfig-Drift", "status": "ok",
            "detail": "Config deckt sich mit der Instanz."}


def _asn_pages(url, token, fields):
    """Alle Dokumentwerte eines Feldes einsammeln (max. 40 Seiten = read-only, gedeckelt)."""
    hdr = {"Authorization": "Token " + token} if token else {}
    rows, pages = [], 0
    nexturl = (url.rstrip("/") + "/api/documents/?page_size=250&fields=" + fields
               + "&ordering=" + fields)
    while nexturl and pages < 40:
        r = requests.get(nexturl, headers=hdr, timeout=10, allow_redirects=False)
        if r.status_code != 200:
            raise ValueError("HTTP %d" % r.status_code)
        j = r.json()
        rows.extend(j.get("results", []))
        nexturl = j.get("next")
        pages += 1
    return rows


def _chk_asn_gap(url, token, threshold):
    """Luecken im vergebenen ASN-Bereich (min..max) — Semantik wie ASN-Luecken-Finder."""
    try:
        rows = _asn_pages(url, token, "archive_serial_number")
    except (requests.RequestException, ValueError, TypeError):
        return {"event": "asn_gap", "label": "ASN-Lücken", "status": "unknown",
                "detail": "Nicht abrufbar."}
    asns = set()
    for d in rows:
        a = d.get("archive_serial_number")
        if a:
            asns.add(int(a))
    if not asns:
        return {"event": "asn_gap", "label": "ASN-Lücken", "status": "ok",
                "detail": "Keine ASN vergeben."}
    lo, hi = min(asns), max(asns)
    gaps = (hi - lo + 1) - len(asns)
    if gaps >= threshold:
        return {"event": "asn_gap", "label": "ASN-Lücken", "status": "bad",
                "detail": "%d Lücke(n) im ASN-Bereich %d–%d." % (gaps, lo, hi)}
    return {"event": "asn_gap", "label": "ASN-Lücken", "status": "ok",
            "detail": "Lückenlos (%d–%d)." % (lo, hi)}


def _chk_duplicate(url, token):
    """Best-effort: identische Dokumenttitel (moegliche Duplikate). Heuristik, default aus."""
    try:
        rows = _asn_pages(url, token, "title")
    except (requests.RequestException, ValueError, TypeError):
        return {"event": "duplicate", "label": "Duplikate", "status": "unknown",
                "detail": "Nicht abrufbar."}
    seen, dups = {}, 0
    for d in rows:
        t = (d.get("title") or "").strip().lower()
        if not t:
            continue
        seen[t] = seen.get(t, 0) + 1
        if seen[t] == 2:
            dups += 1
    if dups > 0:
        return {"event": "duplicate", "label": "Duplikate", "status": "bad",
                "detail": "%d Titel mehrfach vergeben (mögliche Duplikate)." % dups}
    return {"event": "duplicate", "label": "Duplikate", "status": "ok",
            "detail": "Keine gleichlautenden Titel."}


def _is_consume_task(t):
    """Nur der Dokumenteneinzug zaehlt (task_name 'consume_file', je nach Version mit
    Modulpfad davor). Wartungs-Tasks wie train_classifier/check_sanity bleiben aussen vor:
    train_classifier scheitert auf jeder frischen Instanz reihenweise mit 'No training data
    available' — an der Test-Instanz waren 413 von 414 Fehlern genau das. Die als Alarm zu
    melden waere reines Rauschen. Ohne task_name (aeltere Versionen) dient der Dateiname als
    Indiz: den haben nur Einzug-Tasks."""
    name = t.get("task_name")
    if name:
        return str(name).endswith("consume_file")
    return bool(t.get("task_file_name"))


def _chk_task_fail(url, token):
    """Fehlgeschlagener Dokumenteneinzug: /api/tasks/ auf FAILURE. Read-only.
    Paperless behaelt FAILURE-Tasks dauerhaft; abgehakte ('acknowledged') zaehlen nicht mehr
    mit — sonst haengt der Alarm fuer immer an einem laengst erledigten Fehler."""
    hdr = {"Authorization": "Token " + token} if token else {}
    try:
        r = requests.get(url.rstrip("/") + "/api/tasks/", headers=hdr, timeout=8,
                         allow_redirects=False)
        if r.status_code != 200:
            return {"event": "task_fail", "label": "Verarbeitung", "status": "unknown",
                    "detail": "Task-Liste nicht abrufbar (HTTP %d)." % r.status_code}
        data = r.json()
        rows = data.get("results", data) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return {"event": "task_fail", "label": "Verarbeitung", "status": "unknown",
                    "detail": "Unerwartetes Format der Task-Liste."}
    except (requests.RequestException, ValueError):
        return {"event": "task_fail", "label": "Verarbeitung", "status": "unknown",
                "detail": "Task-Liste nicht abrufbar."}
    fails = [t for t in rows
             if isinstance(t, dict) and str(t.get("status", "")).upper() == "FAILURE"
             and not t.get("acknowledged") and _is_consume_task(t)]
    if not fails:
        return {"event": "task_fail", "label": "Verarbeitung", "status": "ok",
                "detail": "Kein offener Einzug-Fehler."}
    names = [str(t.get("task_file_name") or "?") for t in fails[:3]]
    more = "" if len(fails) <= 3 else " u. a."
    return {"event": "task_fail", "label": "Verarbeitung", "status": "bad",
            "detail": "%d Dokument(e) nicht eingelesen: %s%s — in Paperless unter "
                      "Verwaltung → Aufgaben prüfen und abhaken." % (
                          len(fails), ", ".join(names), more)}


def _fmt_dur(sec):
    """Dauer in Klartext: '45 min', '3 h', '2 d 5 h'."""
    sec = int(max(0, sec or 0))
    if sec < 3600:
        return "%d min" % (sec // 60)
    if sec < 86400:
        return "%d h" % (sec // 3600)
    return "%d d %d h" % (sec // 86400, (sec % 86400) // 3600)


def _alert_meta(pid, event):
    """Streak-/Takt-Eintrag zu (Profil, Ereignis) — wird bei Bedarf angelegt."""
    m = _watcher_state.setdefault("alert_meta", {})
    return m.setdefault("%s|%s" % (pid, event),
                        {"fail": 0, "ok": 0, "since": None, "last": None, "reps": 0})


def _repeat_gap_min(wc, reps):
    """Abstand bis zur naechsten Erinnerung in Minuten. Mit Backoff verdoppelt er sich
    je gesendeter Erinnerung (1×, 2×, 4× …), gedeckelt auf 24 h."""
    gap = wc["repeat_min"]
    if wc["repeat_backoff"]:
        gap *= 2 ** min(reps, _ESC_MAX_DOUBLE)
    return min(gap, _ESC_MAX_GAP_MIN)


def _maybe_alert(prof, pid, c, wc=None):
    """Eskalation (Z2). Alarm erst nach ``fail_threshold`` schlechten Laeufen in Folge
    (Flapping-Schutz), danach Erinnerungen mit wachsendem Abstand statt Dauerfeuer, ab
    ``escalate_after`` Erinnerungen eine Stufe lauter, und beim Zurueckkommen einmal
    Entwarnung. 'unknown' friert die Streaks ein — wir wissen es schlicht nicht."""
    wc = wc or _watcher_cfg()
    key = (pid, c["event"])
    active = _watcher_state["alerts_active"]
    meta = _alert_meta(pid, c["event"])
    name = prof.get("name") or pid
    now = time.time()

    if c["status"] not in ("bad", "ok"):
        return

    if c["status"] == "bad":
        meta["ok"] = 0
        meta["fail"] = (meta.get("fail") or 0) + 1
        if key not in active:
            if meta["fail"] < wc["fail_threshold"]:
                return  # noch in Beobachtung — koennte ein einzelner Aussetzer sein
            active.add(key)
            meta.update({"since": now, "last": now, "reps": 0})
            _dispatch_notification(prof, c["event"],
                                   "Paperless-Wächter: %s (%s)" % (c["label"], name),
                                   c["detail"])
            _fire_webhook("alarm", c["event"], name, "bad", c["detail"])
            _log_activity("watcher", "Alarm: %s (%s)" % (c["label"], name),
                          level="err", detail=c["detail"])
            return
        # Bereits gemeldet -> hoechstens erinnern.
        if not wc["repeat_min"]:
            return
        if not meta.get("last"):
            meta["last"] = now  # Bestandsalarm ohne Takt (z. B. direkt nach Update)
            return
        if now - meta["last"] < _repeat_gap_min(wc, meta.get("reps") or 0) * 60:
            return
        meta["reps"] = (meta.get("reps") or 0) + 1
        meta["last"] = now
        bump = 1 if (wc["escalate_after"] and meta["reps"] >= wc["escalate_after"]) else 0
        msg = "%s\n\nSeit %s offen (%d. Erinnerung)." % (
            c["detail"], _fmt_dur(now - (meta.get("since") or now)), meta["reps"])
        _dispatch_notification(prof, c["event"],
                               "Paperless-Wächter: %s (%s) — weiterhin offen" % (c["label"], name),
                               msg, bump=bump)
        _fire_webhook("reminder", c["event"], name, "bad", msg)
        _log_activity("watcher", "Erinnerung %d: %s (%s)" % (meta["reps"], c["label"], name),
                      level="err", detail=msg)
        return

    # status == 'ok'
    meta["fail"] = 0
    if key not in active:
        meta["ok"] = 0
        return
    meta["ok"] = (meta.get("ok") or 0) + 1
    if meta["ok"] < wc["ok_threshold"]:
        return  # noch nicht stabil genug fuer die Entwarnung
    active.discard(key)
    dur = _fmt_dur(now - (meta.get("since") or now))
    meta.update({"ok": 0, "since": None, "last": None, "reps": 0})
    if wc["recovery_notify"]:
        _dispatch_notification(prof, c["event"],
                               "Paperless-Wächter: %s (%s) — behoben" % (c["label"], name),
                               "Wieder in Ordnung nach %s.\n%s" % (dur, c["detail"]),
                               bump=-1)
    _fire_webhook("recovery", c["event"], name, "ok", "Entwarnung: " + c["detail"])
    _log_activity("watcher", "Entwarnung: %s (%s)" % (c["label"], name),
                  level="ok", detail="Offen gewesen: %s\n%s" % (dur, c["detail"]))


def _prune_alert_state(live_pids):
    """Zustand zu geloeschten/abgeschalteten Profilen verwerfen, sonst waechst er ewig."""
    meta = _watcher_state.get("alert_meta") or {}
    for k in [k for k in meta if k.split("|", 1)[0] not in live_pids]:
        meta.pop(k, None)
    for key in [k for k in _watcher_state["alerts_active"] if k[0] not in live_pids]:
        _watcher_state["alerts_active"].discard(key)


def _metrics_path(pid):
    return os.path.join(METRICS_DIR, pid + ".jsonl")


def _record_metrics(force=False):
    """Kennzahlen je Profil als JSONL-Zeile mitschreiben (Trends). Read-only; gedrosselt
    auf ~1x/Stunde, damit haeufige Waechter-Zyklen nicht dauernd Zaehler abfragen."""
    now = time.time()
    if not force and _watcher_state.get("last_metrics") and now - _watcher_state["last_metrics"] < 3300:
        return
    _watcher_state["last_metrics"] = now
    try:
        profs = load_profiles()
    except (OSError, ValueError):
        return
    os.makedirs(METRICS_DIR, exist_ok=True)
    for pid, p in profs.items():
        url = p.get("paperless_url")
        if not url:
            continue
        token = _dec(p.get("paperless_token"))
        _t0 = time.time()
        total = _api_count(url, token, "documents/?page_size=1")
        lat = int((time.time() - _t0) * 1000)  # Antwortzeit der Instanz in ms
        if total is None:
            continue  # nicht erreichbar -> keine Luecke mit Nullwerten erzeugen
        row = {
            "ts": int(now),
            "total": total,
            "inbox": _api_count(url, token, "documents/?is_in_inbox=true&page_size=1"),
            "no_type": _api_count(url, token, "documents/?document_type__isnull=true&page_size=1"),
            "no_corr": _api_count(url, token, "documents/?correspondent__isnull=true&page_size=1"),
            "lat": lat,
        }
        path = _metrics_path(pid)
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            # Datei gedeckelt halten (letzte METRICS_MAX Zeilen)
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
            if len(lines) > METRICS_MAX:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.writelines(lines[-METRICS_MAX:])
        except OSError:
            pass


def _read_metrics(pid, limit=500):
    rows = []
    try:
        with open(_metrics_path(pid), encoding="utf-8") as fh:
            for ln in fh.readlines()[-limit:]:
                try:
                    rows.append(json.loads(ln))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return rows


def _watch_path(pid):
    return os.path.join(WATCH_DIR, pid + ".jsonl")


def _append_watch(pid, checks, ts):
    """Ergebnis eines Prueflaufs als JSONL-Zeile mitschreiben — kompakt: nur Event->Status.
    Die Details wandern nicht mit (Protokoll deckt sie ab), die Historie soll klein bleiben."""
    row = {"ts": int(ts), "c": {c["event"]: c["status"] for c in checks}}
    try:
        os.makedirs(WATCH_DIR, exist_ok=True)
        path = _watch_path(pid)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
        if len(lines) > WATCH_MAX:
            with open(path, "w", encoding="utf-8") as fh:
                fh.writelines(lines[-WATCH_MAX:])
    except OSError:
        pass


def _read_watch(pid, limit=WATCH_MAX):
    rows = []
    try:
        with open(_watch_path(pid), encoding="utf-8") as fh:
            for ln in fh.readlines()[-limit:]:
                try:
                    rows.append(json.loads(ln))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return rows


def _uptime_pct(rows, event="downtime"):
    """Anteil der Prueflaeufe mit Status ok. 'unknown' zaehlt nicht mit (weder gut noch
    schlecht). None, wenn es zu dem Event keine verwertbaren Laeufe gibt."""
    vals = [r.get("c", {}).get(event) for r in rows]
    good = sum(1 for v in vals if v == "ok")
    bad = sum(1 for v in vals if v == "bad")
    if good + bad == 0:
        return None
    return round(good / (good + bad) * 100, 1)


def _last_bad_ts(rows, event="downtime"):
    """Unix-ts des juengsten Laufs, in dem das Event 'bad' war (oder None)."""
    for r in reversed(rows):
        if r.get("c", {}).get(event) == "bad":
            return r.get("ts")
    return None


def _save_watch_state():
    """Laufzeitzustand des Waechters auf Platte spiegeln, damit ihn auch Worker sehen,
    die die Schleife NICHT ausfuehren (sonst zeigt die UI je nach Worker Leere)."""
    st = dict(_watcher_state)
    st["alerts_active"] = ["%s|%s" % (p, e) for p, e in _watcher_state["alerts_active"]]
    st.pop("owner", None)  # prozesslokal — sagt nichts ueber den fremden Worker aus
    if not _watcher_state.get("owner"):
        # Ein manueller "Jetzt pruefen"-Lauf kann in einem Worker OHNE Schleife landen.
        # Taktung/Heartbeat/Digest gehoeren dem Owner — dessen Werte hier nicht mit den
        # leeren Lokalwerten ueberschreiben, sonst zeigt die UI "naechster Lauf —".
        try:
            with open(WATCH_STATE_PATH, encoding="utf-8") as fh:
                old = json.load(fh)
            for k in ("next_run", "last_heartbeat", "last_digest", "last_report", "last_error"):
                if st.get(k) is None:
                    st[k] = old.get(k)
        except (OSError, ValueError):
            pass
    try:
        tmp = WATCH_STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
        os.replace(tmp, WATCH_STATE_PATH)  # atomar: nie ein halb geschriebener Stand
    except (OSError, TypeError):
        pass


def _load_watch_state():
    """Gespiegelten Zustand lesen. Im Owner-Prozess ist der eigene Speicher aktueller."""
    if _watcher_state.get("owner"):
        return _watcher_state
    try:
        with open(WATCH_STATE_PATH, encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return _watcher_state
    if (_watcher_state.get("last_run") or 0) > (st.get("last_run") or 0):
        return _watcher_state  # eigener manueller Lauf ist juenger als der Spiegel
    st["alerts_active"] = {tuple(k.split("|", 1)) for k in (st.get("alerts_active") or [])}
    st["owner"] = False
    return st


def _profile_watch(p, gwc):
    """Effektive Ueberwachung eines Profils: eigene 'watch'-Einstellung ODER globale Vorgabe.
    Rueckgabe: {enabled, checks:{…}, custom}. Fehlt 'watch' -> ueberwacht mit globalen Checks."""
    w = p.get("watch") or {}
    ch = w.get("checks") or {}
    checks = {k: bool(ch.get(k, gwc["checks"][k])) for k in gwc["checks"]}
    return {"enabled": bool(w.get("enabled", True)), "checks": checks, "custom": bool(p.get("watch"))}


def _run_watch_cycle(wc, dispatch=True):
    """Ein Prueflauf ueber alle Profile mit URL. STRIKT read-only. Rueckgabe: results-Dict.
    Checks je Profil ueber _profile_watch (globale Vorgabe, pro Profil ueberschreibbar)."""
    results = {}
    try:
        profs = load_profiles()
    except (OSError, ValueError):
        return results
    for pid, p in profs.items():
        url = p.get("paperless_url")
        if not url:
            continue
        pw = _profile_watch(p, wc)
        if not pw["enabled"]:
            continue  # diese Instanz nicht ueberwachen
        token = _dec(p.get("paperless_token"))
        checks = []
        if pw["checks"]["downtime"]:
            checks.append(_chk_downtime(url, token))
        if pw["checks"]["drift"]:
            checks.append(_chk_drift(url, token, p.get("generator_config") or {}))
        if pw["checks"]["task_fail"]:
            checks.append(_chk_task_fail(url, token))
        if pw["checks"]["asn_gap"]:
            checks.append(_chk_asn_gap(url, token, wc["asn_gap_threshold"]))
        if pw["checks"]["duplicate"]:
            checks.append(_chk_duplicate(url, token))
        now = time.time()
        results[pid] = {"name": p.get("name") or pid, "ts": now, "checks": checks}
        if checks:
            _append_watch(pid, checks, now)  # Historie: Uptime/Timeline im Waechter-Reiter
        if dispatch:
            for c in checks:
                _maybe_alert(p, pid, c, wc)
    if dispatch:
        _prune_alert_state(set(results))
    _watcher_state["results"] = results
    _watcher_state["last_run"] = time.time()
    _record_metrics()  # Trends miterfassen (gedrosselt)
    _save_watch_state()
    return results


def _profile_digest_line(url, token, gc):
    """Einzeilige Statuszusammenfassung eines Profils fuer den Tages-Digest."""
    d = _chk_downtime(url, token)
    if d["status"] != "ok":
        return "⚠ " + d["detail"]
    parts = []
    total = _api_count(url, token, "documents/?page_size=1")
    if total is not None:
        parts.append("%s Dokumente" % total)
    inbox = _api_count(url, token, "documents/?is_in_inbox=true&page_size=1")
    if inbox:
        parts.append("%s im Posteingang" % inbox)
    dr = _chk_drift(url, token, gc)
    parts.append("keine Drift" if dr["status"] == "ok" else dr["detail"])
    return "✓ " + ", ".join(parts)


def _send_digest():
    """Pro Profil eine Kurz-Statusmeldung (Ereignis 'digest') an die aktiven Kanaele und/oder
    den Webhook. Profile, die niemand hoert (kein Kanal UND kein Webhook), werden gar nicht
    erst abgefragt — der Digest kostet Live-Abfragen."""
    hook = _webhook_wants("digest")
    try:
        profs = load_profiles()
    except (OSError, ValueError):
        return 0
    sent = 0
    for pid, p in profs.items():
        url = p.get("paperless_url")
        if not url:
            continue
        chan = _has_channel(p)
        if not (chan or hook):
            continue
        name = p.get("name") or pid
        token = _dec(p.get("paperless_token"))
        line = _profile_digest_line(url, token, p.get("generator_config") or {})
        if chan:
            _dispatch_notification(p, "digest", "Paperless-Digest: %s" % name, line)
        if hook:
            _fire_webhook("digest", "digest", name, "info", line)
        sent += 1
    if sent:
        _log_activity("watcher", "Täglicher Digest gesendet (%d Profil(e))" % sent)
    return sent


# ─────────────────────────────────────────────────────────────────────────────
# WOCHEN-/MONATSREPORT (Z4) — Rueckblick statt Momentaufnahme.
# Unterschied zum Tages-Digest: der fragt die Instanz live "wie steht es jetzt". Der Report
# beantwortet "was war in den letzten N Tagen" — und das kann NUR die aufgezeichnete
# Historie. Deshalb hier kein einziger Netz-Aufruf ausser dem Versand selbst.
# ─────────────────────────────────────────────────────────────────────────────
_REPORT_PERIODS = {"week": ("Woche", 7), "month": ("Monat", 30)}
_WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def _report_days(period):
    return _REPORT_PERIODS.get(period, _REPORT_PERIODS["week"])[1]


def _rows_since(rows, since):
    return [r for r in rows if (r.get("ts") or 0) >= since]


def _fmt_int(n):
    """1284 -> '1.284' (deutsche Tausendertrennung)."""
    return "{:,}".format(int(n)).replace(",", ".")


def _median(vals):
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _outages(rows, event="downtime"):
    """Zusammenhaengende Schlecht-Strecken -> [(start_ts, end_ts|None)]. Ende ist der Lauf,
    in dem der Check wieder ok war; None = beim letzten Lauf noch offen.
    'unknown' beendet eine Strecke NICHT — wir wissen dann schlicht nichts, und ein
    Aussetzer der Messung darf einen Ausfall nicht kuenstlich zerteilen."""
    out, start = [], None
    for r in rows:
        s = (r.get("c") or {}).get(event)
        if s == "bad" and start is None:
            start = r.get("ts")
        elif s == "ok" and start is not None:
            out.append((start, r.get("ts")))
            start = None
    if start is not None:
        out.append((start, None))
    return out


def _longest_outage(rows, event="downtime", now=None):
    """Laengste Schlecht-Strecke als {start, end, sec} (oder None). Eine noch offene
    Strecke zaehlt bis jetzt."""
    now = now or time.time()
    best = None
    for start, end in _outages(rows, event):
        sec = int((end or now) - start)
        if best is None or sec > best["sec"]:
            best = {"start": start, "end": end, "sec": sec}
    return best


def _report_stats(pid, since, now=None):
    """Rueckblick eines Profils, ausschliesslich aus der Historie. Read-only, kein Netz."""
    now = now or time.time()
    w = _rows_since(_read_watch(pid), since)
    m = _rows_since(_read_metrics(pid, limit=METRICS_MAX), since)
    checks = {}
    for ev in sorted({e for r in w for e in (r.get("c") or {})}):
        vals = [(r.get("c") or {}).get(ev) for r in w]
        checks[ev] = {"ok": sum(1 for v in vals if v == "ok"),
                      "bad": sum(1 for v in vals if v == "bad"),
                      "pct": _uptime_pct(w, ev)}
    st = {"runs": len(w), "checks": checks,
          "outage": _longest_outage(w, "downtime", now),
          "docs": None, "inbox": None, "lat": None}
    if m:
        first, last = m[0], m[-1]
        if first.get("total") is not None and last.get("total") is not None:
            st["docs"] = {"first": first["total"], "last": last["total"],
                          "delta": last["total"] - first["total"]}
        if first.get("inbox") is not None and last.get("inbox") is not None:
            st["inbox"] = {"first": first["inbox"], "last": last["inbox"]}
        lats = [r["lat"] for r in m if r.get("lat") is not None]
        if lats:
            st["lat"] = {"med": int(_median(lats)), "max": max(lats)}
    return st


def _report_text(days, st, now=None):
    """Report als Klartext — geht so an Pushover/ntfy/E-Mail und in die UI-Vorschau."""
    now = now or time.time()
    L = ["Rückblick über %d Tage · %d Prüfläufe" % (days, st["runs"]), ""]
    d = st["checks"].get("downtime")
    if d:
        if d["pct"] is None:
            L.append("Erreichbarkeit: keine verwertbaren Läufe")
        else:
            L.append("Erreichbarkeit: %s %% (%d ok, %d auffällig)"
                     % (("%g" % d["pct"]), d["ok"], d["bad"]))
    o = st["outage"]
    if o:
        when = datetime.fromtimestamp(o["start"]).strftime("%d.%m. %H:%M")
        L.append("Längster Ausfall: %s (ab %s%s)"
                 % (_fmt_dur(o["sec"]), when, "" if o["end"] else ", noch offen"))
    elif d and d["ok"]:
        L.append("Kein Ausfall im Zeitraum.")
    for ev, c in st["checks"].items():
        if ev == "downtime":
            continue
        label = _WATCH_LABELS.get(ev, ev)
        L.append("%s: %s" % (label, "durchgehend ok" if not c["bad"]
                             else "%d auffällige Läufe" % c["bad"]))
    if st["docs"]:
        dd = st["docs"]
        pro = dd["delta"] / days
        L += ["", "Dokumente: %s (%+d im Zeitraum, Ø %.1f/Tag)"
              % (_fmt_int(dd["last"]), dd["delta"], pro)]
    if st["inbox"]:
        i = st["inbox"]
        trend = ("unverändert" if i["last"] == i["first"]
                 else ("abgebaut, vorher %s" % _fmt_int(i["first"]) if i["last"] < i["first"]
                       else "gewachsen, vorher %s" % _fmt_int(i["first"])))
        L.append("Posteingang: %s (%s)" % (_fmt_int(i["last"]), trend))
    if st["lat"]:
        L.append("Antwortzeit: Ø %d ms, max %d ms" % (st["lat"]["med"], st["lat"]["max"]))
    return "\n".join(L)


def _report_profiles(period, now=None):
    """[(pid, profil, stats)] fuer alle Profile mit Instanz und aufgezeichneter Historie.
    Profile ohne einen einzigen Lauf im Zeitraum fallen raus — ein Report voller Striche
    ist keine Information, sondern Rauschen."""
    now = now or time.time()
    days = _report_days(period)
    since = now - days * 86400
    try:
        profs = load_profiles()
    except (OSError, ValueError):
        return []
    out = []
    for pid, p in sorted(profs.items(), key=lambda kv: (kv[1].get("name") or kv[0]).lower()):
        if not p.get("paperless_url"):
            continue
        st = _report_stats(pid, since, now)
        if not st["runs"]:
            continue
        out.append((pid, p, st))
    return out


def _report_preview(period, now=None):
    """Vorschau fuer die UI: exakt der Text, der auch versendet wuerde."""
    days = _report_days(period)
    return [{"name": p.get("name") or pid, "body": _report_text(days, st, now),
             "channels": _has_channel(p)}
            for pid, p, st in _report_profiles(period, now)]


def _has_channel(p):
    n = _notif_of(p)
    return bool(n["pushover"]["enabled"] or n["ntfy"]["enabled"] or n["email"]["enabled"])


def _send_report(period=None):
    """Report je Profil an die aktiven Kanaele und/oder den Webhook (Ereignis 'report').
    Rueckgabe: Anzahl der bedienten Profile."""
    wc = _watcher_cfg()
    period = period or wc["report_period"]
    days = _report_days(period)
    hook = _webhook_wants("report")
    sent = 0
    for pid, p, st in _report_profiles(period):
        chan = _has_channel(p)
        if not (chan or hook):
            continue
        name = p.get("name") or pid
        body = _report_text(days, st)
        if chan:
            _dispatch_notification(p, "report", "Paperless-Report (%d Tage): %s"
                                   % (days, name), body)
        if hook:
            _fire_webhook("report", "report", name, "info", body)
        sent += 1
    if sent:
        _log_activity("watcher", "%s-Report gesendet (%d Profil(e))"
                      % (_REPORT_PERIODS[period][0], sent))
    return sent


def _report_due(lt, wc, last):
    """Ist zur lokalen Zeit lt ein Report faellig? last = 'YYYY-MM-DD' des letzten.
    Reine Funktion — so ist die Taktung ohne Warten pruefbar."""
    if not wc["report_enabled"] or lt.hour != wc["report_hour"]:
        return False
    if lt.strftime("%Y-%m-%d") == last:
        return False
    if wc["report_period"] == "week":
        return lt.weekday() == wc["report_weekday"]
    return lt.day == wc["report_day"]


def _ping_heartbeat(url):
    """Dead-Man's-Switch: externen Ping-Dienst (z. B. healthchecks.io) anstossen.
    Bleibt der Ping aus (Portal tot), alarmiert der Dienst — deckt genau den Fall ab,
    in dem das Portal selbst nicht mehr ueber Downtime warnen kann."""
    try:
        requests.get(url, timeout=8)
        _watcher_state["last_heartbeat"] = time.time()
    except requests.RequestException:
        pass


# Was den Webhook ausloesen kann. Bewusst NICHT dabei: 'update' und 'error' — die stehen
# zwar in _NOTIFY_EVENTS, werden aber nirgends gefeuert ('update' nutzt nur der Kanal-
# Testknopf als Traeger). Sie hier anzubieten hiesse, Ereignisse zu versprechen, die nie
# kommen.
_WEBHOOK_KINDS = [
    ("alarm", "Alarm — ein Check wird auffällig"),
    ("reminder", "Erinnerung — der Alarm bleibt offen"),
    ("recovery", "Entwarnung — der Check ist wieder in Ordnung"),
    ("digest", "Täglicher Digest"),
    ("report", "Wochen-/Monatsreport"),
]
# Standard = das Verhalten bis v1.8.0 (nur Alarm + Entwarnung). Bestehende config.json
# ohne 'kinds' verhaelt sich damit unveraendert.
_WEBHOOK_KIND_DEFAULTS = {"alarm": True, "reminder": False, "recovery": True,
                          "digest": False, "report": False}


def _webhook_cfg():
    """Globale Webhook-/n8n-Konfiguration aus config.json['webhook']."""
    try:
        w = load_config().get("webhook") or {}
    except (OSError, ValueError):
        w = {}
    k = w.get("kinds") if isinstance(w.get("kinds"), dict) else {}
    return {"enabled": bool(w.get("enabled", False)),
            "url": (w.get("url") or "").strip(),
            "kinds": {n: bool(k.get(n, d)) for n, d in _WEBHOOK_KIND_DEFAULTS.items()}}


def _webhook_wants(kind, wc=None):
    """Soll dieses Ereignis ueber den Webhook? 'test' immer (der Knopf soll immer feuern)."""
    wc = wc or _webhook_cfg()
    if not (wc["enabled"] and wc["url"]):
        return False
    return wc["kinds"].get(kind, True)


def _fire_webhook(kind, event, profile, status, detail, wc=None):
    """Bei Ereignissen ein JSON an die konfigurierte Webhook-/n8n-URL POSTen.
    Fire-and-forget, read-only gegenueber der Instanz.
    ``kind`` sagt, WAS passiert ist (alarm/reminder/recovery/digest/report/test), ``event``
    WELCHER Check es betrifft. Beides ist noetig: Alarm und Erinnerung haben denselben
    status 'bad' — ohne kind koennte die Gegenstelle sie nicht auseinanderhalten."""
    wc = wc or _webhook_cfg()
    if not (wc["enabled"] and wc["url"]):
        return False, "Webhook nicht konfiguriert"
    if not wc["kinds"].get(kind, True):
        return False, "Ereignisart '%s' ist für den Webhook abgewählt" % kind
    payload = {
        "source": "paperless-generator-portal",
        "portal_version": PORTAL_VERSION,
        "kind": kind,
        "event": event, "profile": profile, "status": status, "detail": detail,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        r = requests.post(wc["url"], json=payload, timeout=10)
        body = " ".join((r.text or "").split())[:200]   # Antwort-Body (gekuerzt) fuer Diagnose
        ok = r.status_code < 400
        out = "HTTP %d%s" % (r.status_code, (" · " + body) if body else "")
        _watcher_state["last_webhook"] = {"ts": time.time(), "ok": ok, "detail": out,
                                          "event": event, "kind": kind}
        _log_activity("webhook", "Webhook %s (Ereignis: %s)" % ("gesendet" if ok else "abgelehnt", event),
                      level=("ok" if ok else "err"), detail="%s\n%s" % (wc["url"], out))
        return ok, out
    except requests.RequestException as exc:
        _watcher_state["last_webhook"] = {"ts": time.time(), "ok": False, "detail": str(exc),
                                          "event": event, "kind": kind}
        _log_activity("webhook", "Webhook nicht erreichbar (Ereignis: %s)" % event,
                      level="err", detail="%s\n%s" % (wc["url"], exc))
        return False, str(exc)


def _watcher_loop():
    """Hintergrund-Schleife: alle interval_min Minuten ein Prueflauf, wenn aktiviert.
    Zusaetzlich: Heartbeat je Zyklus + Tages-Digest zur eingestellten Stunde.
    Laeuft nur im Prozess mit der Dateisperre; faengt alle Fehler ab (Thread stirbt nie)."""
    poll = 20  # Sekunden — feiner Takt, damit Enable/Intervall-Aenderungen zeitnah greifen
    while True:
        dirty = False
        try:
            wc = _watcher_cfg()
            if wc["enabled"]:
                now = time.time()
                if not _watcher_state.get("next_run") or now >= _watcher_state["next_run"]:
                    _run_watch_cycle(wc, dispatch=True)  # spiegelt selbst
                    _watcher_state["next_run"] = time.time() + wc["interval_min"] * 60
                    if wc["heartbeat_url"]:
                        _ping_heartbeat(wc["heartbeat_url"])
                    dirty = True
                lt = datetime.now()
                today = lt.strftime("%Y-%m-%d")
                if wc["digest_enabled"]:
                    if lt.hour == wc["digest_hour"] and _watcher_state.get("last_digest") != today:
                        _watcher_state["last_digest"] = today
                        _send_digest()
                        dirty = True
                if _report_due(lt, wc, _watcher_state.get("last_report")):
                    # Datum ZUERST setzen: scheitert der Versand, wird nicht in derselben
                    # Stunde bei jedem Poll (20 s) erneut gefeuert.
                    _watcher_state["last_report"] = today
                    _send_report(wc["report_period"])
                    dirty = True
            elif _watcher_state.get("next_run") is not None:
                _watcher_state["next_run"] = None
                dirty = True
            if _watcher_state.get("last_error") is not None:
                _watcher_state["last_error"] = None
                dirty = True
        except Exception as exc:  # Schleife muss alles ueberleben
            _watcher_state["last_error"] = str(exc)
            dirty = True
        if dirty:
            # next_run/Heartbeat/Digest/Fehler aendern sich auch ausserhalb des Zyklus —
            # ohne Spiegeln zeigt der Nicht-Owner-Worker einen veralteten Stand.
            _save_watch_state()
        time.sleep(poll)


def _acquire_watcher_lock():
    """Exklusive, nicht-blockierende POSIX-Sperre — nur EIN Worker gewinnt sie.
    Ohne fcntl (Windows/Tests) wird ohne Sperre gestartet (dort laeuft nur ein Prozess)."""
    global _watcher_lock_fh
    if fcntl is None:
        return True
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        fh = open(WATCHER_LOCK_PATH, "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(str(os.getpid()))
        fh.flush()
        _watcher_lock_fh = fh  # Referenz halten -> Sperre bleibt fuer die Prozesslebensdauer
        return True
    except OSError:
        return False


def _restore_watch_state():
    """Gespiegelten Zustand nach einem Neustart zurueck in den Speicher holen.

    Ohne das startet JEDER Neustart (also jeder Deploy) bei null, und zwar mit Folgen:
      * last_digest/last_report sind None -> faellt der Neustart in die eingestellte
        Stunde, gehen Digest und Report ein ZWEITES Mal raus.
      * alerts_active/alert_meta sind leer -> ein laengst gemeldeter Alarm gilt wieder als
        neu und wird erneut gemeldet, Streaks und Erinnerungstakt fangen von vorn an.
        Genau das soll die Eskalation aus Z2 ja verhindern.
    Bewusst NICHT uebernommen: 'next_run' (nach einem Neustart soll sofort geprueft werden,
    nicht bis zum alten Termin gewartet) und 'last_error' (ein alter Fehler darf nicht
    wieder aufleben; die Schleife setzt ihn ohnehin je Runde neu)."""
    try:
        with open(WATCH_STATE_PATH, encoding="utf-8") as fh:
            old = json.load(fh)
    except (OSError, ValueError):
        return
    for k in ("last_run", "last_digest", "last_report", "last_heartbeat",
              "last_metrics", "last_webhook", "results"):
        if old.get(k) is not None:
            _watcher_state[k] = old[k]
    _watcher_state["alerts_active"] = {tuple(k.split("|", 1))
                                       for k in (old.get("alerts_active") or [])}
    _watcher_state["alert_meta"] = old.get("alert_meta") or {}


def _ensure_watcher():
    """Startet den Waechter-Thread einmal pro Prozess — aber nur, wenn dieser Prozess die
    Sperre gewinnt. Traege ueber before_request angestossen (nach dem Fork der Worker)."""
    global _watcher_started
    if _watcher_started:
        return
    _watcher_started = True  # egal wie es ausgeht: nicht erneut versuchen
    if os.environ.get("PORTAL_WATCHER", "1") != "1":
        return  # in Tests deaktivierbar
    # VOR der Sperre und fuer BEIDE Worker: ein manuelles "Jetzt pruefen" kann im
    # Nicht-Owner landen, und der wuerde sonst mit leeren Streaks rechnen und den Spiegel
    # anschliessend mit ebendieser Leere ueberschreiben.
    _restore_watch_state()
    if not _acquire_watcher_lock():
        return  # anderer Worker fuehrt die Schleife
    _watcher_state["owner"] = True
    threading.Thread(target=_watcher_loop, name="paperless-watcher", daemon=True).start()


@app.before_request
def _watcher_boot():
    # Traeger Start nach dem Worker-Fork (unter --preload laeuft Modul-Code nur im Master).
    # Der Docker-Healthcheck trifft /healthz alle 30 s -> Waechter startet auch ohne Login.
    _ensure_watcher()
    return None


def _fmt_rel_ts(ts):
    """Unix-ts -> 'vor X' (Vergangenheit) / 'in X' (Zukunft) / '—'.
    Zukunft muss mit: 'naechster Lauf' liegt immer voraus und stand sonst als
    'vor -3540 s' in der Statuszeile."""
    if not ts:
        return "—"
    delta = int(time.time() - ts)
    ahead = delta < 0
    d = abs(delta)
    if d >= 86400:
        return datetime.fromtimestamp(ts).strftime("%d.%m. %H:%M")
    if d < 60:
        s = "%d s" % d
    elif d < 3600:
        s = "%d min" % (d // 60)
    else:
        s = "%d h" % (d // 3600)
    return ("in " + s) if ahead else ("vor " + s)


def _alert_board(st, wc):
    """Eskalations-Uebersicht fuer die UI: was ist offen (seit wann, wievielte Erinnerung,
    naechste faellig) und was ist erst in Beobachtung (Streak unter der Alarmschwelle)."""
    labels = dict(_NOTIFY_EVENTS)
    try:
        profs = load_profiles()
    except (OSError, ValueError):
        profs = {}
    meta = st.get("alert_meta") or {}
    active = st.get("alerts_active") or ()
    now = time.time()
    open_, watching = [], []
    for pid, ev in sorted(active):
        m = meta.get("%s|%s" % (pid, ev)) or {}
        nxt = "—"
        if wc["repeat_min"] and m.get("last"):
            due = m["last"] + _repeat_gap_min(wc, m.get("reps") or 0) * 60
            nxt = ("in " + _fmt_dur(due - now)) if due > now else "fällig"
        open_.append({"profile": (profs.get(pid) or {}).get("name") or pid,
                      "label": labels.get(ev, ev), "reps": m.get("reps") or 0, "next": nxt,
                      "since": _fmt_dur(now - m["since"]) if m.get("since") else "—"})
    for k, m in sorted(meta.items()):
        pid, _, ev = k.partition("|")
        if (pid, ev) in active or not (m.get("fail") or 0):
            continue
        watching.append({"profile": (profs.get(pid) or {}).get("name") or pid,
                         "label": labels.get(ev, ev), "fail": m["fail"],
                         "need": wc["fail_threshold"]})
    return {"open": open_, "watching": watching}


# ─────────────────────────────────────────────────────────────────────────────
# PROMETHEUS /metrics (Z3) — Exposition im Text-Format 0.0.4.
# STRIKT read-only und OHNE Netz-Call je Scrape: gespeist wird ausschliesslich aus dem
# Waechter-Spiegel (watcher-state.json) und den JSONL-Historien. Prometheus scrapt im
# Sekundentakt — ein Scrape darf weder eine Paperless-Instanz noch GitHub anfassen,
# sonst prellt die Ueberwachung genau das System, das sie beobachten soll.
# ─────────────────────────────────────────────────────────────────────────────
_PROM_STATUS_VAL = {"ok": 1, "bad": 0}   # 'unknown' -> gar kein Sample: eine Luecke ist
# ehrlicher als eine 0, sonst zaehlt Grafana Aussetzer als Ausfall.


def _metrics_cfg():
    """Globale /metrics-Konfiguration aus config.json['metrics']."""
    try:
        m = load_config().get("metrics") or {}
    except (OSError, ValueError):
        m = {}
    return {"enabled": bool(m.get("enabled", False)), "token": (m.get("token") or "").strip()}


def _metrics_token(val):
    """Token auf harmlose Zeichen eindampfen. compare_digest wirft bei nicht-ASCII, und ein
    Umbruch im Header-Vergleich waere ohnehin kaputt. isalnum() allein reicht NICHT — das ist
    fuer 'ü' True; ein Token mit Umlaut haette den Endpunkt dauerhaft auf 401 gelegt."""
    return "".join(c for c in (val or "")
                   if c.isascii() and (c.isalnum() or c in "-_"))[:80]


def _metrics_auth_ok(tok):
    """Zugriff, wenn ein gueltiges Bearer-Token kommt (Prometheus) ODER die Sitzung
    eingeloggt ist (Mensch schaut im Browser nach). Vergleich zeitkonstant."""
    if session.get("logged_in"):
        return True
    hdr = request.headers.get("Authorization") or ""
    if hdr[:7].lower() != "bearer ":
        return False
    try:
        return secrets.compare_digest(hdr[7:].strip(), tok)
    except TypeError:      # nicht-ASCII im Header
        return False


def _prom_lbl(val):
    """Label-Wert escapen. Profilnamen sind frei waehlbar — ein \" oder \\ darin wuerde
    die Exposition sonst zerlegen."""
    return str(val).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _prom_num(val):
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, int):
        return str(val)
    return repr(round(float(val), 4))


def _prom_block(out, name, mtype, help_, samples):
    """Eine Metrik anhaengen: HELP/TYPE + Samples [(labels, wert)]. Ohne Samples faellt der
    Block ganz weg, statt ein leeres TYPE zu hinterlassen."""
    if not samples:
        return
    out.append("# HELP %s %s" % (name, help_))
    out.append("# TYPE %s %s" % (name, mtype))
    for labels, val in samples:
        lbl = ("{%s}" % ",".join('%s="%s"' % (k, _prom_lbl(v)) for k, v in labels)) if labels else ""
        out.append("%s%s %s" % (name, lbl, _prom_num(val)))


def _prom_update_cache():
    """Update-Stand NUR aus dem Cache lesen — check_for_update() wuerde bei abgelaufener
    TTL GitHub fragen, und das darf ein Scrape nicht ausloesen."""
    try:
        with open(UPDATE_CHECK_CACHE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


_watch_stats_cache = {}   # pid -> ((mtime, size), stats) — siehe _watch_stats


def _watch_stats(pid):
    """Uptime-Anteil + letzter Schlecht-Lauf je Event aus der JSONL-Historie.
    Mit mtime/size-Cache: Prometheus scrapt alle paar Sekunden, die Datei aendert sich aber
    nur pro Prueflauf (Minuten bis Stunden) — ohne Cache wuerden je Scrape bis zu WATCH_MAX
    Zeilen pro Profil neu geparst."""
    path = _watch_path(pid)
    try:
        stt = os.stat(path)
        key = (stt.st_mtime, stt.st_size)
    except OSError:
        return {"ratio": {}, "last_bad": {}, "runs": 0}
    hit = _watch_stats_cache.get(pid)
    if hit and hit[0] == key:
        return hit[1]
    rows = _read_watch(pid)
    stats = {"ratio": {}, "last_bad": {}, "runs": len(rows)}
    for ev in {e for r in rows for e in (r.get("c") or {})}:
        pct = _uptime_pct(rows, ev)
        if pct is not None:
            stats["ratio"][ev] = pct / 100.0
        bad = _last_bad_ts(rows, ev)
        if bad:
            stats["last_bad"][ev] = int(bad)
    _watch_stats_cache[pid] = (key, stats)
    return stats


def _build_metrics():
    """Exposition zusammenbauen. Quellen ausschliesslich lokal: watcher-state.json (Spiegel),
    history-watch/*.jsonl, history-metrics/*.jsonl, profiles.json, config.json."""
    t0 = time.time()
    st = _load_watch_state()
    wc = _watcher_cfg()
    try:
        profs = load_profiles()
    except (OSError, ValueError):
        profs = {}
    results = st.get("results") or {}
    meta = st.get("alert_meta") or {}
    active = set(st.get("alerts_active") or ())
    out = []

    bs = _build_stamp() or {}
    _prom_block(out, "paperless_portal_info", "gauge",
                "Portal-Version und laufender Commit (Wert immer 1).",
                [([("version", PORTAL_VERSION), ("build", bs.get("sha") or ""),
                   ("build_date", bs.get("date") or "")], 1)])

    upd = _prom_update_cache()
    if upd and upd.get("latest"):
        _prom_block(out, "paperless_portal_update_available", "gauge",
                    "1 = auf GitHub liegt eine neuere Portal-Version (aus dem Cache).",
                    [([("latest", upd["latest"])],
                      1 if _ver_tuple(upd["latest"]) > _ver_tuple(PORTAL_VERSION) else 0)])

    _prom_block(out, "paperless_portal_watcher_enabled", "gauge",
                "1 = Waechter ist eingeschaltet.", [(None, wc["enabled"])])
    _prom_block(out, "paperless_portal_watcher_interval_seconds", "gauge",
                "Eingestellter Abstand zwischen zwei Prueflaeufen.",
                [(None, wc["interval_min"] * 60)])
    for key, name, help_ in (
        ("last_run", "paperless_portal_watcher_last_run_timestamp_seconds",
         "Zeitpunkt des letzten Prueflaufs."),
        ("next_run", "paperless_portal_watcher_next_run_timestamp_seconds",
         "Zeitpunkt des naechsten faelligen Prueflaufs."),
        ("last_heartbeat", "paperless_portal_watcher_last_heartbeat_timestamp_seconds",
         "Zeitpunkt des letzten Heartbeat-Pings."),
    ):
        if st.get(key):
            _prom_block(out, name, "gauge", help_, [(None, int(st[key]))])
    _prom_block(out, "paperless_portal_watcher_error", "gauge",
                "1 = der letzte Waechter-Zyklus endete mit einem Fehler.",
                [(None, bool(st.get("last_error")))])
    _prom_block(out, "paperless_portal_alerts_active", "gauge",
                "Anzahl aktuell gemeldeter Auffaelligkeiten ueber alle Profile.",
                [(None, len(active))])
    _prom_block(out, "paperless_portal_profiles_total", "gauge",
                "Angelegte Profile.", [(None, len(profs))])
    _prom_block(out, "paperless_portal_profiles_watched", "gauge",
                "Profile, die der Waechter tatsaechlich prueft (Instanz hinterlegt + aktiv).",
                [(None, sum(1 for p in profs.values()
                            if p.get("paperless_url") and _profile_watch(p, wc)["enabled"]))])

    up, chk_ok, ratio, last_bad, runs, check_ts = [], [], [], [], [], []
    streak, al_act, al_since, al_reps = [], [], [], []
    docs, inbox, untyped, uncorr, lat, sample_ts = [], [], [], [], [], []
    for pid in sorted(set(results) | set(profs)):
        name = ((results.get(pid) or {}).get("name")
                or (profs.get(pid) or {}).get("name") or pid)
        base = [("profile", name), ("id", pid)]

        r = results.get(pid) or {}
        if r.get("ts"):
            check_ts.append((base, int(r["ts"])))
        for c in r.get("checks") or []:
            v = _PROM_STATUS_VAL.get(c.get("status"))
            if v is None:
                continue
            chk_ok.append((base + [("check", c.get("event") or "?")], v))
            if c.get("event") == "downtime":
                up.append((base, v))

        ws = _watch_stats(pid)
        if ws["runs"]:
            runs.append((base, ws["runs"]))
        for ev, val in sorted(ws["ratio"].items()):
            ratio.append((base + [("check", ev)], val))
        for ev, ts in sorted(ws["last_bad"].items()):
            last_bad.append((base + [("check", ev)], ts))

        for ev in sorted({k.partition("|")[2] for k in meta if k.partition("|")[0] == pid}):
            m = meta.get("%s|%s" % (pid, ev)) or {}
            lb = base + [("check", ev)]
            streak.append((lb, int(m.get("fail") or 0)))
            on = (pid, ev) in active
            al_act.append((lb, on))
            if on and m.get("since"):
                al_since.append((lb, int(m["since"])))
            if on:
                al_reps.append((lb, int(m.get("reps") or 0)))

        mrows = _read_metrics(pid, limit=1)
        if mrows:
            mr = mrows[-1]
            if mr.get("ts"):
                sample_ts.append((base, int(mr["ts"])))
            for val, bucket in ((mr.get("total"), docs), (mr.get("inbox"), inbox),
                                (mr.get("no_type"), untyped), (mr.get("no_corr"), uncorr)):
                if val is not None:
                    bucket.append((base, int(val)))
            if mr.get("lat") is not None:
                lat.append((base, int(mr["lat"]) / 1000.0))

    _prom_block(out, "paperless_portal_instance_up", "gauge",
                "1 = Instanz war beim letzten Prueflauf erreichbar und das Token gueltig.", up)
    _prom_block(out, "paperless_portal_check_ok", "gauge",
                "Ergebnis des letzten Prueflaufs je Check: 1 = ok, 0 = auffaellig. "
                "Ein unentschiedener Check ('unknown') liefert bewusst kein Sample.", chk_ok)
    _prom_block(out, "paperless_portal_check_last_run_timestamp_seconds", "gauge",
                "Zeitpunkt des letzten Prueflaufs fuer dieses Profil.", check_ts)
    _prom_block(out, "paperless_portal_check_uptime_ratio", "gauge",
                "Anteil guter Laeufe an allen entschiedenen Laeufen der Portal-Historie "
                "(0..1). Nicht aus Prometheus-Daten gerechnet, sondern aus der laengeren "
                "Historie des Portals.", ratio)
    _prom_block(out, "paperless_portal_check_last_bad_timestamp_seconds", "gauge",
                "Zeitpunkt des juengsten Laufs, in dem der Check auffaellig war.", last_bad)
    _prom_block(out, "paperless_portal_check_runs", "gauge",
                "Aufgezeichnete Prueflaeufe in der Historie dieses Profils.", runs)
    _prom_block(out, "paperless_portal_fail_streak", "gauge",
                "Schlechte Laeufe in Folge. Ab paperless_portal_alert_threshold wird "
                "gemeldet — darunter ist der Check nur in Beobachtung.", streak)
    _prom_block(out, "paperless_portal_alert_threshold", "gauge",
                "Schwelle, ab der ein Fehl-Streak zum Alarm wird.",
                [(None, wc["fail_threshold"])])
    _prom_block(out, "paperless_portal_alert_active", "gauge",
                "1 = fuer dieses Profil/Check ist gerade ein Alarm offen.", al_act)
    _prom_block(out, "paperless_portal_alert_since_timestamp_seconds", "gauge",
                "Beginn des offenen Alarms (erster schlechter Lauf des Streaks).", al_since)
    _prom_block(out, "paperless_portal_alert_repeats", "gauge",
                "Bereits verschickte Erinnerungen zum offenen Alarm.", al_reps)
    _prom_block(out, "paperless_portal_documents", "gauge",
                "Dokumente in der Instanz (aus der Kennzahl-Historie, ~1x/Stunde erhoben).", docs)
    _prom_block(out, "paperless_portal_documents_inbox", "gauge",
                "Dokumente im Posteingang.", inbox)
    _prom_block(out, "paperless_portal_documents_without_type", "gauge",
                "Dokumente ohne Dokumenttyp.", untyped)
    _prom_block(out, "paperless_portal_documents_without_correspondent", "gauge",
                "Dokumente ohne Korrespondent.", uncorr)
    _prom_block(out, "paperless_portal_api_latency_seconds", "gauge",
                "Antwortzeit der Instanz bei der letzten Kennzahl-Erfassung.", lat)
    _prom_block(out, "paperless_portal_documents_timestamp_seconds", "gauge",
                "Zeitpunkt der Kennzahl-Erfassung. Die Dokument-Zaehler sind so alt wie "
                "dieser Wert — sie werden gedrosselt erhoben, nicht je Scrape.", sample_ts)
    _prom_block(out, "paperless_portal_scrape_duration_seconds", "gauge",
                "Dauer des Zusammenbauens dieser Exposition.", [(None, time.time() - t0)])
    return "\n".join(out) + "\n"


@app.route("/metrics")
def metrics_endpoint():
    """Prometheus-Endpunkt. Bewusst OHNE Login-Gate (require_login wuerde Prometheus mit 302
    auf /login schicken) — stattdessen Bearer-Token. Abgeschaltet -> 404 statt 401, damit der
    Endpunkt seine Existenz nicht verraet."""
    mc = _metrics_cfg()
    if not (mc["enabled"] and mc["token"]):
        return Response("Not Found\n", status=404, mimetype="text/plain; charset=utf-8")
    if not _metrics_auth_ok(mc["token"]):
        return Response("Unauthorized\n", status=401, mimetype="text/plain; charset=utf-8",
                        headers={"WWW-Authenticate": 'Bearer realm="metrics"'})
    return Response(_build_metrics(), mimetype="text/plain; version=0.0.4; charset=utf-8")


@app.route("/verwaltung/waechter", methods=["GET", "POST"])
def waechter():
    if request.method == "POST":
        action = request.form.get("action", "save")
        f = request.form
        w = {
            "enabled": bool(f.get("enabled")),
            "interval_min": max(5, _watch_int(f.get("interval_min"), 60)),
            "checks": {k: bool(f.get("chk_" + k)) for k in _WATCHER_DEFAULTS["checks"]},
            "asn_gap_threshold": max(1, _watch_int(f.get("asn_gap_threshold"), 1)),
            "digest_enabled": bool(f.get("digest_enabled")),
            "digest_hour": max(0, min(23, _watch_int(f.get("digest_hour"), 8))),
            "heartbeat_url": (f.get("heartbeat_url", "") or "").strip(),
            "fail_threshold": max(1, min(10, _watch_int(f.get("fail_threshold"), 2))),
            "ok_threshold": max(1, min(10, _watch_int(f.get("ok_threshold"), 1))),
            "repeat_min": max(0, min(_ESC_MAX_GAP_MIN, _watch_int(f.get("repeat_min"), 0))),
            "repeat_backoff": bool(f.get("repeat_backoff")),
            "escalate_after": max(0, min(20, _watch_int(f.get("escalate_after"), 3))),
            "recovery_notify": bool(f.get("recovery_notify")),
            "report_enabled": bool(f.get("report_enabled")),
            "report_period": (f.get("report_period") if f.get("report_period") in _REPORT_PERIODS
                              else "week"),
            "report_weekday": max(0, min(6, _watch_int(f.get("report_weekday"), 0))),
            "report_day": max(1, min(_REPORT_MAX_DAY, _watch_int(f.get("report_day"), 1))),
            "report_hour": max(0, min(23, _watch_int(f.get("report_hour"), 8))),
        }
        cfg = load_config()
        cfg["watcher"] = w
        cfg["webhook"] = {"enabled": bool(f.get("webhook_enabled")),
                          "url": (f.get("webhook_url", "") or "").strip(),
                          "kinds": {k: bool(f.get("wh_" + k)) for k in _WEBHOOK_KIND_DEFAULTS}}
        # /metrics: Token wird erzeugt, nicht getippt — beim Einschalten ohne Token gibt es
        # sonst einen aktiven Endpunkt ohne Schutz (der Route-Guard faengt das zwar mit 404
        # ab, aber der Nutzer stuende ratlos da).
        m_enabled = bool(f.get("metrics_enabled"))
        mtok = _metrics_token(f.get("metrics_token", ""))
        if action == "metrics_token_new" or (m_enabled and not mtok):
            mtok = secrets.token_urlsafe(32)
        cfg["metrics"] = {"enabled": m_enabled, "token": mtok}
        save_config(cfg)
        _watcher_state["next_run"] = None  # Aenderung sofort wirksam
        if action == "metrics_token_new":
            return redirect(url_for("verwaltung", tab="waechter",
                                    msg="Neues /metrics-Token erzeugt — Prometheus-Konfiguration anpassen."))
        if action == "webhook_now":
            ok, detail = _fire_webhook("test", "test", "Portal", "test",
                                       "Test-Webhook vom Paperless-Portal.")
            if ok:
                return redirect(url_for("verwaltung", tab="waechter", msg="Webhook gesendet (%s)." % detail))
            return redirect(url_for("verwaltung", tab="waechter", err="Webhook fehlgeschlagen: %s" % detail))
        if action == "run_now":
            _run_watch_cycle(_watcher_cfg(), dispatch=True)
            _log_activity("watcher", "Manueller Prüflauf")
            return redirect(url_for("verwaltung", tab="waechter", msg="Prüflauf ausgeführt — Ergebnis unten."))
        if action == "digest_now":
            _send_digest()
            return redirect(url_for("verwaltung", tab="waechter", msg="Digest an alle Profile mit aktivem Kanal gesendet."))
        if action == "report_now":
            n = _send_report(w["report_period"])
            if n:
                return redirect(url_for("verwaltung", tab="waechter",
                                        msg="Report an %d Profil(e) gesendet." % n))
            return redirect(url_for("verwaltung", tab="waechter",
                                    err="Kein Report gesendet — kein Profil hat sowohl "
                                        "aufgezeichnete Prüfläufe im Zeitraum als auch einen "
                                        "aktiven Benachrichtigungs-Kanal."))
        if action == "heartbeat_now":
            if w["heartbeat_url"]:
                _ping_heartbeat(w["heartbeat_url"])
                return redirect(url_for("verwaltung", tab="waechter", msg="Heartbeat-Ping gesendet."))
            return redirect(url_for("verwaltung", tab="waechter", err="Keine Heartbeat-URL gesetzt."))
        return redirect(url_for("verwaltung", tab="waechter", msg="Wächter-Einstellungen gespeichert."))

    wc = _watcher_cfg()
    st = _load_watch_state()  # Owner: eigener Speicher; sonst der gespiegelte Stand
    results = []
    for pid, r in (st.get("results") or {}).items():
        results.append({"name": r.get("name") or pid, "ts": _fmt_rel_ts(r.get("ts")),
                        "checks": r.get("checks", [])})
    results.sort(key=lambda x: x["name"].lower())
    status = {
        "owner": bool(st.get("owner")),
        "last_run": _fmt_rel_ts(st.get("last_run")),
        "next_run": (_fmt_rel_ts(st["next_run"]) if st.get("next_run") else "—"),
        "active_alerts": len(st.get("alerts_active") or ()),
        "error": st.get("last_error"),
        "last_heartbeat": _fmt_rel_ts(st.get("last_heartbeat")),
        "last_digest": st.get("last_digest") or "—",
        "last_report": st.get("last_report") or "—",
    }
    lw = st.get("last_webhook")
    webhook_last = None
    if lw:
        webhook_last = {"ok": lw.get("ok"), "detail": lw.get("detail"),
                        "event": lw.get("event"), "kind": lw.get("kind"),
                        "ts": _fmt_rel_ts(lw.get("ts"))}
    return render_template(
        "waechter.html", w=wc, wh=_webhook_cfg(), mc=_metrics_cfg(), events=_NOTIFY_EVENTS,
        results=results, status=status, webhook_last=webhook_last,
        board=_alert_board(st, wc), weekdays=_WEEKDAYS, wh_kinds=_WEBHOOK_KINDS,
        portal_version=PORTAL_VERSION,
        report_preview=_report_preview(wc["report_period"]),
        report_days=_report_days(wc["report_period"]),
        hist=_watch_history_cards(request.args.get("range", "30d")),
        hrng=_watch_range(request.args.get("range", "30d")),
        msg=request.args.get("msg"), err=request.args.get("err"))


_WATCH_LABELS = {"downtime": "Erreichbarkeit", "drift": "Konfig-Drift",
                 "task_fail": "Verarbeitung", "asn_gap": "ASN-Lücken",
                 "duplicate": "Duplikate"}


def _watch_range(rng):
    return rng if rng in ("7d", "30d", "all") else "30d"


def _watch_history_cards(rng):
    """Je Profil: Uptime + Statusband pro Event aus der persistierten Waechter-Historie."""
    rng = _watch_range(rng)
    cutoff = {"7d": time.time() - 7 * 86400, "30d": time.time() - 30 * 86400}.get(rng)
    try:
        profs = load_profiles()
    except (OSError, ValueError):
        return []
    cards = []
    for pid, p in profs.items():
        rows = [r for r in _read_watch(pid) if cutoff is None or r.get("ts", 0) >= cutoff]
        if not rows:
            continue
        bands = []
        for ev, label in _WATCH_LABELS.items():
            svg = _timeline_svg(rows, ev)
            if not svg:
                continue  # dieser Check lief im Zeitraum nie
            bad_ts = _last_bad_ts(rows, ev)
            bands.append({"label": label, "svg": svg, "pct": _uptime_pct(rows, ev),
                          "last_bad": _fmt_rel_ts(bad_ts) if bad_ts else None})
        if not bands:
            continue
        cards.append({"name": p.get("name") or pid, "runs": len(rows),
                      "since": _fmt_rel_ts(rows[0].get("ts")),
                      "uptime": _uptime_pct(rows, "downtime"), "bands": bands})
    cards.sort(key=lambda c: c["name"].lower())
    return cards


def _line_svg(values, color="#3b82f6", w=440, h=110, pad=16):
    """Kompaktes Inline-SVG-Liniendiagramm aus einer Zahlenreihe (kein externes JS/CDN)."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    step = (w - 2 * pad) / (len(vals) - 1)
    pts = []
    for i, v in enumerate(vals):
        x = pad + i * step
        y = h - pad - (v - lo) / rng * (h - 2 * pad)
        pts.append("%.1f,%.1f" % (x, y))
    last = pts[-1].split(",")
    return (
        '<svg viewBox="0 0 %d %d" style="width:100%%;height:auto;display:block" '
        'xmlns="http://www.w3.org/2000/svg">' % (w, h)
        + '<polyline fill="none" stroke="%s" stroke-width="2" stroke-linejoin="round" '
          'stroke-linecap="round" points="%s"/>' % (color, " ".join(pts))
        + '<circle cx="%s" cy="%s" r="3" fill="%s"/></svg>' % (last[0], last[1], color)
    )


def _multi_line_svg(series, w=440, h=110, pad=16):
    """Mehrere Zahlenreihen in EIN Inline-SVG mit gemeinsamer Y-Skala (kein externes JS/CDN).
    series = [{"label","color","values"}]. None-Werte werden uebersprungen. Gibt None zurueck,
    wenn keine Serie mindestens 2 Messpunkte hat."""
    drawn = [s for s in series if len([v for v in s["values"] if v is not None]) >= 2]
    if not drawn:
        return None
    allv = [v for s in drawn for v in s["values"] if v is not None]
    lo, hi = min(allv), max(allv)
    rng = (hi - lo) or 1
    n = max(len(s["values"]) for s in drawn)
    step = (w - 2 * pad) / (n - 1) if n > 1 else 0
    parts = ['<svg viewBox="0 0 %d %d" style="width:100%%;height:auto;display:block" '
             'xmlns="http://www.w3.org/2000/svg">' % (w, h)]
    for s in drawn:
        pts = []
        for i, v in enumerate(s["values"]):
            if v is None:
                continue
            x = pad + i * step
            y = h - pad - (v - lo) / rng * (h - 2 * pad)
            pts.append("%.1f,%.1f" % (x, y))
        if len(pts) < 2:
            continue
        last = pts[-1].split(",")
        parts.append('<polyline fill="none" stroke="%s" stroke-width="2" stroke-linejoin="round" '
                     'stroke-linecap="round" points="%s"/>' % (s["color"], " ".join(pts)))
        parts.append('<circle cx="%s" cy="%s" r="3" fill="%s"/>' % (last[0], last[1], s["color"]))
    parts.append("</svg>")
    return "".join(parts)


_TL_COLORS = {"ok": "#22c55e", "bad": "#ef4444", "unknown": "#6b7280"}


def _timeline_svg(rows, event, w=440, h=22):
    """Statusband der letzten Prueflaeufe als Inline-SVG: ein Streifen je Lauf,
    gruen=ok / rot=bad / grau=unbekannt. Aeltester links, neuester rechts."""
    vals = [r.get("c", {}).get(event) for r in rows]
    vals = [v for v in vals if v]
    if not vals:
        return None
    n = len(vals)
    bw = w / n
    gap = 0.5 if bw > 2 else 0  # bei sehr vielen Laeufen luecklos zeichnen
    parts = ['<svg viewBox="0 0 %d %d" preserveAspectRatio="none" '
             'style="width:100%%;height:%dpx;display:block;border-radius:4px" '
             'xmlns="http://www.w3.org/2000/svg">' % (w, h, h)]
    for i, v in enumerate(vals):
        parts.append('<rect x="%.2f" y="0" width="%.2f" height="%d" fill="%s"/>'
                     % (i * bw, max(bw - gap, 0.4), h, _TL_COLORS.get(v, _TL_COLORS["unknown"])))
    parts.append("</svg>")
    return "".join(parts)


def _mark_backup():
    """Zeitstempel des letzten Voll-Backups persistieren (fuer die Backup-Alter-Ampel)."""
    try:
        with open(LAST_BACKUP_PATH, "w", encoding="utf-8") as fh:
            json.dump({"ts": int(time.time())}, fh)
    except OSError:
        pass


def _last_backup_ts():
    """Epoch des letzten Voll-Backups oder None."""
    try:
        with open(LAST_BACKUP_PATH, encoding="utf-8") as fh:
            return int(json.load(fh).get("ts"))
    except (OSError, ValueError, TypeError):
        return None


@app.route("/verwaltung/werkzeuge")
def werkzeuge():
    """Read-only Instanz-Werkzeuge fuer das AKTIVE Profil: Dokumente ohne Typ/Korrespondent/
    ASN, ASN-Luecken, moegliche Duplikate (Titel-Kollision) — jeweils mit Direktlink nach
    Paperless. Reine GETs, kein Schreibzugriff."""
    profs = load_profiles()
    p = profs.get(_active_id(), {})
    url = p.get("paperless_url")
    token = _dec(p.get("paperless_token"))
    d = {"name": p.get("name") or "", "url": (url or "").rstrip("/"), "online": False,
         "counts": None, "asn": None, "dups": None}
    if url and _test_paperless(url, token) == 200:
        d["online"] = True
        d["counts"] = {
            "no_type": _api_count(url, token, "documents/?document_type__isnull=true&page_size=1"),
            "no_corr": _api_count(url, token, "documents/?correspondent__isnull=true&page_size=1"),
            "no_asn": _api_count(url, token, "documents/?archive_serial_number__isnull=true&page_size=1"),
            "inbox": _api_count(url, token, "documents/?is_in_inbox=true&page_size=1"),
        }
        try:
            rows = _asn_pages(url, token, "archive_serial_number")
            asns = sorted({int(r["archive_serial_number"]) for r in rows if r.get("archive_serial_number")})
            if asns:
                have = set(asns)
                gaps = [i for i in range(asns[0], asns[-1] + 1) if i not in have]
                d["asn"] = {"min": asns[0], "max": asns[-1], "count": len(asns),
                            "gaps": gaps[:120], "gap_count": len(gaps), "next_free": asns[-1] + 1}
            else:
                d["asn"] = {"min": None, "max": None, "count": 0, "gaps": [], "gap_count": 0, "next_free": 1}
        except (requests.RequestException, ValueError, TypeError):
            d["asn"] = None
        try:
            rows = _asn_pages(url, token, "title")
            seen = {}
            for r in rows:
                t = (r.get("title") or "").strip()
                if t:
                    seen[t] = seen.get(t, 0) + 1
            dups = sorted([(t, n) for t, n in seen.items() if n > 1], key=lambda x: -x[1])
            d["dups"] = {"list": dups[:60], "count": len(dups)}
        except (requests.RequestException, ValueError, TypeError):
            d["dups"] = None
    return render_template("werkzeuge.html", d=d,
                           msg=request.args.get("msg"), err=request.args.get("err"))


@app.route("/verwaltung/trends", methods=["GET", "POST"])
def trends():
    if request.method == "POST":
        _record_metrics(force=True)
        _log_activity("trends", "Kennzahlen manuell erfasst")
        return redirect(url_for("verwaltung", tab="trends", msg="Kennzahlen erfasst."))
    profs = load_profiles()
    aid = _active_id()
    rng = request.args.get("range", "30d")
    if rng not in ("7d", "30d", "all"):
        rng = "30d"
    now = int(time.time())
    cutoff = {"7d": now - 7 * 86400, "30d": now - 30 * 86400}.get(rng)  # None = alles
    cards = []
    for pid, p in profs.items():
        allrows = _read_metrics(pid, limit=METRICS_MAX)
        rows = [r for r in allrows if cutoff is None or r.get("ts", 0) >= cutoff]
        if not rows and not p.get("paperless_url"):
            continue
        totals = [r.get("total") for r in rows]
        cleanup = [
            {"label": "Posteingang",   "color": "#f59e0b", "values": [r.get("inbox") for r in rows]},
            {"label": "ohne Typ",      "color": "#ef4444", "values": [r.get("no_type") for r in rows]},
            {"label": "ohne Korresp.", "color": "#a855f7", "values": [r.get("no_corr") for r in rows]},
        ]
        lats = [r.get("lat") for r in rows]
        delta = growth = None
        if len(rows) >= 2 and rows[0].get("total") is not None and rows[-1].get("total") is not None:
            delta = rows[-1]["total"] - rows[0]["total"]
            span = rows[-1].get("ts", 0) - rows[0].get("ts", 0)
            if span > 0:
                growth = round(delta / span * 7 * 86400, 1)  # hochgerechnet auf Dok./Woche
        cards.append({
            "name": p.get("name") or pid, "active": pid == aid, "count": len(rows),
            "svg": _line_svg(totals),
            "cleanup_svg": _multi_line_svg(cleanup),
            "cleanup_legend": [{"label": s["label"], "color": s["color"]} for s in cleanup],
            "lat_svg": _line_svg(lats, color="#22c55e"),
            "cur": rows[-1] if rows else None,
            "since": _fmt_rel_ts(rows[0]["ts"]) if rows else None,
            "delta": delta, "growth": growth,
        })
    cards.sort(key=lambda c: c["name"].lower())
    return render_template("trends.html", cards=cards, rng=rng,
                           msg=request.args.get("msg"), err=request.args.get("err"))


# ─────────────────────────────────────────────────────────────────────────────
# DRIFT „ANWENDEN" — sicher: Einzel-Haekchen, Passwort-Re-Auth, Instanz-Snapshot,
# nur create/update (nie Dokumente/DELETE), Undo, Protokoll.
# Kategorien mit klarem Namensfeld (Custom Fields bewusst NICHT — Typ-Mapping
# gehoert in den Generator-Direkt-Lauf).
# ─────────────────────────────────────────────────────────────────────────────
_APPLY_CATS = [
    ("Tags", "tags", "tags/"),
    ("Typen", "types", "document_types/"),
    ("Korrespondenten", "correspondents", "correspondents/"),
    ("Speicherpfade", "storagePaths", "storage_paths/"),
]


def _instance_names(url, token, endpoint):
    """Menge vorhandener Namen (lowercase) eines Endpunkts, oder None bei Fehler."""
    names = set()
    hdr = {"Authorization": "Token " + token} if token else {}
    nexturl = url.rstrip("/") + "/api/" + endpoint + "?page_size=250"
    try:
        while nexturl:
            r = requests.get(nexturl, headers=hdr, timeout=10, allow_redirects=False)
            if r.status_code != 200:
                return None
            j = r.json()
            for row in j.get("results", []):
                if row.get("name"):
                    names.add(row["name"].strip().lower())
            nexturl = j.get("next")
    except (requests.RequestException, ValueError):
        return None
    return names


def _instance_snapshot(url, token):
    """Ist-Zustand der Instanz (Config-Objekte) sichern — Restore-Punkt vor dem Schreiben."""
    hdr = {"Authorization": "Token " + token} if token else {}
    snap = {}
    for ep in ("tags", "document_types", "correspondents", "storage_paths", "custom_fields"):
        rows, nexturl = [], url.rstrip("/") + "/api/" + ep + "/?page_size=250"
        try:
            while nexturl:
                r = requests.get(nexturl, headers=hdr, timeout=10, allow_redirects=False)
                if r.status_code != 200:
                    break
                j = r.json()
                rows.extend(j.get("results", []))
                nexturl = j.get("next")
        except (requests.RequestException, ValueError):
            pass
        snap[ep] = rows
    return snap


def _save_instance_snapshot(pid, snap):
    d = os.path.join(SNAP_DIR, pid)
    os.makedirs(d, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    with open(os.path.join(d, ts + ".json"), "w", encoding="utf-8") as fh:
        json.dump(snap, fh, ensure_ascii=False)


def _api_create(url, token, endpoint, payload):
    hdr = {"Content-Type": "application/json"}
    if token:
        hdr["Authorization"] = "Token " + token
    try:
        r = requests.post(url.rstrip("/") + "/api/" + endpoint, headers=hdr, json=payload,
                          timeout=15, allow_redirects=False)
        if r.status_code in (200, 201):
            return True, (r.json() or {}).get("id"), None
        return False, None, "HTTP %d" % r.status_code
    except (requests.RequestException, ValueError) as exc:
        return False, None, str(exc)


def _api_delete(url, token, endpoint, oid):
    hdr = {"Authorization": "Token " + token} if token else {}
    try:
        r = requests.delete(url.rstrip("/") + "/api/" + endpoint + str(oid) + "/",
                            headers=hdr, timeout=15, allow_redirects=False)
        return r.status_code in (200, 204)
    except requests.RequestException:
        return False


def _gc_apply_entries(gc, key):
    """Geordnete Liste anlegbarer Eintraege einer Kategorie — spiegelt exakt die
    Payloads des Generator-Direkt-Modus (05-direct.js). Tags sind ein Baum
    (Eltern zuerst, dann Kinder mit ``parent``); Tag-Matching stammt aus
    ``tagMatch`` (per Name). Jeder Eintrag: {name, endpoint, payload, parent_name}."""
    entries = []
    if key == "tags":
        tm = {m.get("name"): m for m in (gc.get("tagMatch") or [])
              if isinstance(m, dict) and m.get("name")}
        for par in (gc.get("tags") or []):
            if not isinstance(par, dict) or not par.get("name") or not _entry_on(par):
                continue
            ppl = {"name": par["name"], "matching_algorithm": 0}
            if par.get("color"):
                ppl["color"] = par["color"]
            if par.get("isInbox"):
                ppl["is_inbox_tag"] = True
            entries.append({"name": par["name"], "endpoint": "tags/",
                            "payload": ppl, "parent_name": None})
            for ch in (par.get("children") or []):
                if not isinstance(ch, dict) or not ch.get("name") or not _entry_on(ch):
                    continue
                cpl = {"name": ch["name"], "matching_algorithm": 0}
                if ch.get("color"):
                    cpl["color"] = ch["color"]
                m = tm.get(ch["name"])
                if m and m.get("algo", 0) > 0 and m.get("match"):
                    cpl.update({"matching_algorithm": m["algo"],
                                "match": m["match"], "is_insensitive": True})
                entries.append({"name": ch["name"], "endpoint": "tags/",
                                "payload": cpl, "parent_name": par["name"]})
        return entries
    simple = {
        "types": ("document_types/", None),
        "correspondents": ("correspondents/", None),
        "storagePaths": ("storage_paths/", "path"),
    }
    if key in simple:
        endpoint, pathfield = simple[key]
        for e in (gc.get(key) or []):
            if not isinstance(e, dict) or not e.get("name") or not _entry_on(e):
                continue
            pl = {"name": e["name"], "matching_algorithm": 0}
            if pathfield:
                pl["path"] = e.get(pathfield, "")
            if e.get("algo", 0) > 0 and e.get("match"):
                pl.update({"matching_algorithm": e["algo"],
                           "match": e["match"], "is_insensitive": True})
            entries.append({"name": e["name"], "endpoint": endpoint,
                            "payload": pl, "parent_name": None})
        return entries
    return []


def _save_undo(pid, items):
    os.makedirs(UNDO_DIR, exist_ok=True)
    with open(os.path.join(UNDO_DIR, pid + ".json"), "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False)


def _load_undo(pid):
    try:
        with open(os.path.join(UNDO_DIR, pid + ".json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return []


@app.route("/anwenden")
def anwenden():
    profs = load_profiles()
    aid = _active_id()
    act = profs.get(aid, {})
    url = act.get("paperless_url")
    token = _dec(act.get("paperless_token"))
    ctx = {"active": act, "productive": bool(act.get("productive")),
           "err": request.args.get("err"), "undo_available": bool(_load_undo(aid))}
    if act.get("readonly"):
        return render_template("anwenden.html", blocked="Dieses Profil ist auf „nur lesen“ gesetzt — Schreiben gesperrt.", groups=None, **ctx)
    if not act.get("generator_config"):
        return render_template("anwenden.html", blocked="Für dieses Profil ist noch keine Konfiguration gespeichert.", groups=None, **ctx)
    if not url:
        return render_template("anwenden.html", blocked="Keine Paperless-URL gesetzt.", groups=None, **ctx)
    gc = act.get("generator_config") or {}
    groups = []
    for label, key, ep in _APPLY_CATS:
        inst = _instance_names(url, token, ep)
        if inst is None:
            return render_template("anwenden.html", blocked="Instanz nicht erreichbar oder Token ungültig.", groups=None, **ctx)
        missing, seen = [], set()
        for e in _gc_apply_entries(gc, key):
            low = e["name"].strip().lower()
            if low in inst or low in seen:
                continue
            seen.add(low)
            missing.append(e["name"])
        if missing:
            groups.append({"label": label, "key": key, "entries": missing})
    return render_template("anwenden.html", blocked=None, groups=groups, **ctx)


@app.route("/anwenden", methods=["POST"])
def anwenden_post():
    profs = load_profiles()
    aid = _active_id()
    act = profs.get(aid, {})
    url = act.get("paperless_url")
    token = _dec(act.get("paperless_token"))
    if act.get("readonly") or not url:
        return redirect(url_for("anwenden"))
    if not check_password_hash(load_config()["admin_pw_hash"], request.form.get("password", "")):
        return redirect(url_for("anwenden", err="Passwort falsch — es wurde NICHTS geändert."))
    selected = request.form.getlist("item")
    if not selected:
        return redirect(url_for("anwenden", err="Nichts angehakt — es wurde nichts geändert."))
    gc = act.get("generator_config") or {}
    selected = set(selected)
    snap = _instance_snapshot(url, token)
    _save_instance_snapshot(aid, snap)  # Restore-Punkt VOR dem Schreiben
    # Name→ID der vorhandenen Tags (fuer Kind→Eltern-Verknuepfung); waechst mit neu Angelegten.
    tagid = {row["name"].strip().lower(): row["id"]
             for row in snap.get("tags", [])
             if isinstance(row, dict) and row.get("name") and row.get("id") is not None}
    created, errors = [], []
    for _label, key, _ep in _APPLY_CATS:  # Reihenfolge wichtig: Eltern-Tags vor Kindern
        for e in _gc_apply_entries(gc, key):
            if (key + "|" + e["name"]) not in selected:
                continue
            payload = dict(e["payload"])
            if key == "tags" and e["parent_name"]:
                pid = tagid.get(e["parent_name"].strip().lower())
                if not pid:
                    errors.append("%s: Eltern-Tag „%s“ fehlt — bitte zuerst anhaken/anlegen"
                                  % (e["name"], e["parent_name"]))
                    continue
                payload["parent"] = pid
            ok, oid, err = _api_create(url, token, e["endpoint"], payload)
            if ok:
                created.append({"endpoint": e["endpoint"], "id": oid, "name": e["name"]})
                if key == "tags" and oid is not None:
                    tagid[e["name"].strip().lower()] = oid
            else:
                errors.append("%s: %s" % (e["name"], err))
    _save_undo(aid, created)
    _log_activity("apply", "Anwenden auf %s: %d angelegt%s"
                  % (act.get("name"), len(created),
                     (", %d Fehler" % len(errors)) if errors else ""),
                  level=("warn" if errors else "ok"),
                  detail=("Angelegt:\n" + "\n".join("· %s (%s)" % (c["name"], c["endpoint"]) for c in created)
                          + (("\n\nFehler:\n" + "\n".join("· " + e for e in errors)) if errors else ""))
                  if created or errors else None)
    return render_template("anwenden_done.html", created=created, errors=errors, active=act)


@app.route("/anwenden/undo", methods=["POST"])
def anwenden_undo():
    profs = load_profiles()
    aid = _active_id()
    act = profs.get(aid, {})
    url = act.get("paperless_url")
    token = _dec(act.get("paperless_token"))
    undo = _load_undo(aid)
    removed = 0
    for it in reversed(undo):  # Kinder vor Eltern loeschen
        if it.get("id") and _api_delete(url, token, it.get("endpoint"), it["id"]):
            removed += 1
    _save_undo(aid, [])
    _log_activity("undo", "Rückgängig auf %s: %d Einträge entfernt" % (act.get("name"), removed))
    return redirect(url_for("protokoll"))


@app.route("/profiles", methods=["POST"])
def profiles_create():
    name = request.form.get("name", "").strip() or "Neues Profil"
    profs = load_profiles()
    pid = _new_profile_id()
    profs[pid] = {"name": name, "paperless_url": "", "paperless_token": "",
                  "generator_config": None, "productive": False, "readonly": False, "color": ""}
    save_profiles(profs)
    set_active_profile(pid)
    _log_activity("profile", "Profil angelegt: %s" % name)
    return redirect(url_for("verwaltung", tab="profiles", msg="Profil angelegt und aktiviert."))


@app.route("/profiles/<pid>/activate", methods=["POST"])
def profiles_activate(pid):
    if set_active_profile(pid):
        return redirect(url_for("index"))
    return redirect(url_for("verwaltung", tab="profiles", err="Profil nicht gefunden."))


@app.route("/profiles/<pid>/rename", methods=["POST"])
def profiles_rename(pid):
    profs = load_profiles()
    if pid in profs:
        new = request.form.get("name", "").strip()
        if new:
            profs[pid]["name"] = new
            save_profiles(profs)
    return redirect(url_for("verwaltung", tab="profiles"))


@app.route("/profiles/<pid>/delete", methods=["POST"])
def profiles_delete(pid):
    profs = load_profiles()
    if pid not in profs:
        return redirect(url_for("verwaltung", tab="profiles", err="Profil nicht gefunden."))
    if len(profs) <= 1:
        return redirect(url_for("verwaltung", tab="profiles", err="Das letzte Profil kann nicht gelöscht werden."))
    del profs[pid]
    save_profiles(profs)
    if _active_id() not in profs:            # war es aktiv -> auf ein anderes umschalten
        set_active_profile(next(iter(profs)))
    return redirect(url_for("verwaltung", tab="profiles", msg="Profil gelöscht."))


@app.route("/profiles/export")
def profiles_export():
    """Alle Profile als JSON herunterladen (Disaster-Recovery / Umzug).
    Tokens werden entschluesselt exportiert, damit das Backup auf einer anderen
    Instanz (mit anderem 'secret') einspielbar ist."""
    out = {}
    for pid, p in load_profiles().items():
        q = json.loads(json.dumps(p))  # tiefe Kopie, damit die Entschluesselung nicht durchschlaegt
        if q.get("paperless_token"):
            q["paperless_token"] = _dec(q["paperless_token"])
        n = q.get("notifications")
        if isinstance(n, dict):
            for ch, field in _NOTIFY_SECRETS:
                c = n.get(ch)
                if isinstance(c, dict) and c.get(field):
                    c[field] = _dec(c[field])
        out[pid] = q
    data = json.dumps(out, indent=2, ensure_ascii=False)
    return Response(data, mimetype="application/json", headers={
        "Content-Disposition": "attachment; filename=paperless-portal-profiles.json"})


@app.route("/profiles/import", methods=["POST"])
def profiles_import():
    """Profile aus hochgeladener JSON wiederherstellen (ersetzt alle; vorige werden gesichert)."""
    f = request.files.get("file")
    if not f:
        return redirect(url_for("verwaltung", tab="profiles", err="Keine Datei ausgewählt."))
    try:
        data = json.load(f.stream)
    except ValueError:
        return redirect(url_for("verwaltung", tab="profiles", err="Ungültige JSON-Datei."))
    if not isinstance(data, dict) or not data or \
            any(not isinstance(v, dict) or "name" not in v for v in data.values()):
        return redirect(url_for("verwaltung", tab="profiles", err="Datei enthält keine gültigen Profile."))
    save_profiles(data)  # sichert die vorige Version automatisch (rotierendes Backup)
    if _active_id() not in data:
        set_active_profile(next(iter(data)))
    return redirect(url_for("verwaltung", tab="profiles", msg="Profile importiert (vorheriger Stand gesichert)."))


# Kategorien der generator_config fuer Historie-Diff/selektiven Restore (Schluessel wie im
# Generator-Export). Gekoppelte Nebenschluessel werden beim Restore mitgezogen.
_CFG_DIFF_CATS = [
    ("tags", "Tags"),
    ("types", "Dokumenttypen"),
    ("fields", "Benutzerdef. Felder"),
    ("correspondents", "Korrespondenten"),
    ("storagePaths", "Speicherpfade"),
    ("workflows", "Arbeitsabläufe"),
    ("fristConfigs", "Frist-Erinnerungen"),
]
_CFG_DIFF_COUPLED = {"tags": "tagMatch", "fields": "fieldGroups"}


def _cfg_names(gc, key):
    """Menge der Anzeigenamen (lowercase) einer Kategorie. Tags flach (Eltern + Kinder)."""
    gc = gc or {}
    out = set()
    if key == "tags":
        for par in (gc.get("tags") or []):
            if isinstance(par, dict) and par.get("name"):
                out.add(par["name"].strip().lower())
                for ch in (par.get("children") or []):
                    if isinstance(ch, dict) and ch.get("name"):
                        out.add(ch["name"].strip().lower())
        return out
    for e in (gc.get(key) or []):
        if isinstance(e, dict):
            nm = e.get("name") or e.get("label") or e.get("title")
            if nm:
                out.add(str(nm).strip().lower())
    return out


def _history_diff_rows(cur, snap):
    """Pro Kategorie: was ein Restore hinzufuegen (added) bzw. entfernen (removed) wuerde."""
    rows = []
    for key, label in _CFG_DIFF_CATS:
        c = _cfg_names(cur, key)
        s = _cfg_names(snap, key)
        if not (c or s):
            continue
        added = sorted(s - c)    # im Snapshot, nicht aktuell -> kaemen durch Restore zurueck
        removed = sorted(c - s)  # aktuell, nicht im Snapshot -> wuerden durch Restore entfernt
        rows.append({"key": key, "label": label, "cur": len(c), "snap": len(s),
                     "added": added, "removed": removed, "changed": bool(added or removed)})
    return rows


@app.route("/profiles/<pid>/history/<ts>/diff")
def profiles_history_diff(pid, ts):
    """Fragment: Vergleich eines Snapshots mit dem aktuellen Stand + Auswahl zum Restore."""
    profs = load_profiles()
    if pid not in profs:
        return Response("Profil nicht gefunden", status=404)
    path = os.path.join(_history_dir(pid), ts + ".json")
    if not os.path.exists(path):
        return Response("Snapshot nicht gefunden", status=404)
    with open(path, encoding="utf-8") as fh:
        snap = json.load(fh)
    rows = _history_diff_rows(profs[pid].get("generator_config") or {}, snap)
    return render_template("history_diff.html", pid=pid, ts=ts, label=_fmt_ts(ts),
                           rows=rows, name=profs[pid].get("name") or pid,
                           any_change=any(r["changed"] for r in rows))


@app.route("/profiles/<pid>/history/<ts>/restore", methods=["POST"])
def profiles_history_restore(pid, ts):
    profs = load_profiles()
    if pid not in profs:
        return redirect(url_for("verwaltung", tab="profiles", err="Profil nicht gefunden."))
    path = os.path.join(_history_dir(pid), ts + ".json")
    if not os.path.exists(path):
        return redirect(url_for("verwaltung", tab="profiles", err="Snapshot nicht gefunden."))
    with open(path, encoding="utf-8") as fh:
        snap = json.load(fh)
    keys = [k for k in request.form.getlist("keys") if k in dict(_CFG_DIFF_CATS)]
    _snapshot_history(pid, profs[pid].get("generator_config"))  # aktuellen Stand vorher sichern
    if keys:
        base = dict(profs[pid].get("generator_config") or {})
        for k in keys:
            if k in snap:
                base[k] = snap[k]
            else:
                base.pop(k, None)  # Kategorie im Snapshot leer -> im Ziel leeren
            coupled = _CFG_DIFF_COUPLED.get(k)   # z. B. tags -> tagMatch mitziehen
            if coupled:
                if coupled in snap:
                    base[coupled] = snap[coupled]
                else:
                    base.pop(coupled, None)
        profs[pid]["generator_config"] = _strip_conn(base)
        note = "%d Kategorie(n)" % len(keys)
        detail = "Kategorien: " + ", ".join(dict(_CFG_DIFF_CATS).get(k, k) for k in keys)
    else:
        profs[pid]["generator_config"] = _strip_conn(snap)  # komplett
        note, detail = "komplett", "vollständiger Snapshot"
    save_profiles(profs)
    _log_activity("restore", "Snapshot vom %s wiederhergestellt (%s)" % (_fmt_ts(ts), note),
                  detail=detail)
    return redirect(url_for("verwaltung", tab="profiles",
                            msg="Snapshot vom %s wiederhergestellt (%s)." % (_fmt_ts(ts), note)))


@app.route("/profiles/<pid>/flags", methods=["POST"])
def profiles_flags(pid):
    profs = load_profiles()
    if pid not in profs:
        return redirect(url_for("verwaltung", tab="profiles", err="Profil nicht gefunden."))
    profs[pid]["productive"] = bool(request.form.get("productive"))
    profs[pid]["readonly"] = bool(request.form.get("readonly"))
    profs[pid]["color"] = request.form.get("color", "").strip()[:16]
    save_profiles(profs)
    _log_activity("profile", "Flags geaendert (%s): produktiv=%s, readonly=%s"
                  % (profs[pid].get("name"), profs[pid]["productive"], profs[pid]["readonly"]))
    return redirect(url_for("verwaltung", tab="profiles", msg="Profil-Einstellungen gespeichert."))


@app.route("/profiles/<pid>/connection", methods=["POST"])
def profiles_connection(pid):
    profs = load_profiles()
    if pid not in profs:
        return redirect(url_for("verwaltung", tab="profiles", err="Profil nicht gefunden."))
    url = request.form.get("paperless_url", "").strip().rstrip("/")
    tok = request.form.get("paperless_token", "").strip()
    if url:
        profs[pid]["paperless_url"] = url
    if tok:
        profs[pid]["paperless_token"] = tok
    if "notify_email" in request.form:
        gc = profs[pid].get("generator_config") or {}
        gc["notifyEmail"] = request.form.get("notify_email", "").strip()
        profs[pid]["generator_config"] = gc
    save_profiles(profs)
    _log_activity("connection", "Verbindung geändert: %s" % (profs[pid].get("name") or pid),
                  detail="Ziel-URL: %s" % (profs[pid].get("paperless_url") or "—"))
    return redirect(url_for("verwaltung", tab="profiles", msg="Verbindung gespeichert."))


@app.route("/profiles/<pid>/watch", methods=["POST"])
def profiles_watch(pid):
    """Pro-Profil-Ueberwachung setzen: an/aus + welche Checks (ueberschreibt die globale Vorgabe)."""
    profs = load_profiles()
    if pid not in profs:
        return redirect(url_for("verwaltung", tab="profiles", err="Profil nicht gefunden."))
    f = request.form
    profs[pid]["watch"] = {
        "enabled": bool(f.get("watch_enabled")),
        "checks": {k: bool(f.get("wchk_" + k)) for k in _WATCHER_DEFAULTS["checks"]},
    }
    save_profiles(profs)
    _log_activity("profile", "Ueberwachung geaendert (%s): enabled=%s"
                  % (profs[pid].get("name") or pid, profs[pid]["watch"]["enabled"]))
    return redirect(url_for("verwaltung", tab="profiles", msg="Überwachung gespeichert."))


# ─── Generator-Config des aktiven Profils laden/speichern (vom injizierten JS genutzt) ──
@app.route("/portal/config", methods=["GET"])
def portal_config_get():
    return jsonify(active_profile().get("generator_config"))


@app.route("/portal/config", methods=["POST"])
def portal_config_post():
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"ok": False, "error": "kein JSON"}), 400
    profs = load_profiles()
    aid = _active_id()
    if not aid or aid not in profs:
        return jsonify({"ok": False, "error": "kein aktives Profil"}), 400
    _snapshot_history(aid, profs[aid].get("generator_config"))  # vorigen Stand sichern
    profs[aid]["generator_config"] = _strip_conn(data)
    save_profiles(profs)
    _log_activity("save", "Profil-Konfiguration gespeichert: %s" % (profs[aid].get("name") or aid))
    return jsonify({"ok": True, "name": profs[aid].get("name")})


@app.route("/portal/inject.js")
def portal_inject_js():
    return Response(INJECT_JS, mimetype="application/javascript")


@app.route("/portal/profiles.json", methods=["GET"])
def portal_profiles_list():
    """Leichte Profil-Liste fuer den Dropdown im Generator (ohne Tokens/Config)."""
    profs = load_profiles()
    aid = _active_id()
    act = profs.get(aid, {})
    return jsonify({
        "active": aid,
        "active_name": act.get("name") or "",
        "active_productive": bool(act.get("productive")),
        "active_readonly": bool(act.get("readonly")),
        "active_color": act.get("color") or "",
        "profiles": [{"id": pid, "name": p.get("name") or "(ohne Name)"}
                     for pid, p in profs.items()],
    })


_DELETE_METHODS = ("delete", "delete_documents")
_BULK_EDIT_PATH = "/api/documents/bulk_edit"


def _bulk_method():
    """Das 'method'-Feld eines bulk_edit-Rumpfs ziehen — egal, wie der Client es kodiert.
    Rueckgabe: der Name klein und ohne Rand-Leerzeichen, oder None = Rumpf unlesbar.

    request.get_json() allein reicht NICHT: bei einem Content-Type ausser
    application/json liefert es None, der Proxy schickt den Rumpf aber unveraendert weiter
    (data=request.get_data()). Riegel und Paperless muessen denselben Byte-Strom beurteilen,
    sonst laesst sich der Riegel durch das blosse Umdeklarieren des Content-Type umgehen."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raw = request.get_data(as_text=True) or ""
        try:
            body = json.loads(raw)          # JSON, nur falsch deklariert
        except ValueError:
            body = dict(parse_qsl(raw))     # formular-kodiert
    if not isinstance(body, dict):
        return None
    if "method" not in body:
        return None
    return str(body.get("method") or "").strip().lower()


def _is_document_delete():
    """True, wenn der Request ein Dokument loeschen wuerde — harter Sicherheits-Riegel."""
    if request.method == "DELETE" and request.path.startswith("/api/documents/"):
        return True
    if request.method == "POST" and request.path.rstrip("/") == _BULK_EDIT_PATH:
        m = _bulk_method()
        # Unlesbarer Rumpf -> sperren. Ein ehrlicher Client schickt hier immer lesbares
        # JSON; wer das nicht tut, bekommt keinen Freifahrtschein am Riegel vorbei.
        if m is None or m in _DELETE_METHODS:
            return True
    return False


@app.route("/api/", defaults={"path": ""}, methods=PROXY_METHODS)
@app.route("/api/<path:path>", methods=PROXY_METHODS)
def proxy(path):  # noqa: ARG001 (path steckt schon in request.path)
    prof = active_profile()
    # ── Sicherheits-Riegel (unabhaengig vom Client) ──
    if _is_document_delete():
        _log_activity("blocked", "Dokument-Löschung geblockt", level="warn",
                      detail="%s %s" % (request.method, request.path))
        return Response("Gesperrt: Dokument-Loeschung ist im Portal nicht erlaubt.", status=403)
    if prof.get("readonly") and request.method in WRITE_METHODS:
        return Response("Profil ist auf 'nur lesen' gesetzt — Schreibzugriff gesperrt.", status=403)
    base = prof.get("paperless_url")
    token = _dec(prof.get("paperless_token"))
    if not base:
        return Response(
            "Paperless-URL ist nicht konfiguriert. Bitte in den Einstellungen setzen.",
            status=503,
        )

    target = base.rstrip("/") + request.path
    fwd_headers = {}
    for key, value in request.headers:
        low = key.lower()
        if low in ("host", "cookie", "content-length", "connection",
                   "authorization", "accept-encoding"):
            continue
        fwd_headers[key] = value
    # Nur Verfahren erlauben, die `requests` sicher dekodiert (zlib) — sonst liefert
    # Paperless evtl. brotli/zstd, das ungoutet an den Browser durchgereicht wuerde.
    fwd_headers["Accept-Encoding"] = "gzip, deflate"
    if token:
        fwd_headers["Authorization"] = "Token " + token

    try:
        upstream = requests.request(
            method=request.method,
            url=target,
            params=request.args,
            data=request.get_data(),
            headers=fwd_headers,
            allow_redirects=False,
            timeout=120,
        )
    except requests.RequestException as exc:
        return Response("Paperless nicht erreichbar: " + str(exc), status=502)

    headers = [(k, v) for k, v in upstream.headers.items()
               if k.lower() not in EXCLUDED_RESP_HEADERS]
    return Response(upstream.content, status=upstream.status_code, headers=headers)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
