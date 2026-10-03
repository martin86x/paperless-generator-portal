#!/usr/bin/env python3
"""Portal-Konnektor: lokaler MCP-Server (stdio) fuer Claude Code.

Spricht das Generator-Portal mit dem Portal-API-Token an. Nur Python-Standardbibliothek,
keine Installation noetig.

Konfiguration (Umgebungsvariablen):
    PORTAL_URL         Adresse des Portals, z. B. http://<portal-ip>:8080   (Pflicht)
    PORTAL_TOKEN_FILE  Datei mit dem API-Token (Standard: ~/.portal-token,
                       unter Windows %USERPROFILE%\\.portal-token)

Das Token wird nur als Header "Authorization: Bearer" gesendet und nie ausgegeben.
Solange der Konnektor laeuft, meldet er sich alle 45 s beim Portal (Lampe im Generator).
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

SERVER_NAME = "paperless-portal"
SERVER_VERSION = "1.0.0"
HEARTBEAT_SECS = 45
MAX_BODY = 100_000  # Zeichen; laengere Antworten werden gekuerzt

PORTAL_URL = (os.environ.get("PORTAL_URL") or "").rstrip("/")
TOKEN_FILE = os.environ.get("PORTAL_TOKEN_FILE") or os.path.join(
    os.path.expanduser("~"), ".portal-token")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # Redirects nicht folgen, sondern melden
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _token():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            tok = fh.read().strip()
    except OSError:
        raise RuntimeError("Token-Datei nicht lesbar: %s" % TOKEN_FILE)
    if not tok:
        raise RuntimeError("Token-Datei ist leer: %s" % TOKEN_FILE)
    return tok


def _request(method, path, query=None, json_body=None, form=None, timeout=30):
    """HTTP-Aufruf ans Portal. Gibt (status, headers, text) zurueck."""
    if not PORTAL_URL:
        raise RuntimeError("PORTAL_URL ist nicht gesetzt.")
    url = PORTAL_URL + path
    if query:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query, doseq=True)
    headers = {"Authorization": "Bearer " + _token(), "Accept": "application/json"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read().decode("utf-8", "replace")


def _fmt(status, headers, text):
    out = "HTTP %s" % status
    loc = headers.get("Location")
    if loc:
        out += "\nLocation: %s" % loc
    if len(text) > MAX_BODY:
        text = text[:MAX_BODY] + "\n… (gekuerzt, %d Zeichen)" % len(text)
    return out + "\n\n" + text, status >= 400


# ── Werkzeuge ────────────────────────────────────────────────────────────────
def tool_status(_args):
    return _fmt(*_request("GET", "/portal/status.json"))


def tool_update(args):
    action = args.get("action") or "update"
    if action not in ("update", "rollback"):
        return "action muss 'update' oder 'rollback' sein.", True
    return _fmt(*_request("POST", "/verwaltung/update/trigger", form={"action": action}))


def tool_portal_request(args):
    method = (args.get("method") or "GET").upper()
    path = args.get("path") or ""
    if not path.startswith("/") or path.startswith("//"):
        return "path muss mit / beginnen (z. B. /portal/profiles.json).", True
    if path == "/api" or path.startswith("/api/"):
        return "Fuer Paperless das Werkzeug paperless_request nutzen.", True
    return _fmt(*_request(method, path, query=args.get("query"),
                          json_body=args.get("json"), form=args.get("form")))


def tool_paperless_request(args):
    method = (args.get("method") or "GET").upper()
    path = (args.get("path") or "").lstrip("/")
    if path.startswith("api/"):
        path = path[4:]
    if ".." in path.split("?")[0].split("/"):
        return "Ungueltiger Pfad.", True
    return _fmt(*_request(method, "/api/" + path, query=args.get("query"),
                          json_body=args.get("json"), timeout=120))


_OBJ = {"type": "object"}
TOOLS = [
    {"name": "portal_status",
     "description": "Zustand des Generator-Portals: Version, verfuegbares Update, Host-Helper, "
                    "Waechter, aktives Profil, Konnektor-Lampe und beide Schalter. Antwortet das Portal "
                    "mit 403 'Konnektor ist im Portal ausgeschaltet', hat der Besitzer ihn gesperrt.",
     "inputSchema": {"type": "object", "properties": {}},
     "fn": tool_status},
    {"name": "portal_update",
     "description": "1-Klick-Update oder Rollback des Portals anfordern. Der Host-Helper auf dem "
                    "LXC fuehrt es innerhalb etwa einer Minute aus.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["update", "rollback"]}},
         "required": ["action"]},
     "fn": tool_update},
    {"name": "portal_request",
     "description": "Beliebiger Aufruf im Portal (nicht /api). Gesperrt mit dem Token: Passwort, "
                    "Recovery-Codes, Token, Backup/Restore, Profil-Export/-Import, Paperless-URL, "
                    "'nur lesen', Profil anlegen/wechseln, Waechter/Benachrichtigungen speichern, "
                    "Anwenden, beide Schalter. "
                    "Redirects werden nicht verfolgt, sondern mit Location gemeldet.",
     "inputSchema": {"type": "object", "properties": {
         "method": {"type": "string", "enum": ["GET", "POST"]},
         "path": {"type": "string", "description": "z. B. /portal/profiles.json"},
         "query": _OBJ, "json": _OBJ,
         "form": {"type": "object", "description": "Formularfelder (statt json)"}},
         "required": ["path"]},
     "fn": tool_portal_request},
    {"name": "paperless_request",
     "description": "Aufruf der Paperless-API ueber den Portal-Proxy. Geht nur, wenn im Generator "
                    "die Schalter 'Konnektor' und 'Paperless' auf AN stehen. Dokumente loeschen ist immer gesperrt; "
                    "ein Profil auf 'nur lesen' sperrt alle Schreibzugriffe.",
     "inputSchema": {"type": "object", "properties": {
         "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
         "path": {"type": "string", "description": "relativ zu /api/, z. B. documents/?page_size=5"},
         "query": _OBJ, "json": _OBJ},
         "required": ["path"]},
     "fn": tool_paperless_request},
]
_BY_NAME = {t["name"]: t for t in TOOLS}


# ── MCP (JSON-RPC 2.0 ueber stdio, eine Nachricht je Zeile) ──────────────────
def _send(msg):
    sys.stdout.buffer.write(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def _heartbeat(stop):
    while not stop.wait(HEARTBEAT_SECS):
        try:
            _request("GET", "/portal/connector.json", timeout=10)
        except Exception:  # noqa: BLE001 - Portal kurz weg: naechster Versuch
            pass


def _handle(msg, stop):
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        if not getattr(_handle, "hb", None):
            _handle.hb = threading.Thread(target=_heartbeat, args=(stop,), daemon=True)
            _handle.hb.start()
            try:  # sofort melden, damit die Lampe nicht erst nach 45 s angeht
                _request("GET", "/portal/connector.json", timeout=10)
            except Exception:  # noqa: BLE001
                pass
        return {"protocolVersion": want, "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{k: v for k, v in t.items() if k != "fn"} for t in TOOLS]}
    if method == "tools/call":
        params = msg.get("params") or {}
        tool = _BY_NAME.get(params.get("name"))
        if tool is None:
            raise _RpcError(-32602, "Unbekanntes Werkzeug: %s" % params.get("name"))
        try:
            text, is_err = tool["fn"](params.get("arguments") or {})
        except Exception as exc:  # noqa: BLE001 - als Werkzeug-Fehler melden
            text, is_err = "Fehler: %s" % exc, True
        return {"content": [{"type": "text", "text": text}], "isError": bool(is_err)}
    if mid is None:
        return None  # Benachrichtigung (z. B. notifications/initialized)
    raise _RpcError(-32601, "Methode nicht unterstuetzt: %s" % method)


class _RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def main():
    stop = threading.Event()
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "Parse error"}})
            continue
        if not isinstance(msg, dict):
            continue
        mid = msg.get("id")
        try:
            result = _handle(msg, stop)
        except _RpcError as exc:
            if mid is not None:
                _send({"jsonrpc": "2.0", "id": mid,
                       "error": {"code": exc.code, "message": str(exc)}})
            continue
        if mid is not None and "method" in msg:
            _send({"jsonrpc": "2.0", "id": mid, "result": result})
    stop.set()


if __name__ == "__main__":
    main()
