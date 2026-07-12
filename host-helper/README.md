# Host-Helper: sicheres 1-Klick-Update

Ermöglicht **Update** und **Rollback** direkt aus dem Portal (Reiter *Version*), **ohne**
den Docker-Socket in den Container zu mounten. Der Container bekommt dadurch **keine**
zusätzlichen Rechte auf dem Host — das ist der sichere Weg.

## Wie es funktioniert

1. Im Portal klickst du auf **„Jetzt aktualisieren"** (oder **Rollback**).
2. Das Portal schreibt nur eine Anforderungsdatei ins `/config`-Volume
   (`config/update-request.json`).
3. Dieses Skript läuft als **Cron auf dem LXC-Host** (dort, wo das Repo + Docker liegen),
   liest die Anforderung und führt `git fetch/reset + docker compose up -d --build` aus.
4. Ergebnis + der vorherige Commit (für Rollback) landen in `config/update-status.json`
   bzw. `config/update-rollback-to`. Das Portal zeigt den Status an.

Sicherheit: Der Container kann nur eine Datei in sein eigenes Volume schreiben. Die
Docker-Rechte liegen ausschließlich beim Host-Cron (root auf dem LXC). Kein
`/var/run/docker.sock` im Container.

## Einrichtung (einmalig, auf dem LXC 230)

```bash
# 1) Skript ausführbar machen
chmod +x /opt/paperless-generator-portal/host-helper/paperless-portal-updater.sh

# 2) Als Minuten-Cron eintragen (root-Crontab auf dem LXC)
( crontab -l 2>/dev/null; \
  echo '* * * * * /opt/paperless-generator-portal/host-helper/paperless-portal-updater.sh >/dev/null 2>&1' ) \
  | crontab -
```

Vom **Proxmox-Host** aus in einem Rutsch:

```bash
pct exec 230 -- bash -c 'chmod +x /opt/paperless-generator-portal/host-helper/paperless-portal-updater.sh; ( crontab -l 2>/dev/null; echo "* * * * * /opt/paperless-generator-portal/host-helper/paperless-portal-updater.sh >/dev/null 2>&1" ) | crontab -'
```

Nach spätestens einer Minute schreibt das Skript `config/update-helper.alive`; das Portal
zeigt dann **„Helper aktiv"** und blendet die Knöpfe **Aktualisieren** / **Rollback** ein.

## Deinstallation

```bash
crontab -l | grep -v paperless-portal-updater | crontab -
rm -f /opt/paperless-generator-portal/config/update-helper.alive
```

## Variablen

- `PORTAL_REPO` — Repo-Pfad, falls abweichend (Standard `/opt/paperless-generator-portal`).
