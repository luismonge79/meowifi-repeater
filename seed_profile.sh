#!/usr/bin/env bash
# Log in to MEO WiFi in a REAL Chromium (NOT Selenium) so hCaptcha behaves
# normally, seeding the shared chrome-profile that meowifi.py later reuses.
# Run this at the Pi's screen:  ./seed_profile.sh
set -u
export DISPLAY="${DISPLAY:-:0}"
DIR="$(cd "$(dirname "$0")" && pwd)"
PROFILE="$DIR/chrome-profile"

echo ">> Forcing wlan0 onto MEO-WiFi (the Pi's internet will drop until you log in)..."
# `dev wifi connect` alone doesn't reliably kick an already-active connection
# (e.g. Vodafone) off wlan0, so drop whatever's there first, then bring MEO-WiFi
# up explicitly and confirm it actually took the interface.
nmcli connection modify MEO-WiFi connection.autoconnect yes 2>/dev/null || true
nmcli device disconnect wlan0 2>/dev/null || true
nmcli connection up MEO-WiFi ifname wlan0 2>/dev/null \
  || nmcli device wifi connect MEO-WiFi ifname wlan0 2>/dev/null || true
inuse=""
for _ in $(seq 1 12); do
  inuse="$(nmcli -t -f NAME,DEVICE connection show --active | awk -F: '$2=="wlan0"{print $1}')"
  [ "$inuse" = "MEO-WiFi" ] && break
  sleep 1
done
if [ "$inuse" != "MEO-WiFi" ]; then
  echo "!! Could not put wlan0 on MEO-WiFi (currently: ${inuse:-none}). The portal"
  echo "!! won't load. Fix manually: nmcli device disconnect wlan0 && nmcli connection up MEO-WiFi"
fi

# A Selenium run may have left a lock that stops a normal launch.
rm -f "$PROFILE/SingletonLock" "$PROFILE/SingletonCookie" "$PROFILE/SingletonSocket" 2>/dev/null || true

echo ">> Opening a normal Chromium window. Log in BY HAND:"
echo "     Close Ad -> tick terms -> Continue -> email + password -> solve captcha."
echo ">> When you can browse the internet, just CLOSE the Chromium window to continue."
chromium --user-data-dir="$PROFILE" --no-first-run --no-default-browser-check \
         https://meowifi.meo.pt/ >/dev/null 2>&1

echo ">> Browser closed. Checking connectivity..."
if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 \
        http://connectivitycheck.gstatic.com/generate_204)" = "204" ]; then
  echo ">> ONLINE via MEO-WiFi. Session seeded successfully."
  echo ">> Now test the automation:  .venv/bin/python meowifi.py"
else
  echo ">> Still not online. Restoring Vodafone fallback..."
  nmcli connection up netplan-wlan0-Vodafone-3C9D4E
fi
