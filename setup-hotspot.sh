#!/usr/bin/env bash
# Recreate the Wi-Fi access point the repeater rebroadcasts on (default
# MONGE-PI on wlan1, WPA2, sharing the uplink via NAT/DHCP). Run once after a
# fresh flash. The AP password comes from (in order): $HOTSPOT_PSK, the
# `hotspot.psk` field in config.yaml (gitignored), or an interactive prompt.
#
#   HOTSPOT_PSK='yourpassword' ./setup-hotspot.sh     # explicit
#   ./setup-hotspot.sh                                 # reads config.yaml / prompts
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"

cfg() {  # read a hotspot.<key> from config.yaml, empty if unavailable
  [ -x "$REPO/.venv/bin/python" ] && [ -f "$REPO/config.yaml" ] || return 0
  "$REPO/.venv/bin/python" -c "import yaml;print(yaml.safe_load(open('$REPO/config.yaml')).get('hotspot',{}).get('$1','') or '')" 2>/dev/null || true
}

CON="${HOTSPOT_CON:-$(cfg connection)}"; CON="${CON:-MONGE-PI}"
SSID="${HOTSPOT_SSID:-$CON}"
IFACE="${HOTSPOT_IFACE:-wlan1}"
PSK="${HOTSPOT_PSK:-$(cfg psk)}"

if [ -z "$PSK" ]; then
  read -rsp "AP password for '$SSID' (min 8 chars): " PSK; echo
fi
if [ "${#PSK}" -lt 8 ]; then
  echo "Password must be at least 8 characters." >&2; exit 1
fi

# Replace any existing profile of the same name.
nmcli connection delete "$CON" >/dev/null 2>&1 || true
nmcli connection add type wifi ifname "$IFACE" con-name "$CON" ssid "$SSID" \
  autoconnect yes \
  802-11-wireless.mode ap \
  802-11-wireless-security.key-mgmt wpa-psk \
  802-11-wireless-security.psk "$PSK" \
  ipv4.method shared

echo "Created AP '$CON' on $IFACE. The repeater brings it up automatically;"
echo "to start it now: nmcli connection up '$CON'"
