#!/usr/bin/env python3
"""
Avianca iCargo downloader.

This module is designed to be called by the Flask web app, but it still has a
small CLI for local/manual runs.
"""

from __future__ import annotations

import argparse
import email
import email.header
import imaplib
import json
import logging
import os
import re
import shutil
import smtplib
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    import dropbox

    DROPBOX_AVAILABLE = True
except ImportError:
    DROPBOX_AVAILABLE = False


DEFAULT_AIRPORTS = [
    "CAN",
    "HKG",
    "NGO",
    "ISB",
    "XMN",
    "CGK",
    "ICN",
    "TPE",
    "CGO",
    "DPS",
    "GMP",
    "HAN",
    "PEK",
    "NRT",
    "MFM",
    "SGN",
    "PVG",
    "HND",
    "KHI",
    "DAD",
    "SZX",
    "KIX",
    "LHE",
]

MAX_RANGE_DAYS = 15
LOGIN_URL = "https://avianca-icargo.ibsplc.aero/icargo/login.do"
VERIFICATION_CODE_SENDER = "account-security-noreply@accountprotection.microsoft.com"
DOWNLOADER_BUILD_VERSION = "job-api-v18-cap142-clear-flight"
EXPORT_FILE_SUFFIXES = (".xlsx", ".xls")
EXPORT_SETTLE_SECONDS = 5
CAP142_MODES = {"specific_flight", "booking_period"}
VERIFICATION_SUBJECT_FRAGMENT = "account verification code"

ProgressCallback = Callable[[str, int | None], None]
CancelCallback = Callable[[], bool]


class WorkflowCancelled(Exception):
    """Raised when a web user cancels a running download job."""

logger = logging.getLogger("avianca_downloader")
if not logger.handlers:
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(stream_handler)
logger.setLevel(logging.INFO)
logger.propagate = False


@dataclass
class DownloaderConfig:
    avianca_email: str | None
    gmail_email: str | None
    gmail_app_password: str | None
    verification_code_sender: str
    dropbox_access_token: str | None
    dropbox_upload_path: str
    download_dir: Path
    log_dir: Path
    headless: bool

    @classmethod
    def from_env(cls, download_dir: str | Path | None = None) -> "DownloaderConfig":
        avianca_email = os.getenv("AVIANCA_EMAIL")
        gmail_email = os.getenv("GMAIL_EMAIL") or avianca_email
        gmail_app_password = os.getenv("GMAIL_APP_PASSWORD") or os.getenv("GMAIL_PASSWORD")
        default_download_dir = Path(tempfile.gettempdir()) / "avianca_downloads" / "manual"
        default_log_dir = Path(tempfile.gettempdir()) / "avianca_logs"

        return cls(
            avianca_email=avianca_email,
            gmail_email=gmail_email,
            gmail_app_password=gmail_app_password,
            verification_code_sender=os.getenv("VERIFICATION_CODE_SENDER", VERIFICATION_CODE_SENDER),
            dropbox_access_token=os.getenv("DROPBOX_TOKEN") or os.getenv("DROPBOX_ACCESS_TOKEN"),
            dropbox_upload_path=os.getenv("DROPBOX_UPLOAD_PATH", "/Cargo_Bookings"),
            download_dir=Path(download_dir or os.getenv("DOWNLOAD_DIR", str(default_download_dir))).expanduser(),
            log_dir=Path(os.getenv("LOG_DIR", str(default_log_dir))).expanduser(),
            headless=os.getenv("HEADLESS", "true").lower() not in {"0", "false", "no"},
        )

    def validate(self) -> None:
        missing = []
        if not self.avianca_email:
            missing.append("AVIANCA_EMAIL")
        if not self.gmail_email:
            missing.append("GMAIL_EMAIL or AVIANCA_EMAIL")
        if not self.gmail_app_password:
            missing.append("GMAIL_APP_PASSWORD")
        if missing:
            raise RuntimeError("Missing required environment variables: " + ", ".join(missing))


def emit(callback: ProgressCallback | None, message: str, progress: int | None = None) -> None:
    logger.info(message)
    if callback:
        callback(message, progress)


def check_cancelled(cancel_callback: CancelCallback | None) -> None:
    if cancel_callback and cancel_callback():
        raise WorkflowCancelled("Download cancelled by user")


def add_file_logger(log_dir: Path) -> logging.Handler:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"avianca_downloader_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    handler = logging.FileHandler(log_file)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)
    return handler


def normalize_airports(airports: Iterable[str] | None) -> list[str]:
    if not airports:
        return list(DEFAULT_AIRPORTS)

    normalized = []
    for airport in airports:
        code = airport.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{3}", code):
            raise ValueError(f"Invalid airport code: {airport}")
        normalized.append(code)

    if not normalized:
        raise ValueError("At least one airport is required")
    return normalized


def parse_iso_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(value, "%Y-%m-%d").date()


def format_icargo_date(value: str | date) -> str:
    if isinstance(value, date):
        parsed = value
    else:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            parsed = datetime.strptime(value.upper(), "%d-%b-%Y").date()
    return parsed.strftime("%d-%b-%Y").upper()


def validate_date_range(start_date: str | date | None, end_date: str | date | None) -> tuple[str, str]:
    start = parse_iso_date(start_date or date.today())
    end = parse_iso_date(end_date or (start + timedelta(days=MAX_RANGE_DAYS)))

    days = (end - start).days
    if days < 0:
        raise ValueError("End date must be on or after start date")
    if days > MAX_RANGE_DAYS:
        raise ValueError(f"Date range cannot exceed {MAX_RANGE_DAYS} days")

    return format_icargo_date(start), format_icargo_date(end)


def find_chrome_binary() -> str | None:
    configured = os.getenv("CHROME_BIN") or os.getenv("GOOGLE_CHROME_BIN")
    candidates = [
        configured,
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("google-chrome"),
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def init_selenium_driver(config: DownloaderConfig) -> webdriver.Chrome:
    config.download_dir.mkdir(parents=True, exist_ok=True)

    chrome_options = webdriver.ChromeOptions()
    if config.headless:
        chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-notifications")

    chrome_binary = find_chrome_binary()
    if chrome_binary:
        chrome_options.binary_location = chrome_binary
        logger.info("Using Chrome binary: %s", chrome_binary)
    else:
        logger.warning("No Chrome binary found on PATH; Selenium will try its defaults")

    prefs = {
        "download.default_directory": str(config.download_dir),
        "download.prompt_for_download": False,
        "profile.default_content_settings.popups": 0,
        "profile.managed_default_content_settings.notifications": 2,
        "safebrowsing.enabled": True,
    }
    chrome_options.add_experimental_option("prefs", prefs)

    driver_path = os.getenv("CHROMEDRIVER_PATH") or shutil.which("chromedriver")
    if driver_path:
        logger.info("Using ChromeDriver: %s", driver_path)
    elif any(name.startswith("RAILWAY_") for name in os.environ):
        raise RuntimeError(
            "chromedriver was not found on Railway's PATH. "
            "Redeploy with the updated nixpacks.toml so Railway installs system chromedriver."
        )

    service = Service(driver_path) if driver_path else None

    logger.info("Initializing Chrome WebDriver")
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.set_page_load_timeout(45)

    try:
        driver.execute_cdp_cmd(
            "Page.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(config.download_dir)},
        )
    except Exception as exc:
        logger.warning("Could not set Chrome download behavior via CDP: %s", exc)

    return driver


def get_verification_code_from_email(
    config: DownloaderConfig,
    timeout_seconds: int = 300,
    cancel_callback: CancelCallback | None = None,
    requested_after: datetime | None = None,
    progress_callback: ProgressCallback | None = None,
) -> str:
    logger.info("Waiting for verification code email")
    emit(progress_callback, "Connecting to Gmail for verification code", 12)
    mail = None

    def close_mail() -> None:
        nonlocal mail
        if not mail:
            return
        try:
            mail.close()
        except Exception:
            pass
        try:
            mail.logout()
        except Exception:
            pass
        mail = None

    def connect_mail(show_progress: bool = True) -> None:
        nonlocal mail
        close_mail()
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(config.gmail_email, config.gmail_app_password)
        mail.select("INBOX", readonly=True)
        if show_progress:
            emit(progress_callback, "Gmail connected; scanning Microsoft emails", 12)

    try:
        connect_mail()

        start_time = time.time()
        if requested_after is None:
            requested_after = datetime.now(timezone.utc) - timedelta(seconds=30)
        elif requested_after.tzinfo is None:
            requested_after = requested_after.replace(tzinfo=timezone.utc)

        time_windows = [30, 60, 120, 240]
        current_window_idx = 0
        last_search_window_change = 0.0
        last_reconnect_at = 0.0

        while time.time() - start_time < timeout_seconds:
            check_cancelled(cancel_callback)
            elapsed = time.time() - start_time

            if elapsed - last_reconnect_at >= 35:
                emit(progress_callback, "Refreshing Gmail connection", 12)
                connect_mail(show_progress=False)
                last_reconnect_at = elapsed
            else:
                try:
                    mail.noop()
                    mail.select("INBOX", readonly=True)
                except imaplib.IMAP4.abort:
                    emit(progress_callback, "Gmail connection refreshed after mailbox delay", 12)
                    connect_mail(show_progress=False)
                    last_reconnect_at = elapsed

            minutes_ago = time_windows[min(current_window_idx, len(time_windows) - 1)]
            since_date = datetime.now() - timedelta(minutes=minutes_ago)
            since_str = since_date.strftime("%d-%b-%Y")

            status, messages = mail.search(
                None,
                "FROM",
                config.verification_code_sender,
                "SINCE",
                since_str,
            )

            if status == "OK" and messages and messages[0]:
                email_ids = messages[0].split()
                logger.info(
                    "Found %s candidate verification email(s), checking newest first",
                    len(email_ids),
                )
                emit(progress_callback, f"Checking {len(email_ids)} Microsoft email candidate(s)", 12)

                code_candidates = []
                for email_id in email_ids[-100:]:
                    status, msg_data = mail.fetch(email_id, "(RFC822)")
                    if status != "OK" or not msg_data:
                        continue

                    msg = email.message_from_bytes(msg_data[0][1])
                    email_date = parse_email_date(msg)
                    if not email_date:
                        continue

                    body = extract_email_body(msg)
                    code = extract_verification_code(body)
                    if not code:
                        continue

                    subject = email_subject(msg)
                    code_candidates.append((email_date, code, subject))

                code_candidates.sort(key=lambda item: item[0], reverse=True)
                newest_seen = code_candidates[0][0] if code_candidates else None
                if code_candidates:
                    emit(
                        progress_callback,
                        f"Found {len(code_candidates)} Microsoft email(s) containing a code",
                        12,
                    )

                for email_date, code, subject in code_candidates:
                    if email_date < requested_after - timedelta(minutes=10):
                        logger.debug("Stopping at old candidate email: %s", email_date.isoformat())
                        break

                    close_mail()
                    logger.info(
                        "Verification code found from email dated %s with subject %s",
                        email_date.isoformat(),
                        subject,
                    )
                    emit(progress_callback, f"Verification code found from {email_date.strftime('%H:%M:%S')}", 14)
                    return code

                if newest_seen:
                    emit(
                        progress_callback,
                        f"Newest code email is from {newest_seen.strftime('%H:%M:%S')}; waiting for newer code",
                        12,
                    )
                else:
                    emit(progress_callback, "No code found inside Microsoft email candidates yet", 12)

            elapsed = time.time() - start_time
            if elapsed - last_search_window_change > 30 and current_window_idx < len(time_windows) - 1:
                current_window_idx += 1
                last_search_window_change = elapsed

            logger.info("No verification code yet (%ss / %ss)", int(elapsed), timeout_seconds)
            emit(progress_callback, f"Still waiting for verification code ({int(elapsed)}s)", 12)
            time.sleep(5)

        raise TimeoutException("Verification code email not received")

    finally:
        close_mail()


def parse_email_date(msg: email.message.Message) -> datetime | None:
    try:
        email_date = email.utils.parsedate_to_datetime(msg["Date"])
        if email_date.tzinfo is None:
            email_date = email_date.replace(tzinfo=timezone.utc)
        return email_date
    except Exception:
        return None


def email_subject(msg: email.message.Message) -> str:
    raw_subject = msg.get("Subject", "")
    decoded_parts = email.header.decode_header(raw_subject)
    parts = []
    for value, charset in decoded_parts:
        if isinstance(value, bytes):
            parts.append(value.decode(charset or "utf-8", errors="ignore"))
        else:
            parts.append(value)
    return "".join(parts)


def extract_email_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type not in {"text/plain", "text/html"}:
                continue
            try:
                parts.append(part.get_payload(decode=True).decode("utf-8", errors="ignore"))
            except Exception:
                continue
        return "\n".join(parts)

    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="ignore")
    return str(msg.get_payload())


def extract_verification_code(body: str) -> str | None:
    patterns = [
        r"Account verification code:\s*(\d{6,8})",
        r"\b(\d{8})\b",
        r"\b(\d{6})\b",
        r"(\d{4}[-\s]?\d{4})",
        r"code(?: is)?[:\s]+(\d{6,8})",
        r"security code[:\s]+(\d{6,8})",
    ]
    for pattern in patterns:
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            code = re.sub(r"\D", "", match.group(1))
            if len(code) in {6, 8}:
                return code
    return None


def send_notification_email(config: DownloaderConfig, success: bool, details: str = "") -> None:
    if not config.gmail_email or not config.gmail_app_password:
        logger.warning("Skipping notification email because Gmail credentials are missing")
        return

    msg = MIMEMultipart()
    msg["From"] = config.gmail_email
    msg["To"] = config.gmail_email
    msg["Subject"] = (
        f"[Avianca Downloader] {'SUCCESS' if success else 'FAILED'} - "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    msg.attach(MIMEText(details, "plain"))

    server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
    server.login(config.gmail_email, config.gmail_app_password)
    server.send_message(msg)
    server.quit()


def login_to_avianca(
    driver: webdriver.Chrome,
    config: DownloaderConfig,
    cancel_callback: CancelCallback | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    try:
        check_cancelled(cancel_callback)
        logger.info("Navigating to Avianca iCargo login page")
        emit(progress_callback, "Opening Avianca login page", 9)
        driver.get(LOGIN_URL)
        wait = WebDriverWait(driver, 20)

        emit(progress_callback, "Looking for login email field", 10)
        try:
            email_field = wait.until(EC.presence_of_element_located((By.ID, "cred_userid_inputtext")))
        except TimeoutException:
            email_field = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@type='email']")))
        email_field.clear()
        email_field.send_keys(config.avianca_email)
        emit(progress_callback, "Email entered; looking for Next button", 11)

        try:
            next_button = wait.until(EC.element_to_be_clickable((By.ID, "idSIButton9")))
        except TimeoutException:
            next_button = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Next')]")))

        code_requested_at = datetime.now(timezone.utc) - timedelta(seconds=20)
        next_button.click()
        emit(progress_callback, "Verification code requested from Microsoft", 12)

        time.sleep(3)
        code = None
        for attempt in range(1, 4):
            check_cancelled(cancel_callback)
            try:
                logger.info("Waiting for verification code attempt %s/3", attempt)
                emit(progress_callback, f"Waiting for verification code attempt {attempt}/3", 12)
                code = get_verification_code_from_email(
                    config,
                    timeout_seconds=300,
                    cancel_callback=cancel_callback,
                    requested_after=code_requested_at,
                    progress_callback=progress_callback,
                )
                break
            except TimeoutException:
                if attempt == 3:
                    raise
                emit(progress_callback, "Verification code timed out; trying email scan again", 12)
                time.sleep(5)

        if not code:
            raise RuntimeError("Could not retrieve verification code")

        try:
            code_field = wait.until(EC.presence_of_element_located((By.ID, "idTxtBx_OTC_Password")))
        except TimeoutException:
            code_field = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@placeholder='Code']")))
        emit(progress_callback, "Entering verification code", 15)
        code_field.send_keys(code)

        signin_button = wait.until(EC.element_to_be_clickable((By.ID, "idSIButton9")))
        signin_button.click()
        emit(progress_callback, "Verification submitted; waiting for iCargo", 16)

        try:
            stay_signed_in_no = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.ID, "idBtn_Back")))
            stay_signed_in_no.click()
        except TimeoutException:
            pass

        time.sleep(6)
        if "showMainPage" not in driver.current_url and "icargo" not in driver.current_url:
            logger.error("Login may have failed. Current URL: %s", driver.current_url)
            emit(progress_callback, f"Login redirect did not reach iCargo: {driver.current_url}", 16)
            return False

        all_windows = driver.window_handles
        if len(all_windows) > 1:
            driver.switch_to.window(all_windows[-1])
            time.sleep(2)

        return True
    except Exception as exc:
        logger.exception("Login failed")
        emit(progress_callback, f"Login failed: {exc}", 16)
        return False


def switch_to_comm_role(driver: webdriver.Chrome) -> bool:
    try:
        wait = WebDriverWait(driver, 30)
        time.sleep(3)

        more_menu = wait.until(EC.presence_of_element_located((By.XPATH, "//span[contains(@class, 'ic-toggle-menu')]")))
        driver.execute_script("arguments[0].click();", more_menu)
        time.sleep(2)

        switch_role = wait.until(
            EC.presence_of_element_located((By.XPATH, "//span[contains(@class, 'ic-switch-role')]//a"))
        )
        driver.execute_script("arguments[0].click();", switch_role)
        time.sleep(4)

        iframe = wait.until(EC.presence_of_element_located((By.ID, "swichRoleiframe")))
        driver.switch_to.frame(iframe)

        result = driver.execute_script(
            """
            const select = document.querySelector('[name="selectedStationRoleGroup"]') ||
                document.querySelector('#CMB_ADMIN_USER_SWITCHROLES_LISTROLES');
            if (!select) return 'ERROR';
            select.value = 'COMM_ARL_N';
            select.dispatchEvent(new Event('change', { bubbles: true }));
            return 'SUCCESS';
            """
        )
        if result != "SUCCESS":
            driver.switch_to.default_content()
            return False

        time.sleep(2)
        click_result = driver.execute_script(
            """
            const okButton = document.querySelector('#CMB_ADMIN_USER_SWITCHROLES_OK_BUTTON') ||
                document.querySelector('button[name="btnOK"]') ||
                document.querySelector('button[type="button"]');
            if (!okButton) return 'ERROR';
            okButton.click();
            return 'OK';
            """
        )
        if click_result != "OK":
            driver.execute_script("document.body.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter'}))")

        driver.switch_to.default_content()
        time.sleep(5)
        return True
    except Exception:
        logger.exception("Role switch failed")
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        return False


def navigate_to_screen(driver: webdriver.Chrome, screen_num: str = "TRF007") -> bool:
    try:
        screen_num = screen_num.upper()
        driver.switch_to.default_content()
        wait = WebDriverWait(driver, 20)
        screen_field = wait.until(EC.presence_of_element_located((By.ID, "ic-screen-search")))
        screen_field.clear()
        screen_field.send_keys(screen_num)
        time.sleep(1)
        screen_field.send_keys(Keys.RETURN)

        wait.until(EC.presence_of_element_located((By.NAME, f"iCargoContentFrame{screen_num}")))
        time.sleep(2)
        return True
    except Exception:
        logger.exception("Could not navigate to screen %s", screen_num)
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        return False


def enter_screen_frame(driver: webdriver.Chrome, screen_num: str, timeout: int = 20) -> WebDriverWait:
    driver.switch_to.default_content()
    wait = WebDriverWait(driver, timeout)
    iframe = wait.until(EC.presence_of_element_located((By.NAME, f"iCargoContentFrame{screen_num.upper()}")))
    driver.switch_to.frame(iframe)
    return wait


def enter_trf007_frame(driver: webdriver.Chrome, timeout: int = 20) -> WebDriverWait:
    return enter_screen_frame(driver, "TRF007", timeout=timeout)


def enter_cap142_frame(driver: webdriver.Chrome, timeout: int = 20) -> WebDriverWait:
    return enter_screen_frame(driver, "CAP142", timeout=timeout)


def wait_for_icargo_idle(
    driver: webdriver.Chrome,
    timeout: int = 45,
    cancel_callback: CancelCallback | None = None,
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        check_cancelled(cancel_callback)
        try:
            is_idle = driver.execute_script(
                """
                const selectors = [
                    '.blockUI',
                    '.blockOverlay',
                    '.loading',
                    '.spinner',
                    '[id*="loading" i]',
                    '[class*="loading" i]',
                    '[class*="progress" i]'
                ];
                function visible(el) {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        Number(style.opacity || 1) > 0 &&
                        rect.width > 0 &&
                        rect.height > 0;
                }
                return selectors.every((selector) =>
                    Array.from(document.querySelectorAll(selector)).every((el) => !visible(el))
                );
                """
            )
            if is_idle:
                return True
        except Exception:
            return True
        time.sleep(0.5)
    return False


def visible_export_link(driver: webdriver.Chrome):
    return driver.execute_script(
        """
        function visible(el) {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none' &&
                style.visibility !== 'hidden' &&
                Number(style.opacity || 1) > 0 &&
                rect.width > 0 &&
                rect.height > 0 &&
                !el.disabled;
        }
        const candidates = [
            document.querySelector('#exportToExcelLink'),
            ...Array.from(document.querySelectorAll('a, button, input')).filter((el) => {
                const text = [
                    el.innerText,
                    el.textContent,
                    el.value,
                    el.title,
                    el.id,
                    el.name,
                    el.getAttribute('aria-label'),
                    el.getAttribute('href'),
                    el.getAttribute('src'),
                    el.className
                ].join(' ');
                return /excel|export/i.test(text);
            })
        ].filter(Boolean);
        return candidates.find(visible) || null;
        """
    )


def click_export_link(driver: webdriver.Chrome, export_link) -> str:
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", export_link)
    time.sleep(0.5)

    try:
        export_link.click()
        return "browser click"
    except Exception as first_exc:
        logger.info("Normal export click failed, trying ActionChains click: %s", first_exc)

    try:
        ActionChains(driver).move_to_element(export_link).pause(0.2).click().perform()
        return "action click"
    except Exception as second_exc:
        logger.info("ActionChains export click failed, trying JavaScript click: %s", second_exc)

    driver.execute_script("arguments[0].click();", export_link)
    return "javascript click"


def cap142_set_search_fields(
    driver: webdriver.Chrome,
    *,
    mode: str,
    origin: str,
    origin_type: str,
    start_date: str,
    end_date: str,
    awb_prefix: str,
    flight_carrier: str,
    flight_number: str,
    cancel_callback: CancelCallback | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    last_result = None
    deadline = time.time() + 45
    last_progress_at = 0.0

    try:
        while time.time() < deadline:
            check_cancelled(cancel_callback)
            enter_cap142_frame(driver, timeout=20)
            wait_for_icargo_idle(driver, timeout=20, cancel_callback=cancel_callback)

            result = driver.execute_script(
                """
                const values = arguments[0];

                function visible(el) {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        Number(style.opacity || 1) > 0 &&
                        rect.width > 0 &&
                        rect.height > 0;
                }

                function sortByPosition(a, b) {
                    const ar = a.getBoundingClientRect();
                    const br = b.getBoundingClientRect();
                    if (Math.abs(ar.top - br.top) > 10) return ar.top - br.top;
                    return ar.left - br.left;
                }

                function editableInputs() {
                    const ignoredTypes = new Set([
                        'button',
                        'checkbox',
                        'file',
                        'hidden',
                        'image',
                        'radio',
                        'reset',
                        'submit'
                    ]);
                    return Array.from(document.querySelectorAll('input, textarea'))
                        .filter((el) => visible(el) && !ignoredTypes.has(String(el.type || '').toLowerCase()))
                        .sort(sortByPosition);
                }

                function visibleSelects() {
                    return Array.from(document.querySelectorAll('select'))
                        .filter(visible)
                        .sort(sortByPosition);
                }

                function setInput(el, value) {
                    if (!el) return false;
                    const nextValue = value || '';
                    el.focus();
                    try {
                        const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                        const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
                        if (descriptor && descriptor.set) {
                            descriptor.set.call(el, nextValue);
                        } else {
                            el.value = nextValue;
                        }
                    } catch (error) {
                        el.value = nextValue;
                    }
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                    return true;
                }

                function setSelect(el, wanted) {
                    if (!el) return false;
                    const normalized = String(wanted || '').toLowerCase();
                    const option = Array.from(el.options || []).find((item) => {
                        return String(item.textContent || '').trim().toLowerCase() === normalized ||
                            String(item.value || '').trim().toLowerCase() === normalized;
                    }) || Array.from(el.options || []).find((item) => {
                        return String(item.textContent || '').toLowerCase().includes(normalized);
                    });
                    if (!option) return false;
                    el.value = option.value;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                    return true;
                }

                function describe(el, index) {
                    const rect = el.getBoundingClientRect();
                    return {
                        index,
                        tag: el.tagName,
                        type: el.type || '',
                        id: el.id || '',
                        name: el.name || '',
                        title: el.title || '',
                        value: el.value || '',
                        top: Math.round(rect.top),
                        left: Math.round(rect.left)
                    };
                }

                const textControls = editableInputs();
                const selectControls = visibleSelects();
                const bodyText = (document.body.innerText || '').slice(0, 1000);

                if (!/AWB\\s+Number/i.test(bodyText) || !/Flight\\s+No/i.test(bodyText)) {
                    return {
                        ok: false,
                        retryable: true,
                        error: 'CAP142 search form is not visible yet',
                        textCount: textControls.length,
                        selectCount: selectControls.length,
                        bodyText
                    };
                }

                if (textControls.length < 9) {
                    return {
                        ok: false,
                        retryable: true,
                        error: `Expected at least 9 editable CAP142 fields, found ${textControls.length}`,
                        textCount: textControls.length,
                        selectCount: selectControls.length,
                        controls: textControls.slice(0, 14).map(describe),
                        selects: selectControls.slice(0, 4).map(describe)
                    };
                }

                if (selectControls.length < 1) {
                    return {
                        ok: false,
                        retryable: true,
                        error: 'Could not find CAP142 origin type dropdown',
                        textCount: textControls.length,
                        selectCount: selectControls.length,
                        controls: textControls.slice(0, 14).map(describe),
                        selects: selectControls.slice(0, 4).map(describe)
                    };
                }

                setInput(textControls[0], values.awbPrefix);
                setInput(textControls[1], '');

                if (values.mode === 'specific_flight') {
                    setInput(textControls[2], values.flightCarrier);
                    setInput(textControls[3], values.flightNumber);
                    setInput(textControls[4], values.startDate);
                    setInput(textControls[5], values.endDate);
                    setInput(textControls[6], '');
                    setInput(textControls[7], '');
                } else {
                    setInput(textControls[2], '');
                    setInput(textControls[3], '');
                    setInput(textControls[4], '');
                    setInput(textControls[5], '');
                    setInput(textControls[6], values.startDate);
                    setInput(textControls[7], values.endDate);
                }

                if (!setSelect(selectControls[0], values.originType)) {
                    return {
                        ok: false,
                        retryable: false,
                        error: `Could not set origin type ${values.originType}`,
                        textCount: textControls.length,
                        selectCount: selectControls.length,
                        selects: selectControls.slice(0, 4).map(describe)
                    };
                }

                setInput(textControls[8], values.origin);

                return {
                    ok: true,
                    textControls: textControls.slice(0, 12).map(describe),
                    selects: selectControls.slice(0, 4).map(describe)
                };
                """,
                {
                    "mode": mode,
                    "origin": origin,
                    "originType": origin_type,
                    "startDate": start_date,
                    "endDate": end_date,
                    "awbPrefix": awb_prefix,
                    "flightCarrier": flight_carrier,
                    "flightNumber": flight_number,
                },
            )

            driver.switch_to.default_content()
            last_result = result
            if result and result.get("ok"):
                if progress_callback:
                    if mode == "specific_flight":
                        emit(
                            progress_callback,
                            f"{origin}: CAP142 fields entered (flight {flight_carrier}{flight_number}, dates {start_date} to {end_date}, origin {origin})",
                            None,
                        )
                    else:
                        emit(
                            progress_callback,
                            f"{origin}: CAP142 fields entered (AWB {awb_prefix}, flight blank, booking {start_date} to {end_date}, origin {origin})",
                            None,
                        )
                time.sleep(1)
                return True

            if result and not result.get("retryable", True):
                break

            now = time.time()
            if progress_callback and now - last_progress_at >= 12:
                error = result.get("error", "waiting for CAP142 form") if isinstance(result, dict) else "waiting for CAP142 form"
                emit(progress_callback, f"{origin}: waiting for CAP142 fields ({error})", None)
                last_progress_at = now
            time.sleep(2)

        logger.error("CAP142 field setup failed: %s", last_result)
        if progress_callback:
            if isinstance(last_result, dict):
                error = last_result.get("error", "field setup failed")
            else:
                error = "field setup failed"
            emit(progress_callback, f"{origin}: CAP142 field setup failed ({error})", None)
        return False
    except Exception:
        logger.exception("Could not set CAP142 search fields for %s", origin)
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        return False


def cap142_click_list(
    driver: webdriver.Chrome,
    origin: str,
    cancel_callback: CancelCallback | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    try:
        check_cancelled(cancel_callback)
        wait = enter_cap142_frame(driver, timeout=20)
        wait_for_icargo_idle(driver, timeout=20, cancel_callback=cancel_callback)

        list_button = wait.until(
            lambda current_driver: current_driver.execute_script(
                """
                function visible(el) {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        Number(style.opacity || 1) > 0 &&
                        rect.width > 0 &&
                        rect.height > 0 &&
                        !el.disabled;
                }
                return Array.from(document.querySelectorAll('button, input, a'))
                    .filter(visible)
                    .find((el) => {
                        const text = [
                            el.innerText,
                            el.textContent,
                            el.value,
                            el.title,
                            el.id,
                            el.name,
                            el.className
                        ].join(' ');
                        return /\\blist\\b/i.test(text);
                    }) || null;
                """
            )
        )
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", list_button)
        time.sleep(0.5)
        driver.execute_script("arguments[0].click();", list_button)
        emit(progress_callback, f"{origin}: CAP142 query submitted; waiting for results", None)

        if not wait_for_query_results(
            driver,
            origin,
            timeout=120,
            cancel_callback=cancel_callback,
            progress_callback=progress_callback,
        ):
            driver.switch_to.default_content()
            return False

        emit(progress_callback, f"{origin}: CAP142 results are ready", None)
        driver.switch_to.default_content()
        return True
    except Exception:
        logger.exception("Could not execute CAP142 query for %s", origin)
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        return False


def wait_for_query_results(
    driver: webdriver.Chrome,
    airport: str,
    timeout: int = 90,
    cancel_callback: CancelCallback | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    start_time = time.time()
    last_progress_at = start_time
    no_results_since = None

    while time.time() - start_time < timeout:
        check_cancelled(cancel_callback)
        wait_for_icargo_idle(driver, timeout=3, cancel_callback=cancel_callback)
        now = time.time()

        try:
            if visible_export_link(driver):
                return True

            no_results = driver.execute_script(
                """
                const text = (document.body.innerText || '').toLowerCase();
                return /no\\s+records|no\\s+data|no\\s+result|no\\s+spot\\s+rate/.test(text);
                """
            )
            if no_results:
                if no_results_since is None:
                    no_results_since = now
                    emit(progress_callback, f"{airport}: Avianca says no rows; checking again", None)
                elif now - no_results_since >= 12:
                    emit(progress_callback, f"{airport}: Avianca returned no rows", None)
                    return False
            else:
                no_results_since = None
        except Exception:
            pass

        if now - last_progress_at >= 15:
            elapsed = int(now - start_time)
            emit(progress_callback, f"{airport}: still waiting for Avianca results ({elapsed}s)", None)
            last_progress_at = now
        time.sleep(1)

    return False


def set_date_range_and_airport(
    driver: webdriver.Chrome,
    origin: str,
    is_first_airport: bool,
    start_date: str,
    end_date: str,
    cancel_callback: CancelCallback | None = None,
) -> bool:
    try:
        check_cancelled(cancel_callback)
        wait = enter_trf007_frame(driver, timeout=20)
        wait_for_icargo_idle(driver, timeout=20, cancel_callback=cancel_callback)

        if is_first_airport or start_date or end_date:
            driver.execute_script(
                """
                const fromDate = arguments[0];
                const toDate = arguments[1];
                const fromField = document.querySelector('#fromdate');
                if (fromField) {
                    fromField.value = fromDate;
                    fromField.dispatchEvent(new Event('change', { bubbles: true }));
                    fromField.dispatchEvent(new Event('blur', { bubbles: true }));
                }
                const toField = document.querySelector('#todate');
                if (toField) {
                    toField.value = toDate;
                    toField.dispatchEvent(new Event('change', { bubbles: true }));
                    toField.dispatchEvent(new Event('blur', { bubbles: true }));
                }
                """,
                start_date,
                end_date,
            )
            time.sleep(2)

        check_cancelled(cancel_callback)
        origin_field = wait.until(
            EC.presence_of_element_located((By.ID, "CMP_Tariff_Freight_ListSpotRateRequests_Origin"))
        )
        driver.execute_script(
            """
            const field = arguments[0];
            const origin = arguments[1];
            field.value = '';
            field.dispatchEvent(new Event('change', { bubbles: true }));
            field.value = origin;
            field.dispatchEvent(new Event('change', { bubbles: true }));
            field.dispatchEvent(new Event('blur', { bubbles: true }));
            """,
            origin_field,
            origin,
        )
        time.sleep(1)
        driver.switch_to.default_content()
        return True
    except Exception:
        logger.exception("Could not set parameters for %s", origin)
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        return False


def execute_tariff_query(
    driver: webdriver.Chrome,
    airport: str,
    cancel_callback: CancelCallback | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    try:
        check_cancelled(cancel_callback)
        wait = enter_trf007_frame(driver, timeout=20)
        wait_for_icargo_idle(driver, timeout=20, cancel_callback=cancel_callback)

        list_button = wait.until(
            EC.element_to_be_clickable((By.ID, "CMP_Tariff_Freight_ListSpotRateRequests_List"))
        )
        driver.execute_script("arguments[0].scrollIntoView(true);", list_button)
        time.sleep(0.5)
        driver.execute_script("arguments[0].click();", list_button)
        emit(progress_callback, f"{airport}: query submitted; waiting for results", None)

        if not wait_for_query_results(
            driver,
            airport,
            timeout=90,
            cancel_callback=cancel_callback,
            progress_callback=progress_callback,
        ):
            driver.switch_to.default_content()
            return False

        emit(progress_callback, f"{airport}: results are ready", None)
        driver.switch_to.default_content()
        return True
    except Exception:
        logger.exception("Could not execute tariff query")
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        return False


def download_results_as_excel(
    driver: webdriver.Chrome,
    download_dir: Path,
    airport: str,
    cancel_callback: CancelCallback | None = None,
    progress_callback: ProgressCallback | None = None,
    screen_num: str = "TRF007",
) -> Path | None:
    try:
        existing_names = {
            path.name
            for suffix in EXPORT_FILE_SUFFIXES
            for path in download_dir.glob(f"*{suffix}")
        }

        check_cancelled(cancel_callback)
        wait = enter_screen_frame(driver, screen_num, timeout=20)
        wait_for_icargo_idle(driver, timeout=20, cancel_callback=cancel_callback)

        try:
            export_link = wait.until(lambda current_driver: visible_export_link(current_driver))
        except TimeoutException:
            driver.switch_to.default_content()
            emit(progress_callback, f"{airport}: export link did not appear", None)
            return None

        emit(progress_callback, f"{airport}: export is visible; waiting {EXPORT_SETTLE_SECONDS}s before click", None)
        driver.switch_to.default_content()
        for _ in range(EXPORT_SETTLE_SECONDS):
            check_cancelled(cancel_callback)
            time.sleep(1)

        wait = enter_screen_frame(driver, screen_num, timeout=20)
        export_link = wait.until(lambda current_driver: visible_export_link(current_driver))
        click_time = time.time()
        click_method = click_export_link(driver, export_link)
        driver.switch_to.default_content()
        emit(progress_callback, f"{airport}: export clicked ({click_method}); waiting for Excel file", None)

        downloaded_file = wait_for_new_download(
            download_dir,
            existing_names,
            click_time,
            timeout=60,
            cancel_callback=cancel_callback,
            progress_callback=progress_callback,
            airport=airport,
        )
        if downloaded_file:
            return downloaded_file

        return None
    except Exception:
        logger.exception("Could not export results to Excel")
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        return None


def wait_for_new_download(
    download_dir: Path,
    existing_names: set[str],
    start_time: float,
    timeout: int = 90,
    cancel_callback: CancelCallback | None = None,
    progress_callback: ProgressCallback | None = None,
    airport: str | None = None,
) -> Path | None:
    deadline = time.time() + timeout
    start_wait = time.time()
    last_progress_at = start_wait
    partial_seen = False

    while time.time() < deadline:
        check_cancelled(cancel_callback)
        partials = (
            list(download_dir.glob("*.crdownload"))
            + list(download_dir.glob("*.tmp"))
            + list(download_dir.glob("*.download"))
        )
        if partials:
            partial_seen = True
            time.sleep(1)
            continue

        candidates = []
        for suffix in EXPORT_FILE_SUFFIXES:
            for path in download_dir.glob(f"*{suffix}"):
                try:
                    is_new_name = path.name not in existing_names
                    stat = path.stat()
                    is_recent = stat.st_mtime >= start_time
                    has_content = stat.st_size > 0
                    if (is_new_name or is_recent) and has_content:
                        candidates.append(path)
                except FileNotFoundError:
                    continue

        if candidates:
            newest = max(candidates, key=lambda item: item.stat().st_mtime)
            try:
                first_size = newest.stat().st_size
                time.sleep(0.75)
                second_size = newest.stat().st_size
                if first_size == second_size and second_size > 0:
                    return newest
            except FileNotFoundError:
                pass

        now = time.time()
        if progress_callback and airport and now - last_progress_at >= 20:
            elapsed = int(now - start_wait)
            if partial_seen:
                emit(progress_callback, f"{airport}: Excel download is still finishing ({elapsed}s)", None)
            else:
                emit(progress_callback, f"{airport}: still waiting for Excel file ({elapsed}s)", None)
            last_progress_at = now

        time.sleep(1)

    return None


def airport_export_filename(original_path: Path, airport: str, sequence: int) -> str:
    airport_code = re.sub(r"[^A-Z0-9]", "", airport.upper())[:8] or "AIRPORT"
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", original_path.stem).strip("._") or "export"
    suffix = original_path.suffix.lower() if original_path.suffix.lower() in EXPORT_FILE_SUFFIXES else ".xlsx"
    return f"{sequence:02d}_{airport_code}_{stem}{suffix}"


def rename_airport_export(downloaded_file: Path, airport: str, sequence: int) -> Path:
    target_dir = downloaded_file.parent
    target_path = target_dir / airport_export_filename(downloaded_file, airport, sequence)

    counter = 2
    while target_path.exists() and target_path.resolve() != downloaded_file.resolve():
        target_path = target_dir / (
            f"{sequence:02d}_{airport.upper()}_{downloaded_file.stem}_{counter}{downloaded_file.suffix}"
        )
        counter += 1

    if target_path.resolve() == downloaded_file.resolve():
        return downloaded_file

    downloaded_file.rename(target_path)
    logger.info("Renamed export %s to %s", downloaded_file.name, target_path.name)
    return target_path


def merge_excel_files(
    file_directory: str | Path,
    output_filename: str = "merged_tariffs.xlsx",
    source_column: str = "Source Airport",
) -> Path | None:
    target_dir = Path(file_directory)
    output_path = target_dir / output_filename

    excel_files = sorted(
        path
        for suffix in EXPORT_FILE_SUFFIXES
        for path in target_dir.glob(f"*{suffix}")
        if path.name != output_filename and not path.name.startswith("~$")
    )
    if not excel_files:
        logger.error("No Excel files found to merge in %s", target_dir)
        return None

    dataframes = []
    for excel_file in excel_files:
        try:
            df = pd.read_excel(excel_file)
            source_match = re.match(r"^\d{2}_([A-Z0-9]{3,8})_", excel_file.name)
            if source_match and source_column not in df.columns:
                df.insert(0, source_column, source_match.group(1))
            dataframes.append(df)
            logger.info("Loaded %s (%s rows)", excel_file.name, len(df))
        except Exception as exc:
            logger.warning("Could not read %s: %s", excel_file.name, exc)

    if not dataframes:
        logger.error("No readable Excel files found in %s", target_dir)
        return None

    merged_df = pd.concat(dataframes, ignore_index=True).dropna(how="all")
    merged_df.to_excel(output_path, index=False, sheet_name="Data")
    logger.info("Merged file saved to %s", output_path)

    for excel_file in excel_files:
        try:
            excel_file.unlink()
        except Exception as exc:
            logger.warning("Could not delete %s: %s", excel_file.name, exc)

    return output_path


def upload_to_dropbox(config: DownloaderConfig, file_path: str | Path) -> bool:
    if not DROPBOX_AVAILABLE:
        logger.warning("Dropbox SDK is not installed; skipping upload")
        return True
    if not config.dropbox_access_token:
        logger.info("DROPBOX_TOKEN is not configured; skipping upload")
        return True

    try:
        file_path = Path(file_path)
        dbx = dropbox.Dropbox(config.dropbox_access_token)
        dbx.users_get_current_account()
        destination = f"{config.dropbox_upload_path}/{file_path.name}"
        with file_path.open("rb") as handle:
            dbx.files_upload(handle.read(), destination, autorename=True)
        logger.info("Uploaded merged file to Dropbox: %s", destination)
        return True
    except Exception:
        logger.exception("Dropbox upload failed")
        return False


def run_cap142_workflow(
    airports: Iterable[str] | None = None,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    download_dir: str | Path | None = None,
    upload_dropbox: bool = False,
    send_email: bool = False,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
    cap142_mode: str = "booking_period",
    flight_carrier: str = "QT",
    flight_number: str | None = None,
    awb_prefix: str = "729",
    origin_type: str = "Airport",
) -> dict:
    config = DownloaderConfig.from_env(download_dir=download_dir)
    config.validate()

    mode = (cap142_mode or "booking_period").strip().lower()
    if mode not in CAP142_MODES:
        raise ValueError("CAP142 mode must be specific_flight or booking_period")

    selected_origins = normalize_airports(airports)
    if mode == "specific_flight" and len(selected_origins) != 1:
        raise ValueError("Specific flight download needs exactly one origin airport")

    if origin_type.strip().lower() != "airport":
        raise ValueError("CAP142 country-origin automation is not enabled yet. Use Airport for now.")

    awb_prefix = re.sub(r"\D", "", awb_prefix or "729") or "729"
    if mode == "specific_flight":
        flight_carrier = (flight_carrier or "QT").strip().upper()
    else:
        flight_carrier = ""
        flight_number = ""
    flight_number = (flight_number or "").strip()
    if mode == "specific_flight" and not flight_number:
        raise ValueError("Flight number is required for CAP142 specific flight")

    icargo_start_date, icargo_end_date = validate_date_range(start_date, end_date)
    config.download_dir.mkdir(parents=True, exist_ok=True)

    file_handler = add_file_logger(config.log_dir)
    driver = None
    successful_downloads = 0
    failed_downloads = 0
    downloaded_files = []

    try:
        check_cancelled(cancel_callback)
        emit(progress_callback, "Starting Avianca CAP142 booking download", 3)
        if mode == "specific_flight":
            emit(progress_callback, f"Mode: specific flight {flight_carrier}{flight_number}", 4)
            emit(progress_callback, f"Flight date: {icargo_start_date} to {icargo_end_date}", 5)
        else:
            emit(progress_callback, f"Mode: booking period for AWB prefix {awb_prefix}", 4)
            emit(progress_callback, f"Booking date range: {icargo_start_date} to {icargo_end_date}", 5)
        emit(progress_callback, f"Origins selected: {', '.join(selected_origins)}", 6)

        driver = prepare_icargo_session(
            config=config,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
            max_attempts=2,
            screen_num="CAP142",
        )

        total_origins = len(selected_origins)
        for index, origin in enumerate(selected_origins, start=1):
            check_cancelled(cancel_callback)
            base_progress = 30 + int(((index - 1) / total_origins) * 50)
            emit(progress_callback, f"Processing {origin} ({index}/{total_origins})", base_progress)

            downloaded_file = None
            last_failure = "download did not complete"
            max_origin_attempts = 3

            for attempt in range(1, max_origin_attempts + 1):
                check_cancelled(cancel_callback)
                if attempt > 1:
                    emit(
                        progress_callback,
                        f"{origin}: retrying from a fresh CAP142 screen ({attempt}/{max_origin_attempts})",
                        base_progress,
                    )
                    if not navigate_to_screen(driver, "CAP142"):
                        last_failure = "could not reopen CAP142"
                        continue

                emit(progress_callback, f"{origin}: setting CAP142 search fields", base_progress + 1)
                if not cap142_set_search_fields(
                    driver,
                    mode=mode,
                    origin=origin,
                    origin_type=origin_type,
                    start_date=icargo_start_date,
                    end_date=icargo_end_date,
                    awb_prefix=awb_prefix,
                    flight_carrier=flight_carrier,
                    flight_number=flight_number,
                    cancel_callback=cancel_callback,
                    progress_callback=progress_callback,
                ):
                    last_failure = "failed to set CAP142 search fields"
                    continue

                emit(progress_callback, f"{origin}: running CAP142 query", base_progress + 1)
                if not cap142_click_list(
                    driver,
                    origin,
                    cancel_callback=cancel_callback,
                    progress_callback=progress_callback,
                ):
                    last_failure = "CAP142 query failed or returned no export"
                    continue

                emit(progress_callback, f"{origin}: exporting CAP142 Excel", base_progress + 2)
                downloaded_file = download_results_as_excel(
                    driver,
                    config.download_dir,
                    airport=origin,
                    cancel_callback=cancel_callback,
                    progress_callback=progress_callback,
                    screen_num="CAP142",
                )
                if downloaded_file:
                    break

                last_failure = "CAP142 export failed or timed out"
                if attempt < max_origin_attempts:
                    emit(progress_callback, f"{origin}: no Excel file appeared; retrying full CAP142 query", base_progress)

            if not downloaded_file:
                failed_downloads += 1
                emit(progress_callback, f"{origin}: {last_failure}", base_progress)
                continue

            downloaded_file = rename_airport_export(downloaded_file, origin, index)
            downloaded_files.append(downloaded_file)
            successful_downloads += 1
            emit(progress_callback, f"{origin}: downloaded {downloaded_file.name}", base_progress + 3)
            for _ in range(4):
                check_cancelled(cancel_callback)
                time.sleep(0.5)

        if successful_downloads == 0:
            raise RuntimeError("No CAP142 exports were downloaded")

        check_cancelled(cancel_callback)
        if mode == "specific_flight" and len(downloaded_files) == 1:
            merged_file = downloaded_files[0]
            emit(progress_callback, "CAP142 file is ready", 100)
        elif len(downloaded_files) == 1:
            merged_file = downloaded_files[0]
            emit(progress_callback, "Single CAP142 file is ready", 100)
        else:
            emit(progress_callback, "Merging CAP142 Excel exports", 84)
            output_name = f"CAP142_{successful_downloads}origins_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            merged_file = merge_excel_files(
                config.download_dir,
                output_filename=output_name,
                source_column="Source Origin",
            )
            if not merged_file:
                raise RuntimeError("CAP142 downloaded files could not be merged")

        if upload_dropbox:
            emit(progress_callback, "Uploading CAP142 file to Dropbox", 92)
            upload_to_dropbox(config, merged_file)

        result = {
            "merged_file": str(merged_file),
            "successful_downloads": successful_downloads,
            "failed_downloads": failed_downloads,
            "airports": selected_origins,
            "start_date": icargo_start_date,
            "end_date": icargo_end_date,
            "module": "CAP142",
            "cap142_mode": mode,
        }

        if send_email:
            send_notification_email(config, success=True, details=json.dumps(result, indent=2))

        emit(progress_callback, "CAP142 download workflow completed", 100)
        return result

    except Exception as exc:
        if send_email:
            try:
                send_notification_email(config, success=False, details=str(exc))
            except Exception:
                logger.exception("Failed to send CAP142 failure notification email")
        raise
    finally:
        if driver:
            driver.quit()
            logger.info("Browser closed")
        logger.removeHandler(file_handler)
        file_handler.close()


def prepare_icargo_session(
    config: DownloaderConfig,
    progress_callback: ProgressCallback | None,
    cancel_callback: CancelCallback | None,
    max_attempts: int = 2,
    screen_num: str = "TRF007",
) -> webdriver.Chrome:
    last_error = None

    for attempt in range(1, max_attempts + 1):
        driver = None
        try:
            check_cancelled(cancel_callback)
            if attempt > 1:
                emit(progress_callback, f"Retrying Avianca login setup ({attempt}/{max_attempts})", 7)

            driver = init_selenium_driver(config)
            emit(progress_callback, "Chrome started", 8)

            check_cancelled(cancel_callback)
            if not login_to_avianca(
                driver,
                config,
                cancel_callback=cancel_callback,
                progress_callback=progress_callback,
            ):
                raise RuntimeError("Failed to login to Avianca iCargo")
            emit(progress_callback, "Logged in to iCargo", 18)

            check_cancelled(cancel_callback)
            if not switch_to_comm_role(driver):
                raise RuntimeError("Failed to switch to COMM role")
            emit(progress_callback, "Switched to COMM role", 23)

            check_cancelled(cancel_callback)
            if not navigate_to_screen(driver, screen_num):
                raise RuntimeError(f"Failed to navigate to {screen_num}")
            emit(progress_callback, f"Opened {screen_num}", 28)

            return driver

        except WorkflowCancelled:
            if driver:
                driver.quit()
            raise
        except Exception as exc:
            last_error = exc
            logger.exception("Avianca setup attempt %s/%s failed", attempt, max_attempts)
            emit(progress_callback, f"Avianca setup attempt {attempt}/{max_attempts} failed: {exc}", 7)
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

            if attempt < max_attempts:
                for remaining in range(10, 0, -1):
                    check_cancelled(cancel_callback)
                    if remaining in {10, 5, 1}:
                        emit(progress_callback, f"Retrying in {remaining}s", 7)
                    time.sleep(1)

    raise RuntimeError(f"Avianca setup failed after {max_attempts} attempts: {last_error}")


def run_download_workflow(
    airports: Iterable[str] | None = None,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    download_dir: str | Path | None = None,
    upload_dropbox: bool = False,
    send_email: bool = False,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
    module: str = "TRF007",
    cap142_mode: str = "booking_period",
    flight_carrier: str = "QT",
    flight_number: str | None = None,
    awb_prefix: str = "729",
    origin_type: str = "Airport",
) -> dict:
    module_code = (module or "TRF007").strip().upper()
    if module_code == "CAP142":
        return run_cap142_workflow(
            airports=airports,
            start_date=start_date,
            end_date=end_date,
            download_dir=download_dir,
            upload_dropbox=upload_dropbox,
            send_email=send_email,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
            cap142_mode=cap142_mode,
            flight_carrier=flight_carrier,
            flight_number=flight_number,
            awb_prefix=awb_prefix,
            origin_type=origin_type,
        )
    if module_code != "TRF007":
        raise ValueError(f"Unsupported module: {module}")

    config = DownloaderConfig.from_env(download_dir=download_dir)
    config.validate()

    selected_airports = normalize_airports(airports)
    icargo_start_date, icargo_end_date = validate_date_range(start_date, end_date)
    config.download_dir.mkdir(parents=True, exist_ok=True)

    file_handler = add_file_logger(config.log_dir)
    driver = None
    successful_downloads = 0
    failed_downloads = 0

    try:
        check_cancelled(cancel_callback)
        emit(progress_callback, "Starting Avianca TRF007 download", 3)
        emit(progress_callback, f"Airports selected: {', '.join(selected_airports)}", 5)
        emit(progress_callback, f"Date range: {icargo_start_date} to {icargo_end_date}", 6)

        driver = prepare_icargo_session(
            config=config,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
            max_attempts=2,
        )

        total_airports = len(selected_airports)
        for index, airport in enumerate(selected_airports, start=1):
            check_cancelled(cancel_callback)
            base_progress = 30 + int(((index - 1) / total_airports) * 50)
            emit(progress_callback, f"Processing {airport} ({index}/{total_airports})", base_progress)

            downloaded_file = None
            last_failure = "download did not complete"

            max_airport_attempts = 3
            for attempt in range(1, max_airport_attempts + 1):
                check_cancelled(cancel_callback)
                if attempt > 1:
                    emit(
                        progress_callback,
                        f"{airport}: retrying from a fresh TRF007 screen ({attempt}/{max_airport_attempts})",
                        base_progress,
                    )
                    if not navigate_to_screen(driver, "TRF007"):
                        last_failure = "could not reopen TRF007"
                        continue

                emit(progress_callback, f"{airport}: setting dates and origin", base_progress + 1)
                if not set_date_range_and_airport(
                    driver,
                    airport,
                    is_first_airport=True,
                    start_date=icargo_start_date,
                    end_date=icargo_end_date,
                    cancel_callback=cancel_callback,
                ):
                    last_failure = "failed to set query parameters"
                    continue

                emit(progress_callback, f"{airport}: running query", base_progress + 1)
                if not execute_tariff_query(
                    driver,
                    airport,
                    cancel_callback=cancel_callback,
                    progress_callback=progress_callback,
                ):
                    last_failure = "query failed or returned no export"
                    continue

                check_cancelled(cancel_callback)
                emit(progress_callback, f"{airport}: exporting Excel", base_progress + 2)
                downloaded_file = download_results_as_excel(
                    driver,
                    config.download_dir,
                    airport=airport,
                    cancel_callback=cancel_callback,
                    progress_callback=progress_callback,
                )
                if downloaded_file:
                    break

                last_failure = "export failed or timed out"
                if attempt < max_airport_attempts:
                    emit(progress_callback, f"{airport}: no Excel file appeared; retrying full airport query", base_progress)

            if not downloaded_file:
                failed_downloads += 1
                emit(progress_callback, f"{airport}: {last_failure}", base_progress)
                continue

            downloaded_file = rename_airport_export(downloaded_file, airport, index)
            successful_downloads += 1
            emit(progress_callback, f"{airport}: downloaded {downloaded_file.name}", base_progress + 3)
            for _ in range(4):
                check_cancelled(cancel_callback)
                time.sleep(0.5)

        if successful_downloads == 0:
            raise RuntimeError("No airport exports were downloaded")

        check_cancelled(cancel_callback)
        emit(progress_callback, "Merging Excel exports", 84)
        output_name = f"TRF007_{successful_downloads}airports_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        merged_file = merge_excel_files(config.download_dir, output_filename=output_name)
        if not merged_file:
            raise RuntimeError("Downloaded files could not be merged")

        if upload_dropbox:
            emit(progress_callback, "Uploading merged file to Dropbox", 92)
            upload_to_dropbox(config, merged_file)

        result = {
            "merged_file": str(merged_file),
            "successful_downloads": successful_downloads,
            "failed_downloads": failed_downloads,
            "airports": selected_airports,
            "start_date": icargo_start_date,
            "end_date": icargo_end_date,
        }

        if send_email:
            send_notification_email(
                config,
                success=True,
                details=json.dumps(result, indent=2),
            )

        emit(progress_callback, "Download workflow completed", 100)
        return result

    except Exception as exc:
        if send_email:
            try:
                send_notification_email(config, success=False, details=str(exc))
            except Exception:
                logger.exception("Failed to send failure notification email")
        raise
    finally:
        if driver:
            driver.quit()
            logger.info("Browser closed")
        logger.removeHandler(file_handler)
        file_handler.close()


def split_airports(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part.strip().upper() for part in value.split(",") if part.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Avianca iCargo TRF007 downloader")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run a download immediately")
    run_parser.add_argument("--airports", help="Comma-separated airport codes. Defaults to all configured airports.")
    run_parser.add_argument("--start-date", help="Start date in YYYY-MM-DD format. Defaults to today.")
    run_parser.add_argument("--end-date", help="End date in YYYY-MM-DD format. Defaults to start date + 15 days.")
    run_parser.add_argument("--download-dir", help="Folder for exports and merged file.")
    run_parser.add_argument("--upload-dropbox", action="store_true", help="Upload the merged file to Dropbox.")
    run_parser.add_argument("--send-email", action="store_true", help="Send completion/failure email notification.")

    args = parser.parse_args()
    if args.command != "run":
        parser.print_help()
        return

    try:
        result = run_download_workflow(
            airports=split_airports(args.airports),
            start_date=args.start_date,
            end_date=args.end_date,
            download_dir=args.download_dir,
            upload_dropbox=args.upload_dropbox,
            send_email=args.send_email,
        )
        print(result["merged_file"])
    except Exception as exc:
        logger.error("Workflow failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
