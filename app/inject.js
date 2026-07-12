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

  function showProductiveBanner(name, color, readonly) {
    var b = document.getElementById('plx-prod-banner');
    if (!b) { b = document.createElement('div'); b.id = 'plx-prod-banner'; document.body.insertBefore(b, document.body.firstChild); }
    b.style.cssText = 'position:sticky;top:0;z-index:2147483646;background:' + (color || '#b91c1c') + ';color:#fff;text-align:center;padding:6px 12px;font-size:13px;font-weight:600;letter-spacing:.3px;font-family:system-ui,sans-serif;';
    b.textContent = '⚠ PRODUKTIV: ' + (name || '') + ' — Änderungen wirken auf das Live-System' + (readonly ? ' · nur lesen' : '');
  }
  function removeProductiveBanner() {
    var b = document.getElementById('plx-prod-banner'); if (b) b.remove();
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
    if (document.getElementById('plx-portal-nav')) return;
    var n = document.createElement('div');
    n.id = 'plx-portal-nav';
    n.style.cssText = 'position:fixed;top:8px;right:10px;z-index:2147483647;display:flex;gap:6px;align-items:center;font-family:system-ui,sans-serif';

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

    var mk = function (h, txt, col) {
      var a = document.createElement('a');
      a.href = h; a.textContent = txt;
      a.style.cssText = 'background:#1f232c;color:' + col + ';border:1px solid #2b303b;border-radius:6px;padding:5px 10px;font-size:12px;text-decoration:none';
      return a;
    };
    n.appendChild(mk(o + '/verwaltung', '⚙ Verwaltung', '#60a5fa'));
    n.appendChild(mk(o + '/logout', 'Logout', '#9aa4b2'));
    document.body.appendChild(n);
  }

  function tameSection01() {
    var sc = document.getElementById('s-conn');
    if (!sc || document.getElementById('plx-portal-note')) return;
    var b = document.createElement('div');
    b.id = 'plx-portal-note';
    b.style.cssText = 'margin:.2rem 0 1rem;padding:.7rem .95rem;background:rgba(96,165,250,.1);border:1px solid rgba(96,165,250,.4);border-radius:8px;font-size:.82rem;color:#bcd3ff;line-height:1.5';
    b.appendChild(document.createTextNode('🔌 Portal-Modus: Die Verbindung zu Paperless läuft automatisch über den Portal-Proxy (same-origin, kein CORS). URL und Token musst du hier nicht eintragen — den Token verwaltest du je Profil unter '));
    var la = document.createElement('a');
    la.href = o + '/profiles'; la.textContent = 'Profile'; la.style.cssText = 'color:#60a5fa;font-weight:700;text-decoration:underline';
    b.appendChild(la);
    b.appendChild(document.createTextNode('. Die übrigen Felder brauchst du nur für einzelne Funktionen — z. B. die Benachrichtigungs-E-Mail für die Frist-Workflows (Erinnerungen) oder IP/Pfade für den optionalen Bash-Skript-Export.'));
    var h = sc.querySelector('h2');
    if (h) sc.insertBefore(b, h.nextSibling); else sc.insertBefore(b, sc.firstChild);

    var st = document.getElementById('setup-steps'); if (st) st.style.display = 'none';
    var tk = document.getElementById('inp-token'); if (tk) tk.placeholder = '— im Portal nicht nötig (Proxy spritzt den Token ein) —';
    var tw = document.getElementById('token-warn'); if (tw) tw.style.display = 'none';

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
  }

  function forceSameOrigin() {
    try {
      if (typeof _parseUrlToFields === 'function') _parseUrlToFields(o);
      var t = document.getElementById('inp-token'); if (t) t.value = '';
      if (typeof testConnection === 'function') testConnection();
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
    }).catch(function () { forceSameOrigin(); });
  }

  window.addEventListener('load', function () {
    try { buildNav(); } catch (e) {}
    loadProfilesIntoDropdown();
    try { tameSection01(); } catch (e) {}
    // Nach dem generator-eigenen loadAutoSave (~900ms) die Profil-Config anwenden + same-origin erzwingen.
    setTimeout(loadActiveProfileConfig, 1200);
  });

  window.addEventListener('beforeunload', function (e) {
    if (_dirty && !_navigating) { e.preventDefault(); e.returnValue = ''; }
  });
})();
