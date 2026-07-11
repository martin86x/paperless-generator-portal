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
import json
import os
import secrets
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests
from flask import (Flask, Response, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
PROFILES_PATH = os.path.join(CONFIG_DIR, "profiles.json")
HISTORY_DIR = os.path.join(CONFIG_DIR, "history")
HISTORY_MAX = 20
SITE_DIR = os.environ.get("SITE_DIR", os.path.join(os.path.dirname(__file__), "site"))

DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASS = "admin"

# Hop-by-hop-Header + solche, die requests bereits aufloest (Content-Encoding/-Length),
# duerfen nicht 1:1 an den Browser durchgereicht werden.
EXCLUDED_RESP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "content-encoding", "content-length",
}
PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

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
            # Erstlogin oder noch keine Paperless-URL -> direkt in die Einstellungen.
            if cfg.get("is_default_pw") or not cfg.get("paperless_url"):
                return redirect(url_for("settings"))
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


@app.route("/settings", methods=["GET", "POST"])
def settings():
    cfg = load_config()
    msg = err = None
    if request.method == "POST":
        # Ein Formular speichert alles zusammen. Leere Felder behalten den alten Wert,
        # damit nichts versehentlich verloren geht.
        url = request.form.get("paperless_url", "").strip().rstrip("/")
        if url:
            cfg["paperless_url"] = url
        tok = request.form.get("paperless_token", "").strip()
        if tok:
            cfg["paperless_token"] = tok

        # Passwort nur ändern, wenn ein neues eingegeben wurde.
        new = request.form.get("new", "")
        pw_ok = True
        if new:
            cur = request.form.get("current", "")
            rep = request.form.get("repeat", "")
            if not check_password_hash(cfg["admin_pw_hash"], cur):
                err = "Aktuelles Passwort ist falsch."; pw_ok = False
            elif len(new) < 4:
                err = "Neues Passwort muss mindestens 4 Zeichen haben."; pw_ok = False
            elif new != rep:
                err = "Die neuen Passwörter stimmen nicht überein."; pw_ok = False
            else:
                cfg["admin_pw_hash"] = generate_password_hash(new)
                cfg["is_default_pw"] = False

        save_config(cfg)  # Verbindung wird immer gespeichert
        if pw_ok:
            if cfg.get("paperless_url"):
                # Token gleich gegen Paperless testen -> Klartext-Rueckmeldung wie im Generator.
                code = _test_paperless(cfg["paperless_url"], cfg.get("paperless_token", ""))
                if code == 200:
                    return redirect(url_for("index"))       # echt verbunden -> in den Generator
                elif code in (401, 403):
                    err = ("✗ Token-Fehler (HTTP %d) – Token in Paperless prüfen "
                           "(Web-UI → Mein Profil → API-Token) und neu eintragen." % code)
                elif code is None:
                    err = ("✗ Paperless nicht erreichbar unter " + cfg["paperless_url"]
                           + " – URL/Netzwerk prüfen.")
                else:
                    err = "✗ Unerwartete Antwort von Paperless: HTTP %d." % code
            else:
                msg = "Gespeichert."
        cfg = load_config()
    tok = cfg.get("paperless_token") or ""
    conn_kind, conn_text = _connection_status(cfg)
    return render_template(
        "settings.html",
        paperless_url=cfg.get("paperless_url", ""),
        has_token=bool(tok),
        token_tail=(tok[-6:] if tok and "://" not in tok else ""),
        token_looks_url=("://" in tok),
        is_default_pw=cfg.get("is_default_pw", False),
        conn_kind=conn_kind, conn_text=conn_text,
        msg=msg, err=err,
    )


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
            "paperless_token": p.get("paperless_token"),
        })
        items.append({
            "id": pid,
            "name": p.get("name") or "(ohne Name)",
            "url": p.get("paperless_url") or "",
            "has_token": bool(p.get("paperless_token")),
            "has_config": p.get("generator_config") is not None,
            "active": pid == aid,
            "conn_kind": kind, "conn_text": text,
            "history": [{"ts": ts, "label": _fmt_ts(ts)} for ts in _list_history(pid)],
        })
    items.sort(key=lambda x: x["name"].lower())
    return render_template("profiles.html", profiles=items,
                           msg=request.args.get("msg"), err=request.args.get("err"))


@app.route("/profiles", methods=["POST"])
def profiles_create():
    name = request.form.get("name", "").strip() or "Neues Profil"
    profs = load_profiles()
    pid = _new_profile_id()
    profs[pid] = {"name": name, "paperless_url": "", "paperless_token": "",
                  "generator_config": None}
    save_profiles(profs)
    set_active_profile(pid)
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
    """Alle Profile als JSON herunterladen (Disaster-Recovery / Umzug)."""
    data = json.dumps(load_profiles(), indent=2, ensure_ascii=False)
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
    return jsonify({"ok": True, "name": profs[aid].get("name")})


@app.route("/portal/inject.js")
def portal_inject_js():
    return Response(INJECT_JS, mimetype="application/javascript")


@app.route("/portal/profiles.json", methods=["GET"])
def portal_profiles_list():
    """Leichte Profil-Liste fuer den Dropdown im Generator (ohne Tokens/Config)."""
    profs = load_profiles()
    aid = _active_id()
    return jsonify({
        "active": aid,
        "profiles": [{"id": pid, "name": p.get("name") or "(ohne Name)"}
                     for pid, p in profs.items()],
    })


@app.route("/api/", defaults={"path": ""}, methods=PROXY_METHODS)
@app.route("/api/<path:path>", methods=PROXY_METHODS)
def proxy(path):  # noqa: ARG001 (path steckt schon in request.path)
    prof = active_profile()
    base = prof.get("paperless_url")
    token = prof.get("paperless_token")
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
