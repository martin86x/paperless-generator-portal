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
import json
import os
import secrets
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

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
SITE_DIR = os.environ.get("SITE_DIR", os.path.join(os.path.dirname(__file__), "site"))

DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASS = "admin"

PORTAL_VERSION = "1.0"                       # Portal-Release (Profil-System)
GITHUB_REPO = "martin86x/paperless-generator-portal"

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


def _log_activity(kind, message):
    """Append-only Aktivitaetsprotokoll (wann/was/Ergebnis) in /config/activity.log."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        line = json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                           "kind": kind, "msg": message}, ensure_ascii=False)
        with open(ACTIVITY_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
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
    # Tokens verschluesselt ablegen (idempotent: bereits verschluesselte bleiben).
    for p in profs.values():
        if isinstance(p, dict) and p.get("paperless_token"):
            p["paperless_token"] = _enc(p["paperless_token"])
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

PUBLIC_ENDPOINTS = {"login", "healthz", "static"}

# ── Login-Rate-Limit (in-memory, pro IP) ──────────────────────────────────────
_login_fails = {}
LOGIN_MAX = 5
LOGIN_WINDOW = 300  # Sekunden


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
            # Erst-Einrichtung (Default-Passwort aktiv) -> gefuehrter Wizard.
            if cfg.get("is_default_pw"):
                return redirect(url_for("wizard"))
            return redirect(url_for("index"))
        _login_note_fail(ip)
        error = "Falscher Benutzername oder falsches Passwort."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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
                save_config(cfg)
                if aid in profs:
                    profs[aid]["name"] = name
                    profs[aid]["paperless_url"] = url
                    profs[aid]["paperless_token"] = tok
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
                           url=prof.get("paperless_url", ""), err=err)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    cfg = load_config()
    msg = err = None
    if request.method == "POST":
        # Nur noch Passwort — Paperless-Verbindungen laufen ueber Profile (/profiles).
        new = request.form.get("new", "")
        if not new:
            err = "Bitte ein neues Passwort eingeben."
        else:
            cur = request.form.get("current", "")
            rep = request.form.get("repeat", "")
            if not check_password_hash(cfg["admin_pw_hash"], cur):
                err = "Aktuelles Passwort ist falsch."
            elif len(new) < 4:
                err = "Neues Passwort muss mindestens 4 Zeichen haben."
            elif new != rep:
                err = "Die neuen Passwörter stimmen nicht überein."
            else:
                cfg["admin_pw_hash"] = generate_password_hash(new)
                cfg["is_default_pw"] = False
                save_config(cfg)
                msg = "Passwort geändert."
    return render_template("settings.html", is_default_pw=cfg.get("is_default_pw", False),
                           msg=msg, err=err)


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
    items = []
    for pid, p in profs.items():
        kind, text = _connection_status({
            "paperless_url": p.get("paperless_url"),
            "paperless_token": _dec(p.get("paperless_token")),
        })
        items.append({
            "id": pid,
            "name": p.get("name") or "(ohne Name)",
            "url": p.get("paperless_url") or "",
            "has_token": bool(p.get("paperless_token")),
            "has_config": p.get("generator_config") is not None,
            "active": pid == aid,
            "conn_kind": kind, "conn_text": text,
            "productive": bool(p.get("productive")),
            "readonly": bool(p.get("readonly")),
            "color": p.get("color") or "",
            "history": [{"ts": ts, "label": _fmt_ts(ts)} for ts in _list_history(pid)],
        })
    items.sort(key=lambda x: x["name"].lower())
    return render_template("profiles.html", profiles=items,
                           msg=request.args.get("msg"), err=request.args.get("err"))


@app.route("/update")
def update_page():
    """Versions-Anzeige + Update-Pruefung gegen GitHub + Rebuild-Befehl.

    Ein echter 1-Klick-Self-Rebuild ist bewusst NICHT umgesetzt: der Container
    (python:slim) hat kein git/docker und das Repo liegt auf dem LXC-Host — ein
    Self-Rebuild braeuchte den Docker-Socket (root-aequivalent). Daher: Update
    ANZEIGEN, Rebuild-Befehl zum Kopieren. (Self-Rebuild -> Roadmap.)
    """
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
    rebuild_cmd = ("cd /opt/paperless-generator-portal && git fetch origin main && "
                   "git reset --hard origin/main && docker compose up -d --build")
    return render_template("update.html", version=PORTAL_VERSION, latest=latest,
                           gh_err=gh_err, rebuild_cmd=rebuild_cmd, repo=GITHUB_REPO)


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


@app.route("/verwaltung")
def verwaltung():
    """Cockpit-Startseite: Status des aktiven Profils, Kennzahlen, Profilliste, Portal-Selbststatus."""
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
    plist = []
    for pid, p in profs.items():
        pk, _t = _connection_status({"paperless_url": p.get("paperless_url"),
                                     "paperless_token": _dec(p.get("paperless_token"))})
        plist.append({"id": pid, "name": p.get("name") or "(ohne Name)",
                      "active": pid == aid, "kind": pk})
    plist.sort(key=lambda x: x["name"].lower())
    ref = PROFILES_PATH + ".bak.1"
    if not os.path.exists(ref):
        ref = PROFILES_PATH if os.path.exists(PROFILES_PATH) else None
    last_backup = datetime.fromtimestamp(os.path.getmtime(ref)).strftime("%d.%m.%Y %H:%M") if ref else "—"
    portal = {"version": PORTAL_VERSION, "config_size": _fmt_size(_dir_size(CONFIG_DIR)),
              "last_backup": last_backup}
    return render_template(
        "cockpit.html",
        active={"id": aid, "name": act.get("name") or "", "url": url or "",
                "productive": bool(act.get("productive")), "readonly": bool(act.get("readonly")),
                "conn_kind": kind, "conn_text": text,
                "has_config": act.get("generator_config") is not None},
        stats=stats, profiles=plist, portal=portal)


@app.route("/protokoll")
def protokoll():
    return render_template("protokoll.html", entries=_read_activity(200))


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
                        cfg_n = len(gc.get(key) or [])
                        inst_n = _api_count(url, token, ep + "?page_size=1")
                        drift.append({"label": label, "cfg": cfg_n, "inst": inst_n,
                                      "diff": (cfg_n - inst_n) if inst_n is not None else None})
                    card["drift"] = drift
        cards.append(card)
    cards.sort(key=lambda c: c["name"].lower())
    return render_template("dashboard.html", cards=cards)


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
            if not isinstance(par, dict) or not par.get("name"):
                continue
            ppl = {"name": par["name"], "matching_algorithm": 0}
            if par.get("color"):
                ppl["color"] = par["color"]
            if par.get("isInbox"):
                ppl["is_inbox_tag"] = True
            entries.append({"name": par["name"], "endpoint": "tags/",
                            "payload": ppl, "parent_name": None})
            for ch in (par.get("children") or []):
                if not isinstance(ch, dict) or not ch.get("name"):
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
            if not isinstance(e, dict) or not e.get("name"):
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
                     (", %d Fehler" % len(errors)) if errors else ""))
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
    return redirect(url_for("profiles", msg="Profil angelegt und aktiviert."))


@app.route("/profiles/<pid>/activate", methods=["POST"])
def profiles_activate(pid):
    if set_active_profile(pid):
        return redirect(url_for("index"))
    return redirect(url_for("profiles", err="Profil nicht gefunden."))


@app.route("/profiles/<pid>/rename", methods=["POST"])
def profiles_rename(pid):
    profs = load_profiles()
    if pid in profs:
        new = request.form.get("name", "").strip()
        if new:
            profs[pid]["name"] = new
            save_profiles(profs)
    return redirect(url_for("profiles"))


@app.route("/profiles/<pid>/delete", methods=["POST"])
def profiles_delete(pid):
    profs = load_profiles()
    if pid not in profs:
        return redirect(url_for("profiles", err="Profil nicht gefunden."))
    if len(profs) <= 1:
        return redirect(url_for("profiles", err="Das letzte Profil kann nicht gelöscht werden."))
    del profs[pid]
    save_profiles(profs)
    if _active_id() not in profs:            # war es aktiv -> auf ein anderes umschalten
        set_active_profile(next(iter(profs)))
    return redirect(url_for("profiles", msg="Profil gelöscht."))


@app.route("/profiles/export")
def profiles_export():
    """Alle Profile als JSON herunterladen (Disaster-Recovery / Umzug).
    Tokens werden entschluesselt exportiert, damit das Backup auf einer anderen
    Instanz (mit anderem 'secret') einspielbar ist."""
    out = {}
    for pid, p in load_profiles().items():
        q = dict(p)
        if q.get("paperless_token"):
            q["paperless_token"] = _dec(q["paperless_token"])
        out[pid] = q
    data = json.dumps(out, indent=2, ensure_ascii=False)
    return Response(data, mimetype="application/json", headers={
        "Content-Disposition": "attachment; filename=paperless-portal-profiles.json"})


@app.route("/profiles/import", methods=["POST"])
def profiles_import():
    """Profile aus hochgeladener JSON wiederherstellen (ersetzt alle; vorige werden gesichert)."""
    f = request.files.get("file")
    if not f:
        return redirect(url_for("profiles", err="Keine Datei ausgewählt."))
    try:
        data = json.load(f.stream)
    except ValueError:
        return redirect(url_for("profiles", err="Ungültige JSON-Datei."))
    if not isinstance(data, dict) or not data or \
            any(not isinstance(v, dict) or "name" not in v for v in data.values()):
        return redirect(url_for("profiles", err="Datei enthält keine gültigen Profile."))
    save_profiles(data)  # sichert die vorige Version automatisch (rotierendes Backup)
    if _active_id() not in data:
        set_active_profile(next(iter(data)))
    return redirect(url_for("profiles", msg="Profile importiert (vorheriger Stand gesichert)."))


@app.route("/profiles/<pid>/history/<ts>/restore", methods=["POST"])
def profiles_history_restore(pid, ts):
    profs = load_profiles()
    if pid not in profs:
        return redirect(url_for("profiles", err="Profil nicht gefunden."))
    path = os.path.join(_history_dir(pid), ts + ".json")
    if not os.path.exists(path):
        return redirect(url_for("profiles", err="Snapshot nicht gefunden."))
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    _snapshot_history(pid, profs[pid].get("generator_config"))  # aktuellen Stand sichern
    profs[pid]["generator_config"] = cfg
    save_profiles(profs)
    return redirect(url_for("profiles", msg="Snapshot vom %s wiederhergestellt." % _fmt_ts(ts)))


@app.route("/profiles/<pid>/flags", methods=["POST"])
def profiles_flags(pid):
    profs = load_profiles()
    if pid not in profs:
        return redirect(url_for("profiles", err="Profil nicht gefunden."))
    profs[pid]["productive"] = bool(request.form.get("productive"))
    profs[pid]["readonly"] = bool(request.form.get("readonly"))
    profs[pid]["color"] = request.form.get("color", "").strip()[:16]
    save_profiles(profs)
    _log_activity("profile", "Flags geaendert (%s): produktiv=%s, readonly=%s"
                  % (profs[pid].get("name"), profs[pid]["productive"], profs[pid]["readonly"]))
    return redirect(url_for("profiles", msg="Profil-Einstellungen gespeichert."))


@app.route("/profiles/<pid>/connection", methods=["POST"])
def profiles_connection(pid):
    profs = load_profiles()
    if pid not in profs:
        return redirect(url_for("profiles", err="Profil nicht gefunden."))
    url = request.form.get("paperless_url", "").strip().rstrip("/")
    tok = request.form.get("paperless_token", "").strip()
    if url:
        profs[pid]["paperless_url"] = url
    if tok:
        profs[pid]["paperless_token"] = tok
    save_profiles(profs)
    _log_activity("connection", "Verbindung geaendert: %s" % (profs[pid].get("name") or pid))
    return redirect(url_for("profiles", msg="Verbindung gespeichert."))


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


def _is_document_delete():
    """True, wenn der Request ein Dokument loeschen wuerde — harter Sicherheits-Riegel."""
    if request.method == "DELETE" and request.path.startswith("/api/documents/"):
        return True
    if request.method == "POST" and request.path.rstrip("/") == "/api/documents/bulk_edit":
        body = request.get_json(silent=True) or {}
        if str(body.get("method", "")).lower() in ("delete", "delete_documents"):
            return True
    return False


@app.route("/api/", defaults={"path": ""}, methods=PROXY_METHODS)
@app.route("/api/<path:path>", methods=PROXY_METHODS)
def proxy(path):  # noqa: ARG001 (path steckt schon in request.path)
    prof = active_profile()
    # ── Sicherheits-Riegel (unabhaengig vom Client) ──
    if _is_document_delete():
        _log_activity("blocked", "Dokument-Loeschung geblockt: %s %s" % (request.method, request.path))
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
