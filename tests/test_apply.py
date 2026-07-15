"""Tests fuer /anwenden (Schreibpfad gegen Paperless) + Rueckgaengig + Instanz-Snapshot.

SICHERHEIT: Es geht KEIN Request ins Netz. requests.get/post/delete sind durch einen
gestubbten Upstream ersetzt (Muster aus tests/test_proxy.py). Die echte Instanz .200
wird NIEMALS beschrieben — dieser Pfad ist der einzige im Portal, der fremde Daten
anlegt und loescht, deshalb laeuft er hier ausschliesslich gegen den Stub.

    .venv/Scripts/python tests/test_apply.py
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_CFG = tempfile.mkdtemp(prefix="portal-apply-")
os.environ["CONFIG_DIR"] = _CFG
os.environ["SITE_DIR"] = os.path.join(os.path.dirname(_HERE), "site")
os.environ["PORTAL_WATCHER"] = "0"
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "app"))

import app as A  # noqa: E402

_fails = []
_count = [0]


def check(name, cond):
    _count[0] += 1
    if not cond:
        _fails.append(name)
        print("  FAIL  " + name)


def eq(name, got, want):
    check("%s (erwartet %r, war %r)" % (name, want, got), got == want)


PID = "9d5a263844e6fb1c"
TOKEN = "382810ecd86741419d580e029d121b7a5537f602"
URL = "http://stub.invalid:8000"
PW = "geheim123"
PW_HASH = A.generate_password_hash(PW)


# ── Gestubbter Paperless-Upstream ────────────────────────────────────────────
class Resp:
    def __init__(self, code, body=None):
        self.status_code = code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class Srv:
    """Minimal-Paperless: haelt Objekte je Endpunkt, vergibt IDs, protokolliert alles."""

    def __init__(self):
        self.data = {"tags": [], "document_types": [], "correspondents": [],
                     "storage_paths": [], "custom_fields": []}
        self._next = 100
        self.posts = []      # (endpoint, payload)
        self.deletes = []    # (endpoint, id)
        self.gets = []
        self.get_down = False    # GET wirft Netzfehler
        self.post_fail = set()   # Endpunkte, deren POST scheitert

    def seed(self, ep, name, oid=None):
        oid = oid if oid is not None else self._take_id()
        self.data[ep].append({"id": oid, "name": name})
        return oid

    def _take_id(self):
        self._next += 1
        return self._next

    @staticmethod
    def _ep(url):
        tail = url.split("/api/", 1)[1]
        return tail.split("?", 1)[0].strip("/")

    def get(self, url, **kw):
        self.gets.append(url)
        if self.get_down:
            raise A.requests.RequestException("Netz weg")
        ep = self._ep(url)
        if "/" in ep:  # Einzelobjekt: /api/tags/101/
            base, oid = ep.rsplit("/", 1)
            hit = [r for r in self.data.get(base, []) if str(r.get("id")) == oid]
            return Resp(200, hit[0]) if hit else Resp(404)
        return Resp(200, {"results": list(self.data.get(ep, [])), "next": None})

    def post(self, url, **kw):
        ep = self._ep(url)
        pl = kw.get("json") or {}
        self.posts.append((ep, pl))
        if ep in self.post_fail:
            return Resp(400)
        oid = self._take_id()
        row = dict(pl)
        row["id"] = oid
        self.data.setdefault(ep, []).append(row)
        return Resp(201, row)

    def delete(self, url, **kw):
        tail = url.split("/api/", 1)[1].strip("/")
        ep, oid = tail.rsplit("/", 1)
        self.deletes.append((ep, int(oid)))
        self.data[ep] = [r for r in self.data.get(ep, []) if r.get("id") != int(oid)]
        return Resp(204)


SRV = Srv()
A.requests.get = lambda url, **kw: SRV.get(url, **kw)
A.requests.post = lambda url, **kw: SRV.post(url, **kw)
A.requests.delete = lambda url, **kw: SRV.delete(url, **kw)


# ── Fixtures ─────────────────────────────────────────────────────────────────
def gc_simple():
    """Generator-Config wie sie der Direkt-Modus liefert."""
    return {
        "tags": [{"name": "Finanzen", "color": "#ff0000", "children": [
            {"name": "Rechnung", "color": "#00ff00"},
            {"name": "Mahnung"},
        ]}],
        "tagMatch": [{"name": "Rechnung", "algo": 1, "match": "invoice"}],
        "types": [{"name": "Vertrag"}],
        "correspondents": [{"name": "Telekom"}],
        "storagePaths": [{"name": "Archiv", "path": "arch/{created_year}"}],
    }


def reset(gc=None, readonly=False, url=URL):
    global SRV
    SRV = Srv()
    A.save_config({"admin_user": "admin", "admin_pw_hash": PW_HASH, "paperless_url": "",
                   "paperless_token": "", "secret": "s3cr3t", "is_default_pw": False,
                   "active_profile": PID})
    A._cfg0.clear()
    A._cfg0.update(A.load_config())
    A.save_profiles({PID: {"name": "Stub", "paperless_url": url, "paperless_token": TOKEN,
                           "generator_config": gc_simple() if gc is None else gc,
                           "productive": False, "readonly": readonly, "color": ""}})
    for d in (A.UNDO_DIR, A.SNAP_DIR):
        if os.path.isdir(d):
            for root, _dirs, files in os.walk(d):
                for f in files:
                    os.remove(os.path.join(root, f))


A.app.config["TESTING"] = True


def client(logged_in=True):
    c = A.app.test_client()
    if logged_in:
        with c.session_transaction() as s:
            s["logged_in"] = True
            s["active_profile"] = PID
    return c


def snap_files():
    d = os.path.join(A.SNAP_DIR, PID)
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


# ── 1. Payload-Bau: spiegelt der Server den Direkt-Modus? ────────────────────
print("Payload-Bau (_gc_apply_entries)")
reset()
ents = A._gc_apply_entries(gc_simple(), "tags")
eq("Tags: Eltern + 2 Kinder", [e["name"] for e in ents], ["Finanzen", "Rechnung", "Mahnung"])
eq("das Eltern-Tag kommt ZUERST (Kinder brauchen dessen ID)", ents[0]["name"], "Finanzen")
eq("Eltern kennt kein parent_name", ents[0]["parent_name"], None)
eq("Kind verweist auf den Eltern-Namen", ents[1]["parent_name"], "Finanzen")
eq("Farbe wandert in die Payload", ents[0]["payload"].get("color"), "#ff0000")
eq("ohne tagMatch bleibt matching_algorithm 0", ents[2]["payload"]["matching_algorithm"], 0)
eq("mit tagMatch greift der Algorithmus", ents[1]["payload"]["matching_algorithm"], 1)
eq("und der Match-Text", ents[1]["payload"]["match"], "invoice")
check("Match ist unabhängig von Groß/Klein", ents[1]["payload"]["is_insensitive"] is True)
sp = A._gc_apply_entries(gc_simple(), "storagePaths")
eq("Speicherpfad: das path-Feld wird mitgegeben", sp[0]["payload"]["path"], "arch/{created_year}")
eq("Speicherpfad landet auf storage_paths/", sp[0]["endpoint"], "storage_paths/")


# ── 2. GET /anwenden: Riegel und Vorschau ────────────────────────────────────
print("GET /anwenden — Riegel")
reset(readonly=True)
page = client().get("/anwenden").data.decode("utf-8")
check("„nur lesen“ blockt die Seite", "nur lesen" in page)
eq("und fragt die Instanz gar nicht erst ab", len(SRV.gets), 0)

reset(gc={})
page = client().get("/anwenden").data.decode("utf-8")
check("ohne gespeicherte Konfiguration wird geblockt", "keine Konfiguration" in page)

reset(url="")
r = client().get("/anwenden")
# Nicht der Block-Text von /anwenden greift, sondern schon das Setup-Gate davor.
eq("ohne URL fängt das Setup-Gate den Aufruf vorher ab",
   (r.status_code, r.headers.get("Location")), (302, "/wizard"))

reset()
SRV.get_down = True
page = client().get("/anwenden").data.decode("utf-8")
check("unerreichbare Instanz wird gemeldet statt geraten", "nicht erreichbar" in page)

print("GET /anwenden — Vorschau")
reset()
SRV.seed("tags", "Finanzen")
page = client().get("/anwenden").data.decode("utf-8")
check("bereits vorhandenes Tag taucht NICHT als fehlend auf",
      page.count("Finanzen") == 0 or "value=\"tags|Finanzen\"" not in page)
check("fehlendes Tag wird angeboten", "tags|Rechnung" in page)
check("fehlender Korrespondent wird angeboten", "correspondents|Telekom" in page)
eq("es wurde nichts geschrieben (reine Vorschau)", SRV.posts, [])


# ── 3. POST /anwenden: Passwort-Riegel ───────────────────────────────────────
print("POST /anwenden — Passwort")
reset()
r = client().post("/anwenden", data={"password": "falsch", "item": "tags|Finanzen"})
eq("falsches Passwort -> Redirect", r.status_code, 302)
eq("und es wird NICHTS angelegt", SRV.posts, [])
eq("kein Snapshot bei falschem Passwort", snap_files(), [])

reset()
r = client().post("/anwenden", data={"password": PW})
eq("nichts angehakt -> Redirect", r.status_code, 302)
eq("und nichts angelegt", SRV.posts, [])

reset(readonly=True)
r = client().post("/anwenden", data={"password": PW, "item": "tags|Finanzen"})
eq("„nur lesen“ blockt auch den POST", SRV.posts, [])


# ── 4. POST /anwenden: der Erfolgsfall ───────────────────────────────────────
print("POST /anwenden — anlegen")
reset()
r = client().post("/anwenden", data={"password": PW,
                                     "item": ["tags|Finanzen", "tags|Rechnung",
                                              "correspondents|Telekom"]})
eq("Erfolg -> Seite", r.status_code, 200)
eq("genau die drei angehakten Einträge werden angelegt", len(SRV.posts), 3)
eq("Eltern-Tag zuerst", SRV.posts[0][1]["name"], "Finanzen")
eq("dann das Kind", SRV.posts[1][1]["name"], "Rechnung")
parent_id = SRV.posts[0][1] and [r_ for r_ in SRV.data["tags"] if r_["name"] == "Finanzen"][0]["id"]
eq("das Kind wird mit der ID des frisch angelegten Eltern-Tags verknüpft",
   SRV.posts[1][1].get("parent"), parent_id)
eq("nicht Angehaktes bleibt unangetastet",
   [p for p in SRV.posts if p[1]["name"] == "Mahnung"], [])
undo = A._load_undo(PID)
eq("die Undo-Liste hält alle drei", len(undo), 3)
eq("mit Endpunkt", undo[0]["endpoint"], "tags/")
check("und echter ID", all(u["id"] for u in undo))

print("POST /anwenden — vorhandenes Eltern-Tag")
reset()
pid_par = SRV.seed("tags", "Finanzen")
client().post("/anwenden", data={"password": PW, "item": ["tags|Rechnung"]})
eq("nur das Kind wird angelegt", len(SRV.posts), 1)
eq("verknüpft mit dem BEREITS vorhandenen Eltern-Tag", SRV.posts[0][1].get("parent"), pid_par)

print("POST /anwenden — Fehler vom Upstream")
reset()
SRV.post_fail = {"correspondents"}
r = client().post("/anwenden", data={"password": PW,
                                     "item": ["tags|Finanzen", "correspondents|Telekom"]})
page = r.data.decode("utf-8")
check("der Upstream-Fehler wird gemeldet", "HTTP 400" in page)
eq("das Gelungene bleibt in der Undo-Liste", [u["name"] for u in A._load_undo(PID)], ["Finanzen"])

print("POST /anwenden — fehlendes Eltern-Tag")
reset()
r = client().post("/anwenden", data={"password": PW, "item": ["tags|Rechnung"]})
check("Kind ohne Eltern-Tag wird gemeldet statt verwaist angelegt",
      "Eltern-Tag" in r.data.decode("utf-8"))
eq("und es wird nichts geschrieben", SRV.posts, [])


# ── 4b. BEFUND: ein Haken legte mehrere Objekte an ───────────────────────────
print("BEFUND: Namensdublette in der Config")
GC_DUP = {"tags": [], "tagMatch": [], "types": [],
          "correspondents": [{"name": "Telekom"}, {"name": "Telekom"}], "storagePaths": []}
reset(gc=GC_DUP)
page = client().get("/anwenden").data.decode("utf-8")
eq("die Vorschau zeigt genau EINEN Haken", page.count('value="correspondents|Telekom"'), 1)
client().post("/anwenden", data={"password": PW, "item": ["correspondents|Telekom"]})
eq("ein Haken legt genau EIN Objekt an (vorher: zwei)", len(SRV.posts), 1)
eq("keine Dublette in der Instanz", [c["name"] for c in SRV.data["correspondents"]], ["Telekom"])
eq("und die Undo-Liste hält genau einen Eintrag", len(A._load_undo(PID)), 1)


# ── 5. Instanz-Snapshot ──────────────────────────────────────────────────────
print("Instanz-Snapshot")
reset()
SRV.seed("tags", "Bestand")
SRV.seed("correspondents", "AltKunde")
client().post("/anwenden", data={"password": PW, "item": ["tags|Finanzen"]})
files = snap_files()
eq("vor dem Schreiben wird genau ein Snapshot abgelegt", len(files), 1)
snap = json.load(open(os.path.join(A.SNAP_DIR, PID, files[0]), encoding="utf-8"))
eq("er hält den Ist-Stand VOR dem Schreiben (Tags)", [r_["name"] for r_ in snap["tags"]], ["Bestand"])
check("das frisch Angelegte steht NICHT im Snapshot",
      "Finanzen" not in [r_["name"] for r_ in snap["tags"]])
eq("und den Ist-Stand der Korrespondenten",
   [r_["name"] for r_ in snap["correspondents"]], ["AltKunde"])

# Die Snapshots halten je den KOMPLETTEN Objekt-Bestand -> ohne Kappung waechst das Volume.
reset()
for i in range(A.SNAP_MAX + 3):
    A._save_instance_snapshot(PID, {"tags": [{"id": i, "name": "t%d" % i}]})
eq("die Snapshots werden auf SNAP_MAX gekappt (vorher: unbegrenzt)",
   len(snap_files()), A.SNAP_MAX)


def _snap_vals():
    d = os.path.join(A.SNAP_DIR, PID)
    return sorted(int(json.load(open(os.path.join(d, f), encoding="utf-8"))["tags"][0]["id"])
                  for f in os.listdir(d))


eq("gekappt werden die ÄLTESTEN — nicht die lexikografisch kleinsten",
   _snap_vals(), list(range(3, A.SNAP_MAX + 3)))

# Derselbe Sortier-Defekt steckte im Bestandscode der Config-Historie (_snapshot_history):
# das Kollisions-Suffix bricht die lexikografische Ordnung ('-10' < '-2', und die Datei
# ohne Suffix sortiert ans Ende). Belegt: von 21 Ständen flog Stand 1 statt Stand 0.
print("Sortier-Schlüssel (_ts_key)")
eq("Datei ohne Suffix ist die erste ihrer Sekunde", A._ts_key("20260715T120000.json"),
   ("20260715T120000", 0))
eq("Kollisions-Suffix wird als Zahl gewertet, nicht als Text",
   A._ts_key("20260715T120000-10.json"), ("20260715T120000", 10))
check("-2 kommt dadurch vor -10 (lexikografisch wäre es umgekehrt)",
      A._ts_key("20260715T120000-2.json") < A._ts_key("20260715T120000-10.json"))
check("und die suffixlose Datei vor allen Suffixen ihrer Sekunde",
      A._ts_key("20260715T120000.json") < A._ts_key("20260715T120000-1.json"))
eq("kaputte Namen kippen die Sortierung nicht", A._ts_key("20260715T120000-xx.json"),
   ("20260715T120000", 0))

hpid = "hist-test"
for i in range(A.HISTORY_MAX + 1):  # 21 Stände in derselben Sekunde
    A._snapshot_history(hpid, {"v": i})
hd = A._history_dir(hpid)
vals = sorted(json.load(open(os.path.join(hd, f), encoding="utf-8"))["v"]
              for f in os.listdir(hd))
eq("Config-Historie: jetzt fliegt der ÄLTESTE Stand (vorher: Stand 1)",
   vals, list(range(1, A.HISTORY_MAX + 1)))
hs = A._list_history(hpid)
eq("und die Anzeige führt den jüngsten Stand oben",
   json.load(open(os.path.join(hd, hs[0] + ".json"), encoding="utf-8"))["v"], A.HISTORY_MAX)

# Ehrlichkeit: die Snapshots sind lesbar, aber es gibt bewusst KEINEN Restore aus ihnen.
# Kein Schreibweg heisst hier konkret: die Snapshot-Routen nehmen nur GET entgegen.
_snap_rules = [r for r in A.app.url_map.iter_rules() if "snapshot" in (r.endpoint or "")]
check("es gibt Snapshot-Routen (Liste + Download)", len(_snap_rules) == 2)
for _r in _snap_rules:
    eq("%s ist GET-only — kein Weg, aus einem Snapshot zu schreiben" % _r.endpoint,
       sorted(_r.methods - {"HEAD", "OPTIONS"}), ["GET"])
_tpl = open(os.path.join(os.path.dirname(_HERE), "app", "templates", "anwenden.html"),
            encoding="utf-8").read()
check("und die Anwenden-Seite verspricht keinen Restore-Punkt mehr", "Restore-Punkt" not in _tpl)


# ── 5b. Leerläufe hinterlassen keinen Snapshot ───────────────────────────────
print("Snapshot bei Leerlauf")
reset()
SRV.post_fail = {"tags"}  # nichts geht durch
client().post("/anwenden", data={"password": PW, "item": ["tags|Finanzen"]})
eq("ein Lauf, der nichts anlegt, hinterlässt KEINEN Snapshot (er dokumentiert nichts)",
   snap_files(), [])
SRV.post_fail = set()
client().post("/anwenden", data={"password": PW, "item": ["tags|Finanzen"]})
eq("ein erfolgreicher Lauf schon", len(snap_files()), 1)

reset()
SRV.post_fail = {"correspondents"}  # Teilerfolg: Tag geht, Korrespondent nicht
client().post("/anwenden", data={"password": PW,
                                 "item": ["tags|Finanzen", "correspondents|Telekom"]})
eq("bei Teilerfolg bleibt der Snapshot (genau dann will man den Vorher-Stand)",
   len(snap_files()), 1)


# ── 5c. Snapshots ansehen und herunterladen ──────────────────────────────────
print("Snapshot-Liste + Download")
reset()
SRV.seed("tags", "Bestand")
SRV.seed("correspondents", "AltKunde")
client().post("/anwenden", data={"password": PW, "item": ["tags|Finanzen"]})
ts = snap_files()[0][:-5]

r = client().get("/profiles/%s/snapshots" % PID)
page = r.data.decode("utf-8")
eq("die Liste liefert 200", r.status_code, 200)
check("sie nennt den Zeitstempel lesbar", A._fmt_ts(ts) in page)
check("und den Umfang je Kategorie", "1 Tags" in page and "1 Korrespondenten" in page)
check("leere Kategorien werden nicht aufgeführt", "0 Typen" not in page)
check("mit Download-Link", "/snapshots/%s/download" % ts in page)
check("die Seite sagt, dass es KEIN Wiederherstellungspunkt ist", "kein Wiederherstellungspunkt" in page)

r = client().get("/profiles/%s/snapshots/%s/download" % (PID, ts))
eq("der Download liefert 200", r.status_code, 200)
check("als Anhang mit sprechendem Namen",
      "attachment" in r.headers.get("Content-Disposition", "")
      and ts in r.headers.get("Content-Disposition", ""))
eq("als JSON", r.headers.get("Content-Type"), "application/json")
snap = json.loads(r.data.decode("utf-8"))
eq("und enthält den Stand VOR dem Schreiben", [t["name"] for t in snap["tags"]], ["Bestand"])

# Die Zahl fuers Akkordeon-Label darf die Profil-Seite nichts kosten (kein json.load).
eq("_list_snapshots liefert die Zeitstempel, jüngster zuerst", A._list_snapshots(PID), [ts])
eq("ein Profil ohne Snapshots liefert eine leere Liste", A._list_snapshots("gibtsnicht"), [])
r = client().get("/profiles/gibtsnicht/snapshots")
eq("Liste eines unbekannten Profils -> 404", r.status_code, 404)

print("Snapshot-Download: Riegel")
for bad in ("../../config", "..", "20260715T120000/../../x", "nicht-ein-ts", ""):
    r = client().get("/profiles/%s/snapshots/%s/download" % (PID, bad))
    check("Pfad-Ausbruch abgewiesen: %r -> %d" % (bad, r.status_code),
          r.status_code in (404, 308))
r = client().get("/profiles/%s/snapshots/20260101T000000/download" % PID)
eq("ein nicht existierender (aber gültiger) Zeitstempel -> 404", r.status_code, 404)
r = client().get("/profiles/..%2F..%2Fetc/snapshots/" + ts + "/download")
check("Traversal über die Profil-ID -> kein Fund", r.status_code in (404, 308))

c = client(logged_in=False)
r = c.get("/profiles/%s/snapshots" % PID)
eq("ohne Login gibt es keine Liste", (r.status_code, "/login" in r.headers.get("Location", "")),
   (302, True))
r = c.get("/profiles/%s/snapshots/%s/download" % (PID, ts))
eq("und keinen Download", r.status_code, 302)
check("die Datei leckt dabei keine Bytes", b"Bestand" not in r.data)


# ── 6. Rückgängig ────────────────────────────────────────────────────────────
print("Rückgängig")
reset()
client().post("/anwenden", data={"password": PW, "item": ["tags|Finanzen", "tags|Rechnung"]})
ids = [u["id"] for u in A._load_undo(PID)]
r = client().post("/anwenden/undo", data={"password": PW})
eq("Undo -> Redirect ins Protokoll", r.status_code, 302)
eq("beide Einträge werden entfernt", len(SRV.deletes), 2)
eq("KINDER ZUERST, dann Eltern (sonst haengt das Kind am toten Elter)",
   [d[1] for d in SRV.deletes], list(reversed(ids)))
eq("die Instanz ist wieder leer", SRV.data["tags"], [])
eq("die Undo-Liste ist verbraucht", A._load_undo(PID), [])
page = client().get("/anwenden").data.decode("utf-8")
check("und der Knopf verschwindet", "anwenden/undo" not in page)


# ── 6b. BEFUND: der Löschpfad hatte keine Riegel ─────────────────────────────
print("BEFUND: Riegel am Löschpfad")
reset()
client().post("/anwenden", data={"password": PW, "item": ["tags|Finanzen"]})
r = client().post("/anwenden/undo")  # ohne Passwort
eq("ohne Passwort wird NICHTS gelöscht (vorher: löschte kommentarlos)", SRV.deletes, [])
check("und es wird gemeldet", "Passwort+falsch" in r.headers.get("Location", ""))
eq("die Undo-Liste bleibt erhalten", len(A._load_undo(PID)), 1)
r = client().post("/anwenden/undo", data={"password": "falsch"})
eq("falsches Passwort löscht ebenfalls nichts", SRV.deletes, [])

# Profil NACHTRAeGLICH auf nur-lesen: der Loeschweg muss genauso dicht sein wie das Anlegen.
pr = A.load_profiles()
pr[PID]["readonly"] = True
A.save_profiles(pr)
r = client().post("/anwenden/undo", data={"password": PW})
eq("„nur lesen“ blockt das Löschen (vorher: löschte trotzdem)", SRV.deletes, [])
check("und meldet es", "nur+lesen" in r.headers.get("Location", "")
      or "nur%20lesen" in r.headers.get("Location", ""))
page = client().get("/anwenden").data.decode("utf-8")
check("der Undo-Knopf wird bei „nur lesen“ gar nicht angeboten", "anwenden/undo" not in page)
pr[PID]["readonly"] = False
A.save_profiles(pr)
client().post("/anwenden/undo", data={"password": PW})
eq("nach dem Zurücksetzen greift Undo wieder", len(SRV.deletes), 1)


# ── 6c. BEFUND: der zweite Lauf löschte die Undo-Liste ───────────────────────
print("BEFUND: Undo-Liste über mehrere Läufe")
reset()
client().post("/anwenden", data={"password": PW, "item": ["tags|Finanzen"]})
eq("Lauf 1 legt an", [u["name"] for u in A._load_undo(PID)], ["Finanzen"])
SRV.post_fail = {"tags"}  # Lauf 2 geht komplett schief
client().post("/anwenden", data={"password": PW, "item": ["tags|Rechnung"]})
eq("ein fehlgeschlagener Lauf 2 löscht die Undo-Liste NICHT mehr",
   [u["name"] for u in A._load_undo(PID)], ["Finanzen"])
SRV.post_fail = set()
client().post("/anwenden", data={"password": PW, "item": ["tags|Rechnung"]})
eq("ein erfolgreicher Lauf 3 hängt an, statt Lauf 1 zu vergessen",
   [u["name"] for u in A._load_undo(PID)], ["Finanzen", "Rechnung"])
client().post("/anwenden/undo", data={"password": PW})
eq("Undo räumt beide Läufe ab", len(SRV.deletes), 2)
eq("Kind vor Eltern, über Lauf-Grenzen hinweg",
   [SRV.data and d[1] for d in SRV.deletes][0], 102)
eq("die Instanz ist wieder leer", SRV.data["tags"], [])

# Was der Upstream nicht loeschen will, darf nicht stillschweigend aus der Liste fallen.
reset()
client().post("/anwenden", data={"password": PW, "item": ["tags|Finanzen"]})
_real_delete = SRV.delete
SRV.delete = lambda url, **kw: Resp(500)
client().post("/anwenden/undo", data={"password": PW})
eq("ein gescheitertes Löschen bleibt in der Undo-Liste (kein stiller Verlust)",
   [u["name"] for u in A._load_undo(PID)], ["Finanzen"])
SRV.delete = _real_delete
client().post("/anwenden/undo", data={"password": PW})
eq("und lässt sich später nachholen", A._load_undo(PID), [])


# ── 6d. BEFUND: die Undo-Liste hängt am Profil, nicht an der Instanz ─────────
print("BEFUND: Undo nach Instanz-Wechsel")
reset()
client().post("/anwenden", data={"password": PW, "item": ["tags|Finanzen"]})
oid = A._load_undo(PID)[0]["id"]
# Profil zieht um (Test -> Prod). Dort traegt DIESELBE ID ein voellig fremdes Objekt.
SRV.data["tags"] = [{"id": oid, "name": "WICHTIGES-PROD-TAG"}]
r = client().post("/anwenden/undo", data={"password": PW})
eq("ein fremdes Objekt mit gleicher ID wird NICHT gelöscht (vorher: gelöscht)",
   SRV.deletes, [])
eq("es bleibt in der Instanz", [t["name"] for t in SRV.data["tags"]], ["WICHTIGES-PROD-TAG"])
eq("und bleibt in der Undo-Liste, statt still zu verschwinden",
   [u["name"] for u in A._load_undo(PID)], ["Finanzen"])
log = open(A.ACTIVITY_PATH, encoding="utf-8").read()
check("das Protokoll benennt den Verdacht", "anderen Namen" in log)
check("und nennt den fremden Namen", "WICHTIGES-PROD-TAG" in log)

# Gegenprobe: passt der Name, laeuft Undo normal durch.
reset()
client().post("/anwenden", data={"password": PW, "item": ["tags|Finanzen"]})
client().post("/anwenden/undo", data={"password": PW})
eq("bei passendem Namen löscht Undo wie gehabt", len(SRV.deletes), 1)

# Was in der Instanz schon von Hand geloescht wurde, blockiert die Liste nicht.
reset()
client().post("/anwenden", data={"password": PW, "item": ["tags|Finanzen"]})
SRV.data["tags"] = []  # Nutzer hat es selbst entfernt
client().post("/anwenden/undo", data={"password": PW})
eq("ein längst entferntes Objekt wird übersprungen", SRV.deletes, [])
eq("und fällt aus der Liste (nichts mehr zu tun)", A._load_undo(PID), [])

# Netz weg: nichts loeschen, nichts verlieren.
reset()
client().post("/anwenden", data={"password": PW, "item": ["tags|Finanzen"]})
SRV.get_down = True
client().post("/anwenden/undo", data={"password": PW})
eq("bei unerreichbarer Instanz wird nichts gelöscht", SRV.deletes, [])
eq("und die Liste bleibt für später erhalten", len(A._load_undo(PID)), 1)
SRV.get_down = False

# Altbestand (blanke Liste, wie sie auf 230 liegt) bleibt nutzbar — kein Formatwechsel.
reset()
SRV.seed("tags", "Anstellungsvertrag", 172)
A._save_undo(PID, [{"endpoint": "tags/", "id": 172, "name": "Anstellungsvertrag"}])
client().post("/anwenden/undo", data={"password": PW})
eq("eine Undo-Liste im Altformat funktioniert weiter", SRV.deletes, [("tags", 172)])


# ── 7. Login-Schutz ──────────────────────────────────────────────────────────
print("Login-Schutz")
reset()
c = client(logged_in=False)
for path in ("/anwenden",):
    r = c.get(path)
    eq("GET %s ohne Login -> Login" % path, (r.status_code, "/login" in r.headers.get("Location", "")),
       (302, True))
r = c.post("/anwenden", data={"password": PW, "item": ["tags|Finanzen"]})
eq("POST /anwenden ohne Login -> Login", r.status_code, 302)
r = c.post("/anwenden/undo", data={"password": PW})
eq("POST /anwenden/undo ohne Login -> Login", r.status_code, 302)
check("und landet auf /login, nicht im Löschpfad", "/login" in r.headers.get("Location", ""))
eq("und ohne Login wurde nichts geschrieben oder gelöscht", (SRV.posts, SRV.deletes), ([], []))

print("\n%d Prüfungen, %d Fehler" % (_count[0], len(_fails)))
for f in _fails:
    print("  - " + f)
sys.exit(1 if _fails else 0)
