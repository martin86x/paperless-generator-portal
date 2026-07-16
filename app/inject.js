/* paperless-generator-portal — Laufzeit-Injektion in den (unveraenderten) Generator.
 * Wird von app.py als /portal/inject.js ausgeliefert und vor </head> per <script src> geladen.
 * Aufgaben:
 *   - Portal-Nav (Profil-Dropdown, "Profil speichern", Dirty-Anzeige, Einstellungen, Logout)
 *   - Sektion 01 im Portal-Modus entschaerfen (Banner, Setup-Schritte weg, Token optional,
 *     Benachrichtigungs-E-Mail als Pflichtfeld)
 *   - Config des aktiven Profils laden (_applyLoadedConfig), danach same-origin erzwingen
 *   - Profil wechseln / speichern; ungespeicherte Aenderungen anzeigen
 * Der synchrone localStorage-Patch (plx_conn_preset + paperless_gen_cfg_v2 -> origin) passiert
 * separat inline im <head>, BEVOR die Generator-Skripte laufen.
 */
(function () {
  var o = location.origin;
  // Portal-Modus-Flag: der Generator (Health-Check) blendet damit Hinweise aus, die nur
  // fuer den lokalen Datei-/PowerShell-Betrieb gelten (z.B. Windows-Pfad fuer SCP-Kopie).
  window.PLX_PORTAL_MODE = true;
  var _loading = false;    // true, waehrend eine Config programmatisch angewendet wird
  var _dirty = false;      // ungespeicherte Aenderungen im Generator
  var _navigating = false; // absichtlicher Wechsel/Reload -> keine beforeunload-Warnung
  var _dropdown = null;

  function applyOrigin(cfg) {
    if (cfg && typeof cfg === 'object') { cfg.url = o; cfg.token = ''; }
    return cfg;
  }
  function toast(msg, dur) { if (typeof showToast === 'function') showToast(msg, dur); }

  function setDirty(on) {
    _dirty = on;
    var b = document.getElementById('plx-dirty');
    if (b) b.style.display = on ? 'inline' : 'none';
    var s = document.getElementById('plx-save-btn');
    if (s) s.style.background = on ? '#c2410c' : '#1a7a4a';
  }
  function markDirty() { if (!_loading && !_dirty) setDirty(true); }

  function saveProfile() {
    if (typeof getConfigSnapshot !== 'function') return;
    // Benachrichtigungs-E-Mail ist Pflicht (fuer Frist-Workflows) — hart erzwingen, nicht nur
    // optisch. Ohne diese Pruefung liess sich ein Profil mit leerer Mail speichern -> beim
    // naechsten Laden fehlte sie und der Health-Check meldete "E-Mail fehlt".
    var emEl = document.getElementById('inp-notify-email');
    var emVal = emEl ? emEl.value.trim() : '';
    if (!emVal || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(emVal)) {
      toast(emVal ? 'E-Mail-Format ungültig – Profil nicht gespeichert' :
                    'Benachrichtigungs-E-Mail fehlt (Pflichtfeld) – Profil nicht gespeichert', 4000);
      if (emEl) {
        try { emEl.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch (e) {}
        try { emEl.focus(); } catch (e) {}
        emEl.style.outline = '2px solid var(--danger)';
        setTimeout(function () { emEl.style.outline = ''; }, 2600);
        var ew = document.getElementById('plx-email-warn'); if (ew) ew.style.display = 'block';
      }
      return;
    }
    var snap = getConfigSnapshot();
    fetch('/portal/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(snap)
    }).then(function (r) {
      if (r.status === 401) { location.href = '/login'; return; }
      if (r.ok) {
        try { localStorage.setItem('paperless_gen_cfg_v2', JSON.stringify(applyOrigin(snap))); } catch (e) {}
        setDirty(false);
        toast('Profil gespeichert ✓');
      } else {
        toast('Speichern fehlgeschlagen (' + r.status + ')', 3500);
      }
    }).catch(function () { toast('Speichern fehlgeschlagen', 3500); });
  }

  function switchProfile(id) {
    if (!id) return;
    if (_dirty && !confirm('Ungespeicherte Änderungen gehen verloren. Trotzdem wechseln?')) {
      // Auswahl zuruecksetzen
      loadProfilesIntoDropdown();
      return;
    }
    _navigating = true;
    fetch('/profiles/' + encodeURIComponent(id) + '/activate', { method: 'POST' })
      .then(function () { return fetch('/portal/config'); })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (cfg) {
        try {
          if (cfg) localStorage.setItem('paperless_gen_cfg_v2', JSON.stringify(applyOrigin(cfg)));
          else localStorage.removeItem('paperless_gen_cfg_v2'); // leeres Profil -> Generator-Defaults
        } catch (e) {}
        try { localStorage.removeItem('plx_progress_set'); } catch (e) {} // Resume nicht bluten lassen
        try { localStorage.setItem('plx_conn_preset', JSON.stringify({ url: o, token: '' })); } catch (e) {}
        location.href = '/';
      })
      .catch(function () { location.href = '/'; });
  }

  function updateHealthBadge(crit, warn) {
    var hb = document.getElementById('plx-health-badge');
    if (!hb) return;
    var col, txt;
    if (crit > 0) { col = '#f76f6f'; txt = '🔴 ' + crit + ' kritisch'; }
    else if (warn > 0) { col = '#f59e0b'; txt = '🟡 ' + warn + ' Hinweis' + (warn > 1 ? 'e' : ''); }
    else { col = '#38d9a9'; txt = '🟢 Health OK'; }
    hb.style.color = col; hb.style.borderColor = col; hb.textContent = txt;
    syncHeadPadding();
  }

  function syncHeadPadding() {
    // Reserviert oben Platz in Hoehe der fixierten Leiste (inkl. evtl. Produktiv-Banner),
    // damit nichts darunter verschwindet. Bei Umbruch/Resize neu berechnen.
    var head = document.getElementById('plx-portal-head');
    if (head) document.body.style.paddingTop = head.offsetHeight + 'px';
  }

  function showProductiveBanner(name, color, readonly) {
    var head = document.getElementById('plx-portal-head');
    if (!head) return; // Kopf wird von buildNav() erzeugt; Banner lebt darin
    var b = document.getElementById('plx-prod-banner');
    if (!b) { b = document.createElement('div'); b.id = 'plx-prod-banner'; head.insertBefore(b, head.firstChild); }
    b.style.cssText = 'background:' + (color || '#b91c1c') + ';color:#fff;text-align:center;padding:6px 12px;font-size:13px;font-weight:600;letter-spacing:.3px;font-family:system-ui,sans-serif;';
    b.textContent = '⚠ PRODUKTIV: ' + (name || '') + ' — Änderungen wirken auf das Live-System' + (readonly ? ' · nur lesen' : '');
    syncHeadPadding();
  }
  function removeProductiveBanner() {
    var b = document.getElementById('plx-prod-banner'); if (b) b.remove();
    syncHeadPadding();
  }

  function loadProfilesIntoDropdown() {
    fetch('/portal/profiles.json')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        if (_dropdown) {
          _dropdown.innerHTML = '';
          d.profiles.forEach(function (p) {
            var op = document.createElement('option');
            op.value = p.id; op.textContent = p.name;
            if (p.id === d.active) op.selected = true;
            _dropdown.appendChild(op);
          });
        }
        if (d.active_productive) showProductiveBanner(d.active_name, d.active_color, d.active_readonly);
        else removeProductiveBanner();
      }).catch(function () {});
  }

  function buildNav() {
    if (document.getElementById('plx-portal-head')) return;
    // Voller Kopf im normalen Fluss (sticky) -> schiebt den Generator-Inhalt nach unten,
    // statt ihn zu ueberdecken. Produktiv-Banner (falls) lebt oben im selben Kopf.
    var head = document.createElement('div');
    head.id = 'plx-portal-head';
    // Fest ganz oben angepinnt (fixed). Body bekommt unten passenden Abstand (syncHeadPadding),
    // damit der Generator-Inhalt nicht unter der Leiste verschwindet.
    head.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;width:100%;font-family:system-ui,sans-serif';

    var n = document.createElement('div');
    n.id = 'plx-portal-nav';
    n.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;width:100%;box-sizing:border-box;background:#171a21;border-bottom:1px solid #2b303b;padding:7px 12px';

    var sel = document.createElement('select');
    sel.id = 'plx-profile-sel';
    sel.title = 'Aktives Profil wechseln';
    sel.style.cssText = 'background:#1f232c;color:#e6e9ef;border:1px solid #2b303b;border-radius:6px;padding:5px 8px;font-size:12px;max-width:200px';
    sel.addEventListener('change', function () { switchProfile(sel.value); });
    _dropdown = sel; n.appendChild(sel);

    var save = document.createElement('button');
    save.id = 'plx-save-btn'; save.textContent = '💾 Profil speichern';
    save.style.cssText = 'background:#1a7a4a;color:#fff;border:1px solid #2b303b;border-radius:6px;padding:5px 10px;font-size:12px;cursor:pointer';
    save.addEventListener('click', saveProfile); n.appendChild(save);

    var dirty = document.createElement('span');
    dirty.id = 'plx-dirty'; dirty.textContent = '● ungespeichert';
    dirty.title = 'Es gibt ungespeicherte Änderungen in diesem Profil';
    dirty.style.cssText = 'display:none;color:#f59e0b;font-size:12px;font-weight:600'; n.appendChild(dirty);

    // Health-Ampel: immer sichtbarer Status (der Health-Check in Sektion 10 "geht sonst unter").
    var hb = document.createElement('button');
    hb.id = 'plx-health-badge';
    hb.title = 'Health-Check — klicken, um zum Ergebnis zu springen und neu zu prüfen';
    hb.style.cssText = 'background:#1f232c;color:#9aa4b2;border:1px solid #2b303b;border-radius:6px;padding:5px 10px;font-size:12px;cursor:pointer;white-space:nowrap';
    hb.textContent = '● Health-Check';
    hb.addEventListener('click', function () {
      try { if (typeof goTo === 'function') goTo('s-gen'); } catch (e) {}
      var hr = document.getElementById('health-results');
      if (hr) { try { hr.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch (e) {} }
      try { if (typeof runFullHealthCheck === 'function') runFullHealthCheck(); } catch (e) {}
    });
    n.appendChild(hb);

    // "Alle Tools durchlaufen" – read-only Diagnose-Sweep, immer erreichbar in der Kopfzeile.
    var sweep = document.createElement('button');
    sweep.id = 'plx-sweep-btn';
    sweep.title = 'Alle read-only Diagnose-Tools einmal durchlaufen (keine Änderungen an der Instanz)';
    sweep.textContent = '🚀 Alle Tools';
    sweep.style.cssText = 'background:#1f232c;color:#7aadff;border:1px solid #2b303b;border-radius:6px;padding:5px 10px;font-size:12px;cursor:pointer;white-space:nowrap';
    sweep.addEventListener('click', function () {
      try { if (typeof goTo === 'function') goTo('s-tools'); } catch (e) {}
      try { if (typeof switchToolsTab === 'function') switchToolsTab('online'); } catch (e) {}
      try {
        if (typeof runAllTools === 'function') runAllTools();
        else toast('Tool-Durchlauf noch nicht bereit – kurz warten / Seite neu laden', 3000);
      } catch (e) {}
    });
    n.appendChild(sweep);

    var spacer = document.createElement('span');
    spacer.style.cssText = 'flex:1 1 auto'; n.appendChild(spacer); // drueckt Verwaltung/Logout nach rechts

    // Slot fuer den Update-Hinweis (wird von checkForUpdate() gefuellt, wenn eine neuere
    // Portal-Version auf GitHub vorliegt). Leer, solange alles aktuell ist.
    var uslot = document.createElement('span');
    uslot.id = 'plx-update-slot'; uslot.style.cssText = 'display:inline-flex'; n.appendChild(uslot);

    var mk = function (h, txt, col) {
      var a = document.createElement('a');
      a.href = h; a.textContent = txt;
      a.style.cssText = 'background:#1f232c;color:' + col + ';border:1px solid #2b303b;border-radius:6px;padding:5px 10px;font-size:12px;text-decoration:none;white-space:nowrap';
      return a;
    };
    n.appendChild(mk(o + '/verwaltung', '⚙ Verwaltung', '#60a5fa'));
    n.appendChild(mk(o + '/logout', 'Logout', '#9aa4b2'));

    head.appendChild(n);
    document.body.insertBefore(head, document.body.firstChild);
    syncHeadPadding();
  }

  function tameSection01() {
    var sc = document.getElementById('s-conn');
    if (!sc || document.getElementById('plx-portal-note')) return;
    var b = document.createElement('div');
    b.id = 'plx-portal-note';
    b.style.cssText = 'margin:.2rem 0 1rem;padding:.7rem .95rem;background:rgba(96,165,250,.1);border:1px solid rgba(96,165,250,.4);border-radius:8px;font-size:.82rem;color:#bcd3ff;line-height:1.5';
    b.appendChild(document.createTextNode('🔌 Portal-Modus: Die Verbindung zu Paperless läuft automatisch über den Portal-Proxy (same-origin, kein CORS). URL und Token verwaltest du je Instanz unter '));
    var la = document.createElement('a');
    la.href = o + '/verwaltung?tab=profiles'; la.textContent = 'Profile'; la.style.cssText = 'color:#60a5fa;font-weight:700;text-decoration:underline';
    b.appendChild(la);
    b.appendChild(document.createTextNode('. Hier unten bleibt nur, was der Generator wirklich braucht — v. a. die Erinnerungs-E-Mail für die Frist-Workflows.'));
    var h = sc.querySelector('h2');
    if (h) sc.insertBefore(b, h.nextSibling); else sc.insertBefore(b, sc.firstChild);

    // Den GANZEN "Einrichtung – Schritt für Schritt"-Block ausblenden (Kopfbalken +
    // Inhalt). Er beschreibt den Standalone-Weg (lokaler http.server, CORS in der
    // docker-compose, Verbindung per Datei) — im Portal-Modus alles gegenstandslos und
    // verwirrend. Vorher wurde nur der Inhalt (#setup-steps) versteckt, der Kopfbalken
    // blieb klickbar und klappte die Schritte wieder auf. Jetzt das Eltern-Panel weg.
    var st = document.getElementById('setup-steps');
    if (st) { var panel = st.parentElement; (panel || st).style.display = 'none'; }

    // Im Portal-Modus wirkungslose Verbindungs-/Bash-Felder ausblenden (Proxy übernimmt die
    // Verbindung; IP/Pfade nur für den optionalen Bash-Export). Sichtbar bleiben
    // Benachrichtigungs-E-Mail + Paperless-Version.
    ['inp-host', 'inp-token', 'inp-ip', 'inp-user', 'inp-base-path', 'inp-dc-path'].forEach(function (id) {
      var el = document.getElementById(id);
      var fg = el && el.closest('.field-group');
      if (fg) fg.style.display = 'none';
    });
    // Preset laden/speichern (URL/Token) + Token-.env-Hinweis raus
    sc.querySelectorAll('[onchange*="loadConnectionPreset"], [onclick*="saveConnectionPreset"]').forEach(function (el) {
      var w = el.closest('label') || el; if (w && w.style) w.style.display = 'none';
    });
    // Token-.env-Hinweis: innersten Div mit dem Text treffen (nicht die Eltern-Container)
    Array.prototype.forEach.call(sc.querySelectorAll('div'), function (d) {
      if (d.textContent.indexOf('Token-Feld leer lassen') >= 0 && !d.querySelector('div')) d.style.display = 'none';
    });
    // „Verbindung testen": im Portal-Modus testet der Generator die PORTAL-Origin (Proxy),
    // nicht die echte Instanz -> „verbunden mit sich selbst" ist verwirrend. Der echte
    // Instanz-Status steht in Profile/Dashboard. Also den Test-Teil ausblenden.
    var ctr = document.getElementById('conn-test-result');
    if (ctr) {
      ctr.style.display = 'none';
      var lbl = ctr.previousElementSibling; if (lbl) lbl.style.display = 'none'; // „🔌 Verbindung testen"-Label
    }
    var bct = document.getElementById('btn-conn-test');
    if (bct) { var row = bct.closest('div'); if (row) row.style.display = 'none'; }

    // Benachrichtigungs-E-Mail als Pflichtfeld (fuer die Frist-Workflows)
    var em = document.getElementById('inp-notify-email');
    if (em) {
      var fg = em.closest('.field-group');
      if (fg) {
        var fl = fg.querySelector('.flabel');
        if (fl && !fl.querySelector('.plx-req')) {
          var rq = document.createElement('span');
          rq.className = 'plx-req'; rq.textContent = ' * '; rq.style.color = 'var(--danger)';
          var sub = fl.querySelector('span');
          if (sub) fl.insertBefore(rq, sub); else fl.appendChild(rq);
        }
        var ew = document.getElementById('plx-email-warn');
        if (!ew) {
          ew = document.createElement('span'); ew.id = 'plx-email-warn';
          ew.textContent = '⚠ E-Mail wird für die Frist-Workflows (Erinnerungen) benötigt';
          ew.style.cssText = 'font-size:.7rem;color:var(--danger);margin-top:.2rem';
          fg.appendChild(ew);
        }
        var chk = function () { ew.style.display = (em.value.trim() ? 'none' : 'block'); };
        em.addEventListener('input', chk); chk();
      }
    }

    // Bash-Skript-Weg im Portal-Modus: Sektion 11 „So wendest du das Skript an" ist
    // reine SSH/scp-Anleitung — im Portal richtet der Direkt-Modus über den Proxy ein.
    // Sektion + Sidebar-Link ausblenden. (Sektion 09 bleibt: sie hält Health-Check,
    // Statistik und die Dry-Run/Ausführen-Buttons; Sektion 10 taucht ohnehin nur nach
    // dem Bash-Generieren auf.)
    var howto = document.getElementById('s-howto');
    if (howto) howto.style.display = 'none';
    var howtoLink = document.querySelector('.sb-link[onclick*="s-howto"]');
    if (howtoLink) howtoLink.style.display = 'none';
  }

  // Sentinel-Token (40 Hex): Der /api/-Proxy verwirft den Client-Authorization-Header und
  // spritzt IMMER den echten Profil-Token ein. Die Instanz-Tools und der Health-Check
  // brauchen aber clientseitig ein nicht-leeres, formal gültiges Token, sonst brechen sie
  // mit „URL + Token eintragen" ab. Dieser Platzhalter erfüllt genau diesen Guard; der reale
  // Token bleibt serverseitig. Wird NICHT persistiert (applyOrigin/plx_conn_preset bleiben leer).
  var PLX_TOKEN_SENTINEL = '0000000000000000000000000000000000000000';

  function forceSameOrigin() {
    try {
      if (typeof _parseUrlToFields === 'function') _parseUrlToFields(o);
      var t = document.getElementById('inp-token'); if (t) t.value = PLX_TOKEN_SENTINEL;
      // KEIN testConnection() im Portal-Modus: es würde die Portal-Origin (Proxy) testen und
      // „✓ Verbindung OK – <portal>" zeigen (verbunden mit sich selbst). Der echte
      // Instanz-Status steht in Profile/Dashboard.
    } catch (e) {}
  }

  function loadActiveProfileConfig() {
    fetch('/portal/config').then(function (r) {
      if (r.status === 401) { location.href = '/login'; return null; }
      return r.ok ? r.json() : null;
    }).then(function (cfg) {
      try {
        if (cfg && typeof _applyLoadedConfig === 'function') {
          _loading = true;
          _applyLoadedConfig(cfg);
          _loading = false;
          try { localStorage.setItem('paperless_gen_cfg_v2', JSON.stringify(applyOrigin(cfg))); } catch (e) {}
        }
      } catch (e) { _loading = false; }
      forceSameOrigin();
      // Dirty-Tracking erst JETZT aktivieren (nach dem Laden), damit das Anwenden keinen Fehlalarm ausloest
      document.addEventListener('input', markDirty, true);
      setDirty(false);
      // Health-Check jetzt deterministisch NACH dem Config-Apply erneut, damit die Profil-E-Mail
      // (inp-notify-email) und die Ampel-Badge nicht durch ein Timing-Rennen mit dem festen
      // 1900-ms-Timer faelschlich als "fehlt" gemeldet werden.
      try { if (typeof runFullHealthCheck === 'function') runFullHealthCheck(); } catch (e) {}
    }).catch(function () { forceSameOrigin(); });
  }

  // Health-Status vom Generator (07-health-match.js feuert 'plx:health' nach jeder Prüfung).
  document.addEventListener('plx:health', function (e) {
    var d = e.detail || {};
    updateHealthBadge(d.critical || 0, d.warn || 0);
  });

  // Automatische Update-Pruefung: fragt den (serverseitig gecachten) Versions-Abgleich ab
  // und zeigt in der Kopfzeile eine Pille "⬆ Update vX.Y verfuegbar", die auf die Version-Seite
  // verlinkt. GitHub wird serverseitig hoechstens alle 6 h wirklich kontaktiert (Cache).
  function checkForUpdate() {
    fetch('/portal/update-check.json').then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (d) {
      var slot = document.getElementById('plx-update-slot');
      if (!slot) return;
      if (!d || !d.update_available) { slot.innerHTML = ''; return; }
      if (slot.getAttribute('data-ver') === d.latest) return; // schon angezeigt
      slot.setAttribute('data-ver', d.latest);
      slot.innerHTML = '';
      var a = document.createElement('a');
      a.href = o + '/verwaltung?tab=version';
      a.textContent = '⬆ Update v' + d.latest + ' verfügbar';
      a.title = 'Neuere Portal-Version v' + d.latest + ' auf GitHub (installiert: v' + d.installed + ') — hier aktualisieren';
      a.style.cssText = 'background:#7a4a1a;color:#ffd9a8;border:1px solid #a86a2b;border-radius:6px;padding:5px 10px;font-size:12px;font-weight:600;text-decoration:none;white-space:nowrap';
      slot.appendChild(a);
    }).catch(function () {});
  }

  window.addEventListener('load', function () {
    try { buildNav(); } catch (e) {}
    loadProfilesIntoDropdown();
    try { tameSection01(); } catch (e) {}
    window.addEventListener('resize', syncHeadPadding);
    setTimeout(syncHeadPadding, 400); // nach Layout-Settle nochmal (Umbruch/Schriftmaße)
    // Nach dem generator-eigenen loadAutoSave (~900ms) die Profil-Config anwenden + same-origin erzwingen.
    setTimeout(loadActiveProfileConfig, 1200);
    // Ampel initial füllen: einmal prüfen, sobald Editoren + Config stehen (~1,9 s).
    setTimeout(function () {
      try {
        if (typeof runFullHealthCheck === 'function') runFullHealthCheck();
        else if (typeof window._lastHealthCriticalCount !== 'undefined')
          updateHealthBadge(window._lastHealthCriticalCount || 0, window._lastHealthWarnCount || 0);
      } catch (e) {}
    }, 1900);
    // Update-Hinweis: einmal kurz nach dem Laden, danach alle 6 h (falls der Tab offen bleibt).
    setTimeout(checkForUpdate, 2500);
    setInterval(checkForUpdate, 6 * 3600 * 1000);
  });

  window.addEventListener('beforeunload', function (e) {
    if (_dirty && !_navigating) { e.preventDefault(); e.returnValue = ''; }
  });
})();
