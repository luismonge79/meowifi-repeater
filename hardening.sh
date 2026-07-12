#!/usr/bin/env bash
# One-shot SD-card wear hardening for the Pi running the MEO-WiFi repeater.
# SD cards die from *writes*, so this cuts the routine ones the OS makes:
#   1. noatime  - stop a disk write on every file *read* (atime updates).
#   2. journald - keep the systemd journal in RAM (we already keep our own
#                 persistent log in debug/meowifi.log), capped in size.
#   3. swappiness=1 - almost never swap to the card (keeps swap as a safety net
#                 without routine paging). Pass --disable-swap to remove it
#                 entirely (only do this if the Pi has RAM to spare).
#
# Idempotent: safe to run repeatedly. Requires root:  sudo ./hardening.sh
set -euo pipefail

DISABLE_SWAP=0
[ "${1:-}" = "--disable-swap" ] && DISABLE_SWAP=1

if [ "$(id -u)" -ne 0 ]; then
  echo "!! Must run as root:  sudo $0 ${1:-}" >&2
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

echo ">> 1/3  noatime on /"
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

echo ">> 2/3  journald in RAM (capped)"
JC=/etc/systemd/journald.conf
set_kv "$JC" Storage volatile
set_kv "$JC" RuntimeMaxUse 20M
systemctl restart systemd-journald
echo "   journal now stored in RAM, capped at 20M."
echo "   NOTE: journal history is lost on reboot — persistent history still"
echo "         lives in debug/meowifi.log (that's the point)."

echo ">> 3/3  swap"
SYSCTL=/etc/sysctl.d/99-meowifi-hardening.conf
if [ "$DISABLE_SWAP" -eq 1 ]; then
  if command -v dphys-swapfile >/dev/null 2>&1; then
    dphys-swapfile swapoff 2>/dev/null || true
    systemctl disable --now dphys-swapfile 2>/dev/null || true
    echo "   disabled dphys-swapfile (swap removed)."
  else
    swapoff -a 2>/dev/null || true
    echo "   swapoff -a done (no dphys-swapfile present)."
  fi
  rm -f "$SYSCTL"
else
  echo 'vm.swappiness=1' > "$SYSCTL"
  sysctl -q -w vm.swappiness=1
  echo "   set vm.swappiness=1 (swap kept as a safety net, but rarely used)."
  echo "   re-run with --disable-swap to remove swap entirely."
fi

cat <<EOF

>> DONE. Applied live where possible; a reboot makes everything take full effect.
   Undo noatime:   restore /etc/fstab.hardening.bak
   Check swap use: free -h ; cat /proc/sys/vm/swappiness
   Check journal:  journalctl --disk-usage
EOF
