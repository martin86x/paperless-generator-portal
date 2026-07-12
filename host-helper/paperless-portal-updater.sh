#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Paperless-Generator-Portal — Host-Helper fuer sicheres 1-Klick-Update.
#
# Laeuft auf dem LXC-HOST (dort, wo das Repo + docker leben), NICHT im Container.
# Ermoeglicht Update/Rollback aus dem Portal, OHNE den Docker-Socket in den
# Container zu mounten (das waere root-aequivalent). Der Container schreibt nur
# eine Anforderungsdatei in sein /config-Volume; dieses Skript fuehrt sie aus.
#
# Einrichtung (auf dem LXC, einmalig) — siehe host-helper/README.md:
#   */1 * * * * /opt/paperless-generator-portal/host-helper/paperless-portal-updater.sh >/dev/null 2>&1
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO="${PORTAL_REPO:-/opt/paperless-generator-portal}"
CFG="$REPO/config"

mkdir -p "$CFG"
# Lebenszeichen (das Portal zeigt darüber „Helper aktiv"):
touch "$CFG/update-helper.alive"

REQ="$CFG/update-request.json"
[ -f "$REQ" ] || exit 0   # nichts angefordert

ACTION="$(grep -oE '"action"[[:space:]]*:[[:space:]]*"[a-z]+"' "$REQ" | grep -oE 'update|rollback' | head -1 || true)"
rm -f "$REQ"
[ -n "$ACTION" ] || exit 0

cd "$REPO"

write_status() { # action result commit detail
  printf '{"action":"%s","result":"%s","commit":"%s","ts":"%s","detail":"%s"}\n' \
    "$1" "$2" "$3" "$(date -Iseconds)" "$4" > "$CFG/update-status.json"
}

PREV="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

if [ "$ACTION" = "rollback" ]; then
  TARGET="$(cat "$CFG/update-rollback-to" 2>/dev/null || true)"
  if [ -z "$TARGET" ]; then
    write_status rollback error "$PREV" "kein Rollback-Ziel gespeichert"
    exit 0
  fi
  if git reset --hard "$TARGET" && docker compose up -d --build; then
    write_status rollback ok "$(git rev-parse --short HEAD)" ""
  else
    write_status rollback error "$PREV" "Rebuild fehlgeschlagen"
  fi
else
  # Vor dem Update den aktuellen Commit als Rollback-Ziel merken:
  echo "$PREV" > "$CFG/update-rollback-to"
  if git fetch origin main && git reset --hard origin/main && docker compose up -d --build; then
    write_status update ok "$(git rev-parse --short HEAD)" ""
  else
    write_status update error "$PREV" "Update/Rebuild fehlgeschlagen"
  fi
fi
