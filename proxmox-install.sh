#!/usr/bin/env bash
#
# paperless-generator-portal — Proxmox-LXC-Installer
#
# Auf der PROXMOX-HOST-Shell ausfuehren:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/martin86x/paperless-generator-portal/main/proxmox-install.sh)"
#
# Legt einen privilegierten Debian-12-LXC (mit Nesting) an, installiert Docker und
# startet den Generator-Portal-Container. Danach: http://<lxc-ip>:8080 -> Login admin/admin.
#
set -euo pipefail

REPO_URL="https://github.com/martin86x/paperless-generator-portal"
APP_DIR="/opt/paperless-generator-portal"

# ── Farben & Statusausgabe (community-scripts-Stil) ──────────────────────────
if [[ -t 1 ]]; then
  RD=$'\033[31m'; GN=$'\033[32m'; BL=$'\033[34m'; YW=$'\033[33m'; DIM=$'\033[2m'; RS=$'\033[0m'
else
  RD=""; GN=""; BL=""; YW=""; DIM=""; RS=""
fi
CHECK="${GN}✓${RS}"; CROSS="${RD}✗${RS}"

msg_info()  { echo -e " ${BL}▶${RS} $1"; }
msg_ok()    { echo -e " ${CHECK} $1"; }
msg_error() { echo -e " ${CROSS} ${RD}$1${RS}"; }

CREATED_CTID=""
cleanup_on_error() {
  local code=$?
  if [[ $code -ne 0 ]]; then
    echo
    msg_error "Abbruch (Exit $code)."
    if [[ -n "$CREATED_CTID" ]] && pct status "$CREATED_CTID" &>/dev/null; then
      echo -e " ${YW}Raeume angelegten Container $CREATED_CTID wieder auf...${RS}"
      pct stop "$CREATED_CTID" &>/dev/null || true
      pct destroy "$CREATED_CTID" &>/dev/null || true
    fi
  fi
}
trap cleanup_on_error EXIT

header() {
  echo -e "${BL}"
  echo "   ___                       _         ___         _        _ "
  echo "  | _ \\__ _ _ __  ___ _ _ | |___ ___/ __| ___ _ _ | |___ _ _| |"
  echo "  |  _/ _\` | '_ \\/ -_) '_|| / -_|_-<| (_ |/ -_) ' \\| / -_) '_|_|"
  echo "  |_| \\__,_| .__/\\___|_|  |_\\___/__/ \\___|\\___|_||_|_\\___|_| (_)"
  echo "           |_|   Generator Portal — Proxmox Installer"
  echo -e "${RS}"
}

# ── Vorpruefung: laufen wir auf einem Proxmox-Host? ──────────────────────────
require_host() {
  if ! command -v pct &>/dev/null || ! command -v pveversion &>/dev/null; then
    msg_error "Dieses Skript muss auf dem PROXMOX-HOST laufen (pct/pveversion fehlen)."
    exit 1
  fi
  msg_ok "Proxmox-Host erkannt ($(pveversion | head -1))"
}

# ── Abfragen (whiptail, mit Defaults) ────────────────────────────────────────
ask_config() {
  local nextid
  nextid=$(pvesh get /cluster/nextid 2>/dev/null || echo 200)

  # Container-faehige Storages als Menue-Eintraege (Name + Typ) sammeln.
  local stor_items=() sname stype srest
  while read -r sname stype srest; do
    stor_items+=("$sname" "Typ: $stype")
  done < <(pvesm status --content rootdir 2>/dev/null | awk 'NR>1{print}')

  if command -v whiptail &>/dev/null; then
    CTID=$(whiptail --inputbox "Container-ID" 8 60 "$nextid" --title "LXC anlegen" 3>&1 1>&2 2>&3) || exit 1
    HOSTNAME=$(whiptail --inputbox "Hostname" 8 60 "paperless-generator" --title "LXC anlegen" 3>&1 1>&2 2>&3) || exit 1
    if [[ ${#stor_items[@]} -gt 0 ]]; then
      STORAGE=$(whiptail --title "Storage waehlen" --menu "Storage fuer den LXC-Rootfs:" 16 64 7 \
        "${stor_items[@]}" 3>&1 1>&2 2>&3) || exit 1
    else
      STORAGE=$(whiptail --inputbox "Storage (rootfs)" 8 60 "local-lvm" --title "LXC anlegen" 3>&1 1>&2 2>&3) || exit 1
    fi
    BRIDGE=$(whiptail --inputbox "Netzwerk-Bridge" 8 60 "vmbr0" --title "LXC anlegen" 3>&1 1>&2 2>&3) || exit 1
    DISK=$(whiptail --inputbox "Disk-Groesse (GB)" 8 60 "4" --title "LXC anlegen" 3>&1 1>&2 2>&3) || exit 1
    RAM=$(whiptail --inputbox "RAM (MB)" 8 60 "512" --title "LXC anlegen" 3>&1 1>&2 2>&3) || exit 1
    CORES=$(whiptail --inputbox "CPU-Kerne" 8 60 "1" --title "LXC anlegen" 3>&1 1>&2 2>&3) || exit 1
  else
    CTID="$nextid"; HOSTNAME="paperless-generator"
    STORAGE="${stor_items[0]:-local-lvm}"
    BRIDGE="vmbr0"; DISK="4"; RAM="512"; CORES="1"
    msg_info "whiptail nicht gefunden — nutze Defaults (CTID $CTID, $STORAGE, $BRIDGE)."
  fi
}

# ── Debian-Template sicherstellen ────────────────────────────────────────────
ensure_template() {
  msg_info "Pruefe Debian-Template..."
  pveam update &>/dev/null || true
  local tmpl
  tmpl=$(pveam available --section system 2>/dev/null | awk '/debian-12-standard/{print $2}' | sort -V | tail -1)
  if [[ -z "$tmpl" ]]; then
    msg_error "Kein debian-12-standard Template verfuegbar."; exit 1
  fi
  if ! pveam list local 2>/dev/null | grep -q "$tmpl"; then
    msg_info "Lade Template $tmpl ..."
    pveam download local "$tmpl" >/dev/null
  fi
  TEMPLATE_REF="local:vztmpl/$tmpl"
  msg_ok "Template bereit ($tmpl)"
}

# ── LXC anlegen & starten ────────────────────────────────────────────────────
create_ct() {
  local ctpw
  ctpw=$(openssl rand -base64 12)
  msg_info "Erstelle LXC $CTID (privileged, Nesting)..."
  pct create "$CTID" "$TEMPLATE_REF" \
    --hostname "$HOSTNAME" \
    --cores "$CORES" --memory "$RAM" \
    --rootfs "${STORAGE}:${DISK}" \
    --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp" \
    --features nesting=1 \
    --unprivileged 0 \
    --onboot 1 \
    --password "$ctpw" >/dev/null
  CREATED_CTID="$CTID"
  msg_ok "LXC $CTID erstellt (privileged, Nesting, onboot)"

  msg_info "Starte Container..."
  pct start "$CTID" >/dev/null

  msg_info "Warte auf Netzwerk (DHCP)..."
  local ip="" i
  for i in $(seq 1 30); do
    ip=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}') || true
    [[ -n "$ip" ]] && break
    sleep 2
  done
  if [[ -z "$ip" ]]; then msg_error "Keine IP erhalten."; exit 1; fi
  LXC_IP="$ip"
  msg_ok "Container gestartet, Netzwerk erreichbar (IP $LXC_IP)"
}

# ── Docker + App im Container installieren ───────────────────────────────────
install_app() {
  local inner="/tmp/pgp-inner-$CTID.sh"
  cat > "$inner" <<INNER
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export LANG=C.UTF-8 LC_ALL=C.UTF-8
apt-get update -qq
apt-get install -y -qq ca-certificates curl git >/dev/null
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian \$(. /etc/os-release && echo \$VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
systemctl enable --now docker >/dev/null 2>&1
rm -rf "$APP_DIR"
git clone --depth 1 "$REPO_URL" "$APP_DIR" >/dev/null 2>&1
cd "$APP_DIR"
docker compose up -d --build
INNER

  msg_info "Installiere Docker im Container (kann 1-2 Min dauern)..."
  pct push "$CTID" "$inner" /root/pgp-inner.sh >/dev/null
  pct exec "$CTID" -- bash /root/pgp-inner.sh
  rm -f "$inner"
  msg_ok "Docker installiert, Projekt geholt & Container gebaut"
}

# ── Abschluss-Selbstcheck (grün/rot) ─────────────────────────────────────────
self_check() {
  echo
  echo -e "${BL}── Selbstcheck ──${RS}"
  local fail=0

  if pct exec "$CTID" -- docker ps --format '{{.Names}}' 2>/dev/null | grep -q paperless-generator-portal; then
    msg_ok "Container laeuft (docker ps)"
  else msg_error "Container laeuft nicht"; fail=1; fi

  local i healthy=0
  for i in $(seq 1 15); do
    if pct exec "$CTID" -- docker inspect --format '{{.State.Health.Status}}' paperless-generator-portal 2>/dev/null | grep -q healthy; then
      healthy=1; break; fi
    sleep 2
  done
  if [[ $healthy -eq 1 ]]; then msg_ok "Healthcheck: healthy"; else msg_error "Healthcheck nicht healthy"; fail=1; fi

  if curl -fsS -o /dev/null -w '%{http_code}' "http://${LXC_IP}:8080/login" 2>/dev/null | grep -q 200; then
    msg_ok "HTTP 200 auf http://${LXC_IP}:8080/login"
  else msg_error "Login-Seite nicht erreichbar"; fail=1; fi

  if pct config "$CTID" | grep -q "onboot: 1"; then
    msg_ok "Autostart aktiv (onboot=1, restart=unless-stopped)"
  else msg_error "Autostart nicht gesetzt"; fail=1; fi

  echo
  if [[ $fail -eq 0 ]]; then
    echo -e "${GN}══════════════════════════════════════════════════════${RS}"
    echo -e "${GN} Fertig!${RS} Portal laeuft:  ${BL}http://${LXC_IP}:8080${RS}"
    echo -e "   Login:  ${YW}admin${RS} / ${YW}admin${RS}  → bitte Passwort aendern"
    echo -e "   Danach in den Einstellungen die Paperless-URL + Token eintragen."
    echo -e "${GN}══════════════════════════════════════════════════════${RS}"
  else
    msg_error "Selbstcheck mit Fehlern — bitte oben pruefen (CT $CTID, IP $LXC_IP)."
    exit 1
  fi
}

# ── Ablauf ───────────────────────────────────────────────────────────────────
header
require_host
ask_config
ensure_template
create_ct
install_app
self_check
trap - EXIT
