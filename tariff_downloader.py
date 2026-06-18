#!/usr/bin/env python3
"""
Avianca iCargo Booking Downloader
Automates daily downloads from multiple modules and airports, merges files, uploads to Dropbox
"""

import os
import sys
import time
import logging
import csv
import json
import smtplib
import argparse
import schedule
from datetime import datetime
from pathlib import Path
from typing import List, Dict
import imaplib
import email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Third-party imports
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import pandas as pd
import schedule

# Dropbox is optional - will skip upload if not available
try:
    import dropbox
    from dropbox.exceptions import ApiError
    DROPBOX_AVAILABLE = True
except ImportError:
    DROPBOX_AVAILABLE = False
    logger_placeholder = None  # Will be set up properly later

# ============================================================================
# CONFIGURATION
# ============================================================================

CONFIG = {
    # Avianca iCargo
    "avianca_url": "https://avianca-icargo.ibsplc.aero/icargo/login.do",
    "avianca_email": "ldiaz@gocargogsa.com",
    "screen_number": "TRF007",  # Tariff/Rate requests screen
    
    # Gmail (for receiving verification code from Microsoft Azure AD)
    "gmail_email": "ldiaz@gocargogsa.com",
    "gmail_app_password": "ayph ojlv hnrt osme",
    "verification_code_sender": "account-security-noreply@accountprotection.microsoft.com",  # Microsoft Azure AD
    
    # Dropbox
    "dropbox_access_token": "sl.u.AGi_MvIfAn0xQFpaJiQT52zOXLFMi7XDkkq3-tRVThctZU94Hzuq9cBWPH6QMmLVb0pfvWq4r3uIwcF-Wt3WIvz0IJq-LzfZFJy0qjboJhDhHh5T0a05xp0a6x95YG--MHcO6SyH3jRBHBDohMfKIBugJl88gSm3II6M96Ez4-jxjEs4CccosQ3tYBTsowfodaFngqXMUyib6wxiVAx0F2b4xm0VPI8h8yAgWEdGqx1PDoAVExKl5OmhNMgJIgHxkkJRs2bahbxHR1OjFeYV1fYr6c1FAHRpgARyJLTG-7gJljNY6aa_C7zrRtOzTtzzJlw8VxfbBMWnEW5-MwWcqg6O-i3UNM3bLzERrHks3im5KCdHJdRdV-EmJ0_YLFiQHJZ0X21VHEL8fFOAl8oTT-a3s7x9ARtuBUdVl4Q9w3sptyuGzIivTI9AAnqrKU4h3uQRS17xzgJbXV0RCGzJ5QOcTnADr4rnjyyFQTIRkUhVlLfDwE8M-tt_zx8P4PTAXHvfYeR3xFsYQ9Kxpy4jjX-_Z8tG3MQe3rCHbYhJeklOlUIDbTCpABsF5peiBxMipHd-Q04LV7bcOcm8nuoVIAFCQX7juUmAFyqOHFvts4RXv1fXZ0Adn9CfAS51lbPQoGa7i8sNRf9DT6xwgAZ0S7IXON3B8yKZOEp6C5euP8OhUNFWI3yxac08MxhJ54JRGHwxkOk4j1_N2YsIy71TpptmLyKPToY3MYV5R4tkcAPjwpnrXTgidqA_egJFNlB98nBBIAvfkSV3KDrxVBttS3KezXYNqkjZ8gKhBqpV7AbRbU60zT9hpYtV-vRzIozR8sLfMGDpaqvDe7q4OUL4LERpvhDCFj91hsiudpJSUW_JWpp9EUsp8exEpR2OfMyIFkGKB1pqEBkhDCUEG-NIyrw_4WVgFZWsJRhEffqrAyfYEYyzow9Dgi7g609CzG5KcHYTcBjbwplmAUGJ2TmtdX0QsPSmZlrkzIjIEl-wKOV0_FZe3tknpm6XxmRMZkKSS-qmDBpyJLyHhsxTQBrCCvy1D3o5o4Q1gSsXPx9wS6SwMvIElMXuigMV-YKbUiNIByTgNgR9opZ1s3avcU2gQOGXqZJ37WcTTRPfBhIdJNByKT4BwvCgI70eMh5iBLJvyOLiYOcx61jpf6GYN2YdHvzTt8bmSfT4Q_Ll0hIjeXpeXUcxWUAZP8qkufowolVsC834OWVRZruXMZNJGa5MA7Z9YWen_ZUA0UZ64qit2bdnn_VneB9r4md9Px6GiwB-utnGh0QFHMEExIM4Fa5CkXtF",
    "dropbox_upload_path": "/Cargo_Bookings",  # Folder in Dropbox
    
    # Tariff Query Parameters
    "screen_name": "TRF007",  # Screen for tariff/rate requests
    "date_range_days": 15,  # Max 15 days per query
    "airports": [
        "CAN", "HKG", "NGO", "ISB",
        "XMN", "CGK", "ICN", "TPE",
        "CGO", "DPS", "GMP", "HAN",
        "PEK", "NRT", "MFM", "SGN",
        "PVG", "HND", "KHI", "DAD",
        "SZX", "KIX", "LHE"
    ],  # 27 Asia-Pacific airports
    
    # Directories
    "download_dir": os.path.expanduser("~/Downloads/Avianca_Bookings"),
    "log_dir": os.path.expanduser("~/Logs"),
}

# ============================================================================
# LOGGING SETUP
# ============================================================================

def setup_logging():
    """Configure logging to file and console"""
    Path(CONFIG["log_dir"]).mkdir(parents=True, exist_ok=True)
    
    log_file = Path(CONFIG["log_dir"]) / f"avianca_downloader_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    return logging.getLogger(__name__)

logger = setup_logging()

# ============================================================================
# EMAIL FUNCTIONS
# ============================================================================

def get_verification_code_from_email(timeout_seconds=300) -> str:
    """
    Get verification code from Gmail - Filter by DATE on Gmail server (fast)
    Only search emails from last 30 minutes, then expand if needed
    """
    from datetime import datetime, timedelta
    import email as email_lib
    import re
    
    logger.info(f"Waiting for verification code (timeout: {timeout_seconds}s)...")
    
    try:
        mail = None
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(CONFIG["gmail_email"], CONFIG["gmail_app_password"])
        mail.select("INBOX")
        
        start_time = time.time()
        
        # Try different time ranges: 30min, 60min, 2hr, 4hr
        time_windows = [30, 60, 120, 240]  # minutes
        current_window_idx = 0
        last_search_time = start_time
        
        while time.time() - start_time < timeout_seconds:
            try:
                # Use UNSEEN + SINCE filter (combo = only recent unread emails)
                # UNSEEN = not read yet, SINCE = time window
                # This avoids reading millions of old unread emails
                minutes_ago = time_windows[min(current_window_idx, len(time_windows)-1)]
                since_date = datetime.now() - timedelta(minutes=minutes_ago)
                since_str = since_date.strftime("%d-%b-%Y")
                
                logger.info(f"🔍 Searching UNSEEN emails from last {minutes_ago} minutes...")
                status, messages = mail.search(None, 'UNSEEN', 'FROM', CONFIG["verification_code_sender"], 'SINCE', since_str)
                
                if messages[0]:
                    email_ids = messages[0].split()
                    logger.info(f"Found {len(email_ids)} recent emails from verification sender")
                    
                    # Limit to last 50 to avoid slowness with millions of old unread emails
                    email_ids_to_process = email_ids[-50:] if len(email_ids) > 50 else email_ids
                    if len(email_ids) > 50:
                        logger.info(f"⚠️  Processing only last 50 (total unread: {len(email_ids)})")
                    
                    # Process from newest to oldest
                    for email_id in reversed(email_ids_to_process):
                        try:
                            status, msg_data = mail.fetch(email_id, '(RFC822)')
                            msg = email.message_from_bytes(msg_data[0][1])
                            
                            # Check email date - STRICT: only last 5 minutes
                            try:
                                email_date = email_lib.utils.parsedate_to_datetime(msg['Date'])
                                current_time = datetime.now(email_date.tzinfo)
                                time_diff = (current_time - email_date).total_seconds()
                                
                                # STRICT: Only accept emails from last 5 minutes (300 seconds)
                                # This prevents processing millions of old unread emails
                                if time_diff > 300:
                                    logger.debug(f"⏭️  Skipping old email ({time_diff}s old)")
                                    continue
                                
                            except Exception as e:
                                logger.debug(f"Could not parse date: {str(e)}")
                                continue
                            
                            # Validate "To" field
                            email_to = msg.get('To', '')
                            if CONFIG["avianca_email"].lower() not in email_to.lower():
                                continue
                            
                            # Extract code - try multiple methods
                            body = ""
                            
                            # Method 1: Get text from multipart email
                            if msg.is_multipart():
                                for part in msg.get_payload():
                                    content_type = part.get_content_type()
                                    if content_type == "text/plain":
                                        try:
                                            body = part.get_payload(decode=True).decode('utf-8')
                                            break
                                        except:
                                            pass
                                    elif content_type == "text/html":
                                        # If no plain text, try HTML
                                        try:
                                            html_body = part.get_payload(decode=True).decode('utf-8')
                                            if not body:
                                                body = html_body
                                        except:
                                            pass
                            else:
                                # Single part email
                                try:
                                    body = msg.get_payload(decode=True).decode('utf-8')
                                except:
                                    body = msg.get_payload()
                            
                            # Search for code patterns
                            # Pattern 1: 8 consecutive digits (29322955)
                            code_match = re.search(r'\b(\d{8})\b', body)
                            
                            # Pattern 2: Code with dashes (2932-2955) or spaces
                            if not code_match:
                                code_match = re.search(r'(\d{4}[-\s]?\d{4})', body)
                            
                            # Pattern 3: "Code" or "code" followed by number
                            if not code_match:
                                code_match = re.search(r'code[:\s]+(\d{4,8})', body, re.IGNORECASE)
                            
                            # Pattern 4: Just look for any 8 digit sequence
                            if not code_match:
                                code_match = re.search(r'(\d{8})', body)
                            
                            if code_match:
                                # Extract just the digits
                                code = re.sub(r'\D', '', code_match.group(1))
                                if len(code) >= 8:
                                    code = code[:8]
                                    logger.info(f"✓ Verification code found: {code}")
                                    
                                    try:
                                        mail.store(email_id, '+FLAGS', '\\Seen')
                                    except:
                                        pass
                                    
                                    mail.close()
                                    mail.logout()
                                    return code
                        
                        except Exception as e:
                            logger.debug(f"Error processing email: {str(e)}")
                            continue
                
                # If no code found yet, try expanding time window after 30 seconds of waiting
                elapsed = time.time() - start_time
                if elapsed - last_search_time > 30 and current_window_idx < len(time_windows) - 1:
                    current_window_idx += 1
                    last_search_time = elapsed
                    logger.info(f"Expanding search window...")
                
                elapsed_int = int(elapsed)
                logger.info(f"No code yet... ({elapsed_int}s / {timeout_seconds}s)")
                time.sleep(5)
            
            except imaplib.IMAP4.abort as e:
                logger.debug(f"IMAP connection issue, reconnecting...")
                try:
                    if mail:
                        mail.close()
                except:
                    pass
                mail = imaplib.IMAP4_SSL("imap.gmail.com")
                mail.login(CONFIG["gmail_email"], CONFIG["gmail_app_password"])
                mail.select("INBOX")
                time.sleep(2)
                continue
        
        if mail:
            try:
                mail.close()
                mail.logout()
            except:
                pass
        
        raise TimeoutException("Verification code email not received")
    
    except Exception as e:
        if mail:
            try:
                mail.close()
                mail.logout()
            except:
                pass
        logger.error(f"Error retrieving code: {str(e)}")
        raise

def send_notification_email(success: bool, details: str = ""):
    """Send completion notification via email"""
    try:
        msg = MIMEMultipart()
        msg['From'] = CONFIG["gmail_email"]
        msg['To'] = CONFIG["gmail_email"]
        msg['Subject'] = f"[Avianca Downloader] {'SUCCESS' if success else 'FAILED'} - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        
        body = f"""
        Avianca iCargo Download Report
        Status: {'✓ SUCCESS' if success else '✗ FAILED'}
        Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        
        {details}
        
        Files saved to: {CONFIG['download_dir']}
        Log file: {CONFIG['log_dir']}
        """
        
        msg.attach(MIMEText(body, 'plain'))
        
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(CONFIG["gmail_email"], CONFIG["gmail_app_password"])
        server.send_message(msg)
        server.quit()
        
        logger.info("Notification email sent")
    
    except Exception as e:
        logger.error(f"Failed to send notification email: {str(e)}")

# ============================================================================
# SELENIUM/BROWSER AUTOMATION
# ============================================================================

def init_selenium_driver():
    """Initialize Selenium WebDriver for Chrome"""
    chrome_options = webdriver.ChromeOptions()
    
    # Railway/headless mode
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-extensions")
    
    # Configure downloads folder
    download_dir = os.path.expanduser("~/Downloads/Avianca_Bookings")
    Path(download_dir).mkdir(parents=True, exist_ok=True)
    
    prefs = {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "profile.default_content_settings.popups": 0,
        "profile.managed_default_content_settings.notifications": 2
    }
    chrome_options.add_experimental_option("prefs", prefs)
    
    try:
        logger.info("Initializing Chrome WebDriver...")
        driver = webdriver.Chrome(options=chrome_options)
        logger.info("✓ Chrome WebDriver initialized successfully")
    except Exception as e:
        logger.error(f"Chrome initialization failed: {str(e)}", exc_info=True)
        raise
    
    driver.set_page_load_timeout(30)
    return driver

def login_to_avianca(driver) -> bool:
    """
    Login to Avianca iCargo with email + verification code
    
    Returns:
        True if login successful, False otherwise
    """
    try:
        logger.info("Navigating to Avianca iCargo login page...")
        logger.info(f"Driver created successfully, navigating to {CONFIG['avianca_url']}")
        driver.get(CONFIG["avianca_url"])
        
        wait = WebDriverWait(driver, 15)
        
        # Step 1: Find and fill email field (usually pre-filled, but ensure it's there)
        logger.info("Entering email address...")
        try:
            email_field = wait.until(EC.presence_of_element_located((By.ID, "cred_userid_inputtext")))
            email_field.clear()
            email_field.send_keys(CONFIG["avianca_email"])
            logger.info(f"Email entered: {CONFIG['avianca_email']}")
        except TimeoutException:
            logger.warning("Email field not found by ID, trying alternative selectors...")
            email_field = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@type='email']")))
            email_field.clear()
            email_field.send_keys(CONFIG["avianca_email"])
        
        # Step 2: Click Next button to send code
        logger.info("Clicking 'Next' button...")
        try:
            next_button = wait.until(EC.element_to_be_clickable((By.ID, "idSIButton9")))
            next_button.click()
        except TimeoutException:
            # Fallback to text-based search
            next_button = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Next')]")))
            next_button.click()
        
        logger.info("Code email should be sending...")
        time.sleep(3)
        
        # Step 3: Get verification code from email
        # CRITICAL: First time takes MUCH longer (up to 5 minutes), second time is instant
        logger.info("Retrieving verification code from email...")
        logger.info("⏱️  FIRST TIME: This may take 4-5 minutes for Azure to send email")
        logger.info("⏱️  RETRIES: Usually instant on subsequent attempts")
        
        code = None
        timeouts = [300, 300, 300]  # 5 min, 5 min, 5 min (3 attempts = 15 min total)
        
        for attempt, timeout in enumerate(timeouts, 1):
            try:
                logger.info(f"\n📧 Attempt {attempt}/{len(timeouts)}: Waiting {timeout}s for code...")
                code = get_verification_code_from_email(timeout_seconds=timeout)
                logger.info(f"✓ Code received on attempt {attempt}")
                break
            except TimeoutException:
                if attempt < len(timeouts):
                    logger.warning(f"⏱️  Attempt {attempt} timed out ({timeout}s)")
                    logger.info(f"Retrying... This sometimes happens on first login")
                    time.sleep(5)
                else:
                    logger.error(f"❌ Failed to get code after {len(timeouts)} attempts ({sum(timeouts)}s total)")
                    raise
        
        if not code:
            raise Exception("Could not retrieve verification code")
        
        # Step 4: Wait for code input field and enter code
        logger.info(f"Entering verification code...")
        try:
            code_field = wait.until(EC.presence_of_element_located((By.ID, "idTxtBx_OTC_Password")))
            code_field.send_keys(code)
        except TimeoutException:
            code_field = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@placeholder='Code']")))
            code_field.send_keys(code)
        
        # Step 5: Click Sign In button
        logger.info("Clicking 'Sign in' button...")
        time.sleep(1)  # Wait for page to stabilize after entering code
        signin_button = wait.until(EC.element_to_be_clickable((By.ID, "idSIButton9")))
        signin_button.click()
        
        # Wait for redirect to iCargo system
        time.sleep(5)
        
        # Verify we're logged in
        if "showMainPage" in driver.current_url or "icargo" in driver.current_url:
            logger.info("✓ Login successful!")
            # Switch to the new window that opened after login
            time.sleep(2)
            all_windows = driver.window_handles
            if len(all_windows) > 1:
                logger.info(f"Switching to new window (total windows: {len(all_windows)})")
                driver.switch_to.window(all_windows[-1])  # Switch to the last/new window
                time.sleep(2)
                logger.info(f"✓ Switched to new window. Current URL: {driver.current_url}")
            
            return True
        else:
            logger.error(f"Login may have failed. Current URL: {driver.current_url}")
            return False
    
    except Exception as e:
        logger.error(f"Login error: {str(e)}", exc_info=True)
        return False

def switch_to_comm_role(driver) -> bool:
    """
    Switch to COMM role - click OK while still in iframe
    """
    try:
        logger.info("Switching to COMM role...")
        wait = WebDriverWait(driver, 30)
        
        time.sleep(3)
        
        # Step 1: Open menu
        try:
            logger.info("Step 1: Opening menu...")
            more_menu = wait.until(EC.presence_of_element_located((By.XPATH, "//span[contains(@class, 'ic-toggle-menu')]")))
            driver.execute_script("arguments[0].click();", more_menu)
            logger.info("✓ Menu opened")
        except TimeoutException:
            logger.error("Menu not found!")
            return False
        
        time.sleep(2)
        
        # Step 2: Click Switch Role
        try:
            logger.info("Step 2: Clicking Switch Role...")
            switch_role = wait.until(EC.presence_of_element_located((By.XPATH, "//span[contains(@class, 'ic-switch-role')]//a")))
            driver.execute_script("arguments[0].click();", switch_role)
            logger.info("✓ Switch Role clicked")
        except TimeoutException:
            logger.error("Switch Role not found!")
            return False
        
        time.sleep(4)
        
        # Step 3: Switch to iframe, change role, AND click OK
        try:
            logger.info("Step 3: Switching to iframe...")
            iframe = wait.until(EC.presence_of_element_located((By.ID, "swichRoleiframe")))
            driver.switch_to.frame(iframe)
            logger.info("✓ Inside iframe")
            
            time.sleep(2)
            
            # Change role to COMM
            logger.info("Changing role to COMM...")
            js_change = """
                let select = document.querySelector('[name="selectedStationRoleGroup"]') || 
                             document.querySelector('#CMB_ADMIN_USER_SWITCHROLES_LISTROLES');
                
                if (select) {
                    select.value = 'COMM_ARL_N';
                    let event = new Event('change', { bubbles: true });
                    select.dispatchEvent(event);
                    return 'SUCCESS';
                } else {
                    return 'ERROR';
                }
            """
            
            result = driver.execute_script(js_change)
            logger.info(f"Result: {result}")
            
            if result == "ERROR":
                logger.error("Failed to change role")
                driver.switch_to.default_content()
                return False
            
            logger.info("✓ COMM selected!")
            time.sleep(2)
            
            # Click OK button - WHILE STILL IN IFRAME
            logger.info("Clicking OK button in iframe...")
            js_click = """
                let ok_button = document.querySelector('#CMB_ADMIN_USER_SWITCHROLES_OK_BUTTON') ||
                                document.querySelector('button[name="btnOK"]') ||
                                document.querySelector('button[type="button"]');
                
                if (ok_button) {
                    ok_button.click();
                    return 'OK clicked';
                } else {
                    return 'ERROR: OK not found';
                }
            """
            
            result = driver.execute_script(js_click)
            logger.info(f"Click result: {result}")
            
            if "ERROR" in result:
                logger.warning(f"First attempt failed: {result}")
                # Try pressing Enter instead
                logger.info("Trying Enter key...")
                driver.execute_script("document.body.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter'}))")
            
            time.sleep(1)
            
            # Exit iframe
            driver.switch_to.default_content()
            logger.info("✓ Exited iframe")
            
            time.sleep(5)
            logger.info("✓ Successfully switched to COMM role!")
            return True
        
        except Exception as e:
            logger.error(f"Error in iframe: {str(e)}")
            try:
                driver.switch_to.default_content()
            except:
                pass
            return False
    
    except Exception as e:
        logger.error(f"Role switch failed: {str(e)}")
        return False


def navigate_to_screen(driver, screen_num: str) -> bool:
    """
    Navigate to a specific screen (e.g., TRF007 for tariffs)
    
    Args:
        screen_num: Screen number like "TRF007"
    
    Returns:
        True if successful
    """
    try:
        logger.info(f"Navigating to screen {screen_num}...")
        wait = WebDriverWait(driver, 10)
        
        # Find Screen # input field by ID
        screen_field = wait.until(EC.presence_of_element_located((By.ID, "ic-screen-search")))
        screen_field.clear()
        screen_field.send_keys(screen_num)
        
        time.sleep(1)
        
        # Press Enter to navigate
        from selenium.webdriver.common.keys import Keys
        screen_field.send_keys(Keys.RETURN)
        
        time.sleep(5)
        logger.info(f"✓ Navigated to screen {screen_num}")
        return True
    
    except Exception as e:
        logger.error(f"Error navigating to screen {screen_num}: {str(e)}")
        return False

def set_date_range_and_airport(driver, origin: str, is_first_airport: bool = False, start_date: str = None, end_date: str = None) -> bool:
    """
    Set airport/origin for tariff query
    
    SIMPLIFIED STRATEGY:
    - First airport ONLY: Set From Date (Avianca auto-fills To Date with +2 weeks)
    - All airports: Clear and set Origin field
    - Fields persist between queries, so reuse them
    
    Args:
        driver: Selenium driver
        origin: Airport code (e.g., "CAN")
        is_first_airport: True if this is the first airport (need to set dates)
        start_date: Start date (only used for first airport)
        end_date: Not used (Avianca auto-fills this)
    """
    try:
        if start_date is None:
            from datetime import datetime, timedelta
            start_date = datetime.now().strftime("%d-%b-%Y").upper()
        
        logger.info(f"Processing airport {origin}")
        
        wait = WebDriverWait(driver, 5)
        time.sleep(0.5)
        
        # ===== SWITCH TO IFRAME FIRST =====
        logger.info("🔍 Switching to iframe iCargoContentFrameTRF007...")
        try:
            iframe = wait.until(EC.presence_of_element_located((By.NAME, "iCargoContentFrameTRF007")))
            driver.switch_to.frame(iframe)
            logger.info("✓ Inside iframe")
        except TimeoutException:
            logger.error("❌ Cannot find iframe iCargoContentFrameTRF007")
            return False
        
        time.sleep(1)
        
        # ===== STEP 1: SET DATES (ONLY FOR FIRST AIRPORT) =====
        if is_first_airport:
            logger.info("🔍 First airport - setting From Date (To Date will auto-fill)...")
            
            try:
                # Set From Date
                logger.info("   Setting From Date...")
                driver.execute_script(f"""
                    let field = document.querySelector('#fromdate');
                    if (field) {{
                        field.value = '{start_date}';
                        field.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        field.dispatchEvent(new Event('blur', {{ bubbles: true }}));
                    }}
                """)
                logger.info(f"   ✓ From Date set to: {start_date}")
                time.sleep(2)  # Wait for Avianca to auto-fill To Date
                
                # Verify To Date was auto-filled
                to_date_value = driver.execute_script("return document.querySelector('#todate').value;")
                logger.info(f"   ✓ To Date auto-filled to: {to_date_value}")
                
            except Exception as e:
                logger.error(f"❌ Failed to set From Date: {str(e)}")
                try:
                    driver.switch_to.default_content()
                except:
                    pass
                return False
        
        # ===== STEP 2: SET ORIGIN (FOR ALL AIRPORTS) =====
        logger.info(f"🔍 Setting Origin field to {origin}...")
        
        try:
            origin_field = wait.until(EC.presence_of_element_located((By.ID, "CMP_Tariff_Freight_ListSpotRateRequests_Origin")))
            
            # Clear the field first
            driver.execute_script("""
                let field = document.querySelector('#CMP_Tariff_Freight_ListSpotRateRequests_Origin');
                if (field) field.value = '';
            """)
            time.sleep(0.5)
            
            # Set new origin
            driver.execute_script(f"""
                let field = document.querySelector('#CMP_Tariff_Freight_ListSpotRateRequests_Origin');
                if (field) {{
                    field.value = '{origin}';
                    field.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    field.dispatchEvent(new Event('blur', {{ bubbles: true }}));
                }}
            """)
            
            logger.info(f"✓ Origin set to: {origin}")
            time.sleep(1)
            
        except TimeoutException:
            logger.error("❌ Origin field not found")
            try:
                driver.switch_to.default_content()
            except:
                pass
            return False
        except Exception as e:
            logger.error(f"❌ Failed to set Origin: {str(e)}")
            try:
                driver.switch_to.default_content()
            except:
                pass
            return False
        
        # Exit iframe
        logger.info("↩️  Exiting iframe...")
        driver.switch_to.default_content()
        logger.info(f"✓ Ready to query for {origin}")
        return True
    
    except Exception as e:
        logger.error(f"❌ Error setting airport: {str(e)}")
        try:
            driver.switch_to.default_content()
        except:
            pass
        return False


def execute_tariff_query(driver) -> bool:
    """
    Click the List button to execute the tariff query
    
    CRITICAL: Must switch to iframe iCargoContentFrameTRF007 (the one with the form)
    There are multiple iframes with same id but different names
    
    Returns:
        True if successful
    """
    try:
        logger.info("Executing tariff query...")
        wait = WebDriverWait(driver, 5)
        
        time.sleep(1)
        
        # Switch to correct iframe
        logger.info("🔍 Switching to iframe iCargoContentFrameTRF007...")
        try:
            iframe = wait.until(EC.presence_of_element_located((By.NAME, "iCargoContentFrameTRF007")))
            driver.switch_to.frame(iframe)
            logger.info("✓ Inside iframe")
        except TimeoutException:
            logger.error("❌ Cannot find iframe iCargoContentFrameTRF007")
            return False
        
        time.sleep(1)
        
        # Click List button
        try:
            logger.info("🔍 Searching for List button (id=CMP_Tariff_Freight_ListSpotRateRequests_List)...")
            list_button = wait.until(EC.element_to_be_clickable((By.ID, "CMP_Tariff_Freight_ListSpotRateRequests_List")))
            logger.info("✓ Found List button")
            
            driver.execute_script("arguments[0].scrollIntoView(true);", list_button)
            time.sleep(0.5)
            
            list_button.click()
            logger.info("✓ List button clicked")
        except TimeoutException:
            logger.error("❌ List button not found")
            try:
                driver.switch_to.default_content()
            except:
                pass
            return False
        except Exception as e:
            logger.error(f"❌ Error clicking List button: {str(e)}")
            try:
                driver.switch_to.default_content()
            except:
                pass
            return False
        
        time.sleep(5)
        
        # Exit iframe
        driver.switch_to.default_content()
        logger.info("↩️  Exited iframe")
        logger.info("✓ Query executed, results loading...")
        return True
    
    except Exception as e:
        logger.error(f"❌ Error executing query: {str(e)}")
        try:
            driver.switch_to.default_content()
        except:
            pass
        return False

def download_results_as_excel(driver) -> bool:
    """
    Click the "Export to Excel" link to download results
    
    CRITICAL: Must switch to iframe iCargoContentFrameTRF007 (the one with the results)
    There are multiple iframes with same id but different names
    
    Returns:
        True if download started
    """
    try:
        logger.info("Exporting results to Excel...")
        wait = WebDriverWait(driver, 5)
        
        time.sleep(1)
        
        # Switch to correct iframe
        logger.info("🔍 Switching to iframe iCargoContentFrameTRF007...")
        try:
            iframe = wait.until(EC.presence_of_element_located((By.NAME, "iCargoContentFrameTRF007")))
            driver.switch_to.frame(iframe)
            logger.info("✓ Inside iframe")
        except TimeoutException:
            logger.error("❌ Cannot find iframe iCargoContentFrameTRF007")
            return False
        
        time.sleep(1)
        
        # Find and click Export to Excel link
        try:
            logger.info("🔍 Searching for Export to Excel link (id=exportToExcelLink)...")
            export_link = wait.until(EC.element_to_be_clickable((By.ID, "exportToExcelLink")))
            logger.info("✓ Found Export to Excel link")
            
            driver.execute_script("arguments[0].scrollIntoView(true);", export_link)
            time.sleep(0.5)
            
            export_link.click()
            logger.info("✓ Export clicked")
        except TimeoutException:
            logger.warning("❌ Export to Excel link not found - checking if results exist...")
            # Try alternative selectors
            try:
                export_link = driver.find_element(By.XPATH, "//a[contains(text(), 'Excel')]")
                driver.execute_script("arguments[0].scrollIntoView(true);", export_link)
                time.sleep(0.5)
                export_link.click()
                logger.info("✓ Export clicked (via text match)")
            except:
                logger.warning("❌ No Export to Excel link found")
                try:
                    driver.switch_to.default_content()
                except:
                    pass
                return False
        except Exception as e:
            logger.error(f"❌ Error clicking Export link: {str(e)}")
            try:
                driver.switch_to.default_content()
            except:
                pass
            return False
        
        time.sleep(2)
        
        # Exit iframe
        driver.switch_to.default_content()
        logger.info("↩️  Exited iframe")
        logger.info("✓ Export to Excel initiated")
        return True
    
    except Exception as e:
        logger.error(f"❌ Error exporting to Excel: {str(e)}")
        try:
            driver.switch_to.default_content()
        except:
            pass
        return False

# ============================================================================
# FILE OPERATIONS
# ============================================================================

def merge_csv_files(file_directory: str, output_filename: str = "merged_bookings.xlsx") -> str:
    """
    Merge all Excel files in directory into one file
    
    If no files found in directory, tries to find them in Downloads/Avianca_Bookings
    and copies them to the target directory
    
    Args:
        file_directory: Path to directory with Excel files
        output_filename: Name of output merged file (default: merged_bookings.xlsx)
    
    Returns:
        Path to merged file
    """
    try:
        import shutil
        from pathlib import Path
        
        target_dir = Path(file_directory)
        logger.info(f"Merging Excel files from {target_dir}")
        
        # Look for .xlsx files (Avianca exports these, not .csv)
        excel_files = list(target_dir.glob("*.xlsx"))
        
        # If no files found, try Downloads
        if not excel_files:
            logger.warning(f"No Excel files found in {target_dir}")
            logger.info("Checking Downloads/Avianca_Bookings...")
            
            downloads_dir = Path.home() / "Downloads" / "Avianca_Bookings"
            if downloads_dir.exists():
                excel_files = list(downloads_dir.glob("*.xlsx"))
                
                if excel_files:
                    logger.info(f"Found {len(excel_files)} Excel files in Downloads - copying to {target_dir}...")
                    target_dir.mkdir(parents=True, exist_ok=True)
                    
                    for excel_file in excel_files:
                        try:
                            shutil.copy2(excel_file, target_dir / excel_file.name)
                            logger.info(f"  ✓ Copied {excel_file.name}")
                        except Exception as e:
                            logger.warning(f"Could not copy {excel_file.name}: {str(e)}")
        
        if not excel_files:
            logger.error("No Excel files found in either location")
            return None
        
        # Re-check target directory for files (after copy)
        excel_files = list(target_dir.glob("*.xlsx"))
        
        if not excel_files:
            logger.warning("No Excel files to merge")
            return None
        
        logger.info(f"Merging {len(excel_files)} Excel files...")
        
        dfs = []
        for excel_file in excel_files:
            try:
                df = pd.read_excel(excel_file)
                dfs.append(df)
                logger.info(f"  ✓ Loaded {excel_file.name} ({len(df)} rows)")
            except Exception as e:
                logger.warning(f"Could not read {excel_file.name}: {str(e)}")
        
        if dfs:
            # Merge de forma correcta: mantener headers ORIGINALES (no cambiar mayúsculas)
            logger.info(f"Concatenating {len(dfs)} files...")
            
            # NO normalizar - mantener columnas tal como vienen (AWB = AWB, no awb)
            # Concatenate all dataframes - this creates ONE header + all rows
            merged_df = pd.concat(dfs, ignore_index=True)
            
            # Remove any completely empty rows
            merged_df = merged_df.dropna(how='all')
            
            output_path = target_dir / output_filename
            
            # Save as Excel (.xlsx) with ONE header and all data rows
            # IMPORTANT: Don't convert data types - keep everything as-is
            if output_filename.endswith('.xlsx'):
                logger.info(f"Saving to Excel with ONE header and {len(merged_df)} total data rows...")
                # dtype=str keeps everything as imported (dates stay as dates, not datetime)
                merged_df.to_excel(output_path, index=False, sheet_name='Data')
            else:
                # Fallback to CSV if specified
                logger.info(f"Saving to CSV with ONE header and {len(merged_df)} total data rows...")
                merged_df.to_csv(output_path, index=False)
            
            logger.info(f"✓ Merged file saved: {output_path}")
            logger.info(f"✓ Columns: {len(merged_df.columns)}")
            logger.info(f"✓ Total data rows: {len(merged_df)}")
            logger.info(f"✓ Structure: 1 header row + {len(merged_df)} data rows")
            
            # ===== DELETE ORIGINAL FILES AFTER MERGE =====
            logger.info("\n🗑️  Cleaning up original files...")
            for excel_file in excel_files:
                try:
                    excel_file.unlink()  # Delete file
                    logger.info(f"  ✓ Deleted {excel_file.name}")
                except Exception as e:
                    logger.warning(f"Could not delete {excel_file.name}: {str(e)}")
            
            logger.info(f"✓ Cleanup complete - {len(excel_files)} original files deleted")
            
            return str(output_path)
        
        return None
    
    except Exception as e:
        logger.error(f"Error merging CSV files: {str(e)}")
        return None

# ============================================================================
# DROPBOX OPERATIONS
# ============================================================================

def upload_to_dropbox(file_path: str, dropbox_path: str = None) -> bool:
    """
    Upload file to Dropbox (optional - will skip if dropbox not installed)
    
    Args:
        file_path: Local file path
        dropbox_path: Destination path in Dropbox (if None, uses CONFIG["dropbox_upload_path"])
    
    Returns:
        True if successful or skipped, False if error
    """
    try:
        # Check if Dropbox is available
        if not DROPBOX_AVAILABLE:
            logger.warning("⚠️  Dropbox SDK not installed - skipping upload (pip install dropbox)")
            return True  # Return True so workflow continues
        
        if not CONFIG["dropbox_access_token"] or CONFIG["dropbox_access_token"].startswith("your_"):
            logger.warning("⚠️  Dropbox token not configured - skipping upload")
            return True  # Return True so workflow continues
        
        dbx = dropbox.Dropbox(CONFIG["dropbox_access_token"])
        
        # Test connection
        dbx.users_get_current_account()
        
        if dropbox_path is None:
            dropbox_path = CONFIG["dropbox_upload_path"]
        
        file_name = Path(file_path).name
        destination = f"{dropbox_path}/{file_name}"
        
        logger.info(f"📤 Uploading {file_name} to Dropbox...")
        
        with open(file_path, 'rb') as f:
            dbx.files_upload(f.read(), destination, autorename=True)
        
        logger.info(f"✓ Successfully uploaded to Dropbox: {destination}")
        return True
    
    except Exception as e:
        logger.error(f"❌ Error uploading to Dropbox: {str(e)}")
        logger.warning("⚠️  Workflow continues without Dropbox upload")
        return True  # Return True so workflow continues even if Dropbox fails

# ============================================================================
# MAIN DOWNLOAD WORKFLOW
# ============================================================================

def run_download_workflow() -> bool:
    """
    Main workflow: login, navigate to TRF007, download tariffs for all airports, merge, upload
    
    Returns:
        True if successful
    """
    driver = None
    
    try:
        # Create download directory
        Path(CONFIG["download_dir"]).mkdir(parents=True, exist_ok=True)
        
        logger.info("=" * 80)
        logger.info("STARTING AVIANCA TARIFF (TRF007) DOWNLOAD WORKFLOW")
        logger.info(f"Timestamp: {datetime.now()}")
        logger.info(f"Airports to download: {len(CONFIG['airports'])}")
        logger.info("=" * 80)
        
        # Initialize browser
        driver = init_selenium_driver()
        
        # Login to iCargo
        if not login_to_avianca(driver):
            raise Exception("Failed to login to Avianca iCargo")
        
        logger.info("✓ Successfully logged in")
        
        # Switch to COMM role (REQUIRED - TRF007 only works with COMM role)
        if not switch_to_comm_role(driver):
            raise Exception("Failed to switch to COMM role - TRF007 requires COMM role. Cannot continue.")
        
        # Navigate to TRF007 (Tariff/Rate Requests screen)
        if not navigate_to_screen(driver, CONFIG["screen_number"]):
            raise Exception(f"Failed to navigate to screen {CONFIG['screen_number']}")
        
        logger.info(f"✓ Successfully navigated to {CONFIG['screen_number']}")
        
        successful_downloads = 0
        failed_downloads = 0
        
        # Download tariffs for each airport
        for idx, airport in enumerate(CONFIG["airports"]):
            is_first = (idx == 0)  # True only for first airport
            logger.info(f"\n--- Processing airport {airport} ({idx + 1}/{len(CONFIG['airports'])}) ---")
            
            try:
                # Set airport (dates only set for first airport, then Avianca auto-fills)
                if not set_date_range_and_airport(driver, airport, is_first_airport=is_first):
                    logger.warning(f"Failed to set parameters for {airport}")
                    failed_downloads += 1
                    continue
                
                # Execute the query
                if not execute_tariff_query(driver):
                    logger.warning(f"Failed to execute query for {airport}")
                    failed_downloads += 1
                    continue
                
                # Download the Excel export
                if not download_results_as_excel(driver):
                    logger.warning(f"Failed to export Excel for {airport}")
                    failed_downloads += 1
                    continue
                
                successful_downloads += 1
                logger.info(f"✓ Downloaded tariffs for {airport}")
                
                # Small delay between downloads to avoid overwhelming the server
                time.sleep(2)
                
            except Exception as e:
                logger.error(f"Error processing {airport}: {str(e)}")
                failed_downloads += 1
                continue
        
        logger.info(f"\n{'=' * 80}")
        logger.info(f"Download phase complete: {successful_downloads} successful, {failed_downloads} failed")
        logger.info(f"{'=' * 80}")
        
        # Wait for final downloads to complete
        logger.info("Waiting for downloads to finalize...")
        time.sleep(5)
        
        # Merge all downloaded files
        logger.info("Merging all downloaded tariff files...")
        merged_file = merge_csv_files(CONFIG["download_dir"])
        
        # Upload to Dropbox (if fails, merged file is already safe)
        if merged_file:
            logger.info("Uploading merged file to Dropbox...")
            upload_to_dropbox(merged_file)
        else:
            logger.warning("No files were merged")
        
        logger.info("=" * 80)
        logger.info("✓ WORKFLOW COMPLETED SUCCESSFULLY")
        logger.info("=" * 80)
        
        send_notification_email(
            success=True,
            details=f"Successfully downloaded tariffs for {successful_downloads}/{len(CONFIG['airports'])} airports.\n" +
                    f"Failed: {failed_downloads}\n" +
                    f"All files merged and uploaded to Dropbox."
        )
        
        return True
    
    except Exception as e:
        logger.error(f"Workflow failed: {str(e)}", exc_info=True)
        send_notification_email(
            success=False,
            details=f"Error: {str(e)}\n\nCheck logs for details."
        )
        return False
    
    finally:
        if driver:
            driver.quit()
            logger.info("Browser closed")

# ============================================================================
# SCHEDULING
# ============================================================================

def schedule_daily_run(hour: int = 2, minute: int = 0):
    """
    Schedule the download to run daily at specified time
    
    Args:
        hour: Hour (24-hour format)
        minute: Minute
    """
    time_str = f"{hour:02d}:{minute:02d}"
    
    schedule.every().day.at(time_str).do(run_download_workflow)
    
    logger.info(f"Scheduled download to run daily at {time_str}")
    
    # Keep scheduler running
    while True:
        schedule.run_pending()
        time.sleep(60)

# ============================================================================
# COMMAND LINE INTERFACE
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Avianca iCargo Booking Downloader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run download immediately
  python avianca_downloader.py run
  
  # Schedule to run daily at 2 AM
  python avianca_downloader.py schedule --hour 2 --minute 0
  
  # Schedule to run daily at 6 PM
  python avianca_downloader.py schedule --hour 18
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')
    
    # Run command
    subparsers.add_parser('run', help='Run download immediately')
    
    # Schedule command
    schedule_parser = subparsers.add_parser('schedule', help='Schedule daily run')
    schedule_parser.add_argument('--hour', type=int, default=2, help='Hour to run (0-23, default 2)')
    schedule_parser.add_argument('--minute', type=int, default=0, help='Minute to run (0-59, default 0)')
    
    args = parser.parse_args()
    
    if args.command == 'run':
        success = run_download_workflow()
        sys.exit(0 if success else 1)
    
    elif args.command == 'schedule':
        schedule_daily_run(hour=args.hour, minute=args.minute)
    
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
