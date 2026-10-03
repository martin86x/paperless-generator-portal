---
paths:
  - "tests/*.py"
---

# Tests (tests/)

- Eigenständige Skripte mit eigenem kleinen Testgerüst, **kein pytest**. Start: `.venv\Scripts\python tests\test_<bereich>.py`.
- Kein Netzwerk: Zugriffe auf Paperless, GitHub oder Benachrichtigungsdienste werden gestubbt.
- Jedes Skript setzt vor dem Import von `app` ein temporäres `CONFIG_DIR`, `SITE_DIR` und `PORTAL_WATCHER=0`. Neue Tests übernehmen dieses Muster.
- Tests dürfen nie auf ein echtes `config/` oder `.dev-config/` zugreifen.
