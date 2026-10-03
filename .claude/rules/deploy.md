---
paths:
  - "Dockerfile"
  - "docker-compose.yml"
  - "proxmox-install.sh"
  - "update.ps1"
  - "host-helper/**"
  - "site/**"
---

# Deployment und Generator-Artefakt

- `site/index.html` ist ein Build-Ergebnis aus `../Generator-Build/dist/index.html` und wird nur über `update.ps1` ersetzt, nie von Hand bearbeitet. Commit: `site: Generator vX.XX - <Kurzbeschreibung>`.
- Kein Docker-Socket im Container. Update und Rollback laufen über den Host-Helper (Cron auf dem LXC), der nur eine Anforderungsdatei im `/config`-Volume liest.
- `gunicorn -w 2 --preload` bleibt: ohne `--preload` erzeugt jeder Worker ein eigenes Session-Secret (Login-Schleife).
- `proxmox-install.sh` wird per `curl` aus dem öffentlichen Repo geladen und läuft als root auf dem Proxmox-Host; der Host-Helper läuft als root-Cron auf dem LXC. Änderungen dort besonders vorsichtig, keine zusätzlichen Downloads einbauen.
- Das Repo ist öffentlich: keine IPs, Hostnamen, Tokens oder persönlichen Pfade in Skripte, Beispiele oder Kommentare.
