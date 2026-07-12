import datetime
import logging
import os
import re
import requests
import subprocess
import time
import yaml
from logging.handlers import RotatingFileHandler
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By


# Log to stderr (captured by systemd) and to a rotating file so the history
# survives reboots and can be inspected with `tail -f debug/meowifi.log`.
_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'debug')
os.makedirs(_log_dir, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(os.path.join(_log_dir, 'meowifi.log'),
                            maxBytes=512_000, backupCount=3),
    ],
)

# Load the YAML configuration file
with open('config.yaml', 'r') as file:
    config = yaml.safe_load(file)

def check_internet_connection():
    """Check for real internet access, not just an HTTP response.

    A captive portal (like MEO-WiFi's login page) answers any plain HTTP
    request with its own 200 OK page, so treating "got a response" as
    "online" gives a false positive while still stuck behind the portal.
    generate_204 is the same trick Android/ChromeOS use: a real internet
    connection gets an empty 204; a captive portal serves its login page
    instead, which won't be a bare 204.
    """
    try:
        response = requests.get("http://connectivitycheck.gstatic.com/generate_204", timeout=5)
        return response.status_code == 204
    except requests.RequestException as e:
        logging.error(f"Error connecting to the internet: {e}")
        return False

def is_connected():
    """Check if the device is connected to the specified network."""
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'active,ssid', 'dev', 'wifi'],
            capture_output=True,
            text=True,
            check=True
        )
        for line in result.stdout.splitlines():
            if not line.strip():  # Skip empty lines
                continue
            # nmcli -t escapes literal colons in field values as "\:"
            active, ssid = re.split(r'(?<!\\):', line)
            ssid = ssid.replace('\\:', ':')
            if active == 'yes' and ssid == config['network']['ssid']:
                return True
    except subprocess.CalledProcessError as e:
        logging.error(f"Error checking connection status: {e}")
    return False

def connect_to_network():
    ssid = config['network']['ssid']
    logging.info("Attempting to connect to " + ssid
                 + " on " + config['network']['device'] + "...")
    # A previous failed login may have disabled MEO-WiFi's autoconnect to hand
    # the uplink to another network (see disconnect_meo_wifi). Re-enable it so
    # normal operation is restored before we (re)associate.
    try:
        subprocess.run(['nmcli', 'connection', 'modify', ssid,
                        'connection.autoconnect', 'yes'], check=True)
    except subprocess.CalledProcessError:
        pass  # profile may not exist yet; the connect below will create it
    device = config['network']['device']
    # Prefer activating the saved profile: unlike `dev wifi connect`, it doesn't
    # need the SSID to be in the latest scan cache (which goes stale/empty after
    # rapid reconnects and then fails with "No network with SSID found").
    try:
        subprocess.run(['nmcli', 'connection', 'up', ssid, 'ifname', device], check=True)
        logging.info("Successfully connected to " + ssid + "!")
        return
    except subprocess.CalledProcessError:
        logging.warning(f"'connection up {ssid}' failed; rescanning and trying by SSID...")
    # Fallback: force a rescan, then connect by SSID (also creates the profile
    # if it doesn't exist yet).
    subprocess.run(['nmcli', 'device', 'wifi', 'rescan', 'ifname', device],
                   capture_output=True)
    time.sleep(4)
    try:
        subprocess.run(['nmcli', 'dev', 'wifi', 'connect', ssid, 'ifname', device], check=True)
        logging.info("Successfully connected to " + ssid + "!")
    except subprocess.CalledProcessError as e:
        logging.error(f"Error connecting to network: {e}")

def disconnect_other_networks():
    """Commit to MEO-WiFi: bring down every other active connection.

    Once we're on MEO-WiFi we don't want NetworkManager flapping to another
    saved network. This downs all active connections except MEO-WiFi itself,
    the repeater hotspot (which serves our clients on a separate interface),
    and loopback.
    """
    keep = {config['network']['ssid'], config['hotspot']['connection'], 'lo'}
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'NAME,TYPE', 'connection', 'show', '--active'],
            capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError as e:
        logging.error(f"Error listing active connections: {e}")
        return
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        # nmcli -t escapes literal colons in field values as "\:".
        name, ctype = (f.replace('\\:', ':') for f in re.split(r'(?<!\\):', line))
        if name in keep or ctype == 'loopback':
            continue
        logging.info(f"Disconnecting other network: {name!r}")
        try:
            subprocess.run(['nmcli', 'connection', 'down', name], check=True)
        except subprocess.CalledProcessError as e:
            logging.error(f"Error disconnecting {name!r}: {e}")

def disconnect_meo_wifi():
    """On login failure, stop using MEO-WiFi and restore the fallback uplink.

    We disable MEO-WiFi's autoconnect (it and the fallback share priority, and
    MEO-WiFi was just active, so NetworkManager would otherwise immediately
    re-grab it and loop forever) and then explicitly bring the fallback up.
    Explicitly is the operative word: because disconnect_other_networks() *down*ed
    the fallback by hand, NM will not auto-reconnect it on its own — leaving the
    Pi with no uplink at all. connect_to_network re-enables MEO-WiFi autoconnect
    the next time we deliberately choose it.
    """
    ssid = config['network']['ssid']
    logging.error(f"Login failed; disconnecting from {ssid} so the system can "
                  f"connect to another network.")
    try:
        subprocess.run(['nmcli', 'connection', 'modify', ssid,
                        'connection.autoconnect', 'no'], check=True)
    except subprocess.CalledProcessError as e:
        logging.error(f"Error disabling autoconnect for {ssid}: {e}")
    # Bringing the fallback up on the same device replaces MEO-WiFi, so there's
    # no separate `down` needed; activate_fallback also handles the no-fallback
    # case by logging a warning.
    activate_fallback()

def activate_fallback():
    """Restore the fallback uplink so the Pi is never left with no internet."""
    fallback = config['network'].get('fallback', {}).get('connection')
    if not fallback:
        logging.warning("No fallback connection configured; staying as-is.")
        return
    logging.error(f"Falling back to {fallback}...")
    try:
        subprocess.run(['nmcli', 'connection', 'up', fallback], check=True)
        logging.info(f"Fallback connection {fallback} is up.")
    except subprocess.CalledProcessError as e:
        logging.error(f"Error activating fallback connection: {e}")

def is_hotspot_active():
    """Check if the repeater hotspot connection is currently up."""
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'GENERAL.STATE', 'connection', 'show', config['hotspot']['connection']],
            capture_output=True,
            text=True,
            check=True
        )
        return 'activated' in result.stdout
    except subprocess.CalledProcessError as e:
        logging.error(f"Error checking hotspot status: {e}")
        return False

def start_hotspot():
    logging.info(f"Starting hotspot {config['hotspot']['connection']}...")
    try:
        subprocess.run(['nmcli', 'connection', 'up', config['hotspot']['connection']], check=True)
        logging.info(f"Hotspot {config['hotspot']['connection']} is up.")
    except subprocess.CalledProcessError as e:
        logging.error(f"Error starting hotspot: {e}")

def ensure_hotspot():
    if is_hotspot_active():
        logging.info(f"Hotspot {config['hotspot']['connection']} already active.")
    else:
        logging.error(f"Hotspot {config['hotspot']['connection']} is down. Starting it...")
        start_hotspot()

def find_first(driver, locators, description, require_visible=True):
    """Return the first (visible) element matching any of the locators, or None."""
    for by, value in locators:
        for element in driver.find_elements(by, value):
            if not require_visible or element.is_displayed():
                return element
    logging.warning(f"Could not find {description} on the portal page.")
    return None

def wait_for_first(driver, locators, description, timeout=15, require_visible=True):
    """Poll up to `timeout`s for the first (visible) element matching any locator.

    The MEO portal is an Angular app that renders its form fields progressively
    (each field is an ng-if that pops in a moment after its form container), so
    an instantaneous lookup can miss a field that is about to appear. This is
    why the password field intermittently came back 'missing' the instant the
    username field showed up. Unlike find_first, this waits it out.
    """
    deadline = time.time() + timeout
    while True:
        for by, value in locators:
            for element in driver.find_elements(by, value):
                if not require_visible or element.is_displayed():
                    return element
        if time.time() >= deadline:
            break
        time.sleep(0.5)
    logging.warning(f"Could not find {description} on the portal page (waited {timeout}s).")
    return None

def js_click(driver, element):
    """Click via scrollIntoView + JS, which works even when a styled overlay
    or Angular wrapper would make a native .click() fail as 'intercepted'."""
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center'}); arguments[0].click();", element)

def captcha_present(driver):
    """True if a *visible* bot-challenge (hCaptcha/reCAPTCHA) is on the page.

    When the saved MEO session has expired, the WebSSO login raises a challenge
    we can't solve unattended. Detecting it lets us stop retrying and emit a
    clear 're-seed needed' signal instead of burning attempts against a wall.
    An invisible/passive hCaptcha (the kind that passes silently) is not
    displayed, so it won't trip this.
    """
    selectors = ('iframe[src*="hcaptcha"]', 'iframe[src*="recaptcha"]',
                 '.h-captcha', '.g-recaptcha')
    for sel in selectors:
        for el in driver.find_elements(By.CSS_SELECTOR, sel):
            try:
                if el.is_displayed():
                    return True
            except Exception:
                pass
    return False

def tick_remember_me(driver):
    """Best-effort tick of a 'keep me signed in' checkbox on the WebSSO page.

    A remembered session lasts longer, so the repeater re-logs-in less often
    (and, when it does, is likelier to sail through without a captcha). The
    box's id/name varies between portal versions and may be absent entirely, so
    match a remember/save checkbox and quietly do nothing if there isn't one.
    """
    locators = [
        (By.ID, 'save_credentials'),                          # pre-2026 portal
        (By.CSS_SELECTOR, 'input[type="checkbox"][id*="emember"]'),
        (By.CSS_SELECTOR, 'input[type="checkbox"][name*="emember"]'),
        (By.CSS_SELECTOR, 'input[type="checkbox"][id*="ave"]'),
    ]
    for by, value in locators:
        for box in driver.find_elements(by, value):
            try:
                if not box.is_selected():
                    js_click(driver, box)
                    logging.info(f"Ticked 'keep me signed in' ({value}).")
                return
            except Exception:
                pass

def dump_debug_artifacts(driver, tag):
    """Save a screenshot and page source so a failed login can be diagnosed later."""
    try:
        debug_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'debug')
        os.makedirs(debug_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        base = os.path.join(debug_dir, f'{stamp}-{tag}')
        driver.save_screenshot(base + '.png')
        with open(base + '.html', 'w') as f:
            f.write(driver.page_source)
        logging.info(f"Saved debug artifacts: {base}.png / .html "
                     f"(page was: {driver.current_url!r}, title: {driver.title!r})")
    except Exception as e:
        logging.error(f"Could not save debug artifacts: {e}")

def login_meo_wifi():
    """Log in to the MEO WiFi captive portal using Selenium.

    Flow (as of 2026-07): go straight to https://meowifi.meo.pt/ (no more
    neverssl probe redirect). The Angular portal may show an ad ("Close Ad");
    we tick the "I have read and accept the terms and conditions" checkbox,
    click Continue, and that redirects to the classic MEO ID / WebSSO login
    (login.telecom.pt) where we enter the email + password.
    """
    driver = None
    try:
        service = Service(executable_path=config['chromedriver']['path'])
        options = webdriver.ChromeOptions()
        browser = config.get('browser', {})
        if browser.get('binary'):
            options.binary_location = browser['binary']
        if browser.get('headless', True):
            options.add_argument('--headless=new')
        options.add_argument('--window-size=1366,900')
        # Reuse a persistent Chrome profile so a MEO ID session established once
        # (e.g. via manual_login.py, solving the captcha by hand) carries over:
        # a valid session cookie lets the portal auto-authenticate without ever
        # showing the login form or the hCaptcha bot-challenge.
        profile_dir = browser.get('profile_dir', 'chrome-profile')
        if profile_dir:
            if not os.path.isabs(profile_dir):
                profile_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), profile_dir)
            options.add_argument(f'--user-data-dir={profile_dir}')
        # Look less like automation, so hCaptcha is more likely to pass silently.
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option('excludeSwitches', ['enable-automation'])
        options.add_experimental_option('useAutomationExtension', False)
        # The portal loads Google's ad SDK (ima3.js), which hangs behind the
        # captive portal before we're authenticated. 'none' makes get() return
        # immediately so that hanging resource can't block us; we then drive the
        # DOM with explicit polling below.
        options.page_load_strategy = 'none'
        driver = webdriver.Chrome(service=service, options=options)
        driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument',
            {'source': "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"})
        driver.set_page_load_timeout(45)

        portal_url = (config['meowifi'].get('portal_url')
                      or config['meowifi'].get('probe_url')
                      or 'https://meowifi.meo.pt/')
        max_attempts = config['meowifi'].get('login_attempts', 3)

        # The real login form is the WebSSO page, not the Angular portal's own
        # voucher/recover fields (those look like a login form but aren't).
        username_locators = [
            (By.ID, 'ContentPlaceHolder1_LoginTemplate_Template_WebSSOUsernameTextBox'),
            (By.CSS_SELECTOR, 'input[name$="WebSSOUsernameTextBox"]'),
            (By.ID, 'user'),                          # pre-2026 portal
            (By.CSS_SELECTOR, 'input[type="email"]'),
        ]

        # The portal SPA sometimes never renders past its loading spinner (the
        # `no-login-form` failures), and a submitted login can also fail to bring
        # the session up. Both usually clear on a fresh navigation, so drive the
        # whole flow in a retry loop and only give up (handing back to the
        # caller's back-off) once every attempt has failed. A genuine captcha is
        # the exception: it can't be solved unattended, so we bail immediately.
        for attempt in range(1, max_attempts + 1):
            logging.info(f"MEO-WiFi login attempt {attempt}/{max_attempts}.")
            driver.get(portal_url)
            time.sleep(8)  # let the SPA render (page load never "completes")
            logging.info(f"Portal landed on: {driver.current_url!r} (title: {driver.title!r})")

            # Walk the portal wizard until the WebSSO login form appears: dismiss
            # the ad if present, tick the terms checkbox (via its label — the
            # input is style-hidden, so clicking it directly does nothing), then
            # click Continue, which becomes enabled once terms are accepted.
            deadline = time.time() + 60
            username_field = None
            while time.time() < deadline:
                # A saved session may auto-authenticate after "Continue" without
                # ever showing the login form — if we're already online, done.
                if check_internet_connection():
                    logging.info("Already authenticated via saved session; internet is up.")
                    return
                username_field = find_first(driver, username_locators, "login username field")
                if username_field is not None:
                    break
                for btn in driver.find_elements(By.CSS_SELECTOR, 'button.skip-button'):
                    try:
                        if btn.is_displayed() and btn.is_enabled():
                            js_click(driver, btn)
                            logging.info("Closed ad interstitial.")
                            break
                    except Exception:
                        pass
                for label in driver.find_elements(By.CSS_SELECTOR, 'label[for*="CheckboxTerms"]'):
                    try:
                        if not label.is_displayed():
                            continue
                        boxes = driver.find_elements(By.ID, label.get_attribute('for'))
                        if boxes and not boxes[0].is_selected():
                            js_click(driver, label)
                            logging.info(f"Accepted terms ({label.get_attribute('for')}).")
                    except Exception:
                        pass
                for btn in driver.find_elements(By.CSS_SELECTOR, 'button'):
                    try:
                        text = (btn.text or '').strip().lower()
                        if (btn.is_displayed() and btn.is_enabled()
                                and text in ('continue', 'continuar', 'connect')):
                            js_click(driver, btn)
                            logging.info(f"Clicked proceed button: {text!r}")
                            break
                    except Exception:
                        pass
                time.sleep(2)

            if username_field is None:
                # A real captcha is unrecoverable without a human: stop retrying
                # and flag it loudly (this is the re-seed signal). Otherwise it's
                # the transient spinner — reload and try again.
                if captcha_present(driver):
                    dump_debug_artifacts(driver, 'captcha-required')
                    logging.error("CAPTCHA REQUIRED: a bot-challenge is blocking login; the "
                                  "saved session has expired. Re-seed with ./seed_profile.sh.")
                    return
                dump_debug_artifacts(driver, 'no-login-form')
                logging.warning(f"WebSSO login form never appeared (attempt "
                                f"{attempt}/{max_attempts}); reloading the portal.")
                continue

            password_field = wait_for_first(driver, [
                (By.ID, 'ContentPlaceHolder1_LoginTemplate_Template_WebSSOPasswordTextBox'),
                (By.CSS_SELECTOR, 'input[name$="WebSSOPasswordTextBox"]'),
                (By.ID, 'password'),
                (By.CSS_SELECTOR, 'input[type="password"]'),
            ], "password field")
            if password_field is None:
                dump_debug_artifacts(driver, 'missing-fields')
                continue

            # The form is up; a challenge here is equally unrecoverable unattended.
            if captcha_present(driver):
                dump_debug_artifacts(driver, 'captcha-required')
                logging.error("CAPTCHA REQUIRED: a bot-challenge is on the login form; the "
                              "saved session has expired. Re-seed with ./seed_profile.sh.")
                return

            username_field.send_keys(config['meowifi']['username'])
            password_field.send_keys(config['meowifi']['password'])
            tick_remember_me(driver)

            # Click the WebSSO "Entrar" submit button (falls back to submitting
            # the form directly). The terms checkbox lives on the earlier portal
            # step, so there's nothing to tick here.
            submit_button = wait_for_first(driver, [
                (By.ID, 'ContentPlaceHolder1_LoginTemplate_Template_WebSSOSubmitButton'),
                (By.CSS_SELECTOR, 'button[name="SubmitButton"]'),
                (By.CSS_SELECTOR, 'button[type="submit"]'),
                (By.CSS_SELECTOR, 'input[type="submit"]'),
            ], "submit button", timeout=15)
            if submit_button is not None:
                js_click(driver, submit_button)
            else:
                logging.warning("No enabled submit button found; submitting the form directly.")
                password_field.submit()

            # Poll for real internet access instead of blindly sleeping, so we
            # know whether the login actually worked.
            for _ in range(10):
                time.sleep(2)
                if check_internet_connection():
                    logging.info("Internet access confirmed after login.")
                    return
            dump_debug_artifacts(driver, 'login-not-confirmed')
            logging.warning(f"Login submitted but internet not confirmed (attempt "
                            f"{attempt}/{max_attempts}).")

        logging.error(f"MEO-WiFi login failed after {max_attempts} attempts.")
    except Exception as e:
        logging.error(f"Error during login: {e}")
        if driver is not None:
            dump_debug_artifacts(driver, 'exception')
    finally:
        if driver is not None:
            driver.quit()

BACKOFF_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.meo_backoff')

def _boot_time():
    """Epoch seconds when the system last booted."""
    with open('/proc/uptime') as f:
        return time.time() - float(f.read().split()[0])

def meo_in_backoff():
    """True while a recent MEO login failure should keep us on the fallback.

    Without this, every scheduled run would yank the uplink back to MEO-WiFi to
    re-test the login — dropping the hotspot's internet each time if MEO keeps
    demanding a login we can't complete (e.g. the captcha). The back-off lets us
    ride the fallback quietly until it's worth trying MEO again.

    A reboot is a clean retry point: whatever transient thing broke the login is
    gone, so a back-off written before this boot is stale and must be ignored,
    otherwise the Pi comes up on the fallback and never tries MEO on its own.
    """
    try:
        if os.path.getmtime(BACKOFF_FILE) < _boot_time():
            clear_meo_backoff()
            return False
        with open(BACKOFF_FILE) as f:
            return time.time() < float(f.read().strip())
    except (OSError, ValueError):
        return False

def set_meo_backoff():
    minutes = config['network'].get('meo_retry_backoff_minutes', 30)
    try:
        with open(BACKOFF_FILE, 'w') as f:
            f.write(str(time.time() + minutes * 60))
        logging.info(f"Backing off MEO-WiFi retries for {minutes} min.")
    except OSError as e:
        logging.error(f"Could not write back-off file: {e}")

def clear_meo_backoff():
    try:
        os.remove(BACKOFF_FILE)
    except OSError:
        pass

def main():
    # Check if actually associated with the MEO-WiFi SSID (not just "some" internet)
    if not is_connected():
        # If MEO login failed recently and the fallback still has internet, stay
        # put rather than thrashing back to MEO every run.
        if meo_in_backoff() and check_internet_connection():
            logging.info("In MEO-WiFi back-off window and already online; staying on the fallback.")
            ensure_hotspot()
            return

        logging.error("Not connected to " + config['network']['ssid'])

        retry_count = 0

        while not is_connected() and retry_count < config['network']['retry']['attempts']:
            connect_to_network()
            time.sleep(config['network']['retry']['interval'])
            retry_count += 1

        if not is_connected():
            logging.error(f"Failed to connect to the network after {config['network']['retry']['attempts']} attempts.")
            activate_fallback()
            ensure_hotspot()
            return

        logging.info("Successfully connected to the network.")

    # We're on MEO-WiFi: commit to it and stop competing with other networks.
    disconnect_other_networks()

    if check_internet_connection():
        logging.info("Connected to the internet. Nothing to do!")
        clear_meo_backoff()
    else:
        logging.info("Connected to the network but no internet. Logging in to MEO-WiFi...")
        login_meo_wifi()
        if check_internet_connection():
            clear_meo_backoff()
        else:
            logging.error("Still no internet after the login attempt.")
            # Hand the uplink back so the system can connect to another network,
            # and don't thrash back to MEO until the back-off expires.
            disconnect_meo_wifi()
            set_meo_backoff()

    ensure_hotspot()

if __name__ == "__main__":
    main()
