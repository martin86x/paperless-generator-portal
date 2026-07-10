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

import requests
from flask import (Flask, Response, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
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

# Vorkonfig-Injektion: zwingt den Generator auf same-origin (er arbeitet dann ueber /api/,
# der Proxy spritzt den Token ein -> kein CORS). location.origin wird im Browser ausgewertet.
#
# Wichtig: Der Generator stellt ~900 ms nach dem Laden die letzte Sitzung wieder her
# (LS_KEY 'paperless_gen_cfg_v2') und schreibt dabei die zuletzt genutzte (direkte)
# Paperless-URL + Token zurueck in die Felder. Damit das die same-origin-Vorkonfig nicht
# ueberschreibt, patchen wir BEIDE localStorage-Eintraege synchron im <head> (bevor der
# Generator sie liest) und setzen die Felder nach der Wiederherstellung final auf origin.
INJECT = (
    "<script>(function(){"
    "var o=location.origin;"
    "try{localStorage.setItem('plx_conn_preset',JSON.stringify({url:o,token:''}));}catch(e){}"
    "try{var K='paperless_gen_cfg_v2',r=localStorage.getItem(K);"
    "if(r){var c=JSON.parse(r);c.url=o;c.token='';localStorage.setItem(K,JSON.stringify(c));}}catch(e){}"
    "window.addEventListener('load',function(){"
    "try{if(!document.getElementById('plx-portal-nav')){"
    "var n=document.createElement('div');n.id='plx-portal-nav';"
    "n.style.cssText='position:fixed;top:8px;right:10px;z-index:2147483647;display:flex;gap:6px;font-family:system-ui,sans-serif';"
    "var mk=function(h,txt,col){var a=document.createElement('a');a.href=h;a.textContent=txt;"
    "a.style.cssText='background:#1f232c;color:'+col+';border:1px solid #2b303b;border-radius:6px;padding:5px 10px;font-size:12px;text-decoration:none';return a;};"
    "n.appendChild(mk(o+'/settings','⚙ Einstellungen','#60a5fa'));"
    "n.appendChild(mk(o+'/logout','Logout','#9aa4b2'));"
    "document.body.appendChild(n);}}catch(e){}"
    "setTimeout(function(){try{"
    "if(typeof _parseUrlToFields==='function')_parseUrlToFields(o);"
    "var t=document.getElementById('inp-token');if(t)t.value='';"
    "if(typeof testConnection==='function')testConnection();"
    "}catch(e){}},1200);});"
    "})();</script>"
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


def build_index_html():
    """Generator-HTML einlesen und die Vorkonfig-Zeile vor </head> einfuegen."""
    with open(os.path.join(SITE_DIR, "index.html"), encoding="utf-8") as fh:
        html = fh.read()
    idx = html.lower().find("</head>")
    if idx != -1:
        return html[:idx] + INJECT + html[idx:]
    return INJECT + html


_cfg0 = init_config()
INDEX_HTML = build_index_html()

app = Flask(__name__)
app.secret_key = _cfg0["secret"]

PUBLIC_ENDPOINTS = {"login", "healthz", "static"}


@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if not session.get("logged_in"):
        if request.path.startswith("/api"):
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
        cfg = load_config()
        user = request.form.get("username", "")
        pw = request.form.get("password", "")
        if user == cfg.get("admin_user") and check_password_hash(cfg["admin_pw_hash"], pw):
            session["logged_in"] = True
            # Erstlogin oder noch keine Paperless-URL -> direkt in die Einstellungen.
            if cfg.get("is_default_pw") or not cfg.get("paperless_url"):
                return redirect(url_for("settings"))
            return redirect(url_for("index"))
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


@app.route("/api/", defaults={"path": ""}, methods=PROXY_METHODS)
@app.route("/api/<path:path>", methods=PROXY_METHODS)
def proxy(path):  # noqa: ARG001 (path steckt schon in request.path)
    cfg = load_config()
    base = cfg.get("paperless_url")
    token = cfg.get("paperless_token")
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
