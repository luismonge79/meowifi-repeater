import logging
import requests
import subprocess
import time
import yaml
from selenium import webdriver
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By


# Configure logging
logging.basicConfig(level=logging.INFO)

# Load the YAML configuration file
with open('config.yaml', 'r') as file:
    config = yaml.safe_load(file)

def check_internet_connection():
    try:
        requests.get("http://www.google.com", timeout=5)
        # If the request is successful, no error is raised, and we return True.
        return True
    except requests.ConnectionError as e:
        # This means the request failed to connect to the internet.
        logging.error("Error connecting to the internet: {e}")
        return False
    except requests.Timeout as e:
        # This means the request timed out.
        logging.error("Connection timed out: {e}")
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
            active, ssid = line.split(':')
            if active == 'yes' and ssid == config['network']['ssid']:
                return True
    except subprocess.CalledProcessError as e:
        logging.error("Error checking connection status: {e}")
    return False

def connect_to_network():
    logging.info("Attempting to connect to " + config['network']['device'] + "...")
    try:
        subprocess.run(
            ['nmcli', 'dev', 'wifi', 'connect', config['network']['ssid'], 'ifname', config['network']['device']],
            check=True
        )
        logging.info("Successfully connected to " + config['network']['ssid'] + "!")
    except subprocess.CalledProcessError as e:
        logging.error("Error connecting to network: {e}")

def login_meo_wifi():
    """Log in to the Meo WiFi network using Selenium."""
    try:
        driver = webdriver.Chrome(config['chromedriver']['path'])
        driver.get(config['meowifi.url'])

        username_field = driver.find_element(By.ID, 'user')
        password_field = driver.find_element(By.ID, 'password')
        username_field.send_keys(config['meowifi']['username'])
        password_field.send_keys(config['meowifi']['password'])

        remember_box = driver.find_element(By.ID, 'save_credentials')
        terms_box = driver.find_element(By.ID, 'conditions')
        remember_box.click()
        terms_box.click()

        submit_button = driver.find_element(By.ID, 'login')
        submit_button.click()

        time.sleep(10)  # Wait for 10 seconds
    except Exception as e:
        logging.error("Error during login: {e}")
    finally:
        driver.quit()

def main():
    # Check if connected to the internet
    if check_internet_connection() and 0:
        logging.info("Connected to the internet. Nothing to do!")
    else:
        logging.error("Not connected to the internet")

        retry_count = 0

        while not is_connected() and retry_count < config['network']['retry']['attempts']:
            connect_to_network()
            time.sleep(config['network']['retry']['interval'])
            retry_count += 1
        
        if retry_count == config['network']['retry']['attempts']:
            logging.error("Failed to connect to the network after 6 attempts.")
            return
        else:
            logging.info("Successfully connected to the network.")
    
        logging.info("Connected to the network. Logging in to MEO-WiFi...")
        login_meo_wifi()

if __name__ == "__main__":
    main()
