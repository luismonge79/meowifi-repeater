#!/usr/bin/env bash
# One-shot SD-card wear hardening for the Pi running the MEO-WiFi repeater.
# SD cards die from *writes*, so this cuts the routine ones the OS makes:
#   1. noatime  - stop a disk write on every file *read* (atime updates).
#   2. journald - keep the systemd journal in RAM (we already keep our own
#                 persistent log in debug/meowifi.log), capped in size.
#
# Swap is intentionally left alone: this Pi swaps to zram (a compressed RAM
# block device), not to the SD card, so swappiness/disabling swap does nothing
# for card wear. Nothing to harden there.
#
# Idempotent: safe to run repeatedly. Requires root:  sudo ./hardening.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "!! Must run as root:  sudo $0" >&2
  exit 1
fi

# Set key=value in a config file: rewrite an active line, uncomment a commented
# default, or append — whichever applies. Keeps the file idempotent.
set_kv() {
  local f=$1 k=$2 v=$3
  if grep -qE "^${k}=" "$f"; then
    sed -i "s|^${k}=.*|${k}=${v}|" "$f"
  elif grep -qE "^#\s*${k}=" "$f"; then
    sed -i "s|^#\s*${k}=.*|${k}=${v}|" "$f"
  else
    echo "${k}=${v}" >> "$f"
  fi
}

echo ">> 1/2  noatime on /"
if awk '!/^#/ && $2=="/"{print $4}' /etc/fstab | grep -q noatime; then
  echo "   / already has noatime — skipping."
else
  cp -n /etc/fstab /etc/fstab.hardening.bak
  # Append ',noatime' to the options (field 4) of the root (/) entry only.
  awk 'BEGIN{OFS="\t"}
       !/^#/ && $2=="/" && $4 !~ /(^|,)noatime(,|$)/ {$4=$4",noatime"}
       {print}' /etc/fstab > /etc/fstab.hardening.tmp
  mv /etc/fstab.hardening.tmp /etc/fstab
  mount -o remount,noatime / 2>/dev/null || true
  echo "   added noatime to / (backup: /etc/fstab.hardening.bak; live-remounted)."
fi

echo ">> 2/2  journald in RAM (capped)"
JC=/etc/systemd/journald.conf
set_kv "$JC" Storage volatile
set_kv "$JC" RuntimeMaxUse 20M
systemctl restart systemd-journald
echo "   journal now stored in RAM, capped at 20M."
echo "   NOTE: journal history is lost on reboot — persistent history still"
echo "         lives in debug/meowifi.log (that's the point)."

cat <<EOF

>> DONE. Applied live where possible; a reboot makes everything take full effect.
   Undo noatime:  restore /etc/fstab.hardening.bak
   Check journal: journalctl --disk-usage
EOF
