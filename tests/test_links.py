"""Link-Test: fuehren alle Links, Knoepfe, Weiterleitungen und Sprungmarken an ein Ziel, das es gibt?

Prueft statisch (Quelltext gegen Routen/IDs) und dynamisch (jede GET-Seite und jeder
Verwaltungs-Reiter laesst sich oeffnen). KEIN Netzwerk: requests ist gestubbt.

    .venv/Scripts/python tests/test_links.py
"""
import os
import re
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
os.environ["CONFIG_DIR"] = tempfile.mkdtemp(prefix="portal-links-")
os.environ["SITE_DIR"] = os.path.join(_ROOT, "site")
os.environ["PORTAL_WATCHER"] = "0"
sys.path.insert(0, os.path.join(_ROOT, "app"))

import app as A  # noqa: E402

_fails = []
_count = [0]


def check(name, cond):
    _count[0] += 1
    if not cond:
        _fails.append(name)
        print("  FAIL  " + name)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class Resp:
    status_code = 200
    headers = {}
    content = b"{}"
    text = "{}"

    def json(self):
        return {"count": 0, "results": [], "next": None}


A.requests.get = lambda *a, **k: Resp()
A.requests.post = lambda *a, **k: Resp()

APP_PY = read(os.path.join(_ROOT, "app", "app.py"))
INJECT = read(os.path.join(_ROOT, "app", "inject.js"))
TPL_DIR = os.path.join(_ROOT, "app", "templates")
TEMPLATES = {n: read(os.path.join(TPL_DIR, n)) for n in sorted(os.listdir(TPL_DIR)) if n.endswith(".html")}
ALL_SRC = {"app.py": APP_PY, "inject.js": INJECT}
ALL_SRC.update(TEMPLATES)

ENDPOINTS = set(A.app.view_functions)
TABS = {t[0] for t in A._VERW_TABS} | {g[0] for g in A._VERW_GROUPS}
RULES = list(A.app.url_map.iter_rules())


def path_exists(path):
    """Gibt es eine Route fuer diesen Pfad (beliebige Methode)? Platzhalter-Segmente erlaubt."""
    adapter = A.app.url_map.bind("localhost")
    for m in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        try:
            adapter.match(path, method=m)
            return True
        except Exception:  # noqa: BLE001 (NotFound / MethodNotAllowed / RequestRedirect)
            if "RequestRedirect" in repr(sys.exc_info()[0]):
                return True
    return False


# ── 1. url_for-Ziele (Endpunkte) ─────────────────────────────────────────────
print("url_for -> Endpunkte")
for fname, src in ALL_SRC.items():
    for m in re.finditer(r"url_for\(\s*['\"]([a-zA-Z0-9_.]+)['\"]", src):
        ep = m.group(1)
        check("%s: url_for('%s') hat keinen Endpunkt" % (fname, ep), ep in ENDPOINTS or ep == "static")

# ── 2. Reiter-Namen ──────────────────────────────────────────────────────────
print("Reiter (?tab= / tab=) -> bekannte Reiter")
for fname, src in ALL_SRC.items():
    for m in re.finditer(r"""tab\s*=\s*['"]([a-z_]+)['"]""", src):
        check("%s: unbekannter Reiter '%s'" % (fname, m.group(1)), m.group(1) in TABS)
    for m in re.finditer(r"\?tab=([a-z_]+)", src):
        check("%s: unbekannter Reiter '?tab=%s'" % (fname, m.group(1)), m.group(1) in TABS)
for m in re.finditer(r"""_recovery_redirect\(\s*['"]([a-z_]+)['"]""", APP_PY):
    check("app.py: _recovery_redirect auf unbekannten Reiter '%s'" % m.group(1), m.group(1) in TABS)
for g in A._VERW_GROUPS:
    for member in g[2]:
        check("Gruppe %s: Mitglied %s ist kein Reiter" % (g[0], member), member in {t[0] for t in A._VERW_TABS})
for t in A._VERW_TABS:
    check("Reiter %s: Endpunkt %s fehlt" % (t[0], t[2]), t[2] in ENDPOINTS)
    check("Reiter %s: keine Beschreibung/Gruppe" % t[0], any(t[0] in g[2] for g in A._VERW_GROUPS))

# ── 3. Feste Pfade in Quelltext (href, fetch, location, redirect) ────────────
print("feste Pfade -> Routen")
PATH_RE = re.compile(r"""(?:href=|action=|location\.href\s*=\s*|redirect\(\s*|window\.open\()\s*['"](/[A-Za-z0-9_\-/\.]*)""")
# fetch('/a/' + id + '/b?x=1', ...): Zeichenketten zusammensetzen, dynamische Teile -> "x"
FETCH_RE = re.compile(r"""fetch\(\s*((?:'[^']*'|"[^"]*"|\+|[A-Za-z_][A-Za-z_0-9\.]*(?:\([^)]*\))?|\s)+)""")
TOKEN_RE = re.compile(r"""'([^']*)'|"([^"]*)"|([A-Za-z_][A-Za-z_0-9\.]*(?:\([^)]*\))?)""")


FETCHED = []


def fetch_path(expr):
    out = ""
    for m in TOKEN_RE.finditer(expr):
        lit = m.group(1) if m.group(1) is not None else m.group(2)
        out += lit if lit is not None else "x"
    return out.split("?")[0]


for fname, src in ALL_SRC.items():
    for m in PATH_RE.finditer(src):
        p = m.group(1)
        if p == "/" or p.startswith("/static"):
            continue
        probe = p if not p.endswith("/") else p + "x"
        check("%s: Pfad %s hat keine Route" % (fname, p), path_exists(probe) or path_exists(p))
    for m in FETCH_RE.finditer(src):
        p = fetch_path(m.group(1))
        if not p.startswith("/") or p == "/" or p.startswith("/static"):
            continue
        FETCHED.append(p)
        check("%s: fetch-Pfad %s hat keine Route" % (fname, p), path_exists(p))

check("Scanner findet fetch-Aufrufe (%d)" % len(FETCHED), len(FETCHED) >= 10 and "/profiles/x/snapshots" in FETCHED)

# ── 4. Anker innerhalb einer Seite ───────────────────────────────────────────
print("Anker (#id) -> id im selben Template")
for fname, src in TEMPLATES.items():
    ids = set(re.findall(r"""\bid=['"]([^'"{}]+)['"]""", src))
    for m in re.finditer(r"""href=['"]#([A-Za-z0-9_\-]+)['"]""", src):
        check("%s: Anker #%s ohne id" % (fname, m.group(1)), m.group(1) in ids)

# ── 5. Einrichtungs-Checkliste: jeder Schritt fuehrt auf einen Reiter ────────
print("Einrichtungs-Checkliste")
A.app.config["TESTING"] = True


def logged_in_client():
    c = A.app.test_client()
    A.save_config({"admin_user": "admin", "admin_pw_hash": A.generate_password_hash("pw12345"),
                   "paperless_url": "", "paperless_token": "", "secret": "s3", "is_default_pw": False,
                   "active_profile": "9d5a263844e6fb1c"})
    A._cfg0.clear()
    A._cfg0.update(A.load_config())
    A.save_profiles({"9d5a263844e6fb1c": {"name": "T", "paperless_url": "http://stub.invalid:8000",
                                         "paperless_token": "tok", "generator_config": None,
                                         "productive": False, "readonly": False, "color": ""}})
    with c.session_transaction() as s:
        s["logged_in"] = True
        s["sid"] = A._sessions_add()
        s["active_profile"] = "9d5a263844e6fb1c"
    return c


c = logged_in_client()
page = c.get("/verwaltung/einrichtung?embed=1").data.decode("utf-8")
step_tabs = re.findall(r'href="/verwaltung\?tab=([a-z_]+)"', page)
check("Checkliste enthält Schritt-Links", len(step_tabs) >= 5)
for t in step_tabs:
    check("Checkliste: Reiter %s unbekannt" % t, t in TABS)
    r = c.get("/verwaltung?tab=" + t)
    check("Checkliste: /verwaltung?tab=%s liefert %s" % (t, r.status_code), r.status_code == 200)


# ── 5b. Formulare und Links: richtige Methode, Felder werden gelesen, Handler vorhanden ──
print("Formulare / Handler")
import inspect  # noqa: E402

FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.S)
for fname, src in TEMPLATES.items():
    for m in FORM_RE.finditer(src):
        attrs, inner = m.group(1), m.group(2)
        act = re.search(r"""action=['"]\{\{\s*url_for\(\s*['"]([a-z_0-9]+)['"]""", attrs)
        meth = (re.search(r"""method=['"]([a-zA-Z]+)['"]""", attrs) or [None, "get"])[1].upper()
        if not act:
            continue
        ep = act.group(1)
        if ep not in ENDPOINTS:
            continue
        rules = [r for r in RULES if r.endpoint == ep]
        check("%s: Formular -> %s sendet %s, Route erlaubt %s" % (fname, ep, meth, sorted(rules[0].methods)),
              any(meth in r.methods for r in rules))
        # Felder: jedes benannte Eingabefeld sollte vom Handler gelesen werden
        try:
            body = inspect.getsource(A.app.view_functions[ep])
        except (OSError, TypeError):
            continue
        for fm in re.finditer(r"""<(?:input|select|textarea)\b[^>]*\bname=['"]([a-z_0-9\-]+)['"]""", inner):
            field = fm.group(1)
            if field in ("csrf", "csrf_token"):
                continue
            check("%s: Feld '%s' (Formular -> %s) wird im Handler nicht gelesen" % (fname, field, ep),
                  field in body or ("request.form" in body and "dict(" in body))
    for m in re.finditer(r"""<a\b[^>]*href=['"]\{\{\s*url_for\(\s*['"]([a-z_0-9]+)['"]""", src):
        ep = m.group(1)
        if ep in ENDPOINTS:
            check("%s: Link auf %s braucht GET" % (fname, ep),
                  any("GET" in r.methods for r in RULES if r.endpoint == ep))
    for m in re.finditer(r"""on(?:click|change|submit|input)=['"]\s*(?:return\s+)?([A-Za-z_][A-Za-z_0-9]*)\(""", src):
        fn = m.group(1)
        if fn in ("confirm", "alert", "if", "function"):
            continue
        defined = re.search(r"(?:function\s+%s\b|%s\s*=\s*function|window\.%s\s*=)" % (fn, fn, fn),
                            src + TEMPLATES.get("base.html", "") + INJECT)
        check("%s: Handler %s() ist nirgends definiert" % (fname, fn), bool(defined))

# ── 6b. Jeder POST-Endpunkt antwortet auf leere Eingabe ohne Absturz ──────────
print("POST-Rauchtest (leere Eingabe, nur lokale Testdaten)")
POST_SKIP = {"logout", "proxy", "login", "login_recovery", "wizard"}
PID = "9d5a263844e6fb1c"
for rule in RULES:
    if "POST" not in rule.methods or rule.endpoint in POST_SKIP:
        continue
    path = re.sub(r"<[^>]*:?pid>", PID, rule.rule)
    path = re.sub(r"<[^>]*ts>", "20200101-000000", path)
    path = re.sub(r"<[^>]+>", "x", path)
    c = logged_in_client()
    r = c.post(path, data={}, headers={"Origin": "http://localhost"}, base_url="http://localhost")
    check("POST %s (%s): Status %s" % (rule.rule, rule.endpoint, r.status_code), r.status_code < 500)

# ── 6. Jeder Reiter und jedes Fragment laesst sich oeffnen ───────────────────
print("Verwaltungs-Reiter")
for tid, _label, ep in A._VERW_TABS:
    shell = c.get("/verwaltung?tab=" + tid)
    check("Shell ?tab=%s: Status %s" % (tid, shell.status_code), shell.status_code == 200)
    html = shell.data.decode("utf-8")
    check("Shell ?tab=%s: Reiter-Panel vorhanden" % tid, ('id="vpart-%s"' % tid) in html)
    frag = c.get(A.app.url_map.bind("localhost").build(ep, {"embed": 1}))
    check("Fragment %s: Status %s" % (ep, frag.status_code), frag.status_code == 200)

# ── 7. Alle GET-Seiten ohne Parameter duerfen nicht abstuerzen ───────────────
print("alle GET-Seiten")
SKIP = {"logout", "static", "proxy", "update_page", "notfall_pdf"}
for rule in RULES:
    if "GET" not in rule.methods or rule.arguments or rule.endpoint in SKIP:
        continue
    r = c.get(rule.rule)
    check("GET %s (%s): Status %s" % (rule.rule, rule.endpoint, r.status_code), r.status_code < 500)
# Redirect-Ziele (302) muessen selbst existieren
for rule in RULES:
    if "GET" not in rule.methods or rule.arguments or rule.endpoint in SKIP:
        continue
    r = c.get(rule.rule)
    if r.status_code in (301, 302, 303, 307):
        loc = r.headers.get("Location", "")
        p = re.sub(r"^https?://[^/]+", "", loc).split("?")[0]
        check("Weiterleitung %s -> %s: Ziel unbekannt" % (rule.rule, loc), path_exists(p))

# ── 8. Generator: Sprungmarken und Seitenleiste ──────────────────────────────
print("Generator (site/index.html)")
SITE = read(os.path.join(_ROOT, "site", "index.html"))
site_ids = set(re.findall(r"""\bid=["']([^"']+)["']""", SITE))
tool_ids = {"s-stats", "s-instanz-import", "s-match-tester", "s-wf-preview", "s-asn", "s-jsondiff"}
for m in re.finditer(r"""goTo\(\s*['"]([A-Za-z0-9_\-]+)['"]""", SITE + INJECT):
    check("Generator goTo('%s'): id fehlt" % m.group(1), m.group(1) in site_ids or m.group(1) in tool_ids)
for m in re.finditer(r"""data-sid=["']([^"']+)["']""", SITE):
    check("Seitenleiste data-sid='%s': Sektion fehlt" % m.group(1), m.group(1) in site_ids)
for m in re.finditer(r"""href=["']#([A-Za-z0-9_\-]+)["']""", SITE):
    check("Generator-Anker #%s ohne id" % m.group(1), m.group(1) in site_ids)
for sid in ("s-tools", "s-conn", "s-instanz-import", "import-log-wrap", "import-log"):
    check("inject.js/Blanko-Knöpfe brauchen id %s" % sid, sid in site_ids)
for m in re.finditer(r"""switchToolsTab\(\s*['"]([a-z]+)['"]""", SITE + INJECT):
    check("switchToolsTab('%s'): Reiter fehlt" % m.group(1), ("tab-" + m.group(1)) in site_ids)
for m in re.finditer(r"""_BLANK_HIDE\s*=\s*\[([^\]]*)\]""", INJECT):
    for sid in re.findall(r"'([^']+)'", m.group(1)):
        check("Blanko-Ausblendliste: id %s existiert nicht" % sid, sid in site_ids)

print("\n%d Prüfungen, %d Fehler" % (_count[0], len(_fails)))
for f in _fails:
    print("  - " + f)
sys.exit(1 if _fails else 0)
