#!/usr/bin/env bash
# One-shot setup for the MEO-WiFi repeater. Installs the Python venv, the
# systemd *user* timer/service, and prints the two sudo commands you still need
# to run by hand. Safe to re-run.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
PY="$REPO/.venv/bin/python"
UNIT_DIR="$HOME/.config/systemd/user"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

echo ">> Python venv + dependencies"
[ -x "$PY" ] || python3 -m venv "$REPO/.venv"
"$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt"

echo ">> config.yaml"
if [ ! -f "$REPO/config.yaml" ]; then
  cp "$REPO/config-sample.yaml" "$REPO/config.yaml"
  echo "   created config.yaml from the sample — EDIT YOUR CREDENTIALS before use!"
fi

echo ">> hotspot AP profile"
HOTSPOT_CON="$("$PY" -c "import yaml;print(yaml.safe_load(open('$REPO/config.yaml'))['hotspot']['connection'])" 2>/dev/null || echo MONGE-PI)"
if nmcli -t -f NAME connection show 2>/dev/null | grep -qx "$HOTSPOT_CON"; then
  echo "   '$HOTSPOT_CON' already exists — leaving it."
else
  echo "   '$HOTSPOT_CON' missing — creating it (you'll be asked for the AP password)."
  HOTSPOT_CON="$HOTSPOT_CON" "$REPO/setup-hotspot.sh"
fi

echo ">> systemd user units"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/meowifi.service" <<EOF
[Unit]
Description=MEO-WiFi repeater: keep the uplink logged in and the hotspot up
After=graphical-session.target

[Service]
Type=oneshot
WorkingDirectory=$REPO
# DISPLAY/XAUTHORITY let the (headful) login browser render in the desktop
# session; harmless when no browser is needed.
Environment=DISPLAY=:0
Environment=XAUTHORITY=$HOME/.Xauthority
ExecStart=$PY $REPO/meowifi.py
EOF

cat > "$UNIT_DIR/meowifi.timer" <<EOF
[Unit]
Description=Run the MEO-WiFi repeater check periodically

[Timer]
OnBootSec=2min
OnUnitActiveSec=3min
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now meowifi.timer
echo "   timer enabled:"
systemctl --user list-timers meowifi.timer --no-pager | head -2

cat <<EOF

>> DONE. Two things still need sudo (run them yourself):

   # 1. Run the timer at boot even without an interactive login:
   sudo loginctl enable-linger $USER

   # 2. Auto-reboot if the Pi ever hangs (hardware watchdog):
   sudo sed -i 's/^#\\?RuntimeWatchdogSec=.*/RuntimeWatchdogSec=15/' /etc/systemd/system.conf
   sudo systemctl daemon-reexec

Then seed the login once by hand (solves any captcha in a real browser):
   ./seed_profile.sh

Watch it run:  tail -f debug/meowifi.log
EOF
