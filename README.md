# meowifi-repeater

Turns a Raspberry Pi into a self-healing Wi-Fi repeater that rides a
**MEO-WiFi** community hotspot as its uplink and re-broadcasts it on its own
access point. It keeps the uplink authenticated, keeps the hotspot up, falls
back to a second network if MEO is unavailable, and recovers on its own after a
power cut or a hang.

## How it actually works

MEO-WiFi (`COMUNIDADE_WIFI`) authorises a device by its **MAC address** once it
has logged in. So most of the time there is nothing to log in for — joining the
network just works. The script leans on that:

Each run (`meowifi.py`, fired every ~3 min by a systemd timer):

1. **Ensure we're on MEO-WiFi.** If not associated, activate the saved profile
   (`nmcli connection up`, which — unlike `dev wifi connect` — doesn't depend on
   a fresh scan). Retry a few times; if it can't, bring up the **fallback**
   network and keep the hotspot alive.
2. **Commit to MEO-WiFi.** Disconnect other uplinks so NetworkManager doesn't
   wander (the hotspot on its own interface is left alone).
3. **Check for real internet** (an HTTP `generate_204` probe — a captive portal
   answers with its own page instead of a bare 204, so this can't be fooled).
   - **Online** → nothing to do.
   - **No internet** → we've been de-authorised. Drive a **real browser** login
     (see below). If it works, great. If not, hand the uplink back to the
     **fallback** and **back off** from retrying MEO for a while (default 30 min)
     so the hotspot's internet doesn't flap every run.
4. **Ensure the hotspot** (`MONGE-PI`) is up.

### The login, and the captcha

The login only matters in the window after MEO forgets the device. MEO's login
for community hotspots goes through their WebSSO/identity page, which is
protected by an **hCaptcha**. Key facts we learned the hard way:

- A **headless** browser is flagged and gets an unsolvable image challenge.
- A **real (headful) browser** passes the invisible captcha silently.
- The public "SCL" login API (`session-login`, used by community scripts on
  *other* MEO products) returns `LOCATION_INFO` here and does **not** work for
  `COMUNIDADE_WIFI`.

So the login browser runs **headful** on the Pi's own display (`DISPLAY=:0`)
using a **persistent Chrome profile** you seed once by hand (`seed_profile.sh`).
MEO's session has a **fixed lifetime — refreshing does not extend it** — so when
it expires the repeater has to log in again. Whether an *automated* headful login
clears the captcha unattended is the one thing only real use will confirm; if it
can't, you re-seed occasionally with `seed_profile.sh`.

## First-time setup

System packages (Raspberry Pi OS / Debian):

```bash
sudo apt update
sudo apt install -y python3-venv network-manager chromium chromium-driver
```

Then, from the repo:

```bash
./install.sh          # venv + deps, systemd user timer/service, config.yaml
# edit config.yaml with your details, then run the two sudo commands it prints:
sudo loginctl enable-linger "$USER"                     # start at boot, no login needed
sudo sed -i 's/^#\?RuntimeWatchdogSec=.*/RuntimeWatchdogSec=15/' /etc/systemd/system.conf
sudo systemctl daemon-reexec                            # arm the hardware watchdog
./seed_profile.sh     # log in by hand once to seed the browser profile
```

`install.sh` is safe to re-run and is the whole reproducible setup — after a
re-flash, run it again and you're back.

## Configuration (`config.yaml`)

```yaml
network:
   device: wlan0                 # uplink interface
   ssid: MEO-WiFi                # network to ride
   retry:
      interval: 5                # seconds between reconnect attempts
      attempts: 6                # max attempts before falling back
   meo_retry_backoff_minutes: 30 # after a failed login, stay on fallback this long
   fallback:
      connection: netplan-wlan0-Vodafone-3C9D4E   # nmcli connection to use if MEO fails

hotspot:
   connection: MONGE-PI          # nmcli AP profile (mode=ap, ipv4.method=shared)
   psk: your-ap-password         # only used by setup-hotspot.sh to recreate the AP

meowifi:
   portal_url: https://meowifi.meo.pt/
   username: your-email@example.com
   password: your-password

browser:
   binary: /usr/bin/chromium
   headless: false               # headful passes hCaptcha; needs DISPLAY=:0
   profile_dir: chrome-profile   # persistent profile, shared with seed_profile.sh

chromedriver:
   path: /usr/bin/chromedriver
```

`config.yaml`, `chrome-profile/`, `debug/` and `.meo_backoff` are gitignored.

## Operating it

- **It runs itself** via the systemd user timer. Nothing to start by hand.
- **Watch it:** `tail -f debug/meowifi.log`
- **Check the schedule:** `systemctl --user list-timers meowifi.timer`
- **Run once now:** `DISPLAY=:0 .venv/bin/python meowifi.py`
- **Re-seed the login by hand** (only if MEO forgets the device *and* auto-login
  can't get back in): `./seed_profile.sh` at the Pi's screen.

## Resilience

- **Power cut:** the Pi auto-boots when power returns; `enable-linger` starts the
  timer at boot without a login.
- **Hang:** the hardware watchdog reboots the Pi if systemd stops responding.
- **MEO down / login fails:** falls back to the configured network, with a
  back-off so the hotspot's internet doesn't flap.
- **Concurrent runs:** systemd won't start a second `meowifi.service` while one
  is still running, so a slow login can't overlap the next tick.

## Files

| File | Purpose |
|------|---------|
| `meowifi.py` | The repeater logic (run each tick). |
| `seed_profile.sh` | One-time/occasional manual browser login to seed the profile. |
| `setup-hotspot.sh` | Recreate the `MONGE-PI` access point (run once after a re-flash). |
| `install.sh` | Reproducible setup (venv, systemd units, hotspot). |
| `config.yaml` | Your settings (gitignored). |
| `debug/meowifi.log` | Rotating run log. |

## Known limitations / not-yet-foolproof

- **SD-card wear** is the usual 24/7-Pi failure mode. For true set-and-forget,
  boot from a quality card or a USB SSD, and consider a read-only/overlay root.
- **Unattended re-login past the captcha is unproven.** MEO's session expires on
  a fixed timer that nothing extends, so a re-login is eventually unavoidable. If
  the log shows repeated `Login submitted but internet access was not confirmed`,
  the headful auto-login is being captcha'd and you'll need the occasional
  `seed_profile.sh`.
- **No external alerting.** If you want to know when it's been stuck on the
  fallback, add a ping to a service like healthchecks.io in the "nothing to do"
  branch.
- **Abrupt power loss** can corrupt the SD card; a small UPS avoids that.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
